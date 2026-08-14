"""``neoedgex.testutil`` is public API: it is what an application's own tests
use to build the message a handler receives, so its behaviour — including its
error messages, which are the only guidance a test author gets — is pinned
here."""

from __future__ import annotations

import threading

import cbor2
import pytest

from neoedgex import CodeProcessError
from neoedgex.contract import DataType, Message, Node, NodeData, PortFieldSchema
from neoedgex.testutil import (
    UNDECLARED,
    MockNodeEnv,
    NoopLogger,
    PublishedMessage,
    Single,
    new_message,
)
from support import RecordingLogger


def _config() -> Node:
    return Node(
        id="node-1",
        type="demo",
        data=NodeData(
            name="demo-node",
            inputs={
                "input1": [
                    PortFieldSchema(key="value", type=DataType.INT64),
                    PortFieldSchema(key="ratio", type=DataType.FLOAT),
                    PortFieldSchema(key="absent", type=DataType.STRING),
                ]
            },
            outputs={"output1": [PortFieldSchema(key="value", type=DataType.INT64)]},
        ),
    )


# -----------------------------------------------------------------------------
# new_message
# -----------------------------------------------------------------------------


def test_new_message_builds_the_message_the_runtime_would_build() -> None:
    message = new_message("input1", {"value": (42, DataType.INT64)})
    assert isinstance(message, Message)
    assert message.handle == "input1"
    assert message.source == "upstream-node"
    assert message.timestamp == "2026-01-01T00:00:00.000Z"
    assert message.to_dict() == {"value": 42}


def test_new_message_encodes_by_the_python_type_not_the_declared_type() -> None:
    # The sending and receiving schemas are independent in production, so the
    # value's Python type decides the encoding: Single -> 0xfa, a bare float
    # -> 0xfb, None -> 0xf6, whatever the declared type says.
    message = new_message(
        "input1",
        {
            "single": (Single(25.34), DataType.FLOAT),
            "double": (25.34, DataType.DOUBLE),
            "nil": (None, DataType.INT64),
        },
    )
    assert message.raw.hex() == (
        "a3"
        "6673696e676c65" "fa41cab852"
        "66646f75626c65" "fb4039570a3d70a3d7"
        "636e696c" "f6"
    )
    assert message.to_dict() == {"single": 25.34, "double": 25.34, "nil": None}


def test_undeclared_keeps_the_key_out_of_the_decode_plan() -> None:
    message = new_message(
        "input1",
        {"declared": (Single(25.34), DataType.FLOAT), "bypassed": (Single(25.34), UNDECLARED)},
    )
    # Same bytes on the wire, different delivery: the declared field is
    # restored to the decimal the float32 carries, the bypassed one arrives in
    # its natural (widened) domain.
    assert message.to_dict() == {"declared": 25.34, "bypassed": 25.34000015258789}
    assert repr(UNDECLARED) == "testutil.UNDECLARED"


def test_new_message_requires_a_declared_type_or_the_undeclared_marker() -> None:
    with pytest.raises(ValueError, match="no declared type"):
        new_message("input1", {"value": (1, DataType.UNDEFINED)})
    with pytest.raises(ValueError, match="not a declarable schema type"):
        new_message("input1", {"value": (1, "int64")})
    with pytest.raises(TypeError, match=r"expected a \(wire value, declared type\) tuple"):
        new_message("input1", {"value": 1})


def test_r5_a_container_value_reaches_the_wire_and_decodes_to_undefined() -> None:
    """R5: a container is a legal CBOR item, so it is *encoded*, not refused —
    the Go testutil marshals an arbitrary value the same way. What it cannot be
    is a NeoFlow value: on the declared path it fails the schema decode and on
    the bypass path it has no natural domain, so both deliver None."""
    declared = new_message("input1", {"value": ({"a": 1}, DataType.STRING)})
    assert cbor2.loads(declared.raw) == {"value": {"a": 1}}  # encoded, not refused
    assert declared.to_dict() == {"value": None}

    bypassed = new_message("input1", {"value": ([1, 2], UNDECLARED)})
    assert cbor2.loads(bypassed.raw) == {"value": [1, 2]}
    assert bypassed.to_dict() == {"value": None}


