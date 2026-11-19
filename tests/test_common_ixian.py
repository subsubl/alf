"""Offline 4-tier unit, boundary, integration, and scenario tests for common_ixian."""

from __future__ import annotations

import asyncio
import base64
import binascii
import importlib
import json
import struct
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tests._stubs import (
    FakeHTTPResponse,
    FakeHTTPSession,
    FakeMQTTClient,
    FakeQuIXIBus,
    install_dependency_stubs,
)

install_dependency_stubs()

import common_ixian
from common_ixian.quixi_client import QuIXIClient, _extract_b64_bytes
from common_ixian.spixi_voip import (
    SPIXI_VOIP_APP_ID,
    AppDataMessage,
    SpixiMessageCode,
    SpixiVoIPSessionManager,
    pack_app_data_message,
    unpack_app_data_message,
)


class TestCommonIxianExports(unittest.TestCase):
    """Tier 1: Package exports and standalone spixi_voip importability."""

    def test_public_exports_present(self) -> None:
        expected = {
            "SpixiMessageCode",
            "AppDataMessage",
            "SpixiVoIPSessionManager",
            "pack_app_data_message",
            "unpack_app_data_message",
            "QuIXIClient",
        }
        self.assertTrue(expected.issubset(set(common_ixian.__all__)))
        for name in expected:
            self.assertTrue(hasattr(common_ixian, name))

    def test_standalone_spixi_voip_import_without_aiohttp_or_paho(self) -> None:
        # M2 contract (PROJECT.md:36,100): common_ixian/__init__.py must use lazy/guarded
        # exports so common_ixian.spixi_voip imports standalone without aiohttp/paho.
        saved_modules = {}
        for mod_name in list(sys.modules):
            if mod_name == "common_ixian" or mod_name.startswith("common_ixian.") or mod_name in {"aiohttp", "paho", "paho.mqtt", "paho.mqtt.client"}:
                saved_modules[mod_name] = sys.modules.pop(mod_name)
        try:
            sys.modules["aiohttp"] = None  # type: ignore[assignment]
            sys.modules["paho"] = None  # type: ignore[assignment]
            sys.modules["paho.mqtt"] = None  # type: ignore[assignment]
            sys.modules["paho.mqtt.client"] = None  # type: ignore[assignment]
            mod = importlib.import_module("common_ixian.spixi_voip")
            self.assertTrue(hasattr(mod, "pack_app_data_message"))
            self.assertTrue(hasattr(mod, "SpixiVoIPSessionManager"))
        finally:
            for mod_name in ("common_ixian", "common_ixian.spixi_voip", "common_ixian.quixi_client", "aiohttp", "paho", "paho.mqtt", "paho.mqtt.client"):
                sys.modules.pop(mod_name, None)
            sys.modules.update(saved_modules)


