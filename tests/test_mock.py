"""Mock mode: the config file, and the injection path that turns the config's
``{type, value}`` fields into a real CBOR message a handler decodes exactly
like an upstream one — except that it is stamped source "mock" and carries no
timestamp."""

from __future__ import annotations

import math
import threading
from pathlib import Path
from typing import Any

import pytest

from neoedgex import App
from neoedgex._internal.messenger import _QUEUE_CLOSED
from neoedgex._internal.mock_messenger import MockMessenger
from neoedgex._internal.sdk import SDK
from neoedgex.contract import (
    DataType,
    Message,
    Node,
    NodeData,
    PortFieldData,
    PortFieldSchema,
    RawMessengerPayload,
)
from neoedgex.contract.codec import decode_neoflow_envelope, scan_data_map
from neoedgex.mock import MockConfig, MockMessage, MockSection, load_config
from neoedgex import load_mock_config
from support import RecordingLogger, make_node

CONFIG_PATH = Path(__file__).parent / "testdata" / "mock-config.json"

_MOCK_FIELDS = {
    "count": PortFieldData(type=DataType.INT64, value="42"),
    "ratio": PortFieldData(type=DataType.FLOAT, value="25.34"),
    "flag": PortFieldData(type=DataType.BOOL, value="true"),
    "blob": PortFieldData(type=DataType.RAW, value="aGVsbG8="),
    "name": PortFieldData(type=DataType.STRING, value="sensor-1"),
    "nothing": PortFieldData.empty(),
}

_MOCK_PLAN = {
    "count": DataType.INT64,
    "ratio": DataType.FLOAT,
    "flag": DataType.BOOL,
    "blob": DataType.RAW,
    "name": DataType.STRING,
    "nothing": DataType.DOUBLE,
}


# -----------------------------------------------------------------------------
# config file
# -----------------------------------------------------------------------------


def test_load_config_valid() -> None:
    config = load_config(CONFIG_PATH)
    assert len(config.nodes) == 1
    assert config.nodes[0].id == "test-node-1"
    assert len(config.mock.messages) == 1
    assert config.mock.message_interval == "1s"


def test_load_config_reads_types_and_ignores_the_legacy_format_key() -> None:
    # The checked-in config still carries "format" entries from the pre-2.0.0
    # wire format; they must be ignored, not rejected.
    config = load_config(CONFIG_PATH)
    schema = config.nodes[0].data.inputs["input1"]
    assert schema == [PortFieldSchema(key="temperature", type=DataType.DOUBLE)]
    field = config.mock.messages[0].data["temperature"]
    assert field == PortFieldData(type=DataType.DOUBLE, value="2.55e+01")
    # Round-tripping drops the legacy key for good.
    assert config.to_dict()["mock"]["messages"][0]["data"]["temperature"] == {
        "type": "double",
        "value": "2.55e+01",
    }


def test_top_level_load_mock_config() -> None:
    assert load_mock_config(CONFIG_PATH).nodes[0].id == "test-node-1"


def test_load_config_file_not_found() -> None:
    with pytest.raises(ValueError, match="read mock config"):
        load_config("nonexistent.json")


def test_load_config_invalid_json(tmp_path: Path) -> None:
    path = tmp_path / "broken.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError, match="parse mock config"):
        load_config(path)


def test_load_config_empty_nodes(tmp_path: Path) -> None:
    path = tmp_path / "empty.json"
    path.write_text('{"nodes":[]}', encoding="utf-8")
    with pytest.raises(ValueError, match="nodes must not be empty"):
        load_config(path)


# -----------------------------------------------------------------------------
# injection: {type, value} -> CBOR -> native values
# -----------------------------------------------------------------------------


