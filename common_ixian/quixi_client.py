"""
Hybrid QuIXI Client combining:
1. Local HTTP REST API (http://127.0.0.1:8001) matching QuIXI/API/APIServer.cs
2. Local Mosquitto MQTT Subscriber (127.0.0.1:1883) for real-time MQTopics events
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import uuid
from typing import Any, Awaitable, Callable, Dict, Optional
from urllib.parse import urlencode

try:
    import aiohttp
except ImportError:
    aiohttp = None  # type: ignore[assignment]

try:
    import paho.mqtt.client as mqtt
except ImportError:
    mqtt = None  # type: ignore[assignment]

from .spixi_voip import AppDataMessage, SpixiVoIPSessionManager, unpack_app_data_message

logger = logging.getLogger(__name__)

MQCallback = Callable[[str, Dict[str, Any]], Awaitable[None]]


class QuIXIError(RuntimeError):
    """Raised when QuIXI REST API returns an HTTP error or JSON-RPC error payload."""

    def __init__(
        self,
        message: str,
        method: str = "",
        code: Any = None,
        payload: Any = None,
    ) -> None:
        super().__init__(message)
        self.method = method
        self.code = code
        self.payload = payload


def is_quixi_ok(res: Any) -> bool:
    """Return True iff `res` looks like a successful QuIXI REST response."""
    if not isinstance(res, dict) or not res:
        return False
    err = res.get("error")
    if err is not None and err is not False:
        return False
    if "result" in res and res["result"] is None:
        return False
    if res.get("ok") is False:
        return False
    return True


def _extract_b64_bytes(payload: Dict[str, Any]) -> bytes:
    """
    Extract base64-encoded payload bytes from QuIXI JsonStreamMessageSerializer output.
    Handles both flat `data.data` and wrapped `data.data.data` structures across QuIXI versions.
    """
    inner = payload.get("data")
    if isinstance(inner, dict):
        d = inner.get("data")
        if isinstance(d, dict):
            d = d.get("data")
        if isinstance(d, str):
            return base64.b64decode(d)
    elif isinstance(inner, str):
        return base64.b64decode(inner)
    return b""


class QuIXIClient:
    """Async REST + MQTT client for a local QuIXI node."""

    def __init__(
        self,
        rest_url: str = "http://127.0.0.1:8001",
        mqtt_host: str = "127.0.0.1",
        mqtt_port: int = 1883,
        mqtt_username: Optional[str] = None,
        mqtt_password: Optional[str] = None,
    ) -> None:
        self.rest_url = rest_url.rstrip("/")
        self.mqtt_host = mqtt_host
        self.mqtt_port = mqtt_port
        self.mqtt_username = mqtt_username
        self.mqtt_password = mqtt_password

        self._http: Optional[aiohttp.ClientSession] = None
        self._mqtt: Optional[mqtt.Client] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._topic_handlers: Dict[str, list[MQCallback]] = {}
        self._voip_manager: Optional[SpixiVoIPSessionManager] = None
        self._connected_event = asyncio.Event()

    def attach_voip_manager(self, manager: SpixiVoIPSessionManager) -> None:
        """Bind a SpixiVoIPSessionManager to automatically handle MQTT VoIP lifecycle topics."""
        self._voip_manager = manager

    def on_topic(self, topic_prefix: str, callback: MQCallback) -> None:
        """Register an async callback for a QuIXI MQTT topic (e.g. 'AppProtocolData', 'ChatStream')."""
        self._topic_handlers.setdefault(topic_prefix, []).append(callback)

    async def start(self) -> None:
        if aiohttp is None:
            raise RuntimeError("aiohttp is required to start QuIXIClient")
        if mqtt is None:
            raise RuntimeError("paho-mqtt is required to start QuIXIClient")

        self._loop = asyncio.get_running_loop()
        timeout = aiohttp.ClientTimeout(total=15)
        self._http = aiohttp.ClientSession(timeout=timeout)

        # Compatible with both paho-mqtt v1.x and v2.x
        try:
            self._mqtt = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        except AttributeError:
            self._mqtt = mqtt.Client()

        if self.mqtt_username:
            self._mqtt.username_pw_set(self.mqtt_username, self.mqtt_password)

        self._mqtt.on_connect = self._on_mqtt_connect
        self._mqtt.on_message = self._on_mqtt_message
        self._mqtt.connect_async(self.mqtt_host, self.mqtt_port, keepalive=30)
        self._mqtt.loop_start()

    async def stop(self) -> None:
        if self._mqtt is not None:
            self._mqtt.loop_stop()
            self._mqtt.disconnect()
            self._mqtt = None
        if self._http is not None:
            await self._http.close()
            self._http = None

    def _on_mqtt_connect(self, client: Any, userdata: Any, flags: Any, rc: Any, props: Any = None) -> None:
        is_failure = getattr(rc, "is_failure", None)
        failed = bool(is_failure) if is_failure is not None else (rc != 0)
        if failed:
            logger.error("Failed to connect to QuIXI MQTT broker at %s:%d (rc=%s)", self.mqtt_host, self.mqtt_port, rc)
            return

        topics = [
            ("AppRequest/#", 0),
            ("AppRequestAccept/#", 0),
            ("AppRequestReject/#", 0),
            ("AppRequestError/#", 0),
            ("AppData/#", 0),
            ("AppEndSession/#", 0),
            ("AppProtocolData/#", 0),
            ("ChatStream/#", 0),
            ("Chat/#", 0),
            ("FriendStatusUpdate/#", 0),
        ]
        client.subscribe(topics)
        if self._loop and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._connected_event.set)
        logger.info("Connected to QuIXI MQTT broker at %s:%d", self.mqtt_host, self.mqtt_port)

    def _on_mqtt_message(self, client: Any, userdata: Any, msg: Any) -> None:
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except Exception as exc:
            logger.warning("Failed to parse MQTT JSON on %s: %s", msg.topic, exc)
            return

        if not isinstance(payload, dict):
            logger.warning(
                "Ignoring non-object MQTT JSON payload on %s: %s",
                msg.topic,
                type(payload).__name__,
            )
            return

        if self._loop is not None and not self._loop.is_closed():
            asyncio.run_coroutine_threadsafe(
                self._dispatch_mqtt_event(msg.topic, payload),
                self._loop,
            )

    async def _dispatch_mqtt_event(self, topic: str, payload: Dict[str, Any]) -> None:
        prefix = topic.split("/", 1)[0]
        sender = str(payload.get("sender", ""))

        # Route VoIP lifecycle topics to attached SpixiVoIPSessionManager
        if self._voip_manager is not None and prefix in {
            "AppRequest",
            "AppRequestAccept",
            "AppRequestReject",
            "AppRequestError",
            "AppData",
            "AppEndSession",
        }:
            try:
                raw_bytes = _extract_b64_bytes(payload)
                if prefix == "AppRequestError" and not raw_bytes:
                    app_msg = AppDataMessage(session_id=b"")
                else:
                    app_msg = unpack_app_data_message(raw_bytes)
                if prefix == "AppRequest":
                    await self._voip_manager.on_received_call(sender, app_msg)
                elif prefix == "AppRequestAccept":
                    await self._voip_manager.on_accepted_call(sender, app_msg)
                elif prefix == "AppRequestReject":
                    await self._voip_manager.on_rejected_call(app_msg)
                elif prefix == "AppRequestError":
                    await self._voip_manager.on_request_error(app_msg, sender)
                elif prefix == "AppData":
                    await self._voip_manager.on_data(sender, app_msg)
                elif prefix == "AppEndSession":
                    await self._voip_manager.on_hangup_call(app_msg)
            except Exception as exc:
                logger.warning("Failed handling VoIP MQTT message on %s: %s", topic, exc)

        # Invoke registered topic handlers
        for cb in self._topic_handlers.get(prefix, []):
            try:
                await cb(topic, payload)
            except Exception:
                logger.exception("Error in MQTT callback for topic %s", topic)

    async def _call_api(self, method: str, params: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        """Call QuIXI APIServer endpoint (all numeric/bool values passed as strings per APIServer.cs)."""
        if self._http is None:
            raise RuntimeError("QuIXIClient not started")

        url = f"{self.rest_url}/{method}"
        body = {"method": method, "params": params or {}}
        async with self._http.post(url, json=body) as resp:
            text = await resp.text()
            status = getattr(resp, "status", 200)
            data: Any = None
            parse_err: Optional[Exception] = None
            if text:
                try:
                    data = json.loads(text)
                except Exception as exc:
                    parse_err = exc

            if isinstance(status, int) and status >= 400:
                if isinstance(data, dict):
                    err_obj = data.get("error")
                    if err_obj is not None and err_obj is not False:
                        code = err_obj.get("code") if isinstance(err_obj, dict) else None
                        msg = (
                            err_obj.get("message", str(err_obj))
                            if isinstance(err_obj, dict)
                            else str(err_obj)
                        )
                        raise QuIXIError(
                            f"QuIXI {method} HTTP {status}: {msg}",
                            method=method,
                            code=code,
                            payload=data,
                        )
                snippet = text.strip()[:200] if text else ""
                raise QuIXIError(
                    f"QuIXI {method} HTTP {status}: {snippet}" if snippet else f"QuIXI {method} HTTP {status}",
                    method=method,
                    code=status,
                    payload=data if data is not None else text,
                )

            resp.raise_for_status()

            if not text:
                return {}
            if parse_err is not None:
                raise QuIXIError(
                    f"QuIXI {method} returned invalid JSON: {parse_err}",
                    method=method,
                    payload=text,
                ) from parse_err

            if isinstance(data, dict):
                err_obj = data.get("error")
                if err_obj is not None and err_obj is not False:
                    code = err_obj.get("code") if isinstance(err_obj, dict) else None
                    msg = (
                        err_obj.get("message", str(err_obj))
                        if isinstance(err_obj, dict)
                        else str(err_obj)
                    )
                    raise QuIXIError(
                        f"QuIXI {method} error: {msg}",
                        method=method,
                        code=code,
                        payload=data,
                    )
                return data

            return {"result": data}

    async def status(self) -> Dict[str, Any]:
        """Query QuIXI node status (/status)."""
        return await self._call_api("status")

    async def my_wallet(self) -> Dict[str, Any]:
        """Retrieve local QuIXI wallet addresses and balances (/mywallet)."""
        return await self._call_api("mywallet")

    async def get_balance(self, address: Optional[str] = None) -> Dict[str, Any]:
        """Retrieve total IXI balance (/gettotalbalance)."""
        params: Dict[str, str] = {"address": address} if address else {}
        return await self._call_api("gettotalbalance", params)

    async def generate_new_address(self, base_address: Optional[str] = None) -> Dict[str, Any]:
        """Generate a new Ixian wallet address/account (/generatenewaddress)."""
        params: Dict[str, str] = {"address": base_address} if base_address else {}
        return await self._call_api("generatenewaddress", params)

    async def view_wallet(self) -> Dict[str, Any]:
        """Inspect wallet details (/viewwallet)."""
        return await self._call_api("viewwallet")

    async def backup_wallet(self) -> Dict[str, Any]:
        """Export encrypted wallet backup (/backupwallet)."""
        return await self._call_api("backupwallet")

    async def get_nick(self) -> Dict[str, Any]:
        """Retrieve current local Spixi/QuIXI nickname (/getNick)."""
        return await self._call_api("getNick")

    async def set_nick(self, nick: str) -> Dict[str, Any]:
        """Set local Spixi/QuIXI nickname (/setNick)."""
        return await self._call_api("setNick", {"nick": nick})

    async def contacts(self, search: Optional[str] = None) -> Dict[str, Any]:
        params: Dict[str, str] = {"search": search} if search else {}
        return await self._call_api("contacts", params)

    async def add_contact(self, address: str) -> Dict[str, Any]:
        return await self._call_api("addContact", {"address": address})

    async def accept_contact(self, address: str) -> Dict[str, Any]:
        return await self._call_api("acceptContact", {"address": address})

    async def remove_contact(self, address: str) -> Dict[str, Any]:
        """Remove a contact from QuIXI (/removeContact)."""
        return await self._call_api("removeContact", {"address": address})

    async def call_rpc(self, method: str, params: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        """Invoke any QuIXI or Ixian-Core REST API method by name."""
        return await self._call_api(method.lstrip("/"), params)

    async def send_chat_message(self, address: str, message: str, channel: int = 0) -> Dict[str, Any]:
        return await self._call_api(
            "sendChatMessage",
            {"address": address, "message": message, "channel": str(channel)},
        )

    async def send_chat_stream_message(
        self,
        address: str,
        message: str,
        message_id_b64: Optional[str] = None,
        sequence: int = 0,
        is_stream: bool = True,
        channel: int = 0,
    ) -> Dict[str, Any]:
        if is_stream and not message_id_b64:
            message_id_b64 = base64.b64encode(uuid.uuid4().bytes).decode("ascii")
        params: Dict[str, str] = {
            "address": address,
            "message": message,
            "channel": str(channel),
            "isStream": "true" if is_stream else "false",
        }
        if message_id_b64:
            params["messageId"] = message_id_b64
            params["sequence"] = str(sequence)
        return await self._call_api("sendChatStreamMessage", params)

    async def send_spixi_message(
        self,
        address: str,
        msg_type: int,
        data_bytes: bytes,
        channel: int = 0,
    ) -> Dict[str, Any]:
        """
        Send raw SpixiMessage via QuIXI /sendSpixiMessage.
        Note: APIServer.cs uses Crypto.stringToHash((string)parameters["data"]),
        which parses a hex string into byte[].
        """
        return await self._call_api(
            "sendSpixiMessage",
            {
                "address": address,
                "type": str(int(msg_type)),
                "data": data_bytes.hex(),
                "channel": str(channel),
            },
        )

    async def send_app_data(
        self,
        address: str,
        data_str: str,
        protocol_id: str = "com.tamagci.agent",
    ) -> Dict[str, Any]:
        """Send structured JSON/string payload on an app protocol channel."""
        return await self._call_api(
            "sendAppData",
            {
                "address": address,
                "protocolId": protocol_id,
                "data": data_str,
            },
        )
