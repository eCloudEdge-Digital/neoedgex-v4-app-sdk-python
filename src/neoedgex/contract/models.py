from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from .types import ErrorCode, coerce_data_format, coerce_data_type
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
    type: Any
    format: Any

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "PortFieldSchema":
        return cls(
            key=str(payload.get("key", "")),
            type=coerce_data_type(payload.get("type", "")),
            format=coerce_data_format(payload.get("format", "")),
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "key": self.key,
            "type": self.type.value,
            "format": self.format.value,
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


@dataclass(slots=True)
class NeoFlowMessage:
    source_node_id: str
    data: dict[str, PortFieldData]
    timestamp: str = ""

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "NeoFlowMessage":
        timestamp = payload.get("timestamp", "")
        return cls(
            source_node_id=str(payload.get("source", "")),
            data={
                str(key): PortFieldData.from_dict(dict(value))
                for key, value in dict(payload.get("data", {})).items()
            },
            timestamp="" if timestamp is None else str(timestamp),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source_node_id,
            "timestamp": self.timestamp,
            "data": {key: value.to_dict() for key, value in self.data.items()},
        }


@dataclass(slots=True)
class Message:
    handle: str
    data: dict[str, Any]
    source: str
    timestamp: str = ""


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
