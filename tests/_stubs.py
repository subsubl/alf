"""
Stdlib-only missing-dependency stubs and shared offline boundary test doubles.

Design rules:
1. Never overwrite real packages already installed in the active Python environment
   (e.g., aiohttp, paho-mqtt, pyserial, PyYAML in C:\\zephyrproject\\.venv).
2. Stub missing packages idempotently via importlib.util.find_spec(...) so the entire
   test suite runs identically under both system Python 3.12 (unittest) and .venv (pytest).
3. Provide a genuine pure-Python StubNdarray + numpy math functions so SpeechProcessor
   and AudioSyncEngine DSP math executes faithfully without numpy installed.
4. Provide shared boundary test doubles (FakeQuIXIBus, FakeHTTPSession, FakeMQTTClient,
   FakeSerialPort, MockESP32, FakeAsyncOpenAI) for 100% offline in-memory testing.
"""

from __future__ import annotations

import asyncio
import base64
import bisect
import importlib.util
import json
import math
import queue
import re
import struct
import sys
import threading
import time
import types
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple


REPO_ROOT = Path(__file__).resolve().parent.parent


def ensure_repo_root() -> Path:
    """Ensure the repository root is on sys.path for both unittest and pytest."""
    root_str = str(REPO_ROOT)
    if root_str not in sys.path:
        sys.path.insert(0, root_str)
    return REPO_ROOT


def _is_available(top_pkg: str) -> bool:
    """Return True if a top-level package is already imported or importable."""
    if top_pkg in sys.modules:
        return True
    try:
        return importlib.util.find_spec(top_pkg) is not None
    except (ModuleNotFoundError, ValueError):
        return False


# ---------------------------------------------------------------------------
# Pure-Python numpy math stub (only installed when real numpy is absent)
# ---------------------------------------------------------------------------

class _DType:
    def __init__(self, name: str) -> None:
        self.name = name

    def __eq__(self, other: object) -> bool:
        if isinstance(other, _DType):
            return self.name == other.name
        return self.name == other

    def __repr__(self) -> str:
        return f"dtype('{self.name}')"


_INT16_DTYPE = _DType("int16")
_FLOAT32_DTYPE = _DType("float32")


def _normalize_dtype(dtype: Any) -> _DType:
    if isinstance(dtype, _DType):
        return dtype
    s = str(dtype).lower()
    if "i2" in s or "int16" in s:
        return _INT16_DTYPE
    return _FLOAT32_DTYPE


def _to_int16(val: float) -> int:
    ival = int(val)
    return ((ival + 32768) & 0xFFFF) - 32768


class StubNdarray:
    """Minimal pure-Python ndarray supporting 1D and 2D int16/float32 DSP operations."""

    def __init__(
        self,
        data: Iterable[Any],
        dtype: Any = _FLOAT32_DTYPE,
        shape: Optional[Tuple[int, ...]] = None,
    ) -> None:
        self.dtype = _normalize_dtype(dtype)
        if self.dtype == _INT16_DTYPE:
            self._data: List[Any] = [_to_int16(x) for x in data]
        else:
            self._data = [float(x) for x in data]
        if shape is None:
            self._shape: Tuple[int, ...] = (len(self._data),)
        else:
            self._shape = tuple(int(d) for d in shape)

    @property
    def shape(self) -> Tuple[int, ...]:
        return self._shape

    @property
    def ndim(self) -> int:
        return len(self._shape)

    @property
    def size(self) -> int:
        return len(self._data)

    def __len__(self) -> int:
        return self._shape[0] if self._shape else 0

    def __iter__(self):
        if len(self._shape) <= 1:
            return iter(self._data)
        cols = self._shape[1]
        return (
            StubNdarray(self._data[r * cols : (r + 1) * cols], dtype=self.dtype)
            for r in range(self._shape[0])
        )

    def __getitem__(self, idx: Any) -> Any:
        if isinstance(idx, slice):
            if len(self._shape) <= 1:
                return StubNdarray(self._data[idx], dtype=self.dtype)
            rows = list(range(*idx.indices(self._shape[0])))
            cols = self._shape[1]
            flat: List[Any] = []
            for r in rows:
                flat.extend(self._data[r * cols : (r + 1) * cols])
            return StubNdarray(flat, dtype=self.dtype, shape=(len(rows), cols))
        if len(self._shape) <= 1:
            return self._data[idx]
        cols = self._shape[1]
        r = idx if idx >= 0 else self._shape[0] + idx
        if r < 0 or r >= self._shape[0]:
            raise IndexError("index out of bounds")
        return StubNdarray(self._data[r * cols : (r + 1) * cols], dtype=self.dtype)

    def __repr__(self) -> str:
        return f"StubNdarray({self._data[:8]!r}, shape={self._shape}, dtype={self.dtype.name})"

    def reshape(self, *dims: Any) -> "StubNdarray":
        if len(dims) == 1 and isinstance(dims[0], (tuple, list)):
            target = tuple(int(d) for d in dims[0])
        else:
            target = tuple(int(d) for d in dims)
        if not target or len(target) > 2:
            raise ValueError(f"Unsupported reshape target {target}")
        total = len(self._data)
        neg_indices = [i for i, d in enumerate(target) if d == -1]
        if len(neg_indices) > 1 or any(d < -1 for d in target):
            raise ValueError(f"Invalid reshape dimensions {target}")
        if neg_indices:
            known = 1
            for d in target:
                if d != -1:
                    known *= d
            if known <= 0 or total % known != 0:
                raise ValueError(f"Cannot reshape array of size {total} into shape {target}")
            resolved = list(target)
            resolved[neg_indices[0]] = total // known
            new_shape = tuple(resolved)
        else:
            prod = 1
            for d in target:
                prod *= d
            if prod != total:
                raise ValueError(f"Cannot reshape array of size {total} into shape {target}")
            new_shape = target
        return StubNdarray(self._data, dtype=self.dtype, shape=new_shape)

    def _check_compat(self, other: "StubNdarray") -> None:
        if len(self._data) != len(other._data) or self._shape != other._shape:
            raise ValueError(
                f"operands could not be broadcast together with shapes {self._shape} {other._shape}"
            )

    def _binary_op(self, other: Any, op: Callable[[float, float], float]) -> "StubNdarray":
        if isinstance(other, StubNdarray):
            self._check_compat(other)
            out = [op(a, b) for a, b in zip(self._data, other._data)]
            res_dtype = _FLOAT32_DTYPE if (self.dtype == _FLOAT32_DTYPE or other.dtype == _FLOAT32_DTYPE) else _INT16_DTYPE
        else:
            s = float(other)
            out = [op(a, s) for a in self._data]
            res_dtype = _FLOAT32_DTYPE if (self.dtype == _FLOAT32_DTYPE or isinstance(other, float)) else _INT16_DTYPE
        return StubNdarray(out, dtype=res_dtype, shape=self._shape)

    def __mul__(self, other: Any) -> "StubNdarray":
        return self._binary_op(other, lambda a, b: a * b)

    def __rmul__(self, other: Any) -> "StubNdarray":
        return self.__mul__(other)

    def __add__(self, other: Any) -> "StubNdarray":
        return self._binary_op(other, lambda a, b: a + b)

    def __radd__(self, other: Any) -> "StubNdarray":
        return self.__add__(other)

    def __sub__(self, other: Any) -> "StubNdarray":
        return self._binary_op(other, lambda a, b: a - b)

    def __rsub__(self, other: Any) -> "StubNdarray":
        if isinstance(other, StubNdarray):
            return other.__sub__(self)
        s = float(other)
        return StubNdarray([s - a for a in self._data], dtype=_FLOAT32_DTYPE, shape=self._shape)

    def __truediv__(self, other: Any) -> "StubNdarray":
        if isinstance(other, StubNdarray):
            self._check_compat(other)
            out = [a / b for a, b in zip(self._data, other._data)]
        else:
            s = float(other)
            out = [a / s for a in self._data]
        return StubNdarray(out, dtype=_FLOAT32_DTYPE, shape=self._shape)

    def __neg__(self) -> "StubNdarray":
        return StubNdarray([-x for x in self._data], dtype=self.dtype, shape=self._shape)

    def astype(self, dtype: _DType) -> "StubNdarray":
        return StubNdarray(self._data, dtype=dtype, shape=self._shape)

    def tobytes(self) -> bytes:
        if not self._data:
            return b""
        if self.dtype == _INT16_DTYPE:
            return struct.pack(f"<{len(self._data)}h", *self._data)
        return struct.pack(f"<{len(self._data)}f", *self._data)

    def tolist(self) -> List[Any]:
        if len(self._shape) <= 1:
            return list(self._data)
        rows, cols = self._shape
        return [list(self._data[r * cols : (r + 1) * cols]) for r in range(rows)]


