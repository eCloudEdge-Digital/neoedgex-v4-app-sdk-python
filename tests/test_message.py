"""``Message`` semantics: schema-driven decoding with its logging, and
``to_dataclass``.

The read surface is deliberately raw-bytes based: ``raw`` keeps the encoded
CBOR data map and ``to_dict()`` decodes it against the input schema (and
caches). "What did the wire actually carry" is a question only the bytes can
answer — an undefined field and an absent key both decode to None — so the
tests below that need it decode ``raw`` themselves, exactly as an application
would.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Any

import cbor2
import pytest

from neoedgex.contract import DataType, Message
from neoedgex.contract.codec import encode_data_map
from support import RecordingLogger

# -----------------------------------------------------------------------------
# to_dict
# -----------------------------------------------------------------------------


def test_defaults_are_empty_and_decode_to_an_empty_mapping() -> None:
    message = Message()
    assert (message.source, message.timestamp, message.handle, message.raw) == ("", "", "", b"")
    logger = RecordingLogger()
    # An empty payload is not a CBOR map: it decodes to {} with a warning.
    assert Message(logger=logger).to_dict() == {}
    assert logger.lines_containing("cannot decode data map")


def test_s1_s2_the_decode_is_cached_but_every_call_hands_back_a_fresh_copy() -> None:
    """S1/S2: decoding happens once, the dict does not survive the call. Go's
    ``ToMap`` builds a new map per call; matching that is what makes a handler
    that mutates the result harmless — the next reader, and ``to_dataclass``,
    still see the wire."""

    @dataclasses.dataclass
    class Row:
        a: int = 0

    message = Message(raw=encode_data_map([("a", DataType.INT64, 1)]), plan={"a": DataType.INT64})
    first = message.to_dict()
    second = message.to_dict()
    assert first == second == {"a": 1}
    assert second is not first

    first["a"] = 999
    first["injected"] = True
    assert message.to_dict() == {"a": 1}
    assert message.to_dataclass(Row) == Row(a=1)


def test_undecodable_data_map_yields_an_empty_dict_and_warns_once() -> None:
    logger = RecordingLogger()
    message = Message(
        source="upstream-node",
        handle="input1",
        raw=b"\x01\x02",
        plan={"a": DataType.INT64},
        logger=logger,
    )
    assert message.to_dict() == {}
    assert message.to_dict() == {}
    complaints = logger.lines_containing("cannot decode data map")
    assert len(complaints) == 1, logger.warns
    assert "'upstream-node'" in complaints[0] and "'input1'" in complaints[0]
    assert complaints[0] in logger.warns


def test_declared_field_that_fails_conversion_is_undefined_and_warns() -> None:
    logger = RecordingLogger()
    message = Message(
        raw=encode_data_map([("value", DataType.STRING, "abc")]),
        plan={"value": DataType.INT64},
        logger=logger,
    )
    assert message.to_dict() == {"value": None}
    assert any(line.startswith("Field 'value':") for line in logger.warns), logger.warns


def test_key_outside_the_schema_bypasses_with_a_debug_note() -> None:
    logger = RecordingLogger()
    message = Message(
        raw=encode_data_map([("extra", DataType.INT64, 7)]), plan={}, logger=logger
    )
    assert message.to_dict() == {"extra": 7}
    assert any("not defined in the input schema" in line for line in logger.debugs)
    assert logger.warns == []


def test_bypassed_key_with_no_natural_value_warns_as_unknown_tag() -> None:
    logger = RecordingLogger()
    # {"arr": [1, 2]} — a container has no place in the natural domain.
    message = Message(raw=bytes.fromhex("a163617272820102"), plan=None, logger=logger)
    assert message.to_dict() == {"arr": None}
    assert any(line.startswith("Unknown tag 'arr':") for line in logger.warns), logger.warns


def test_decoding_needs_no_logger() -> None:
    message = Message(raw=b"\x01", plan={"a": DataType.INT64})
    assert message.to_dict() == {}


# -----------------------------------------------------------------------------
# to_dataclass
# -----------------------------------------------------------------------------


@dataclass
class Reading:
    temp: float
    label: str = "unset"
    optional: int | None = None
    tags: list[str] = field(default_factory=list)


@dataclass
class Keyed:
    temperature: float = field(default=0.0, metadata={"key": "temp"})


@dataclass
class Bare:
    value: int = 0


@dataclass
class Optional:
    value: int | None = None


@dataclass
class Loose:
    anything: Any = None
    thing: object = None


@dataclass
class Derived:
    value: int = 0
    computed: str = field(default="local", init=False)


class NotADataclass:
    pass


def _message(fields: dict[str, tuple[DataType, Any]], logger: Any = None) -> Message:
    return Message(
        raw=encode_data_map([(key, spec[0], spec[1]) for key, spec in fields.items()]),
        plan={key: spec[0] for key, spec in fields.items()},
        logger=logger,
    )


def test_to_dataclass_only_accepts_dataclass_types() -> None:
    message = _message({"temp": (DataType.DOUBLE, 1.5)})
    with pytest.raises(TypeError, match="must be a dataclass type"):
        message.to_dataclass(NotADataclass)
    with pytest.raises(TypeError, match="must be a dataclass type"):
        message.to_dataclass(dict)
    with pytest.raises(TypeError, match="must be a dataclass type"):
        message.to_dataclass(Reading(temp=1.0))  # an instance, not the type


def test_to_dataclass_fills_fields_and_returns_a_new_instance() -> None:
    message = _message({"temp": (DataType.DOUBLE, 1.5), "label": (DataType.STRING, "ok")})
    reading = message.to_dataclass(Reading)
    assert reading == Reading(temp=1.5, label="ok", optional=None, tags=[])
    assert message.to_dataclass(Reading) is not reading


def test_to_dataclass_maps_the_wire_key_from_field_metadata() -> None:
    assert _message({"temp": (DataType.DOUBLE, 2.5)}).to_dataclass(Keyed) == Keyed(
        temperature=2.5
    )


def test_to_dataclass_keeps_defaults_for_undefined_fields() -> None:
    # Absent and explicit null are both "undefined" and both let the declared
    # default stand (a present-but-bad value does not — see case C/E below).
    assert _message({}).to_dataclass(Reading).label == "unset"
    assert _message({"label": (DataType.STRING, None)}).to_dataclass(Reading).label == "unset"
    assert _message({}).to_dataclass(Reading).tags == []


def test_to_dataclass_fills_none_when_there_is_no_default() -> None:
    @dataclasses.dataclass
    class Required:
        a: int
        b: str

    required = _message({"a": (DataType.INT64, None)}).to_dataclass(Required)
    assert required.a is None
    assert required.b is None


def test_to_dataclass_optional_annotation_aborts_on_an_incompatible_value() -> None:
    """`X | None` is the Go pointer field, not Go's `any`: a value that is not
    an X aborts exactly as a bare X does. None stays reserved for null/absent."""
    message = _message({"value": (DataType.STRING, "text")})
    with pytest.raises(ValueError) as excinfo:
        message.to_dataclass(Optional)
    assert "field 'value'" in str(excinfo.value)
    assert "not compatible with annotation" in str(excinfo.value)


def test_to_dataclass_bare_annotation_aborts_naming_the_field() -> None:
    message = _message({"value": (DataType.STRING, "text")})
    with pytest.raises(ValueError, match="field 'value'"):
        message.to_dataclass(Bare)


def test_f12_an_integer_wire_head_reads_directly_into_a_float_annotation() -> None:
    # Integer majors count as a `float` match on the declaration-wins path
    # (= Go's fallback, a plain codec decode, which puts integer wire values
    # into float fields under every schema). Not to be confused with the R4
    # widening F13 removed: that one took a schema-*converted* int, from a wire
    # head no float can read — see the F13 block at the end of this file.
    reading = _message({"temp": (DataType.INT64, 5)}).to_dataclass(Reading)
    assert reading.temp == 5.0
    assert type(reading.temp) is float


def test_to_dataclass_never_passes_a_bool_as_an_int() -> None:
    # bool is an int subclass in Python; the wire types are not related.
    message = _message({"value": (DataType.BOOL, True)})
    with pytest.raises(ValueError, match="field 'value'"):
        message.to_dataclass(Bare)
    # `int | None` is the pointer analogue and rejects the bool just as harshly.
    with pytest.raises(ValueError, match="field 'value'"):
        message.to_dataclass(Optional)


def test_to_dataclass_passes_values_through_for_any_and_object() -> None:
    loose = _message(
        {"anything": (DataType.BOOL, True), "thing": (DataType.RAW, b"ab")}
    ).to_dataclass(Loose)
    assert loose.anything is True
    assert loose.thing == b"ab"


def test_to_dataclass_skips_non_init_fields() -> None:
    derived = _message(
        {"value": (DataType.INT64, 1), "computed": (DataType.STRING, "from-wire")}
    ).to_dataclass(Derived)
    assert derived.value == 1
    assert derived.computed == "local"


# -----------------------------------------------------------------------------
# F7 / S6②: undefined vs "present but undecodable"
#
# CR-P1 settled the contract to_dataclass must meet: it *is* Go's ToStruct.
# The two categories are not the same and must not be conflated —
#   undefined (key absent, or the wire carried CBOR null)
#       -> the declared default stands, like Go leaving the field untouched;
#   present but undecodable (the wire carried a non-null value the schema type
#   cannot read)
#       -> a bare concrete annotation aborts the whole call, like a Go concrete
#          field; the default never stands in.
# The table below is the A-F comparison, pinned against real Go ToStruct runs.
# -----------------------------------------------------------------------------


def _wire(
    entries: list[tuple[str, DataType, Any]],
    plan: dict[str, DataType],
    logger: Any = None,
) -> Message:
    """A message whose wire encoding and decode plan are stated separately —
    the only way to reproduce "the wire carried something the declared schema
    type cannot read", which is what the A-F table is about."""
    return Message(raw=encode_data_map(entries), plan=plan, logger=logger)


