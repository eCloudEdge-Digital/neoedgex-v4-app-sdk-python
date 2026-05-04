from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass

import pytest

from neoedgex._internal.messenger import MQTTMessenger, _is_success_reason_code
from neoedgex.contract import MessengerConfig, MessengerOptions, RawMessengerPayload


class NoopLogger:
    def tag(self) -> str:
        return "test"

    def debug(self, _msg: str, *_args: object) -> None:
        return None

    def info(self, _msg: str, *_args: object) -> None:
        return None

    def warn(self, _msg: str, *_args: object) -> None:
        return None

    def error(self, _msg: str, *_args: object) -> None:
        return None


class StubSDK:
    def __init__(self) -> None:
        self._shutdown = threading.Event()

    def new_logger(self, _tag: str) -> NoopLogger:
        return NoopLogger()

    def shutdown_event(self) -> threading.Event:
        return self._shutdown


@dataclass
class FakePublishResult:
    rc: int = 0

    def wait_for_publish(self, timeout: float | None = None) -> bool:
        return True


class FakeMQTTClient:
    def __init__(self) -> None:
        self.on_connect = None
        self.on_disconnect = None
        self.callbacks: dict[str, object] = {}
        self.subscriptions: list[tuple[str, int]] = []
        self.unsubscribed: list[str] = []
        self.published: list[tuple[str, bytes, int, bool]] = []
        self.disconnected = False
        self.loop_started = False

    def username_pw_set(self, _username: str, _password: str) -> None:
        return None

    def reconnect_delay_set(self, min_delay: int, max_delay: int) -> None:
        return None

    def connect(self, _broker: str, _port: int, keepalive: int = 60) -> None:
        assert self.on_connect is not None
        self.on_connect(self, None, None, 0, None)

    def loop_start(self) -> None:
        self.loop_started = True

    def loop_stop(self) -> None:
        self.loop_started = False

    def disconnect(self) -> None:
        self.disconnected = True

    def message_callback_add(self, topic: str, callback) -> None:
        self.callbacks[topic] = callback

    def message_callback_remove(self, topic: str) -> None:
        self.callbacks.pop(topic, None)

    def subscribe(self, topic: str, qos: int = 0, callback=None) -> tuple[int, int]:
        if callback is not None:
            self.callbacks[topic] = callback
        self.subscriptions.append((topic, qos))
        return (0, len(self.subscriptions))

    def unsubscribe(self, topic: str) -> tuple[int, int]:
        self.unsubscribed.append(topic)
        return (0, len(self.unsubscribed))

    def publish(self, topic: str, payload: bytes, qos: int, retain: bool) -> FakePublishResult:
        self.published.append((topic, payload, qos, retain))
        return FakePublishResult()


class FakeReasonCode:
    def __init__(self, value: int, label: str) -> None:
        self.value = value
        self._label = label

    def __str__(self) -> str:
        return self._label


class FakeReasonCodeWithMethod:
    def __init__(self, failed: bool, label: str) -> None:
        self._failed = failed
        self._label = label

    def is_failure(self) -> bool:
        return self._failed

    def __str__(self) -> str:
        return self._label


def test_connect_returns_error_when_config_is_nil() -> None:
    client = MQTTMessenger(StubSDK(), MessengerOptions(config=None))
    with pytest.raises(ValueError, match="messenger config is nil"):
        client.connect()


def test_on_connection_lost_cancels_resubscribe_context() -> None:
    client = MQTTMessenger(StubSDK(), MessengerOptions(config=MessengerConfig()))
    cancel = threading.Event()
    client._resubscribe_cancel = cancel
    client.on_connection_lost(RuntimeError("lost"))
    assert cancel.is_set()
    assert client._resubscribe_cancel is None


def test_on_connect_starts_resubscribe_loop() -> None:
    client = MQTTMessenger(StubSDK(), MessengerOptions(config=MessengerConfig(), resubscribe_interval=0.01))
    client.on_connect(None, None, None, 0, None)
    deadline = time.time() + 0.2
    while time.time() < deadline:
        if client._resubscribe_cancel is not None:
            client._resubscribe_cancel.set()
            return
        time.sleep(0.005)
    pytest.fail("expected on_connect to start resubscribe loop")


