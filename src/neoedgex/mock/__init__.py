from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from neoedgex.contract import Node, PortFieldData


@dataclass(slots=True)
class MockMessage:
    node_id: str
    handle: str
    data: dict[str, PortFieldData]

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "MockMessage":
        return cls(
            node_id=str(payload.get("nodeID", "")),
            handle=str(payload.get("handle", "")),
            data={
                str(key): PortFieldData.from_dict(dict(value))
                for key, value in dict(payload.get("data", {})).items()
            },
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "nodeID": self.node_id,
            "handle": self.handle,
            "data": {key: value.to_dict() for key, value in self.data.items()},
        }


@dataclass(slots=True)
class MockSection:
    message_interval: str = ""
    messages: list[MockMessage] = field(default_factory=list)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "MockSection":
        return cls(
            message_interval=str(payload.get("messageInterval", "")),
            messages=[MockMessage.from_dict(item) for item in payload.get("messages", [])],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "messageInterval": self.message_interval,
            "messages": [message.to_dict() for message in self.messages],
        }


@dataclass(slots=True)
class MockConfig:
    nodes: list[Node] = field(default_factory=list)
    mock: MockSection = field(default_factory=MockSection)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "MockConfig":
        return cls(
            nodes=[Node.from_dict(item) for item in payload.get("nodes", [])],
            mock=MockSection.from_dict(dict(payload.get("mock", {}))),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "nodes": [node.to_dict() for node in self.nodes],
            "mock": self.mock.to_dict(),
        }


def load_config(path: str | Path) -> MockConfig:
    config_path = Path(path)
    try:
        raw = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"read mock config: {exc}") from exc

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"parse mock config: {exc}") from exc

    config = MockConfig.from_dict(payload)
    if not config.nodes:
        raise ValueError("mock config: nodes must not be empty")
    return config


__all__ = ["MockConfig", "MockMessage", "MockSection", "load_config"]