@dataclass
class Counted:
    count: int = 7  # bare concrete: a bad value must abort


@dataclass
class CountedAny:
    count: Any = 7  # Go `any`: absorbs, and the default never stands in


def test_f7_s6_case_a_an_absent_key_lets_the_default_stand() -> None:
    assert _wire([], {"count": DataType.INT16}).to_dataclass(Counted) == Counted(count=7)


def test_f7_s6_case_b_an_explicit_null_lets_the_default_stand() -> None:
    message = _wire([("count", DataType.INT16, None)], {"count": DataType.INT16})
    # The wire really carried the key — it is null, not absent — and the
    # default still stands, exactly as for case A.
    assert cbor2.loads(message.raw) == {"count": None}
    assert message.to_dataclass(Counted) == Counted(count=7)


def test_f12_case_c_an_out_of_range_schema_value_is_taken_by_the_declaration() -> None:
    """Case C, rewritten by F12/CR-P6. 70000 rides the wire as an int64 head
    while the schema declares int16, so the two APIs part ways: ``to_dict()``
    lets the schema win and delivers undefined, ``to_dataclass()`` lets the
    declaration win and reads the head straight off the wire.

    DEV-3 (2): a Python ``int`` carries no width and *is* the int64 domain, so
    it takes the value. Go's judgment table splits here — its ``int16`` field
    errors, its ``int64`` field returns 70000 — and Python can only spell the
    second. Nothing about "present but undecodable" changed: see case D, which
    still aborts, for the unrewritten half of the F7/S6 contract."""
    message = _wire([("count", DataType.INT64, 70000)], {"count": DataType.INT16})
    # The wire carried a real, non-null 70000 — so to_dict's None below is the
    # schema refusing it, not an absent key.
    assert cbor2.loads(message.raw) == {"count": 70000}
    assert message.to_dict() == {"count": None}  # to_dict still delivers undefined
    assert message.to_dataclass(Counted) == Counted(count=70000)