def _np_frombuffer(buf: bytes, dtype: Any = _FLOAT32_DTYPE) -> StubNdarray:
    norm = _normalize_dtype(dtype)
    mv = memoryview(buf).cast("B")
    if norm == _INT16_DTYPE:
        if len(mv) % 2 != 0:
            raise ValueError("buffer size must be a multiple of element size (2 bytes for int16)")
        count = len(mv) // 2
        vals = struct.unpack(f"<{count}h", mv) if count else ()
        return StubNdarray(vals, dtype=_INT16_DTYPE)
    if len(mv) % 4 != 0:
        raise ValueError("buffer size must be a multiple of element size (4 bytes for float32)")
    count = len(mv) // 4
    vals = struct.unpack(f"<{count}f", mv) if count else ()
    return StubNdarray(vals, dtype=_FLOAT32_DTYPE)


def _np_linspace(start: float, stop: float, num: int = 50, endpoint: bool = True) -> StubNdarray:
    n = int(num)
    if n <= 0:
        return StubNdarray([], dtype=_FLOAT32_DTYPE)
    if n == 1:
        return StubNdarray([float(start)], dtype=_FLOAT32_DTYPE)
    denom = (n - 1) if endpoint else n
    step = (float(stop) - float(start)) / float(denom)
    return StubNdarray([float(start) + step * i for i in range(n)], dtype=_FLOAT32_DTYPE)


def _np_interp(x: Iterable[float], xp: Sequence[float] | StubNdarray, fp: Sequence[float] | StubNdarray) -> StubNdarray:
    xp_list = xp.tolist() if isinstance(xp, StubNdarray) else list(xp)
    fp_list = fp.tolist() if isinstance(fp, StubNdarray) else list(fp)
    if not xp_list or len(xp_list) != len(fp_list):
        raise ValueError("xp and fp must be non-empty and equal length")
    x_list = x.tolist() if isinstance(x, StubNdarray) else list(x)
    out: List[float] = []
    n = len(xp_list)
    for xi in x_list:
        if xi <= xp_list[0]:
            out.append(float(fp_list[0]))
        elif xi >= xp_list[-1]:
            out.append(float(fp_list[-1]))
        else:
            idx = bisect.bisect_right(xp_list, xi) - 1
            idx = max(0, min(idx, n - 2))
            x0, x1 = xp_list[idx], xp_list[idx + 1]
            y0, y1 = fp_list[idx], fp_list[idx + 1]
            if x1 == x0:
                out.append(float(y0))
            else:
                t = (xi - x0) / (x1 - x0)
                out.append(float(y0 + t * (y1 - y0)))
    return StubNdarray(out, dtype=_FLOAT32_DTYPE)


def _np_clip(a: Any, a_min: float, a_max: float) -> Any:
    if isinstance(a, StubNdarray):
        return StubNdarray([min(a_max, max(a_min, x)) for x in a._data], dtype=a.dtype, shape=a._shape)
    return min(a_max, max(a_min, a))


def _np_sqrt(a: Any) -> Any:
    if isinstance(a, StubNdarray):
        return StubNdarray([math.sqrt(x) for x in a._data], dtype=_FLOAT32_DTYPE, shape=a._shape)
    return math.sqrt(float(a))


def _np_mean(a: Any, axis: Optional[int] = None) -> Any:
    if axis is not None and isinstance(a, StubNdarray) and len(a._shape) == 2:
        rows, cols = a._shape
        if axis in (1, -1):
            if rows == 0 or cols == 0:
                return StubNdarray([], dtype=_FLOAT32_DTYPE)
            return StubNdarray(
                [sum(a._data[r * cols : (r + 1) * cols]) / float(cols) for r in range(rows)],
                dtype=_FLOAT32_DTYPE,
            )
        if axis == 0:
            if rows == 0 or cols == 0:
                return StubNdarray([], dtype=_FLOAT32_DTYPE)
            return StubNdarray(
                [sum(a._data[r * cols + c] for r in range(rows)) / float(rows) for c in range(cols)],
                dtype=_FLOAT32_DTYPE,
            )
        raise ValueError(f"axis {axis} is out of bounds for array of dimension {len(a._shape)}")
    vals = a._data if isinstance(a, StubNdarray) else list(a)
    if not vals:
        return 0.0
    return float(sum(vals) / len(vals))


def _np_round(a: Any, decimals: int = 0) -> Any:
    if isinstance(a, StubNdarray):
        rounded = [float(round(x, decimals)) if decimals else float(round(x)) for x in a._data]
        return StubNdarray(rounded, dtype=a.dtype, shape=a._shape)
    return round(a, decimals) if decimals else round(a)


def _np_abs(a: Any) -> Any:
    if isinstance(a, StubNdarray):
        return StubNdarray([abs(x) for x in a._data], dtype=a.dtype, shape=a._shape)
    return abs(a)


def _np_zeros(shape: int | Tuple[int, ...], dtype: _DType = _FLOAT32_DTYPE) -> StubNdarray:
    n = shape[0] if isinstance(shape, tuple) else int(shape)
    return StubNdarray([0] * n, dtype=dtype)


# ---------------------------------------------------------------------------
# Pure-Python minimal YAML subset parser (handles pi_hub/config.yaml cleanly)
# ---------------------------------------------------------------------------

