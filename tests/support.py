"""Shared test doubles and wire-inspection helpers.

Lives next to the tests (not in the package) so the SDK ships nothing that
exists only for its own test suite. ``testutil`` is the *public* helper
surface and is exercised as production code by ``test_testutil.py``.
"""

from __future__ import annotations

import queue
import threading
from dataclasses import dataclass
from typing import Any

from neoedgex.contract import DataType, Node, NodeData, PortFieldSchema
from neoedgex.contract.codec import decode_neoflow_envelope, scan_data_map


class RecordingLogger:
    """Logger that keeps every line, per level, already %-formatted.

    Level matters in several assertions (a missing output field is a debug
    note, not a warning), so the levels are kept in separate lists instead of
    one merged log.
    """

    def __init__(self, tag: str = "test") -> None:
        self._tag = tag
        self.debugs: list[str] = []
        self.infos: list[str] = []
        self.warns: list[str] = []
        self.errors: list[str] = []

    def tag(self) -> str:
        return self._tag

    @staticmethod
    def _format(msg: str, args: tuple[Any, ...]) -> str:
        return msg % args if args else msg

    def debug(self, msg: str, *args: Any) -> None:
        self.debugs.append(self._format(msg, args))

    def info(self, msg: str, *args: Any) -> None:
        self.infos.append(self._format(msg, args))

    def warn(self, msg: str, *args: Any) -> None:
        self.warns.append(self._format(msg, args))

    def error(self, msg: str, *args: Any) -> None:
        self.errors.append(self._format(msg, args))

    def lines_containing(self, needle: str) -> list[str]:
        return [
            line
            for line in (*self.debugs, *self.infos, *self.warns, *self.errors)
            if needle in line
        ]


@dataclass
class PublishedRecord:
    topic: str
    qos: int
    data: bytes


class FakeMessenger:
    def __init__(self) -> None:
        self.subscriber: queue.Queue[Any] = queue.Queue()
        self.published: list[PublishedRecord] = []
        self.removed_node_id = ""
        self.connect_err: BaseException | None = None
        self.connect_called = False
        self.connected = False
        self.disconnected = False

    def connect(self) -> None:
        self.connect_called = True
        if self.connect_err is not None:
            raise self.connect_err
        self.connected = True

    def disconnect(self) -> None:
        self.disconnected = True
        self.connected = False

    def add_subscriber(self, node_id: str) -> queue.Queue[Any]:
        return self.subscriber

    def remove_subscriber(self, node_id: str) -> None:
        self.removed_node_id = node_id

    def publish(self, topic: str, qos: int, data: bytes) -> None:
        self.published.append(PublishedRecord(topic=topic, qos=qos, data=data))

    def topic_records(self, segment: str) -> list[PublishedRecord]:
        return [record for record in self.published if f"/{segment}/" in record.topic]


class FakeSDK:
    """The two loggers are kept apart on purpose: ``new_logger`` is SDK
    machinery output (what ``disable_sdk_log`` silences) and
    ``new_handler_logger`` is what the application writes through and is never
    silenced. Recording them separately is what lets a test say *which* side
    produced a line."""

    def __init__(
        self,
        messenger: FakeMessenger,
        logger: RecordingLogger | None = None,
        handler_logger: RecordingLogger | None = None,
    ) -> None:
        self._shutdown = threading.Event()
        self._messenger = messenger
        self._logger = logger or RecordingLogger()
        self.handler_logger = handler_logger or RecordingLogger()
        self.logger_tags: list[str] = []
        self.handler_logger_tags: list[str] = []
        self._sentinel = object()

    def new_logger(self, tag: str) -> RecordingLogger:
        self.logger_tags.append(tag)
        return self._logger

    def new_handler_logger(self, tag: str) -> RecordingLogger:
        self.handler_logger_tags.append(tag)
        return self.handler_logger

    def messenger(self) -> FakeMessenger:
        return self._messenger

    def shutdown_event(self) -> threading.Event:
        return self._shutdown

    def queue_closed_sentinel(self) -> object:
        return self._sentinel


def make_node(node_id: str = "node-1") -> Node:
    """Node configuration used across the runtime tests: one input field and
    an output schema whose three fields cover a float (single-precision on the
    wire), a string and a range-checked integer."""
    return Node(
        id=node_id,
        type="demo",
        data=NodeData(
            name="demo-node",
            inputs={"input1": [PortFieldSchema(key="value", type=DataType.INT64)]},
            outputs={
                "output1": [
                    PortFieldSchema(key="temp", type=DataType.FLOAT),
                    PortFieldSchema(key="label", type=DataType.STRING),
                    PortFieldSchema(key="count", type=DataType.INT16),
                ]
            },
        ),
    )


def envelope_of(payload: bytes) -> tuple[str, str, bytes]:
    """(source, timestamp, still-encoded data map) of a published payload."""
    return decode_neoflow_envelope(payload)


def data_spans(payload: bytes) -> dict[str, bytes]:
    """key -> raw CBOR bytes of each field of a published data message, in
    wire order."""
    return scan_data_map(envelope_of(payload)[2])