def test_f7_s6_case_d_an_unparseable_value_aborts_naming_the_field() -> None:
    message = _wire([("count", DataType.STRING, "abc")], {"count": DataType.INT64})
    with pytest.raises(ValueError) as excinfo:
        message.to_dataclass(Counted)
    assert "field 'count'" in str(excinfo.value)
    assert "invalid syntax" in str(excinfo.value)


@dataclass
class CountedOptional:
    count: int | None = None  # the Go `*int16` analogue


def test_f9_case_e_an_optional_annotation_is_a_pointer_field_not_an_any_field() -> None:
    """Case E, the F9 ruling (CR-P3): `int | None` maps to Go's ``*int16``, and
    real ``ToStruct`` runs against a ``*int16`` field say — "abc" -> error,
    null -> nil, absent -> nil. A pointer never absorbs a bad value; nil is
    reserved for "the wire said nothing". Only Go's ``any`` (= Python
    ``Any``/unannotated, case F) stays quiet.

    F12/CR-P6 amended one row of that table: 70000, whose *head* an ``int``
    reads directly, now never reaches the schema at all — see the first
    assertion, and case C for the bare-annotation twin."""
    # Declaration wins (F12), width margin DEV-3 (2): the wire carries an
    # integer head, and `int | None` is an int64-domain pointer, so the int16
    # schema never gets a say. Go's *int64 field agrees; only its *int16 errors.
    assert _wire([("count", DataType.INT64, 70000)], {"count": DataType.INT16}).to_dataclass(
        CountedOptional
    ) == CountedOptional(count=70000)

    # Present but undecodable: unparseable against the declared int64.
    with pytest.raises(ValueError) as unparseable:
        _wire([("count", DataType.STRING, "abc")], {"count": DataType.INT64}).to_dataclass(
            CountedOptional
        )
    assert "field 'count'" in str(unparseable.value)
    assert "invalid syntax" in str(unparseable.value)

    # Present and perfectly decodable, but the decoded str is not an int:
    # incompatible with the annotation aborts just like a decode failure.
    with pytest.raises(ValueError) as incompatible:
        _wire([("count", DataType.STRING, "abc")], {"count": DataType.STRING}).to_dataclass(
            CountedOptional
        )
    assert "field 'count'" in str(incompatible.value)
    assert "not compatible with annotation" in str(incompatible.value)

    # Undefined — an explicit null and an absent key both leave None (= nil).
    carried_null = _wire([("count", DataType.INT16, None)], {"count": DataType.INT16})
    assert cbor2.loads(carried_null.raw) == {"count": None}  # carried, as null
    assert carried_null.to_dataclass(CountedOptional) == CountedOptional(count=None)
    assert _wire([], {"count": DataType.INT16}).to_dataclass(CountedOptional) == CountedOptional(
        count=None
    )

    # And a value that does fit still arrives untouched.
    assert _wire(
        [("count", DataType.INT16, 25)], {"count": DataType.INT16}
    ).to_dataclass(CountedOptional) == CountedOptional(count=25)