def _parse_yaml_scalar(raw: str) -> Any:
    val = raw.strip()
    if not val or val in {"null", "Null", "NULL", "~"}:
        return None
    if val in {"true", "True", "TRUE"}:
        return True
    if val in {"false", "False", "FALSE"}:
        return False
    if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
        return val[1:-1]
    try:
        return int(val)
    except ValueError:
        pass
    try:
        return float(val)
    except ValueError:
        pass
    return val


def _strip_yaml_comment(line: str) -> str:
    in_single = False
    in_double = False
    for idx, ch in enumerate(line):
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == "#" and not in_single and not in_double:
            if idx == 0 or line[idx - 1].isspace():
                return line[:idx]
    return line


def _stub_yaml_safe_load(stream: Any) -> Any:
    text = stream.read() if hasattr(stream, "read") else str(stream)
    root: Dict[str, Any] = {}
    stack: List[Tuple[int, Dict[str, Any]]] = [(-1, root)]
    for raw_line in text.splitlines():
        cleaned = _strip_yaml_comment(raw_line).rstrip()
        if not cleaned.strip():
            continue
        indent = len(cleaned) - len(cleaned.lstrip(" "))
        stripped = cleaned.strip()
        if ":" not in stripped:
            continue
        key, rest = stripped.split(":", 1)
        key = key.strip()
        rest = rest.strip()
        while len(stack) > 1 and indent <= stack[-1][0]:
            stack.pop()
        parent = stack[-1][1]
        if rest == "":
            child: Dict[str, Any] = {}
            parent[key] = child
            stack.append((indent, child))
        else:
            parent[key] = _parse_yaml_scalar(rest)
    return root


# ---------------------------------------------------------------------------
# Missing-only third-party module installation
# ---------------------------------------------------------------------------

def install_dependency_stubs() -> Dict[str, bool]:
    """
    Install lightweight stdlib stubs ONLY for missing external packages.
    Returns a dict mapping package name -> True if stubbed, False if real package present.
    """
    stubbed: Dict[str, bool] = {}

    # 1. aiohttp
    if not _is_available("aiohttp"):
        aiohttp_mod = types.ModuleType("aiohttp")

        @dataclass
        class ClientTimeout:
            total: Optional[float] = None
            connect: Optional[float] = None
            sock_connect: Optional[float] = None
            sock_read: Optional[float] = None

        class ClientError(Exception):
            pass

        class ClientResponseError(ClientError):
            def __init__(
                self,
                request_info: Any = None,
                history: Any = (),
                *,
                status: int = 500,
                message: str = "",
                headers: Any = None,
            ) -> None:
                super().__init__(f"{status}, message='{message}'")
                self.request_info = request_info
                self.history = history
                self.status = status
                self.message = message
                self.headers = headers

        class ClientSession:
            def __init__(self, *args: Any, timeout: Any = None, **kwargs: Any) -> None:
                self.timeout = timeout
                self.closed = False

            async def close(self) -> None:
                self.closed = True

            def post(self, url: str, json: Any = None, **kwargs: Any) -> Any:
                raise NotImplementedError("ClientSession.post must be mocked in offline tests")

        aiohttp_mod.ClientTimeout = ClientTimeout
        aiohttp_mod.ClientError = ClientError
        aiohttp_mod.ClientResponseError = ClientResponseError
        aiohttp_mod.ClientSession = ClientSession
        sys.modules["aiohttp"] = aiohttp_mod
        stubbed["aiohttp"] = True
    else:
        stubbed["aiohttp"] = False

    # 2. paho.mqtt.client
    if not _is_available("paho"):
        paho_mod = types.ModuleType("paho")
        paho_mod.__path__ = []
        mqtt_pkg = types.ModuleType("paho.mqtt")
        mqtt_pkg.__path__ = []
        client_mod = types.ModuleType("paho.mqtt.client")

        class CallbackAPIVersion(Enum):
            VERSION1 = 1
            VERSION2 = 2

        class MQTTMessage:
            def __init__(self, topic: str = "", payload: bytes = b"", qos: int = 0) -> None:
                self.topic = topic
                self.payload = payload
                self.qos = qos

        class Client:
            def __init__(self, callback_api_version: Any = None, *args: Any, **kwargs: Any) -> None:
                self.callback_api_version = callback_api_version
                self.on_connect = None
                self.on_message = None
                self.on_disconnect = None

            def username_pw_set(self, username: str, password: Optional[str] = None) -> None:
                self._auth = (username, password)

            def connect_async(self, host: str, port: int = 1883, keepalive: int = 60) -> None:
                self._connect_target = (host, port, keepalive)

            def loop_start(self) -> None:
                pass

            def loop_stop(self) -> None:
                pass

            def disconnect(self) -> None:
                pass

            def subscribe(self, topic: Any, qos: int = 0) -> Tuple[int, int]:
                return (0, 1)

        client_mod.CallbackAPIVersion = CallbackAPIVersion
        client_mod.MQTTMessage = MQTTMessage
        client_mod.Client = Client
        mqtt_pkg.client = client_mod
        paho_mod.mqtt = mqtt_pkg

        sys.modules["paho"] = paho_mod
        sys.modules["paho.mqtt"] = mqtt_pkg
        sys.modules["paho.mqtt.client"] = client_mod
        stubbed["paho"] = True
    else:
        stubbed["paho"] = False

    # 3. serial (pyserial)
    if not _is_available("serial"):
        serial_mod = types.ModuleType("serial")

        class SerialException(OSError):
            pass

        class Serial:
            def __init__(self, port: Optional[str] = None, baudrate: int = 9600, timeout: Optional[float] = None, **kwargs: Any) -> None:
                self.port = port
                self.baudrate = baudrate
                self.timeout = timeout
                self.is_open = False

            def __enter__(self) -> "Serial":
                self.is_open = True
                return self

            def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
                self.close()

            def open(self) -> None:
                self.is_open = True

            def close(self) -> None:
                self.is_open = False

            def readline(self) -> bytes:
                return b""

            def write(self, data: bytes) -> int:
                return len(data)

            def flush(self) -> None:
                pass

        serial_mod.SerialException = SerialException
        serial_mod.Serial = Serial
        sys.modules["serial"] = serial_mod
        stubbed["serial"] = True
    else:
        stubbed["serial"] = False

    # 4. yaml (PyYAML)
    if not _is_available("yaml"):
        yaml_mod = types.ModuleType("yaml")
        yaml_mod.safe_load = _stub_yaml_safe_load
        sys.modules["yaml"] = yaml_mod
        stubbed["yaml"] = True
    else:
        stubbed["yaml"] = False

    # 5. numpy
    if not _is_available("numpy"):
        np_mod = types.ModuleType("numpy")
        np_mod.int16 = _INT16_DTYPE
        np_mod.float32 = _FLOAT32_DTYPE
        np_mod.ndarray = StubNdarray
        np_mod.frombuffer = _np_frombuffer
        np_mod.linspace = _np_linspace
        np_mod.interp = _np_interp
        np_mod.clip = _np_clip
        np_mod.sqrt = _np_sqrt
        np_mod.mean = _np_mean
        np_mod.round = _np_round
        np_mod.abs = _np_abs
        np_mod.zeros = _np_zeros
        sys.modules["numpy"] = np_mod
        stubbed["numpy"] = True
    else:
        stubbed["numpy"] = False

    # 6. openai
    if not _is_available("openai"):
        openai_mod = types.ModuleType("openai")

        class _UnconfiguredEndpoint:
            async def create(self, *args: Any, **kwargs: Any) -> Any:
                raise NotImplementedError("AsyncOpenAI endpoint must be mocked in offline tests")

        class AsyncOpenAI:
            def __init__(self, base_url: Optional[str] = None, api_key: Optional[str] = None, **kwargs: Any) -> None:
                self.base_url = base_url
                self.api_key = api_key
                self.audio = types.SimpleNamespace(
                    transcriptions=_UnconfiguredEndpoint(),
                    speech=_UnconfiguredEndpoint(),
                )
                self.chat = types.SimpleNamespace(
                    completions=_UnconfiguredEndpoint(),
                )

        openai_mod.AsyncOpenAI = AsyncOpenAI
        sys.modules["openai"] = openai_mod
        stubbed["openai"] = True
    else:
        stubbed["openai"] = False

    # 7. dotenv
    if not _is_available("dotenv"):
        dotenv_mod = types.ModuleType("dotenv")
        dotenv_mod.load_dotenv = lambda *args, **kwargs: False
        sys.modules["dotenv"] = dotenv_mod
        stubbed["dotenv"] = True
    else:
        stubbed["dotenv"] = False

    # 8. sounddevice
    sd_working = False
    if "sounddevice" in sys.modules:
        sd_working = hasattr(sys.modules["sounddevice"], "RawInputStream")
    elif _is_available("sounddevice"):
        try:
            mod = importlib.import_module("sounddevice")
            sd_working = hasattr(mod, "RawInputStream")
        except Exception:
            sd_working = False
            sys.modules.pop("sounddevice", None)

    if not sd_working:
        sd_mod = types.ModuleType("sounddevice")

        class _StubStream:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                self.kwargs = kwargs
                self.started = False
                self.closed = False

            def start(self) -> None:
                self.started = True

            def stop(self) -> None:
                self.started = False

            def close(self) -> None:
                self.closed = True

        sd_mod.RawInputStream = _StubStream
        sd_mod.RawOutputStream = _StubStream
        sys.modules["sounddevice"] = sd_mod
        stubbed["sounddevice"] = True
    else:
        stubbed["sounddevice"] = False

    # 9. webrtcvad
    if not _is_available("webrtcvad"):
        vad_mod = types.ModuleType("webrtcvad")

        class Vad:
            def __init__(self, mode: int = 2) -> None:
                self.mode = mode

            def is_speech(self, buf: bytes, sample_rate: int) -> bool:
                return False

        vad_mod.Vad = Vad
        sys.modules["webrtcvad"] = vad_mod
        stubbed["webrtcvad"] = True
    else:
        stubbed["webrtcvad"] = False

    return stubbed


