"""Unit and loopback simulation tests for Spixi VoIPManager.cs protocol parity."""

from __future__ import annotations

import asyncio
import base64
import struct
import sys
import unittest
import uuid
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tests._stubs import install_dependency_stubs

install_dependency_stubs()

from common_ixian.quixi_client import QuIXIClient, _extract_b64_bytes
from common_ixian.spixi_voip import (
    SPIXI_VOIP_APP_ID,
    AppDataMessage,
    SpixiMessageCode,
    SpixiVoIPSessionManager,
    pack_app_data_message,
    unpack_app_data_message,
)


class TestAppDataBinaryCodec(unittest.TestCase):
    """Tier 1 & Tier 2 binary codec parity tests against C# AppDataMessage.cs."""

    def test_app_data_binary_roundtrip(self) -> None:
        sid = uuid.uuid4().bytes
        codecs = b"pcm|opus"
        packed = pack_app_data_message(sid, codecs, app_id=SPIXI_VOIP_APP_ID)
        unpacked = unpack_app_data_message(packed)

        self.assertEqual(unpacked.session_id, sid)
        self.assertEqual(unpacked.data, codecs)
        self.assertEqual(unpacked.app_id, "spixi.voip")

        # Without appId (standard VoIP audio frame)
        audio_frame = b"\x01\x02\x03\x04" * 80
        packed_frame = pack_app_data_message(sid, audio_frame)
        unpacked_frame = unpack_app_data_message(packed_frame)
        self.assertEqual(unpacked_frame.session_id, sid)
        self.assertEqual(unpacked_frame.data, audio_frame)
        self.assertIsNone(unpacked_frame.app_id)

    def test_spixi_message_code_enum_values(self) -> None:
        self.assertEqual(int(SpixiMessageCode.CHAT), 0)
        self.assertEqual(int(SpixiMessageCode.APP_DATA), 21)
        self.assertEqual(int(SpixiMessageCode.APP_REQUEST), 22)
        self.assertEqual(int(SpixiMessageCode.APP_REQUEST_ACCEPT), 28)
        self.assertEqual(int(SpixiMessageCode.APP_REQUEST_REJECT), 29)
        self.assertEqual(int(SpixiMessageCode.APP_REQUEST_ERROR), 30)
        self.assertEqual(int(SpixiMessageCode.APP_END_SESSION), 31)
        self.assertEqual(int(SpixiMessageCode.APP_PROTOCOL_DATA), 45)
        self.assertEqual(int(SpixiMessageCode.CHAT_STREAM), 53)

    def test_boundary_lengths_and_7bit_varint(self) -> None:
        # 0-byte session_id and empty payload
        packed_empty = pack_app_data_message(b"", b"", app_id="")
        unpacked_empty = unpack_app_data_message(packed_empty)
        self.assertEqual(unpacked_empty.session_id, b"")
        self.assertEqual(unpacked_empty.data, b"")
        self.assertEqual(unpacked_empty.app_id, "")

        # Max 255-byte session_id
        sid_255 = b"S" * 255
        packed_255 = pack_app_data_message(sid_255, b"ok")
        unpacked_255 = unpack_app_data_message(packed_255)
        self.assertEqual(unpacked_255.session_id, sid_255)
        self.assertEqual(unpacked_255.data, b"ok")

        # >255-byte session_id rejected
        with self.assertRaises(ValueError):
            pack_app_data_message(b"X" * 256, b"")

        # Multi-byte 7-bit encoded int app_id (128B -> 2B varint, 16384B -> 3B varint) and UTF-8
        for app_id in ("a" * 128, "b" * 16384, "spixi.voip.测试"):
            raw = pack_app_data_message(b"1234", b"payload", app_id=app_id)
            dec = unpack_app_data_message(raw)
            self.assertEqual(dec.app_id, app_id)

    def test_malformed_and_negative_length_rejected(self) -> None:
        # Empty input
        with self.assertRaises(ValueError):
            unpack_app_data_message(b"")

        # Truncated session_id
        with self.assertRaises(ValueError):
            unpack_app_data_message(b"\x10abc")

        # Truncated data_len
        with self.assertRaises(ValueError):
            unpack_app_data_message(b"\x01a\x00\x00")

        # Truncated data payload
        with self.assertRaises(ValueError):
            unpack_app_data_message(b"\x01a" + struct.pack("<i", 10) + b"123")

        # Negative data_len MUST raise ValueError (M2 contract)
        with self.assertRaises(ValueError):
            unpack_app_data_message(b"\x01a" + struct.pack("<i", -1))

        # Overlong 7-bit varint (shift >= 35)
        with self.assertRaises(ValueError):
            unpack_app_data_message(b"\x01a" + struct.pack("<i", 0) + b"\x80\x80\x80\x80\x80\x01")

        # Truncated appId
        with self.assertRaises(ValueError):
            unpack_app_data_message(b"\x01a" + struct.pack("<i", 0) + b"\x05abc")


