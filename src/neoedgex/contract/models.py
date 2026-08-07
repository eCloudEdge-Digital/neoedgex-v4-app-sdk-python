from __future__ import annotations

import dataclasses
import threading
from dataclasses import dataclass, field
from types import UnionType
from typing import Any, Protocol, Union, get_args, get_origin, get_type_hints, runtime_checkable

from .codec import (
    decode_declared,
    decode_field_with_schema,
    decode_natural,
    is_undefined,
    scan_data_map,
    wire_matches_declared,
)
from .types import DataType, coerce_data_type
from .values import PortFieldData


@dataclass(slots=True)
class Application:
    key: str = ""
    version: str = ""

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "Application":
        return cls(
            key=str(payload.get("key", "")),
            version=str(payload.get("version", "")),
        )

    def to_dict(self) -> dict[str, str]:
        return {"key": self.key, "version": self.version}


@dataclass(slots=True)
class PortFieldSchema:
    key: str
    type: DataType

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "PortFieldSchema":
        # A legacy "format" key is tolerated and ignored.
        return cls(
            key=str(payload.get("key", "")),
            type=coerce_data_type(payload.get("type", "")),
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "key": self.key,
            "type": self.type.value,
        }


@dataclass(slots=True)
class NodeData:
    name: str = ""
    description: str = ""
    inputs: dict[str, list[PortFieldSchema]] = field(default_factory=dict)
    outputs: dict[str, list[PortFieldSchema]] = field(default_factory=dict)
    application: Application = field(default_factory=Application)
    settings: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "NodeData":
        return cls(
            name=str(payload.get("name", "")),
            description=str(payload.get("description", "")),
            inputs={
                str(handle): [PortFieldSchema.from_dict(item) for item in fields]
                for handle, fields in dict(payload.get("inputs", {})).items()
            },
            outputs={
                str(handle): [PortFieldSchema.from_dict(item) for item in fields]
                for handle, fields in dict(payload.get("outputs", {})).items()
            },
            application=Application.from_dict(dict(payload.get("application", {}))),
            settings=dict(payload.get("settings", {})),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "inputs": {
                handle: [field.to_dict() for field in fields]
                for handle, fields in self.inputs.items()
            },
            "outputs": {
                handle: [field.to_dict() for field in fields]
                for handle, fields in self.outputs.items()
            },
            "application": self.application.to_dict(),
            "settings": self.settings,
        }


@dataclass(slots=True)
class Node:
    id: str = ""
    type: str = ""
    data: NodeData = field(default_factory=NodeData)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "Node":
        return cls(
            id=str(payload.get("id", "")),
            type=str(payload.get("type", "")),
            data=NodeData.from_dict(dict(payload.get("data", {}))),
        )

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "type": self.type, "data": self.data.to_dict()}


class Message:
    """An incoming NeoFlow message: source, timestamp, handle and the still-
    encoded CBOR data map (``raw``), decoded on demand by the accessors."""

    def __init__(
        self,
        source: str = "",
        timestamp: str = "",
        handle: str = "",
        raw: bytes = b"",
        plan: dict[str, DataType] | None = None,
        logger: "Logger | None" = None,
    ) -> None:
        self.source = source
        self.timestamp = timestamp
        self.handle = handle
        self.raw = raw
        self._plan = plan
        self._logger = logger
        self._lock = threading.Lock()
        self._scanned = False
        self._spans: dict[str, bytes] | None = None
        self._scan_exc: Exception | None = None
        self._decoded: dict[str, Any] | None = None
        # Keys the wire carried with a non-null value that failed to decode,
        # mapped to the reason. Distinct from undefined (absent or null):
        # to_dataclass raises for these on any concrete annotation.
        self._undecodable: dict[str, str] = {}

    def to_dict(self) -> dict[str, Any]:
        return dict(self._decode())

    def _scan_locked(self) -> dict[str, bytes] | None:
        if not self._scanned:
            try:
                self._spans = scan_data_map(self.raw)
            except Exception as exc:
                self._scan_exc = exc
            self._scanned = True
        return self._spans

    def _decode(self) -> dict[str, Any]:
        if self._decoded is not None:
            return self._decoded
        with self._lock:
            if self._decoded is not None:
                return self._decoded
            fields = self._scan_locked()
            if fields is None:
                self._warn(
                    "cannot decode data map (source=%r, handle=%r): %s",
                    self.source,
                    self.handle,
                    self._scan_exc,
                )
                self._decoded = {}
                return self._decoded

            plan = self._plan or {}
            out: dict[str, Any] = {}
            for key, tag_type in plan.items():
                span = fields.get(key)
                if span is None or is_undefined(span):
                    out[key] = None
                    continue
                try:
                    out[key] = decode_field_with_schema(span, tag_type)
                except Exception as exc:
                    self._warn("Field %r: %s; delivering undefined", key, exc)
                    out[key] = None
                    self._undecodable[key] = str(exc)

            for key, span in fields.items():
                if key in plan:
                    continue
                if is_undefined(span):
                    out[key] = None
                    continue
                try:
                    value = decode_natural(span)
                except Exception as exc:
                    self._warn("Unknown tag %r: %s; delivering undefined", key, exc)
                    out[key] = None
                    self._undecodable[key] = str(exc)
                    continue
                self._debug(
                    "Tag %r is not defined in the input schema; bypassing with natural value",
                    key,
                )
                out[key] = value

            self._decoded = out
            return out

    def to_dataclass(self, cls: type[Any]) -> Any:
        if not (isinstance(cls, type) and dataclasses.is_dataclass(cls)):
            raise TypeError(f"target must be a dataclass type, got {cls!r}")

        values = self._decode()
        with self._lock:
            spans = self._scan_locked()
        hints = get_type_hints(cls)
        kwargs: dict[str, Any] = {}
        for field_def in dataclasses.fields(cls):
            if not field_def.init:
                continue
            key = field_def.metadata.get("key", field_def.name)
            base = _annotation_base(hints.get(field_def.name))

            # Declaration wins over the schema (= Go ToStruct): a wire value
            # the annotation can take directly is decoded as declared and the
            # schema route below only handles mismatched heads.
            span = None if spans is None else spans.get(key)
            if (
                base is not None
                and span is not None
                and not is_undefined(span)
                and wire_matches_declared(span[0], base)
            ):
                try:
                    kwargs[field_def.name] = decode_declared(span, base)
                except Exception as exc:
                    raise ValueError(
                        f"field '{field_def.name}': wire value for key '{key}' "
                        f"cannot be decoded as declared: {exc}"
                    ) from None
                continue

            value = values.get(key)
            has_default = (
                field_def.default is not dataclasses.MISSING
                or field_def.default_factory is not dataclasses.MISSING
            )
            if value is None:
                reason = self._undecodable.get(key)
                if reason is not None:
                    if base is not None:
                        raise ValueError(
                            f"field '{field_def.name}': wire value for key '{key}' "
                            f"could not be decoded: {reason}"
                        )
                    kwargs[field_def.name] = None
                    continue
                if not has_default:
                    kwargs[field_def.name] = None
                continue

            if base is None:
                kwargs[field_def.name] = value
                continue
            if isinstance(value, base) and not (base is int and isinstance(value, bool)):
                kwargs[field_def.name] = value
                continue
            raise ValueError(
                f"field '{field_def.name}': value of type '{type(value).__name__}' "
                f"is not compatible with annotation '{base.__name__}'"
            )
        return cls(**kwargs)

    def _debug(self, msg: str, *args: Any) -> None:
        if self._logger is not None:
            self._logger.debug(msg, *args)

    def _warn(self, msg: str, *args: Any) -> None:
        if self._logger is not None:
            self._logger.warn(msg, *args)


