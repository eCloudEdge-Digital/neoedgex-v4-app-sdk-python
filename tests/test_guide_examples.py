"""The developer guide's runnable examples, executed verbatim.

Each test carries one code block from ``docs/developer-guide.en.md`` (the
zh-tw guide holds the same code with localized comments) and asserts the
behavior the guide claims next to it. The guide marks every covered block
with "executed as-is by tests/test_guide_examples.py"; when the SDK drifts
from the documented behavior, this file goes red before a reader can be
misled. Everything runs on ``testutil`` — no platform, no broker.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

import neoedgex
from neoedgex import DataType, convert_to_typed_value, mock, testutil
from neoedgex.contract import Node, NodeData, PortFieldSchema
from neoedgex.testutil import MockNodeEnv, PublishedMessage

# -----------------------------------------------------------------------------
# Quick Start / Unit Test Helper
# -----------------------------------------------------------------------------


class ExampleApp:
    def handle(self, ctx: neoedgex.NodeEnv) -> None:
        for _msg in ctx.messages():
            try:
                ctx.publish("output1", {"power": 42.0})
            except Exception as err:
                ctx.report_error(neoedgex.CodeProcessError, err)


def test_example_app() -> None:
    """Guide: Quick Start handler, run through the Unit Test Helper example."""
    env = MockNodeEnv(
        config=Node(
            id="node-1",
            data=NodeData(
                name="demo-node",
                inputs={"input1": [PortFieldSchema(key="temperature", type=DataType.DOUBLE)]},
                outputs={"output1": [PortFieldSchema(key="power", type=DataType.DOUBLE)]},
            ),
        )
    )
    env.message_iterable = [env.new_message("input1", {"temperature": 25.5})]

    ExampleApp().handle(env)

    assert env.published_data == [PublishedMessage(handle="output1", data={"power": 42.0})]
    assert env.reported_errors == []


# -----------------------------------------------------------------------------
# Reading Input Values
# -----------------------------------------------------------------------------


def test_decoding_a_double_and_a_string_field() -> None:
    """Guide: Reading Input Values, the first decode example."""
    msg = testutil.new_message("input1", {
        "temperature": (25.5, DataType.DOUBLE),
        "deviceName": ("sensor-1", DataType.STRING),
    })
    assert msg.handle == "input1"
    assert msg.source == "upstream-node"

    data = msg.to_dict()
    assert data == {"temperature": 25.5, "deviceName": "sensor-1"}


class TemperatureApp:
    def handle(self, ctx: neoedgex.NodeEnv) -> None:
        for msg in ctx.messages():
            if msg.handle != "input1":
                # Handle not defined in the schema: ignore it.
                continue

            data = msg.to_dict()
            if "temperature" not in data:
                ctx.report_error(
                    neoedgex.CodeProcessError,
                    RuntimeError("internal error: input1 schema does not define tag temperature"),
                )
                continue
            value = data["temperature"]
            if value is None:
                ctx.report_error(
                    neoedgex.CodeProcessError,
                    RuntimeError("temperature was not successfully produced by the upstream node"),
                )
                continue
            if not isinstance(value, float):
                ctx.report_error(
                    neoedgex.CodeProcessError,
                    RuntimeError("internal error: tag temperature has an unexpected type, expected float"),
                )
                continue

            ctx.publish("output1", {"power": value * 2})


def test_defensive_reading_pattern() -> None:
    """Guide: Reading Input Values, the defensive handler — one message per
    branch: a good value, an undefined value, a missing key, a wrong type,
    and a handle outside the dispatch."""
    env = MockNodeEnv(
        config=Node(
            id="node-1",
            data=NodeData(
                name="demo-node",
                inputs={"input1": [PortFieldSchema(key="temperature", type=DataType.DOUBLE)]},
                outputs={"output1": [PortFieldSchema(key="power", type=DataType.DOUBLE)]},
            ),
        )
    )
    env.message_iterable = [
        env.new_message("input1", {"temperature": 25.5}),
        env.new_message("input1", {"temperature": None}),
        testutil.new_message("input1", {"other": (1, testutil.UNDECLARED)}),
        testutil.new_message("input1", {"temperature": ("25.5", testutil.UNDECLARED)}),
        testutil.new_message("input2", {"temperature": (25.5, DataType.DOUBLE)}),
    ]

    TemperatureApp().handle(env)

    assert env.published_data == [PublishedMessage(handle="output1", data={"power": 51.0})]
    assert [str(reported.err) for reported in env.reported_errors] == [
        "temperature was not successfully produced by the upstream node",
        "internal error: input1 schema does not define tag temperature",
        "internal error: tag temperature has an unexpected type, expected float",
    ]


# -----------------------------------------------------------------------------
# Decoding into a Dataclass
# -----------------------------------------------------------------------------


def test_to_dataclass_reading_example() -> None:
    """Guide: Decoding into a Dataclass, the full example — the Python
    counterpart of the Go guide's ExampleMessage_ToStruct."""
    msg = testutil.new_message("input1", {
        "temperature": (None, DataType.DOUBLE),   # upstream produced no value
        "offset": (0.0, DataType.DOUBLE),         # upstream produced a real 0
        "count": (None, DataType.INT64),          # upstream produced no value
        "ratio": (testutil.Single(25.34), DataType.FLOAT),
        "level": (25.34, DataType.DOUBLE),
        "restored": (testutil.Single(25.34), testutil.UNDECLARED),
        "seq": (5, testutil.UNDECLARED),
        "total": (18446744073709551615, testutil.UNDECLARED),
        "deviceName": ("sensor-1", testutil.UNDECLARED),
        "running": (True, testutil.UNDECLARED),
        "payload": (b"\x01\x02", testutil.UNDECLARED),
    })

    @dataclass
    class Reading:
        temperature: float | None = None  # no value -> the default None stands
        offset: float | None = None       # real 0 -> 0.0
        count: int = 0                    # no value -> default 0: a real 0 looks the same
        ratio: float = 0.0                # sent at single precision -> restored 25.34
        level: float = 0.0                # sent at double precision -> 25.34
        restored: float = 0.0             # not declared in the schema; the annotation
                                          # still wins -> restored 25.34 (to_dict(),
                                          # trusting the schema, would widen it)
        seq: int = 0                      # not declared -> 5
        total: Any = 0                    # above the int64 domain: an `int`
                                          # annotation would raise; Any takes the
                                          # natural int
        device_name: str = field(default="", metadata={"key": "deviceName"})
        running: bool = False             # not declared -> True
        payload: bytes = b""              # not declared -> b"\x01\x02"

    reading = msg.to_dataclass(Reading)
    assert reading == Reading(
        temperature=None,
        offset=0.0,
        count=0,
        ratio=25.34,
        level=25.34,
        restored=25.34,
        seq=5,
        total=18446744073709551615,
        device_name="sensor-1",
        running=True,
        payload=b"\x01\x02",
    )