class TestSpixiVoIPLoopback(unittest.IsolatedAsyncioTestCase):
    """Tier 1–4 SpixiVoIPSessionManager handshake, streaming, negotiation & error recovery tests."""

    async def test_voip_call_handshake_and_audio_stream(self) -> None:
        pi_addr = "1PiAddressExample1111111111111111111"
        srv_addr = "1SrvAddressExample222222222222222222"

        pi_rx_audio: list[bytes] = []
        srv_rx_audio: list[bytes] = []
        srv_ended = asyncio.Event()

        async def on_pi_audio(sid: bytes, data: bytes, codec: str) -> None:
            pi_rx_audio.append(data)

        async def on_srv_audio(sid: bytes, data: bytes, codec: str) -> None:
            srv_rx_audio.append(data)

        async def on_srv_ended(contact: str, sid: bytes, codec: str) -> None:
            srv_ended.set()

        async def route_from_pi(addr: str, msg_type: int, data_bytes: bytes, ch: int) -> None:
            msg = unpack_app_data_message(data_bytes)
            if msg_type == SpixiMessageCode.APP_REQUEST:
                await srv_voip.on_received_call(pi_addr, msg, auto_accept=True)
            elif msg_type == SpixiMessageCode.APP_DATA:
                await srv_voip.on_data(pi_addr, msg)
            elif msg_type == SpixiMessageCode.APP_END_SESSION:
                await srv_voip.on_hangup_call(msg)

        async def route_from_srv(addr: str, msg_type: int, data_bytes: bytes, ch: int) -> None:
            msg = unpack_app_data_message(data_bytes)
            if msg_type == SpixiMessageCode.APP_REQUEST_ACCEPT:
                await pi_voip.on_accepted_call(srv_addr, msg)
            elif msg_type == SpixiMessageCode.APP_DATA:
                await pi_voip.on_data(srv_addr, msg)
            elif msg_type == SpixiMessageCode.APP_END_SESSION:
                await pi_voip.on_hangup_call(msg)

        pi_voip = SpixiVoIPSessionManager(route_from_pi, ("pcm", "opus"), on_audio_data=on_pi_audio)
        srv_voip = SpixiVoIPSessionManager(
            route_from_srv,
            ("pcm", "opus"),
            on_audio_data=on_srv_audio,
            on_session_ended=on_srv_ended,
        )

        # 1. Pi initiates call (VoIPManager.initiateCall)
        sid = await pi_voip.initiate_call(srv_addr)
        self.assertTrue(pi_voip.is_in_call)
        self.assertTrue(srv_voip.is_in_call)
        self.assertEqual(pi_voip.current_codec, "pcm")
        self.assertEqual(srv_voip.current_session_id, sid)

        # 2. Bi-directional audio frames (VoIPManager.onData)
        ok_up = await pi_voip.send_audio_frame(b"MIC_FRAME_1")
        ok_dn = await srv_voip.send_audio_frame(b"TTS_FRAME_1")
        self.assertTrue(ok_up and ok_dn)
        self.assertEqual(srv_rx_audio, [b"MIC_FRAME_1"])
        self.assertEqual(pi_rx_audio, [b"TTS_FRAME_1"])

        # 3. MQTT payload extraction helper check
        b64_payload = base64.b64encode(pack_app_data_message(sid, b"TTS_FRAME_2")).decode("ascii")
        extracted = _extract_b64_bytes({"sender": srv_addr, "data": {"type": 21, "data": b64_payload}})
        self.assertEqual(unpack_app_data_message(extracted).data, b"TTS_FRAME_2")

        # 4. Hangup (VoIPManager.hangupCall)
        await pi_voip.hangup_call()
        await asyncio.wait_for(srv_ended.wait(), timeout=1.0)
        self.assertFalse(pi_voip.is_in_call)
        self.assertFalse(srv_voip.is_in_call)

    async def test_manual_accept_and_reject_flow(self) -> None:
        pi_sent: list[tuple[int, bytes]] = []
        srv_sent: list[tuple[int, bytes]] = []

        async def send_pi(addr: str, mtype: int, data: bytes, ch: int) -> None:
            pi_sent.append((mtype, data))

        async def send_srv(addr: str, mtype: int, data: bytes, ch: int) -> None:
            srv_sent.append((mtype, data))

        pi_voip = SpixiVoIPSessionManager(send_pi, ("pcm", "opus"))
        srv_voip = SpixiVoIPSessionManager(send_srv, ("pcm", "opus"))

        sid = await pi_voip.initiate_call("1Srv")
        req_msg = unpack_app_data_message(pi_sent[-1][1])
        ok = await srv_voip.on_received_call("1Pi", req_msg, auto_accept=False)
        self.assertTrue(ok)
        self.assertFalse(srv_voip.is_in_call)

        accepted = await srv_voip.accept_call(sid)
        self.assertTrue(accepted)
        self.assertTrue(srv_voip.is_in_call)
        self.assertEqual(srv_sent[-1][0], int(SpixiMessageCode.APP_REQUEST_ACCEPT))

        # Verify APP_REQUEST_REJECT clears initiator pending state
        sid2 = await pi_voip.initiate_call("1Srv")
        self.assertEqual(sid2, sid)
        await pi_voip.on_rejected_call(AppDataMessage(session_id=sid, data=b""))
        self.assertIsNone(pi_voip.current_session_id)
        self.assertFalse(pi_voip.is_in_call)

    async def test_codec_negotiation_and_error_recovery(self) -> None:
        sent_srv: list[tuple[int, bytes]] = []
        sent_pi: list[tuple[int, bytes]] = []

        async def send_srv(addr: str, mtype: int, data: bytes, ch: int) -> None:
            sent_srv.append((mtype, data))

        async def send_pi(addr: str, mtype: int, data: bytes, ch: int) -> None:
            sent_pi.append((mtype, data))

        srv_voip = SpixiVoIPSessionManager(send_srv, ("pcm", "opus"))

        # Unsupported codec -> APP_REQUEST_REJECT
        bad_req = AppDataMessage(session_id=b"bad1", data=b"speex|g729", app_id=SPIXI_VOIP_APP_ID)
        ok_bad = await srv_voip.on_received_call("1Pi", bad_req, auto_accept=True)
        self.assertFalse(ok_bad)
        self.assertEqual(sent_srv[-1][0], int(SpixiMessageCode.APP_REQUEST_REJECT))

        # Peer offers opus|pcm -> selects opus
        good_req = AppDataMessage(session_id=b"good1", data=b"opus|pcm", app_id=SPIXI_VOIP_APP_ID)
        ok_good = await srv_voip.on_received_call("1Pi", good_req, auto_accept=True)
        self.assertTrue(ok_good)
        self.assertEqual(srv_voip.current_codec, "opus")

        # APP_REQUEST_ERROR (type 30) via QuIXIClient clears pending initiator call
        pi_voip = SpixiVoIPSessionManager(send_pi, ("pcm", "opus"))
        quixi = QuIXIClient()
        quixi.attach_voip_manager(pi_voip)
        sid = await pi_voip.initiate_call("1Srv")
        self.assertIsNotNone(pi_voip.current_session_id)

        err_b64 = base64.b64encode(pack_app_data_message(sid, b"")).decode("ascii")
        await quixi._dispatch_mqtt_event(
            "AppRequestError/1Srv",
            {"sender": "1Srv", "data": {"type": int(SpixiMessageCode.APP_REQUEST_ERROR), "data": err_b64}},
        )
        self.assertIsNone(pi_voip.current_session_id)
        self.assertFalse(pi_voip.is_in_call)

        # Subsequent initiate_call gets a fresh session_id
        sid_retry = await pi_voip.initiate_call("1Srv")
        self.assertNotEqual(sid_retry, sid)

    async def test_multiframe_bidirectional_stream_and_redial(self) -> None:
        pi_addr = "1PiNode"
        srv_addr = "1SrvNode"
        pi_rx: list[bytes] = []
        srv_rx: list[bytes] = []

        async def on_pi_audio(sid: bytes, data: bytes, codec: str) -> None:
            pi_rx.append(data)

        async def on_srv_audio(sid: bytes, data: bytes, codec: str) -> None:
            srv_rx.append(data)

        async def route_from_pi(addr: str, msg_type: int, data_bytes: bytes, ch: int) -> None:
            msg = unpack_app_data_message(data_bytes)
            if msg_type == SpixiMessageCode.APP_REQUEST:
                await srv_voip.on_received_call(pi_addr, msg, auto_accept=True)
            elif msg_type == SpixiMessageCode.APP_DATA:
                await srv_voip.on_data(pi_addr, msg)
            elif msg_type == SpixiMessageCode.APP_END_SESSION:
                await srv_voip.on_hangup_call(msg)

        async def route_from_srv(addr: str, msg_type: int, data_bytes: bytes, ch: int) -> None:
            msg = unpack_app_data_message(data_bytes)
            if msg_type == SpixiMessageCode.APP_REQUEST_ACCEPT:
                await pi_voip.on_accepted_call(srv_addr, msg)
            elif msg_type == SpixiMessageCode.APP_DATA:
                await pi_voip.on_data(srv_addr, msg)
            elif msg_type == SpixiMessageCode.APP_END_SESSION:
                await pi_voip.on_hangup_call(msg)

        pi_voip = SpixiVoIPSessionManager(route_from_pi, ("pcm", "opus"), on_audio_data=on_pi_audio)
        srv_voip = SpixiVoIPSessionManager(route_from_srv, ("pcm", "opus"), on_audio_data=on_srv_audio)

        sid1 = await pi_voip.initiate_call(srv_addr)
        for i in range(50):
            up = f"UP_{i:02d}".encode()
            dn = f"DN_{i:02d}".encode()
            self.assertTrue(await pi_voip.send_audio_frame(up))
            self.assertTrue(await srv_voip.send_audio_frame(dn))
            b64_up = base64.b64encode(pack_app_data_message(sid1, up)).decode("ascii")
            self.assertEqual(
                unpack_app_data_message(_extract_b64_bytes({"data": {"data": b64_up}})).data,
                up,
            )
            self.assertEqual(
                unpack_app_data_message(_extract_b64_bytes({"data": {"data": {"data": b64_up}}})).data,
                up,
            )

        self.assertEqual(len(srv_rx), 50)
        self.assertEqual(len(pi_rx), 50)

        await pi_voip.hangup_call()
        self.assertFalse(pi_voip.is_in_call)
        self.assertFalse(srv_voip.is_in_call)

        # Sequential redial on the same manager instances
        sid2 = await pi_voip.initiate_call(srv_addr)
        self.assertNotEqual(sid2, sid1)
        self.assertTrue(pi_voip.is_in_call)
        self.assertTrue(srv_voip.is_in_call)
        await srv_voip.hangup_call()
        self.assertFalse(pi_voip.is_in_call)
        self.assertFalse(srv_voip.is_in_call)


if __name__ == "__main__":
    unittest.main()