def test_new_message_rejects_values_it_cannot_put_on_the_wire() -> None:
    # Integers outside [-2**63, 2**64-1] have no CBOR integer encoding in the
    # message domain, and the SDK never emits a bignum (D6).
    with pytest.raises(ValueError, match="no CBOR integer encoding"):
        new_message("input1", {"value": (2**64, DataType.UINT64)})
    with pytest.raises(ValueError, match="no CBOR integer encoding"):
        new_message("input1", {"value": (-(2**63) - 1, DataType.INT64)})
    with pytest.raises(ValueError, match="out of float32 range"):
        new_message("input1", {"value": (Single(1e300), DataType.FLOAT)})


def test_new_message_attaches_the_given_logger() -> None:
    logger = RecordingLogger()
    message = new_message("input1", {"value": ("abc", DataType.INT64)}, logger=logger)
    assert message.to_dict() == {"value": None}
    assert any(line.startswith("Field 'value':") for line in logger.warns), logger.warns

    # Without one, decoding stays silent instead of failing on a missing logger.
    assert new_message("input1", {"value": ("abc", DataType.INT64)}).to_dict() == {"value": None}


def test_noop_logger_satisfies_the_logger_protocol() -> None:
    logger = NoopLogger("unit")
    assert logger.tag() == "unit"
    for level in (logger.debug, logger.info, logger.warn, logger.error):
        assert level("msg %s", 1) is None


# -----------------------------------------------------------------------------
# MockNodeEnv
# -----------------------------------------------------------------------------


def test_mock_node_env_new_message_reads_the_types_from_the_config() -> None:
    env = MockNodeEnv(config=_config())
    message = env.new_message("input1", {"value": 7, "ratio": Single(25.34), "extra": "x"})

    assert message.handle == "input1"
    # value/ratio come from the schema; "absent" is declared but was never
    # produced; "extra" is not declared and takes the bypass path.
    assert message.to_dict() == {"value": 7, "ratio": 25.34, "absent": None, "extra": "x"}
    assert set(cbor2.loads(message.raw)) == {"value", "ratio", "extra"}


def test_mock_node_env_new_message_rejects_an_undeclared_handle() -> None:
    env = MockNodeEnv(config=_config())
    with pytest.raises(ValueError, match="is not declared in config.data.inputs"):
        env.new_message("output1", {})
    with pytest.raises(ValueError, match=r"declared: \['input1'\]"):
        env.new_message("nope", {})


def test_mock_node_env_new_message_uses_the_env_logger_unless_overridden() -> None:
    env_logger = RecordingLogger()
    override = RecordingLogger()
    env = MockNodeEnv(config=_config(), mock_logger=env_logger)

    env.new_message("input1", {"value": "abc"}).to_dict()
    assert any(line.startswith("Field 'value':") for line in env_logger.warns)

    env.new_message("input1", {"value": "abc"}, logger=override).to_dict()
    assert any(line.startswith("Field 'value':") for line in override.warns)
    assert len(env_logger.warns) == 1


def test_mock_node_env_records_handler_interactions() -> None:
    messages = [new_message("input1", {"value": (7, DataType.INT64)})]
    done_event = threading.Event()
    env = MockNodeEnv(message_iterable=messages, done_event=done_event)

    assert list(env.messages()) == messages
    assert env.node_config() == Node()
    assert env.context() is done_event
    assert env.logger().tag() == "test"

    env.publish("output1", {"value": 7})
    env.report_error(CodeProcessError, RuntimeError("boom"))
    env.stop()

    assert env.published_data == [PublishedMessage(handle="output1", data={"value": 7})]
    assert env.reported_errors[0].code == CodeProcessError
    assert str(env.reported_errors[0].err) == "boom"
    assert env.stop_called is True


def test_r4_stop_records_the_call_without_cancelling_the_context() -> None:
    """R4: Go's MockNodeEnv.Stop leaves ctx.Err() nil — it records the call and
    nothing else. A test that wants the handler to observe cancellation sets
    done_event itself, which is the only thing that makes context() fire."""
    done_event = threading.Event()
    env = MockNodeEnv(done_event=done_event)

    env.stop()
    assert env.stop_called is True
    assert env.context() is done_event
    assert not done_event.is_set()

    done_event.set()
    assert env.context().is_set()


def test_mock_node_env_publish_error() -> None:
    env = MockNodeEnv(publish_error=RuntimeError("publish failed"))

    with pytest.raises(RuntimeError, match="publish failed"):
        env.publish("output1", {"value": 7})

    # The publish is recorded before the failure surfaces, so a handler test
    # can still assert what was attempted.
    assert env.published_data == [PublishedMessage(handle="output1", data={"value": 7})]