def test_f7_s6_case_f_an_any_annotation_absorbs_it_and_ignores_the_default() -> None:
    for message in (
        _wire([("count", DataType.INT64, 70000)], {"count": DataType.INT16}),
        _wire([("count", DataType.STRING, "abc")], {"count": DataType.INT64}),
    ):
        # Not 7: a default covers absent/null only, never a bad value.
        # (F12 does not reach here: `Any` is no declaration to win with, so
        # both rows still take the schema's verdict, undefined.)
        assert message.to_dataclass(CountedAny) == CountedAny(count=None)


# -----------------------------------------------------------------------------
# F12 / CR-P6: D13, "the declaration wins" inside to_dataclass
#
# The two read APIs split the way Go's ToMap and ToStruct do: ToMap trusts the
# input schema, ToStruct trusts the field declaration. to_dataclass therefore
# runs three stages — a wire head the annotation can read is decoded as
# declared and the schema never gets a say; otherwise the schema-decoded value
# is taken, and only if it already *is* the annotated type (F13).
#
# The expectations below are the Python column of the Go judgment table (784
# cells against real ToStruct runs on v2.1.0). The cells where the two cannot
# agree are width margins only, split out into the DEV-3 block that follows.
# -----------------------------------------------------------------------------


@dataclass
class IntField:
    v: int = 0


@dataclass
class FloatField:
    v: float = 0.0


@dataclass
class OptionalIntField:
    v: int | None = None


@dataclass
class AnyField:
    v: Any = None