def test_injected_message_round_trips_to_native_values() -> None:
    messenger = MockMessenger(RecordingLogger())
    subscriber = messenger.add_subscriber("node-1")
    messenger.inject_neoflow_message("node-1", "input1", _MOCK_FIELDS)

    payload = subscriber.get_nowait()
    assert isinstance(payload, RawMessengerPayload)
    assert payload.handle == "input1"

    source, timestamp, data = decode_neoflow_envelope(payload.data)
    # Handlers can tell an injected message from a real one by its source;
    # injected messages carry no timestamp.
    assert source == "mock"
    assert timestamp == ""

    message = Message(source, timestamp, handle=payload.handle, raw=data, plan=_MOCK_PLAN)
    assert message.to_dict() == {
        "count": 42,
        "ratio": 25.34,
        "flag": True,
        "blob": b"hello",
        "name": "sensor-1",
        "nothing": None,
    }


def test_injection_reports_a_field_the_config_cannot_parse() -> None:
    messenger = MockMessenger(RecordingLogger())
    messenger.add_subscriber("node-1")
    with pytest.raises(ValueError, match="mock field 'bad'"):
        messenger.inject_neoflow_message(
            "node-1", "input1", {"bad": PortFieldData(type=DataType.INT64, value="oops")}
        )


def test_injection_requires_a_subscribed_node() -> None:
    messenger = MockMessenger(RecordingLogger())
    with pytest.raises(ValueError, match="node 'node-1' is not subscribed"):
        messenger.inject_neoflow_message("node-1", "input1", {})


def test_mock_messenger_reuses_one_subscriber_per_node() -> None:
    messenger = MockMessenger(RecordingLogger())
    assert messenger.add_subscriber("node-1") is messenger.add_subscriber("node-1")


def test_mock_messenger_wakes_subscribers_on_disconnect_and_removal() -> None:
    messenger = MockMessenger(RecordingLogger())
    first = messenger.add_subscriber("node-1")
    second = messenger.add_subscriber("node-2")
    messenger.disconnect()
    assert first.get_nowait() is _QUEUE_CLOSED
    assert second.get_nowait() is _QUEUE_CLOSED

    messenger.remove_subscriber("node-1")
    assert first.get_nowait() is _QUEUE_CLOSED


def test_mock_messenger_publish_logs_both_wire_formats() -> None:
    logger = RecordingLogger()
    messenger = MockMessenger(logger)
    # A data message (CBOR), the error topic (JSON) and the heartbeat (empty).
    messenger.publish("neoedgex/neoflow/out/node-1/output1", 2, bytes.fromhex("a1616b01"))
    messenger.publish("neoedgex/neoflow/error/node-1", 0, b'{"code": "PROCESS_ERROR"}')
    messenger.publish("neoedgex/neoflow/heartbeat/node-1", 0, b"")

    assert len(logger.infos) == 3
    assert all("[MOCK PUBLISH]" in line for line in logger.infos)
    assert "'k': 1" in logger.infos[0]
    assert "PROCESS_ERROR" in logger.infos[1]


# -----------------------------------------------------------------------------
# mock mode end to end
# -----------------------------------------------------------------------------


def _mock_config(*messages: MockMessage, node: Node | None = None) -> MockConfig:
    return MockConfig(
        nodes=[node or make_node()],
        mock=MockSection(message_interval="0.01s", messages=list(messages)),
    )


def test_sdk_injects_configured_messages_round_robin() -> None:
    sdk = SDK()
    sdk.enable_mock(
        _mock_config(
            MockMessage(
                node_id="node-1",
                handle="input1",
                data={"value": PortFieldData(type=DataType.INT64, value="1")},
            ),
            MockMessage(
                node_id="node-1",
                handle="input1",
                data={"value": PortFieldData(type=DataType.INT64, value="2")},
            ),
        )
    )
    subscriber = sdk.messenger().add_subscriber("node-1")
    sdk.start_message_injection()
    try:
        values = []
        for _ in range(3):
            payload = subscriber.get(timeout=2.0)
            _, _, data = decode_neoflow_envelope(payload.data)
            values.append(Message(raw=data, plan={"value": DataType.INT64}).to_dict()["value"])
        # Third message wraps around to the first.
        assert values == [1, 2, 1]
    finally:
        sdk.shutdown()


