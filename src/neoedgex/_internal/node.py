from __future__ import annotations

import json
import queue
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable

from neoedgex.contract import ErrorCode, Event, Message, NeoFlowMessage, Node, PortFieldData

_MESSAGE_CLOSED = object()


class MessageStream:
    def __init__(self) -> None:
        self._queue: queue.Queue[Any] = queue.Queue(maxsize=4096)
        self._closed = False
        self._lock = threading.Lock()

    def __iter__(self) -> "MessageStream":
        return self

    def __next__(self) -> Message:
        item = self._queue.get()
        if item is _MESSAGE_CLOSED:
            raise StopIteration
        return item

    def put_nowait(self, message: Message) -> None:
        self._queue.put_nowait(message)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        try:
            self._queue.put_nowait(_MESSAGE_CLOSED)
        except queue.Full:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                pass
            self._queue.put_nowait(_MESSAGE_CLOSED)


class NodeInstance:
    def __init__(self, sdk: Any, node_config: Node) -> None:
        if sdk is None:
            raise ValueError("sdk is nil")
        self._sdk = sdk
        self._logger = sdk.new_logger(f"Node-{node_config.data.name}")
        self._node_config = node_config
        self._message_stream = MessageStream()
        self._shutdown_event = threading.Event()
        self._queue_closed_sentinel = sdk.queue_closed_sentinel()
        # Single event that fires when *either* this instance is shut down or
        # the parent SDK is shut down. Lets internal loops do one event-wait
        # instead of polling both flags.
        self._combined_shutdown = threading.Event()
        self._sdk_shutdown_watcher = threading.Thread(
            target=self._mirror_sdk_shutdown, daemon=True
        )
        self._sdk_shutdown_watcher.start()

    def _mirror_sdk_shutdown(self) -> None:
        try:
            self._sdk.shutdown_event().wait()
        except Exception:
            return
        self._combined_shutdown.set()

    def node_config(self) -> Node:
        return self._node_config

    def messages(self) -> MessageStream:
        return self._message_stream

    def context(self) -> threading.Event:
        return self._shutdown_event

    def logger(self) -> Any:
        return self._logger

    def publish(self, handle: str, data: dict[str, Any]) -> None:
        desired_output = self._node_config.data.outputs.get(handle)
        if desired_output is None:
            raise ValueError(
                f"output handle '{handle}' does not exist for node {self._node_config.data.name}"
            )

        defined_keys = {field_def.key for field_def in desired_output}
        for key in data:
            if key not in defined_keys:
                self._logger.warn("Tag %r is not defined in the output schema; dropping", key)

        port_fields: dict[str, PortFieldData] = {}
        for field_def in desired_output:
            if field_def.key not in data:
                self._logger.warn("Output field %r not provided, sending nil", field_def.key)
                port_fields[field_def.key] = PortFieldData.empty()
                continue
            raw_value = data[field_def.key]
            if raw_value is None:
                self._logger.warn("Output field %r provided with nil value, sending nil", field_def.key)
                port_fields[field_def.key] = PortFieldData.empty()
                continue
            try:
                port_fields[field_def.key] = PortFieldData.new_with_any(raw_value, field_def.format)
            except Exception as exc:
                port_fields[field_def.key] = PortFieldData.empty()
                self.report_error(ErrorCode.PROCESS_ERROR, ValueError(f"field '{field_def.key}': {exc}"))

        message = NeoFlowMessage(
            source_node_id=self._node_config.id,
            timestamp=_now_rfc3339(),
            data=port_fields,
        )
        topic = f"neoedgex/neoflow/out/{self._node_config.id}/{handle}"
        self._sdk.messenger().publish(topic, 2, json.dumps(message.to_dict()).encode("utf-8"))

    def report_error(self, code: ErrorCode, err: BaseException | None) -> None:
        try:
            self._publish_node_event(code, err)
        except Exception as exc:
            self._logger.warn("Failed to publish node event: %s", exc)

    def shutdown(self) -> None:
        self._shutdown_event.set()
        self._combined_shutdown.set()
        self._message_stream.close()

    def stop(self) -> None:
        self.shutdown()

    def run(self, handler: Callable[[], None]) -> None:
        self._logger.info("Starting node instance")
        loop_thread = threading.Thread(target=self._run_loop, daemon=True)
        loop_thread.start()
        try:
            self._supervise_handler(handler)
        finally:
            self.shutdown()
            loop_thread.join()

    def _supervise_handler(self, handler: Callable[[], None]) -> None:
        initial_backoff = 1.0
        max_backoff = 30.0
        reset_threshold = 30.0
        backoff = initial_backoff

        while not self._is_done():
            started_at = time.monotonic()
            crashed = self._run_handler_once(handler)
            if not crashed:
                return
            if time.monotonic() - started_at > reset_threshold:
                backoff = initial_backoff
            self._logger.warn("Handler crashed, restarting in %s", backoff)
            self.report_error(ErrorCode.PROCESS_ERROR, RuntimeError("handler crashed, restarting"))
            if self._wait(backoff):
                return
            backoff = min(backoff * 2, max_backoff)

    def _run_handler_once(self, handler: Callable[[], None]) -> bool:
        try:
            handler()
        except Exception as exc:
            self._logger.error("Handler panicked: %s", exc)
            return True
        return not self._is_done()

    def _publish_node_event(self, code: ErrorCode, detail: BaseException | None) -> None:
        event = Event(
            code=code.value,
            detail="" if detail is None else str(detail),
            updated_at=int(time.time()),
        )
        topic = f"neoedgex/neoflow/error/{self._node_config.id}"
        self._sdk.messenger().publish(topic, 0, json.dumps(event.to_dict()).encode("utf-8"))

    def _publish_heartbeat(self) -> None:
        topic = f"neoedgex/neoflow/heartbeat/{self._node_config.id}"
        self._sdk.messenger().publish(topic, 0, b"")

    def _run_loop(self) -> None:
        subscriber = self._sdk.messenger().add_subscriber(self._node_config.id)
        # When shutdown fires, unblock the in-flight ``subscriber.get()`` by
        # pushing the close sentinel into the queue. Lets the loop reach
        # shutdown without a polling timeout.
        wakeup_done = threading.Event()

        def wakeup_on_shutdown() -> None:
            self._combined_shutdown.wait()
            wakeup_done.set()
            try:
                subscriber.put_nowait(self._queue_closed_sentinel)
            except queue.Full:
                pass

        wakeup_thread = threading.Thread(target=wakeup_on_shutdown, daemon=True)
        wakeup_thread.start()

        next_heartbeat = time.monotonic() + 5.0
        try:
            while not self._is_done():
                now = time.monotonic()
                if now >= next_heartbeat:
                    try:
                        self._publish_heartbeat()
                    except Exception as exc:
                        self._logger.warn("Failed to publish heartbeat: %s", exc)
                    next_heartbeat = now + 5.0
                wait_for = max(0.0, next_heartbeat - time.monotonic())
                if wait_for == 0.0:
                    continue
                try:
                    payload = subscriber.get(timeout=wait_for)
                except queue.Empty:
                    continue
                if payload is self._queue_closed_sentinel:
                    return
                if payload is None or getattr(payload, "handle", None) is None:
                    continue
                try:
                    neoflow_message = NeoFlowMessage.from_dict(json.loads(payload.data.decode("utf-8")))
                except Exception as exc:
                    self._logger.error("Failed to unmarshal neoflow message: %s", exc)
                    continue
                try:
                    self._message_stream.put_nowait(
                        Message(
                            handle=payload.handle,
                            data=_decode_incoming_data(neoflow_message.data),
                            source=neoflow_message.source_node_id,
                            timestamp=neoflow_message.timestamp,
                        )
                    )
                except queue.Full:
                    err = RuntimeError("message channel is full, dropping incoming message")
                    self._logger.warn(str(err))
                    self.report_error(ErrorCode.PROCESS_ERROR, err)
        finally:
            self._message_stream.close()
            self._sdk.messenger().remove_subscriber(self._node_config.id)
            # Ensure the wakeup thread can exit even if shutdown never fires
            # (e.g. handler returned of its own accord).
            if not wakeup_done.is_set():
                self._combined_shutdown.set()

    def _wait(self, seconds: float) -> bool:
        # Block until either the timeout elapses or shutdown is requested.
        # Returns True when shutdown caused the wakeup, False on timeout.
        return self._combined_shutdown.wait(timeout=seconds)

    def _is_done(self) -> bool:
        return self._combined_shutdown.is_set()


def _decode_incoming_data(data: dict[str, PortFieldData]) -> dict[str, Any]:
    decoded: dict[str, Any] = {}
    for key, field in data.items():
        if field.type.value == "" or field.format.value == "":
            decoded[key] = None
            continue
        try:
            decoded[key] = field.get_any_value()
        except Exception:
            decoded[key] = None
    return decoded


def _now_rfc3339() -> str:
    # Second-precision ISO 8601 timestamp in the local timezone. Output uses a
    # numeric offset (e.g. ``+08:00``) and a ``Z`` suffix is normalized for UTC.
    local_tz = datetime.now().astimezone().tzinfo
    base = datetime.now(tz=local_tz).replace(microsecond=0)
    offset = base.strftime("%z")
    if offset:
        offset_str = f"{offset[:3]}:{offset[3:]}"
    else:
        offset_str = ""
    head = base.strftime("%Y-%m-%dT%H:%M:%S")
    if offset_str in ("+00:00", "-00:00", ""):
        suffix = "Z"
    else:
        suffix = offset_str
    return f"{head}{suffix}"
