"""Shared Ixian Spixi VoIP & QuIXI protocol library for Pi Hub and AI Server."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .spixi_voip import (
    SpixiMessageCode,
    AppDataMessage,
    SpixiVoIPSessionManager,
    pack_app_data_message,
    unpack_app_data_message,
)

if TYPE_CHECKING:
    from .quixi_client import QuIXIClient, QuIXIError, is_quixi_ok

_LAZY_QUIXI_EXPORTS = frozenset({"QuIXIClient", "QuIXIError", "is_quixi_ok"})

__all__ = [
    "SpixiMessageCode",
    "AppDataMessage",
    "SpixiVoIPSessionManager",
    "pack_app_data_message",
    "unpack_app_data_message",
    "QuIXIClient",
    "QuIXIError",
    "is_quixi_ok",
]


def __getattr__(name: str) -> Any:
    if name in _LAZY_QUIXI_EXPORTS:
        from . import quixi_client

        return getattr(quixi_client, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(set(globals().keys()) | _LAZY_QUIXI_EXPORTS)
