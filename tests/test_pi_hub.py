"""Offline 4-tier unit, boundary, integration, and scenario tests for pi_hub."""

from __future__ import annotations

import asyncio
import base64
import inspect
import json
import math
import struct
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tests._stubs import (
    FakeQuIXIBus,
    FakeSerialPort,
    MockESP32,
    install_dependency_stubs,
    make_pcm16_sine,
)

install_dependency_stubs()

import yaml
from pi_hub import audio_sync_engine as ase_mod
from pi_hub.audio_sync_engine import AudioSyncEngine
from pi_hub.esp_serial_bridge import EspSerialBridge
from pi_hub.main_hub import AGENT_PROTOCOL_ID, PiCompanionHub


def _make_hub_with_mocks(tmp_dir: Path, mic_mode: str = "vad") -> tuple[PiCompanionHub, MockESP32, FakeQuIXIBus, FakeSerialPort]:
    cfg_path = tmp_dir / "config.yaml"
    cfg_path.write_text(
        "\n".join(
            [
                "serial:",
                "  port: /dev/ttyACM0",
                "  baudrate: 921600",
                "quixi:",
                "  rest_url: http://127.0.0.1:8001",
                "  mqtt_host: 127.0.0.1",
                "  mqtt_port: 1883",
                "  server_ixian_address: 1SrvAddrTest",
                "audio:",
                "  sample_rate: 16000",
                "  frame_ms: 100",
                "  vad_aggressiveness: 2",
                "  vad_silence_ms: 200",
                "  bt_latency_ms: 10",
                f"  default_mic_mode: {mic_mode}",
            ]
        ),
        encoding="utf-8",
    )
    hub = PiCompanionHub(cfg_path)
    esp = MockESP32()
    port = FakeSerialPort(esp=esp)
    port.is_open = True
    bus = FakeQuIXIBus()
    pi_http, pi_mqtt = bus.register("1PiAddrTest", hub.quixi)
    hub.quixi._http = pi_http
    hub.quixi._mqtt = pi_mqtt
    pi_mqtt.on_message = hub.quixi._on_mqtt_message
    return hub, esp, bus, port


