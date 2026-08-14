"""Runtime tests: the publish path (schema-driven native conversion -> CBOR),
the receive loop, and the SDK / app lifecycle around them.

Only data messages are CBOR: the error topic stays JSON and the heartbeat
stays an empty payload, which the wire-shape tests below pin down.
"""

from __future__ import annotations

import json
import re
import signal
import threading
import time
from datetime import datetime, timedelta, timezone

import cbor2
import pytest

import neoedgex
from neoedgex import App
from neoedgex._internal.logger import NoopLogger, SDKLogger
from neoedgex._internal.node import MessageStream, NodeInstance, _format_rfc3339
from neoedgex._internal.sdk import SDK
from neoedgex.contract import DataType, Message, PortFieldSchema, RawMessengerPayload
from neoedgex.contract.codec import (
    decode_neoflow_envelope,
    encode_data_map,
    encode_neoflow_message,
    scan_data_map,
)
from support import (
    FakeMessenger,
    FakeSDK,
    RecordingLogger,
    data_spans,
    envelope_of,
    make_node,
    non_utc_local_zone,  # noqa: F401  -- imported to register the fixture
)

# RFC3339 with exactly three fractional digits: the shape a fixed-width
# fraction guarantees even when the clock lands on a whole second. The renderer
# stays offset-capable for a datetime handed to it directly, so only the
# publish-output form is pinned to ``Z``.
_RFC3339 = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}(Z|[+-]\d{2}:\d{2})$")
_PUBLISHED_RFC3339 = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")

# The whole-second prefix two stamps share when only their fraction differs.
_SECOND_PRECISION_WIDTH = len("2006-01-02T15:04:05")


def _instance(logger: RecordingLogger | None = None) -> tuple[NodeInstance, FakeMessenger]:
    messenger = FakeMessenger()
    return NodeInstance(FakeSDK(messenger, logger), make_node()), messenger


def _out_payload(messenger: FakeMessenger) -> bytes:
    records = messenger.topic_records("out")
    assert len(records) == 1, [record.topic for record in messenger.published]
    return records[0].data


def test_public_exports_use_node_env_not_node_context() -> None:
    assert "NodeEnv" in neoedgex.__all__
    assert "NodeContext" not in neoedgex.__all__
    assert not hasattr(neoedgex, "NodeContext")


def test_wire_types_are_type_based_only() -> None:
    # DataFormat is gone for good: nothing may re-introduce a second, parallel
    # notion of a field's type.
    import neoedgex.contract as contract

    assert not hasattr(contract, "DataFormat")
    assert not hasattr(neoedgex, "DataFormat")
    assert {field_def.name for field_def in PortFieldSchema.__dataclass_fields__.values()} == {
        "key",
        "type",
    }


# -----------------------------------------------------------------------------
# publish
# -----------------------------------------------------------------------------


def test_publish_topic_qos_and_envelope() -> None:
    instance, messenger = _instance()
    instance.publish("output1", {"temp": 25.34, "label": "ok", "count": 7})

    record = messenger.published[0]
    assert record.topic == "neoedgex/neoflow/out/node-1/output1"
    assert record.qos == 2
    source, timestamp, _ = envelope_of(record.data)
    assert source == "node-1"
    assert _PUBLISHED_RFC3339.match(timestamp), timestamp
    # Publishing reads the clock as UTC, so the stamp cannot regress to the
    # machine's local offset — both SDKs render one instant as one string.
    assert timestamp.endswith("Z"), timestamp
    # An RFC3339 timestamp Python itself can read back, timezone included.
    assert datetime.fromisoformat(timestamp).tzinfo == timezone.utc