class TestAppDataMessageCodec(unittest.TestCase):
    """Tier 1 & Tier 2 binary format and edge-case tests for AppDataMessage."""

    def test_pack_exact_csharp_binary_layout(self) -> None:
        sid = b"\xAA\xBB\xCC\xDD"
        payload = b"hello"
        raw = pack_app_data_message(sid, payload, app_id="spixi.voip")
        # [1B len=4][4B sid][4B LE len=5][5B payload][7bit len=0x0a][10B utf8]
        expected = (
            b"\x04\xAA\xBB\xCC\xDD"
            + struct.pack("<i", 5)
            + b"hello"
            + b"\x0a"
            + b"spixi.voip"
        )
        self.assertEqual(raw, expected)

        raw_no_app = pack_app_data_message(sid, payload)
        self.assertEqual(raw_no_app, b"\x04\xAA\xBB\xCC\xDD" + struct.pack("<i", 5) + b"hello")

    def test_binary_edge_and_error_cases(self) -> None:
        # 0-byte and 255-byte session IDs
        self.assertEqual(unpack_app_data_message(pack_app_data_message(b"", b"")).session_id, b"")
        sid255 = bytes(range(255))
        self.assertEqual(unpack_app_data_message(pack_app_data_message(sid255, b"123")).session_id, sid255)

        with self.assertRaises(ValueError):
            pack_app_data_message(b"a" * 256, b"")

        # Negative data_len (M2 contract)
        with self.assertRaises(ValueError):
            unpack_app_data_message(b"\x01x" + struct.pack("<i", -5))

        # Truncated cases
        for bad in (
            b"",
            b"\x05abc",
            b"\x01a\x01\x00",
            b"\x01a" + struct.pack("<i", 4) + b"ab",
            b"\x01a" + struct.pack("<i", 0) + b"\x80",
            b"\x01a" + struct.pack("<i", 0) + b"\x80\x80\x80\x80\x80\x00",
            b"\x01a" + struct.pack("<i", 0) + b"\x04ab",
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    unpack_app_data_message(bad)

        # Invalid UTF-8 in appId replaced gracefully
        raw_bad_utf8 = b"\x01a" + struct.pack("<i", 0) + b"\x02\xff\xfe"
        dec = unpack_app_data_message(raw_bad_utf8)
        self.assertEqual(dec.app_id, "\ufffd\ufffd")


class TestExtractB64Bytes(unittest.TestCase):
    """Tier 1 & Tier 2 tests for _extract_b64_bytes payload parsing."""

    def test_supported_payload_shapes(self) -> None:
        raw = b"\x00\x01\xfe\xffhello"
        b64 = base64.b64encode(raw).decode("ascii")
        self.assertEqual(_extract_b64_bytes({"data": {"data": b64}}), raw)
        self.assertEqual(_extract_b64_bytes({"data": {"data": {"data": b64}}}), raw)
        self.assertEqual(_extract_b64_bytes({"data": b64}), raw)

    def test_missing_and_invalid_shapes(self) -> None:
        for payload in (
            {},
            {"data": None},
            {"data": 123},
            {"data": {}},
            {"data": {"data": None}},
            {"data": {"data": 42}},
        ):
            with self.subTest(payload=payload):
                self.assertEqual(_extract_b64_bytes(payload), b"")

        # Invalid base64 raises binascii.Error or returns b"" if guarded cleanly
        try:
            res = _extract_b64_bytes({"data": "!!!not_base64!!!"})
            self.assertEqual(res, b"")
        except (binascii.Error, ValueError):
            pass


class TestSpixiVoIPSessionManager(unittest.IsolatedAsyncioTestCase):
    """Tier 1–3 state machine, guard, concurrency, and stale-session tests."""

    async def test_full_initiator_and_callee_lifecycle(self) -> None:
        events: list[tuple[str, str]] = []
        pi_sent: list[tuple[str, int, bytes, int]] = []
        srv_sent: list[tuple[str, int, bytes, int]] = []

        async def send_pi(addr: str, mtype: int, data: bytes, ch: int) -> None:
            pi_sent.append((addr, mtype, data, ch))

        async def send_srv(addr: str, mtype: int, data: bytes, ch: int) -> None:
            srv_sent.append((addr, mtype, data, ch))

        async def on_start(who: str, contact: str, sid: bytes, codec: str) -> None:
            events.append((who, f"start:{contact}:{codec}"))

        async def on_end(who: str, contact: str, sid: bytes, codec: str) -> None:
            events.append((who, f"end:{contact}:{codec}"))

        pi_voip = SpixiVoIPSessionManager(
            send_pi,
            ("pcm", "opus"),
            on_session_started=lambda c, s, k: on_start("pi", c, s, k),
            on_session_ended=lambda c, s, k: on_end("pi", c, s, k),
        )
        srv_voip = SpixiVoIPSessionManager(
            send_srv,
            ("pcm", "opus"),
            on_session_started=lambda c, s, k: on_start("srv", c, s, k),
            on_session_ended=lambda c, s, k: on_end("srv", c, s, k),
        )

        sid = await pi_voip.initiate_call("1Srv")
        self.assertTrue(pi_voip.is_initiator)
        self.assertTrue(pi_voip.call_accepted)
        self.assertFalse(pi_voip.callee_accepted)
        self.assertFalse(pi_voip.is_in_call)

        req = unpack_app_data_message(pi_sent[-1][2])
        self.assertTrue(await srv_voip.on_received_call("1Pi", req, auto_accept=True))
        self.assertFalse(srv_voip.is_initiator)
        self.assertTrue(srv_voip.is_in_call)
        self.assertGreater(srv_voip.started_time, 0.0)

        acc = unpack_app_data_message(srv_sent[-1][2])
        self.assertTrue(await pi_voip.on_accepted_call("1Srv", acc))
        self.assertTrue(pi_voip.is_in_call)
        self.assertEqual(pi_voip.current_codec, "pcm")
        self.assertGreater(pi_voip.last_packet_received_time, 0.0)

        await pi_voip.hangup_call()
        await srv_voip.on_hangup_call(AppDataMessage(session_id=sid, data=b""))
        self.assertFalse(pi_voip.is_in_call)
        self.assertFalse(srv_voip.is_in_call)
        self.assertIn(("pi", "start:1Srv:pcm"), events)
        self.assertIn(("srv", "start:1Pi:pcm"), events)
        self.assertIn(("pi", "end:1Srv:pcm"), events)
        self.assertIn(("srv", "end:1Pi:pcm"), events)

    async def test_guards_and_mismatched_sessions(self) -> None:
        sent: list[tuple[int, bytes]] = []
        rx_audio: list[bytes] = []

        async def send_fn(addr: str, mtype: int, data: bytes, ch: int) -> None:
            sent.append((mtype, data))

        async def audio_cb(sid: bytes, data: bytes, codec: str) -> None:
            rx_audio.append(data)

        voip = SpixiVoIPSessionManager(send_fn, ("pcm",), on_audio_data=audio_cb)

        # Idle send_audio_frame returns False
        self.assertFalse(await voip.send_audio_frame(b"123"))

        sid1 = await voip.initiate_call("1Peer")
        # Duplicate initiate_call returns existing sid without extra APP_REQUEST
        self.assertEqual(await voip.initiate_call("1Peer"), sid1)
        self.assertEqual(len(sent), 1)

        # Wrong app_id ignored without reject
        wrong_app = AppDataMessage(session_id=b"other", data=b"pcm", app_id="com.other")
        self.assertFalse(await voip.on_received_call("1Other", wrong_app))
        self.assertEqual(len(sent), 1)

        # Wrong session_id on accept/data/reject/hangup ignored
        self.assertFalse(await voip.on_accepted_call("1Peer", AppDataMessage(session_id=b"wrong", data=b"pcm")))
        await voip.on_data("1Peer", AppDataMessage(session_id=b"wrong", data=b"audio"))
        await voip.on_rejected_call(AppDataMessage(session_id=b"wrong", data=b""))
        await voip.on_hangup_call(AppDataMessage(session_id=b"wrong", data=b""))
        self.assertEqual(rx_audio, [])
        self.assertEqual(voip.current_session_id, sid1)

        # Unsupported codec in on_accepted_call returns False
        self.assertFalse(await voip.on_accepted_call("1Peer", AppDataMessage(session_id=sid1, data=b"flac")))
        # Duplicate accept_call / on_accepted_call
        self.assertTrue(await voip.on_accepted_call("1Peer", AppDataMessage(session_id=sid1, data=b"pcm")))
        self.assertFalse(await voip.on_accepted_call("1Peer", AppDataMessage(session_id=sid1, data=b"pcm")))
        self.assertFalse(await voip.accept_call(sid1))

        # Empty audio frame updates timestamp without invoking on_audio_data
        before = voip.last_packet_received_time
        await asyncio.sleep(0.01)
        await voip.on_data("1Peer", AppDataMessage(session_id=sid1, data=b""))
        self.assertGreaterEqual(voip.last_packet_received_time, before)
        self.assertEqual(rx_audio, [])

    async def test_lock_free_io_reentrancy_and_stale_sessions(self) -> None:
        # (a) Re-entrant _send_spixi calling hangup_call/on_rejected_call must NOT deadlock (M2 contract)
        voip_holder: list[SpixiVoIPSessionManager] = []
        sent_codes: list[int] = []

        async def reentrant_send(addr: str, mtype: int, data: bytes, ch: int) -> None:
            sent_codes.append(mtype)
            if mtype == int(SpixiMessageCode.APP_REQUEST_REJECT):
                msg = unpack_app_data_message(data)
                await voip_holder[0].on_rejected_call(msg)

        voip = SpixiVoIPSessionManager(reentrant_send, ("pcm",))
        voip_holder.append(voip)
        sid = await voip.initiate_call("1Peer")

        # Active in-call must reject second caller without deadlock
        await voip.on_accepted_call("1Peer", AppDataMessage(session_id=sid, data=b"pcm"))
        third_party_req = AppDataMessage(session_id=b"third_party_sid", data=b"pcm", app_id=SPIXI_VOIP_APP_ID)
        accepted_third = await asyncio.wait_for(
            voip.on_received_call("1Third", third_party_req),
            timeout=1.0,
        )
        self.assertFalse(accepted_third)
        self.assertIn(int(SpixiMessageCode.APP_REQUEST_REJECT), sent_codes)
        self.assertEqual(voip.current_session_id, sid)

        # (b) Stale unaccepted initiator call recovery (M2 contract)
        voip2_codes: list[int] = []

        async def send2(addr: str, mtype: int, data: bytes, ch: int) -> None:
            voip2_codes.append(mtype)

        voip2 = SpixiVoIPSessionManager(send2, ("pcm",))
        stale_sid = await voip2.initiate_call("1Peer")
        # Backdate any internal timestamp attributes so the unaccepted call is unambiguously stale
        past = time.time() - 3600.0
        for attr in ("started_time", "last_packet_received_time", "initiated_time", "_initiated_at"):
            if hasattr(voip2, attr):
                setattr(voip2, attr, past)
        with patch("common_ixian.spixi_voip.time.time", return_value=past + 3600.0):
            # Also check if monotonic is used
            incoming = AppDataMessage(session_id=b"fresh_incoming_1", data=b"pcm", app_id=SPIXI_VOIP_APP_ID)
            recovered = await voip2.on_received_call("1Peer", incoming, auto_accept=True)
        self.assertTrue(recovered)
        self.assertNotEqual(voip2.current_session_id, stale_sid)
        self.assertEqual(voip2.current_session_id, b"fresh_incoming_1")
        self.assertTrue(voip2.is_in_call)

    async def test_tier5_authorize_caller_force_empty_sid_error_and_accept_rollback(self) -> None:
        sent: list[tuple[str, int, bytes]] = []
        ended: list[tuple[str, bytes, str]] = []
        errors: list[tuple[str, bytes, str]] = []

        async def send_fn(addr: str, mtype: int, data: bytes, ch: int) -> None:
            sent.append((addr, mtype, data))

        voip = SpixiVoIPSessionManager(
            send_fn,
            ("pcm", "opus"),
            on_session_ended=lambda c, s, k: _append_async(ended, (c, s, k)),
            on_request_error=lambda c, s, d: _append_async(errors, (c, s, d)),
            authorize_caller=lambda addr: addr == "1TrustedPi",
        )

        # 1. authorize_caller rejects untrusted caller and sends APP_REQUEST_REJECT
        ok = await voip.on_received_call(
            "1Untrusted",
            AppDataMessage(session_id=b"s_unauth", data=b"pcm", app_id=SPIXI_VOIP_APP_ID),
        )
        self.assertFalse(ok)
        self.assertEqual(sent[-1][0], "1Untrusted")
        self.assertEqual(sent[-1][1], int(SpixiMessageCode.APP_REQUEST_REJECT))
        self.assertIsNone(voip.current_session_id)

        # 2. initiate_call(..., force=True) replaces active session and notifies on_session_ended
        sid1 = await voip.initiate_call("1TrustedPi")
        await voip.on_accepted_call("1TrustedPi", AppDataMessage(session_id=sid1, data=b"pcm"))
        self.assertTrue(voip.is_in_call)
        sid2 = await voip.initiate_call("1TrustedPi", force=True)
        self.assertNotEqual(sid2, sid1)
        self.assertEqual(ended[-1], ("1TrustedPi", sid1, "pcm"))
        self.assertEqual(voip.current_session_id, sid2)

        # 3. on_request_error with empty session_id cleans up matching sender / pending session
        self.assertTrue(await voip.on_request_error(AppDataMessage(session_id=b"", data=b"busy"), "1TrustedPi"))
        self.assertIsNone(voip.current_session_id)
        self.assertEqual(errors[-1], ("1TrustedPi", sid2, "busy"))

        sid3 = await voip.initiate_call("1TrustedPi")
        self.assertTrue(await voip.on_request_error(AppDataMessage(session_id=b"", data=b"timeout"), ""))
        self.assertIsNone(voip.current_session_id)
        self.assertEqual(errors[-1], ("1TrustedPi", sid3, "timeout"))

        # 4. accept_call send failure rolls back half-open session completely
        async def failing_accept_send(addr: str, mtype: int, data: bytes, ch: int) -> None:
            if mtype == int(SpixiMessageCode.APP_REQUEST_ACCEPT):
                raise RuntimeError("accept TX failed")

        voip_fail = SpixiVoIPSessionManager(failing_accept_send, ("pcm",))
        with self.assertRaises(RuntimeError):
            await voip_fail.on_received_call(
                "1TrustedPi",
                AppDataMessage(session_id=b"s_fail_accept", data=b"pcm", app_id=SPIXI_VOIP_APP_ID),
                auto_accept=True,
            )
        self.assertIsNone(voip_fail.current_session_id)
        self.assertIsNone(voip_fail.current_contact)
        self.assertIsNone(voip_fail.current_codec)
        self.assertFalse(voip_fail.callee_accepted)
        self.assertFalse(voip_fail.call_accepted)
        self.assertFalse(voip_fail.is_in_call)

    async def test_tier5_empty_session_id_rejected_in_on_received_call(self) -> None:
        sent: list[tuple[str, int, bytes]] = []

        async def send_fn(addr: str, mtype: int, data: bytes, ch: int) -> None:
            sent.append((addr, mtype, data))

        voip = SpixiVoIPSessionManager(send_fn, ("pcm", "opus"))
        ok = await voip.on_received_call(
            "1Peer",
            AppDataMessage(session_id=b"", data=b"pcm", app_id=SPIXI_VOIP_APP_ID),
            auto_accept=True,
        )
        self.assertFalse(ok)
        self.assertFalse(voip.is_in_call)
        self.assertIsNone(voip.current_session_id)
        self.assertIsNone(voip.current_contact)
        self.assertEqual(sent, [])


class TestQuIXIClientRESTAndMQTT(unittest.IsolatedAsyncioTestCase):
    """Tier 1–3 tests for QuIXIClient REST endpoints, MQTT routing, and error checking."""

    async def test_rest_endpoints_payload_format(self) -> None:
        fake_http = FakeHTTPSession(
            post_handler=lambda url, body: FakeHTTPResponse(200, json.dumps({"result": {"url": url, "body": body}}))
        )
        fake_mqtt = FakeMQTTClient()
        client = QuIXIClient(rest_url="http://127.0.0.1:8001/")
        with (
            patch("common_ixian.quixi_client.aiohttp.ClientSession", return_value=fake_http),
            patch("common_ixian.quixi_client.mqtt.Client", return_value=fake_mqtt),
        ):
            await client.start()

        await client.contacts()
        await client.add_contact("1Alice")
        await client.accept_contact("1Alice")
        await client.send_chat_message("1Alice", "hi", channel=2)
        await client.send_chat_stream_message("1Alice", "chunk", message_id_b64="bWlk", sequence=3, is_stream=True)
        await client.send_chat_stream_message("1Alice", "final", is_stream=False)
        await client.send_spixi_message("1Alice", 21, b"\xde\xad\xbe\xef", channel=0)
        await client.send_app_data("1Alice", '{"kind":"ping"}')

        urls = [u for u, _ in fake_http.calls]
        bodies = [b for _, b in fake_http.calls]
        self.assertEqual(
            urls,
            [
                "http://127.0.0.1:8001/contacts",
                "http://127.0.0.1:8001/addContact",
                "http://127.0.0.1:8001/acceptContact",
                "http://127.0.0.1:8001/sendChatMessage",
                "http://127.0.0.1:8001/sendChatStreamMessage",
                "http://127.0.0.1:8001/sendChatStreamMessage",
                "http://127.0.0.1:8001/sendSpixiMessage",
                "http://127.0.0.1:8001/sendAppData",
            ],
        )
        self.assertEqual(bodies[3]["params"], {"address": "1Alice", "message": "hi", "channel": "2"})
        self.assertEqual(
            bodies[4]["params"],
            {"address": "1Alice", "message": "chunk", "channel": "0", "isStream": "true", "messageId": "bWlk", "sequence": "3"},
        )
        self.assertEqual(bodies[5]["params"]["isStream"], "false")
        self.assertNotIn("messageId", bodies[5]["params"])
        self.assertEqual(
            bodies[6]["params"],
            {"address": "1Alice", "type": "21", "data": "deadbeef", "channel": "0"},
        )
        self.assertEqual(
            bodies[7]["params"],
            {"address": "1Alice", "protocolId": "com.tamagci.agent", "data": '{"kind":"ping"}'},
        )
        await client.stop()
        self.assertTrue(fake_http.closed)
        self.assertTrue(fake_mqtt.loop_stopped)
        self.assertTrue(fake_mqtt.disconnected)

    async def test_mqtt_connect_subscribes_and_dispatches(self) -> None:
        fake_http = FakeHTTPSession()
        fake_mqtt = FakeMQTTClient()
        client = QuIXIClient(mqtt_username="u1", mqtt_password="p1")
        with (
            patch("common_ixian.quixi_client.aiohttp.ClientSession", return_value=fake_http),
            patch("common_ixian.quixi_client.mqtt.Client", return_value=fake_mqtt),
        ):
            await client.start()

        self.assertEqual(fake_mqtt.credentials, ("u1", "p1"))
        self.assertTrue(fake_mqtt.loop_started)
        fake_mqtt.emit_connect()
        await asyncio.wait_for(client._connected_event.wait(), timeout=1.0)

        subscribed_topics = {t[0] if isinstance(t, tuple) else t for t in fake_mqtt.subscriptions}
        expected_topics = {
            "AppRequest/#",
            "AppRequestAccept/#",
            "AppRequestReject/#",
            "AppRequestError/#",
            "AppData/#",
            "AppEndSession/#",
            "AppProtocolData/#",
            "ChatStream/#",
            "Chat/#",
            "FriendStatusUpdate/#",
        }
        self.assertTrue(expected_topics.issubset(subscribed_topics))
        await client.stop()

    async def test_rest_and_mqtt_error_handling(self) -> None:
        client = QuIXIClient()
        with self.assertRaises(RuntimeError):
            await client.contacts()

        # Empty body -> {}, HTTP 500 -> raises, JSON {"error": ...} -> raises (M2 contract)
        responses = [
            FakeHTTPResponse(200, ""),
            FakeHTTPResponse(500, "Server Error"),
            FakeHTTPResponse(200, json.dumps({"result": None, "error": {"code": -1, "message": "Peer offline"}})),
        ]
        fake_http = FakeHTTPSession(post_handler=lambda u, b: responses.pop(0))
        fake_mqtt = FakeMQTTClient()

        # Also test paho v1 fallback (AttributeError on CallbackAPIVersion)
        def legacy_client_factory(*args, **kwargs):
            return fake_mqtt

        with (
            patch("common_ixian.quixi_client.aiohttp.ClientSession", return_value=fake_http),
            patch("common_ixian.quixi_client.mqtt") as mock_mqtt_mod,
        ):
            mock_mqtt_mod.Client.side_effect = legacy_client_factory
            del mock_mqtt_mod.CallbackAPIVersion  # Force AttributeError fallback
            await client.start()

        self.assertEqual(await client.contacts(), {})
        with self.assertRaises(Exception):
            await client.contacts()
        with self.assertRaises(Exception):
            await client.contacts()

        # Malformed MQTT JSON & non-UTF8 must not crash
        client._on_mqtt_message(fake_mqtt, None, type("M", (), {"topic": "Chat/1A", "payload": b"\xff\xfe{bad"})())
        client._on_mqtt_message(fake_mqtt, None, type("M", (), {"topic": "Chat/1A", "payload": b"{not-json"})())

        # Idempotent stop()
        await client.stop()
        await client.stop()

    async def test_mqtt_voip_lifecycle_and_callback_isolation(self) -> None:
        bus = FakeQuIXIBus()
        pi_client = QuIXIClient()
        srv_client = QuIXIClient()
        pi_http, pi_mqtt = bus.register("1Pi", pi_client)
        srv_http, srv_mqtt = bus.register("1Srv", srv_client)

        pi_client._loop = asyncio.get_running_loop()
        srv_client._loop = asyncio.get_running_loop()
        pi_client._http = pi_http
        pi_client._mqtt = pi_mqtt
        srv_client._http = srv_http
        srv_client._mqtt = srv_mqtt
        pi_mqtt.on_message = pi_client._on_mqtt_message
        srv_mqtt.on_message = srv_client._on_mqtt_message

        pi_voip = SpixiVoIPSessionManager(pi_client.send_spixi_message, ("pcm",))
        srv_voip = SpixiVoIPSessionManager(srv_client.send_spixi_message, ("pcm",))
        pi_client.attach_voip_manager(pi_voip)
        srv_client.attach_voip_manager(srv_voip)

        callback_hits: list[str] = []

        async def bad_cb(topic: str, payload: dict) -> None:
            callback_hits.append("bad")
            raise RuntimeError("boom")

        async def good_cb(topic: str, payload: dict) -> None:
            callback_hits.append("good")

        pi_client.on_topic("AppData", bad_cb)
        pi_client.on_topic("AppData", good_cb)

        # Malformed VoIP payload in AppData must still invoke registered on_topic callbacks
        await pi_client._dispatch_mqtt_event("AppData/1Srv", {"sender": "1Srv", "data": {"data": ""}})
        self.assertEqual(callback_hits, ["bad", "good"])

        # Full bus call + AppRequestError reset
        sid = await pi_voip.initiate_call("1Srv")
        await _wait_until(lambda: pi_voip.is_in_call and srv_voip.is_in_call)
        self.assertTrue(pi_voip.is_in_call)
        self.assertTrue(srv_voip.is_in_call)

        err_bytes = pack_app_data_message(sid, b"")
        bus.deliver_mqtt(
            "1Pi",
            "AppRequestError/1Srv",
            {
                "sender": "1Srv",
                "data": {"type": 30, "data": base64.b64encode(err_bytes).decode("ascii")},
            },
        )
        await _wait_until(lambda: not pi_voip.is_in_call)
        self.assertFalse(pi_voip.is_in_call)

    async def test_tier5_quixi_error_false_allowed_and_closed_loop_mqtt_guard(self) -> None:
        from common_ixian.quixi_client import QuIXIError, is_quixi_ok

        self.assertTrue(is_quixi_ok({"result": "sent", "error": False}))
        self.assertFalse(is_quixi_ok({"result": "sent", "error": True}))

        responses = [
            FakeHTTPResponse(200, json.dumps({"result": "sent", "error": False})),
            FakeHTTPResponse(200, json.dumps({"result": "ok", "error": True})),
        ]
        fake_http = FakeHTTPSession(post_handler=lambda u, b: responses.pop(0))
        fake_mqtt = FakeMQTTClient()
        client = QuIXIClient()
        with (
            patch("common_ixian.quixi_client.aiohttp.ClientSession", return_value=fake_http),
            patch("common_ixian.quixi_client.mqtt.Client", return_value=fake_mqtt),
        ):
            await client.start()

        res = await client.send_app_data("1Peer", '{"kind":"ping"}')
        self.assertEqual(res, {"result": "sent", "error": False})
        with self.assertRaises(QuIXIError):
            await client.send_app_data("1Peer", '{"kind":"ping"}')

        # Closed-loop guard on _on_mqtt_message
        closed_loop = asyncio.new_event_loop()
        closed_loop.close()
        client._loop = closed_loop
        msg = type("M", (), {"topic": "Chat/1A", "payload": b'{"sender":"1A","data":{"data":""}}'})()
        client._on_mqtt_message(fake_mqtt, None, msg)
        await client.stop()


class TestQuIXIEndToEndScenario(unittest.IsolatedAsyncioTestCase):
    """Tier 4: Two QuIXIClient + SpixiVoIPSessionManager nodes wired via FakeQuIXIBus."""

    async def test_pi_to_server_call_audio_chatstream_tools_and_error_redial(self) -> None:
        bus = FakeQuIXIBus()
        pi_client = QuIXIClient()
        srv_client = QuIXIClient()
        pi_http, pi_mqtt = bus.register("1PiNode", pi_client)
        srv_http, srv_mqtt = bus.register("1SrvNode", srv_client)

        loop = asyncio.get_running_loop()
        for cli, h, m in ((pi_client, pi_http, pi_mqtt), (srv_client, srv_http, srv_mqtt)):
            cli._loop = loop
            cli._http = h
            cli._mqtt = m
            m.on_message = cli._on_mqtt_message

        srv_audio: list[bytes] = []
        pi_audio: list[bytes] = []
        pi_streams: list[str] = []
        pi_proto: list[dict] = []

        pi_voip = SpixiVoIPSessionManager(
            pi_client.send_spixi_message,
            ("pcm", "opus"),
            on_audio_data=lambda s, d, c: _append_async(pi_audio, d),
        )
        srv_voip = SpixiVoIPSessionManager(
            srv_client.send_spixi_message,
            ("pcm", "opus"),
            on_audio_data=lambda s, d, c: _append_async(srv_audio, d),
        )
        pi_client.attach_voip_manager(pi_voip)
        srv_client.attach_voip_manager(srv_voip)

        pi_client.on_topic(
            "ChatStream",
            lambda t, p: _append_async(pi_streams, _extract_b64_bytes(p).decode("utf-8")),
        )
        pi_client.on_topic(
            "AppProtocolData",
            lambda t, p: _append_async(pi_proto, json.loads(_extract_b64_bytes(p).decode("utf-8"))),
        )

        # 1. Dial
        sid1 = await pi_voip.initiate_call("1SrvNode")
        await _wait_until(lambda: pi_voip.is_in_call and srv_voip.is_in_call)
        self.assertTrue(pi_voip.is_in_call)
        self.assertTrue(srv_voip.is_in_call)

        # 2. Uplink PCM + Server ChatStream + AppProtocolData + downlink PCM
        await pi_voip.send_audio_frame(b"UPLINK_PCM_1")
        await srv_client.send_chat_stream_message("1PiNode", "Hello ", message_id_b64="m1", sequence=0, is_stream=True)
        await srv_client.send_chat_stream_message("1PiNode", "world!", message_id_b64="m1", sequence=1, is_stream=True)
        await srv_client.send_app_data("1PiNode", json.dumps({"kind": "face_state", "emo": "HAPPY"}))
        await srv_voip.send_audio_frame(b"DOWNLINK_TTS_1")
        await srv_client.send_app_data("1PiNode", json.dumps({"kind": "tts_end", "turn_id": "t1"}))
        await _wait_until(lambda: len(srv_audio) == 1 and len(pi_audio) == 1 and len(pi_streams) == 2 and len(pi_proto) == 2)

        self.assertEqual(srv_audio, [b"UPLINK_PCM_1"])
        self.assertEqual(pi_audio, [b"DOWNLINK_TTS_1"])
        self.assertEqual(pi_streams, ["Hello ", "world!"])
        self.assertEqual([m["kind"] for m in pi_proto], ["face_state", "tts_end"])

        # 3. Simulated APP_REQUEST_ERROR + immediate redial
        err_pkt = pack_app_data_message(sid1, b"")
        bus.deliver_mqtt(
            "1PiNode",
            "AppRequestError/1SrvNode",
            {"sender": "1SrvNode", "data": {"type": 30, "data": base64.b64encode(err_pkt).decode("ascii")}},
        )
        await srv_voip.hangup_call()
        await _wait_until(lambda: not pi_voip.is_in_call and not srv_voip.is_in_call)
        self.assertFalse(pi_voip.is_in_call)

        sid2 = await pi_voip.initiate_call("1SrvNode")
        await _wait_until(lambda: pi_voip.is_in_call and srv_voip.is_in_call)
        self.assertNotEqual(sid2, sid1)
        self.assertTrue(pi_voip.is_in_call)
        self.assertTrue(srv_voip.is_in_call)


async def _append_async(target: list, value: object) -> None:
    target.append(value)


async def _wait_until(pred, timeout: float = 1.0) -> None:
    deadline = time.monotonic() + timeout
    while not pred() and time.monotonic() < deadline:
        await asyncio.sleep(0.005)


if __name__ == "__main__":
    unittest.main()
