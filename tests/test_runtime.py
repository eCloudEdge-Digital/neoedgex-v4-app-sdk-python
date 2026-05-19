from __future__ import annotations

import json
import queue
import signal
import threading
import time
from dataclasses import dataclass

import pytest

import neoedgex
from neoedgex import App
from neoedgex._internal.mock_messenger import MockMessenger
from neoedgex._internal.node import NodeInstance
from neoedgex._internal.sdk import SDK
from neoedgex.contract import (
    ErrorCode,
    Message,
    MessengerConfig,
    NeoFlowMessage,
    Node,
    NodeData,
    PortFieldData,
    PortFieldSchema,
    RawMessengerPayload,
)
from neoedgex.contract import DataFormat, DataType
from neoedgex.mock import MockConfig, MockMessage, MockSection


class FakeLogger:
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


@dataclass
class PublishedRecord:
    topic: str
    qos: int
    data: bytes


class FakeMessenger:
    def __init__(self) -> None:
        self.subscriber: queue.Queue[object] = queue.Queue()
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

    def add_subscriber(self, node_id: str) -> queue.Queue[object]:
        return self.subscriber

    def remove_subscriber(self, node_id: str) -> None:
        self.removed_node_id = node_id

    def publish(self, topic: str, qos: int, data: bytes) -> None:
        self.published.append(PublishedRecord(topic=topic, qos=qos, data=data))


class FakeSDK:
    def __init__(self, messenger: FakeMessenger) -> None:
        self._shutdown = threading.Event()
        self._messenger = messenger
        self._sentinel = object()

    def new_logger(self, _tag: str) -> FakeLogger:
        return FakeLogger()

    def messenger(self) -> FakeMessenger:
        return self._messenger

    def shutdown_event(self) -> threading.Event:
        return self._shutdown

    def queue_closed_sentinel(self) -> object:
        return self._sentinel


def test_public_exports_use_node_env_not_node_context() -> None:
    assert "NodeEnv" in neoedgex.__all__
    assert "NodeContext" not in neoedgex.__all__
    assert not hasattr(neoedgex, "NodeContext")


def make_node() -> Node:
    return Node(
        id="node-1",
        type="demo",
        data=NodeData(
            name="demo-node",
            inputs={
                "input1": [
                    PortFieldSchema(
                        key="value",
                        type=DataType.STRING,
                        format=DataFormat.STRING,
                    )
                ]
            },
            outputs={
                "output1": [
                    PortFieldSchema(
                        key="value",
                        type=DataType.STRING,
                        format=DataFormat.INT64,
                    ),
                    PortFieldSchema(
                        key="status",
                        type=DataType.STRING,
                        format=DataFormat.STRING,
                    ),
                ]
            },
        ),
    )


def test_publish_output_topic_and_payload_shape() -> None:
    messenger = FakeMessenger()
    instance = NodeInstance(FakeSDK(messenger), make_node())
    instance.publish("output1", {"value": 7})
    assert messenger.published[0].topic == "neoedgex/neoflow/out/node-1/output1"
    assert messenger.published[0].qos == 2

    message = NeoFlowMessage.from_dict(json.loads(messenger.published[0].data.decode("utf-8")))
    assert message.timestamp
    assert message.data["value"].type == DataType.INT64
    assert message.data["value"].value == "7"


def test_publish_fills_missing_output_field_with_empty_field() -> None:
    messenger = FakeMessenger()
    instance = NodeInstance(FakeSDK(messenger), make_node())
    instance.publish("output1", {"value": 7})
    message = NeoFlowMessage.from_dict(json.loads(messenger.published[0].data.decode("utf-8")))
    assert message.data["status"].type == DataType.UNDEFINED
    assert message.data["status"].format == DataFormat.UNDEFINED
    assert message.data["status"].value == ""


def test_publish_treats_nil_field_value_as_empty_field() -> None:
    messenger = FakeMessenger()
    instance = NodeInstance(FakeSDK(messenger), make_node())
    instance.publish("output1", {"value": None, "status": "ok"})
    message = NeoFlowMessage.from_dict(json.loads(messenger.published[0].data.decode("utf-8")))
    assert message.data["value"].type == DataType.UNDEFINED
    assert message.data["value"].format == DataFormat.UNDEFINED
    assert message.data["value"].value == ""