def test_f12_an_integer_wire_head_into_an_int_field_ignores_a_double_schema() -> None:
    """F12 judgment table: wire 25 (integer head) + schema ``double`` + ``int``
    annotation -> 25. The same message read through ``to_dict()``, where the
    schema wins, is 25.0 — the split is the whole point of D13."""
    message = _wire([("v", DataType.INT64, 25)], {"v": DataType.DOUBLE})
    assert message.to_dict() == {"v": 25.0}
    value = message.to_dataclass(IntField).v
    assert value == 25
    assert type(value) is int  # not the schema's 25.0 (25 == 25.0 in Python)


def test_f12_a_double_wire_head_into_a_float_field_ignores_an_int64_schema() -> None:
    """F12 judgment table: wire 0xfb 25.34 + schema ``int64`` + ``float``
    annotation -> 25.34. The schema route would truncate it to 25, and
    ``to_dict()`` does exactly that."""
    message = _wire([("v", DataType.DOUBLE, 25.34)], {"v": DataType.INT64})
    assert message.to_dict() == {"v": 25}
    assert message.to_dataclass(FloatField) == FloatField(v=25.34)


def test_f12_a_single_precision_wire_head_into_a_float_field_restores_the_decimal() -> None:
    """F12 judgment table: wire 0xfa 25.34 + schema ``float`` + ``float``
    annotation -> 25.34, never the widened 25.34000015258789. The
    declaration-wins path decodes the head itself, and it restores 0xfa to its
    shortest float32 decimal there too. (This is also DEV-3 (1): Go restores
    the same way through a ``float32`` field but widens through ``float64``,
    and Python's single ``float`` always restores.)"""
    message = _wire([("v", DataType.FLOAT, 25.34)], {"v": DataType.FLOAT})
    assert message.to_dataclass(FloatField) == FloatField(v=25.34)


def test_f12_a_text_wire_head_no_int_can_read_falls_back_to_the_schema() -> None:
    """F12 judgment table: wire "25" (text head) + schema ``string`` + ``int``
    annotation -> abort. No ``int`` reads a text head, so stage one is skipped
    and the schema route runs; the ``str`` it hands back is not an ``int``, and
    the schema route coerces nothing (F13)."""
    message = _wire([("v", DataType.STRING, "25")], {"v": DataType.STRING})
    assert message.to_dict() == {"v": "25"}
    with pytest.raises(ValueError) as excinfo:
        message.to_dataclass(IntField)
    assert "field 'v'" in str(excinfo.value)
    assert "not compatible with annotation 'int'" in str(excinfo.value)


def test_f12_leaves_undefined_any_and_optional_semantics_untouched() -> None:
    """F12 only reroutes wire values that are *present and readable*. One spot
    check per surrounding rule, all unchanged: an absent key and an explicit
    null are still undefined and still let the default stand (R3); ``Any`` is
    still no declaration to win with (case F); ``X | None`` is still the Go
    pointer field — None for undefined, abort for a bad value (F9/CR-P3)."""
    # Absent and null carry no span to decode as declared, so both fall through
    # to the schema route and, finding undefined, leave the default alone.
    assert _wire([], {"v": DataType.DOUBLE}).to_dataclass(IntField) == IntField(v=0)
    assert _wire([("v", DataType.INT16, None)], {"v": DataType.DOUBLE}).to_dataclass(
        IntField
    ) == IntField(v=0)
    # Any: the schema's verdict stands, undefined included.
    assert _wire([("v", DataType.STRING, "abc")], {"v": DataType.INT64}).to_dataclass(
        AnyField
    ) == AnyField(v=None)
    # X | None: undefined -> None; a head no int reads plus an undecodable
    # schema value -> abort, exactly as for a bare int.
    assert _wire([], {"v": DataType.INT64}).to_dataclass(
        OptionalIntField
    ) == OptionalIntField(v=None)
    with pytest.raises(ValueError, match="field 'v'"):
        _wire([("v", DataType.STRING, "abc")], {"v": DataType.INT64}).to_dataclass(
            OptionalIntField
        )


# -----------------------------------------------------------------------------
# DEV-3 (2026-08-07, narrowed): the width margin, and nothing else
#
# With D13 ported (F12) and the int->float widening removed (F13), the Go
# judgment table matches cell for cell except where a Python annotation cannot
# express what a Go declaration can: `int` is one width where Go has six,
# `float` is one where Go has two. The cells below are that residue, pinned so
# the divergence stays a ruling and never decays into drift. Family (1) — 0xfa
# always restoring — is pinned by the F12 block above.
# -----------------------------------------------------------------------------