# ---------------------------------------------------------------------------
# Shared Offline Boundary Test Doubles (No real TCP/MQTT/Serial/OpenAI)
# ---------------------------------------------------------------------------

class FakeHTTPResponse:
    """Async context-manager HTTP response compatible with aiohttp ClientResponse."""

    def __init__(
        self,
        status: int = 200,
        text_body: str = "{}",
        raise_on_status: Optional[Exception] = None,
    ) -> None:
        self.status = status
        self._text_body = text_body
        self._raise_on_status = raise_on_status

    async def __aenter__(self) -> "FakeHTTPResponse":
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        return None

    def raise_for_status(self) -> None:
        if self._raise_on_status is not None:
            raise self._raise_on_status
        if self.status >= 400:
            import aiohttp
            raise aiohttp.ClientResponseError(
                types.SimpleNamespace(real_url="http://127.0.0.1:8001"),
                (),
                status=self.status,
                message=f"HTTP {self.status}",
            )

    async def text(self) -> str:
        return self._text_body


class FakeHTTPSession:
    """In-memory aiohttp.ClientSession stand-in."""

    def __init__(
        self,
        post_handler: Optional[Callable[[str, Any], FakeHTTPResponse]] = None,
    ) -> None:
        self.calls: List[Tuple[str, Any]] = []
        self.closed = False
        self._post_handler = post_handler

    def post(self, url: str, json: Any = None, **kwargs: Any) -> FakeHTTPResponse:
        self.calls.append((url, json))
        if self._post_handler is not None:
            return self._post_handler(url, json)
        return FakeHTTPResponse(200, '{"result": {}}')

    async def close(self) -> None:
        self.closed = True


class FakeMQTTClient:
    """In-memory paho.mqtt.client.Client stand-in (no background threads or sockets)."""

    def __init__(self, callback_api_version: Any = None, *args: Any, **kwargs: Any) -> None:
        self.callback_api_version = callback_api_version
        self.on_connect = None
        self.on_message = None
        self.on_disconnect = None
        self.subscriptions: List[Any] = []
        self.connect_args: Optional[Tuple[str, int, int]] = None
        self.credentials: Optional[Tuple[str, Optional[str]]] = None
        self.loop_started = False
        self.loop_stopped = False
        self.disconnected = False

    def username_pw_set(self, username: str, password: Optional[str] = None) -> None:
        self.credentials = (username, password)

    def connect_async(self, host: str, port: int = 1883, keepalive: int = 60) -> None:
        self.connect_args = (host, port, keepalive)

    def loop_start(self) -> None:
        self.loop_started = True

    def loop_stop(self) -> None:
        self.loop_stopped = True

    def disconnect(self) -> None:
        self.disconnected = True

    def subscribe(self, topic: Any, qos: int = 0) -> Tuple[int, int]:
        if isinstance(topic, list):
            self.subscriptions.extend(topic)
        else:
            self.subscriptions.append((topic, qos))
        return (0, 1)

    def emit_connect(self) -> None:
        if self.on_connect:
            self.on_connect(self, None, None, 0)

    def emit_message(self, topic: str, payload: Any) -> None:
        if self.on_message:
            raw = payload if isinstance(payload, (bytes, bytearray)) else json.dumps(payload).encode("utf-8")
            msg = types.SimpleNamespace(topic=topic, payload=bytes(raw), qos=0)
            self.on_message(self, None, msg)