# The renderer is asserted directly, because the clock-driven tests below cannot
# choose which instant they observe: the whole-second case is where a variable
# fraction (``datetime.isoformat``) would silently drop it, and the expected
# strings are exactly what Go's "2006-01-02T15:04:05.000Z07:00" layout produces
# for the same instants, so the two SDKs cannot drift apart.
@pytest.mark.parametrize(
    "base, want",
    [
        pytest.param(
            datetime(2026, 3, 22, 18, 30, 0, 0, tzinfo=timezone(timedelta(hours=8))),
            "2026-03-22T18:30:00.000+08:00",
            id="whole second still carries a fraction",
        ),
        pytest.param(
            datetime(2026, 3, 22, 18, 30, 0, 123000, tzinfo=timezone(timedelta(hours=8))),
            "2026-03-22T18:30:00.123+08:00",
            id="millisecond",
        ),
        pytest.param(
            datetime(2026, 3, 22, 10, 30, 0, 5000, tzinfo=timezone.utc),
            "2026-03-22T10:30:00.005Z",
            id="leading zeros in the fraction are kept, utc renders Z",
        ),
        pytest.param(
            datetime(2026, 3, 22, 18, 30, 0, 999999, tzinfo=timezone(timedelta(hours=8))),
            "2026-03-22T18:30:00.999+08:00",
            id="sub-millisecond truncates, never rounds up",
        ),
    ],
)
def test_rfc3339_renderer_keeps_a_fixed_width_fraction(base: datetime, want: str) -> None:
    got = _format_rfc3339(base)
    assert got == want
    assert _RFC3339.match(got), got