def test_dev3_2_an_int_annotation_is_the_int64_domain_whatever_the_schema_says() -> None:
    """DEV-3 (2). Go splits on the declared width: its ``int16`` field errors
    on 70000, its ``int64`` field returns it. Python's ``int`` has no width and
    is the int64 domain, so it always returns it. The int64 bound itself is
    real, though — past it the declared decode raises rather than inventing a
    wider field."""
    assert _wire([("v", DataType.INT64, 70000)], {"v": DataType.INT16}).to_dataclass(
        IntField
    ) == IntField(v=70000)
    with pytest.raises(ValueError) as excinfo:
        _wire([("v", DataType.UINT64, 2**63)], {"v": DataType.UINT64}).to_dataclass(IntField)
    assert "field 'v'" in str(excinfo.value)
    assert "out of int64 range" in str(excinfo.value)


def test_dev3_3_a_float_annotation_takes_a_double_wire_value_as_is() -> None:
    """DEV-3 (3). ``float`` follows the wire width, not a declared one: a 0xfb
    1e300 arrives whole, as it would in a Go ``float64`` field. A Go ``float32``
    field narrows or rejects it — and so does the ``float`` *schema* here,
    which is why ``to_dict()`` says undefined for the very same bytes."""
    message = _wire([("v", DataType.DOUBLE, 1e300)], {"v": DataType.FLOAT})
    assert message.to_dict() == {"v": None}
    assert message.to_dataclass(FloatField) == FloatField(v=1e300)


def test_dev3_4_a_schema_produced_float_reaches_a_float_annotation_from_any_head() -> None:
    """DEV-3 (4). A text wire value offers no head a ``float`` can read, so the
    schema route runs and its ``double`` conversion produces 25.0, which the
    annotation accepts. Go accepts exactly one of its two widths per schema
    (``double`` schema -> ``float64`` field yes, ``float32`` field no, and the
    mirror image for a ``float`` schema); Python's single ``float`` takes both
    sides of that pair."""
    message = _wire([("v", DataType.STRING, "25")], {"v": DataType.DOUBLE})
    assert message.to_dict() == {"v": 25.0}
    assert message.to_dataclass(FloatField) == FloatField(v=25.0)


# -----------------------------------------------------------------------------
# F13: the schema route coerces nothing
#
# R4 used to widen an int value into a `float` annotation. Once F12's
# declaration-wins stage claimed every *integer wire head*, all that rule still
# did was absorb schema-converted ints arriving from non-numeric heads — six
# cells where Go's ToStruct aborts. Removing it took the non-width divergence
# to zero; these tests keep it there.
# -----------------------------------------------------------------------------


def test_f13_a_schema_produced_int_does_not_widen_into_a_float_annotation() -> None:
    """F13: wire "25" + schema ``int64`` + ``float`` annotation -> abort. The
    schema route really does produce an int here — ``to_dict()`` proves it —
    and an int is not a float. (Before F13 the field received 25.0.)"""
    message = _wire([("v", DataType.STRING, "25")], {"v": DataType.INT64})
    assert message.to_dict() == {"v": 25}
    with pytest.raises(ValueError) as excinfo:
        message.to_dataclass(FloatField)
    assert "field 'v'" in str(excinfo.value)
    assert "not compatible with annotation 'float'" in str(excinfo.value)


def test_f13_a_schema_produced_int_from_a_bool_wire_value_does_not_widen_either() -> None:
    """F13, the bool corner of the same six cells: ``true`` converted through
    an ``int64`` schema is 1, and 1 is still not a float. A bool head is no
    ``float`` match either, so nothing rescues it on the declared path.
    (Before F13 the field received 1.0.)"""
    message = _wire([("v", DataType.BOOL, True)], {"v": DataType.INT64})
    assert message.to_dict() == {"v": 1}
    with pytest.raises(ValueError, match="not compatible with annotation 'float'"):
        message.to_dataclass(FloatField)