def test_reason_code_parser_accepts_legacy_and_paho_v2_shapes() -> None:
    assert _is_success_reason_code(0) is True
    assert _is_success_reason_code(1) is False
    assert _is_success_reason_code(FakeReasonCode(0, "Success")) is True
    assert _is_success_reason_code(FakeReasonCode(135, "Not authorized")) is False
    assert _is_success_reason_code(FakeReasonCodeWithMethod(False, "Success")) is True
    assert _is_success_reason_code(FakeReasonCodeWithMethod(True, "Server unavailable")) is False


def test_on_connect_handles_reason_code_object_failure() -> None:
    client = MQTTMessenger(StubSDK(), MessengerOptions(config=MessengerConfig()))

    client.on_connect(None, None, None, FakeReasonCode(135, "Not authorized"), None)

    assert isinstance(client._connect_error, RuntimeError)
    assert "Not authorized" in str(client._connect_error)
    assert client._connected_event.is_set()


def test_disconnect_and_connection_lost_are_race_safe() -> None:
    fake_client = FakeMQTTClient()
    client = MQTTMessenger(
        StubSDK(),
        MessengerOptions(config=MessengerConfig()),
        mqtt_client_factory=lambda: fake_client,
    )
    client.connect()

    threads = []
    for _ in range(50):
        threads.append(threading.Thread(target=client.disconnect))
        threads.append(threading.Thread(target=client.on_connection_lost, args=(RuntimeError("lost"),)))
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()


def test_add_subscriber_returns_existing_channel_for_same_node_id() -> None:
    client = MQTTMessenger(StubSDK(), MessengerOptions(config=MessengerConfig()))
    ch1 = client.add_subscriber("node-1")
    ch2 = client.add_subscriber("node-1")
    assert ch1 is ch2


def test_resubscribe_uses_message_callback_add_for_paho_v2() -> None:
    fake_client = FakeMQTTClient()
    client = MQTTMessenger(
        StubSDK(),
        MessengerOptions(config=MessengerConfig()),
        mqtt_client_factory=lambda: fake_client,
    )
    client.connect()
    client.add_subscriber("node-1")

    client.resubscribe_all()

    assert fake_client.subscriptions == [("neoedgex/neoflow/in/node-1/+", 2)]
    assert "neoedgex/neoflow/in/node-1/+" in fake_client.callbacks


def test_remove_subscriber_unsubscribes_and_removes_callback() -> None:
    fake_client = FakeMQTTClient()
    client = MQTTMessenger(
        StubSDK(),
        MessengerOptions(config=MessengerConfig()),
        mqtt_client_factory=lambda: fake_client,
    )
    client.connect()
    client.add_subscriber("node-1")
    client.resubscribe_all()

    client.remove_subscriber("node-1")

    assert fake_client.unsubscribed == ["neoedgex/neoflow/in/node-1/+"]
    assert "neoedgex/neoflow/in/node-1/+" not in fake_client.callbacks


def test_parse_topic() -> None:
    client = MQTTMessenger(StubSDK(), MessengerOptions(config=MessengerConfig()))
    assert client.parse_topic("neoedgex/neoflow/in/node-1/input1") == ("node-1", "input1")


def test_create_message_handler_forwards_payload() -> None:
    client = MQTTMessenger(StubSDK(), MessengerOptions(config=MessengerConfig()))
    subscription: queue.Queue[object] = queue.Queue()
    handler = client._create_message_handler(subscription)

    class Message:
        topic = "neoedgex/neoflow/in/node-1/input1"
        payload = b'{"source":"node-2","data":{}}'

    handler(None, None, Message())
    raw = subscription.get_nowait()
    assert isinstance(raw, RawMessengerPayload)
    assert raw.handle == "input1"
    assert raw.data == b'{"source":"node-2","data":{}}'


