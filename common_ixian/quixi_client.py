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
from typing import Any, Awaitable, Callable, Dict, Optional
from urllib.parse import urlencode

import aiohttp
import paho.mqtt.client as mqtt

from .spixi_voip import AppDataMessage, SpixiVoIPSessionManager, unpack_app_data_message

logger = logging.getLogger(__name__)

MQCallback = Callable[[str, Dict[str, Any]], Awaitable[None]]


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

    def _on_mqtt_connect(self, client: mqtt.Client, userdata: Any, flags: Any, rc: Any, props: Any = None) -> None:
        topics = [
            ("AppRequest/#", 0),
            ("AppRequestAccept/#", 0),
            ("AppRequestReject/#", 0),
            ("AppData/#", 0),
            ("AppEndSession/#", 0),
            ("AppProtocolData/#", 0),
            ("ChatStream/#", 0),
            ("Chat/#", 0),
            ("FriendStatusUpdate/#", 0),
        ]
        client.subscribe(topics)
        if self._loop:
            self._loop.call_soon_threadsafe(self._connected_event.set)
        logger.info("Connected to QuIXI MQTT broker at %s:%d", self.mqtt_host, self.mqtt_port)

    def _on_mqtt_message(self, client: mqtt.Client, userdata: Any, msg: mqtt.MQTTMessage) -> None:
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except Exception as exc:
            logger.warning("Failed to parse MQTT JSON on %s: %s", msg.topic, exc)
            return

        if self._loop is not None:
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
            "AppData",
            "AppEndSession",
        }:
            try:
                raw_bytes = _extract_b64_bytes(payload)
                app_msg: AppDataMessage = unpack_app_data_message(raw_bytes)
                if prefix == "AppRequest":
                    await self._voip_manager.on_received_call(sender, app_msg)
                elif prefix == "AppRequestAccept":
                    await self._voip_manager.on_accepted_call(sender, app_msg)
                elif prefix == "AppRequestReject":
                    await self._voip_manager.on_rejected_call(app_msg)
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
            resp.raise_for_status()
            text = await resp.text()
            return json.loads(text) if text else {}

    async def contacts(self) -> Dict[str, Any]:
        return await self._call_api("contacts")

    async def add_contact(self, address: str) -> Dict[str, Any]:
        return await self._call_api("addContact", {"address": address})

    async def accept_contact(self, address: str) -> Dict[str, Any]:
        return await self._call_api("acceptContact", {"address": address})

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