def test_publish_timestamp_has_millisecond_precision() -> None:
    instance, messenger = _instance()

    before = datetime.now().astimezone()
    instance.publish("output1", {"temp": 25.34, "label": "ok", "count": 7})
    after = datetime.now().astimezone()

    _, timestamp, _ = envelope_of(messenger.published[0].data)
    assert _PUBLISHED_RFC3339.match(timestamp), timestamp
    assert timestamp.endswith("Z"), timestamp

    # A hardcoded ".000" would satisfy the shape check, so the stamp is also
    # pinned to the wall clock it claims to record. The lower bound is truncated
    # because the renderer truncates.
    published = datetime.fromisoformat(timestamp)
    assert published.tzinfo is not None
    assert before.replace(microsecond=(before.microsecond // 1000) * 1000) <= published <= after


def test_publish_stamps_utc_even_when_the_host_zone_is_not_utc(
    non_utc_local_zone: timedelta,
) -> None:
    """The teeth behind the ``Z`` assertions above. Those also hold on a host
    that is *already* UTC, which is what ``TZ`` unset gives most CI containers:
    there, reading the clock as local time still renders a ``Z``-terminated
    string and a regression to ``datetime.now().astimezone()`` would pass
    unnoticed. With the local zone forced off UTC, the two ways this can break
    are both fatal — a local read renders a numeric offset instead of ``Z``, and
    stamping local wall-clock digits under a ``Z`` suffix moves the instant a
    whole offset away from now.
    """
    # The harness itself is checked, so this can never pass by not being armed.
    assert non_utc_local_zone != timedelta(0), non_utc_local_zone

    instance, messenger = _instance()

    before = datetime.now(timezone.utc)
    instance.publish("output1", {"temp": 25.34, "label": "ok", "count": 7})
    after = datetime.now(timezone.utc)

    _, timestamp, _ = envelope_of(messenger.published[0].data)
    assert _PUBLISHED_RFC3339.match(timestamp), timestamp
    assert timestamp.endswith("Z"), timestamp

    published = datetime.fromisoformat(timestamp)
    assert published.tzinfo == timezone.utc
    # Not merely Z-suffixed but the correct instant: local digits mislabelled as
    # UTC would land outside this window by the offset above. Lower bound is
    # truncated because the renderer truncates.
    assert before.replace(microsecond=(before.microsecond // 1000) * 1000) <= published <= after


def test_publish_separates_messages_within_the_same_second() -> None:
    """NEO-7263: tags are polled on millisecond intervals, so two publishes
    inside the same second must not collapse onto one timestamp — which is
    exactly what the previous second-precision format did."""
    instance, messenger = _instance()

    def publish_timestamp() -> str:
        instance.publish("output1", {"temp": 25.34, "label": "ok", "count": 7})
        return envelope_of(messenger.published[-1].data)[1]

    # The pair is retried so the assertion always lands on two publishes that
    # share a second — a pair straddling a second boundary would differ even
    # under the old format and prove nothing.
    first = second = ""
    for _attempt in range(5):
        first = publish_timestamp()
        time.sleep(0.002)  # guarantees the millisecond advances
        second = publish_timestamp()
        if first[:_SECOND_PRECISION_WIDTH] == second[:_SECOND_PRECISION_WIDTH]:
            break

    assert first[:_SECOND_PRECISION_WIDTH] == second[:_SECOND_PRECISION_WIDTH], (
        f"could not observe two publishes within one second: {first!r} and {second!r}"
    )
    assert first != second, f"two publishes 2ms apart share the timestamp {first!r}"


def test_publish_keeps_timestamp_wire_compatible() -> None:
    """Interop with consumers built against the second-precision format: they
    read ``timestamp`` as text, so the CBOR major type must not change and the
    value must stay a parseable RFC3339 string. Only the content gains a
    fraction."""
    instance, messenger = _instance()
    instance.publish("output1", {"temp": 25.34, "label": "ok", "count": 7})
    payload = messenger.published[0].data

    envelope = cbor2.loads(payload)
    assert isinstance(envelope["timestamp"], str), type(envelope["timestamp"])
    # Byte-level: CBOR major type 3 (text string) lives in the top 3 bits.
    timestamp_span = scan_data_map(payload)["timestamp"]
    assert timestamp_span[0] >> 5 == 3, timestamp_span[:1].hex()

    # The strict decoder rejects a non-text timestamp, so a type change here
    # would drop the whole message on a peer node rather than one field.
    _, timestamp, data = decode_neoflow_envelope(payload)
    assert datetime.fromisoformat(timestamp).tzinfo is not None
    assert len(data) > 0


def test_publish_emits_the_schema_fields_in_schema_order() -> None:
    instance, messenger = _instance()
    # Provided out of order, and one schema field left out entirely.
    instance.publish("output1", {"count": 7, "temp": 25.34})
    assert list(data_spans(_out_payload(messenger))) == ["temp", "label", "count"]


def test_publish_encodes_each_declared_type_on_the_wire() -> None:
    instance, messenger = _instance()
    instance.publish("output1", {"temp": 25.34, "label": "ok", "count": 7})
    spans = data_spans(_out_payload(messenger))
    # A float field is narrowed to single precision: head 0xfa, never 0xfb.
    assert spans["temp"][0] == 0xFA
    assert spans["temp"].hex() == "fa41cab852"
    assert spans["label"].hex() == "626f6b"
    assert spans["count"].hex() == "07"


def test_publish_drops_keys_the_output_schema_does_not_declare() -> None:
    logger = RecordingLogger()
    instance, messenger = _instance(logger)
    instance.publish("output1", {"temp": 1.0, "label": "ok", "count": 1, "extra": "x"})

    spans = data_spans(_out_payload(messenger))
    assert "extra" not in spans
    assert any(
        "'extra' is not defined in the output schema" in line for line in logger.warns
    ), logger.warns


def test_publish_sends_null_for_missing_and_nil_fields_at_debug_level() -> None:
    logger = RecordingLogger()
    instance, messenger = _instance(logger)
    instance.publish("output1", {"temp": None, "count": 1})

    spans = data_spans(_out_payload(messenger))
    assert spans["temp"].hex() == "f6"  # provided as None
    assert spans["label"].hex() == "f6"  # not provided at all
    # Neither is an anomaly worth a warning.
    assert any("'temp' provided with nil value" in line for line in logger.debugs)
    assert any("'label' not provided" in line for line in logger.debugs)
    assert logger.warns == []
    assert messenger.topic_records("error") == []


def test_publish_field_conversion_failure_nulls_the_field_and_reports_an_error() -> None:
    instance, messenger = _instance()
    # 70000 does not fit the declared int16; the other fields are unaffected
    # and the message is still published.
    instance.publish("output1", {"temp": 25.34, "label": "ok", "count": 70000})

    spans = data_spans(_out_payload(messenger))
    assert spans["count"].hex() == "f6"
    assert spans["temp"].hex() == "fa41cab852"
    assert spans["label"].hex() == "626f6b"

    errors = messenger.topic_records("error")
    assert len(errors) == 1
    assert errors[0].topic == "neoedgex/neoflow/error/node-1"
    payload = json.loads(errors[0].data.decode("utf-8"))
    assert set(payload) == {"code", "detail", "updatedAt"}
    assert payload["code"] == "PROCESS_ERROR"
    assert "'count'" in payload["detail"]
    assert isinstance(payload["updatedAt"], int)


def test_f4_a_field_the_encoder_cannot_serialize_nulls_only_that_field() -> None:
    """F4: a lone surrogate is a valid ``str`` and survives conversion, but not
    the utf-8 encode. The trial encode sits inside the per-field guard, so the
    field goes out as null with a PROCESS_ERROR — the exception must not escape
    publish and crash the handler out of its loop."""
    instance, messenger = _instance()
    instance.publish("output1", {"temp": 25.34, "label": "\ud800", "count": 7})

    spans = data_spans(_out_payload(messenger))
    assert spans["label"].hex() == "f6"
    # The neighbouring fields are untouched and the message still goes out.
    assert spans["temp"].hex() == "fa41cab852"
    assert spans["count"].hex() == "07"

    errors = messenger.topic_records("error")
    assert len(errors) == 1
    payload = json.loads(errors[0].data.decode("utf-8"))
    assert payload["code"] == "PROCESS_ERROR"
    assert "'label'" in payload["detail"]


def test_publish_rejects_an_unknown_handle() -> None:
    instance, messenger = _instance()
    with pytest.raises(ValueError, match="output handle 'nope' does not exist"):
        instance.publish("nope", {})
    assert messenger.published == []


def test_error_topic_payload_is_json_not_cbor() -> None:
    instance, messenger = _instance()
    instance.report_error(neoedgex.CodeNetworkError, RuntimeError("broker down"))
    record = messenger.published[0]
    assert record.topic == "neoedgex/neoflow/error/node-1"
    assert record.qos == 0
    payload = json.loads(record.data.decode("utf-8"))
    assert payload["code"] == "NETWORK_ERROR"
    assert payload["detail"] == "broker down"
    assert payload["updatedAt"] == pytest.approx(int(time.time()), abs=5)


def test_heartbeat_payload_stays_empty() -> None:
    instance, messenger = _instance()
    instance._publish_heartbeat()
    record = messenger.published[0]
    assert record.topic == "neoedgex/neoflow/heartbeat/node-1"
    assert record.qos == 0
    assert record.data == b""


# -----------------------------------------------------------------------------
# receive
# -----------------------------------------------------------------------------


def _run_loop(instance: NodeInstance) -> threading.Thread:
    thread = threading.Thread(target=instance._run_loop, daemon=True)
    thread.start()
    return thread


def _inject(messenger: FakeMessenger, handle: str, data_map: bytes, **kwargs: str) -> None:
    payload = encode_neoflow_message(
        kwargs.get("source", "upstream-node"),
        kwargs.get("timestamp", "2026-03-31T09:10:11Z"),
        data_map,
    )
    messenger.subscriber.put(RawMessengerPayload(handle=handle, data=payload))


def test_run_loop_delivers_a_message_decoded_against_the_input_schema() -> None:
    logger = RecordingLogger()
    instance, messenger = _instance(logger)
    thread = _run_loop(instance)
    try:
        _inject(
            messenger,
            "input1",
            encode_data_map([("value", DataType.INT64, 42), ("extra", DataType.STRING, "x")]),
        )
        message = next(iter(instance.messages()))

        assert message.source == "upstream-node"
        assert message.timestamp == "2026-03-31T09:10:11Z"
        assert message.handle == "input1"
        assert message.to_dict() == {"value": 42, "extra": "x"}
    finally:
        instance.shutdown()
        thread.join(timeout=2.0)


@pytest.mark.parametrize(
    "timestamp",
    [
        pytest.param("2026-03-31T09:10:11Z", id="second precision from an older publisher"),
        pytest.param("2026-03-31T09:10:11.123+08:00", id="millisecond precision"),
        pytest.param("2026-03-31T09:10:11.123456789Z", id="nanosecond from a non-SDK publisher"),
    ],
)
def test_run_loop_passes_an_inbound_timestamp_through_verbatim(timestamp: str) -> None:
    """The receiving half of the interop guarantee: the SDK never parses an
    inbound timestamp, it hands the string to the handler untouched. So a
    publisher on either format — and one with a precision neither format uses —
    is delivered verbatim rather than normalized or rejected."""
    instance, messenger = _instance()
    thread = _run_loop(instance)
    try:
        _inject(
            messenger,
            "input1",
            encode_data_map([("value", DataType.INT64, 42)]),
            timestamp=timestamp,
        )
        message = next(iter(instance.messages()))

        assert message.timestamp == timestamp
        assert message.to_dict()["value"] == 42
    finally:
        instance.shutdown()
        thread.join(timeout=2.0)


def test_run_loop_message_reports_field_problems_through_the_node_logger() -> None:
    logger = RecordingLogger()
    instance, messenger = _instance(logger)
    thread = _run_loop(instance)
    try:
        _inject(messenger, "input1", encode_data_map([("value", DataType.STRING, "abc")]))
        message = next(iter(instance.messages()))
        assert message.to_dict() == {"value": None}
        assert any(line.startswith("Field 'value':") for line in logger.warns), logger.warns
    finally:
        instance.shutdown()
        thread.join(timeout=2.0)


def test_run_loop_discards_a_malformed_envelope_and_keeps_running() -> None:
    logger = RecordingLogger()
    instance, messenger = _instance(logger)
    thread = _run_loop(instance)
    try:
        messenger.subscriber.put(RawMessengerPayload(handle="input1", data=b"\x01\x02"))
        _inject(messenger, "input1", encode_data_map([("value", DataType.INT64, 7)]))

        message = next(iter(instance.messages()))
        assert message.to_dict() == {"value": 7}
        assert any(
            "Failed to unmarshal neoflow message" in line for line in logger.errors
        ), logger.errors
    finally:
        instance.shutdown()
        thread.join(timeout=2.0)


def _assert_dropped_with_a_process_error(
    instance: NodeInstance, messenger: FakeMessenger, logger: RecordingLogger
) -> None:
    """The message just injected must never reach the handler, and the node
    must have told the platform why. A well-formed message is queued behind it
    so the assertion has something deterministic to wait for: the loop is one
    thread draining one queue in order, so once the good message is out, the
    bad one has already been fully handled."""
    _inject(messenger, "input1", encode_data_map([("value", DataType.INT64, 7)]))
    message = next(iter(instance.messages()))
    assert message.to_dict() == {"value": 7}  # the *next* message, never the bad one

    errors = messenger.topic_records("error")
    assert len(errors) == 1, [record.topic for record in messenger.published]
    payload = json.loads(errors[0].data.decode("utf-8"))
    assert payload["code"] == "PROCESS_ERROR"
    assert "not a CBOR map" in payload["detail"]
    assert any("not a CBOR map" in line for line in logger.errors), logger.errors


def test_f11_a_data_segment_that_is_not_a_cbor_map_is_dropped_whole() -> None:
    """F11 (CR-P5): Go gates the data segment on its head byte at receive time
    and drops the message with a node error. Striking D7 ("input only a
    non-SDK publisher can produce needs no defence") removed the ground the
    old behaviour stood on — Python used to deliver the Message anyway and let
    ``to_dict()`` warn and return {}, which costs the platform its corrupt-
    traffic alarm. The head gate is O(1); lazy decoding is otherwise intact."""
    logger = RecordingLogger()
    instance, messenger = _instance(logger)
    thread = _run_loop(instance)
    try:
        _inject(messenger, "input1", bytes.fromhex("8101"))  # data is [1], an array
        _assert_dropped_with_a_process_error(instance, messenger, logger)
    finally:
        instance.shutdown()
        thread.join(timeout=2.0)


def test_f11_an_envelope_carrying_no_data_key_at_all_is_dropped_whole() -> None:
    """F11: an absent ``data`` key is the same wire defect as a non-map one —
    the segment comes back empty, and the same gate drops it. Without the gate
    this is the quietest failure of the lot: an empty span scans to no keys, so
    the handler would get a Message that simply looks like an empty payload."""
    logger = RecordingLogger()
    instance, messenger = _instance(logger)
    thread = _run_loop(instance)
    try:
        # A valid envelope map, minus "data": {"source": ..., "timestamp": ...}
        envelope = encode_data_map(
            [
                ("source", DataType.STRING, "upstream-node"),
                ("timestamp", DataType.STRING, "2026-03-31T09:10:11Z"),
            ]
        )
        messenger.subscriber.put(RawMessengerPayload(handle="input1", data=envelope))
        _assert_dropped_with_a_process_error(instance, messenger, logger)
    finally:
        instance.shutdown()
        thread.join(timeout=2.0)


def test_run_loop_ignores_payloads_without_a_handle() -> None:
    instance, messenger = _instance()
    thread = _run_loop(instance)
    try:
        messenger.subscriber.put(object())
        _inject(messenger, "input1", encode_data_map([("value", DataType.INT64, 7)]))
        assert next(iter(instance.messages())).to_dict() == {"value": 7}
    finally:
        instance.shutdown()
        thread.join(timeout=2.0)


def test_run_loop_unsubscribes_on_shutdown() -> None:
    instance, messenger = _instance()
    thread = _run_loop(instance)
    instance.shutdown()
    thread.join(timeout=2.0)
    assert not thread.is_alive()
    assert messenger.removed_node_id == "node-1"


def test_node_env_context_logger_and_stop() -> None:
    instance, _ = _instance()
    assert isinstance(instance.context(), threading.Event)
    assert instance.logger().tag() == "test"
    instance.stop()
    assert instance.context().is_set()


# -----------------------------------------------------------------------------
# R2: MessageStream — a closed Go channel's semantics
# -----------------------------------------------------------------------------


def test_r2_close_wakes_every_blocked_consumer() -> None:
    """R2: close() broadcasts. A single notify() would leave the second
    consumer parked forever — with two handlers draining one stream, shutdown
    would hang instead of returning."""
    stream = MessageStream()
    exited: list[str] = []
    lock = threading.Lock()

    def consume(name: str) -> None:
        for _ in stream:
            pass
        with lock:
            exited.append(name)

    threads = [threading.Thread(target=consume, args=(name,)) for name in ("a", "b")]
    for thread in threads:
        thread.start()
    time.sleep(0.05)  # let both park inside __next__
    stream.close()
    for thread in threads:
        thread.join(timeout=2.0)

    assert [thread.is_alive() for thread in threads] == [False, False]
    assert sorted(exited) == ["a", "b"]


def test_r2_messages_queued_before_close_are_still_delivered() -> None:
    """R2: close() is not a discard. Everything already queued drains first,
    and only then does every consumer — and every further iteration — stop."""
    stream = MessageStream()
    stream.put_nowait(Message(handle="first"))
    stream.put_nowait(Message(handle="second"))
    stream.close()

    assert [message.handle for message in stream] == ["first", "second"]
    # Exhausted from here on, for this and any other consumer.
    assert list(stream) == []
    stream.put_nowait(Message(handle="late"))
    assert list(stream) == []


# -----------------------------------------------------------------------------
# handler supervision
# -----------------------------------------------------------------------------


def test_handler_exception_or_early_return_triggers_restart_and_stable_runtime_resets_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance, messenger = _instance()

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
    assert any(
        record.topic == "neoedgex/neoflow/error/node-1" for record in messenger.published
    )


def test_r3_a_handler_raising_systemexit_is_caught_reported_and_restarted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R3: the supervisor catches BaseException, mirroring Go's recover() which
    catches every panic. SystemExit is the case that matters — a stray
    sys.exit() deep in application code must restart the handler, not silently
    unwind the thread and leave the node alive but deaf."""
    logger = RecordingLogger()
    instance, messenger = _instance(logger)

    waits: list[float] = []
    monkeypatch.setattr(instance, "_wait", lambda seconds: waits.append(seconds) or False)

    calls = {"count": 0}

    def handler() -> None:
        calls["count"] += 1
        if calls["count"] == 1:
            raise SystemExit(2)
        instance.shutdown()

    instance._supervise_handler(handler)

    assert calls["count"] == 2, "the handler was not restarted after SystemExit"
    assert waits == [1.0]
    assert any("Handler panicked" in line for line in logger.errors), logger.errors

    errors = messenger.topic_records("error")
    assert len(errors) == 1
    payload = json.loads(errors[0].data.decode("utf-8"))
    assert payload["code"] == "PROCESS_ERROR"
    assert payload["detail"] == "handler crashed, restarting"


# -----------------------------------------------------------------------------
# SDK lifecycle
# -----------------------------------------------------------------------------


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


def test_sdk_run_running_flag_not_reset_by_concurrent_caller() -> None:
    sdk = SDK()
    sdk._messenger = FakeMessenger()

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
    sdk._messenger = FakeMessenger()

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


def test_shared_queue_closed_sentinel() -> None:
    from neoedgex._internal import messenger as messenger_mod
    from neoedgex._internal import mock_messenger as mock_mod

    assert mock_mod._QUEUE_CLOSED is messenger_mod._QUEUE_CLOSED
    assert SDK().queue_closed_sentinel() is messenger_mod._QUEUE_CLOSED


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


def test_r7_duration_parsing_matches_go_parseduration() -> None:
    """R7: mock-config intervals are written in Go duration syntax, so the
    parser must read the same strings. Multi-token values and a leading-dot
    fraction are legal; a unitless number is *not* — Go errors on it, and here
    that means falling back to the default instead of silently reading "100"
    as 100 seconds."""
    from neoedgex._internal.sdk import _parse_duration_seconds

    assert _parse_duration_seconds("1m30s") == pytest.approx(90.0)
    assert _parse_duration_seconds("500ms") == pytest.approx(0.5)
    assert _parse_duration_seconds(".5s") == pytest.approx(0.5)
    assert _parse_duration_seconds("100") is None  # no unit -> fall back
    assert _parse_duration_seconds("1h30") is None  # trailing token has no unit


# -----------------------------------------------------------------------------
# app / logger
# -----------------------------------------------------------------------------


def test_app_init_rejects_none_handler() -> None:
    with pytest.raises(TypeError, match="handler must not be None"):
        App(None)  # type: ignore[arg-type]


def test_app_disable_sdk_log_uses_noop_logger() -> None:
    app = App(type("Handler", (), {"handle": lambda self, ctx: None})())
    assert app.disable_sdk_log() is app
    assert app._disable_sdk_log is True


def test_r1_disable_sdk_log_silences_only_the_sdk_side_of_the_split() -> None:
    """R1: two logger factories, one switch. ``disable_sdk_log`` is about SDK
    chatter; the logger handed to the application through ``ctx.logger()`` is
    the app's own output and must survive it. Collapsing them into one factory
    makes the switch swallow the handler's lines too."""
    sdk = SDK(log_enabled=False)
    assert isinstance(sdk.new_logger("SDK"), NoopLogger)
    assert isinstance(sdk.new_handler_logger("Node-demo-node"), SDKLogger)

    # Same switch, other position: nothing is silenced.
    assert isinstance(SDK(log_enabled=True).new_logger("SDK"), SDKLogger)


def test_r1_a_node_on_a_silenced_sdk_still_emits_what_the_handler_writes(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """R1, end to end: build a node on a silenced SDK and check which of the
    two lines actually reaches the logging system."""
    import logging

    sdk = SDK(log_enabled=False)
    sdk._messenger = FakeMessenger()
    instance = NodeInstance(sdk, make_node())
    try:
        with caplog.at_level(logging.DEBUG):
            instance._logger.warn("sdk chatter about itself")
            instance.logger().warn("line the application wrote")

        emitted = [record.getMessage() for record in caplog.records]
        assert "line the application wrote" in emitted
        assert "sdk chatter about itself" not in emitted
        # The handler logger is tagged for the node it belongs to.
        assert instance.logger().tag() == "Node-demo-node"
    finally:
        instance.shutdown()
        sdk.shutdown()


def test_app_run_propagates_connect_error_and_does_not_invoke_handler() -> None:
    from neoedgex.mock import MockConfig, MockSection

    handler_called = threading.Event()

    class Handler:
        def handle(self, ctx) -> None:
            handler_called.set()

    app = App(Handler())
    app.enable_mock(MockConfig(nodes=[make_node()], mock=MockSection(message_interval="1s")))

    original_enable_mock = SDK.enable_mock

    def patched_enable_mock(self, cfg) -> None:
        original_enable_mock(self, cfg)

        def fail_connect() -> None:
            raise RuntimeError("forced connect failure")

        self._messenger.connect = fail_connect  # type: ignore[method-assign]

    SDK.enable_mock = patched_enable_mock  # type: ignore[method-assign]
    try:
        with pytest.raises(RuntimeError, match="forced connect failure"):
            app.run()
    finally:
        SDK.enable_mock = original_enable_mock  # type: ignore[method-assign]

    assert handler_called.is_set() is False
    leaked = [
        thread
        for thread in threading.enumerate()
        if not thread.daemon and thread is not threading.main_thread() and thread.is_alive()
    ]
    assert leaked == [], f"leaked non-daemon threads: {leaked}"


def test_logger_default_level_is_debug(monkeypatch: pytest.MonkeyPatch) -> None:
    _assert_logger_level(monkeypatch, env_level=None, expected="DEBUG")


def test_logger_env_override_takes_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    _assert_logger_level(monkeypatch, env_level="WARNING", expected="WARNING")


def _assert_logger_level(
    monkeypatch: pytest.MonkeyPatch, env_level: str | None, expected: str
) -> None:
    import importlib
    import logging

    import neoedgex._internal.logger as logger_mod

    if env_level is None:
        monkeypatch.delenv("NEOEDGEX_LOG_LEVEL", raising=False)
    else:
        monkeypatch.setenv("NEOEDGEX_LOG_LEVEL", env_level)
    monkeypatch.setattr(logger_mod, "_CONFIGURED", False)

    root = logging.getLogger()
    saved_level = root.level
    saved_handlers = list(root.handlers)
    for handler in saved_handlers:
        root.removeHandler(handler)
    try:
        logger_mod._ensure_logging()
        assert root.level == getattr(logging, expected)
    finally:
        for handler in list(root.handlers):
            root.removeHandler(handler)
        for handler in saved_handlers:
            root.addHandler(handler)
        root.setLevel(saved_level)
        importlib.reload(logger_mod)
