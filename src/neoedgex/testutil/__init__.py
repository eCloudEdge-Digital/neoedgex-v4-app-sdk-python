from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any, Final, Iterable

import cbor2

from neoedgex.contract import DataType, ErrorCode, Logger, Message, Node
from neoedgex.contract._float import float32_out_of_range
from neoedgex.contract._limits import MAX_UINT64, MIN_INT64
from neoedgex.contract.codec import _MAJOR_MAP, _encode_head, _encode_text, encode_field

_MESSAGE_SOURCE: Final = "upstream-node"
_MESSAGE_TIMESTAMP: Final = "2026-01-01T00:00:00Z"


class _Undeclared:
    __slots__ = ()

    def __repr__(self) -> str:
        return "testutil.UNDECLARED"


UNDECLARED: Final[_Undeclared] = _Undeclared()
"""Declared-type marker for a key the receiving node's input schema does not
declare. Such a key stays out of the decode plan and takes the bypass path:
it arrives in its natural CBOR domain (every float as a Python float decoded
at double width, integers as int) — what an upstream node's extra tag does in
production. Leaving the type unset is not the same thing and is rejected."""


@dataclass(slots=True, frozen=True)
class Single:
    """Wire value that CBOR-encodes as a single-precision float (0xfa) — the
    counterpart of writing float32(25.34) in a Go test, for reproducing the
    single-into-double wire cases. A bare Python float encodes as 0xfb."""

    value: float


class NoopLogger:
    def __init__(self, tag: str = "test") -> None:
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


@dataclass(slots=True)
class PublishedMessage:
    handle: str
    data: dict[str, Any]


@dataclass(slots=True)
class ReportedError:
    code: ErrorCode
    err: BaseException | None


def new_message(
    handle: str,
    fields: dict[str, tuple[Any, DataType | _Undeclared]],
    *,
    logger: Logger | None = None,
) -> Message:
    """Build the message a node handler receives on ``handle``, the way the
    runtime builds it. Each field maps a key to ``(wire value, declared
    type)``: the wire value is CBOR-encoded by its Python type (independent of
    the declared type, as the two schemas are independent in production), and
    the declared type feeds the decode plan — or is ``UNDECLARED`` for a key
    the input schema does not declare. The envelope carries source
    "upstream-node" and timestamp "2026-01-01T00:00:00Z"; assign
    ``msg.source`` / ``msg.timestamp`` after building to override.

    When the node configuration under test is available, prefer
    ``MockNodeEnv.new_message``: it reads the types out of the configured
    input schema instead of having them repeated here.
    """
    entries: list[tuple[str, bytes]] = []
    plan: dict[str, DataType] = {}
    for key, spec in fields.items():
        if not (isinstance(spec, tuple) and len(spec) == 2):
            raise TypeError(
                f"field {key!r}: expected a (wire value, declared type) tuple, got {spec!r}"
            )
        value, declared = spec
        entries.append((key, _wire_entry(key, value)))
        if declared is UNDECLARED:
            continue
        if not isinstance(declared, DataType) or not declared.is_supported():
            raise ValueError(f"field {key!r}: {_undeclarable_type_reason(declared)}")
        plan[key] = declared

    return _build_message(handle, entries, plan, logger if logger is not None else NoopLogger())


