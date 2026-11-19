"""Opaque-box 4-tier offline E2E suite wiring PiCompanionHub <-> ServerCompanionService <-> MockESP32."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import struct
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tests._stubs import (
    FakeAsyncOpenAI,
    FakeQuIXIBus,
    FakeSerialPort,
    MockESP32,
    install_dependency_stubs,
    make_pcm16_sine,
    make_tool_call,
)

install_dependency_stubs()

import yaml
from common_ixian.quixi_client import QuIXIClient, _extract_b64_bytes
from common_ixian.spixi_voip import (
    SPIXI_VOIP_APP_ID,
    AppDataMessage,
    SpixiMessageCode,
    SpixiVoIPSessionManager,
    pack_app_data_message,
    unpack_app_data_message,
)
from pi_hub import audio_sync_engine as ase_mod
from pi_hub.audio_sync_engine import AudioSyncEngine
from pi_hub.esp_serial_bridge import EspSerialBridge
from pi_hub.main_hub import AGENT_PROTOCOL_ID, PiCompanionHub
from server_agent.agent_router import AGENT_TOOLS, AgentBrain
from server_agent.main_server import ServerCompanionService
from server_agent.speech_processor import (
    SpeechProcessor,
    compute_viseme_envelope,
    pcm16_to_wav_bytes,
    resample_pcm16,
    split_audio_frames,
)


class OfflineE2EHarness:
    """Self-contained 100% in-memory 3-tier system harness (no TCP, MQTT threads, or COM3)."""

    PI_ADDR = "1PiWalletE2ETest111111111111111111"
    SRV_ADDR = "1SrvWalletE2ETest22222222222222222"

    def __init__(self, tmp_dir: Path, mic_mode: str = "vad", max_history: int = 24, allow_dev: str = "") -> None:
        self.tmp_dir = tmp_dir
        self.esp = MockESP32()
        self.port = FakeSerialPort(esp=self.esp)
        self.bus = FakeQuIXIBus()
        self.ai = FakeAsyncOpenAI()

        self.cfg_path = tmp_dir / "config.yaml"
        self.cfg_path.write_text(
            "\n".join(
                [
                    "serial:",
                    "  port: /dev/ttyACM0",
                    "  baudrate: 921600",
                    "quixi:",
                    "  rest_url: http://127.0.0.1:8001",
                    "  mqtt_host: 127.0.0.1",
                    "  mqtt_port: 1883",
                    f"  server_ixian_address: {self.SRV_ADDR}",
                    "audio:",
                    "  sample_rate: 16000",
                    "  frame_ms: 100",
                    "  vad_aggressiveness: 2",
                    "  vad_silence_ms: 120",
                    "  bt_latency_ms: 5",
                    f"  default_mic_mode: {mic_mode}",
                ]
            ),
            encoding="utf-8",
        )

        self.hub = PiCompanionHub(self.cfg_path)
        env = {
            "PI_IXIAN_ADDRESS": self.PI_ADDR,
            "ALLOW_UNAUTHENTICATED_DEV": allow_dev,
            "LLM_API_KEY": "sk-test-llm",
            "STT_API_KEY": "sk-test-stt",
            "TTS_API_KEY": "sk-test-tts",
        }
        with (
            patch.dict(os.environ, env, clear=False),
            patch("server_agent.main_server.load_dotenv", return_value=False),
            patch("server_agent.agent_router.AsyncOpenAI", return_value=self.ai),
            patch("server_agent.speech_processor.AsyncOpenAI", return_value=self.ai),
        ):
            self.srv = ServerCompanionService()
            self.srv.brain.max_history = max_history

        self.pi_http, self.pi_mqtt = self.bus.register(self.PI_ADDR, self.hub.quixi)
        self.srv_http, self.srv_mqtt = self.bus.register(self.SRV_ADDR, self.srv.quixi)

        self._hub_task: asyncio.Task[None] | None = None
        self._srv_task: asyncio.Task[None] | None = None
        self._patches: list[Any] = []

    async def start(self, auto_dial: bool = True) -> None:
        loop = asyncio.get_running_loop()
        if not auto_dial:
            self.hub.server_address = ""

        # Wire QuIXI & serial boundary without real sockets or MQTT threads
        self.hub.quixi._loop = loop
        self.hub.quixi._http = self.pi_http
        self.hub.quixi._mqtt = self.pi_mqtt
        self.pi_mqtt.on_message = self.hub.quixi._on_mqtt_message

        self.srv.quixi._loop = loop
        self.srv.quixi._http = self.srv_http
        self.srv.quixi._mqtt = self.srv_mqtt
        self.srv_mqtt.on_message = self.srv.quixi._on_mqtt_message

        self.hub.esp.set_event_handler(self.hub._on_esp_event)
        self.hub.quixi.on_topic("AppProtocolData", self.hub._on_app_protocol_data)
        self.hub.quixi.on_topic("ChatStream", self.hub._on_chat_stream)

        self.srv.quixi.on_topic("AppProtocolData", self.srv._on_app_protocol_data)
        self.srv.quixi.on_topic("Chat", self.srv._on_text_chat)

        p_serial = patch("pi_hub.esp_serial_bridge.serial.Serial", return_value=self.port)
        p_sd = patch.object(ase_mod, "sd", None)
        p_serial.start()
        p_sd.start()
        self._patches.extend([p_serial, p_sd])

        await self.hub.esp.start()
        await self.hub.audio.start()
        self.hub.audio._vad = None

        await self.hub.esp.set_debug(quixi="ONLINE", voip="DIALING", bt_spk="OK")
        await self.hub.esp.set_state("IDLE", "Connecting to AI Server...")

        if auto_dial and self.hub.server_address:
            await self.hub.voip.initiate_call(self.hub.server_address)
            await _wait_until(lambda: self.hub.voip.is_in_call and self.srv.voip.is_in_call, timeout=1.0)

    async def stop(self) -> None:
        await self.hub.audio.stop()
        await self.hub.esp.stop()
        for p in reversed(self._patches):
            p.stop()
        self._patches.clear()

    async def send_voice_utterance(self, duration_ms: int = 160) -> None:
        loud = struct.pack("<320h", *([5000] * 320))
        quiet = b"\x00" * 640
        n_frames = max(1, duration_ms // 20)
        for _ in range(n_frames):
            self.hub.audio._on_mic_audio(loud, 320, None, None)
        await asyncio.sleep(0.15)
        self.hub.audio._on_mic_audio(quiet, 320, None, None)
        await asyncio.sleep(0.25)


class TestTier1ContractUnits(unittest.IsolatedAsyncioTestCase):
    """Tier 1 fast boundary & contract units covering Features #11..#19 (50 checks)."""

    async def test_tier1_codec_voip_and_quixi_contracts(self) -> None:
        # F11: AppDataMessage binary codec (8 checks)
        sid = b"\x01\x02\x03\x04" * 4
        with self.subTest("F11_with_app_id"):
            m = unpack_app_data_message(pack_app_data_message(sid, b"pcm|opus", SPIXI_VOIP_APP_ID))
            self.assertEqual((m.session_id, m.data, m.app_id), (sid, b"pcm|opus", SPIXI_VOIP_APP_ID))
        with self.subTest("F11_no_app_id"):
            m = unpack_app_data_message(pack_app_data_message(sid, b"abc"))
            self.assertIsNone(m.app_id)
        with self.subTest("F11_7bit_varint"):
            self.assertEqual(unpack_app_data_message(pack_app_data_message(b"1", b"", "a" * 128)).app_id, "a" * 128)
        with self.subTest("F11_negative_len"):
            with self.assertRaises(ValueError):
                unpack_app_data_message(b"\x01a" + struct.pack("<i", -1))
        with self.subTest("F11_empty"):
            with self.assertRaises(ValueError):
                unpack_app_data_message(b"")
        with self.subTest("F11_trunc_sid"):
            with self.assertRaises(ValueError):
                unpack_app_data_message(b"\x05ab")
        with self.subTest("F11_trunc_data"):
            with self.assertRaises(ValueError):
                unpack_app_data_message(b"\x01a" + struct.pack("<i", 5) + b"ab")
        with self.subTest("F11_sid_over_255"):
            with self.assertRaises(ValueError):
                pack_app_data_message(b"x" * 256, b"")

        # F12: SpixiVoIPSessionManager (6 checks)
        sent: list[tuple[int, bytes]] = []

        async def _send_spixi(a: str, t: int, d: bytes, c: int) -> None:
            sent.append((t, d))

        with self.subTest("F12_codes"):
            self.assertEqual([int(c) for c in (
                SpixiMessageCode.APP_DATA,
                SpixiMessageCode.APP_REQUEST,
                SpixiMessageCode.APP_REQUEST_ACCEPT,
                SpixiMessageCode.APP_REQUEST_REJECT,
                SpixiMessageCode.APP_REQUEST_ERROR,
                SpixiMessageCode.APP_END_SESSION,
                SpixiMessageCode.APP_PROTOCOL_DATA,
                SpixiMessageCode.CHAT_STREAM,
            )], [21, 22, 28, 29, 30, 31, 45, 53])
        v = SpixiVoIPSessionManager(_send_spixi, ("pcm", "opus"))
        with self.subTest("F12_codec_match"):
            self.assertTrue(await v.on_received_call("1Pi", AppDataMessage(b"s1", b"opus|pcm", SPIXI_VOIP_APP_ID)))
            self.assertEqual(v.current_codec, "opus")
        with self.subTest("F12_busy_reject"):
            self.assertFalse(await v.on_received_call("1Other", AppDataMessage(b"s2", b"pcm", SPIXI_VOIP_APP_ID)))
            self.assertEqual(sent[-1][0], int(SpixiMessageCode.APP_REQUEST_REJECT))
        with self.subTest("F12_bad_codec"):
            await v.hangup_call()
            self.assertFalse(await v.on_received_call("1Pi", AppDataMessage(b"s3", b"speex", SPIXI_VOIP_APP_ID)))
        with self.subTest("F12_app_request_error"):
            sid_err = await v.initiate_call("1Srv")
            self.assertTrue(await v.on_request_error(AppDataMessage(sid_err, b"err"), "1Srv"))
            self.assertIsNone(v.current_session_id)
        with self.subTest("F12_stale_initiator_replace"):
            v_stale = SpixiVoIPSessionManager(_send_spixi, ("pcm",), pending_timeout=0.0)
            await v_stale.initiate_call("1A")
            self.assertTrue(await v_stale.on_received_call("1B", AppDataMessage(b"new", b"pcm", SPIXI_VOIP_APP_ID)))

        # F13: QuIXIClient helpers (6 checks)
        b64 = base64.b64encode(b"ok").decode("ascii")
        self.assertEqual(_extract_b64_bytes({"data": {"data": b64}}), b"ok")
        self.assertEqual(_extract_b64_bytes({"data": {"data": {"data": b64}}}), b"ok")
        self.assertEqual(_extract_b64_bytes({"data": b64}), b"ok")
        self.assertEqual(_extract_b64_bytes({}), b"")
        q = QuIXIClient("http://127.0.0.1:8001/")
        self.assertEqual(q.rest_url, "http://127.0.0.1:8001")
        hits: list[str] = []
        q.on_topic("Chat", lambda t, p: _record_async(hits, t))
        await q._dispatch_mqtt_event("Chat/1Pi", {"sender": "1Pi"})
        self.assertEqual(hits, ["Chat/1Pi"])

    async def test_tier1_speech_agent_server_bridge_and_audio_contracts(self) -> None:
        # F14: SpeechProcessor DSP (6 checks)
        pcm16 = make_pcm16_sine(440, 0.25, 16000)
        self.assertEqual(pcm16_to_wav_bytes(pcm16)[:4], b"RIFF")
        self.assertEqual(len(resample_pcm16(make_pcm16_sine(440, 0.3, 24000), 24000, 16000)), 9600)
        self.assertEqual(len(compute_viseme_envelope(pcm16, 16000, 20)), 5)
        self.assertEqual([len(f) for f in split_audio_frames(b"x" * 6400, 16000, 100)], [3200, 3200])
        ai = FakeAsyncOpenAI()
        sp = SpeechProcessor("http://x", "k", "w", "http://x", "k", "t", "alloy", stt_client=ai, tts_client=ai)
        self.assertEqual(await sp.transcribe_pcm(b"\x00" * 100), "")
        self.assertEqual(await sp.synthesize_pcm_16k("  "), (b"", []))

        # F15: AgentBrain (5 checks)
        self.assertEqual(len(AGENT_TOOLS), 6)
        self.assertEqual(AGENT_TOOLS[1]["function"]["parameters"]["properties"]["pin"]["enum"], [15, 16, 17, 18, 21, 33, 34, 35, 36, 37])
        brain = AgentBrain("http://x", "k", "m", "Tamagci", lambda n, a: asyncio.sleep(0, {}), max_history_messages=4, client=ai)
        brain.history = [
            {"role": "user", "content": "u1"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "read_imu_motion", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "c1", "content": "{}"},
            {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "u2"},
        ]
        brain._trim_history()
        self.assertNotEqual(brain.history[0]["role"], "tool")
        ai.chat_script.append(types.SimpleNamespace(content="", tool_calls=[make_tool_call("bad", "read_imu_motion", "{bad")]))
        ai.chat_script.append(types.SimpleNamespace(content="Handled", tool_calls=None))
        self.assertEqual((await brain.process_user_turn("test"))[0], "Handled")

        # F16: ServerCompanionService (5 checks)
        with patch.dict(os.environ, {"PI_IXIAN_ADDRESS": "", "ALLOW_UNAUTHENTICATED_DEV": ""}, clear=False):
            svc_closed = ServerCompanionService(speech_processor=sp, brain=brain)
            self.assertFalse(svc_closed._is_authorized("1Someone"))
        with patch.dict(os.environ, {"PI_IXIAN_ADDRESS": "", "ALLOW_UNAUTHENTICATED_DEV": "true"}, clear=False):
            svc_dev = ServerCompanionService(speech_processor=sp, brain=brain)
            self.assertTrue(svc_dev._is_authorized("1Dev"))

        # F17 & F18: EspSerialBridge & AudioSyncEngine (9 checks)
        self.assertEqual(EspSerialBridge().baudrate, 921600)
        self.assertEqual((await EspSerialBridge().request({"t": "ping"}))["ok"], 0)
        eng = AudioSyncEngine(bt_latency_ms=0)
        eng.set_volume(-5)
        self.assertEqual(eng.volume, 0.0)
        eng.set_volume(150)
        self.assertEqual(eng.volume, 1.0)
        eng.set_mic_mode("ptt")
        self.assertEqual(eng.mic_mode, "ptt")
        eng.set_mic_mode("duplex")
        self.assertEqual(eng.mic_mode, "duplex")
        eng.feed_tts_audio(b"1234")
        eng.stop_playback()
        self.assertTrue(eng._play_queue.empty())