class FakeQuIXIBus:
    """
    In-memory QuIXI REST + Mosquitto MQTT router connecting peer QuIXIClient nodes.
    Mirrors QuIXI APIServer.cs (/sendSpixiMessage, /sendAppData, /sendChatStreamMessage,
    /sendChatMessage, /contacts, /addContact, /acceptContact) and JsonStreamMessageSerializer.
    """

    def __init__(self) -> None:
        self.nodes: Dict[str, Any] = {}
        self.mqtt_clients: Dict[str, FakeMQTTClient] = {}
        self.http_sessions: Dict[str, FakeHTTPSession] = {}
        self.contacts: Dict[str, List[str]] = {}
        self.rest_log: List[Tuple[str, str, Dict[str, Any]]] = []
        self.mqtt_log: List[Tuple[str, str, Dict[str, Any]]] = []
        self.forced_http_errors: Dict[Tuple[str, str], Tuple[int, str]] = {}
        self.drop_topics: Dict[str, set[str]] = {}

    def register(self, address: str, client: Any) -> Tuple[FakeHTTPSession, FakeMQTTClient]:
        """Attach a QuIXIClient instance to this in-memory QuIXI node."""
        self.nodes[address] = client
        self.contacts.setdefault(address, [])
        mqtt_cli = FakeMQTTClient()
        http_sess = FakeHTTPSession(post_handler=lambda url, body: self._handle_post(address, url, body))
        self.mqtt_clients[address] = mqtt_cli
        self.http_sessions[address] = http_sess
        return http_sess, mqtt_cli

    def force_http_error(self, src_addr: str, method: str, status: int = 500, body: str = "") -> None:
        self.forced_http_errors[(src_addr, method)] = (status, body)

    def drop_next_topic(self, dst_addr: str, prefix: str) -> None:
        self.drop_topics.setdefault(dst_addr, set()).add(prefix)

    @staticmethod
    def _topic_matches(sub_filter: str, topic: str) -> bool:
        if sub_filter == "#" or sub_filter == topic:
            return True
        if sub_filter.endswith("/#"):
            base = sub_filter[:-2]
            return topic == base or topic.startswith(base + "/")
        sub_parts = sub_filter.split("/")
        top_parts = topic.split("/")
        if len(sub_parts) != len(top_parts):
            return False
        return all(sp == "+" or sp == tp for sp, tp in zip(sub_parts, top_parts))

    def deliver_mqtt(self, dst_addr: str, topic: str, payload: Dict[str, Any]) -> None:
        prefix = topic.split("/", 1)[0]
        if prefix in self.drop_topics.get(dst_addr, set()):
            self.drop_topics[dst_addr].discard(prefix)
            return
        mqtt_cli = self.mqtt_clients.get(dst_addr)
        if mqtt_cli and mqtt_cli.subscriptions:
            matched = any(
                self._topic_matches(t[0] if isinstance(t, tuple) else str(t), topic)
                for t in mqtt_cli.subscriptions
            )
            if not matched:
                return
        self.mqtt_log.append((dst_addr, topic, payload))
        if mqtt_cli and mqtt_cli.on_message:
            mqtt_cli.emit_message(topic, payload)
        else:
            node = self.nodes.get(dst_addr)
            if node and node._loop:
                asyncio.run_coroutine_threadsafe(node._dispatch_mqtt_event(topic, payload), node._loop)

    def _handle_post(self, src_addr: str, url: str, body: Dict[str, Any]) -> FakeHTTPResponse:
        method = url.rstrip("/").rsplit("/", 1)[-1]
        params = dict((body or {}).get("params", {}))
        self.rest_log.append((src_addr, method, params))

        if (src_addr, method) in self.forced_http_errors:
            status, err_body = self.forced_http_errors.pop((src_addr, method))
            return FakeHTTPResponse(status=status, text_body=err_body)

        if method == "status":
            return FakeHTTPResponse(
                200,
                json.dumps(
                    {
                        "result": {
                            "nodeStatus": "Running",
                            "networkType": "mainnet",
                            "blockHeight": 4201337,
                            "dltStatus": "Connected",
                            "s2Status": "Connected",
                            "address": src_addr,
                        }
                    }
                ),
            )

        if method in {"mywallet", "myWallet", "viewwallet"}:
            extra_addrs = getattr(self, "_generated_addrs", {}).get(src_addr, [])
            wallet_map = {src_addr: "100.00000000"}
            for a in extra_addrs:
                wallet_map[a] = "0.00000000"
            return FakeHTTPResponse(200, json.dumps({"result": wallet_map}))

        if method == "gettotalbalance":
            return FakeHTTPResponse(200, json.dumps({"result": "100.00000000"}))

        if method == "generatenewaddress":
            if not hasattr(self, "_generated_addrs"):
                self._generated_addrs = {}
            lst = self._generated_addrs.setdefault(src_addr, [])
            new_addr = f"{src_addr}_sub{len(lst) + 1}"
            lst.append(new_addr)
            return FakeHTTPResponse(200, json.dumps({"result": new_addr}))

        if method == "backupwallet":
            return FakeHTTPResponse(200, json.dumps({"result": f"IXI_WALLET_BACKUP_{src_addr}"}))

        if method == "getNick":
            nick = getattr(self, "_nicks", {}).get(src_addr, "Alf-Pi-Hub")
            return FakeHTTPResponse(200, json.dumps({"result": nick}))

        if method == "setNick":
            if not hasattr(self, "_nicks"):
                self._nicks = {}
            self._nicks[src_addr] = params.get("nick", "Alf-Pi-Hub")
            return FakeHTTPResponse(200, json.dumps({"result": self._nicks[src_addr]}))

        if method == "contacts":
            q = (params.get("search") or "").strip().lower()
            items = self.contacts.get(src_addr, [])
            if q:
                items = [c for c in items if q in str(c).lower()]
            return FakeHTTPResponse(200, json.dumps({"result": items}))

        if method in {"addContact", "acceptContact"}:
            peer = params.get("address", "")
            if peer and peer not in self.contacts.setdefault(src_addr, []):
                self.contacts[src_addr].append(peer)
            return FakeHTTPResponse(200, json.dumps({"result": {"address": peer}}))

        if method == "removeContact":
            peer = params.get("address", "")
            if peer in self.contacts.get(src_addr, []):
                self.contacts[src_addr].remove(peer)
            return FakeHTTPResponse(200, json.dumps({"result": {"removed": peer}}))

        if method == "sendSpixiMessage":
            dst = params["address"]
            msg_type = int(params["type"])
            raw_bytes = bytes.fromhex(params["data"])
            b64 = base64.b64encode(raw_bytes).decode("ascii")
            topic_map = {
                21: "AppData",
                22: "AppRequest",
                28: "AppRequestAccept",
                29: "AppRequestReject",
                30: "AppRequestError",
                31: "AppEndSession",
                45: "AppProtocolData",
                53: "ChatStream",
                0: "Chat",
            }
            topic_prefix = topic_map.get(msg_type, "AppData")
            envelope = {
                "sender": src_addr,
                "recipient": dst,
                "data": {"type": msg_type, "data": b64, "channel": int(params.get("channel", "0"))},
                "timestamp": int(time.time()),
            }
            self.deliver_mqtt(dst, f"{topic_prefix}/{src_addr}", envelope)
            return FakeHTTPResponse(200, json.dumps({"result": "sent"}))

        if method == "sendAppData":
            dst = params["address"]
            b64 = base64.b64encode(params["data"].encode("utf-8")).decode("ascii")
            envelope = {
                "sender": src_addr,
                "recipient": dst,
                "data": {
                    "type": 45,
                    "protocolId": params.get("protocolId", "com.tamagci.agent"),
                    "data": b64,
                },
            }
            self.deliver_mqtt(dst, f"AppProtocolData/{src_addr}", envelope)
            return FakeHTTPResponse(200, json.dumps({"result": "sent"}))

        if method == "sendChatStreamMessage":
            dst = params["address"]
            b64 = base64.b64encode(params["message"].encode("utf-8")).decode("ascii")
            envelope = {
                "sender": src_addr,
                "recipient": dst,
                "data": {
                    "type": 53,
                    "data": b64,
                    "isStream": params.get("isStream", "true"),
                    "sequence": int(params.get("sequence", "0")),
                    "messageId": params.get("messageId"),
                },
            }
            self.deliver_mqtt(dst, f"ChatStream/{src_addr}", envelope)
            return FakeHTTPResponse(200, json.dumps({"result": "sent"}))

        if method == "sendChatMessage":
            dst = params["address"]
            b64 = base64.b64encode(params["message"].encode("utf-8")).decode("ascii")
            envelope = {
                "sender": src_addr,
                "recipient": dst,
                "data": {"type": 0, "data": b64, "channel": int(params.get("channel", "0"))},
            }
            self.deliver_mqtt(dst, f"Chat/{src_addr}", envelope)
            return FakeHTTPResponse(200, json.dumps({"result": "sent"}))

        return FakeHTTPResponse(200, json.dumps({"result": {}}))