def test_run_loop_skips_input_validation() -> None:
    messenger = FakeMessenger()
    instance = NodeInstance(FakeSDK(messenger), make_node())
    loop_thread = threading.Thread(target=instance._run_loop, daemon=True)
    loop_thread.start()

    payload = NeoFlowMessage(
        source_node_id="source-node",
        timestamp="2026-03-31T09:10:11Z",
        data={
            "value": PortFieldData(type=DataType.INT64, format=DataFormat.INT64, value="42"),
        },
    )
    messenger.subscriber.put(
        RawMessengerPayload(handle="input1", data=json.dumps(payload.to_dict()).encode("utf-8"))
    )
    message = next(iter(instance.messages()))
    assert message.source == "source-node"
    assert message.timestamp == "2026-03-31T09:10:11Z"
    assert message.data["value"] == 42
    instance.shutdown()
    loop_thread.join(timeout=2.0)


def test_run_loop_sets_none_for_empty_or_malformed_input_fields() -> None:
    messenger = FakeMessenger()
    instance = NodeInstance(FakeSDK(messenger), make_node())
    loop_thread = threading.Thread(target=instance._run_loop, daemon=True)
    loop_thread.start()

    payload = NeoFlowMessage(
        source_node_id="source-node",
        data={
            "empty": PortFieldData.empty(),
            "bad": PortFieldData(type=DataType.INT64, format=DataFormat.INT64, value="not-an-int"),
        },
    )
    messenger.subscriber.put(
        RawMessengerPayload(handle="input1", data=json.dumps(payload.to_dict()).encode("utf-8"))
    )
    message = next(iter(instance.messages()))
    assert message.data["empty"] is None
    assert message.data["bad"] is None
    instance.shutdown()
    loop_thread.join(timeout=2.0)


def test_node_env_context_logger_and_stop() -> None:
    messenger = FakeMessenger()
    instance = NodeInstance(FakeSDK(messenger), make_node())
    assert isinstance(instance.context(), threading.Event)
    assert instance.logger().tag() == "test"
    instance.stop()
    assert instance.context().is_set()


def test_sdk_run_returns_error_when_messenger_connect_fails() -> None:
    sdk = SDK()
    messenger = FakeMessenger()
    messenger.connect_err = RuntimeError("connect failed")
    sdk._messenger = messenger
    with pytest.raises(RuntimeError, match="connect failed"):
        sdk.run()


def test_sdk_run_does_not_invoke_callback_when_connect_fails() -> None:
    sdk = SDK()
    messenger = FakeMessenger()
    messenger.connect_err = RuntimeError("connect failed")
    sdk._messenger = messenger

    callback_called = False

    def on_connected() -> None:
        nonlocal callback_called
        callback_called = True

    with pytest.raises(RuntimeError, match="connect failed"):
        sdk.run(on_connected)

    assert callback_called is False
    assert messenger.disconnected is False


def test_sdk_run_invokes_callback_after_connect_and_before_blocking() -> None:
    sdk = SDK()
    messenger = FakeMessenger()
    sdk._messenger = messenger

    observed_connected: list[bool] = []
    observed_shutdown_state: list[bool] = []
    callback_fired = threading.Event()

    def on_connected() -> None:
        observed_connected.append(messenger.connected)
        observed_shutdown_state.append(sdk.shutdown_event().is_set())
        callback_fired.set()

    thread = threading.Thread(target=sdk.run, args=(on_connected,), daemon=True)
    thread.start()
    assert callback_fired.wait(timeout=2.0), "on_connected was not invoked in time"
    sdk.shutdown()
    thread.join(timeout=2.0)

    assert observed_connected == [True]
    assert observed_shutdown_state == [False]
    assert messenger.disconnected is True


def test_sdk_run_disconnects_on_shutdown() -> None:
    sdk = SDK()
    messenger = FakeMessenger()
    sdk._messenger = messenger

    thread = threading.Thread(target=sdk.run, daemon=True)
    thread.start()
    time.sleep(0.05)
    sdk.shutdown()
    thread.join(timeout=2.0)

    assert messenger.connect_called is True
    assert messenger.disconnected is True


def test_handler_exception_or_early_return_triggers_restart_and_stable_runtime_resets_backoff(monkeypatch) -> None:
    messenger = FakeMessenger()
    instance = NodeInstance(FakeSDK(messenger), make_node())

    waits: list[float] = []
    monotonic_values = iter([0.0, 10.0, 20.0, 55.0, 60.0])

    monkeypatch.setattr(time, "monotonic", lambda: next(monotonic_values))
    monkeypatch.setattr(instance, "_wait", lambda seconds: waits.append(seconds) or False)

    calls = {"count": 0}

    def handler() -> None:
        calls["count"] += 1
        if calls["count"] < 3:
            raise RuntimeError("boom")
        instance.shutdown()

    instance._supervise_handler(handler)
    assert calls["count"] == 3
    assert waits == [1.0, 1.0]
    assert any(record.topic == "neoedgex/neoflow/error/node-1" for record in messenger.published)