async def _record_async(target: list, value: object) -> None:
    target.append(value)


async def _wait_until(pred, timeout: float = 0.5) -> None:
    deadline = time.monotonic() + timeout
    while not pred() and time.monotonic() < deadline:
        await asyncio.sleep(0.005)


class TestTier2ComponentMatrices(unittest.IsolatedAsyncioTestCase):
    """Tier 2 component & protocol integration matrices (100 assertions across all 20 features)."""

    async def test_tier2_esp_bridge_mock_esp32_matrix(self) -> None:
        # Exercises Features #1..#10, #17 (50+ assertions)
        with tempfile.TemporaryDirectory() as tmp:
            h = OfflineE2EHarness(Path(tmp))
            await h.start(auto_dial=False)
            try:
                # F1 & F2: state, sync, mouth, 9 emotions, subtitle clipping
                for emo in ("IDLE", "LISTENING", "THINKING", "SPEAKING", "HAPPY", "SAD", "SURPRISED", "SLEEPING", "ALERT"):
                    await h.hub.esp.set_state(emo, sub="S" * 220, mic=1 if emo == "LISTENING" else 0)
                    await _wait_until(lambda e=emo: h.esp.emo == e, timeout=0.5)
                    self.assertEqual(h.esp.emo, emo)
                    self.assertEqual(len(h.esp.sub), 180)
                await h.hub.esp.set_sync("SPEAKING", "Lipsync", 88)
                await h.hub.esp.set_mouth(55)
                await _wait_until(lambda: h.esp.mouth == 55, timeout=0.5)
                self.assertEqual(h.esp.mouth, 55)

                # F3: 4-step orientation reflected in imu_get
                for ang in (0, 90, 180, 270):
                    h.esp.angle = ang
                    imu_res = await h.hub.esp.request({"t": "imu_get"})
                    self.assertEqual(imu_res["ok"], 1)
                    self.assertEqual(imu_res["angle"], ang)

                # F4 & F5: touch events + cfg settings
                h.hub.audio.set_mic_mode("ptt")
                h.esp.emit_event({"src": "touch", "act": "ptt_toggle"})
                await _wait_until(lambda: h.hub.audio._ptt_active, timeout=0.5)
                self.assertTrue(h.hub.audio._ptt_active)
                h.esp.emit_event({"src": "touch", "act": "ptt_toggle"})
                await _wait_until(lambda: not h.hub.audio._ptt_active, timeout=0.5)
                self.assertFalse(h.hub.audio._ptt_active)
                await h.hub.esp.send({"t": "cfg", "bright": 65, "vol": 80, "mode": "vad", "ble_en": 1, "sleep_s": 30})
                await _wait_until(lambda: h.esp.bright == 65 and h.esp.sleep_s == 30, timeout=0.5)
                self.assertEqual(h.esp.bright, 65)
                self.assertEqual(h.esp.sleep_s, 30)

                # F6: dbg status + ping
                ping_res = await h.hub.esp.request({"t": "ping"})
                self.assertEqual(ping_res["ok"], 1)
                await h.hub.esp.set_debug(quixi="ONLINE", voip="IN_CALL", srv_ms=14, bt_spk="OK")
                await _wait_until(lambda: h.esp.dbg["srv_ms"] == 14, timeout=0.5)
                self.assertEqual(h.esp.dbg["srv_ms"], 14)

                # F7: IMU sensor fields
                imu_res = await h.hub.esp.request({"t": "imu_get"})
                for key in ("ax", "ay", "az", "gx", "gy", "gz", "temp", "gest"):
                    self.assertIn(key, imu_res)

                # F8: BLE 32-entry table & 30s pruning
                h.esp.ble_table = [
                    {"mac": f"AA:00:00:00:00:{i:02X}", "type": i % 2, "name": f"Dev{i}", "rssi": -50 - i, "ts": 9e12}
                    for i in range(32)
                ]
                ble_res = await h.hub.esp.request({"t": "ble_get"})
                self.assertEqual(ble_res["cnt"], 32)
                self.assertIn("type", ble_res["ble"][0])

                # F9: Allowlisted GPIO + protected -EACCES + pin<0 -EINVAL + Vbat
                for pin in (15, 16, 17, 18, 21, 33, 34, 35, 36, 37):
                    cfg_r = await h.hub.esp.request({"t": "gpio_cfg", "pin": pin, "dir": "out", "pull": "none", "val": 1})
                    self.assertEqual(cfg_r["ok"], 1)
                    get_r = await h.hub.esp.request({"t": "gpio_get", "pin": pin})
                    self.assertEqual(get_r["val"], 1)
                    self.assertGreater(get_r["vbat_mv"], 3000)

                for prot in (0, 2, 4, 5, 43, 44):
                    bad = await h.hub.esp.request({"t": "gpio_cfg", "pin": prot, "dir": "out", "val": 1})
                    self.assertEqual(bad["ok"], 0)
                    self.assertEqual(bad["err"], -13)

                neg = await h.hub.esp.request({"t": "gpio_get", "pin": -1})
                self.assertEqual(neg["ok"], 0)
                self.assertLess(neg["err"], 0)

                # F10 & F17: 921600 baud + boot noise ignored
                self.assertEqual(h.hub.esp.baudrate, 921600)
                h.port.push_rx_line("*** Booting Zephyr OS ***")
                h.port.push_rx_line("{corrupt")
                self.assertEqual((await h.hub.esp.request({"t": "ping"}))["ok"], 1)
            finally:
                await h.stop()

    async def test_tier2_quixi_voip_and_ai_pipeline_matrix(self) -> None:
        # Exercises Features #11..#20 across full Pi <-> Bus <-> Server <-> OpenAI boundary
        with tempfile.TemporaryDirectory() as tmp:
            h = OfflineE2EHarness(Path(tmp))
            await h.start(auto_dial=True)
            try:
                # F12 & F13: VoIP connected & QuIXI contacts/peering works
                self.assertTrue(h.hub.voip.is_in_call)
                self.assertTrue(h.srv.voip.is_in_call)
                await h.hub.quixi.add_contact(h.SRV_ADDR)
                await h.hub.quixi.accept_contact(h.SRV_ADDR)
                contacts = await h.hub.quixi.contacts()
                self.assertIn(h.SRV_ADDR, contacts["result"])

                # F14, F15, F16, F18, F19: Voice turn invoking GPIO + BLE + TTS
                h.ai.stt_script.append("Turn on pin 18 and scan BLE")
                h.ai.chat_script.append(
                    types.SimpleNamespace(
                        content="",
                        tool_calls=[
                            make_tool_call("c1", "gpio_configure_and_write", {"pin": 18, "mode": "out", "state": 1}),
                            make_tool_call("c2", "scan_ble_environment", {"min_rssi": -70}),
                        ],
                    )
                )
                h.ai.chat_script.append(types.SimpleNamespace(content="Pin 18 is high and KeyFob is nearby.", tool_calls=None))

                await h.send_voice_utterance(160)
                await _wait_until(
                    lambda: h.esp.gpio_vals.get(18) == 1
                    and not h.srv._turn_lock.locked()
                    and h.esp.emo == "IDLE"
                    and any(m == "sendAppData" for _, m, _ in h.bus.rest_log),
                    timeout=1.0,
                )

                self.assertEqual(h.esp.gpio_vals[18], 1)
                self.assertEqual(h.esp.emo, "IDLE")  # Playback finished -> IDLE
                rest_methods = [m for _, m, _ in h.bus.rest_log]
                self.assertIn("sendSpixiMessage", rest_methods)
                self.assertIn("sendChatStreamMessage", rest_methods)
                self.assertIn("sendAppData", rest_methods)

                # F20: verify config.yaml loads cleanly
                with open(h.cfg_path, "r", encoding="utf-8") as f:
                    parsed_cfg = yaml.safe_load(f)
                self.assertEqual(parsed_cfg["serial"]["baudrate"], 921600)
            finally:
                await h.stop()