class FlakyMQTTClient(FakeMQTTClient):
    """Fails the first ``fail_count`` ``connect()`` calls, then succeeds."""

    def __init__(self, fail_count: int) -> None:
        super().__init__()
        self.fail_count = fail_count
        self.connect_calls = 0

    def connect(self, _broker: str, _port: int, keepalive: int = 60) -> None:  # type: ignore[override]
        self.connect_calls += 1
        if self.connect_calls <= self.fail_count:
            raise ConnectionRefusedError(f"broker not ready (call {self.connect_calls})")
        assert self.on_connect is not None
        self.on_connect(self, None, None, 0, None)


def test_connect_retries_until_broker_becomes_available(monkeypatch) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(
        "neoedgex._internal.messenger.time.sleep",
        lambda seconds: sleeps.append(seconds),
    )

    sdk = StubSDK()
    flaky = FlakyMQTTClient(fail_count=2)
    # Use SDK without shutdown_event() to force the time.sleep() path.
    sdk_no_shutdown = type("S", (), {})()
    sdk_no_shutdown.new_logger = lambda _tag: NoopLogger()  # type: ignore[attr-defined]

    client = MQTTMessenger(
        sdk_no_shutdown,
        MessengerOptions(config=MessengerConfig()),
        mqtt_client_factory=lambda: flaky,
    )
    client.connect()

    assert flaky.connect_calls == 3  # 2 failures + 1 success
    assert sleeps == [1.0, 1.0]
    assert client._connected is True

    # subscribing afterwards still works — proves the success path wires up
    # callbacks correctly after retries.
    client.add_subscriber("node-after-retry")
    client.resubscribe_all()
    assert ("neoedgex/neoflow/in/node-after-retry/+", 2) in flaky.subscriptions

    # sdk reference held to avoid garbage collection during test
    _ = sdk


def test_connect_retry_is_aborted_promptly_by_shutdown_event() -> None:
    sdk = StubSDK()

    class AlwaysFailsClient(FakeMQTTClient):
        def __init__(self) -> None:
            super().__init__()
            self.connect_calls = 0

        def connect(self, _broker: str, _port: int, keepalive: int = 60) -> None:  # type: ignore[override]
            self.connect_calls += 1
            raise ConnectionRefusedError("never up")

    client = MQTTMessenger(
        sdk,
        MessengerOptions(config=MessengerConfig()),
        mqtt_client_factory=lambda: AlwaysFailsClient(),
    )

    raised: list[BaseException] = []

    def run_connect() -> None:
        try:
            client.connect()
        except BaseException as exc:  # noqa: BLE001 — we want to inspect the exception
            raised.append(exc)

    thread = threading.Thread(target=run_connect, daemon=True)
    thread.start()

    # Let it fail at least once and enter the wait-1s phase.
    time.sleep(0.05)
    sdk._shutdown.set()
    thread.join(timeout=1.0)

    assert not thread.is_alive(), "connect() did not exit promptly after shutdown"
    assert len(raised) == 1
    assert "shutdown requested" in str(raised[0])


def test_connect_callback_is_invoked_after_successful_retry() -> None:
    """After a flaky connect succeeds, on_connect-driven state should be set."""
    sdk_no_shutdown = type("S", (), {})()
    sdk_no_shutdown.new_logger = lambda _tag: NoopLogger()  # type: ignore[attr-defined]

    flaky = FlakyMQTTClient(fail_count=1)
    client = MQTTMessenger(
        sdk_no_shutdown,
        MessengerOptions(config=MessengerConfig()),
        mqtt_client_factory=lambda: flaky,
    )

    # Disable the real sleep between retries to keep the test fast.
    import neoedgex._internal.messenger as messenger_mod

    original_sleep = messenger_mod.time.sleep
    messenger_mod.time.sleep = lambda _seconds: None
    try:
        client.connect()
    finally:
        messenger_mod.time.sleep = original_sleep

    assert client._connected is True
    assert client._connected_event.is_set()
