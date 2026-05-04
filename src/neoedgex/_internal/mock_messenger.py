from __future__ import annotations

import json
import queue
from typing import Any

from neoedgex.contract import RawMessengerPayload

from .messenger import _QUEUE_CLOSED


class MockMessenger:
    def __init__(self, logger: Any) -> None:
        self._logger = logger
        self._subscribers: dict[str, queue.Queue[Any]] = {}

    def connect(self) -> None:
        return None

    def disconnect(self) -> None:
        # Wake up every active subscriber by pushing the close sentinel so
        # readers blocked on Queue.get() can exit cleanly when the mock
        # messenger is torn down directly (without going through
        # remove_subscriber).
        for subscriber in list(self._subscribers.values()):
            try:
                subscriber.put_nowait(_QUEUE_CLOSED)
            except queue.Full:
                pass

    def add_subscriber(self, node_id: str) -> queue.Queue[Any]:
        if node_id in self._subscribers:
            return self._subscribers[node_id]
        subscriber: queue.Queue[Any] = queue.Queue(maxsize=32)
        self._subscribers[node_id] = subscriber
        return subscriber

    def remove_subscriber(self, node_id: str) -> None:
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
        if data:
            try:
                payload: Any = json.loads(data.decode("utf-8"))
            except Exception:
                payload = data.decode("utf-8", errors="replace")
        else:
            payload = ""
        self._logger.info("[MOCK PUBLISH] topic=%s qos=%s payload=%s", topic, qos, payload)

    def inject_neoflow_message(
        self,
        node_id: str,
        handle: str,
        data: dict[str, Any],
    ) -> None:
        subscriber = self._subscribers.get(node_id)
        if subscriber is None:
            raise ValueError(f"node '{node_id}' is not subscribed")
        # The source is hard-coded as "mock" so handlers can distinguish
        # injected mock messages from real upstream-node messages.
        payload = {
            "source": "mock",
            "data": {key: value.to_dict() for key, value in data.items()},
        }
        try:
            subscriber.put_nowait(
                RawMessengerPayload(handle=handle, data=json.dumps(payload).encode("utf-8"))
            )
        except queue.Full as exc:
            raise ValueError("subscription channel is full, dropping incoming message") from exc
