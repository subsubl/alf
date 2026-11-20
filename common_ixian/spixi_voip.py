"""
Python implementation of the Ixian Spixi VoIP protocol matching:
- ixian-platform/Spixi/Spixi/VoIP/VoIPManager.cs
- ixian-platform/Ixian-Core/Streaming/SpixiMessage.cs
- ixian-platform/Ixian-Core/Streaming/Models/AppDataMessage.cs
"""

from __future__ import annotations

import asyncio
import logging
import struct
import time
import uuid
from dataclasses import dataclass
from enum import IntEnum
from typing import Awaitable, Callable, Iterable, Optional

logger = logging.getLogger(__name__)

SPIXI_VOIP_APP_ID = "spixi.voip"


class SpixiMessageCode(IntEnum):
    """Exact enum values from IXICore.SpixiMessageCode."""

    CHAT = 0
    FILE_DATA = 10
    REQUEST_FILE_DATA = 11
    FILE_HEADER = 12
    ACCEPT_FILE = 13
    APP_DATA = 21
    APP_REQUEST = 22
    FILE_FULLY_RECEIVED = 23
    APP_REQUEST_ACCEPT = 28
    APP_REQUEST_REJECT = 29
    APP_REQUEST_ERROR = 30
    APP_END_SESSION = 31
    APP_PROTOCOL_DATA = 45
    CHAT_STREAM = 53


@dataclass
class AppDataMessage:
    """Exact binary counterpart to IXICore.Streaming.Models.AppDataMessage."""

    session_id: bytes
    data: bytes = b""
    app_id: Optional[str] = None


def _write_7bit_encoded_int(buf: bytearray, value: int) -> None:
    if value < 0:
        raise ValueError(f"7-bit encoded int must be non-negative, got {value}")
    v = value
    while v >= 0x80:
        buf.append((v & 0x7F) | 0x80)
        v >>= 7
    buf.append(v)


def _read_7bit_encoded_int(raw: bytes, offset: int) -> tuple[int, int]:
    count = 0
    shift = 0
    while offset < len(raw):
        b = raw[offset]
        offset += 1
        count |= (b & 0x7F) << shift
        if (b & 0x80) == 0:
            return count, offset
        shift += 7
        if shift >= 35:
            raise ValueError("Invalid 7-bit encoded string length in AppDataMessage")
    raise ValueError("Truncated 7-bit encoded string length in AppDataMessage")


def pack_app_data_message(
    session_id: bytes,
    data: bytes = b"",
    app_id: Optional[str] = None,
) -> bytes:
    """Serialize AppDataMessage matching C# BinaryWriter layout."""
    if len(session_id) > 255:
        raise ValueError("session_id length must fit in 1 byte (<= 255)")

    buf = bytearray()
    buf.append(len(session_id))
    buf.extend(session_id)

    buf.extend(struct.pack("<i", len(data)))
    if data:
        buf.extend(data)

    if app_id is not None:
        app_bytes = app_id.encode("utf-8")
        _write_7bit_encoded_int(buf, len(app_bytes))
        buf.extend(app_bytes)

    return bytes(buf)


def unpack_app_data_message(raw: bytes) -> AppDataMessage:
    """Deserialize AppDataMessage matching C# BinaryReader layout."""
    if not raw:
        raise ValueError("Empty AppDataMessage bytes")

    offset = 0
    sid_len = raw[offset]
    offset += 1
    if offset + sid_len > len(raw):
        raise ValueError("Truncated session_id in AppDataMessage")
    session_id = raw[offset : offset + sid_len]
    offset += sid_len

    if offset + 4 > len(raw):
        raise ValueError("Truncated data_length in AppDataMessage")
    (data_len,) = struct.unpack_from("<i", raw, offset)
    offset += 4
    if data_len < 0:
        raise ValueError(f"Negative data_length in AppDataMessage: {data_len}")

    data = b""
    if data_len > 0:
        if offset + data_len > len(raw):
            raise ValueError("Truncated data payload in AppDataMessage")
        data = raw[offset : offset + data_len]
        offset += data_len

    app_id: Optional[str] = None
    if offset < len(raw):
        str_len, offset = _read_7bit_encoded_int(raw, offset)
        if offset + str_len > len(raw):
            raise ValueError("Truncated appId string in AppDataMessage")
        app_id = raw[offset : offset + str_len].decode("utf-8", errors="replace")

    return AppDataMessage(session_id=session_id, data=data, app_id=app_id)