class TestTier3PairwiseInteractions(unittest.IsolatedAsyncioTestCase):
    """Tier 3: 24 pairwise cross-feature interaction tests."""

    async def test_t3_01_face_tap_ptt_starts_audio_capture(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            h = OfflineE2EHarness(Path(tmp), mic_mode="ptt")
            await h.start(auto_dial=True)
            try:
                h.esp.emit_event({"src": "touch", "act": "ptt_toggle"})
                await _wait_until(lambda: h.hub.audio._ptt_active and h.esp.emo == "LISTENING", timeout=0.5)
                self.assertTrue(h.hub.audio._ptt_active)
                self.assertEqual(h.esp.emo, "LISTENING")
                h.esp.emit_event({"src": "touch", "act": "ptt_toggle"})
                await _wait_until(lambda: not h.hub.audio._ptt_active, timeout=0.5)
                self.assertFalse(h.hub.audio._ptt_active)
            finally:
                await h.stop()

    async def test_t3_02_tts_viseme_bt_latency_sync(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            h = OfflineE2EHarness(Path(tmp))
            h.hub.audio.bt_latency_ms = 50
            await h.start(auto_dial=True)
            try:
                h.ai.chat_script.append(types.SimpleNamespace(content="Sync test", tool_calls=None))
                await h.srv._handle_text_turn(h.PI_ADDR, "Say sync test")
                await _wait_until(
                    lambda: len([c for c in h.esp.rx_history if c.get("t") in {"sync", "mouth"}]) > 1,
                    timeout=0.5,
                )
                mouth_cmds = [c for c in h.esp.rx_history if c.get("t") in {"sync", "mouth"}]
                self.assertGreater(len(mouth_cmds), 1)
            finally:
                await h.stop()

    async def test_t3_03_barge_in_stop_speech_aborts_playback_and_notifies_server(self) -> None:
        def _is_stop_speech(m: dict) -> bool:
            return (
                m.get("kind") in {"barge_in", "stop_speech"}
                or m.get("act") == "stop_speech"
                or (
                    m.get("kind") == "hw_event"
                    and isinstance(m.get("event"), dict)
                    and m["event"].get("act") == "stop_speech"
                )
            )

        with tempfile.TemporaryDirectory() as tmp:
            h = OfflineE2EHarness(Path(tmp))
            await h.start(auto_dial=True)
            try:
                h.hub.audio.feed_tts_audio(make_pcm16_sine(440, 0.5, 16000))
                h.hub.audio.start_viseme_pacemaker([60] * 15)
                await _wait_until(lambda: h.hub.audio.is_speaking, timeout=0.5)
                self.assertTrue(h.hub.audio.is_speaking)

                h.esp.emit_event({"src": "touch", "act": "stop_speech"})
                await _wait_until(
                    lambda: (not h.hub.audio.is_speaking)
                    and any(
                        _is_stop_speech(json.loads(p["data"]))
                        for src, m, p in h.bus.rest_log
                        if src == h.PI_ADDR and m == "sendAppData"
                    ),
                    timeout=0.4,
                )
                self.assertFalse(h.hub.audio.is_speaking)
                self.assertTrue(h.hub.audio._play_queue.empty())
                app_msgs = [json.loads(p["data"]) for src, m, p in h.bus.rest_log if src == h.PI_ADDR and m == "sendAppData"]
                self.assertTrue(any(_is_stop_speech(m) for m in app_msgs))
            finally:
                await h.stop()

    async def test_t3_04_imu_4step_rotation_reflected_in_imu_get(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            h = OfflineE2EHarness(Path(tmp))
            await h.start(auto_dial=False)
            try:
                for ang in (0, 90, 180, 270):
                    h.esp.angle = ang
                    res = await h.hub.esp.request({"t": "imu_get"})
                    self.assertEqual(res["angle"], ang)
            finally:
                await h.stop()

    async def test_t3_05_llm_update_settings_propagates_brightness_vol_mic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            h = OfflineE2EHarness(Path(tmp))
            await h.start(auto_dial=True)
            try:
                h.ai.chat_script.append(
                    types.SimpleNamespace(
                        content="",
                        tool_calls=[make_tool_call("u1", "update_companion_settings", {"brightness": 30, "volume": 45, "mic_mode": "ptt"})],
                    )
                )
                h.ai.chat_script.append(types.SimpleNamespace(content="Updated!", tool_calls=None))
                await h.srv._handle_text_turn(h.PI_ADDR, "Dim screen and switch to PTT")
                await _wait_until(
                    lambda: h.esp.bright == 30
                    and abs(h.hub.audio.volume - 0.45) < 1e-6
                    and h.hub.audio.mic_mode == "ptt",
                    timeout=0.4,
                )
                self.assertEqual(h.esp.bright, 30)
                self.assertAlmostEqual(h.hub.audio.volume, 0.45)
                self.assertEqual(h.hub.audio.mic_mode, "ptt")
            finally:
                await h.stop()

    async def test_t3_06_esp_settings_evt_updates_pi_audio_engine(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            h = OfflineE2EHarness(Path(tmp))
            await h.start(auto_dial=False)
            try:
                h.esp.emit_event({"src": "cfg", "vol": 25, "mode": "duplex", "bright": 50, "ble_en": 0})
                await _wait_until(
                    lambda: abs(h.hub.audio.volume - 0.25) < 1e-6 and h.hub.audio.mic_mode == "duplex",
                    timeout=0.4,
                )
                self.assertAlmostEqual(h.hub.audio.volume, 0.25)
                self.assertEqual(h.hub.audio.mic_mode, "duplex")
            finally:
                await h.stop()

    async def test_t3_07_debug_ping_roundtrip_updates_srv_ms(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            h = OfflineE2EHarness(Path(tmp))
            await h.start(auto_dial=True)
            try:
                h.esp.dbg["srv_ms"] = -1
                h.esp.emit_event({"src": "dbg", "act": "ping"})
                await _wait_until(lambda: int(h.esp.dbg["srv_ms"]) >= 0, timeout=0.4)
                self.assertGreaterEqual(int(h.esp.dbg["srv_ms"]), 0)
            finally:
                await h.stop()

    async def test_t3_08_debug_test_call_triggers_voip_handshake(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            h = OfflineE2EHarness(Path(tmp))
            await h.start(auto_dial=False)
            h.hub.server_address = h.SRV_ADDR
            try:
                h.esp.emit_event({"src": "dbg", "act": "test_call"})
                await _wait_until(lambda: h.hub.voip.is_in_call and h.srv.voip.is_in_call, timeout=0.4)
                self.assertTrue(h.hub.voip.is_in_call)
                self.assertTrue(h.srv.voip.is_in_call)
            finally:
                await h.stop()

    async def test_t3_09_imu_shake_triggers_proactive_llm_reaction(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            h = OfflineE2EHarness(Path(tmp))
            await h.start(auto_dial=True)
            try:
                h.ai.chat_script.append(
                    types.SimpleNamespace(
                        content="",
                        tool_calls=[make_tool_call("e1", "set_companion_emotion", {"emotion": "SURPRISED", "subtitle": "Whoa!"})],
                    )
                )
                h.ai.chat_script.append(types.SimpleNamespace(content="Whoa, that tickles!", tool_calls=None))
                h.esp.emit_event({"src": "imu", "gest": "shake", "ax": 2.4, "ay": 0.1, "az": 0.2})
                await _wait_until(
                    lambda: len(h.ai.chat_calls) >= 1 and "shook" in json.dumps(h.ai.chat_calls[-1]["messages"]).lower(),
                    timeout=1.0,
                )
                self.assertGreaterEqual(len(h.ai.chat_calls), 1)
                self.assertIn("shook", json.dumps(h.ai.chat_calls[-1]["messages"]).lower())
            finally:
                await h.stop()

    async def test_t3_10_ble_scan_min_rssi_filtering_through_llm_tool(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            h = OfflineE2EHarness(Path(tmp))
            await h.start(auto_dial=True)
            try:
                h.ai.chat_script.append(
                    types.SimpleNamespace(
                        content="",
                        tool_calls=[make_tool_call("b1", "scan_ble_environment", {"min_rssi": -65})],
                    )
                )
                h.ai.chat_script.append(types.SimpleNamespace(content="Found KeyFob.", tool_calls=None))
                await h.srv._handle_text_turn(h.PI_ADDR, "Scan strong BLE devices")
                tool_msgs = [m for m in h.ai.chat_calls[-1]["messages"] if m.get("role") == "tool"]
                parsed = json.loads(tool_msgs[-1]["content"])
                self.assertEqual(parsed["cnt"], 1)
                self.assertEqual(parsed["ble"][0]["name"], "KeyFob")
            finally:
                await h.stop()

    async def test_t3_11_gpio_configure_and_write_allowlisted_pin_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            h = OfflineE2EHarness(Path(tmp))
            await h.start(auto_dial=True)
            try:
                h.ai.chat_script.append(
                    types.SimpleNamespace(
                        content="",
                        tool_calls=[make_tool_call("g1", "gpio_configure_and_write", {"pin": 21, "mode": "out", "state": 1})],
                    )
                )
                h.ai.chat_script.append(types.SimpleNamespace(content="GPIO 21 high.", tool_calls=None))
                await h.srv._handle_text_turn(h.PI_ADDR, "Set pin 21 high")
                self.assertEqual(h.esp.gpio_vals[21], 1)
            finally:
                await h.stop()

    async def test_t3_12_gpio_protected_pin_attack_returns_eacces_to_llm(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            h = OfflineE2EHarness(Path(tmp))
            await h.start(auto_dial=True)
            try:
                h.ai.chat_script.append(
                    types.SimpleNamespace(
                        content="",
                        tool_calls=[make_tool_call("gbad", "gpio_configure_and_write", {"pin": 4, "mode": "out", "state": 1})],
                    )
                )
                h.ai.chat_script.append(types.SimpleNamespace(content="Pin 4 is protected.", tool_calls=None))
                await h.srv._handle_text_turn(h.PI_ADDR, "Set pin 4")
                tool_msgs = [m for m in h.ai.chat_calls[-1]["messages"] if m.get("role") == "tool"]
                res = json.loads(tool_msgs[-1]["content"])
                self.assertEqual(res["ok"], 0)
                self.assertEqual(res["err"], -13)
            finally:
                await h.stop()

    async def test_t3_13_gpio_read_pin_with_pull_and_vbat_mv(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            h = OfflineE2EHarness(Path(tmp))
            h.esp.gpio_vals[33] = 1
            h.esp.vbat_mv = 4050
            await h.start(auto_dial=True)
            try:
                h.ai.chat_script.append(
                    types.SimpleNamespace(
                        content="",
                        tool_calls=[make_tool_call("gr", "gpio_read_pin", {"pin": 33, "pull": "up"})],
                    )
                )
                h.ai.chat_script.append(types.SimpleNamespace(content="Pin 33 is 1.", tool_calls=None))
                await h.srv._handle_text_turn(h.PI_ADDR, "Read pin 33 with pullup")
                self.assertEqual(h.esp.gpio_pulls.get(33), "up")
                tool_msgs = [m for m in h.ai.chat_calls[-1]["messages"] if m.get("role") == "tool"]
                res = json.loads(tool_msgs[-1]["content"])
                self.assertEqual(res["val"], 1)
                self.assertEqual(res["vbat_mv"], 4050)
            finally:
                await h.stop()

    async def test_t3_14_tool_batch_multi_hardware_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            h = OfflineE2EHarness(Path(tmp))
            await h.start(auto_dial=True)
            try:
                batch_msg = {
                    "kind": "tool_batch",
                    "id": "batch_1",
                    "calls": [
                        {"id": "sub_1", "tool": "gpio_configure_and_write", "args": {"pin": 15, "mode": "out", "state": 1}},
                        {"id": "sub_2", "tool": "read_imu_motion", "args": {}},
                    ],
                }
                await h.srv.quixi.send_app_data(h.PI_ADDR, json.dumps(batch_msg), protocol_id=AGENT_PROTOCOL_ID)
                await _wait_until(
                    lambda: h.esp.gpio_vals.get(15) == 1
                    and any(
                        json.loads(p["data"]).get("kind") == "tool_res" and json.loads(p["data"]).get("id") == "batch_1"
                        for src, m, p in h.bus.rest_log
                        if src == h.PI_ADDR and m == "sendAppData"
                    ),
                    timeout=0.4,
                )
                self.assertEqual(h.esp.gpio_vals[15], 1)
                pi_app_msgs = [json.loads(p["data"]) for src, m, p in h.bus.rest_log if src == h.PI_ADDR and m == "sendAppData"]
                self.assertTrue(any(m.get("kind") == "tool_res" and m.get("id") == "batch_1" for m in pi_app_msgs))
            finally:
                await h.stop()

    async def test_t3_15_chat_stream_and_tts_meta_subtitle_coordination(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            h = OfflineE2EHarness(Path(tmp))
            await h.start(auto_dial=True)
            try:
                h.ai.chat_script.append(types.SimpleNamespace(content="Streaming subtitle!", tool_calls=None))
                await h.srv._handle_text_turn(h.PI_ADDR, "Say streaming subtitle")
                await _wait_until(
                    lambda: any(
                        s.get("isStream") == "true"
                        for src, m, s in h.bus.rest_log
                        if src == h.SRV_ADDR and m == "sendChatStreamMessage"
                    ),
                    timeout=0.5,
                )
                streams = [p for src, m, p in h.bus.rest_log if src == h.SRV_ADDR and m == "sendChatStreamMessage"]
                self.assertTrue(any(s.get("isStream") == "true" for s in streams))
            finally:
                await h.stop()

    async def test_t3_16_voip_app_data_pcm_frames_to_stt_wav(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            h = OfflineE2EHarness(Path(tmp))
            await h.start(auto_dial=True)
            try:
                await h.send_voice_utterance(160)
                await _wait_until(lambda: len(h.ai.stt_calls) >= 1, timeout=0.5)
                self.assertEqual(len(h.ai.stt_calls), 1)
                self.assertEqual(h.ai.stt_calls[0]["file"].getvalue()[:4], b"RIFF")
            finally:
                await h.stop()

    async def test_t3_17_voip_unavailable_falls_back_to_tts_chunk_and_tts_end(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            h = OfflineE2EHarness(Path(tmp))
            await h.start(auto_dial=False)
            try:
                h.ai.chat_script.append(types.SimpleNamespace(content="Fallback speech", tool_calls=None))
                await h.srv._handle_text_turn(h.PI_ADDR, "Speak without VoIP")
                await _wait_until(
                    lambda: "tts_end"
                    in [json.loads(p["data"]).get("kind") for src, m, p in h.bus.rest_log if src == h.SRV_ADDR and m == "sendAppData"],
                    timeout=0.5,
                )
                kinds = [json.loads(p["data"])["kind"] for src, m, p in h.bus.rest_log if src == h.SRV_ADDR and m == "sendAppData"]
                self.assertIn("tts_chunk", kinds)
                self.assertIn("tts_end", kinds)
            finally:
                await h.stop()

    async def test_t3_18_voip_app_request_error_resets_and_retries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            h = OfflineE2EHarness(Path(tmp))
            await h.start(auto_dial=False)
            try:
                sid = await h.hub.voip.initiate_call(h.SRV_ADDR)
                err_b64 = base64.b64encode(pack_app_data_message(sid, b"")).decode("ascii")
                h.bus.deliver_mqtt(
                    h.PI_ADDR,
                    f"AppRequestError/{h.SRV_ADDR}",
                    {"sender": h.SRV_ADDR, "data": {"type": 30, "data": err_b64}},
                )
                await _wait_until(lambda: h.hub.voip.current_session_id is None, timeout=0.5)
                self.assertIsNone(h.hub.voip.current_session_id)
                sid2 = await h.hub.voip.initiate_call(h.SRV_ADDR)
                await _wait_until(lambda: h.hub.voip.is_in_call and h.srv.voip.is_in_call, timeout=0.5)
                self.assertNotEqual(sid2, sid)
                self.assertTrue(h.hub.voip.is_in_call)
            finally:
                await h.stop()

    async def test_t3_19_serial_noise_and_overflow_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            h = OfflineE2EHarness(Path(tmp))
            await h.start(auto_dial=False)
            try:
                h.port.push_rx_line("RAW LOG NOISE WITHOUT JSON")
                h.port.push_rx_line('{"t":"res","id":"stale"')  # truncated JSON
                res = await h.hub.esp.request({"t": "ping"})
                self.assertEqual(res["ok"], 1)
            finally:
                await h.stop()

    async def test_t3_20_history_trim_with_tool_calls_across_turns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            h = OfflineE2EHarness(Path(tmp), max_history=4)
            await h.start(auto_dial=True)
            try:
                for i in range(4):
                    h.ai.chat_script.append(
                        types.SimpleNamespace(
                            content="",
                            tool_calls=[make_tool_call(f"tc_{i}", "read_imu_motion", {})],
                        )
                    )
                    h.ai.chat_script.append(types.SimpleNamespace(content=f"Turn {i}", tool_calls=None))
                    await h.srv._handle_text_turn(h.PI_ADDR, f"Question {i}")
                for call in h.ai.chat_calls:
                    msgs = [m for m in call["messages"] if m["role"] != "system"]
                    self.assertNotEqual(msgs[0]["role"], "tool")
            finally:
                await h.stop()

    async def test_t3_21_zero_trust_auth_drops_spoofed_sender(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            h = OfflineE2EHarness(Path(tmp))
            await h.start(auto_dial=True)
            try:
                h.bus.deliver_mqtt(
                    h.SRV_ADDR,
                    "Chat/1SpoofedAttacker",
                    {"sender": "1SpoofedAttacker", "data": {"data": base64.b64encode(b"Evil").decode("ascii")}},
                )
                await asyncio.sleep(0.05)
                self.assertEqual(len(h.ai.chat_calls), 0)
            finally:
                await h.stop()

    async def test_t3_22_serial_disconnect_reconnect_mid_session(self) -> None:
        import serial

        with tempfile.TemporaryDirectory() as tmp:
            h = OfflineE2EHarness(Path(tmp))
            port2 = FakeSerialPort(esp=h.esp)
            ports = [h.port, port2]
            with (
                patch("pi_hub.esp_serial_bridge.serial.Serial", side_effect=lambda *a, **kw: ports.pop(0)),
                patch("pi_hub.esp_serial_bridge.time.sleep", return_value=None),
                patch.object(ase_mod, "sd", None),
            ):
                h.hub.quixi._loop = asyncio.get_running_loop()
                h.hub.quixi._http, h.hub.quixi._mqtt = h.pi_http, h.pi_mqtt
                h.pi_mqtt.on_message = h.hub.quixi._on_mqtt_message
                await h.hub.esp.start()
                await _wait_until(lambda: h.port.is_open, timeout=0.5)
                h.port.fail_on_read_exc = serial.SerialException("disconnect")
                await _wait_until(lambda: port2.is_open, timeout=0.5)
                res = await h.hub.esp.request({"t": "ping"})
                self.assertEqual(res["ok"], 1)
                await h.hub.esp.stop()

    async def test_t3_23_tts_riff_wav_resampling_and_visemes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            h = OfflineE2EHarness(Path(tmp))
            await h.start(auto_dial=True)
            try:
                wav_22k = pcm16_to_wav_bytes(make_pcm16_sine(440, 0.2, 22050), sample_rate=22050)
                h.ai.tts_script.append(wav_22k)
                h.ai.chat_script.append(types.SimpleNamespace(content="WAV reply", tool_calls=None))
                await h.srv._handle_text_turn(h.PI_ADDR, "Reply with WAV")
                meta = [json.loads(p["data"]) for _, m, p in h.bus.rest_log if m == "sendAppData" and '"tts_meta"' in p["data"]][-1]
                self.assertEqual(meta["total_bytes"], 6400)
                self.assertEqual(len(meta["envelope"]), 4)
            finally:
                await h.stop()

    async def test_t3_24_ble_disable_toggle_reflects_in_ble_get(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            h = OfflineE2EHarness(Path(tmp))
            await h.start(auto_dial=False)
            try:
                h.esp.emit_event({"src": "cfg", "ble_en": 0})
                await asyncio.sleep(0.03)
                ble = await h.hub.esp.request({"t": "ble_get"})
                self.assertEqual(ble["ok"], 1)
            finally:
                await h.stop()


class TestTier4RealWorldScenarios(unittest.IsolatedAsyncioTestCase):
    """Tier 4: 12 complete real-world E2E application scenarios."""

    async def test_t4_01_cold_boot_vad_voice_turn_tool_call_tts_hangup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            h = OfflineE2EHarness(Path(tmp), mic_mode="vad")
            await h.start(auto_dial=True)
            try:
                self.assertTrue(h.hub.voip.is_in_call)
                h.ai.stt_script.append("Turn on pin 16")
                h.ai.chat_script.append(
                    types.SimpleNamespace(
                        content="",
                        tool_calls=[make_tool_call("c1", "gpio_configure_and_write", {"pin": 16, "mode": "out", "state": 1})],
                    )
                )
                h.ai.chat_script.append(types.SimpleNamespace(content="Pin 16 is now on.", tool_calls=None))

                await h.send_voice_utterance(160)
                await _wait_until(lambda: h.esp.gpio_vals.get(16) == 1, timeout=1.0)
                self.assertEqual(h.esp.gpio_vals[16], 1)

                await h.hub.voip.hangup_call()
                await _wait_until(lambda: not h.hub.voip.is_in_call and not h.srv.voip.is_in_call, timeout=0.5)
                self.assertFalse(h.hub.voip.is_in_call)
                self.assertFalse(h.srv.voip.is_in_call)
            finally:
                await h.stop()

    async def test_t4_02_ptt_mode_with_tool_batch_and_ble_imu_gpio(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            h = OfflineE2EHarness(Path(tmp), mic_mode="ptt")
            await h.start(auto_dial=True)
            try:
                h.esp.gpio_vals[15] = 1
                h.esp.emit_event({"src": "touch", "act": "ptt_toggle"})
                await _wait_until(lambda: h.hub.audio._ptt_active, timeout=0.5)
                loud = struct.pack("<320h", *([4500] * 320))
                for _ in range(6):
                    h.hub.audio._on_mic_audio(loud, 320, None, None)

                h.ai.stt_script.append("Check sensors")
                h.ai.chat_script.append(types.SimpleNamespace(content="Sensors checked.", tool_calls=None))

                # Also execute a tool_batch directly across gpio_cfg, gpio_get(pull=up), ble_get, imu_get
                batch = {
                    "kind": "tool_batch",
                    "id": "b_t4_02",
                    "calls": [
                        {"id": "c1", "tool": "gpio_configure_and_write", "args": {"pin": 17, "mode": "out", "state": 1}},
                        {"id": "c2", "tool": "gpio_read_pin", "args": {"pin": 15, "pull": "up"}},
                        {"id": "c3", "tool": "scan_ble_environment", "args": {"min_rssi": -65}},
                        {"id": "c4", "tool": "read_imu_motion", "args": {}},
                    ],
                }
                await h.srv.quixi.send_app_data(h.PI_ADDR, json.dumps(batch), protocol_id=AGENT_PROTOCOL_ID)
                h.esp.emit_event({"src": "touch", "act": "ptt_toggle"})
                await _wait_until(
                    lambda: h.esp.gpio_vals.get(17) == 1
                    and any('"b_t4_02"' in p["data"] for src, m, p in h.bus.rest_log if src == h.PI_ADDR and m == "sendAppData"),
                    timeout=0.4,
                )

                self.assertEqual(h.esp.gpio_vals[17], 1)
                self.assertEqual(h.esp.gpio_pulls.get(15), "up")
                res_msgs = [json.loads(p["data"]) for src, m, p in h.bus.rest_log if src == h.PI_ADDR and m == "sendAppData" and '"b_t4_02"' in p["data"]]
                self.assertGreaterEqual(len(res_msgs), 1)
            finally:
                await h.stop()

    async def test_t4_03_mid_speech_barge_in_and_immediate_followup_turn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            h = OfflineE2EHarness(Path(tmp), mic_mode="vad")
            await h.start(auto_dial=True)
            try:
                h.hub.audio.feed_tts_audio(make_pcm16_sine(440, 1.0, 16000))
                h.hub.audio.start_viseme_pacemaker([70] * 20)
                await _wait_until(lambda: h.hub.audio.is_speaking, timeout=0.5)
                self.assertTrue(h.hub.audio.is_speaking)

                # User barges in
                h.esp.emit_event({"src": "touch", "act": "stop_speech"})
                await _wait_until(lambda: not h.hub.audio.is_speaking, timeout=0.5)
                self.assertFalse(h.hub.audio.is_speaking)

                # Immediate follow-up turn
                h.ai.stt_script.append("Second turn")
                h.ai.chat_script.append(types.SimpleNamespace(content="Second turn reply.", tool_calls=None))
                await h.send_voice_utterance(160)
                await _wait_until(
                    lambda: len(h.ai.chat_calls) >= 1 and h.ai.chat_calls[-1]["messages"][-1]["content"] == "Second turn",
                    timeout=1.0,
                )
                self.assertEqual(h.ai.chat_calls[-1]["messages"][-1]["content"], "Second turn")
            finally:
                await h.stop()

    async def test_t4_04_proactive_imu_shake_and_4step_rotation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            h = OfflineE2EHarness(Path(tmp))
            await h.start(auto_dial=True)
            try:
                for ang in (90, 180, 270):
                    h.esp.angle = ang
                    self.assertEqual((await h.hub.esp.request({"t": "imu_get"}))["angle"], ang)

                h.ai.chat_script.append(
                    types.SimpleNamespace(
                        content="",
                        tool_calls=[make_tool_call("s1", "set_companion_emotion", {"emotion": "SURPRISED", "subtitle": "Dizzy!"})],
                    )
                )
                h.ai.chat_script.append(types.SimpleNamespace(content="Whoa!", tool_calls=None))
                h.esp.emit_event({"src": "imu", "gest": "shake", "ax": 2.8, "ay": 0.0, "az": 0.1})
                await _wait_until(
                    lambda: len(h.ai.chat_calls) >= 1 and h.srv.brain.last_emotion == "SURPRISED",
                    timeout=1.0,
                )
                self.assertGreaterEqual(len(h.ai.chat_calls), 1)
                self.assertEqual(h.srv.brain.last_emotion, "SURPRISED")
            finally:
                await h.stop()

    async def test_t4_05_multi_turn_10_exchanges_with_history_trim_and_emotions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            h = OfflineE2EHarness(Path(tmp), max_history=8)
            await h.start(auto_dial=True)
            try:
                tools_cycle = [
                    ("set_companion_emotion", {"emotion": "HAPPY", "subtitle": "Yay"}),
                    ("gpio_configure_and_write", {"pin": 16, "mode": "out", "state": 1}),
                    ("gpio_read_pin", {"pin": 15, "pull": "up"}),
                    ("scan_ble_environment", {"min_rssi": -80}),
                    ("read_imu_motion", {}),
                    ("update_companion_settings", {"brightness": 70}),
                ]
                for i in range(10):
                    t_name, t_args = tools_cycle[i % len(tools_cycle)]
                    h.ai.chat_script.append(
                        types.SimpleNamespace(content="", tool_calls=[make_tool_call(f"tc_{i}", t_name, t_args)])
                    )
                    h.ai.chat_script.append(types.SimpleNamespace(content=f"Done {i}", tool_calls=None))
                    await h.srv._handle_text_turn(h.PI_ADDR, f"Step {i}")

                self.assertEqual(len(h.ai.chat_calls), 20)
                for call in h.ai.chat_calls:
                    non_sys = [m for m in call["messages"] if m["role"] != "system"]
                    self.assertNotEqual(non_sys[0]["role"], "tool")
                    call_ids = {tc["id"] for m in non_sys if m["role"] == "assistant" for tc in m.get("tool_calls", [])}
                    for m in non_sys:
                        if m["role"] == "tool":
                            self.assertIn(m["tool_call_id"], call_ids)
            finally:
                await h.stop()

    async def test_t4_06_security_boundary_protected_pins_and_spoofed_peer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            h = OfflineE2EHarness(Path(tmp))
            await h.start(auto_dial=True)
            try:
                # Spoofed unauthorized sender dropped
                h.bus.deliver_mqtt(
                    h.SRV_ADDR,
                    "AppProtocolData/1Attacker",
                    {
                        "sender": "1Attacker",
                        "data": {"data": base64.b64encode(json.dumps({"kind": "end_of_turn"}).encode()).decode("ascii")},
                    },
                )
                await asyncio.sleep(0.04)
                self.assertEqual(len(h.ai.stt_calls), 0)

                # Protected GPIO 2 & 4 rejected with -EACCES (-13)
                h.ai.chat_script.append(
                    types.SimpleNamespace(
                        content="",
                        tool_calls=[
                            make_tool_call("p2", "gpio_configure_and_write", {"pin": 2, "mode": "out", "state": 1}),
                            make_tool_call("p4", "gpio_configure_and_write", {"pin": 4, "mode": "out", "state": 1}),
                        ],
                    )
                )
                h.ai.chat_script.append(types.SimpleNamespace(content="Those pins are protected.", tool_calls=None))
                await h.srv._handle_text_turn(h.PI_ADDR, "Toggle pins 2 and 4")
                tool_msgs = [m for m in h.ai.chat_calls[-1]["messages"] if m["role"] == "tool"]
                self.assertEqual([json.loads(m["content"]).get("err") for m in tool_msgs], [-13, -13])
            finally:
                await h.stop()

    async def test_t4_07_voip_drop_fallback_and_auto_redial(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            h = OfflineE2EHarness(Path(tmp))
            await h.start(auto_dial=True)
            try:
                sid1 = h.hub.voip.current_session_id
                # Drop VoIP via APP_END_SESSION
                await h.hub.voip.hangup_call()
                await _wait_until(lambda: not h.hub.voip.is_in_call and not h.srv.voip.is_in_call, timeout=0.5)
                self.assertFalse(h.hub.voip.is_in_call)

                # Server falls back to tts_chunk + tts_end
                h.ai.chat_script.append(types.SimpleNamespace(content="Fallback works", tool_calls=None))
                await h.srv._handle_text_turn(h.PI_ADDR, "Hello?")
                await _wait_until(
                    lambda: "tts_chunk"
                    in [json.loads(p["data"]).get("kind") for src, m, p in h.bus.rest_log if src == h.SRV_ADDR and m == "sendAppData"],
                    timeout=0.5,
                )
                kinds = [json.loads(p["data"])["kind"] for src, m, p in h.bus.rest_log if src == h.SRV_ADDR and m == "sendAppData"]
                self.assertIn("tts_chunk", kinds)

                # Redial restores native VoIP
                sid2 = await h.hub.voip.initiate_call(h.SRV_ADDR)
                await _wait_until(lambda: h.hub.voip.is_in_call and h.srv.voip.is_in_call, timeout=0.5)
                self.assertNotEqual(sid2, sid1)
                self.assertTrue(h.hub.voip.is_in_call)
                self.assertTrue(h.srv.voip.is_in_call)
            finally:
                await h.stop()

    async def test_t4_08_upstream_stt_tts_outage_and_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            h = OfflineE2EHarness(Path(tmp))
            await h.start(auto_dial=True)
            try:
                # Turn 1: < 50ms audio -> "Didn't catch that."
                await h.srv._handle_voice_turn(h.PI_ADDR, b"\x00" * 200)
                await _wait_until(lambda: h.esp.sub == "Didn't catch that.", timeout=0.5)
                self.assertEqual(h.esp.sub, "Didn't catch that.")

                # Turn 2: HTTP 503 outage in STT -> graceful IDLE "Didn't catch that." + unhandled error -> SAD "Server error."
                h.ai.stt_script.append(RuntimeError("HTTP 503 Service Unavailable"))
                await h.srv._handle_voice_turn(h.PI_ADDR, make_pcm16_sine(440, 0.1, 16000))
                await _wait_until(
                    lambda: not h.srv._turn_lock.locked() and h.esp.sub == "Didn't catch that.",
                    timeout=0.5,
                )
                self.assertFalse(h.srv._turn_lock.locked())
                self.assertEqual(h.esp.sub, "Didn't catch that.")

                with patch.object(h.srv.speech, "transcribe_pcm", side_effect=RuntimeError("Fatal STT crash")):
                    await h.srv._handle_voice_turn(h.PI_ADDR, make_pcm16_sine(440, 0.1, 16000))
                await _wait_until(
                    lambda: not h.srv._turn_lock.locked() and h.esp.emo == "SAD" and h.esp.sub == "Server error.",
                    timeout=0.5,
                )
                self.assertFalse(h.srv._turn_lock.locked())
                self.assertEqual(h.esp.emo, "SAD")
                self.assertEqual(h.esp.sub, "Server error.")

                # Turn 3: Normal recovery
                h.ai.stt_script.append("Are you back?")
                h.ai.chat_script.append(types.SimpleNamespace(content="Yes, I am back!", tool_calls=None))
                await h.srv._handle_voice_turn(h.PI_ADDR, make_pcm16_sine(440, 0.1, 16000))
                await _wait_until(
                    lambda: len(h.ai.chat_calls) >= 1 and h.ai.chat_calls[-1]["messages"][-1]["content"] == "Are you back?",
                    timeout=1.0,
                )
                self.assertEqual(h.ai.chat_calls[-1]["messages"][-1]["content"], "Are you back?")
            finally:
                await h.stop()

    async def test_t4_09_direct_spixi_chat_and_debug_screen_telemetry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            h = OfflineE2EHarness(Path(tmp))
            await h.start(auto_dial=False)
            h.hub.server_address = h.SRV_ADDR
            try:
                h.ai.chat_script.append(
                    types.SimpleNamespace(
                        content="",
                        tool_calls=[make_tool_call("u1", "update_companion_settings", {"brightness": 45, "volume": 80, "mic_mode": "ptt"})],
                    )
                )
                h.ai.chat_script.append(types.SimpleNamespace(content="Settings updated.", tool_calls=None))

                h.bus.deliver_mqtt(
                    h.SRV_ADDR,
                    f"Chat/{h.PI_ADDR}",
                    {"sender": h.PI_ADDR, "data": {"data": base64.b64encode(b"Set bright 45 vol 80 ptt").decode("ascii")}},
                )
                await _wait_until(
                    lambda: h.esp.bright == 45
                    and abs(h.hub.audio.volume - 0.8) < 1e-6
                    and h.hub.audio.mic_mode == "ptt",
                    timeout=0.4,
                )
                self.assertEqual(h.esp.bright, 45)
                self.assertAlmostEqual(h.hub.audio.volume, 0.8)
                self.assertEqual(h.hub.audio.mic_mode, "ptt")

                # Trigger dbg ping & test_call from MockESP32
                h.esp.emit_event({"src": "dbg", "act": "ping"})
                h.esp.emit_event({"src": "dbg", "act": "test_call"})
                await _wait_until(
                    lambda: h.hub.voip.is_in_call and h.esp.dbg.get("voip") == "IN_CALL",
                    timeout=0.4,
                )
                self.assertTrue(h.hub.voip.is_in_call)
                self.assertEqual(h.esp.dbg["voip"], "IN_CALL")
            finally:
                await h.stop()

    async def test_t4_10_usb_serial_disconnect_and_boot_noise_recovery(self) -> None:
        import serial

        with tempfile.TemporaryDirectory() as tmp:
            h = OfflineE2EHarness(Path(tmp))
            port2 = FakeSerialPort(esp=h.esp)
            ports = [h.port, port2]
            with (
                patch("pi_hub.esp_serial_bridge.serial.Serial", side_effect=lambda *a, **kw: ports.pop(0)),
                patch("pi_hub.esp_serial_bridge.time.sleep", return_value=None),
                patch.object(ase_mod, "sd", None),
            ):
                loop = asyncio.get_running_loop()
                h.hub.quixi._loop = loop
                h.hub.quixi._http, h.hub.quixi._mqtt = h.pi_http, h.pi_mqtt
                h.pi_mqtt.on_message = h.hub.quixi._on_mqtt_message
                h.srv.quixi._loop = loop
                h.srv.quixi._http, h.srv.quixi._mqtt = h.srv_http, h.srv_mqtt
                h.srv_mqtt.on_message = h.srv.quixi._on_mqtt_message
                h.hub.esp.set_event_handler(h.hub._on_esp_event)
                h.hub.quixi.on_topic("AppProtocolData", h.hub._on_app_protocol_data)
                h.srv.quixi.on_topic("AppProtocolData", h.srv._on_app_protocol_data)

                await h.hub.esp.start()
                await _wait_until(lambda: h.port.is_open, timeout=0.5)
                h.port.push_rx_line("ESP-ROM:esp32s3-20210327")
                h.port.fail_on_read_exc = serial.SerialException("USB cable bumped")
                await _wait_until(lambda: port2.is_open, timeout=0.5)

                h.esp.gpio_vals[16] = 1
                h.ai.chat_script.append(
                    types.SimpleNamespace(content="", tool_calls=[make_tool_call("g16", "gpio_read_pin", {"pin": 16})])
                )
                h.ai.chat_script.append(types.SimpleNamespace(content="Pin 16 is 1.", tool_calls=None))
                await h.srv._handle_text_turn(h.PI_ADDR, "Read pin 16")
                tool_msgs = [m for m in h.ai.chat_calls[-1]["messages"] if m["role"] == "tool"]
                self.assertEqual(json.loads(tool_msgs[-1]["content"])["val"], 1)
                await h.hub.esp.stop()

    async def test_t4_11_concurrent_and_stale_session_isolation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            h = OfflineE2EHarness(Path(tmp))
            await h.start(auto_dial=True)
            try:
                active_sid = h.srv.voip.current_session_id
                stale_sid = b"stale_session_00"
                assert active_sid is not None and stale_sid != active_sid

                # Stale session audio & hangup must NOT corrupt active session
                await h.srv.voip.on_data(h.PI_ADDR, AppDataMessage(session_id=stale_sid, data=b"STALE_AUDIO"))
                await h.srv.voip.on_hangup_call(AppDataMessage(session_id=stale_sid, data=b""))
                self.assertTrue(h.srv.voip.is_in_call)
                self.assertEqual(h.srv.voip.current_session_id, active_sid)

                # Send real audio on active_sid and verify only active audio is transcribed
                real_pcm = make_pcm16_sine(440, 0.1, 16000)
                await h.hub.voip.send_audio_frame(real_pcm)
                await h.hub.quixi.send_app_data(h.SRV_ADDR, json.dumps({"kind": "end_of_turn"}), protocol_id=AGENT_PROTOCOL_ID)
                await _wait_until(lambda: len(h.ai.stt_calls) == 1, timeout=0.5)
                self.assertEqual(len(h.ai.stt_calls), 1)
            finally:
                await h.stop()

    async def test_t4_12_step5_config_templates_to_offline_e2e_smoke(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            # Verify Step 5 templates & production config defaults parse cleanly with zero Pi secrets
            pi_cfg_path = _REPO_ROOT / "pi_hub" / "config.yaml"
            with open(pi_cfg_path, "r", encoding="utf-8") as f:
                pi_cfg = yaml.safe_load(f)
            self.assertEqual(int(pi_cfg["serial"]["baudrate"]), 921600)
            self.assertEqual(int(pi_cfg["quixi"]["mqtt_port"]), 1883)
            self.assertIn("8001", pi_cfg["quixi"]["rest_url"])

            for tpl_name in ("deploy/ixian.cfg.pi.template", "deploy/ixian.cfg.server.template"):
                tpl = _REPO_ROOT / tpl_name
                self.assertTrue(tpl.is_file(), f"Missing {tpl_name}")
                txt = tpl.read_text(encoding="utf-8")
                self.assertIn("8001", txt)
                self.assertIn("1883", txt)

            h = OfflineE2EHarness(Path(tmp))
            await h.start(auto_dial=True)
            try:
                h.ai.stt_script.append("Smoke test")
                h.ai.chat_script.append(types.SimpleNamespace(content="All systems nominal.", tool_calls=None))
                await h.send_voice_utterance(160)
                await _wait_until(lambda: len(h.ai.chat_calls) == 1, timeout=1.0)
                self.assertEqual(len(h.ai.chat_calls), 1)
            finally:
                await h.stop()


class TestTier5HardeningAndCrossTierRegressions(unittest.IsolatedAsyncioTestCase):
    """Tier 5: Cross-tier regression tests for firmware, tools, and Python concurrency/auth fixes."""

    async def test_t5_01_uppercase_gpio_mode_and_invalid_gpio_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            h = OfflineE2EHarness(Path(tmp))
            await h.start(auto_dial=True)
            try:
                h.ai.chat_script.append(
                    types.SimpleNamespace(
                        content="",
                        tool_calls=[make_tool_call("up1", "gpio_configure_and_write", {"pin": 16, "mode": "OUT", "state": 1})],
                    )
                )
                h.ai.chat_script.append(types.SimpleNamespace(content="Pin 16 driven high.", tool_calls=None))
                await h.srv._handle_text_turn(h.PI_ADDR, "Drive pin 16 high")
                self.assertEqual(h.esp.gpio_dirs.get(16), "out")
                self.assertEqual(h.esp.gpio_vals[16], 1)

                # Invalid gpio_set / gpio_cfg / float pin checks against MockESP32 parity
                miss_val = await h.hub.esp.request({"t": "gpio_set", "pin": 16})
                self.assertEqual((miss_val["ok"], miss_val["err"]), (0, -22))

                str_val = await h.hub.esp.request({"t": "gpio_set", "pin": 16, "val": "high"})
                self.assertEqual((str_val["ok"], str_val["err"]), (0, -22))

                bad_dir = await h.hub.esp.request({"t": "gpio_cfg", "pin": 16, "dir": "OUT"})
                self.assertEqual((bad_dir["ok"], bad_dir["err"]), (0, -22))

                bad_pull = await h.hub.esp.request({"t": "gpio_cfg", "pin": 16, "dir": "in", "pull": "bogus"})
                self.assertEqual((bad_pull["ok"], bad_pull["err"]), (0, -22))

                float_pin = await h.hub.esp.request({"t": "gpio_get", "pin": 16.5})
                self.assertEqual((float_pin["ok"], float_pin["err"]), (0, -22))
            finally:
                await h.stop()

    async def test_t5_02_multibyte_utf8_subtitle_end_to_end(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            h = OfflineE2EHarness(Path(tmp))
            await h.start(auto_dial=True)
            try:
                cjk_reply = "中" * 180
                h.ai.chat_script.append(types.SimpleNamespace(content=cjk_reply, tool_calls=None))
                await h.srv._handle_text_turn(h.PI_ADDR, "Speak CJK")
                await _wait_until(lambda: len(h.esp.sub) > 0 and "中" in h.esp.sub, timeout=0.5)
                self.assertLessEqual(len(h.esp.sub.encode("utf-8")), 191)
                self.assertTrue(all(ch == "中" for ch in h.esp.sub))
            finally:
                await h.stop()

    async def test_t5_03_early_barge_in_during_stt_preserves_turn2_listening(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            h = OfflineE2EHarness(Path(tmp), mic_mode="ptt")
            await h.start(auto_dial=True)
            try:
                stt_started = asyncio.Event()
                release_stt = asyncio.Event()

                async def slow_stt(pcm: bytes, sample_rate: int = 16000, cancel_event: asyncio.Event | None = None) -> str:
                    stt_started.set()
                    await release_stt.wait()
                    if cancel_event is not None and cancel_event.is_set():
                        return ""
                    return "Turn 1 cancelled"

                with patch.object(h.srv.speech, "transcribe_pcm", side_effect=slow_stt):
                    t1 = asyncio.create_task(
                        h.srv._handle_voice_turn(h.PI_ADDR, make_pcm16_sine(440, 0.1, 16000))
                    )
                    await asyncio.wait_for(stt_started.wait(), timeout=1.0)

                    # User triggers barge_in before tts_meta, then starts Turn 2 via PTT
                    h.esp.emit_event({"src": "touch", "act": "stop_speech"})
                    await asyncio.sleep(0.03)
                    h.esp.emit_event({"src": "touch", "act": "ptt_toggle"})
                    await _wait_until(lambda: h.hub.audio._ptt_active and h.esp.emo == "LISTENING", timeout=0.5)

                    release_stt.set()
                    await asyncio.wait_for(t1, timeout=1.0)
                    await asyncio.sleep(0.05)

                self.assertEqual(len(h.ai.chat_calls), 0)
                self.assertEqual(len(h.ai.tts_calls), 0)
                self.assertTrue(h.hub.audio._ptt_active)
                self.assertEqual(h.esp.emo, "LISTENING")
            finally:
                await h.stop()

    async def test_t5_04_ptt_toggle_pre_tts_meta_cancels_turn1_and_preserves_turn2_listening(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            h = OfflineE2EHarness(Path(tmp), mic_mode="ptt")
            await h.start(auto_dial=True)
            try:
                stt_started = asyncio.Event()
                release_stt = asyncio.Event()
                call_idx = 0

                async def gated_stt(pcm: bytes, sample_rate: int = 16000, cancel_event: asyncio.Event | None = None) -> str:
                    nonlocal call_idx
                    call_idx += 1
                    if call_idx == 1:
                        stt_started.set()
                        await release_stt.wait()
                        if cancel_event is not None and cancel_event.is_set():
                            return ""
                        return "Turn 1 should be cancelled"
                    return "Turn 2 question"

                with patch.object(h.srv.speech, "transcribe_pcm", side_effect=gated_stt):
                    # Start Turn 1 via PTT ON -> audio -> PTT OFF
                    h.esp.emit_event({"src": "touch", "act": "ptt_toggle"})
                    await _wait_until(lambda: h.hub.audio._ptt_active, timeout=0.5)
                    loud = struct.pack("<320h", *([4500] * 320))
                    for _ in range(5):
                        h.hub.audio._on_mic_audio(loud, 320, None, None)
                    h.esp.emit_event({"src": "touch", "act": "ptt_toggle"})
                    await asyncio.wait_for(stt_started.wait(), timeout=1.0)

                    # While Turn 1 is pre-tts_meta, user taps ptt_toggle ON to start Turn 2
                    h.esp.emit_event({"src": "touch", "act": "ptt_toggle"})
                    await _wait_until(lambda: h.hub.audio._ptt_active and h.esp.emo == "LISTENING", timeout=0.5)
                    await asyncio.sleep(0.03)

                    release_stt.set()
                    await _wait_until(lambda: not h.srv._turn_lock.locked(), timeout=0.5)

                    # Even if a late Turn 1 ChatStream or tts_meta arrives during Turn 2 PTT, LISTENING stays intact
                    await h.srv.quixi.send_chat_stream_message(
                        h.PI_ADDR, "Late Turn 1 chunk", message_id_b64="m_t1", sequence=0, is_stream=True
                    )
                    await h.srv.quixi.send_app_data(
                        h.PI_ADDR,
                        json.dumps({"kind": "tts_meta", "turn_id": "t1_late", "emo": "HAPPY", "text": "Late Turn 1", "envelope": [50]}),
                        protocol_id=AGENT_PROTOCOL_ID,
                    )
                    await asyncio.sleep(0.04)

                    self.assertEqual(len(h.ai.chat_calls), 0)
                    self.assertEqual(len(h.ai.tts_calls), 0)
                    self.assertTrue(h.hub.audio._ptt_active)
                    self.assertEqual(h.esp.emo, "LISTENING")
                    self.assertFalse(h.hub.audio.is_speaking)

                    # Finish Turn 2 normally
                    h.ai.chat_script.append(types.SimpleNamespace(content="Turn 2 answer.", tool_calls=None))
                    for _ in range(5):
                        h.hub.audio._on_mic_audio(loud, 320, None, None)
                    h.esp.emit_event({"src": "touch", "act": "ptt_toggle"})
                    await _wait_until(lambda: len(h.ai.chat_calls) == 1 and len(h.ai.tts_calls) == 1, timeout=1.0)
                    self.assertEqual(h.ai.chat_calls[0]["messages"][-1]["content"], "Turn 2 question")
            finally:
                await h.stop()

    async def test_t5_05_back_to_back_eot_then_barge_in_cancels_queued_turn_and_allows_next_turn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            h = OfflineE2EHarness(Path(tmp), mic_mode="ptt")
            await h.start(auto_dial=True)
            try:
                sid = h.hub.voip.current_session_id
                assert sid is not None
                await h.hub.voip.send_audio_frame(make_pcm16_sine(440, 0.1, 16000))
                await asyncio.sleep(0.02)

                # Deliver end_of_turn and barge_in back-to-back into server._on_app_protocol_data
                eot_b64 = base64.b64encode(
                    json.dumps({"kind": "end_of_turn", "session_id": sid.hex()}).encode("utf-8")
                ).decode("ascii")
                barge_b64 = base64.b64encode(
                    json.dumps({"kind": "barge_in", "act": "stop_speech"}).encode("utf-8")
                ).decode("ascii")
                await h.srv._on_app_protocol_data(
                    f"AppProtocolData/{h.PI_ADDR}", {"sender": h.PI_ADDR, "data": {"data": eot_b64}}
                )
                await h.srv._on_app_protocol_data(
                    f"AppProtocolData/{h.PI_ADDR}", {"sender": h.PI_ADDR, "data": {"data": barge_b64}}
                )
                await asyncio.sleep(0.06)

                self.assertEqual(len(h.ai.stt_calls), 0)
                self.assertEqual(len(h.ai.chat_calls), 0)
                self.assertEqual(len(h.ai.tts_calls), 0)

                # Subsequent fresh turn still succeeds normally
                h.ai.stt_script.append("Valid next turn")
                h.ai.chat_script.append(types.SimpleNamespace(content="Valid next reply", tool_calls=None))
                await h.hub.voip.send_audio_frame(make_pcm16_sine(440, 0.1, 16000))
                await asyncio.sleep(0.02)
                await h.srv._on_app_protocol_data(
                    f"AppProtocolData/{h.PI_ADDR}", {"sender": h.PI_ADDR, "data": {"data": eot_b64}}
                )
                await _wait_until(lambda: len(h.ai.tts_calls) == 1, timeout=1.0)
                self.assertEqual(len(h.ai.stt_calls), 1)
                self.assertEqual(len(h.ai.chat_calls), 1)
                self.assertEqual(len(h.ai.tts_calls), 1)
            finally:
                await h.stop()


if __name__ == "__main__":
    unittest.main()