def test_mock_mode_injects_messages_round_robin_and_logs_publishes(caplog) -> None:
    sdk = SDK()
    config = MockConfig(
        nodes=[make_node()],
        mock=MockSection(
            message_interval="0.01s",
            messages=[
                MockMessage(
                    node_id="node-1",
                    handle="input1",
                    data={"value": PortFieldData(type=DataType.STRING, format=DataFormat.STRING, value="a")},
                ),
                MockMessage(
                    node_id="node-1",
                    handle="input1",
                    data={"value": PortFieldData(type=DataType.STRING, format=DataFormat.STRING, value="b")},
                ),
            ],
        ),
    )
    sdk.enable_mock(config)
    subscriber = sdk.messenger().add_subscriber("node-1")
    sdk.start_message_injection()

    first = subscriber.get(timeout=1.0)
    second = subscriber.get(timeout=1.0)
    assert isinstance(first, RawMessengerPayload)
    assert isinstance(second, RawMessengerPayload)

    with caplog.at_level("INFO"):
        sdk.messenger().publish("neoedgex/neoflow/out/node-1/output1", 2, b'{"demo":true}')
    assert any("[MOCK PUBLISH]" in record.getMessage() for record in caplog.records)

    sdk.shutdown()


def test_mock_inject_neoflow_message_sets_source_to_mock_literal() -> None:
    messenger = MockMessenger(FakeLogger())
    subscriber = messenger.add_subscriber("node-1")
    messenger.inject_neoflow_message(
        "node-1",
        "input1",
        {"value": PortFieldData(type=DataType.INT64, format=DataFormat.INT64, value="7")},
    )
    raw = subscriber.get_nowait()
    assert isinstance(raw, RawMessengerPayload)
    parsed = NeoFlowMessage.from_dict(json.loads(raw.data.decode("utf-8")))
    assert parsed.source_node_id == "mock"


def test_mock_handler_observes_source_mock_in_received_message() -> None:
    config = MockConfig(
        nodes=[make_node()],
        mock=MockSection(
            message_interval="0.01s",
            messages=[
                MockMessage(
                    node_id="node-1",
                    handle="input1",
                    data={
                        "value": PortFieldData(
                            type=DataType.INT64, format=DataFormat.INT64, value="42"
                        )
                    },
                )
            ],
        ),
    )

    observed_sources: list[str] = []
    done = threading.Event()

    class Handler:
        def handle(self, ctx) -> None:
            for msg in ctx.messages():
                observed_sources.append(msg.source)
                done.set()
                ctx._sdk.shutdown()
                break

    app = App(Handler())
    app.enable_mock(config)

    thread = threading.Thread(target=app.run, daemon=True)
    thread.start()
    assert done.wait(timeout=2.0), "handler did not observe a mock-injected message in time"
    thread.join(timeout=1.0)

    assert observed_sources == ["mock"]


def test_mock_messenger_disconnect_pushes_sentinel_to_active_subscribers() -> None:
    messenger = MockMessenger(FakeLogger())
    sub_a = messenger.add_subscriber("node-1")
    sub_b = messenger.add_subscriber("node-2")
    messenger.disconnect()
    from neoedgex._internal.messenger import _QUEUE_CLOSED as messenger_sentinel
    assert sub_a.get_nowait() is messenger_sentinel
    assert sub_b.get_nowait() is messenger_sentinel


def test_mock_messenger_remove_subscriber_pushes_sentinel() -> None:
    messenger = MockMessenger(FakeLogger())
    subscriber = messenger.add_subscriber("node-1")
    messenger.remove_subscriber("node-1")
    sentinel = subscriber.get_nowait()
    from neoedgex._internal.messenger import _QUEUE_CLOSED as messenger_sentinel
    assert sentinel is messenger_sentinel


def test_mock_messenger_publish_logs_outbound_payload() -> None:
    class _CapturingLogger:
        def __init__(self) -> None:
            self.infos: list[str] = []

        def tag(self) -> str:
            return "test"

        def debug(self, _msg: str, *_args: object) -> None:
            return None

        def info(self, msg: str, *args: object) -> None:
            self.infos.append(msg % args if args else msg)

        def warn(self, _msg: str, *_args: object) -> None:
            return None

        def error(self, _msg: str, *_args: object) -> None:
            return None

    logger = _CapturingLogger()
    messenger = MockMessenger(logger)
    messenger.publish("topic/x", 0, b'{"k": 1}')
    messenger.publish("topic/y", 2, b"")
    assert any("[MOCK PUBLISH]" in line and "topic/x" in line for line in logger.infos)
    assert any("[MOCK PUBLISH]" in line and "topic/y" in line for line in logger.infos)


