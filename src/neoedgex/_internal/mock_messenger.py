from __future__ import annotations

import json
import queue
import threading
from typing import Any

import cbor2

from neoedgex.contract import DataType, PortFieldData, RawMessengerPayload
from neoedgex.contract.codec import encode_data_map, encode_neoflow_message

from .messenger import _QUEUE_CLOSED
from .node import _now_rfc3339


class MockMessenger:
    def __init__(self, logger: Any) -> None:
        self._logger = logger
        self._lock = threading.Lock()
        self._subscribers: dict[str, queue.Queue[Any]] = {}

    def connect(self) -> None:
        return None

    def disconnect(self) -> None:
        # Wake up every active subscriber by pushing the close sentinel so
        # readers blocked on Queue.get() can exit cleanly when the mock
        # messenger is torn down directly (without going through
        # remove_subscriber).
        with self._lock:
            subscribers = list(self._subscribers.values())
        for subscriber in subscribers:
            try:
                subscriber.put_nowait(_QUEUE_CLOSED)
            except queue.Full:
                pass

    def add_subscriber(self, node_id: str) -> queue.Queue[Any]:
        with self._lock:
            existing = self._subscribers.get(node_id)
            if existing is not None:
                return existing
            subscriber: queue.Queue[Any] = queue.Queue(maxsize=32)
            self._subscribers[node_id] = subscriber
            return subscriber

    def remove_subscriber(self, node_id: str) -> None:
        with self._lock:
            subscriber = self._subscribers.pop(node_id, None)
        if subscriber is None:
            return
        try:
            subscriber.put_nowait(_QUEUE_CLOSED)
        except queue.Full:
            pass

    def publish(self, topic: str, qos: int, data: bytes) -> None:
        # No real broker to forward to; surface the outbound payload through
        # the SDK logger so local development can observe what handlers emit.
        # Data messages are CBOR, the error topic stays JSON; try both so the
        # human-readable mock output covers either wire format.
        if data:
            try:
                payload: Any = json.loads(data.decode("utf-8"))
            except Exception:
                try:
                    payload = cbor2.loads(data)
                except Exception:
                    payload = data.decode("utf-8", errors="replace")
        else:
            payload = ""
        self._logger.info("[MOCK PUBLISH] topic=%s qos=%s payload=%s", topic, qos, payload)

    def inject_neoflow_message(
        self,
        node_id: str,
        handle: str,
        data: dict[str, PortFieldData],
    ) -> None:
        # Field conversion errors take priority over a missing subscriber
        # (mirrors Go injectNeoFlowMessage, which marshals before looking up).
        entries: list[tuple[str, DataType, Any]] = []
        for key, field in data.items():
            if field.type == DataType.UNDEFINED:
                entries.append((key, field.type, None))
                continue
            try:
                value = field.get_any_value()
            except Exception as exc:
                raise ValueError(f"mock field '{key}': {exc}") from exc
            entries.append((key, field.type, value))
        # The source is hard-coded as "mock" so handlers can distinguish
        # injected mock messages from real upstream-node messages. The timestamp
        # comes from the publish path's clock so local runs see the production
        # shape instead of an empty string.
        message = encode_neoflow_message("mock", _now_rfc3339(), encode_data_map(entries))
        with self._lock:
            subscriber = self._subscribers.get(node_id)
            if subscriber is None:
                raise ValueError(f"node '{node_id}' is not subscribed")
            try:
                subscriber.put_nowait(RawMessengerPayload(handle=handle, data=message))
            except queue.Full as exc:
                raise ValueError(
                    "subscription channel is full, dropping incoming message"
                ) from exc