class MockNodeEnv:
    def __init__(
        self,
        *,
        config: Node | None = None,
        message_iterable: Iterable[Message] | None = None,
        done_event: threading.Event | None = None,
        mock_logger: Logger | None = None,
        publish_error: BaseException | None = None,
    ) -> None:
        self.config = config or Node()
        self.message_iterable = message_iterable or ()
        self.done_event = done_event or threading.Event()
        self.mock_logger = mock_logger or NoopLogger()
        self.publish_error = publish_error

        self.published_data: list[PublishedMessage] = []
        self.reported_errors: list[ReportedError] = []
        self.stop_called = False

    def node_config(self) -> Node:
        return self.config

    def messages(self) -> Iterable[Message]:
        return self.message_iterable

    def context(self) -> threading.Event:
        return self.done_event

    def logger(self) -> Logger:
        return self.mock_logger

    def new_message(
        self,
        handle: str,
        data: dict[str, Any],
        *,
        logger: Logger | None = None,
    ) -> Message:
        """Build a message for ``handle`` out of the input schema in
        ``self.config``, so the values need no types written out. ``data`` is
        the wire payload keyed by field key: a key the schema declares but
        data omits is delivered as None, like a value the upstream never
        produced; a key data carries but the schema does not declare takes
        the bypass path. The message carries this env's logger unless
        ``logger`` overrides it.
        """
        inputs = self.config.data.inputs
        if handle not in inputs:
            raise ValueError(
                f"handle {handle!r} is not declared in config.data.inputs "
                f"(declared: {sorted(inputs)}); to build a message for a handle "
                "the node does not declare, use testutil.new_message with "
                "UNDECLARED field types"
            )
        entries = [(key, _wire_entry(key, value)) for key, value in data.items()]
        plan = {schema.key: schema.type for schema in inputs[handle]}
        return _build_message(
            handle, entries, plan, logger if logger is not None else self.mock_logger
        )

    def publish(self, handle: str, data: dict[str, Any]) -> None:
        self.published_data.append(PublishedMessage(handle=handle, data=data))
        if self.publish_error is not None:
            raise self.publish_error

    def report_error(self, code: ErrorCode, err: BaseException | None) -> None:
        self.reported_errors.append(ReportedError(code=code, err=err))

    def stop(self) -> None:
        """Records the call only. Stop does not cancel the context (Go
        parity: MockNodeEnv.Stop leaves ctx.Err() nil); a test that needs
        the handler to observe cancellation must set done_event itself."""
        self.stop_called = True


def _build_message(
    handle: str,
    entries: list[tuple[str, bytes]],
    plan: dict[str, DataType],
    logger: Logger,
) -> Message:
    raw = bytearray(_encode_head(_MAJOR_MAP, len(entries)))
    for key, value_bytes in entries:
        raw += _encode_text(key)
        raw += value_bytes
    return Message(
        source=_MESSAGE_SOURCE,
        timestamp=_MESSAGE_TIMESTAMP,
        handle=handle,
        raw=bytes(raw),
        plan=plan,
        logger=logger,
    )


def _wire_entry(key: str, value: Any) -> bytes:
    if value is None:
        return encode_field(None, DataType.STRING)
    if isinstance(value, Single):
        wire = float(value.value)
        if float32_out_of_range(wire):
            raise ValueError(
                f"field {key!r}: Single value {wire!r} is out of float32 range and "
                f"cannot be encoded as a single-precision (0xfa) wire value; use a "
                f"bare float for a double-precision (0xfb) wire value"
            )
        return encode_field(wire, DataType.FLOAT)
    if isinstance(value, bool):
        return encode_field(value, DataType.BOOL)
    if isinstance(value, int):
        if not MIN_INT64 <= value <= MAX_UINT64:
            raise ValueError(
                f"field {key!r}: int value {value} has no CBOR integer encoding "
                f"in the message domain (allowed: -2**63 .. 2**64-1)"
            )
        return encode_field(value, DataType.INT64)
    if isinstance(value, float):
        return encode_field(value, DataType.DOUBLE)
    if isinstance(value, str):
        return encode_field(value, DataType.STRING)
    if isinstance(value, (bytes, bytearray)):
        return encode_field(bytes(value), DataType.RAW)
    # Any other value goes onto the wire as cbor2 encodes it (containers,
    # timezone-aware datetimes, ...) — the counterpart of Go testutil's
    # cbor.Marshal of an arbitrary value. On the schema path such a value
    # decodes to undefined.
    try:
        return cbor2.dumps(value)
    except Exception as exc:
        raise TypeError(
            f"field {key!r}: value of type {type(value).__name__!r} cannot be "
            f"CBOR-encoded as a wire value: {exc}"
        ) from exc


def _undeclarable_type_reason(declared: Any) -> str:
    if declared is DataType.UNDEFINED:
        return (
            "no declared type; set it to the DataType the receiving node's input "
            "schema declares for this key, or to testutil.UNDECLARED if the "
            "schema does not declare the key at all"
        )
    return f"declared type {declared!r} is not a declarable schema type"


__all__ = [
    "UNDECLARED",
    "MockNodeEnv",
    "NoopLogger",
    "PublishedMessage",
    "ReportedError",
    "Single",
    "new_message",
]