def test_shared_queue_closed_sentinel() -> None:
    from neoedgex._internal import messenger as messenger_mod
    from neoedgex._internal import mock_messenger as mock_mod
    assert mock_mod._QUEUE_CLOSED is messenger_mod._QUEUE_CLOSED
    sdk = SDK()
    assert sdk.queue_closed_sentinel() is messenger_mod._QUEUE_CLOSED


def test_app_quickstart_style_handler_runs_in_mock_mode() -> None:
    config = MockConfig(
        nodes=[make_node()],
        mock=MockSection(
            message_interval="0.01s",
            messages=[
                MockMessage(
                    node_id="node-1",
                    handle="input1",
                    data={"value": PortFieldData(type=DataType.INT64, format=DataFormat.INT64, value="7")},
                )
            ],
        ),
    )

    class Handler:
        def handle(self, ctx) -> None:
            for msg in ctx.messages():
                ctx.publish("output1", {"value": 7, "status": "ok"})
                ctx._sdk.shutdown()
                break

    app = App(Handler())
    app.enable_mock(config)

    thread = threading.Thread(target=app.run, daemon=True)
    thread.start()
    time.sleep(0.2)
    thread.join(timeout=1.0)


def test_app_disable_sdk_log_uses_noop_logger() -> None:
    app = App(type("Handler", (), {"handle": lambda self, ctx: None})())
    assert app.disable_sdk_log() is app
    assert app._disable_sdk_log is True


def test_logger_default_level_is_debug(monkeypatch) -> None:
    import importlib
    import logging
    import neoedgex._internal.logger as logger_mod

    monkeypatch.delenv("NEOEDGEX_LOG_LEVEL", raising=False)
    monkeypatch.setattr(logger_mod, "_CONFIGURED", False)
    root = logging.getLogger()
    saved_level = root.level
    saved_handlers = list(root.handlers)
    for h in saved_handlers:
        root.removeHandler(h)
    try:
        logger_mod._ensure_logging()
        assert root.level == logging.DEBUG
    finally:
        for h in list(root.handlers):
            root.removeHandler(h)
        for h in saved_handlers:
            root.addHandler(h)
        root.setLevel(saved_level)
        importlib.reload(logger_mod)


def test_logger_env_override_takes_precedence(monkeypatch) -> None:
    import importlib
    import logging
    import neoedgex._internal.logger as logger_mod

    monkeypatch.setenv("NEOEDGEX_LOG_LEVEL", "WARNING")
    monkeypatch.setattr(logger_mod, "_CONFIGURED", False)
    root = logging.getLogger()
    saved_level = root.level
    saved_handlers = list(root.handlers)
    for h in saved_handlers:
        root.removeHandler(h)
    try:
        logger_mod._ensure_logging()
        assert root.level == logging.WARNING
    finally:
        for h in list(root.handlers):
            root.removeHandler(h)
        for h in saved_handlers:
            root.addHandler(h)
        root.setLevel(saved_level)
        importlib.reload(logger_mod)


def test_parse_duration_accepts_floats_and_more_units() -> None:
    from neoedgex._internal.sdk import _parse_duration_seconds

    assert _parse_duration_seconds("1.5s") == pytest.approx(1.5)
    assert _parse_duration_seconds("0.5m") == pytest.approx(30.0)
    assert _parse_duration_seconds("2.5h") == pytest.approx(2.5 * 3600.0)
    assert _parse_duration_seconds("250ms") == pytest.approx(0.25)
    assert _parse_duration_seconds("500us") == pytest.approx(0.0005)
    assert _parse_duration_seconds("500µs") == pytest.approx(0.0005)
    assert _parse_duration_seconds("123ns") == pytest.approx(1.23e-7)
    assert _parse_duration_seconds("") is None
    assert _parse_duration_seconds("bogus") is None
    assert _parse_duration_seconds("10x") is None