def test_to_dataclass_incompatibility_rule() -> None:
    """Guide: Decoding into a Dataclass, the Strict / Pointer / Loose example."""
    # The upstream node sent text where the dataclass expects a number.
    msg = testutil.new_message("input1", {"count": ("not-a-number", DataType.STRING)})

    @dataclass
    class Strict:
        count: int = 0

    @dataclass
    class Pointer:
        count: int | None = None

    @dataclass
    class Loose:
        count: Any = None

    with pytest.raises(ValueError):   # bare int: the whole call aborts
        msg.to_dataclass(Strict)

    with pytest.raises(ValueError):   # int | None aborts the same way
        msg.to_dataclass(Pointer)

    assert msg.to_dataclass(Loose).count == "not-a-number"  # Any: delivered as-is


# -----------------------------------------------------------------------------
# Python Value Conversion
# -----------------------------------------------------------------------------


def test_conversion_table_rows() -> None:
    """Guide: Python Value Conversion, the runnable rows."""
    assert convert_to_typed_value(9527, DataType.BOOL) is True
    assert convert_to_typed_value(12.9, DataType.INT64) == 12
    assert convert_to_typed_value("42", DataType.INT16) == 42
    assert convert_to_typed_value(25.5, DataType.STRING) == "2.55e+01"
    with pytest.raises(ValueError):
        convert_to_typed_value(70000, DataType.INT16)
    with pytest.raises(ValueError):
        convert_to_typed_value(float("nan"), DataType.DOUBLE)


# -----------------------------------------------------------------------------
# Mock Development Flow
# -----------------------------------------------------------------------------

GUIDE_MOCK_CONFIG = """\
{
  "nodes": [
    {
      "id": "node-1",
      "type": "app",
      "data": {
        "name": "demo-node",
        "inputs": {
          "input1": [
            { "key": "temperature", "type": "double" }
          ]
        },
        "outputs": {
          "output1": [
            { "key": "value", "type": "string" }
          ]
        },
        "application": {
          "key": "demo-app",
          "version": "2.0.0"
        },
        "settings": {}
      }
    }
  ],
  "mock": {
    "messageInterval": "3s",
    "messages": [
      {
        "nodeID": "node-1",
        "handle": "input1",
        "data": {
          "temperature": {
            "type": "double",
            "value": "2.55e+01"
          }
        }
      }
    ]
  }
}
"""


def test_minimal_mock_config_loads(tmp_path: Path) -> None:
    """Guide: Mock Development Flow, the minimal mock config shape."""
    config_path = tmp_path / "mock-config.json"
    config_path.write_text(GUIDE_MOCK_CONFIG, encoding="utf-8")

    config = mock.load_config(config_path)

    node = config.nodes[0]
    assert node.id == "node-1"
    assert node.data.inputs["input1"] == [
        PortFieldSchema(key="temperature", type=DataType.DOUBLE)
    ]
    assert node.data.outputs["output1"] == [
        PortFieldSchema(key="value", type=DataType.STRING)
    ]
    assert config.mock.message_interval == "3s"
    message = config.mock.messages[0]
    assert (message.node_id, message.handle) == ("node-1", "input1")
    assert message.data["temperature"].type == DataType.DOUBLE
    assert message.data["temperature"].value == "2.55e+01"