SendSpixiFn = Callable[[str, int, bytes, int], Awaitable[None]]
AudioCallback = Callable[[bytes, bytes, str], Awaitable[None]]
SessionEventCallback = Callable[[str, bytes, str], Awaitable[None]]


class SpixiVoIPSessionManager:
    """
    Async VoIP session state machine mirroring Spixi/VoIP/VoIPManager.cs
    for use via QuIXI's /sendSpixiMessage endpoint and MQTT topics.
    """

    def __init__(
        self,
        send_spixi_fn: SendSpixiFn,
        supported_codecs: Iterable[str] = ("pcm", "opus"),
        on_audio_data: Optional[AudioCallback] = None,
        on_session_started: Optional[SessionEventCallback] = None,
        on_session_ended: Optional[SessionEventCallback] = None,
        on_request_error: Optional[SessionEventCallback] = None,
        authorize_caller: Optional[Callable[[str], bool]] = None,
        can_preempt_caller: Optional[Callable[[str, Optional[str]], bool]] = None,
        pending_timeout: float = 9.0,
    ) -> None:
        self._send_spixi = send_spixi_fn
        self._supported_codecs = list(supported_codecs)
        self._on_audio_data = on_audio_data
        self._on_session_started = on_session_started
        self._on_session_ended = on_session_ended
        self._on_request_error = on_request_error
        self._authorize_caller = authorize_caller
        self._can_preempt_caller = can_preempt_caller
        self._pending_timeout = max(0.0, float(pending_timeout))

        self.current_session_id: Optional[bytes] = None
        self.current_contact: Optional[str] = None
        self.current_codec: Optional[str] = None
        self.call_accepted: bool = False
        self.callee_accepted: bool = False
        self.is_initiator: bool = False
        self.started_time: float = 0.0
        self.last_packet_received_time: float = 0.0
        self._pending_since: float = 0.0
        self._lock = asyncio.Lock()

    @property
    def is_in_call(self) -> bool:
        return (
            self.current_session_id is not None
            and self.call_accepted
            and self.callee_accepted
            and self.current_codec is not None
        )

    def has_session(self, session_id: Optional[bytes]) -> bool:
        return (
            self.current_session_id is not None
            and session_id is not None
            and self.current_session_id == session_id
        )

    def _reset_session_locked(self) -> tuple[Optional[str], Optional[bytes], str]:
        contact = self.current_contact
        sid = self.current_session_id
        codec = self.current_codec or ""
        self.current_session_id = None
        self.current_contact = None
        self.current_codec = None
        self.call_accepted = False
        self.callee_accepted = False
        self.is_initiator = False
        self.started_time = 0.0
        self.last_packet_received_time = 0.0
        self._pending_since = 0.0
        return contact, sid, codec

    async def initiate_call(self, contact_address: str, force: bool = False) -> bytes:
        """Mirror VoIPManager.initiateCall: send appRequest (type 22) with 'spixi.voip'."""
        prev_ended: Optional[tuple[Optional[str], Optional[bytes], str]] = None
        async with self._lock:
            if self.current_session_id is not None:
                elapsed = time.monotonic() - self._pending_since
                replaceable = force or (not self.is_in_call and elapsed >= self._pending_timeout)
                if not replaceable:
                    return self.current_session_id
                if self.started_time > 0 or self.is_in_call:
                    prev_ended = (
                        self.current_contact,
                        self.current_session_id,
                        self.current_codec or "",
                    )
                self._reset_session_locked()

            session_id = uuid.uuid4().bytes
            self.current_session_id = session_id
            self.current_contact = contact_address
            self.call_accepted = True
            self.callee_accepted = False
            self.current_codec = None
            self.is_initiator = True
            self.started_time = 0.0
            self.last_packet_received_time = 0.0
            self._pending_since = time.monotonic()

            codecs_payload = "|".join(self._supported_codecs).encode("utf-8")
            packed = pack_app_data_message(
                session_id=session_id,
                data=codecs_payload,
                app_id=SPIXI_VOIP_APP_ID,
            )

        if prev_ended is not None:
            prev_contact, prev_sid, prev_codec = prev_ended
            if prev_sid is not None and prev_contact is not None and self._on_session_ended:
                await self._on_session_ended(prev_contact, prev_sid, prev_codec)

        try:
            await self._send_spixi(contact_address, int(SpixiMessageCode.APP_REQUEST), packed, 0)
        except Exception:
            async with self._lock:
                if self.current_session_id == session_id:
                    self._reset_session_locked()
            raise

        logger.info("Initiated spixi.voip call to %s session=%s", contact_address, session_id.hex())
        return session_id

    async def on_received_call(
        self,
        sender_address: str,
        app_msg: AppDataMessage,
        auto_accept: bool = True,
    ) -> bool:
        """Mirror VoIPManager.onReceivedCall: negotiate codec and optionally auto-accept."""
        if not app_msg.session_id:
            return False

        if app_msg.app_id and app_msg.app_id != SPIXI_VOIP_APP_ID:
            return False

        if self._authorize_caller is not None and not self._authorize_caller(sender_address):
            logger.warning("Rejected unauthorized spixi.voip call from %s", sender_address)
            reject_bytes = pack_app_data_message(app_msg.session_id, b"")
            await self._send_spixi(
                sender_address, int(SpixiMessageCode.APP_REQUEST_REJECT), reject_bytes, 0
            )
            return False

        reject_bytes: Optional[bytes] = None
        prev_ended: Optional[tuple[Optional[str], Optional[bytes], str]] = None
        already_active = False

        async with self._lock:
            if self.has_session(app_msg.session_id) and not self.is_initiator:
                already_active = True
            else:
                if self.current_session_id is not None and not self.has_session(app_msg.session_id):
                    elapsed = time.monotonic() - self._pending_since
                    can_preempt = bool(
                        self._can_preempt_caller is not None
                        and self._can_preempt_caller(sender_address, self.current_contact)
                    )
                    replaceable = (
                        (sender_address == self.current_contact)
                        or can_preempt
                        or (not self.is_in_call and elapsed >= self._pending_timeout)
                    )
                    if not replaceable:
                        reject_bytes = pack_app_data_message(app_msg.session_id, b"")

                if reject_bytes is None:
                    codecs_str = app_msg.data.decode("utf-8", errors="ignore")
                    offered = [c.strip() for c in codecs_str.split("|") if c.strip()]
                    chosen = next((c for c in offered if c in self._supported_codecs), None)
                    if chosen is None:
                        logger.warning(
                            "Unsupported codecs offered by %s: %s", sender_address, codecs_str
                        )
                        reject_bytes = pack_app_data_message(app_msg.session_id, b"")
                    else:
                        if self.current_session_id is not None and not self.has_session(
                            app_msg.session_id
                        ):
                            if self.started_time > 0 or self.is_in_call:
                                prev_ended = (
                                    self.current_contact,
                                    self.current_session_id,
                                    self.current_codec or "",
                                )
                            self._reset_session_locked()

                        self.current_session_id = app_msg.session_id
                        self.current_contact = sender_address
                        self.callee_accepted = True
                        self.call_accepted = False
                        self.current_codec = chosen
                        self.is_initiator = False
                        self.started_time = 0.0
                        self.last_packet_received_time = 0.0
                        self._pending_since = time.monotonic()

        if already_active:
            return True

        if reject_bytes is not None:
            await self._send_spixi(
                sender_address, int(SpixiMessageCode.APP_REQUEST_REJECT), reject_bytes, 0
            )
            return False

        if prev_ended is not None:
            prev_contact, prev_sid, prev_codec = prev_ended
            if prev_sid is not None and prev_contact is not None and self._on_session_ended:
                await self._on_session_ended(prev_contact, prev_sid, prev_codec)

        if auto_accept:
            await self.accept_call(app_msg.session_id)
        return True

    async def accept_call(self, session_id: bytes) -> bool:
        """Mirror VoIPManager.acceptCall: send appRequestAccept (type 28) with chosen codec."""
        async with self._lock:
            if not self.has_session(session_id) or self.call_accepted:
                return False
            if self.current_contact is None or self.current_codec is None:
                return False

            self.call_accepted = True
            self.started_time = time.time()
            self.last_packet_received_time = self.started_time
            contact = self.current_contact
            codec = self.current_codec

            packed = pack_app_data_message(session_id, codec.encode("utf-8"))

        try:
            await self._send_spixi(contact, int(SpixiMessageCode.APP_REQUEST_ACCEPT), packed, 0)
        except Exception:
            async with self._lock:
                if self.has_session(session_id):
                    self._reset_session_locked()
            raise

        if not self.has_session(session_id):
            return False
        logger.info("Accepted spixi.voip call from %s codec=%s", contact, codec)
        if self._on_session_started:
            await self._on_session_started(contact, session_id, codec)
        return True

    async def on_accepted_call(self, sender_address: str, app_msg: AppDataMessage) -> bool:
        """Mirror VoIPManager.onAcceptedCall."""
        async with self._lock:
            if not self.has_session(app_msg.session_id) or self.callee_accepted:
                return False

            codec = app_msg.data.decode("utf-8", errors="ignore").strip()
            if codec not in self._supported_codecs:
                logger.warning("Peer selected unsupported codec: %s", codec)
                return False

            self.current_codec = codec
            self.callee_accepted = True
            self.started_time = time.time()
            self.last_packet_received_time = self.started_time
            contact = self.current_contact or sender_address
            session_id = self.current_session_id

        logger.info("Call accepted by %s codec=%s", contact, codec)
        if self._on_session_started and session_id is not None:
            await self._on_session_started(contact, session_id, codec)
        return True

    async def on_rejected_call(self, app_msg: AppDataMessage) -> None:
        if app_msg.session_id:
            await self._end_session(expected_session_id=app_msg.session_id)

    async def on_request_error(self, app_msg: AppDataMessage, sender_address: str = "") -> bool:
        """Handle SpixiMessageCode.APP_REQUEST_ERROR (30): clean up matching session."""
        async with self._lock:
            if self.current_session_id is None:
                return False
            matches = self.has_session(app_msg.session_id) or (
                not app_msg.session_id
                and (
                    (bool(sender_address) and sender_address == self.current_contact)
                    or (not sender_address and not self.is_in_call)
                )
            )
            if not matches:
                return False
            contact, sid, codec = self._reset_session_locked()

        err_detail = (
            app_msg.data.decode("utf-8", errors="replace").strip() if app_msg.data else codec
        )
        if sid is not None and contact is not None:
            if self._on_session_ended:
                await self._on_session_ended(contact, sid, codec)
            if self._on_request_error:
                await self._on_request_error(contact, sid, err_detail)
        return True

    async def send_audio_frame(self, audio_bytes: bytes) -> bool:
        """Mirror VoIPManager audioRecorder callback: send appData (type 21)."""
        if not self.is_in_call or self.current_contact is None or self.current_session_id is None:
            return False

        packed = pack_app_data_message(self.current_session_id, audio_bytes)
        await self._send_spixi(self.current_contact, int(SpixiMessageCode.APP_DATA), packed, 0)
        return True

    async def on_data(self, sender_address: str, app_msg: AppDataMessage) -> None:
        """Mirror VoIPManager.onData."""
        if not self.has_session(app_msg.session_id):
            return
        self.last_packet_received_time = time.time()
        if self._on_audio_data and app_msg.data:
            await self._on_audio_data(
                app_msg.session_id,
                app_msg.data,
                self.current_codec or "pcm",
            )

    async def hangup_call(self, session_id: Optional[bytes] = None) -> None:
        """Mirror VoIPManager.hangupCall: send appEndSession (type 31)."""
        async with self._lock:
            target_sid = session_id or self.current_session_id
            contact = self.current_contact

        try:
            if target_sid is not None and contact is not None:
                packed = pack_app_data_message(target_sid, b"")
                await self._send_spixi(contact, int(SpixiMessageCode.APP_END_SESSION), packed, 0)
        finally:
            await self._end_session(expected_session_id=target_sid)

    async def on_hangup_call(self, app_msg: AppDataMessage) -> None:
        if app_msg.session_id:
            await self._end_session(expected_session_id=app_msg.session_id)

    async def _end_session(self, expected_session_id: Optional[bytes] = None) -> None:
        async with self._lock:
            if expected_session_id is not None and self.current_session_id != expected_session_id:
                return
            contact, sid, codec = self._reset_session_locked()

        if sid is not None and contact is not None and self._on_session_ended:
            await self._on_session_ended(contact, sid, codec)