class TestEspSerialBridge(unittest.IsolatedAsyncioTestCase):
    """Tier 1, Tier 2, and Tier 4 tests for EspSerialBridge."""

    def test_default_baudrate_is_921600(self) -> None:
        # M3 contract (PROJECT.md:106,123): EspSerialBridge & config.yaml must default to 921600
        sig = inspect.signature(EspSerialBridge.__init__)
        self.assertEqual(sig.parameters["baudrate"].default, 921600)
        bridge = EspSerialBridge()
        self.assertEqual(bridge.baudrate, 921600)

        with open(_REPO_ROOT / "pi_hub" / "config.yaml", "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        self.assertEqual(int(cfg["serial"]["baudrate"]), 921600)

    async def test_ndjson_rpc_correlation_events_and_helpers(self) -> None:
        esp = MockESP32()
        port = FakeSerialPort(esp=esp)
        events: list[dict] = []

        async def on_evt(evt: dict) -> None:
            events.append(evt)

        bridge = EspSerialBridge(port="/dev/ttyACM0", baudrate=921600)
        bridge.set_event_handler(on_evt)

        with patch("pi_hub.esp_serial_bridge.serial.Serial", return_value=port):
            await bridge.start()
            await asyncio.sleep(0.05)

            # Inject boot noise and malformed JSON before valid event
            port.push_rx_line("I (10) boot: noisy log")
            port.push_rx_line("{bad json")
            esp.emit_event({"src": "sys", "act": "boot"})

            res = await bridge.request({"t": "ping"}, timeout=1.0)
            self.assertEqual(res.get("ok"), 1)
            self.assertIn("up", res)

            long_sub = "A" * 250
            await bridge.set_state("HAPPY", sub=long_sub, mic=1)
            await bridge.set_sync("SPEAKING", sub=long_sub, mouth=77)
            await bridge.set_mouth(42)
            await bridge.set_debug(quixi="ONLINE", voip="IN_CALL", srv_ms=12, bt_spk="OK")
            await asyncio.sleep(0.05)
            await bridge.stop()

        self.assertEqual(events, [{"t": "evt", "src": "sys", "act": "boot"}])
        self.assertEqual(esp.emo, "SPEAKING")
        self.assertEqual(len(esp.sub), 180)
        self.assertEqual(esp.mouth, 42)
        self.assertEqual(esp.mic, 1)
        self.assertEqual(esp.dbg, {"quixi": "ONLINE", "voip": "IN_CALL", "srv_ms": 12, "bt_spk": "OK"})

    async def test_timeout_unstarted_and_concurrent_out_of_order_rpcs(self) -> None:
        bridge = EspSerialBridge(baudrate=921600)
        unstarted = await bridge.request({"t": "ping"})
        self.assertEqual(unstarted["ok"], 0)
        self.assertIn("not started", unstarted["error"])

        port = FakeSerialPort()
        with patch("pi_hub.esp_serial_bridge.serial.Serial", return_value=port):
            await bridge.start()
            await asyncio.sleep(0.05)

            # Timeout cleans _pending
            timed_out = await bridge.request({"t": "ping", "id": "t1"}, timeout=0.05)
            self.assertEqual(timed_out["ok"], 0)
            self.assertIn("timeout", timed_out["error"].lower())
            self.assertNotIn("t1", bridge._pending)

            # Concurrent RPCs resolved out of order + unknown id ignored
            f1 = asyncio.create_task(bridge.request({"t": "ping", "id": "reqA"}, timeout=1.0))
            f2 = asyncio.create_task(bridge.request({"t": "ping", "id": "reqB"}, timeout=1.0))
            await asyncio.sleep(0.03)
            port.push_rx_line('{"t":"res","id":"unknown_id","ok":1}')
            port.push_rx_line('{"t":"res","id":"reqB","ok":1,"n":2}')
            port.push_rx_line('{"t":"res","id":"reqA","ok":1,"n":1}')
            r1, r2 = await asyncio.gather(f1, f2)
            self.assertEqual(r1["n"], 1)
            self.assertEqual(r2["n"], 2)
            await bridge.stop()

    async def test_usb_disconnect_and_auto_reconnect(self) -> None:
        import serial

        esp = MockESP32()
        port1 = FakeSerialPort(esp=esp)
        port2 = FakeSerialPort(esp=esp)
        ports = [port1, port2]

        bridge = EspSerialBridge(baudrate=921600)
        with (
            patch("pi_hub.esp_serial_bridge.serial.Serial", side_effect=lambda *a, **kw: ports.pop(0)),
            patch("pi_hub.esp_serial_bridge.time.sleep", return_value=None),
        ):
            await bridge.start()
            await asyncio.sleep(0.05)
            self.assertEqual((await bridge.request({"t": "ping"}, timeout=1.0))["ok"], 1)

            # Force disconnect on port1
            port1.fail_on_read_exc = serial.SerialException("USB unplugged")
            await asyncio.sleep(0.1)

            # Bridge should have reconnected on port2 and resume RPCs
            res2 = await bridge.request({"t": "ping"}, timeout=1.0)
            self.assertEqual(res2["ok"], 1)
            await bridge.stop()

    async def test_tier5_utf8_emoji_trimming_oversized_drop_surrogate_and_write_fail_reconnect(self) -> None:
        esp = MockESP32()

        class WriteFailPort(FakeSerialPort):
            def __init__(self, *args, **kwargs) -> None:
                super().__init__(*args, **kwargs)
                self.fail_writes = True

            def write(self, data: bytes) -> int:
                if self.fail_writes:
                    raise OSError("write EIO")
                return super().write(data)

        port1 = WriteFailPort(esp=esp)
        port2 = FakeSerialPort(esp=esp)
        ports = [port1, port2]

        bridge = EspSerialBridge(baudrate=921600)
        with (
            patch("pi_hub.esp_serial_bridge.serial.Serial", side_effect=lambda *a, **kw: ports.pop(0)),
            patch("pi_hub.esp_serial_bridge.time.sleep", return_value=None),
        ):
            await bridge.start()
            await asyncio.sleep(0.05)

            # Write failure closes port1 and triggers reconnect onto port2
            r1 = await bridge.request({"t": "ping"}, timeout=1.0)
            self.assertEqual(r1["ok"], 0)
            self.assertFalse(port1.is_open)

            deadline = time.monotonic() + 1.0
            while not port2.is_open and time.monotonic() < deadline:
                await asyncio.sleep(0.01)
            self.assertTrue(port2.is_open)

            r2 = await bridge.request({"t": "ping"}, timeout=1.0)
            self.assertEqual(r2["ok"], 1)

            # 4-byte UTF-8 emoji trimming (< 512 wire bytes, valid UTF-8 <= 191 bytes on ESP)
            ok_emoji = await bridge.send({"t": "state", "emo": "HAPPY", "sub": "😀" * 180})
            self.assertTrue(ok_emoji)
            self.assertLess(len(port2.written[-1]), 512)
            self.assertLessEqual(len(esp.sub.encode("utf-8")), 191)

            # Oversized non-sub frame dropped
            self.assertFalse(await bridge.send({"t": "ping", "id": "x" * 600}))

            # Lone UTF-16 surrogate never crashes send()
            self.assertTrue(await bridge.send({"t": "state", "emo": "HAPPY", "sub": "hi\ud800there"}))

            await bridge.stop()


class TestAudioSyncEngine(unittest.IsolatedAsyncioTestCase):
    """Tier 1–3 tests for AudioSyncEngine VAD/PTT/duplex, speaker scaling, visemes, and thread safety."""

    async def test_vad_preroll_and_ptt_capture_modes(self) -> None:
        sent_frames: list[bytes] = []
        speech_states: list[bool] = []

        async def send_frame(b: bytes) -> bool:
            sent_frames.append(b)
            return True

        async def on_state(active: bool) -> None:
            speech_states.append(active)

        engine = AudioSyncEngine(
            sample_rate=16000,
            frame_ms=100,
            vad_silence_ms=100,
            default_mic_mode="vad",
            send_audio_frame=send_frame,
            on_speech_state=on_state,
        )
        engine._vad = None  # Use RMS energy fallback
        engine._loop = asyncio.get_running_loop()
        engine._running = True

        quiet_20ms = b"\x00" * 640
        loud_20ms = struct.pack("<320h", *([4000] * 320))

        # 2 silent pre-roll frames + 1 loud voice frame -> triggers capture + pre-roll flush
        engine._on_mic_audio(quiet_20ms, 320, None, None)
        engine._on_mic_audio(quiet_20ms, 320, None, None)
        engine._on_mic_audio(loud_20ms, 320, None, None)
        # Send enough voiced frames to exceed 3200B chunk (5 * 640 = 3200)
        for _ in range(4):
            engine._on_mic_audio(loud_20ms, 320, None, None)
        await asyncio.sleep(0.03)
        self.assertIn(True, speech_states)
        self.assertGreaterEqual(sum(len(f) for f in sent_frames), 3200)

        # Wait past vad_silence_ms and feed silence -> triggers _finish_vad_turn
        await asyncio.sleep(0.12)
        engine._on_mic_audio(quiet_20ms, 320, None, None)
        await asyncio.sleep(0.05)
        self.assertIn(False, speech_states)

        # PTT mode gating
        sent_frames.clear()
        speech_states.clear()
        engine.set_mic_mode("ptt")
        self.assertEqual(engine.mic_mode, "ptt")
        engine._on_mic_audio(loud_20ms, 320, None, None)
        self.assertEqual(len(sent_frames), 0)

        self.assertTrue(await engine.toggle_ptt())
        engine._on_mic_audio(loud_20ms, 320, None, None)
        self.assertFalse(await engine.toggle_ptt())
        self.assertEqual(speech_states, [True, False])
        self.assertEqual(b"".join(sent_frames), loud_20ms)

    async def test_speaker_queue_volume_clamping_and_headless(self) -> None:
        engine = AudioSyncEngine(sample_rate=16000)
        engine.set_volume(-20)
        self.assertEqual(engine.volume, 0.0)
        engine.set_volume(150)
        self.assertEqual(engine.volume, 1.0)
        engine.set_volume(50)
        self.assertAlmostEqual(engine.volume, 0.5)

        # Headless sd=None starts/stops cleanly
        with patch.object(ase_mod, "sd", None):
            await engine.start()
            await engine.stop()

        # Empty TTS ignored; valid PCM scaled by 0.5x and zero-padded on underrun
        engine.feed_tts_audio(b"")
        pcm_in = struct.pack("<4h", 2000, -2000, 1000, -1000)
        engine.feed_tts_audio(pcm_in)
        out = bytearray(16)  # 8 frames * 2 bytes
        engine._on_speaker_audio(out, 8, None, None)
        samples_out = struct.unpack("<8h", out)
        self.assertEqual(samples_out[:4], (1000, -1000, 500, -500))
        self.assertEqual(samples_out[4:], (0, 0, 0, 0))

    async def test_viseme_pacemaker_bt_latency_and_empty_envelope(self) -> None:
        visemes: list[int] = []
        done_count = 0

        async def on_viseme(v: int) -> None:
            visemes.append(v)

        async def on_done() -> None:
            nonlocal done_count
            done_count += 1

        engine = AudioSyncEngine(bt_latency_ms=40, on_viseme=on_viseme, on_playback_done=on_done)
        t0 = time.monotonic()
        engine.start_viseme_pacemaker([40, 80, 20])
        assert engine._viseme_task is not None
        await asyncio.wait_for(engine._viseme_task, timeout=2.0)
        elapsed_ms = (time.monotonic() - t0) * 1000.0
        self.assertGreaterEqual(elapsed_ms, 35.0)
        self.assertEqual(visemes, [40, 80, 20, 0])
        self.assertEqual(done_count, 1)
        self.assertFalse(engine.is_speaking)

        # Empty envelope still emits trailing 0 and calls on_playback_done
        visemes.clear()
        engine.bt_latency_ms = 0
        engine.start_viseme_pacemaker([])
        assert engine._viseme_task is not None
        await asyncio.wait_for(engine._viseme_task, timeout=2.0)
        self.assertEqual(visemes, [0])
        self.assertEqual(done_count, 2)

    async def test_duplex_mic_mode_and_tx_accum_thread_safety(self) -> None:
        # M3 contract (PROJECT.md:94,107): duplex mode must be preserved and capture while speaking
        sent: list[bytes] = []

        async def send_fn(b: bytes) -> bool:
            sent.append(b)
            return True

        engine = AudioSyncEngine(sample_rate=16000, frame_ms=20, send_audio_frame=send_fn)
        engine._loop = asyncio.get_running_loop()
        engine._running = True
        engine._vad = None
        engine.set_mic_mode("duplex")
        self.assertEqual(engine.mic_mode, "duplex")

        engine._is_speaking = True
        loud = struct.pack("<320h", *([5000] * 320))
        engine._on_mic_audio(loud, 320, None, None)
        await asyncio.sleep(0.02)
        self.assertGreater(len(sent), 0)

        # Thread-safe _tx_accum stress (M3 contract: PROJECT.md:55,107)
        sent.clear()
        engine2 = AudioSyncEngine(sample_rate=16000, frame_ms=100, send_audio_frame=send_fn)
        engine2._loop = asyncio.get_running_loop()
        engine2._running = True
        engine2.set_mic_mode("ptt")
        errors: list[Exception] = []
        chunk = b"\x01\x02" * 320  # 640 bytes
        produced_bytes = 0

        def producer() -> None:
            nonlocal produced_bytes
            try:
                for _ in range(150):
                    engine2._queue_tx_bytes(chunk)
                    produced_bytes += len(chunk)
                    time.sleep(0.0005)
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        t = threading.Thread(target=producer)
        t.start()
        while t.is_alive():
            await engine2._flush_tx_accum()
            await asyncio.sleep(0.001)
        await engine2._flush_tx_accum()
        await asyncio.sleep(0.05)

        self.assertEqual(errors, [])
        self.assertEqual(sum(len(f) for f in sent), produced_bytes)

    async def test_tier5_no_duplicate_playback_done_and_ptt_while_speaking(self) -> None:
        states: list[str] = []
        mouths: list[int] = []
        sent_ptt: list[bytes] = []

        async def on_viseme(v: int) -> None:
            mouths.append(v)

        async def on_done() -> None:
            states.append("IDLE_FROM_DONE")

        async def on_speech_state(active: bool) -> None:
            states.append("LISTENING" if active else "THINKING")

        async def send_frame(b: bytes) -> bool:
            sent_ptt.append(b)
            return True

        eng = AudioSyncEngine(
            sample_rate=16000,
            frame_ms=20,
            bt_latency_ms=0,
            default_mic_mode="ptt",
            send_audio_frame=send_frame,
            on_speech_state=on_speech_state,
            on_viseme=on_viseme,
            on_playback_done=on_done,
        )
        eng._loop = asyncio.get_running_loop()
        eng._running = True

        # 1. Viseme completion followed by finish_tts_stream(interrupted=False) must NOT duplicate on_playback_done
        eng.start_viseme_pacemaker([40])
        assert eng._viseme_task is not None
        await asyncio.wait_for(eng._viseme_task, timeout=1.0)
        states.append("LISTENING")
        await eng.finish_tts_stream(interrupted=False)
        self.assertEqual(states, ["IDLE_FROM_DONE", "LISTENING"])
        self.assertEqual(mouths, [40, 0])

        # 2. PTT frames must be captured even if _is_speaking is True
        self.assertTrue(await eng.toggle_ptt())
        eng._is_speaking = True
        loud = struct.pack("<320h", *([4000] * 320))
        eng._on_mic_audio(loud, 320, None, None)
        await asyncio.sleep(0.03)
        self.assertEqual(len(sent_ptt), 1)
        self.assertEqual(sent_ptt[0], loud)

    async def test_wyoming_dsp_preprocessor_ns_and_agc(self) -> None:
        """Verify AudioPreprocessor high-pass rumble removal, noise suppression, and auto-gain."""
        proc_off = ase_mod.AudioPreprocessor(16000, noise_suppression_level=0, auto_gain_dbfs=0)
        self.assertFalse(proc_off.enabled)
        raw_quiet = struct.pack("<320h", *([90] * 320))
        self.assertEqual(proc_off.process(raw_quiet), raw_quiet)

        # Noise suppression (level 4) attenuates low-amplitude background hum
        proc_ns = ase_mod.AudioPreprocessor(16000, noise_suppression_level=4, auto_gain_dbfs=0)
        self.assertTrue(proc_ns.enabled)
        hum = struct.pack("<320h", *([120 if i % 2 == 0 else -120 for i in range(320)]))
        out_ns = proc_ns.process(hum)
        in_samples = struct.unpack("<320h", hum)
        out_samples = struct.unpack("<320h", out_ns)
        in_rms = (sum(s * s for s in in_samples) / 320) ** 0.5
        out_rms = (sum(s * s for s in out_samples) / 320) ** 0.5
        self.assertLess(out_rms, in_rms * 0.7)

        # Auto-gain (18 dBFS) boosts quiet speech signal without clipping
        proc_agc = ase_mod.AudioPreprocessor(16000, noise_suppression_level=0, auto_gain_dbfs=18)
        voice_quiet = struct.pack(
            "<320h",
            *([int(500 * math.sin(2.0 * math.pi * 400.0 * (i / 16000.0))) for i in range(320)]),
        )
        for _ in range(3):
            boosted = proc_agc.process(voice_quiet)
        in_v = struct.unpack("<320h", voice_quiet)
        out_v = struct.unpack("<320h", boosted)
        in_v_rms = (sum(s * s for s in in_v) / 320) ** 0.5
        out_v_rms = (sum(s * s for s in out_v) / 320) ** 0.5
        self.assertGreater(out_v_rms, in_v_rms * 1.8)

    async def test_numpy_vectorized_dsp_matches_pure_python_reference(self) -> None:
        """Pi Zero 2 W optimization: vectorized high-pass/NS/AGC must match the pure-Python reference."""
        if not ase_mod._REAL_NUMPY:
            self.skipTest("real NumPy not installed")
        frames = [
            struct.pack(
                "<1600h",
                *[int(6000 * math.sin(2 * math.pi * 300.0 * ((f * 1600 + i) / 16000.0))) + 900 for i in range(1600)],
            )
            for f in range(4)
        ]
        proc_np = ase_mod.AudioPreprocessor(16000, noise_suppression_level=2, auto_gain_dbfs=10)
        proc_py = ase_mod.AudioPreprocessor(16000, noise_suppression_level=2, auto_gain_dbfs=10)
        for frame in frames:
            ase_mod._REAL_NUMPY = True
            out_np = proc_np.process(frame)
            ase_mod._REAL_NUMPY = False
            try:
                out_py = proc_py.process(frame)
            finally:
                ase_mod._REAL_NUMPY = True
            a = struct.unpack("<1600h", out_np)
            b = struct.unpack("<1600h", out_py)
            self.assertLessEqual(max(abs(x - y) for x, y in zip(a, b)), 2)

    async def test_wyoming_vad_trigger_level_debounce_and_max_turn_cutoff(self) -> None:
        """Verify M-of-K VAD onset debounce rejects transient clicks and max_turn_seconds terminates runaway turns."""
        sent: list[bytes] = []
        states: list[bool] = []

        async def send_frame(b: bytes) -> bool:
            sent.append(b)
            return True

        async def on_state(active: bool) -> None:
            states.append(active)

        eng = AudioSyncEngine(
            sample_rate=16000,
            frame_ms=20,
            vad_silence_ms=500,
            vad_trigger_level=3,
            max_turn_seconds=0.08,
            send_audio_frame=send_frame,
            on_speech_state=on_state,
        )
        eng._vad = None
        eng._loop = asyncio.get_running_loop()
        eng._running = True

        quiet = b"\x00" * 640
        loud = struct.pack("<320h", *([4000] * 320))

        # 2 loud frames followed by silence must NOT trigger a turn when vad_trigger_level=3
        eng._on_mic_audio(loud, 320, None, None)
        eng._on_mic_audio(loud, 320, None, None)
        eng._on_mic_audio(quiet, 320, None, None)
        await asyncio.sleep(0.02)
        self.assertFalse(eng.is_capturing)
        self.assertEqual(states, [])

        # 3 consecutive loud frames trigger turn and flush pre-roll
        eng._on_mic_audio(loud, 320, None, None)
        eng._on_mic_audio(loud, 320, None, None)
        eng._on_mic_audio(loud, 320, None, None)
        await asyncio.sleep(0.02)
        self.assertTrue(eng.is_capturing)
        self.assertEqual(states, [True])
        self.assertGreaterEqual(len(sent), 3)

        # Wait past max_turn_seconds (80ms) while still feeding loud audio -> safety cutoff finishes turn
        await asyncio.sleep(0.10)
        eng._on_mic_audio(loud, 320, None, None)
        await asyncio.sleep(0.03)
        self.assertFalse(eng.is_capturing)
        self.assertEqual(states, [True, False])

    async def test_wyoming_wake_word_refractory_earcons_and_followup_window(self) -> None:
        """Verify 'wake' mic mode, refractory cooldown, local earcons, and post-TTS follow-up window."""
        sent: list[bytes] = []
        states: list[bool] = []
        wakes: list[int] = []
        wake_flag = False

        async def send_frame(b: bytes) -> bool:
            sent.append(b)
            return True

        async def on_state(active: bool) -> None:
            states.append(active)

        async def on_wake() -> None:
            wakes.append(1)

        with tempfile.TemporaryDirectory() as tmp:
            custom_wav = Path(tmp) / "awake.wav"
            with ase_mod.wave.open(str(custom_wav), "wb") as wf:
                wf.setnchannels(2)
                wf.setsampwidth(2)
                wf.setframerate(8000)
                wf.writeframes(struct.pack("<8h", 1000, 1000, -1000, -1000, 500, 500, -500, -500))

            eng = AudioSyncEngine(
                sample_rate=16000,
                frame_ms=20,
                bt_latency_ms=0,
                vad_silence_ms=60,
                default_mic_mode="wake",
                earcons_enabled=True,
                awake_wav=custom_wav,
                wake_refractory_ms=500,
                followup_window_ms=400,
                wake_detector_fn=lambda _pcm: wake_flag,
                send_audio_frame=send_frame,
                on_speech_state=on_state,
                on_wake_detected=on_wake,
            )
            eng._vad = None
            eng._loop = asyncio.get_running_loop()
            eng._running = True

            quiet = b"\x00" * 640
            loud = struct.pack("<320h", *([4500] * 320))

            # Speaking without wake word does not trigger in 'wake' mode
            eng._on_mic_audio(loud, 320, None, None)
            await asyncio.sleep(0.02)
            self.assertFalse(eng.is_capturing)
            self.assertEqual(wakes, [])

            # Wake word fires -> turn starts, custom awake earcon queued, on_wake_detected called
            wake_flag = True
            eng._on_mic_audio(loud, 320, None, None)
            wake_flag = False
            await asyncio.sleep(0.03)
            self.assertTrue(eng.is_capturing)
            self.assertEqual(wakes, [1])
            self.assertEqual(states, [True])
            self.assertFalse(eng._play_queue.empty())

            # Finish turn via silence -> 'done' earcon queued
            await asyncio.sleep(0.08)
            eng._on_mic_audio(quiet, 320, None, None)
            await asyncio.sleep(0.03)
            self.assertFalse(eng.is_capturing)
            self.assertEqual(states, [True, False])

            # Refractory period (500ms) prevents immediate wake re-trigger
            wake_flag = True
            eng._on_mic_audio(loud, 320, None, None)
            wake_flag = False
            await asyncio.sleep(0.02)
            self.assertFalse(eng.is_capturing)
            self.assertEqual(wakes, [1])

            # After TTS finishes, follow-up conversation window arms automatically
            eng.start_viseme_pacemaker([50])
            assert eng._viseme_task is not None
            await asyncio.wait_for(eng._viseme_task, timeout=1.0)
            self.assertTrue(eng.is_in_followup_window)

            # User replies during follow-up window without wake word -> starts turn via VAD!
            eng._on_mic_audio(loud, 320, None, None)
            await asyncio.sleep(0.03)
            self.assertTrue(eng.is_capturing)
            self.assertEqual(states, [True, False, True])

    async def test_wyoming_bluetooth_stream_watchdog_auto_heal(self) -> None:
        """Verify check_and_heal_streams recovers stalled or closed PortAudio streams."""
        statuses: list[bool] = []

        async def on_status(ok: bool) -> None:
            statuses.append(ok)

        eng = AudioSyncEngine(
            sample_rate=16000,
            stream_stall_timeout_s=2.0,
            on_stream_status=on_status,
        )
        await eng.start()
        try:
            # Healthy stream -> no recovery triggered
            now = time.monotonic()
            eng._last_mic_rx_ts = now
            self.assertFalse(await eng.check_and_heal_streams(now=now + 0.5))
            self.assertEqual(eng.stream_recoveries, 0)

            # Simulate Bluetooth mic stall beyond stream_stall_timeout_s (2.0s)
            self.assertTrue(await eng.check_and_heal_streams(now=now + 3.0))
            self.assertEqual(eng.stream_recoveries, 1)
            self.assertEqual(statuses, [True])

            # Simulate stream unexpectedly closed by OS audio daemon
            eng._out_stream.closed = True
            self.assertTrue(await eng.check_and_heal_streams(now=now + 3.5))
            self.assertEqual(eng.stream_recoveries, 2)
            self.assertEqual(statuses, [True, True])
        finally:
            await eng.stop()


class TestPiCompanionHub(unittest.IsolatedAsyncioTestCase):
    """Tier 1–4 tests for PiCompanionHub tool execution, events, barge-in, and full loop."""

    async def test_all_six_tools_and_tool_batch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hub, esp, bus, port = _make_hub_with_mocks(Path(tmp))
            with patch("pi_hub.esp_serial_bridge.serial.Serial", return_value=port):
                await hub.esp.start()
                await asyncio.sleep(0.04)

                bus_msgs: list[dict] = []
                orig_send = hub.quixi.send_app_data

                async def spy_send_app_data(addr: str, data_str: str, protocol_id: str = AGENT_PROTOCOL_ID):
                    bus_msgs.append(json.loads(data_str))
                    return await orig_send(addr, data_str, protocol_id=protocol_id)

                hub.quixi.send_app_data = spy_send_app_data  # type: ignore[method-assign]

                async def dispatch_proto(msg: dict) -> None:
                    b64 = base64.b64encode(json.dumps(msg).encode("utf-8")).decode("ascii")
                    await hub._on_app_protocol_data(
                        "AppProtocolData/1SrvAddrTest",
                        {"sender": "1SrvAddrTest", "data": {"data": b64}},
                    )

                # 1. set_companion_emotion
                await dispatch_proto({"kind": "tool_call", "id": "c1", "tool": "set_companion_emotion", "args": {"emotion": "ALERT", "subtitle": "Look!"}})
                # 2. gpio_configure_and_write
                await dispatch_proto({"kind": "tool_call", "id": "c2", "tool": "gpio_configure_and_write", "args": {"pin": 16, "mode": "out", "state": 1}})
                # 3. gpio_read_pin with pull="up" (M3 contract)
                await dispatch_proto({"kind": "tool_call", "id": "c3", "tool": "gpio_read_pin", "args": {"pin": 15, "pull": "up"}})
                # 4. scan_ble_environment with min_rssi
                await dispatch_proto({"kind": "tool_call", "id": "c4", "tool": "scan_ble_environment", "args": {"min_rssi": -60}})
                # 5. read_imu_motion
                await dispatch_proto({"kind": "tool_call", "id": "c5", "tool": "read_imu_motion", "args": {}})
                # 6. update_companion_settings with brightness (M3 contract)
                await dispatch_proto({"kind": "tool_call", "id": "c6", "tool": "update_companion_settings", "args": {"brightness": 35, "volume": 60, "mic_mode": "ptt"}})
                # 7. unknown tool
                await dispatch_proto({"kind": "tool_call", "id": "c7", "tool": "nonexistent_tool", "args": {}})
                # 8. tool_batch (M3 contract)
                await dispatch_proto(
                    {
                        "kind": "tool_batch",
                        "id": "b1",
                        "calls": [
                            {"id": "b1_0", "tool": "gpio_configure_and_write", "args": {"pin": 17, "mode": "out", "state": 1}},
                            {"id": "b1_1", "tool": "read_imu_motion", "args": {}},
                        ],
                    }
                )
                await hub.esp.stop()

        by_id = {m["id"]: m for m in bus_msgs}
        self.assertTrue(by_id["c1"]["result"]["ok"])
        self.assertEqual(esp.gpio_vals[16], 1)
        self.assertTrue(by_id["c2"]["result"]["ok"])
        self.assertEqual(esp.gpio_pulls.get(15), "up")
        self.assertEqual(by_id["c3"]["result"]["vbat_mv"], 4110)
        self.assertEqual(by_id["c4"]["result"]["cnt"], 1)
        self.assertEqual(by_id["c4"]["result"]["ble"][0]["name"], "KeyFob")
        self.assertIn("ax", by_id["c5"]["result"])
        self.assertEqual(esp.bright, 35)
        self.assertEqual(int(round(hub.audio.volume * 100)), 60)
        self.assertEqual(hub.audio.mic_mode, "ptt")
        self.assertFalse(by_id["c7"]["result"]["ok"])
        self.assertIn("Unknown tool", by_id["c7"]["result"]["error"])
        # tool_batch produced response with id="b1"
        self.assertIn("b1", by_id)
        self.assertEqual(esp.gpio_vals[17], 1)

    async def test_esp_events_barge_in_tts_end_and_chat_stream(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hub, esp, bus, port = _make_hub_with_mocks(Path(tmp))
            sent_to_srv: list[dict] = []

            async def spy_send_app_data(addr: str, data_str: str, protocol_id: str = AGENT_PROTOCOL_ID):
                sent_to_srv.append(json.loads(data_str))
                return {}

            hub.quixi.send_app_data = spy_send_app_data  # type: ignore[method-assign]
            with patch("pi_hub.esp_serial_bridge.serial.Serial", return_value=port):
                await hub.esp.start()
                await asyncio.sleep(0.04)

                # cfg events (bright, ble_en, vol, mode)
                await hub._on_esp_event({"t": "evt", "src": "cfg", "vol": 40, "mode": "duplex", "bright": 55, "ble_en": 0})
                self.assertAlmostEqual(hub.audio.volume, 0.4)
                self.assertEqual(hub.audio.mic_mode, "duplex")

                # imu gestures (shake, tilt, pickup, double_tap) -> forwarded as hw_event (M3 contract)
                for g in ("shake", "tilt", "pickup", "double_tap"):
                    await hub._on_esp_event({"t": "evt", "src": "imu", "gest": g})
                forwarded_gests = [m["event"]["gest"] for m in sent_to_srv if m.get("kind") == "hw_event" and m["event"].get("src") == "imu"]
                self.assertEqual(forwarded_gests, ["shake", "tilt", "pickup", "double_tap"])

                # dbg ping + pong
                await hub._on_esp_event({"t": "evt", "src": "dbg", "act": "ping"})
                pong_b64 = base64.b64encode(json.dumps({"kind": "pong", "ts": 1.0}).encode()).decode("ascii")
                await hub._on_app_protocol_data("AppProtocolData/1SrvAddrTest", {"sender": "1SrvAddrTest", "data": {"data": pong_b64}})

                # ChatStream + malformed JSON safety
                await hub._on_chat_stream("ChatStream/1SrvAddrTest", {"sender": "1SrvAddrTest", "data": {"data": base64.b64encode(b"Hi user").decode("ascii")}})
                self.assertEqual(esp.emo, "SPEAKING")
                self.assertEqual(esp.sub, "Hi user")
                await hub._on_app_protocol_data("AppProtocolData/1SrvAddrTest", {"sender": "1SrvAddrTest", "data": {"data": base64.b64encode(b"{bad").decode("ascii")}})

                # tts_meta + tts_chunk + barge-in stop_speech (M3 contract) + tts_end (M3 contract)
                meta_b64 = base64.b64encode(json.dumps({"kind": "tts_meta", "emo": "HAPPY", "text": "Talking", "envelope": [50, 60, 70]}).encode()).decode("ascii")
                chunk_b64 = base64.b64encode(json.dumps({"kind": "tts_chunk", "pcm_b64": base64.b64encode(make_pcm16_sine(440, 0.05, 16000)).decode("ascii")}).encode()).decode("ascii")
                end_b64 = base64.b64encode(json.dumps({"kind": "tts_end", "turn_id": "t1"}).encode()).decode("ascii")
                await hub._on_app_protocol_data("AppProtocolData/1SrvAddrTest", {"sender": "1SrvAddrTest", "data": {"data": meta_b64}})
                await hub._on_app_protocol_data("AppProtocolData/1SrvAddrTest", {"sender": "1SrvAddrTest", "data": {"data": chunk_b64}})
                self.assertTrue(hub.audio.is_speaking)

                await hub._on_esp_event({"t": "evt", "src": "touch", "act": "stop_speech"})
                self.assertFalse(hub.audio.is_speaking)
                barge_msgs = [
                    m
                    for m in sent_to_srv
                    if m.get("kind") in {"barge_in", "stop_speech"}
                    or m.get("act") == "stop_speech"
                    or (
                        m.get("kind") == "hw_event"
                        and isinstance(m.get("event"), dict)
                        and m["event"].get("act") == "stop_speech"
                    )
                ]
                self.assertGreaterEqual(len(barge_msgs), 1)

                # tts_end handling
                await hub._on_app_protocol_data("AppProtocolData/1SrvAddrTest", {"sender": "1SrvAddrTest", "data": {"data": end_b64}})
                await asyncio.sleep(0.03)
                await hub.esp.stop()

    async def test_tier5_sender_auth_uppercase_mode_multibyte_sub_and_late_tts_end(self) -> None:
        from common_ixian.spixi_voip import SPIXI_VOIP_APP_ID, AppDataMessage

        with tempfile.TemporaryDirectory() as tmp:
            hub, esp, bus, port = _make_hub_with_mocks(Path(tmp), mic_mode="ptt")
            hub.quixi._loop = asyncio.get_running_loop()
            with patch("pi_hub.esp_serial_bridge.serial.Serial", return_value=port):
                await hub.esp.start()
                await asyncio.sleep(0.04)

                # 1. Unauthorized AppProtocolData, ChatStream, and VoIP call must be rejected
                evil_tool = base64.b64encode(
                    json.dumps(
                        {
                            "kind": "tool_call",
                            "id": "evil1",
                            "tool": "gpio_configure_and_write",
                            "args": {"pin": 16, "mode": "out", "state": 1},
                        }
                    ).encode()
                ).decode("ascii")
                await hub._on_app_protocol_data(
                    "AppProtocolData/1Attacker", {"sender": "1Attacker", "data": {"data": evil_tool}}
                )
                await hub._on_chat_stream(
                    "ChatStream/1Attacker",
                    {"sender": "1Attacker", "data": {"data": base64.b64encode(b"Pwned").decode("ascii")}},
                )
                self.assertEqual(esp.gpio_vals[16], 0)
                self.assertNotEqual(esp.sub, "Pwned")
                self.assertFalse(
                    await hub.voip.on_received_call(
                        "1Attacker",
                        AppDataMessage(session_id=b"evil_sid", data=b"pcm", app_id=SPIXI_VOIP_APP_ID),
                    )
                )

                # 2. Uppercase mode="OUT" normalized to "out"
                good_tool = base64.b64encode(
                    json.dumps(
                        {
                            "kind": "tool_call",
                            "id": "upper_out",
                            "tool": "gpio_configure_and_write",
                            "args": {"pin": 16, "mode": "OUT", "state": 1},
                        }
                    ).encode()
                ).decode("ascii")
                await hub._on_app_protocol_data(
                    "AppProtocolData/1SrvAddrTest", {"sender": "1SrvAddrTest", "data": {"data": good_tool}}
                )
                self.assertEqual(esp.gpio_dirs.get(16), "out")
                self.assertEqual(esp.gpio_vals[16], 1)

                # 3. Multi-byte UTF-8 subtitle (180 CJK chars) does not split codepoints
                cjk_sub = "中" * 180
                await hub.esp.set_state("SPEAKING", sub=cjk_sub)
                await asyncio.sleep(0.05)
                self.assertLessEqual(len(esp.sub.encode("utf-8")), 191)
                self.assertTrue(all(ch == "中" for ch in esp.sub))

                # 4. Early barge_in before tts_meta + Turn 2 PTT LISTENING not clobbered by late tts_end(interrupted=True)
                await hub._on_esp_event({"t": "evt", "src": "touch", "act": "stop_speech"})
                await hub._on_esp_event({"t": "evt", "src": "touch", "act": "ptt_toggle"})
                await asyncio.sleep(0.03)
                self.assertTrue(hub.audio._ptt_active)
                self.assertEqual(esp.emo, "LISTENING")

                late_end = base64.b64encode(
                    json.dumps({"kind": "tts_end", "turn_id": "late_turn1", "interrupted": True}).encode()
                ).decode("ascii")
                await hub._on_app_protocol_data(
                    "AppProtocolData/1SrvAddrTest", {"sender": "1SrvAddrTest", "data": {"data": late_end}}
                )
                await asyncio.sleep(0.03)
                self.assertTrue(hub.audio._ptt_active)
                self.assertEqual(esp.emo, "LISTENING")

                await hub.esp.stop()

    async def test_tier5_ptt_toggle_pre_tts_meta_sends_barge_in_and_guards_listening(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hub, esp, bus, port = _make_hub_with_mocks(Path(tmp), mic_mode="ptt")
            hub.quixi._loop = asyncio.get_running_loop()
            sent_to_srv: list[dict] = []

            async def spy_send_app_data(addr: str, data_str: str, protocol_id: str = AGENT_PROTOCOL_ID):
                sent_to_srv.append(json.loads(data_str))
                return {}

            hub.quixi.send_app_data = spy_send_app_data  # type: ignore[method-assign]
            with patch("pi_hub.esp_serial_bridge.serial.Serial", return_value=port):
                await hub.esp.start()
                await asyncio.sleep(0.04)

                # Turn 1 is pre-tts_meta (not speaking yet)
                self.assertFalse(hub.audio.is_speaking)
                hub._current_tts_turn_id = "turn1_stale"

                # User taps ptt_toggle ON for Turn 2
                await hub._on_esp_event({"t": "evt", "src": "touch", "act": "ptt_toggle"})
                await asyncio.sleep(0.03)
                self.assertTrue(hub.audio._ptt_active)
                self.assertTrue(hub.audio.is_capturing)
                self.assertEqual(esp.emo, "LISTENING")
                self.assertIsNone(hub._current_tts_turn_id)
                self.assertIn({"kind": "barge_in", "act": "stop_speech"}, sent_to_srv)

                # Late Turn 1 ChatStream, face_state, tts_meta, tts_chunk, and VoIP audio must be ignored
                await hub._on_chat_stream(
                    "ChatStream/1SrvAddrTest",
                    {
                        "sender": "1SrvAddrTest",
                        "data": {
                            "messageId": "m_turn1",
                            "isStream": True,
                            "data": base64.b64encode(b"Turn 1 reply").decode("ascii"),
                        },
                    },
                )
                for msg in (
                    {"kind": "face_state", "emo": "HAPPY", "sub": "Turn 1 face"},
                    {"kind": "tts_meta", "turn_id": "t1_late", "emo": "HAPPY", "text": "Turn 1 reply", "envelope": [60, 70]},
                    {"kind": "tts_chunk", "turn_id": "t1_late", "pcm_b64": base64.b64encode(make_pcm16_sine(440, 0.05, 16000)).decode("ascii")},
                ):
                    b64 = base64.b64encode(json.dumps(msg).encode("utf-8")).decode("ascii")
                    await hub._on_app_protocol_data(
                        "AppProtocolData/1SrvAddrTest",
                        {"sender": "1SrvAddrTest", "data": {"data": b64}},
                    )
                await hub._on_voip_rx_audio(b"sid1", make_pcm16_sine(440, 0.05, 16000), "pcm")
                await asyncio.sleep(0.03)

                self.assertEqual(esp.emo, "LISTENING")
                self.assertFalse(hub.audio.is_speaking)
                self.assertTrue(hub.audio._play_queue.empty())
                self.assertTrue(hub.audio._ptt_active)

                # Even after PTT OFF, late chunk from superseded t1_late stays ignored
                await hub._on_esp_event({"t": "evt", "src": "touch", "act": "ptt_toggle"})
                await asyncio.sleep(0.03)
                late_chunk = base64.b64encode(
                    json.dumps(
                        {
                            "kind": "tts_chunk",
                            "turn_id": "t1_late",
                            "pcm_b64": base64.b64encode(make_pcm16_sine(440, 0.05, 16000)).decode("ascii"),
                        }
                    ).encode("utf-8")
                ).decode("ascii")
                await hub._on_app_protocol_data(
                    "AppProtocolData/1SrvAddrTest",
                    {"sender": "1SrvAddrTest", "data": {"data": late_chunk}},
                )
                self.assertTrue(hub.audio._play_queue.empty())

                await hub.esp.stop()


class TestPiWebGUIQuIXIAndESP32Monitor(unittest.IsolatedAsyncioTestCase):
    """Tests for PiWebGUI: QuIXI account creation, wallet, contacts/friend requests, ESP32-S3 live monitor, and WebSocket."""

    async def test_pi_web_gui_quixi_account_contacts_voip_and_rpc(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            hub, esp, bus, port = _make_hub_with_mocks(tmp_path)
            gui = hub.gui
            gui.ixian_cfg_path = tmp_path / "ixian.cfg"
            gui.auth_token = "pi-admin-token"

            # 1. Token auth gate
            code, _ = await gui.handle_api_request("GET", "/api/status")
            self.assertEqual(code, 401)
            hdrs = {"Authorization": "Bearer pi-admin-token"}

            # 2. QuIXI Account Provisioning (ixian.cfg with masked password)
            code, prov = await gui.handle_api_request(
                "POST",
                "/api/quixi/account/provision",
                body={
                    "walletPassword": "MySuperSecretWalletPassword123!",
                    "networkType": "testnet",
                    "apiPort": 8001,
                    "mqPort": 1883,
                },
                headers=hdrs,
            )
            self.assertEqual(code, 200)
            self.assertTrue(prov["cfg"]["walletPasswordSet"])
            self.assertEqual(prov["cfg"]["walletPasswordMasked"], "***")
            self.assertEqual(prov["cfg"]["networkType"], "testnet")
            self.assertNotIn("MySuperSecretWalletPassword123!", json.dumps(prov))
            self.assertIn("MySuperSecretWalletPassword123!", gui.ixian_cfg_path.read_text(encoding="utf-8"))

            # 3. Account Nickname, New Wallet Address Generation, Wallet View & Backup
            code, nick_res = await gui.handle_api_request(
                "POST",
                "/api/quixi/account/nick",
                body={"nick": "Alf-LivingRoom-Pi"},
                headers=hdrs,
            )
            self.assertEqual(code, 200)
            self.assertEqual(nick_res["nick"], "Alf-LivingRoom-Pi")

            code, new_addr = await gui.handle_api_request(
                "POST",
                "/api/quixi/account/new_address",
                body={},
                headers=hdrs,
            )
            self.assertEqual(code, 200)
            self.assertIn("_sub1", str(new_addr["address"]))

            code, wal = await gui.handle_api_request("GET", "/api/quixi/account/wallet", headers=hdrs)
            self.assertEqual(code, 200)
            self.assertEqual(wal["balance"], "100.00000000")

            code, bkp = await gui.handle_api_request("GET", "/api/quixi/account/backup", headers=hdrs)
            self.assertEqual(code, 200)
            self.assertIn("IXI_WALLET_BACKUP", str(bkp["backup"]))

            # 4. Contacts: Send Friend Request (/addContact), Accept (/acceptContact), Search, Remove (/removeContact)
            code, add_res = await gui.handle_api_request(
                "POST",
                "/api/quixi/contacts/add",
                body={"address": "1FriendWalletABC"},
                headers=hdrs,
            )
            self.assertEqual(code, 200)

            code, acc_res = await gui.handle_api_request(
                "POST",
                "/api/quixi/contacts/accept",
                body={"address": "1FriendWalletXYZ"},
                headers=hdrs,
            )
            self.assertEqual(code, 200)

            code, c_list = await gui.handle_api_request(
                "GET", "/api/quixi/contacts?search=xyz", headers=hdrs
            )
            self.assertEqual(code, 200)
            self.assertEqual(c_list["contacts"], ["1FriendWalletXYZ"])

            code, rem_res = await gui.handle_api_request(
                "POST",
                "/api/quixi/contacts/remove",
                body={"address": "1FriendWalletABC"},
                headers=hdrs,
            )
            self.assertEqual(code, 200)

            # 5. One-Click Pair AI Server + VoIP Dial + Encrypted Chat + Generic RPC Explorer
            code, pair_res = await gui.handle_api_request(
                "POST",
                "/api/quixi/pair_server",
                body={"server_ixian_address": "1NewAIServerWallet", "dial_voip": True},
                headers=hdrs,
            )
            self.assertEqual(code, 200)
            self.assertEqual(hub.server_address, "1NewAIServerWallet")
            self.assertTrue(pair_res["voip_dialed"])

            code, chat_res = await gui.handle_api_request(
                "POST",
                "/api/quixi/chat",
                body={"address": "1NewAIServerWallet", "message": "Hello Server from Pi GUI!"},
                headers=hdrs,
            )
            self.assertEqual(code, 200)

            code, rpc_res = await gui.handle_api_request(
                "POST",
                "/api/quixi/rpc",
                body={"method": "status", "params": {}},
                headers=hdrs,
            )
            self.assertEqual(code, 200)
            self.assertEqual(rpc_res["response"]["result"]["nodeStatus"], "Running")

            code, ov = await gui.handle_api_request("GET", "/api/quixi/overview", headers=hdrs)
            self.assertEqual(code, 200)
            self.assertEqual(ov["nick"], "Alf-LivingRoom-Pi")

    async def test_pi_web_gui_esp32_live_monitoring_and_http_websocket(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hub, esp, bus, port = _make_hub_with_mocks(Path(tmp))
            gui = hub.gui
            gui.host = "127.0.0.1"
            gui.port = 0

            with patch("pi_hub.esp_serial_bridge.serial.Serial", return_value=port):
                await hub.esp.start()
                bound_port = await gui.start()
                self.assertGreater(bound_port, 0)
                try:
                    # 1. Ping ESP32 over 921600 serial bridge
                    code, ping_res = await gui.handle_api_request("POST", "/api/esp32/ping")
                    self.assertEqual(code, 200)
                    self.assertTrue(ping_res["ok"])

                    # 2. Set ESP32 face emotion, subtitle & mouth
                    code, st_res = await gui.handle_api_request(
                        "POST",
                        "/api/esp32/state",
                        body={"emotion": "SURPRISED", "subtitle": "Pi GUI Test!", "mouth": 65},
                    )
                    self.assertEqual(code, 200)
                    await asyncio.sleep(0.03)
                    self.assertEqual(esp.emo, "SURPRISED")
                    self.assertEqual(esp.sub, "Pi GUI Test!")
                    self.assertEqual(esp.mouth, 65)

                    # 3. Update ESP32 display brightness, speaker volume, and 'wake' mic mode
                    code, cfg_res = await gui.handle_api_request(
                        "POST",
                        "/api/esp32/cfg",
                        body={"brightness": 55, "volume": 80, "mic_mode": "wake"},
                    )
                    self.assertEqual(code, 200)
                    self.assertEqual(hub.audio.mic_mode, "wake")
                    self.assertEqual(esp.bright, 55)

                    # 4. Poll QMI8658 6-axis IMU & BLE Radar
                    code, imu_res = await gui.handle_api_request("POST", "/api/esp32/imu")
                    self.assertEqual(code, 200)
                    self.assertTrue(imu_res["ok"])
                    self.assertIn("ax", imu_res["imu"])

                    code, ble_res = await gui.handle_api_request(
                        "POST", "/api/esp32/ble", body={"min_rssi": -70}
                    )
                    self.assertEqual(code, 200)
                    self.assertTrue(ble_res["ok"])
                    self.assertTrue(all(d["rssi"] >= -70 for d in ble_res["ble"]))

                    # 5. Allow-listed GPIO write + read with Vbat ADC
                    code, gw_res = await gui.handle_api_request(
                        "POST",
                        "/api/esp32/gpio",
                        body={"action": "write", "pin": 16, "dir": "out", "pull": "none", "val": 1},
                    )
                    self.assertEqual(code, 200)
                    self.assertTrue(gw_res["ok"])

                    code, gr_res = await gui.handle_api_request(
                        "POST",
                        "/api/esp32/gpio",
                        body={"action": "get", "pin": 16},
                    )
                    self.assertEqual(code, 200)
                    self.assertEqual(gr_res["response"]["val"], 1)
                    self.assertGreater(gui.esp_telemetry["vbat_mv"], 3000)

                    # 6. Audio earcon & wake trigger control
                    code, ear_res = await gui.handle_api_request(
                        "POST",
                        "/api/audio/control",
                        body={"action": "earcon", "kind": "awake"},
                    )
                    self.assertEqual(code, 200)
                    self.assertGreater(ear_res["bytes"], 100)

                    # 6b. ESP32 Network screen: push RPi DHCP IP and QuIXI Ixian address & handle evt.net.refresh
                    code, net_res = await gui.handle_api_request(
                        "POST",
                        "/api/esp32/net",
                        body={"ip": "192.168.1.42", "ixi": "1PiZero2WIxianWalletAddressXYZ"},
                    )
                    self.assertEqual(code, 200)
                    self.assertTrue(net_res["ok"])
                    self.assertEqual(esp.net_ip, "192.168.1.42")
                    self.assertEqual(esp.net_ixi, "1PiZero2WIxianWalletAddressXYZ")

                    await hub._on_esp_event({"t": "evt", "src": "net", "act": "refresh"})
                    await asyncio.sleep(0.03)
                    self.assertEqual(esp.net_ixi, "1PiAddrTest")

                    # 7. Live TCP HTTP GET / and RFC 6455 WebSocket /api/ws
                    reader, writer = await asyncio.open_connection("127.0.0.1", bound_port)
                    writer.write(b"GET / HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
                    await writer.drain()
                    raw_html = await asyncio.wait_for(reader.read(), timeout=3.0)
                    writer.close()
                    self.assertIn(b"HTTP/1.1 200 OK", raw_html)
                    self.assertIn(b"piFaceCanvas", raw_html)

                    reader, writer = await asyncio.open_connection("127.0.0.1", bound_port)
                    ws_req = (
                        "GET /api/ws HTTP/1.1\r\n"
                        "Host: 127.0.0.1\r\n"
                        "Upgrade: websocket\r\n"
                        "Connection: Upgrade\r\n"
                        "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
                        "Sec-WebSocket-Version: 13\r\n\r\n"
                    ).encode("latin-1")
                    writer.write(ws_req)
                    await writer.drain()
                    hdr_lines = b""
                    while not hdr_lines.endswith(b"\r\n\r\n"):
                        ch = await asyncio.wait_for(reader.read(1), timeout=2.0)
                        if not ch:
                            break
                        hdr_lines += ch
                    self.assertIn(b"101 Switching Protocols", hdr_lines)
                    writer.close()
                finally:
                    await gui.stop()
                    await hub.esp.stop()

    async def test_esp32_firmware_updater_and_llm_reflash_tool(self) -> None:
        """Verify EspFirmwareUpdater staging, magic/SHA-256 check, serial release/resume flash, and LLM tool integration."""
        import hashlib

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            hub, esp, bus, port = _make_hub_with_mocks(tmp_path)
            fw_dir = tmp_path / "firmware"
            executed_cmds: list[list[str]] = []
            bridge_was_closed_during_flash = False

            async def fake_esptool_runner(cmd: list[str]) -> tuple[int, str]:
                nonlocal bridge_was_closed_during_flash
                executed_cmds.append(cmd)
                # Confirm EspSerialBridge released the serial port before running esptool!
                bridge_was_closed_during_flash = not hub.esp._running and hub.esp._ser is None
                return 0, "Wrote 512640 bytes at 0x00000000 in 5.2 seconds (effective 788.7 kbit/s)...\nHard resetting via RTS pin..."

            hub.firmware.firmware_dir = fw_dir
            hub.firmware._candidate_paths = [fw_dir / "zephyr.bin"]
            hub.firmware._runner = fake_esptool_runner

            with patch("pi_hub.esp_serial_bridge.serial.Serial", return_value=port):
                await hub.esp.start()
                try:
                    # 1. Invalid magic byte (< 0xE9) is rejected on upload
                    bad_b64 = base64.b64encode(b"\x00" * 64).decode("ascii")
                    code, bad_up = await hub.gui.handle_api_request(
                        "POST",
                        "/api/esp32/firmware/upload",
                        body={"firmware_b64": bad_b64},
                    )
                    self.assertEqual(code, 400)
                    self.assertIn("0xE9", bad_up["error"])

                    # 2. Valid ESP32-S3 firmware image (0xE9 magic header) stages cleanly & writes .sha256
                    valid_bin = b"\xE9" + b"\x01\x02\x30" + (b"\xAA\x55" * 1024)
                    expected_sha = hashlib.sha256(valid_bin).hexdigest()
                    valid_b64 = base64.b64encode(valid_bin).decode("ascii")

                    code, up_res = await hub.gui.handle_api_request(
                        "POST",
                        "/api/esp32/firmware/upload",
                        body={"firmware_b64": valid_b64, "sha256": expected_sha, "filename": "zephyr.bin"},
                    )
                    self.assertEqual(code, 200)
                    self.assertTrue(up_res["ok"])
                    self.assertEqual(up_res["firmware"]["sha256"], expected_sha)

                    # 3. SHA-256 mismatch is rejected before flashing
                    code, bad_sha_flash = await hub.gui.handle_api_request(
                        "POST",
                        "/api/esp32/firmware/flash",
                        body={"sha256": "0" * 64},
                    )
                    self.assertEqual(code, 502)
                    self.assertIn("SHA-256 mismatch", bad_sha_flash["error"])
                    self.assertEqual(len(executed_cmds), 0)

                    # 4. LLM tool call `update_companion_settings(firmware_action='check')`
                    chk_res = await hub._run_single_tool(
                        "update_companion_settings",
                        {"firmware_action": "check"},
                    )
                    self.assertTrue(chk_res["ok"])
                    self.assertTrue(chk_res["firmware"]["ready_to_flash"])
                    self.assertEqual(chk_res["firmware"]["firmware"]["sha256"], expected_sha)

                    # 5. LLM tool call `update_companion_settings(firmware_action='flash')`
                    # Releases serial port -> runs esptool -> resumes serial port @ 921600 -> verifies post-flash ping!
                    flash_res = await hub._run_single_tool(
                        "update_companion_settings",
                        {"firmware_action": "flash", "firmware_sha256": expected_sha},
                    )
                    self.assertTrue(flash_res["ok"])
                    self.assertTrue(bridge_was_closed_during_flash)
                    self.assertEqual(len(executed_cmds), 1)
                    self.assertIn("esp32s3", executed_cmds[0])
                    self.assertIn("write_flash", executed_cmds[0])
                    self.assertEqual(flash_res["firmware"]["post_flash_ping"]["ok"], 1)
                    self.assertTrue(hub.esp._running)

                    # 6. LLM tool call `update_companion_settings(firmware_action='reset')`
                    rst_res = await hub._run_single_tool(
                        "update_companion_settings",
                        {"firmware_action": "reset"},
                    )
                    self.assertTrue(rst_res["ok"])
                    self.assertEqual(rst_res["firmware"]["ping"]["ok"], 1)
                finally:
                    await hub.esp.stop()

    async def test_spixi_friends_direct_incoming_voice_call_and_esp32_message_display(self) -> None:
        """Verify adding Spixi friends on the Pi, accepting incoming friend voice calls directly, and displaying friend messages on the ESP32-S3 LCD while preserving Zero-Trust RPC isolation."""
        from common_ixian.spixi_voip import (
            SPIXI_VOIP_APP_ID,
            AppDataMessage,
            SpixiMessageCode,
            pack_app_data_message,
        )

        with tempfile.TemporaryDirectory() as tmp:
            hub, esp, bus, port = _make_hub_with_mocks(Path(tmp))
            with patch("pi_hub.esp_serial_bridge.serial.Serial", return_value=port):
                await hub.esp.start()
                try:
                    # 1. Start with an active background AI Server VoIP session
                    srv_sid = await hub.voip.initiate_call(hub.server_address)
                    await hub.voip.on_accepted_call(
                        hub.server_address,
                        AppDataMessage(session_id=srv_sid, data=b"pcm"),
                    )
                    self.assertTrue(hub.voip.is_in_call)
                    self.assertFalse(hub.is_in_friend_call)
                    self.assertEqual(hub.voip.current_contact, "1SrvAddrTest")

                    # 2. Add a Spixi friend ("Alice" at 1SpixiFriendAlice) via Pi Web GUI
                    code, add_res = await hub.gui.handle_api_request(
                        "POST",
                        "/api/quixi/contacts/add",
                        body={"address": "1SpixiFriendAlice", "nickname": "Alice"},
                    )
                    self.assertEqual(code, 200)
                    self.assertEqual(hub.trusted_contacts.get("1SpixiFriendAlice"), "Alice")
                    self.assertEqual(hub.get_contact_display_name("1SpixiFriendAlice"), "Alice")

                    # 3. Incoming Chat message from an unauthorized stranger is NOT displayed on ESP32
                    esp.sub = "Ready!"
                    stranger_msg_b64 = base64.b64encode(b"Spam from stranger").decode("ascii")
                    await hub._on_quixi_chat_event(
                        "Chat",
                        {"sender": "1StrangerDanger", "data": {"data": stranger_msg_b64}},
                    )
                    await asyncio.sleep(0.03)
                    self.assertEqual(esp.sub, "Ready!")

                    # 4. Incoming Chat message from trusted friend Alice IS displayed on ESP32 LCD ("Alice: Hey from Spixi!")
                    alice_msg_b64 = base64.b64encode(b"Hey from Spixi!").decode("ascii")
                    await hub._on_quixi_chat_event(
                        "Chat",
                        {"sender": "1SpixiFriendAlice", "data": {"data": alice_msg_b64}},
                    )
                    await asyncio.sleep(0.03)
                    self.assertEqual(esp.emo, "HAPPY")
                    self.assertEqual(esp.sub, "Alice: Hey from Spixi!")

                    # 5. Incoming Spixi voice call from unauthorized stranger is REJECTED
                    stranger_sid = b"\x99" * 16
                    accepted_stranger = await hub.voip.on_received_call(
                        "1StrangerDanger",
                        AppDataMessage(session_id=stranger_sid, data=b"pcm|opus", app_id=SPIXI_VOIP_APP_ID),
                    )
                    self.assertFalse(accepted_stranger)
                    self.assertEqual(hub.voip.current_contact, "1SrvAddrTest")

                    # 6. Incoming Spixi voice call from trusted friend Alice preempts idle AI Server session & auto-accepts!
                    alice_sid = b"\x42" * 16
                    accepted_alice = await hub.voip.on_received_call(
                        "1SpixiFriendAlice",
                        AppDataMessage(session_id=alice_sid, data=b"pcm|opus", app_id=SPIXI_VOIP_APP_ID),
                    )
                    await asyncio.sleep(0.03)
                    self.assertTrue(accepted_alice)
                    self.assertTrue(hub.voip.is_in_call)
                    self.assertTrue(hub.is_in_friend_call)
                    self.assertEqual(hub.voip.current_contact, "1SpixiFriendAlice")
                    self.assertEqual(esp.emo, "HAPPY")
                    self.assertEqual(esp.sub, "Call: Alice")

                    # 7. Incoming voice audio from Alice plays on speaker and animates ESP32 mouth
                    loud_pcm = make_pcm16_sine(440.0, 0.06, 16000)
                    await hub.voip.on_data(
                        "1SpixiFriendAlice",
                        AppDataMessage(session_id=alice_sid, data=loud_pcm),
                    )
                    await asyncio.sleep(0.03)
                    self.assertGreater(esp.mouth, 0)
                    self.assertFalse(hub.audio._play_queue.empty())

                    # 8. Speaking on the Pi during a friend call does NOT send end_of_turn to the AI Server
                    sent_app_data: list[dict] = []
                    orig_send_app = hub._send_agent_app_data

                    async def spy_send_app(target: str, payload: dict) -> bool:
                        sent_app_data.append(payload)
                        return await orig_send_app(target, payload)

                    hub._send_agent_app_data = spy_send_app  # type: ignore[assignment]
                    await hub._on_speech_capture_state(True)
                    await asyncio.sleep(0.02)
                    self.assertEqual(esp.emo, "LISTENING")
                    self.assertEqual(esp.sub, "Call: Alice")
                    await hub._on_speech_capture_state(False)
                    await asyncio.sleep(0.02)
                    self.assertEqual(esp.emo, "HAPPY")
                    self.assertEqual(esp.sub, "Call: Alice")
                    self.assertEqual(sent_app_data, [])

                    # 9. Zero-Trust security invariant: trusted friend Alice STILL cannot execute hardware tool_call RPCs!
                    esp.gpio_vals[16] = 0
                    malicious_tool = base64.b64encode(
                        json.dumps(
                            {
                                "kind": "tool_call",
                                "call_id": "hack1",
                                "tool": "gpio_configure_and_write",
                                "args": {"pin": 16, "mode": "out", "state": 1},
                            }
                        ).encode("utf-8")
                    ).decode("ascii")
                    await hub._on_app_protocol_data(
                        "AppProtocolData",
                        {"sender": "1SpixiFriendAlice", "data": {"data": malicious_tool}},
                    )
                    await asyncio.sleep(0.02)
                    self.assertEqual(esp.gpio_vals.get(16, 0), 0)

                    # 10. Hangup friend call resets ESP32 mouth & subtitle
                    await hub.voip.on_hangup_call(AppDataMessage(session_id=alice_sid, data=b""))
                    await asyncio.sleep(0.03)
                    self.assertFalse(hub.voip.is_in_call)
                    self.assertEqual(esp.mouth, 0)
                    self.assertEqual(esp.sub, "Call ended.")

                    # 11. User says "Call Alice" -> LLM invokes update_companion_settings(spixi_action="call", contact_name="alice")
                    # Re-establish background AI Server call first to verify force preemption
                    srv_sid2 = await hub.voip.initiate_call(hub.server_address)
                    await hub.voip.on_accepted_call(
                        hub.server_address,
                        AppDataMessage(session_id=srv_sid2, data=b"pcm"),
                    )
                    self.assertEqual(hub.voip.current_contact, "1SrvAddrTest")

                    list_res = await hub._run_single_tool(
                        "update_companion_settings",
                        {"spixi_action": "list_contacts"},
                    )
                    self.assertTrue(list_res["ok"])
                    self.assertEqual(list_res["spixi"]["trusted_contacts"]["1SpixiFriendAlice"], "Alice")

                    call_res = await hub._run_single_tool(
                        "update_companion_settings",
                        {"spixi_action": "call", "contact_name": "alice"},
                    )
                    await asyncio.sleep(0.03)
                    self.assertTrue(call_res["ok"])
                    self.assertEqual(call_res["spixi"]["action"], "call")
                    self.assertEqual(call_res["spixi"]["contact_name"], "Alice")
                    self.assertEqual(call_res["spixi"]["address"], "1SpixiFriendAlice")
                    self.assertEqual(hub.voip.current_contact, "1SpixiFriendAlice")
                    self.assertEqual(esp.emo, "HAPPY")
                    self.assertEqual(esp.sub, "Calling: Alice...")

                    # When Alice accepts the outgoing call on Spixi, ESP32 LCD updates to "Call: Alice"
                    out_sid = bytes.fromhex(call_res["spixi"]["session_id"])
                    await hub.voip.on_accepted_call(
                        "1SpixiFriendAlice",
                        AppDataMessage(session_id=out_sid, data=b"pcm"),
                    )
                    await asyncio.sleep(0.03)
                    self.assertTrue(hub.is_in_friend_call)
                    self.assertEqual(esp.sub, "Call: Alice")

                    # Also test LLM sending a dictated Spixi chat message by nickname and hanging up via tool
                    msg_res = await hub._run_single_tool(
                        "update_companion_settings",
                        {"spixi_action": "message", "contact_name": "Alice", "spixi_message": "On my way!"},
                    )
                    await asyncio.sleep(0.03)
                    self.assertTrue(msg_res["ok"])
                    self.assertEqual(msg_res["spixi"]["address"], "1SpixiFriendAlice")
                    self.assertEqual(esp.emo, "HAPPY")
                    self.assertEqual(esp.sub, "To Alice: On my way!")
                    self.assertTrue(
                        any(
                            e.get("topic") == "Chat/out" and e.get("summary") == "To Alice: On my way!"
                            for e in hub.gui.quixi_event_log
                        )
                    )

                    hup_res = await hub._run_single_tool(
                        "update_companion_settings",
                        {"spixi_action": "hangup"},
                    )
                    await asyncio.sleep(0.03)
                    self.assertTrue(hup_res["ok"])
                    self.assertFalse(hub.voip.is_in_call)
                    self.assertEqual(esp.sub, "Call ended.")

                    # Unknown contact nickname returns helpful available_contacts list
                    unk_res = await hub._run_single_tool(
                        "update_companion_settings",
                        {"spixi_action": "call", "contact_name": "NonExistentBob"},
                    )
                    self.assertFalse(unk_res["ok"])
                    self.assertIn("not found", unk_res["error"])
                    self.assertTrue(any("Alice" in c for c in unk_res["available_contacts"]))
                finally:
                    await hub.quixi.stop()
                    await hub.esp.stop()


class TestFirstBootAPSetupPortal(unittest.IsolatedAsyncioTestCase):
    """Tests for FirstBootAPPortal: AP hotspot, Wi-Fi scan parsing, required user+wifi, and skippable optional fields."""

    async def test_first_boot_ap_portal_required_and_skippable_fields(self) -> None:
        from pi_hub.first_boot_setup import FirstBootAPPortal, parse_nmcli_wifi_scan

        # 1. nmcli terse output parsing (escaped colons + dedupe strongest RSSI)
        raw_scan = (
            "HomeNet:5G:40:WPA2\n"
            "My\\:Cafe\\:WiFi:75:WPA2\n"
            "HomeWiFi:55:WPA2\n"
            "HomeWiFi:92:WPA2\n"
            ":30:WPA2\n"
        )
        parsed = parse_nmcli_wifi_scan(raw_scan)
        self.assertEqual(parsed[0]["ssid"], "HomeWiFi")
        self.assertEqual(parsed[0]["signal"], 92)
        self.assertEqual(parsed[1]["ssid"], "My:Cafe:WiFi")

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            sentinel = tmp_path / "firstboot.done"
            ixian_cfg = tmp_path / "ixian.cfg"
            ixian_cfg.write_text("apiPort = 8001\nwalletPassword = REPLACE_ME\n", encoding="utf-8")
            hub_cfg = tmp_path / "config.yaml"
            hub_cfg.write_text("quixi:\n  server_ixian_address: 1ReplaceMe\n", encoding="utf-8")

            executed: list[tuple[list[str], str | None]] = []

            async def fake_runner(cmd: list[str], stdin_txt: str | None) -> tuple[int, str, str]:
                executed.append((cmd, stdin_txt))
                if cmd[:2] == ["id", "-u"]:
                    return 1, "", "no such user"
                if cmd[:4] == ["nmcli", "-t", "-f", "SSID,SIGNAL,SECURITY"]:
                    return 0, raw_scan, ""
                return 0, "", ""

            portal = FirstBootAPPortal(
                host="127.0.0.1",
                port=0,
                sentinel_path=sentinel,
                ixian_cfg_path=ixian_cfg,
                hub_cfg_path=hub_cfg,
                cmd_runner=fake_runner,
                dry_run=False,
            )
            self.assertTrue(portal.is_first_boot_needed())

            # 2. Start AP mode & HTTP server
            ap_ok, _ = await portal.start_ap_mode()
            self.assertTrue(ap_ok)
            self.assertTrue(portal.ap_active)
            bound_port = await portal.start()
            try:
                # 3. Validation rejects missing username, short password, or missing wifi_ssid
                code, bad_u = await portal.handle_api_request(
                    "POST",
                    "/api/setup/apply",
                    body={"username": "", "password": "secret", "wifi_ssid": "HomeWiFi"},
                )
                self.assertEqual(code, 400)
                self.assertIn("username", bad_u["error"])

                code, bad_w = await portal.handle_api_request(
                    "POST",
                    "/api/setup/apply",
                    body={"username": "alex", "password": "secret123", "wifi_ssid": ""},
                )
                self.assertEqual(code, 400)
                self.assertIn("wifi_ssid", bad_w["error"])

                # 4. Wi-Fi scan endpoint
                code, scan_res = await portal.handle_api_request("GET", "/api/setup/wifi")
                self.assertEqual(code, 200)
                self.assertEqual(scan_res["networks"][0]["ssid"], "HomeWiFi")

                # 5. Apply with ONLY required user + wifi (all optional fields skipped!)
                code, ok_res = await portal.handle_api_request(
                    "POST",
                    "/api/setup/apply",
                    body={
                        "username": "alex",
                        "password": "MyPiPassword!",
                        "wifi_ssid": "HomeWiFi",
                        "wifi_password": "WiFiPass123",
                        "wifi_country": "DE",
                        "enable_ssh": False,
                    },
                )
                self.assertEqual(code, 200)
                self.assertTrue(ok_res["ok"])
                self.assertEqual(ok_res["username"], "alex")
                self.assertEqual(ok_res["wifi_ssid"], "HomeWiFi")
                self.assertIn("hostname", ok_res["skipped_optional"])
                self.assertIn("quixi_wallet_password", ok_res["skipped_optional"])
                self.assertIn("server_ixian_address", ok_res["skipped_optional"])
                self.assertFalse(portal.ap_active)
                self.assertFalse(portal.is_first_boot_needed())
                self.assertTrue(portal.setup_complete_event.is_set())

                # Sentinel must NOT store plaintext passwords
                sentinel_txt = sentinel.read_text(encoding="utf-8")
                self.assertNotIn("MyPiPassword!", sentinel_txt)
                self.assertNotIn("WiFiPass123", sentinel_txt)

                # Skipped optional configs remain untouched
                self.assertIn("REPLACE_ME", ixian_cfg.read_text(encoding="utf-8"))
                self.assertIn("1ReplaceMe", hub_cfg.read_text(encoding="utf-8"))

                # Verify useradd, chpasswd, and nmcli wifi connect ran
                self.assertTrue(any(c[0][:1] == ["useradd"] and "alex" in c[0] for c in executed))
                self.assertTrue(any(c[0] == ["chpasswd"] and c[1] == "alex:MyPiPassword!\n" for c in executed))
                self.assertTrue(
                    any(c[0][:4] == ["nmcli", "device", "wifi", "connect"] and "HomeWiFi" in c[0] for c in executed)
                )

                # 6. Captive portal GET / serves setup HTML
                reader, writer = await asyncio.open_connection("127.0.0.1", bound_port)
                writer.write(b"GET /generate_204 HTTP/1.1\r\nHost: 192.168.4.1\r\n\r\n")
                await writer.drain()
                raw_html = await asyncio.wait_for(reader.read(), timeout=3.0)
                writer.close()
                self.assertIn(b"200 OK", raw_html)
                self.assertIn(b"First-Boot Setup", raw_html)
            finally:
                await portal.stop()

    async def test_pi_web_gui_network_and_all_raspi_config_options(self) -> None:
        """Verify PiWebGUI network (Wi-Fi scan/connect, DHCP/static IPv4) and all raspi-config options."""
        from pi_hub.pi_system_config import PiSystemConfigManager

        with tempfile.TemporaryDirectory() as tmp:
            hub, esp, bus, port = _make_hub_with_mocks(Path(tmp))
            gui = hub.gui
            cmds: list[list[str]] = []

            async def fake_sys_runner(cmd: list[str], stdin_txt: str | None) -> tuple[int, str, str]:
                cmds.append(cmd)
                if cmd[:4] == ["nmcli", "-t", "-f", "GENERAL.CONNECTION,IP4.ADDRESS,IP4.GATEWAY,IP4.DNS"]:
                    return (
                        0,
                        "GENERAL.CONNECTION:HomeWiFi\nIP4.ADDRESS[1]:192.168.1.42/24\nIP4.GATEWAY:192.168.1.1\nIP4.DNS[1]:1.1.1.1\n",
                        "",
                    )
                if cmd[:4] == ["nmcli", "-t", "-f", "NAME,TYPE,DEVICE"]:
                    return 0, "HomeWiFi:802-11-wireless:wlan0\n", ""
                if cmd[:4] == ["nmcli", "-t", "-f", "SSID,SIGNAL,SECURITY"]:
                    return 0, "HomeWiFi:90:WPA2\nGuest:45:OPEN\n", ""
                if cmd == ["hostname"]:
                    return 0, "tamagci\n", ""
                if cmd == ["raspi-config", "nonint", "get_wifi_country"]:
                    return 0, "DE\n", ""
                if cmd[:3] == ["raspi-config", "nonint", "get_ssh"]:
                    return 0, "0\n", ""
                if len(cmd) >= 3 and cmd[:2] == ["raspi-config", "nonint"] and cmd[2].startswith("get_"):
                    return 0, "1\n", ""
                return 0, "", ""

            gui.sys_cfg = PiSystemConfigManager(cmd_runner=fake_sys_runner, dry_run=False, use_sudo=False)

            # 1. GET /api/system/network?scan=1
            code, net_res = await gui.handle_api_request("GET", "/api/system/network?scan=1")
            self.assertEqual(code, 200)
            self.assertEqual(net_res["active_connection"], "HomeWiFi")
            self.assertEqual(net_res["ipv4_address"], "192.168.1.42/24")
            self.assertEqual(net_res["networks"][0]["ssid"], "HomeWiFi")

            # 2. Configure Static IPv4 and then revert to DHCP
            code, static_res = await gui.handle_api_request(
                "POST",
                "/api/system/network",
                body={
                    "action": "configure_ipv4",
                    "mode": "manual",
                    "ip_cidr": "192.168.1.88/24",
                    "gateway": "192.168.1.1",
                    "dns": "1.1.1.1",
                },
            )
            self.assertEqual(code, 200)
            self.assertTrue(static_res["ok"])
            self.assertTrue(any("192.168.1.88/24" in c and "manual" in c for c in cmds))

            code, dhcp_res = await gui.handle_api_request(
                "POST",
                "/api/system/network",
                body={"action": "configure_ipv4", "mode": "auto"},
            )
            self.assertEqual(code, 200)
            self.assertTrue(any("auto" in c and "modify" in c for c in cmds))

            # 3. GET /api/system/raspi-config
            code, rc_get = await gui.handle_api_request("GET", "/api/system/raspi-config")
            self.assertEqual(code, 200)
            self.assertEqual(rc_get["hostname"], "tamagci")
            self.assertEqual(rc_get["wifi_country"], "DE")
            self.assertTrue(rc_get["interfaces"]["ssh"])
            self.assertFalse(rc_get["interfaces"]["i2c"])

            # 4. POST /api/system/raspi-config (all options)
            code, rc_post = await gui.handle_api_request(
                "POST",
                "/api/system/raspi-config",
                body={
                    "hostname": "alf-pi",
                    "wifi_country": "US",
                    "timezone": "Europe/Berlin",
                    "locale": "en_US.UTF-8",
                    "keyboard": "de",
                    "boot_behaviour": "B2",
                    "boot_wait_network": True,
                    "gpu_mem": 16,
                    "expand_rootfs": True,
                    "ssh": True,
                    "vnc": False,
                    "spi": True,
                    "i2c": True,
                    "serial_hw": True,
                    "serial_cons": False,
                    "onewire": True,
                    "rgpio": False,
                },
            )
            self.assertEqual(code, 200)
            self.assertTrue(rc_post["ok"])
            self.assertEqual(rc_post["applied"]["hostname"], "alf-pi")
            self.assertEqual(rc_post["applied"]["gpu_mem"], 16)
            self.assertTrue(rc_post["applied"]["spi"])
            self.assertTrue(rc_post["applied"]["i2c"])
            self.assertTrue(any(c == ["raspi-config", "nonint", "do_spi", "0"] for c in cmds))
            self.assertTrue(any(c == ["raspi-config", "nonint", "do_vnc", "1"] for c in cmds))
            self.assertTrue(any(c == ["raspi-config", "nonint", "do_memory_split", "16"] for c in cmds))
            self.assertTrue(any(c == ["raspi-config", "nonint", "do_expand_rootfs"] for c in cmds))

            # 5. Power action
            code, pwr_res = await gui.handle_api_request(
                "POST", "/api/system/power", body={"action": "reboot"}
            )
            self.assertEqual(code, 200)
            self.assertTrue(any(c == ["systemctl", "reboot"] for c in cmds))


if __name__ == "__main__":
    unittest.main()
