from __future__ import annotations

import logging
import os
from typing import Any

_CONFIGURED = False

_LEVEL_MAP = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARN": logging.WARNING,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
}


def _ensure_logging() -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    raw = os.environ.get("NEOEDGEX_LOG_LEVEL", "").upper()
    # Default to DEBUG when no override is provided; NEOEDGEX_LOG_LEVEL still
    # wins when set to a recognized level name.
    level = _LEVEL_MAP.get(raw, logging.DEBUG)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    _CONFIGURED = True


class SDKLogger:
    def __init__(self, tag: str) -> None:
        _ensure_logging()
        self._tag = tag
        self._logger = logging.getLogger(f"neoedgex.{tag}")

    def tag(self) -> str:
        return self._tag

    def debug(self, msg: str, *args: Any) -> None:
        self._logger.debug(_render(msg, args))

    def info(self, msg: str, *args: Any) -> None:
        self._logger.info(_render(msg, args))

    def warn(self, msg: str, *args: Any) -> None:
        self._logger.warning(_render(msg, args))

    def error(self, msg: str, *args: Any) -> None:
        self._logger.error(_render(msg, args))


class NoopLogger:
    def __init__(self, tag: str = "") -> None:
        self._tag = tag

    def tag(self) -> str:
        return self._tag

    def debug(self, _msg: str, *_args: Any) -> None:
        return None

    def info(self, _msg: str, *_args: Any) -> None:
        return None

    def warn(self, _msg: str, *_args: Any) -> None:
        return None

    def error(self, _msg: str, *_args: Any) -> None:
        return None


def _render(msg: str, args: tuple[Any, ...]) -> str:
    if not args:
        return msg
    normalized = msg.replace("%v", "%s")
    try:
        return normalized % args
    except Exception:
        return f"{msg} {' '.join(str(arg) for arg in args)}"