def _annotation_base(annotation: Any) -> type | None:
    """Resolve an annotation to its concrete type. None means "accept as-is":
    Any, object, a missing annotation, containers and multi-type unions all
    take that path. `X | None` resolves to X — the Go pointer-field analogue:
    None stays reserved for null/absent, a bad wire value raises like bare X."""
    if annotation is None or annotation is Any or annotation is object:
        return None
    origin = get_origin(annotation)
    if origin is Union or origin is UnionType:
        args = get_args(annotation)
        non_none = [arg for arg in args if arg is not type(None)]
        if len(non_none) != 1:
            return None
        annotation = non_none[0]
        if annotation is Any or annotation is object:
            return None
        origin = get_origin(annotation)
    if origin is not None or not isinstance(annotation, type):
        return None
    return annotation


@dataclass(slots=True)
class Event:
    code: str
    detail: str = ""
    updated_at: int = 0

    def to_dict(self) -> dict[str, Any]:
        # `detail` is always emitted, even when empty, to keep the wire payload
        # shape stable for downstream consumers.
        return {
            "code": self.code,
            "detail": self.detail,
            "updatedAt": self.updated_at,
        }


@dataclass(slots=True)
class Output:
    data: dict[str, PortFieldData] = field(default_factory=dict)
    updated_at: int = 0


@dataclass(slots=True)
class StatusError:
    code: str
    detail: str
    updated_at: int


@dataclass(slots=True)
class NodeStatus:
    source_node_id: str
    errors: list[StatusError] = field(default_factory=list)
    output: Output = field(default_factory=Output)


@dataclass(slots=True)
class NeoFlowStatus:
    updated_at: int
    nodes: list[NodeStatus] = field(default_factory=list)


@dataclass(slots=True)
class MessengerConfig:
    username: str = ""
    password: str = ""

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "MessengerConfig":
        return cls(
            username=str(payload.get("username", "")),
            password=str(payload.get("password", "")),
        )


@dataclass(slots=True)
class MessengerOptions:
    config: MessengerConfig | None = None
    broker: str = "neoedgex-messenger"
    port: int = 1883
    resubscribe_interval: float = 1.0
    connect_timeout: float = 5.0


@dataclass(slots=True)
class RawMessengerPayload:
    handle: str
    data: bytes


@runtime_checkable
class Logger(Protocol):
    def tag(self) -> str: ...
    def debug(self, msg: str, *args: Any) -> None: ...
    def info(self, msg: str, *args: Any) -> None: ...
    def warn(self, msg: str, *args: Any) -> None: ...
    def error(self, msg: str, *args: Any) -> None: ...


@runtime_checkable
class MessengerClient(Protocol):
    def connect(self) -> None: ...
    def disconnect(self) -> None: ...
    def add_subscriber(self, node_id: str) -> Any: ...
    def remove_subscriber(self, node_id: str) -> None: ...
    def publish(self, topic: str, qos: int, data: bytes) -> None: ...
