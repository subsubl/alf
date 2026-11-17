"""Unit and loopback simulation tests for Spixi VoIPManager.cs protocol parity."""

import asyncio
import base64
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common_ixian.spixi_voip import (
    SpixiMessageCode,
    SpixiVoIPSessionManager,
    pack_app_data_message,
    unpack_app_data_message,
)
from common_ixian.quixi_client import _extract_b64_bytes


def test_app_data_binary_roundtrip() -> None:
    sid = uuid.uuid4().bytes
    codecs = b"pcm|opus"
    packed = pack_app_data_message(sid, codecs, app_id="spixi.voip")
    unpacked = unpack_app_data_message(packed)

    assert unpacked.session_id == sid
    assert unpacked.data == codecs
    assert unpacked.app_id == "spixi.voip"

    # Without appId (standard VoIP audio frame)
    audio_frame = b"\x01\x02\x03\x04" * 80
    packed_frame = pack_app_data_message(sid, audio_frame)
    unpacked_frame = unpack_app_data_message(packed_frame)
    assert unpacked_frame.session_id == sid
    assert unpacked_frame.data == audio_frame
    assert unpacked_frame.app_id is None


async def test_voip_call_handshake_and_audio_stream() -> None:
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

    # Simulated QuIXI bus routing SpixiMessage packets between Pi and Server
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
    assert pi_voip.is_in_call
    assert srv_voip.is_in_call
    assert pi_voip.current_codec == "pcm"
    assert srv_voip.current_session_id == sid

    # 2. Bi-directional audio frames (VoIPManager.onData)
    ok_up = await pi_voip.send_audio_frame(b"MIC_FRAME_1")
    ok_dn = await srv_voip.send_audio_frame(b"TTS_FRAME_1")
    assert ok_up and ok_dn
    assert srv_rx_audio == [b"MIC_FRAME_1"]
    assert pi_rx_audio == [b"TTS_FRAME_1"]

    # 3. MQTT payload extraction helper check
    b64_payload = base64.b64encode(pack_app_data_message(sid, b"TTS_FRAME_2")).decode("ascii")
    extracted = _extract_b64_bytes({"sender": srv_addr, "data": {"type": 21, "data": b64_payload}})
    assert unpack_app_data_message(extracted).data == b"TTS_FRAME_2"

    # 4. Hangup (VoIPManager.hangupCall)
    await pi_voip.hangup_call()
    await asyncio.wait_for(srv_ended.wait(), timeout=1.0)
    assert not pi_voip.is_in_call
    assert not srv_voip.is_in_call


if __name__ == "__main__":
    test_app_data_binary_roundtrip()
    asyncio.run(test_voip_call_handshake_and_audio_stream())
    print("ALL SPIXI VOIP PROTOCOL TESTS PASSED!")