class FakeSerialPort:
    """In-memory serial.Serial stand-in backed by thread-safe queues and optional MockESP32."""

    def __init__(
        self,
        port: str = "/dev/ttyACM0",
        baudrate: int = 921600,
        timeout: float = 0.2,
        esp: Optional["MockESP32"] = None,
    ) -> None:
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.dtr = False
        self.rts = False
        self.esp = esp
        self.is_open = False
        self.rx_queue: queue.Queue[bytes] = queue.Queue()
        self.written: List[bytes] = []
        self.fail_on_read_exc: Optional[Exception] = None
        if esp is not None:
            esp.attach_port(self)

    def __enter__(self) -> "FakeSerialPort":
        self.is_open = True
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    def open(self) -> None:
        self.is_open = True

    def close(self) -> None:
        self.is_open = False
        self.rx_queue.put(b"")

    def reset_input_buffer(self) -> None:
        while not self.rx_queue.empty():
            try:
                self.rx_queue.get_nowait()
            except queue.Empty:
                break

    def push_rx_line(self, line: str | bytes) -> None:
        raw = line.encode("utf-8") if isinstance(line, str) else line
        if not raw.endswith(b"\n"):
            raw += b"\n"
        self.rx_queue.put(raw)

    def readline(self) -> bytes:
        if not self.is_open:
            return b""
        if self.fail_on_read_exc is not None:
            exc = self.fail_on_read_exc
            self.fail_on_read_exc = None
            raise exc
        try:
            return self.rx_queue.get(timeout=0.03)
        except queue.Empty:
            return b""

    def write(self, data: bytes) -> int:
        self.written.append(bytes(data))
        if self.esp is not None:
            self.esp.handle_host_bytes(bytes(data), self)
        return len(data)

    def flush(self) -> None:
        pass


class _TrackedPinDict(dict):
    """Dict that tracks pins explicitly forced/written externally (e.g. esp.gpio_vals[15] = 0)."""

    def __init__(self, initial: Dict[int, int]) -> None:
        super().__init__(initial)
        self.external: set[int] = set()

    def __setitem__(self, key: int, value: int) -> None:
        self.external.add(key)
        super().__setitem__(key, value)

    def set_internal(self, key: int, value: int) -> None:
        super().__setitem__(key, value)


def _safe_int(val: Any) -> Optional[int]:
    if isinstance(val, bool) or val is None:
        return None
    if isinstance(val, int):
        return val
    return None


def _truncate_utf8_bytes(s: str, max_bytes: int = 191) -> str:
    raw = s.encode("utf-8", errors="replace")
    if len(raw) <= max_bytes:
        return raw.decode("utf-8")
    cut = max_bytes
    while cut > 0 and (raw[cut] & 0xC0) == 0x80:
        cut -= 1
    return raw[:cut].decode("utf-8")


def _sanitize_json_id(raw_id: Any) -> str:
    if not isinstance(raw_id, str):
        return ""
    out = [c for c in raw_id if 0x20 <= ord(c) <= 0x7E and c not in {'"', "\\"}]
    return "".join(out)[:31]


def _parse_key_level_json_object(line: str) -> Optional[Dict[str, Any]]:
    """Mirror serial_proto.c key-level scanner when full json.loads fails on malformed tokens."""
    s = line.strip()
    if len(s) < 2 or not s.startswith("{") or not s.endswith("}"):
        return None
    cmd: Dict[str, Any] = {}
    for m in re.finditer(r'"([A-Za-z0-9_]+)"\s*:\s*"((?:\\.|[^"\\])*)"', s):
        k = m.group(1)
        try:
            cmd[k] = json.loads(f'"{m.group(2)}"')
        except json.JSONDecodeError:
            cmd[k] = m.group(2)
    for m in re.finditer(r'"([A-Za-z0-9_]+)"\s*:\s*([^,"}\s]+)\s*(?=[,}])', s):
        k, raw_v = m.group(1), m.group(2)
        if k in cmd:
            continue
        if re.fullmatch(r"-?\d+", raw_v):
            cmd[k] = int(raw_v)
        else:
            cmd[k] = raw_v
    if not isinstance(cmd.get("t"), str):
        return None
    return cmd


