from __future__ import annotations

import queue
import threading
import time
from typing import Any, Callable

from neoedgex.contract import MessengerOptions, RawMessengerPayload

try:
    import paho.mqtt.client as mqtt
except ModuleNotFoundError:
    mqtt = None

_QUEUE_CLOSED = object()


class MQTTMessenger:
    def __init__(
        self,
        sdk: Any,
        options: MessengerOptions | None = None,
        mqtt_client_factory: Callable[[], Any] | None = None,
    ) -> None:
        self._sdk = sdk
        self._logger = sdk.new_logger("MessengerContext")
        self._options = options or MessengerOptions()
        self._mqtt_client_factory = mqtt_client_factory
        self._lock = threading.Lock()
        self._desired: dict[str, queue.Queue[Any]] = {}
        self._actual: set[str] = set()
        self._client: Any = None
        self._connected = False
        self._connect_error: BaseException | None = None
        self._connected_event = threading.Event()
        self._resubscribe_cancel: threading.Event | None = None
        self._resubscribe_thread: threading.Thread | None = None

    def connect(self) -> None:
        if self._options.config is None:
            raise ValueError("messenger config is nil")

        with self._lock:
            if self._client is None:
                self._client = self._new_client()
            elif self._connected:
                return
            client = self._client

        # paho-mqtt's `reconnect_delay_set` only kicks in after the first
        # successful connection, so we run our own retry-until-broker-up loop
        # here. The loop is interruptible via the SDK's shutdown event so a
        # SIGTERM during startup exits promptly instead of looping forever.
        shutdown_event = self._get_shutdown_event()
        attempt = 0
        loop_started = False
        while True:
            if shutdown_event is not None and shutdown_event.is_set():
                self._logger.info(
                    "Shutdown requested while connecting to NeoEdgeX Messenger; aborting connect"
                )
                raise RuntimeError("connect aborted: shutdown requested")

            attempt += 1
            self._logger.debug(
                "NeoEdgeX Messenger is not connected, attempting to connect (attempt %d)",
                attempt,
            )
            self._connected_event.clear()
            self._connect_error = None

            try:
                client.connect(self._options.broker, self._options.port, keepalive=30)
                if not loop_started and hasattr(client, "loop_start"):
                    client.loop_start()
                    loop_started = True
                if not self._connected_event.wait(timeout=self._options.connect_timeout):
                    raise TimeoutError("connect timed out")
                if self._connect_error is not None:
                    raise RuntimeError(str(self._connect_error))
                return
            except Exception as exc:
                self._logger.warn(
                    "Failed to connect to NeoEdgeX Messenger (attempt %d): %s; retrying in 1s",
                    attempt,
                    exc,
                )
                if shutdown_event is not None:
                    if shutdown_event.wait(1.0):
                        self._logger.info(
                            "Shutdown requested while waiting to retry connect; aborting"
                        )
                        raise RuntimeError("connect aborted: shutdown requested") from exc
                else:
                    time.sleep(1.0)

    def _get_shutdown_event(self) -> threading.Event | None:
        getter = getattr(self._sdk, "shutdown_event", None)
        if getter is None:
            return None
        try:
            event = getter()
        except Exception:
            return None
        if isinstance(event, threading.Event):
            return event
        return None

    def disconnect(self) -> None:
        with self._lock:
            client = self._client
            cancel = self._resubscribe_cancel
            self._resubscribe_cancel = None
            self._connected = False
            self._connected_event.clear()
        if cancel is not None:
            cancel.set()
        if client is not None:
            try:
                if hasattr(client, "disconnect"):
                    client.disconnect()
                if hasattr(client, "loop_stop"):
                    client.loop_stop()
            finally:
                self._logger.info("Disconnected from NeoEdgeX Messenger")
        self._cleanup()

    def add_subscriber(self, node_id: str) -> queue.Queue[Any]:
        with self._lock:
            existing = self._desired.get(node_id)
            if existing is not None:
                return existing
            subscriber: queue.Queue[Any] = queue.Queue(maxsize=32)
            self._desired[node_id] = subscriber
            return subscriber

    def remove_subscriber(self, node_id: str) -> None:
        with self._lock:
            subscriber = self._desired.pop(node_id, None)
            self._actual.discard(node_id)
            client = self._client
            connected = self._connected
        if subscriber is None:
            return
        if client is not None and connected:
            topic = self.create_input_topic(node_id)
            if hasattr(client, "message_callback_remove"):
                client.message_callback_remove(topic)
            if hasattr(client, "unsubscribe"):
                client.unsubscribe(topic)
        try:
            subscriber.put_nowait(_QUEUE_CLOSED)
        except queue.Full:
            pass

    def publish(self, topic: str, qos: int, data: bytes) -> None:
        with self._lock:
            client = self._client
            connected = self._connected
        if client is None or not connected:
            raise RuntimeError("MQTT client is not connected")
        result = client.publish(topic, payload=data, qos=qos, retain=False)
        if hasattr(result, "wait_for_publish"):
            completed = result.wait_for_publish(timeout=5.0)
            if completed is False:
                raise TimeoutError(f"publish to topic {topic} timed out")
        if getattr(result, "rc", 0) not in {0, None}:
            raise RuntimeError(f"failed to publish message to topic {topic}: rc={result.rc}")

    def on_connect(self, _client: Any, _userdata: Any, _flags: Any, reason_code: Any, _properties: Any = None) -> None:
        if not _is_success_reason_code(reason_code):
            self._connect_error = RuntimeError(f"connection refused: rc={reason_code}")
            self._connected_event.set()
            return
        with self._lock:
            self._connected = True
            self._connect_error = None
        self._connected_event.set()
        self._logger.info("Connected to NeoEdgeX Messenger")
        self._start_resubscribe_loop()

    def on_disconnect(self, _client: Any, _userdata: Any, reason_code: Any, _properties: Any = None) -> None:
        if _is_success_reason_code(reason_code):
            return
        self.on_connection_lost(RuntimeError(f"lost connection: rc={reason_code}"))

    def on_connection_lost(self, err: BaseException) -> None:
        self._logger.error("Connection to NeoEdgeX Messenger lost: %s", err)
        with self._lock:
            cancel = self._resubscribe_cancel
            self._resubscribe_cancel = None
            self._connected = False
            self._connected_event.clear()
        if cancel is not None:
            cancel.set()
        self._cleanup()

    def create_input_topic(self, node_id: str) -> str:
        return f"neoedgex/neoflow/in/{node_id}/+"

    def parse_topic(self, topic: str) -> tuple[str, str]:
        parts = topic.split("/")
        if len(parts) < 5:
            raise ValueError(f"invalid topic format: {topic}")
        return parts[3], parts[4]

    def resubscribe_all(self) -> None:
        with self._lock:
            client = self._client
            desired = dict(self._desired)
            actual = set(self._actual)
            connected = self._connected
        if client is None or not connected:
            self._logger.debug("MQTT client snapshot disconnected; aborting resubscribe")
            return
        for node_id, subscriber in desired.items():
            if node_id in actual:
                continue
            topic = self.create_input_topic(node_id)
            handler = self._create_message_handler(subscriber)
            if hasattr(client, "message_callback_add"):
                client.message_callback_add(topic, handler)
                result = client.subscribe(topic, qos=2)
            else:
                result = client.subscribe(topic, qos=2, callback=handler)
            rc = result[0] if isinstance(result, tuple) else getattr(result, "rc", 0)
            if rc not in {0, None}:
                if hasattr(client, "message_callback_remove"):
                    client.message_callback_remove(topic)
                self._logger.error("Failed to subscribe to topic %s: rc=%s", topic, rc)
                continue
            self._logger.info("Subscribed to topic %s successfully", topic)
            actual.add(node_id)
        with self._lock:
            self._actual = actual

    def _start_resubscribe_loop(self) -> None:
        with self._lock:
            if self._resubscribe_cancel is not None:
                self._resubscribe_cancel.set()
            cancel = threading.Event()
            self._resubscribe_cancel = cancel
        thread = threading.Thread(target=self._run_resubscribe_loop, args=(cancel,), daemon=True)
        self._resubscribe_thread = thread
        thread.start()

    def _run_resubscribe_loop(self, cancel: threading.Event) -> None:
        self.resubscribe_all()
        interval = self._options.resubscribe_interval or 1.0
        while not cancel.wait(interval):
            self.resubscribe_all()
        self._logger.info("Resubscribing monitoring stopped")

    def _cleanup(self) -> None:
        with self._lock:
            self._actual = set()

    def _create_message_handler(self, subscription_channel: queue.Queue[Any]) -> Callable[..., None]:
        def handler(_client: Any, _userdata: Any, msg: Any) -> None:
            try:
                _node_id, input_handle = self.parse_topic(msg.topic)
            except Exception as exc:
                self._logger.error("Failed to parse topic %s: %s", getattr(msg, "topic", ""), exc)
                return
            try:
                subscription_channel.put_nowait(
                    RawMessengerPayload(handle=input_handle, data=bytes(msg.payload))
                )
            except queue.Full:
                self._logger.warn("Subscription channel is full, dropping incoming message")

        return handler

    def _new_client(self) -> Any:
        if self._mqtt_client_factory is not None:
            client = self._mqtt_client_factory()
        else:
            if mqtt is None:
                raise RuntimeError(
                    "paho-mqtt is required for non-mock runtime; install dependency 'paho-mqtt'"
                )
            client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)

        if hasattr(client, "username_pw_set") and self._options.config is not None:
            client.username_pw_set(self._options.config.username, self._options.config.password)
        if hasattr(client, "reconnect_delay_set"):
            client.reconnect_delay_set(min_delay=1, max_delay=1)
        if hasattr(client, "on_connect"):
            client.on_connect = self.on_connect
        if hasattr(client, "on_disconnect"):
            client.on_disconnect = self.on_disconnect
        return client


def _is_success_reason_code(reason_code: Any) -> bool:
    if reason_code is None:
        return True

    try:
        return int(reason_code) == 0
    except (TypeError, ValueError):
        pass

    raw_value = getattr(reason_code, "value", None)
    if raw_value is not None:
        try:
            return int(raw_value) == 0
        except (TypeError, ValueError):
            pass

    is_failure = getattr(reason_code, "is_failure", None)
    if callable(is_failure):
        return not bool(is_failure())
    if isinstance(is_failure, bool):
        return not is_failure

    return str(reason_code).lower() == "success"
