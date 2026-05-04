from __future__ import annotations

import threading

import pytest

from neoedgex import CodeProcessError, Message
from neoedgex.testutil import MockNodeEnv


def test_mock_node_env_records_handler_interactions() -> None:
    messages = [
        Message(handle="input1", data={"value": 7}, source="source-node"),
    ]
    done_event = threading.Event()
    ctx = MockNodeEnv(message_iterable=messages, done_event=done_event)

    assert list(ctx.messages()) == messages
    assert ctx.context() is done_event
    assert ctx.logger().tag() == "test"

    ctx.publish({"value": 7})
    ctx.report_error(CodeProcessError, RuntimeError("boom"))
    ctx.stop()

    assert ctx.published_data == [{"value": 7}]
    assert ctx.reported_errors[0].code == CodeProcessError
    assert ctx.stop_called is True
    assert done_event.is_set()


def test_mock_node_env_publish_error() -> None:
    ctx = MockNodeEnv(publish_error=RuntimeError("publish failed"))

    with pytest.raises(RuntimeError, match="publish failed"):
        ctx.publish({"value": 7})

    assert ctx.published_data == [{"value": 7}]
