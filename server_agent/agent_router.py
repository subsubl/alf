"""OpenAI-compatible LLM Agent Brain with dynamic ESP32-S3 / Pi hardware tool calling."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, Awaitable, Callable, Dict, List, Optional

try:
    from openai import AsyncOpenAI
except ImportError:
    AsyncOpenAI = None  # type: ignore[assignment,misc]

logger = logging.getLogger(__name__)

HardwareRpcFn = Callable[[str, Dict[str, Any]], Awaitable[Dict[str, Any]]]
HardwareRpcBatchFn = Callable[[List[Dict[str, Any]]], Awaitable[List[Dict[str, Any]]]]

VALID_EMOTIONS = (
    "IDLE",
    "LISTENING",
    "THINKING",
    "SPEAKING",
    "HAPPY",
    "SAD",
    "SURPRISED",
    "SLEEPING",
    "ALERT",
)
_VALID_EMOTION_SET = frozenset(VALID_EMOTIONS)
_EMO_TAG_RE = re.compile(r"\[EMO\s*:\s*([A-Za-z_]+)\s*\]", re.IGNORECASE)


def extract_emotion_tag(text: str, default: str = "HAPPY") -> tuple[str, str]:
    """Extract a valid [EMO:...] tag if present and strip all [EMO:...] tags from text."""
    emotion = default if default in _VALID_EMOTION_SET else "HAPPY"
    for match in _EMO_TAG_RE.finditer(text or ""):
        cand = match.group(1).strip().upper()
        if cand in _VALID_EMOTION_SET:
            emotion = cand
    cleaned = _EMO_TAG_RE.sub("", text or "")
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned).strip()
    return cleaned, emotion

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
                    "subtitle": {
                        "type": "string",
                        "description": "Short text shown on screen (up to ~180 chars / 2-3 lines).",
                    },
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
                "as output or input, with optional pull resistor and digital output state (0=LOW, 1=HIGH)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "pin": {"type": "integer", "enum": [15, 16, 17, 18, 21, 33, 34, 35, 36, 37]},
                    "mode": {"type": "string", "enum": ["out", "in"], "default": "out"},
                    "state": {"type": "integer", "enum": [0, 1], "default": 0},
                    "pull": {
                        "type": "string",
                        "enum": ["none", "up", "down"],
                        "default": "none",
                        "description": "Optional internal pull resistor.",
                    },
                },
                "required": ["pin"],
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
                    "pull": {
                        "type": "string",
                        "enum": ["none", "up", "down"],
                        "default": "none",
                        "description": "Internal pull resistor configuration for reading.",
                    },
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
                        "minimum": -127,
                        "maximum": 0,
                        "description": "Filter out devices weaker than this RSSI in dBm (e.g. -85).",
                    }
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_imu_motion",
            "description": (
                "Read the ESP32-S3 QMI8658 6-axis accelerometer, gyroscope, die temperature, "
                "last detected gesture (none/shake/tilt/pickup/double_tap), and orientation angle (0/90/180/270)."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_companion_settings",
            "description": (
                "Update screen brightness, Bluetooth speaker volume, microphone capture mode (vad/ptt/duplex/wake), "
                "BLE scanner toggle, sleep timeout, or inspect/reset/reflash the ESP32-S3 LCD firmware over USB."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "brightness": {"type": "integer", "minimum": 10, "maximum": 100},
                    "volume": {"type": "integer", "minimum": 0, "maximum": 100},
                    "mic_mode": {"type": "string", "enum": ["vad", "ptt", "duplex", "wake"]},
                    "ble_enabled": {"type": "boolean"},
                    "sleep_timeout_s": {"type": "integer", "enum": [0, 30, 120]},
                    "firmware_action": {
                        "type": "string",
                        "enum": ["check", "flash", "reset"],
                        "description": "Inspect staged ESP32 firmware ('check'), reflash ESP32-S3 LCD over USB ('flash'), or hardware-reset ESP32-S3 ('reset').",
                    },
                    "firmware_source": {
                        "type": "string",
                        "description": "Optional firmware path, HTTPS URL, or 'latest' staged zephyr.bin when firmware_action='flash'.",
                    },
                    "firmware_sha256": {
                        "type": "string",
                        "description": "Optional expected SHA-256 hex digest of zephyr.bin before flashing.",
                    },
                },
            },
        },
    },
]


ALLOWLIST_PINS: List[int] = [15, 16, 17, 18, 21, 33, 34, 35, 36, 37]


def build_default_system_prompt(companion_name: str = "Tamagci") -> str:
    return (
        f"You are {companion_name}, an embodied AI companion running inside a round Waveshare ESP32-S3 "
        "face display tethered via USB to a Raspberry Pi Zero 2 W and Bluetooth speaker/microphone. "
        "Keep spoken replies concise, conversational, and natural for voice output (usually 1-3 short sentences). "
        "You can express emotions on your face (via set_companion_emotion or optional [EMO:HAPPY|SAD|SURPRISED|ALERT|SLEEPING|IDLE] tags), "
        "interact with physical GPIO pins, QMI8658 motion/IMU sensors, and nearby BLE devices, "
        "and inspect, hardware-reset, or reflash/update your ESP32-S3 LCD firmware via update_companion_settings."
    )


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
        client: Any = None,
        rpc_batch_fn: Optional[HardwareRpcBatchFn] = None,
    ) -> None:
        if client is not None:
            self.client = client
        else:
            if AsyncOpenAI is None:
                raise RuntimeError("openai package is required when no client is provided")
            self.client = AsyncOpenAI(base_url=llm_base_url, api_key=llm_api_key)
        self.model = llm_model
        self.companion_name = companion_name
        self.rpc_call = rpc_call_fn
        self.rpc_batch = rpc_batch_fn
        self.max_history = max(0, int(max_history_messages))
        self.last_emotion = "HAPPY"
        self.last_turn_tool_calls: List[Dict[str, Any]] = []

        self.system_prompt = build_default_system_prompt(companion_name)
        self.history: List[Dict[str, Any]] = []

    def reset(self) -> None:
        """Clear dialogue history and reset current emotion."""
        self.history.clear()
        self.last_turn_tool_calls.clear()
        self.last_emotion = "HAPPY"

    def _trim_history(self) -> None:
        if self.max_history <= 0:
            self.history.clear()
            return

        n = len(self.history)
        start = 0
        if n > self.max_history:
            min_start = n - self.max_history
            user_start = next(
                (i for i in range(min_start, n) if self.history[i].get("role") == "user"),
                None,
            )
            start = user_start if user_start is not None else min_start

        # Never leave an orphaned tool response at index 0
        while start < n and self.history[start].get("role") == "tool":
            start += 1

        if start > 0:
            self.history = self.history[start:]

    async def process_user_turn(self, user_text: str) -> tuple[str, str]:
        """Run user turn through LLM + hardware tool calls and return (reply_text, emotion)."""
        self.history.append({"role": "user", "content": user_text})
        self._trim_history()
        self.last_emotion = "HAPPY"
        self.last_turn_tool_calls = []

        for _ in range(6):  # max 6 tool-call rounds per turn
            messages = [{"role": "system", "content": self.system_prompt}, *self.history]
            try:
                resp = await self.client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    tools=AGENT_TOOLS,
                    tool_choice="auto",
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("LLM chat completion failed: %s", exc)
                fallback = "Sorry, I had trouble reaching my brain just now."
                self.history.append({"role": "assistant", "content": fallback})
                self._trim_history()
                self.last_emotion = "SAD"
                return fallback, "SAD"

            if not resp or not getattr(resp, "choices", None):
                fallback = "I didn't receive a clear response. Could you try again?"
                self.history.append({"role": "assistant", "content": fallback})
                self._trim_history()
                self.last_emotion = "SAD"
                return fallback, "SAD"

            msg = resp.choices[0].message

            if not getattr(msg, "tool_calls", None):
                raw_reply = (getattr(msg, "content", None) or "").strip()
                reply, emotion = extract_emotion_tag(raw_reply, default=self.last_emotion)
                self.last_emotion = emotion
                self.history.append({"role": "assistant", "content": reply})
                self._trim_history()
                return reply, self.last_emotion

            # Append assistant tool call request to history
            assistant_entry: Dict[str, Any] = {
                "role": "assistant",
                "content": getattr(msg, "content", None) or "",
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

            # Parse tool arguments and validate JSON object structure first
            parsed_calls: List[Dict[str, Any]] = []
            valid_indices: List[int] = []
            results_by_idx: Dict[int, Dict[str, Any]] = {}

            for idx, tc in enumerate(msg.tool_calls):
                fn_name = tc.function.name
                raw_args = tc.function.arguments or "{}"
                try:
                    args = json.loads(raw_args)
                except json.JSONDecodeError as exc:
                    results_by_idx[idx] = {
                        "ok": False,
                        "error": f"Invalid JSON arguments for {fn_name}: {exc}",
                    }
                    parsed_calls.append({"id": tc.id, "tool": fn_name, "args": None})
                    continue

                if not isinstance(args, dict):
                    results_by_idx[idx] = {
                        "ok": False,
                        "error": f"Arguments for {fn_name} must be a JSON object",
                    }
                    parsed_calls.append({"id": tc.id, "tool": fn_name, "args": None})
                    continue

                if fn_name == "set_companion_emotion" and "emotion" in args:
                    cand = str(args["emotion"]).strip().upper()
                    if cand in _VALID_EMOTION_SET:
                        self.last_emotion = cand

                parsed_calls.append({"id": tc.id, "tool": fn_name, "args": args})
                valid_indices.append(idx)

            # Execute via rpc_batch_fn when multiple valid calls are issued, else rpc_call
            if len(valid_indices) > 1 and self.rpc_batch is not None:
                batch_payload = [parsed_calls[i] for i in valid_indices]
                logger.info("Executing batched agent tools (%d calls)", len(batch_payload))
                try:
                    batch_results = await self.rpc_batch(batch_payload)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    batch_results = [{"ok": False, "error": str(exc)} for _ in valid_indices]

                for pos, idx in enumerate(valid_indices):
                    if pos < len(batch_results) and isinstance(batch_results[pos], dict):
                        results_by_idx[idx] = batch_results[pos]
                    else:
                        results_by_idx[idx] = {"ok": False, "error": "Missing batch tool result"}
            else:
                for idx in valid_indices:
                    call_spec = parsed_calls[idx]
                    fn_name = call_spec["tool"]
                    args = call_spec["args"]
                    logger.info("Executing agent tool %s(%s)", fn_name, args)
                    try:
                        results_by_idx[idx] = await self.rpc_call(fn_name, args)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        results_by_idx[idx] = {"ok": False, "error": str(exc)}

            for idx, tc in enumerate(msg.tool_calls):
                result = results_by_idx.get(idx, {"ok": False, "error": "Tool execution skipped"})
                call_info = parsed_calls[idx] if idx < len(parsed_calls) else {"id": tc.id, "tool": tc.function.name, "args": {}}
                self.last_turn_tool_calls.append(
                    {
                        "id": tc.id,
                        "tool": call_info["tool"],
                        "args": call_info["args"],
                        "result": result,
                    }
                )
                self.history.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": json.dumps(result, default=str),
                    }
                )

        fallback = "I ran into too many hardware steps. Could you try asking again?"
        self.history.append({"role": "assistant", "content": fallback})
        self._trim_history()
        self.last_emotion = "SAD"
        return fallback, "SAD"

    run_turn = process_user_turn
