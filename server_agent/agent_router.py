"""OpenAI-compatible LLM Agent Brain with dynamic ESP32-S3 / Pi hardware tool calling."""

from __future__ import annotations

import json
import logging
from typing import Any, Awaitable, Callable, Dict, List

from openai import AsyncOpenAI

logger = logging.getLogger(__name__)

HardwareRpcFn = Callable[[str, Dict[str, Any]], Awaitable[Dict[str, Any]]]

AGENT_TOOLS: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "set_companion_emotion",
            "description": "Change the animated face expression and status subtitle on the Waveshare ESP32-S3 LCD.",
            "parameters": {
                "type": "object",
                "properties": {
                    "emotion": {
                        "type": "string",
                        "enum": [
                            "IDLE",
                            "LISTENING",
                            "THINKING",
                            "SPEAKING",
                            "HAPPY",
                            "SAD",
                            "SURPRISED",
                            "SLEEPING",
                            "ALERT",
                        ],
                    },
                    "subtitle": {"type": "string", "description": "Short text shown on screen."},
                },
                "required": ["emotion"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "gpio_configure_and_write",
            "description": (
                "Dynamically configure a free ESP32-S3 header GPIO pin (allowlist: 15, 16, 17, 18, 21, 33, 34, 35, 36, 37) "
                "and write a digital output state (0=LOW, 1=HIGH)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "pin": {"type": "integer", "enum": [15, 16, 17, 18, 21, 33, 34, 35, 36, 37]},
                    "mode": {"type": "string", "enum": ["out", "in"]},
                    "state": {"type": "integer", "enum": [0, 1]},
                },
                "required": ["pin", "mode", "state"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "gpio_read_pin",
            "description": "Read a free ESP32-S3 header GPIO digital input state and current battery voltage (mV).",
            "parameters": {
                "type": "object",
                "properties": {
                    "pin": {"type": "integer", "enum": [15, 16, 17, 18, 21, 33, 34, 35, 36, 37]},
                    "pull": {"type": "string", "enum": ["none", "up", "down"]},
                },
                "required": ["pin"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "scan_ble_environment",
            "description": "Query the ESP32-S3 BLE passive observer for nearby Bluetooth Low Energy devices and RSSI signal strengths.",
            "parameters": {
                "type": "object",
                "properties": {
                    "min_rssi": {
                        "type": "integer",
                        "description": "Filter out devices weaker than this RSSI in dBm (e.g. -85).",
                    }
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_imu_motion",
            "description": "Read the ESP32-S3 QMI8658 6-axis accelerometer, gyroscope, die temperature, and last detected gesture (shake/tilt).",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_companion_settings",
            "description": "Update screen brightness, Bluetooth speaker volume, or microphone capture mode (vad/ptt).",
            "parameters": {
                "type": "object",
                "properties": {
                    "brightness": {"type": "integer", "minimum": 10, "maximum": 100},
                    "volume": {"type": "integer", "minimum": 0, "maximum": 100},
                    "mic_mode": {"type": "string", "enum": ["vad", "ptt"]},
                },
            },
        },
    },
]


class AgentBrain:
    """Manages LLM dialogue history and iterative OpenAI tool-calling against remote hardware."""

    def __init__(
        self,
        llm_base_url: str,
        llm_api_key: str,
        llm_model: str,
        companion_name: str,
        rpc_call_fn: HardwareRpcFn,
        max_history_messages: int = 24,
    ) -> None:
        self.client = AsyncOpenAI(base_url=llm_base_url, api_key=llm_api_key)
        self.model = llm_model
        self.rpc_call = rpc_call_fn
        self.max_history = max_history_messages
        self.last_emotion = "HAPPY"

        self.system_prompt = (
            f"You are {companion_name}, an embodied AI companion running inside a round Waveshare ESP32-S3 "
            "face display tethered via USB to a Raspberry Pi Zero 2 W and Bluetooth speaker/microphone. "
            "Keep spoken replies concise, conversational, and natural for voice output (usually 1-3 short sentences). "
            "You can express emotions on your face and interact with physical GPIO pins, QMI8658 motion/IMU sensors, "
            "and nearby BLE devices using your tools."
        )
        self.history: List[Dict[str, Any]] = []

    def _trim_history(self) -> None:
        if len(self.history) > self.max_history:
            self.history = self.history[-self.max_history :]

    async def process_user_turn(self, user_text: str) -> tuple[str, str]:
        """Run user turn through LLM + hardware tool calls and return (reply_text, emotion)."""
        self.history.append({"role": "user", "content": user_text})
        self._trim_history()
        self.last_emotion = "HAPPY"

        for _ in range(6):  # max 6 tool-call rounds per turn
            messages = [{"role": "system", "content": self.system_prompt}, *self.history]
            resp = await self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                tools=AGENT_TOOLS,
                tool_choice="auto",
            )
            msg = resp.choices[0].message

            if not msg.tool_calls:
                reply = (msg.content or "").strip()
                self.history.append({"role": "assistant", "content": reply})
                self._trim_history()
                return reply, self.last_emotion

            # Append assistant tool call request to history
            assistant_entry: Dict[str, Any] = {
                "role": "assistant",
                "content": msg.content or "",
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in msg.tool_calls
                ],
            }
            self.history.append(assistant_entry)

            # Execute each tool call remotely via QuIXI -> Pi -> ESP32-S3
            for tc in msg.tool_calls:
                fn_name = tc.function.name
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}

                if fn_name == "set_companion_emotion" and "emotion" in args:
                    self.last_emotion = str(args["emotion"]).upper()

                logger.info("Executing agent tool %s(%s)", fn_name, args)
                try:
                    result = await self.rpc_call(fn_name, args)
                except Exception as exc:
                    result = {"ok": False, "error": str(exc)}

                self.history.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": json.dumps(result),
                    }
                )

        fallback = "I ran into too many hardware steps. Could you try asking again?"
        self.history.append({"role": "assistant", "content": fallback})
        return fallback, "SAD"