def test_handler_receives_injected_message_with_native_values() -> None:
    node = Node(
        id="node-1",
        type="demo",
        data=NodeData(
            name="demo-node",
            inputs={
                "input1": [
                    PortFieldSchema(key="count", type=DataType.INT64),
                    PortFieldSchema(key="ratio", type=DataType.FLOAT),
                ]
            },
            outputs={"output1": [PortFieldSchema(key="value", type=DataType.INT64)]},
        ),
    )
    config = _mock_config(
        MockMessage(
            node_id="node-1",
            handle="input1",
            data={
                "count": PortFieldData(type=DataType.INT64, value="42"),
                "ratio": PortFieldData(type=DataType.FLOAT, value="25.34"),
            },
        ),
        node=node,
    )

    observed: list[dict[str, Any]] = []
    done = threading.Event()

    class Handler:
        def handle(self, ctx: Any) -> None:
            for message in ctx.messages():
                observed.append(
                    {
                        "source": message.source,
                        "timestamp": message.timestamp,
                        "handle": message.handle,
                        "data": message.to_dict(),
                    }
                )
                ctx.publish("output1", {"value": 7})
                done.set()
                ctx._sdk.shutdown()
                break

    app = App(Handler()).disable_sdk_log()
    app.enable_mock(config)

    thread = threading.Thread(target=app.run, daemon=True)
    thread.start()
    assert done.wait(timeout=5.0), "handler did not observe an injected message in time"
    thread.join(timeout=2.0)

    assert observed == [
        {
            "source": "mock",
            "timestamp": "",
            "handle": "input1",
            "data": {"count": 42, "ratio": 25.34},
        }
    ]


def test_f6_mock_config_nan_reaches_the_handler_as_nan() -> None:
    """F6: ``{"type": "double", "value": "nan"}`` is a legal mock config value
    in both SDKs — the config channel parses NaN like Go ParseFloat, the
    injected CBOR carries it, and the handler sees NaN. Go parity: the publish
    path still refuses to *send* NaN, only this local channel delivers it."""
    node = Node(
        id="node-1",
        type="demo",
        data=NodeData(
            name="demo-node",
            inputs={"input1": [PortFieldSchema(key="reading", type=DataType.DOUBLE)]},
            outputs={"output1": [PortFieldSchema(key="value", type=DataType.INT64)]},
        ),
    )
    field = PortFieldData.from_dict({"type": "double", "value": "nan"})
    assert field == PortFieldData(type=DataType.DOUBLE, value="nan")
    config = _mock_config(
        MockMessage(node_id="node-1", handle="input1", data={"reading": field}), node=node
    )

    observed: list[Any] = []
    done = threading.Event()

    class Handler:
        def handle(self, ctx: Any) -> None:
            for message in ctx.messages():
                observed.append(message.to_dict()["reading"])
                done.set()
                ctx._sdk.shutdown()
                break

    app = App(Handler()).disable_sdk_log()
    app.enable_mock(config)

    thread = threading.Thread(target=app.run, daemon=True)
    thread.start()
    assert done.wait(timeout=5.0), "handler did not observe an injected message in time"
    thread.join(timeout=2.0)

    assert len(observed) == 1
    assert math.isnan(observed[0]), observed


def test_f6_mock_injection_puts_nan_on_the_wire_as_a_double() -> None:
    """F6: the same value at the wire level — the injected data map carries the
    canonical quiet-NaN double, not a null or a dropped field."""
    messenger = MockMessenger(RecordingLogger())
    subscriber = messenger.add_subscriber("node-1")
    messenger.inject_neoflow_message(
        "node-1", "input1", {"reading": PortFieldData(type=DataType.DOUBLE, value="nan")}
    )
    _, _, data = decode_neoflow_envelope(subscriber.get_nowait().data)
    assert scan_data_map(data)["reading"].hex() == "fb7ff8000000000000"


def test_mock_mode_publish_is_logged_instead_of_brokered() -> None:
    logger = RecordingLogger()
    sdk = SDK()
    sdk.enable_mock(_mock_config())
    sdk._messenger._logger = logger
    sdk.messenger().publish("neoedgex/neoflow/out/node-1/output1", 2, bytes.fromhex("a1616b01"))
    assert any("[MOCK PUBLISH]" in line for line in logger.infos)