def test_sdk_run_running_flag_not_reset_by_concurrent_caller() -> None:
    sdk = SDK()
    messenger = FakeMessenger()
    sdk._messenger = messenger

    started = threading.Event()
    can_finish = threading.Event()

    def on_connected() -> None:
        started.set()
        can_finish.wait(timeout=2.0)

    runner = threading.Thread(target=sdk.run, args=(on_connected,), daemon=True)
    runner.start()
    assert started.wait(timeout=2.0)

    with pytest.raises(RuntimeError, match="already running"):
        sdk.run()
    assert sdk._is_running is True

    can_finish.set()
    sdk.shutdown()
    runner.join(timeout=2.0)
    assert sdk._is_running is False


def test_sdk_signal_handler_self_restores_after_first_signal() -> None:
    if not hasattr(signal, "SIGTERM"):
        pytest.skip("SIGTERM not available")

    sdk = SDK()
    messenger = FakeMessenger()
    sdk._messenger = messenger

    previous = signal.getsignal(signal.SIGTERM)
    try:
        sdk._register_signal_handlers()
        installed = signal.getsignal(signal.SIGTERM)
        assert getattr(installed, "__self__", None) is sdk
        assert getattr(installed, "__func__", None) is SDK._handle_signal

        sdk._handle_signal(signal.SIGTERM, None)
        assert sdk.shutdown_event().is_set()
        assert signal.getsignal(signal.SIGTERM) is previous
    finally:
        signal.signal(signal.SIGTERM, previous)
        sdk._signal_handlers.clear()


def test_app_init_rejects_none_handler() -> None:
    with pytest.raises(TypeError, match="handler must not be None"):
        App(None)  # type: ignore[arg-type]


class _RecordingLogger:
    def __init__(self) -> None:
        self.warns: list[str] = []

    def tag(self) -> str:
        return "test"

    def debug(self, _msg: str, *_args: object) -> None:
        return None

    def info(self, _msg: str, *_args: object) -> None:
        return None

    def warn(self, msg: str, *args: object) -> None:
        self.warns.append(msg % args if args else msg)

    def error(self, _msg: str, *_args: object) -> None:
        return None


class _RecordingSDK(FakeSDK):
    def __init__(self, messenger: FakeMessenger) -> None:
        super().__init__(messenger)
        self.recording_logger = _RecordingLogger()

    def new_logger(self, _tag: str) -> _RecordingLogger:
        return self.recording_logger


def test_publish_warns_when_output_field_missing_from_data() -> None:
    sdk = _RecordingSDK(FakeMessenger())
    instance = NodeInstance(sdk, make_node())
    instance.publish("output1", {"value": 7})
    assert any("'status' not provided" in line for line in sdk.recording_logger.warns), sdk.recording_logger.warns


def test_publish_warns_when_output_field_provided_as_nil() -> None:
    sdk = _RecordingSDK(FakeMessenger())
    instance = NodeInstance(sdk, make_node())
    instance.publish("output1", {"value": None, "status": "ok"})
    assert any("'value' provided with nil value" in line for line in sdk.recording_logger.warns), sdk.recording_logger.warns


def test_publish_warns_when_data_contains_tag_not_in_output_schema() -> None:
    sdk = _RecordingSDK(FakeMessenger())
    instance = NodeInstance(sdk, make_node())
    instance.publish("output1", {"value": 7, "status": "ok", "extra": "x"})
    assert any("'extra' is not defined in the output schema" in line for line in sdk.recording_logger.warns), sdk.recording_logger.warns


def test_app_run_propagates_connect_error_and_does_not_invoke_handler() -> None:
    handler_called = threading.Event()

    class Handler:
        def handle(self, ctx) -> None:
            handler_called.set()

    config = MockConfig(
        nodes=[make_node()],
        mock=MockSection(message_interval="1s", messages=[]),
    )

    app = App(Handler())
    app.enable_mock(config)

    original_init = SDK.enable_mock

    def patched_enable_mock(self, cfg) -> None:
        original_init(self, cfg)
        messenger = self._messenger

        def fail_connect() -> None:
            raise RuntimeError("forced connect failure")

        messenger.connect = fail_connect  # type: ignore[method-assign]

    threads_before = threading.active_count()

    SDK.enable_mock = patched_enable_mock  # type: ignore[method-assign]
    try:
        with pytest.raises(RuntimeError, match="forced connect failure"):
            app.run()
    finally:
        SDK.enable_mock = original_init  # type: ignore[method-assign]

    assert handler_called.is_set() is False
    deadline = time.time() + 1.0
    while time.time() < deadline and threading.active_count() > threads_before + 2:
        time.sleep(0.01)
    leaked = [
        t
        for t in threading.enumerate()
        if not t.daemon and t is not threading.main_thread() and t.is_alive()
    ]
    assert leaked == [], f"leaked non-daemon threads: {leaked}"