class MockESP32:
    """
    Faithful in-memory simulation of esp32_face_os Zephyr firmware NDJSON protocol.
    Enforces RX_LINE_MAX=512, the 10-pin allowlist, protected-pin -EACCES (-13),
    pin<0 / non-int -EINVAL (-22), 32-entry BLE table with 30s prune, QMI8658 IMU state,
    4-step orientation, and UI state.
    """

    RX_LINE_MAX = 512
    ALLOWED_PINS = {15, 16, 17, 18, 21, 33, 34, 35, 36, 37}
    PROTECTED_PINS = set(range(0, 15)) | {43, 44}

    def __init__(self) -> None:
        self.screen = "face"
        self.emo = "IDLE"
        self.sub = ""
        self.mouth = 0
        self.mic = 0
        self.bright = 80
        self.vol = 75
        self.mic_mode = "vad"
        self.ble_en = 1
        self.sleep_s = 120
        self.angle = 0
        self.net_ip = "waiting DHCP..."
        self.net_ixi = "waiting QuIXI wallet..."
        self.dbg: Dict[str, Any] = {"quixi": "OFF", "voip": "IDLE", "srv_ms": 0, "bt_spk": "OK"}
        self.gpio_dirs: Dict[int, str] = {}
        self.gpio_pulls: Dict[int, str] = {}
        self.gpio_vals: _TrackedPinDict = _TrackedPinDict({p: 0 for p in self.ALLOWED_PINS})
        self.vbat_mv = 4110
        self.ble_table: List[Dict[str, Any]] = [
            {"mac": "AA:BB:CC:DD:EE:01", "type": 0, "name": "KeyFob", "rssi": -55, "ts": time.time()},
            {"mac": "AA:BB:CC:DD:EE:02", "type": 1, "name": "Thermostat", "rssi": -82, "ts": time.time()},
        ]
        self.imu: Dict[str, Any] = {
            "ax": 0.02,
            "ay": -0.01,
            "az": 1.00,
            "gx": 0.1,
            "gy": -0.2,
            "gz": 0.0,
            "temp": 31.4,
            "gest": "none",
        }
        self.rx_history: List[Dict[str, Any]] = []
        self.ports: List[FakeSerialPort] = []
        self._buf = bytearray()
        self._uptime_ms = 1000
        self.media_visible = False
        self.media_w = 0
        self.media_h = 0
        self.media_frames: List[bytes] = []
        self._bin_remaining = 0
        self._bin_buf = bytearray()
        self._bin_w = 120
        self._bin_h = 120
        self._bin_id = ""

    def attach_port(self, port: FakeSerialPort) -> None:
        if port not in self.ports:
            self.ports.append(port)

    def emit_event(self, evt: Dict[str, Any]) -> None:
        pkt = {"t": "evt", **evt}
        line = json.dumps(pkt, separators=(",", ":"))
        for port in list(self.ports):
            if port.is_open:
                port.push_rx_line(line)

    def handle_host_bytes(self, data: bytes, port: FakeSerialPort) -> None:
        self._buf.extend(data)
        while True:
            if self._bin_remaining > 0:
                take = min(self._bin_remaining, len(self._buf))
                if take <= 0:
                    break
                self._bin_buf.extend(self._buf[:take])
                del self._buf[:take]
                self._bin_remaining -= take
                if self._bin_remaining == 0:
                    self.media_visible = True
                    self.media_w = self._bin_w
                    self.media_h = self._bin_h
                    self.media_frames.append(bytes(self._bin_buf))
                    self._bin_buf.clear()
                    if self._bin_id:
                        self._send_resp(port, {"t": "res", "id": self._bin_id, "ok": 1, "shown": 1})
                        self._bin_id = ""
                continue

            if b"\n" not in self._buf:
                break
            raw_line, _, rest = self._buf.partition(b"\n")
            self._buf = bytearray(rest)
            raw_clean = raw_line.rstrip(b"\r")
            if len(raw_clean) >= self.RX_LINE_MAX:
                continue
            line = raw_clean.decode("utf-8", errors="replace").strip(" \t")
            if len(line) < 2 or not line.startswith("{") or not line.endswith("}"):
                continue
            try:
                cmd = json.loads(line)
            except json.JSONDecodeError:
                cmd = _parse_key_level_json_object(line)
                if cmd is None:
                    continue
            if not isinstance(cmd, dict):
                continue
            self.rx_history.append(cmd)
            self._dispatch_cmd(cmd, port)

    def _send_resp(self, port: FakeSerialPort, resp: Dict[str, Any]) -> None:
        port.push_rx_line(json.dumps(resp, separators=(",", ":"), ensure_ascii=False))

    def _dispatch_cmd(self, cmd: Dict[str, Any], port: FakeSerialPort) -> None:
        t = cmd.get("t")
        if not isinstance(t, str):
            return
        req_id = _sanitize_json_id(cmd.get("id"))

        if t == "ping":
            self._uptime_ms += 10
            self._send_resp(port, {"t": "res", "id": req_id, "ok": 1, "up": self._uptime_ms})

        elif t == "state":
            if "emo" in cmd and isinstance(cmd["emo"], str):
                self.emo = cmd["emo"]
            if "sub" in cmd and isinstance(cmd["sub"], str):
                self.sub = _truncate_utf8_bytes(cmd["sub"], 191)
            mic = _safe_int(cmd.get("mic"))
            if mic is not None and mic >= 0:
                self.mic = mic

        elif t == "sync":
            if "emo" in cmd and isinstance(cmd["emo"], str):
                self.emo = cmd["emo"]
            if "sub" in cmd and isinstance(cmd["sub"], str):
                self.sub = _truncate_utf8_bytes(cmd["sub"], 191)
            mouth = _safe_int(cmd.get("mouth"))
            if mouth is not None and mouth >= 0:
                self.mouth = max(0, min(100, mouth))

        elif t == "mouth":
            v = _safe_int(cmd.get("v"))
            if v is not None and v >= 0:
                self.mouth = max(0, min(100, v))

        elif t == "img_bin":
            w = _safe_int(cmd.get("w")) or 120
            h = _safe_int(cmd.get("h")) or 120
            length = _safe_int(cmd.get("len")) or 0
            if w <= 0 or w > 120 or h <= 0 or h > 120 or length <= 0 or length != w * h * 2:
                self._send_resp(port, {"t": "res", "id": req_id, "ok": 0, "err": -22})
            else:
                self._bin_w = w
                self._bin_h = h
                self._bin_id = req_id
                self._bin_buf.clear()
                self._bin_remaining = length
                self._send_resp(port, {"t": "res", "id": req_id, "ok": 1, "ready": 1})

        elif t == "img_clr":
            self._bin_remaining = 0
            self._bin_buf.clear()
            self.media_visible = False
            if req_id:
                self._send_resp(port, {"t": "res", "id": req_id, "ok": 1})

        elif t == "dbg":
            for k in ("quixi", "voip", "bt_spk"):
                if k in cmd and isinstance(cmd[k], str) and cmd[k]:
                    self.dbg[k] = cmd[k]
            srv_ms = _safe_int(cmd.get("srv_ms"))
            if srv_ms is not None:
                self.dbg["srv_ms"] = srv_ms if srv_ms >= 0 else -1
            if "ip" in cmd and isinstance(cmd["ip"], str) and cmd["ip"]:
                self.net_ip = cmd["ip"][:47]
            if "ixi" in cmd and isinstance(cmd["ixi"], str) and cmd["ixi"]:
                self.net_ixi = cmd["ixi"][:95]

        elif t == "net":
            if "ip" in cmd and isinstance(cmd["ip"], str) and cmd["ip"]:
                self.net_ip = cmd["ip"][:47]
            ixi_val = cmd.get("ixi") if isinstance(cmd.get("ixi"), str) else cmd.get("addr")
            if isinstance(ixi_val, str) and ixi_val:
                self.net_ixi = ixi_val[:95]
            if req_id:
                self._send_resp(port, {"t": "res", "id": req_id, "ok": 1})

        elif t == "cfg":
            bright = _safe_int(cmd.get("bright"))
            if bright is not None:
                self.bright = max(10, min(100, bright))
            vol = _safe_int(cmd.get("vol"))
            if vol is not None:
                self.vol = max(0, min(100, vol))
            if "mode" in cmd and isinstance(cmd["mode"], str):
                self.mic_mode = cmd["mode"]
            ble_en = _safe_int(cmd.get("ble_en"))
            if ble_en is not None:
                self.ble_en = 1 if ble_en != 0 else 0
            sleep_s = _safe_int(cmd.get("sleep_s"))
            if sleep_s is not None:
                self.sleep_s = sleep_s
            if req_id:
                self._send_resp(port, {"t": "res", "id": req_id, "ok": 1, "bright": self.bright})

        elif t == "gpio_cfg":
            pin = _safe_int(cmd.get("pin"))
            has_dir = "dir" in cmd
            has_pull = "pull" in cmd
            direction = cmd.get("dir") if isinstance(cmd.get("dir"), str) else ("in" if not has_dir else "")
            pull = cmd.get("pull") if isinstance(cmd.get("pull"), str) else ("none" if not has_pull else "")
            if pin is None or pin < 0 or direction not in ("in", "out") or pull not in ("none", "up", "down"):
                self._send_resp(
                    port,
                    {"t": "res", "id": req_id, "ok": 0, "pin": -1 if pin is None else pin, "err": -22},
                )
            elif pin not in self.ALLOWED_PINS:
                self._send_resp(port, {"t": "res", "id": req_id, "ok": 0, "pin": pin, "err": -13})
            else:
                raw_val = _safe_int(cmd.get("val"))
                val = 0 if raw_val is None else raw_val
                self.gpio_dirs[pin] = direction
                self.gpio_pulls[pin] = pull
                if direction == "out":
                    self.gpio_vals.set_internal(pin, 1 if val else 0)
                elif direction == "in" and pin not in self.gpio_vals.external:
                    if pull == "up":
                        self.gpio_vals.set_internal(pin, 1)
                    elif pull == "down":
                        self.gpio_vals.set_internal(pin, 0)
                self._send_resp(port, {"t": "res", "id": req_id, "ok": 1, "pin": pin, "err": 0})

        elif t == "gpio_set":
            pin = _safe_int(cmd.get("pin"))
            raw_val = _safe_int(cmd.get("val"))
            val = 1 if (raw_val is not None and raw_val != 0) else 0
            if pin is None or pin < 0 or raw_val is None:
                self._send_resp(
                    port,
                    {"t": "res", "id": req_id, "ok": 0, "pin": -1 if pin is None else pin, "val": val, "err": -22},
                )
            elif pin not in self.ALLOWED_PINS:
                self._send_resp(port, {"t": "res", "id": req_id, "ok": 0, "pin": pin, "val": val, "err": -13})
            else:
                self.gpio_dirs[pin] = "out"
                self.gpio_vals.set_internal(pin, val)
                self._send_resp(port, {"t": "res", "id": req_id, "ok": 1, "pin": pin, "val": val, "err": 0})

        elif t == "gpio_get":
            pin = _safe_int(cmd.get("pin"))
            if pin is None or pin < 0:
                self._send_resp(
                    port,
                    {
                        "t": "res",
                        "id": req_id,
                        "ok": 0,
                        "pin": -1 if pin is None else pin,
                        "val": 0,
                        "vbat_mv": self.vbat_mv,
                        "err": -22,
                    },
                )
            elif pin not in self.ALLOWED_PINS:
                self._send_resp(
                    port,
                    {"t": "res", "id": req_id, "ok": 0, "pin": pin, "val": 0, "vbat_mv": self.vbat_mv, "err": -13},
                )
            else:
                val = self.gpio_vals.get(pin, 0)
                self._send_resp(
                    port,
                    {"t": "res", "id": req_id, "ok": 1, "pin": pin, "val": val, "vbat_mv": self.vbat_mv, "err": 0},
                )

        elif t == "ble_get":
            now = time.time()
            active = [
                {k: v for k, v in d.items() if k != "ts"}
                for d in self.ble_table
                if now - d.get("ts", now) <= 30.0
            ][:32]
            self._send_resp(port, {"t": "res", "id": req_id, "ok": 1, "cnt": len(active), "ble": active})

        elif t == "imu_get":
            self._send_resp(
                port,
                {
                    "t": "res",
                    "id": req_id,
                    "ok": 1,
                    **self.imu,
                    "angle": self.angle,
                },
            )


def make_pcm16_sine(freq_hz: float = 440.0, duration_s: float = 0.25, sample_rate: int = 24000, amp: int = 12000) -> bytes:
    """Generate deterministic 16-bit signed mono PCM sine wave bytes."""
    n = int(round(duration_s * sample_rate))
    samples = [int(amp * math.sin(2.0 * math.pi * freq_hz * i / sample_rate)) for i in range(n)]
    return struct.pack(f"<{len(samples)}h", *samples)


class FakeAsyncOpenAI:
    """In-memory AsyncOpenAI double for STT, TTS, and Chat Completions."""

    def __init__(self) -> None:
        self.stt_calls: List[Dict[str, Any]] = []
        self.tts_calls: List[Dict[str, Any]] = []
        self.chat_calls: List[Dict[str, Any]] = []

        self.stt_script: List[Any] = []
        self.tts_script: List[Any] = []
        self.chat_script: List[Any] = []

        self.default_transcript = "Hello companion"
        self.default_tts_pcm = make_pcm16_sine(440.0, 0.25, 24000, 12000)
        self.default_reply = "Hello there!"

        outer = self

        class _Transcriptions:
            async def create(self, **kwargs: Any) -> Any:
                outer.stt_calls.append(kwargs)
                if outer.stt_script:
                    item = outer.stt_script.pop(0)
                    if isinstance(item, Exception):
                        raise item
                    if callable(item):
                        return item(kwargs)
                    return types.SimpleNamespace(text=item)
                return types.SimpleNamespace(text=outer.default_transcript)

        class _Speech:
            async def create(self, **kwargs: Any) -> Any:
                outer.tts_calls.append(kwargs)
                if outer.tts_script:
                    item = outer.tts_script.pop(0)
                    if isinstance(item, Exception):
                        raise item
                    raw = item(kwargs) if callable(item) else item
                else:
                    raw = outer.default_tts_pcm
                return types.SimpleNamespace(read=lambda: raw)

        class _Completions:
            async def create(self, **kwargs: Any) -> Any:
                outer.chat_calls.append(json.loads(json.dumps(kwargs, default=str)))
                if outer.chat_script:
                    item = outer.chat_script.pop(0)
                    if isinstance(item, Exception):
                        raise item
                    msg = item(kwargs) if callable(item) else item
                else:
                    msg = types.SimpleNamespace(content=outer.default_reply, tool_calls=None)
                return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)])

        self.audio = types.SimpleNamespace(transcriptions=_Transcriptions(), speech=_Speech())
        self.chat = types.SimpleNamespace(completions=_Completions())


def make_tool_call(call_id: str, name: str, args: Dict[str, Any] | str) -> Any:
    """Build an OpenAI-compatible tool_call object for scripted FakeAsyncOpenAI replies."""
    raw_args = args if isinstance(args, str) else json.dumps(args)
    return types.SimpleNamespace(
        id=call_id,
        type="function",
        function=types.SimpleNamespace(name=name, arguments=raw_args),
    )
