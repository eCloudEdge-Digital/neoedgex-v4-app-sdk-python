"""Contract unit tests: the conversion matrix, the native-value conversion
engine (``convert_to_typed_value``) and the string-form ``PortFieldData`` used
by mock configs and device-facing code."""

from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Any

import pytest

from neoedgex.contract import (
    SUPPORTED_TYPES,
    DataType,
    Event,
    Node,
    PortFieldData,
    PortFieldSchema,
    convert_any_value,
    convert_to_typed_value,
    convert_value_by_type,
    get_data_type,
    get_value_and_cast,
)

_NUMBER_TYPES = (
    DataType.INT16,
    DataType.INT32,
    DataType.INT64,
    DataType.UINT16,
    DataType.UINT32,
    DataType.UINT64,
    DataType.FLOAT,
    DataType.DOUBLE,
)
_ALL_TYPES = (DataType.UNDEFINED, *_NUMBER_TYPES, DataType.BOOL, DataType.STRING, DataType.RAW)

# The conversion matrix as a literal truth table (destination -> the sources it
# accepts), written out rather than derived, so it cannot inherit a bug from
# the implementation it checks.
_ACCEPTS: dict[DataType, frozenset[DataType]] = {
    **{
        dest: frozenset({*_NUMBER_TYPES, DataType.BOOL, DataType.STRING})
        for dest in _NUMBER_TYPES
    },
    # string -> bool is deliberately absent: "true" is rejected, not parsed.
    DataType.BOOL: frozenset({*_NUMBER_TYPES, DataType.BOOL}),
    DataType.STRING: frozenset({*_NUMBER_TYPES, DataType.BOOL, DataType.STRING}),
    DataType.RAW: frozenset({DataType.RAW}),
    DataType.UNDEFINED: frozenset(),
}


# -----------------------------------------------------------------------------
# type registry
# -----------------------------------------------------------------------------


def test_data_type_registry_holds_exactly_the_wire_types() -> None:
    assert {data_type.value for data_type in SUPPORTED_TYPES} == {
        "bool",
        "int16",
        "int32",
        "int64",
        "uint16",
        "uint32",
        "uint64",
        "float",
        "double",
        "string",
        "raw",
    }
    assert set(DataType) == set(SUPPORTED_TYPES) | {DataType.UNDEFINED}
    assert not DataType.UNDEFINED.is_supported()
    assert {data_type for data_type in DataType if data_type.is_number()} == set(_NUMBER_TYPES)


def test_get_data_type_maps_python_values_to_wire_types() -> None:
    assert get_data_type(True) == DataType.BOOL
    assert get_data_type(42) == DataType.INT64
    assert get_data_type(2.5) == DataType.DOUBLE
    assert get_data_type("x") == DataType.STRING
    assert get_data_type(b"x") == DataType.RAW
    assert get_data_type(bytearray(b"x")) == DataType.RAW
    # No time type on the wire, and no container types.
    assert get_data_type(datetime(2026, 1, 1, tzinfo=UTC)) == DataType.UNDEFINED
    assert get_data_type({"a": 1}) == DataType.UNDEFINED
    assert get_data_type([1]) == DataType.UNDEFINED
    assert get_data_type(None) == DataType.UNDEFINED


# -----------------------------------------------------------------------------
# can_convert_to: the full 12x12 matrix
# -----------------------------------------------------------------------------


@pytest.mark.parametrize("dest", _ALL_TYPES, ids=lambda t: f"to-{t.name}")
@pytest.mark.parametrize("src", _ALL_TYPES, ids=lambda t: f"from-{t.name}")
def test_can_convert_to_matrix(src: DataType, dest: DataType) -> None:
    assert src.can_convert_to(dest) is (src in _ACCEPTS[dest])


def test_matrix_truth_table_covers_every_type() -> None:
    assert set(_ACCEPTS) == set(_ALL_TYPES)


# -----------------------------------------------------------------------------
# convert_to_typed_value: integer ranges
# -----------------------------------------------------------------------------

_INT_BOUNDS = [
    (DataType.INT16, -(2**15), 2**15 - 1),
    (DataType.INT32, -(2**31), 2**31 - 1),
    (DataType.INT64, -(2**63), 2**63 - 1),
    (DataType.UINT16, 0, 2**16 - 1),
    (DataType.UINT32, 0, 2**32 - 1),
    (DataType.UINT64, 0, 2**64 - 1),
]


@pytest.mark.parametrize(
    ("dest", "low", "high"), _INT_BOUNDS, ids=[case[0].value for case in _INT_BOUNDS]
)
def test_integer_bounds_are_inclusive_and_one_past_is_refused(
    dest: DataType, low: int, high: int
) -> None:
    assert convert_to_typed_value(low, dest) == low
    assert convert_to_typed_value(high, dest) == high
    with pytest.raises(ValueError, match="out of range"):
        convert_to_typed_value(low - 1, dest)
    with pytest.raises(ValueError, match="out of range"):
        convert_to_typed_value(high + 1, dest)


def test_integer_source_outside_the_wire_domain_is_refused() -> None:
    # No declarable type can hold these and CBOR bignums are never emitted, so
    # the value has no legal wire representation at all.
    for dest in (DataType.INT64, DataType.UINT64, DataType.DOUBLE, DataType.STRING):
        with pytest.raises(ValueError, match="out of range"):
            convert_to_typed_value(2**64, dest)
        with pytest.raises(ValueError, match="out of range"):
            convert_to_typed_value(-(2**63) - 1, dest)


def test_float_source_truncates_toward_zero() -> None:
    assert convert_to_typed_value(1.9, DataType.INT16) == 1
    assert convert_to_typed_value(-1.9, DataType.INT16) == -1
    assert convert_to_typed_value(25.9, DataType.INT64) == 25
    assert type(convert_to_typed_value(1.9, DataType.INT16)) is int


def test_float_source_64bit_upper_bounds_are_exclusive() -> None:
    # float64 cannot represent 2**63-1 / 2**64-1: the nearest values round up
    # to 2**63 / 2**64, which do not fit.
    with pytest.raises(ValueError, match="out of range"):
        convert_to_typed_value(float(2**63), DataType.INT64)
    with pytest.raises(ValueError, match="out of range"):
        convert_to_typed_value(float(2**63 - 1), DataType.INT64)
    with pytest.raises(ValueError, match="out of range"):
        convert_to_typed_value(float(2**64), DataType.UINT64)
    # The lower bound is representable and accepted.
    assert convert_to_typed_value(float(-(2**63)), DataType.INT64) == -(2**63)


# -----------------------------------------------------------------------------
# convert_to_typed_value: strings, floats, bools, raw
# -----------------------------------------------------------------------------


@pytest.mark.parametrize("text", ["1_0", " 5", "5 ", "1.5", "abc", "", "0x10"])
def test_string_to_integer_parsing_is_strict(text: str) -> None:
    with pytest.raises(ValueError, match="invalid syntax"):
        convert_to_typed_value(text, DataType.INT16)


def test_string_to_integer_accepts_go_strconv_shapes() -> None:
    assert convert_to_typed_value("+5", DataType.INT16) == 5
    assert convert_to_typed_value("-5", DataType.INT16) == -5
    assert convert_to_typed_value("0025", DataType.INT16) == 25
    with pytest.raises(ValueError, match="out of range"):
        convert_to_typed_value("32768", DataType.INT16)


@pytest.mark.parametrize("text", [" 5", "abc", "", "1.2.3"])
def test_string_to_float_parsing_is_strict(text: str) -> None:
    with pytest.raises(ValueError, match="invalid syntax"):
        convert_to_typed_value(text, DataType.DOUBLE)


def test_string_to_float_accepts_scientific_notation() -> None:
    assert convert_to_typed_value("2.5e1", DataType.DOUBLE) == 25.0
    assert convert_to_typed_value("+5", DataType.DOUBLE) == 5.0
    assert convert_to_typed_value(".5", DataType.DOUBLE) == 0.5


def test_string_to_float_accepts_digit_separating_underscores() -> None:
    """Go ParseFloat permits underscores between digits, so "1_0" is a number,
    not a syntax error. The integer path stays strict: ParseInt with an
    explicit base 10 refuses them."""
    assert convert_to_typed_value("1_0", DataType.DOUBLE) == 10.0
    assert convert_to_typed_value("1_000.5", DataType.DOUBLE) == 1000.5
    assert convert_to_typed_value("1_0", DataType.FLOAT) == 10.0
    with pytest.raises(ValueError, match="invalid syntax"):
        convert_to_typed_value("1_0", DataType.INT16)


@pytest.mark.parametrize("text", ["NaN", "nan", "Inf", "inf", "+Inf", "-Infinity"])
def test_string_nan_and_inf_are_refused(text: str) -> None:
    with pytest.raises(ValueError, match="NaN and Inf"):
        convert_to_typed_value(text, DataType.DOUBLE)


# Texts whose float64 value lands exactly on a float32 rounding boundary,
# where narrowing through float64 (double rounding) picks the wrong float32.
# Go's ParseFloat(s, 32) rounds the text once, directly to float32; the
# expected bit patterns below are Go's output, cross-checked bit-for-bit
# against `strconv` via the Go SDK's ConvertToTypedValue.
@pytest.mark.parametrize(
    ("text", "want_bits"),
    [
        # boundary between 1.0 and its float32 successor: the text is just
        # above it, but ties-to-even on the float64 boundary value goes down.
        ("1.000000059604644830901776231", 0x3F800001),
        # mirrored direction: lower neighbor odd, ties-to-even goes up while
        # the text is just below the boundary.
        ("1.000000178813934270660723769", 0x3F800001),
        # the classic double-rounding example, 7 significant digits.
        ("7.038531e-26", 0x15AE43FD),
        # a text spelling a boundary exactly IS a tie: half-to-even applies.
        ("1.000000059604644775390625", 0x3F800000),
        # boundary between zero and the smallest subnormal float32.
        ("7.006492321624085743557102783E-46", 0x00000001),
        # sign travels with the corrected rounding.
        ("-7.038531e-26", 0x95AE43FD),
    ],
)
def test_string_to_float32_rounds_the_text_once(text: str, want_bits: int) -> None:
    from neoedgex.contract._float import float32_bits

    assert float32_bits(convert_to_typed_value(text, DataType.FLOAT)) == want_bits


def test_string_to_float32_overflow_boundary_matches_go() -> None:
    """The float32 overflow cut sits at the midpoint between MaxFloat32 and
    2^128. A text below it whose float64 lands exactly on the midpoint must
    round down to MaxFloat32 the way Go does — rejecting it is a
    convert-succeeds-on-one-SDK-only divergence, worse than a wrong last bit.
    At or above the midpoint both SDKs refuse."""
    from neoedgex.contract._float import float32_bits

    below = "3.402823567797336521928064297E+38"
    assert float32_bits(convert_to_typed_value(below, DataType.FLOAT)) == 0x7F7FFFFF
    with pytest.raises(ValueError, match="out of range"):
        convert_to_typed_value("3.402823567797336710822723612E+38", DataType.FLOAT)


def test_hex_float_text_to_float32_also_rounds_once() -> None:
    """The exact-value comparison must read Go hex-float syntax too.
    0x1.5c87fb0000000p-84 is the float32 boundary "7.038531e-26" sits just
    below; the extra low mantissa bit spells boundary + 2^-136, which is
    above it, so the correct rounding is the upper neighbor — the opposite
    side from the decimal case. Expectation is Go ParseFloat output."""
    from neoedgex.contract._float import float32_bits

    above = "0x1.5c87fb0000001p-84"
    assert float32_bits(convert_to_typed_value(above, DataType.FLOAT)) == 0x15AE43FE


@pytest.mark.parametrize(
    "dest", [DataType.DOUBLE, DataType.FLOAT, DataType.INT64, DataType.STRING, DataType.BOOL]
)
@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_float_nan_and_inf_are_never_publishable(value: float, dest: DataType) -> None:
    with pytest.raises(ValueError, match="NaN and Inf"):
        convert_to_typed_value(value, dest)


def test_float_destination_narrows_and_range_checks() -> None:
    assert convert_to_typed_value(25.34, DataType.FLOAT) == 25.34
    assert convert_to_typed_value(25, DataType.FLOAT) == 25.0
    assert type(convert_to_typed_value(25, DataType.FLOAT)) is float
    assert convert_to_typed_value(3.4028234663852886e38, DataType.FLOAT) == 3.4028235e38
    with pytest.raises(ValueError, match="out of range"):
        convert_to_typed_value(1e300, DataType.FLOAT)
    # double keeps the value untouched.
    assert convert_to_typed_value(1e300, DataType.DOUBLE) == 1e300


def test_f2_float32_upper_bound_is_judged_after_narrowing() -> None:
    """F2: overflow is decided by round-to-nearest narrowing, not by comparing
    against the exact double value of MaxFloat32. Both the exact constant and
    the decimal Go's ParseFloat(s, 32) round-trips it through are accepted; a
    real overflow is still refused."""
    assert convert_to_typed_value(3.4028234663852886e38, DataType.FLOAT) == 3.4028235e38
    assert convert_to_typed_value("3.4028235e+38", DataType.FLOAT) == 3.4028235e38
    with pytest.raises(ValueError, match="out of range"):
        convert_to_typed_value(1e300, DataType.FLOAT)
    with pytest.raises(ValueError, match="out of range"):
        convert_to_typed_value("1e300", DataType.FLOAT)
    # Past the rounding boundary (~3.4028236e+38) the narrowing overflows.
    with pytest.raises(ValueError, match="out of range"):
        convert_to_typed_value(3.5e38, DataType.FLOAT)


def test_f2_dev6_native_double_inside_the_narrowing_window_is_accepted() -> None:
    """F2 / DEV-6: 3.4028235e+38 as a *native* double sits just above MaxFloat32
    yet rounds into float32. Go rejects it (strictly > MaxFloat32), Python
    narrows and accepts: once restored, an SDK-decoded float32 is
    indistinguishable from a true double, so refusing it would break the SDK's
    own decode -> republish round trip."""
    assert convert_to_typed_value(3.4028235e38, DataType.FLOAT) == 3.4028235e38


def test_bool_source_converts_to_every_scalar() -> None:
    assert convert_to_typed_value(True, DataType.INT16) == 1
    assert convert_to_typed_value(False, DataType.UINT32) == 0
    assert convert_to_typed_value(True, DataType.DOUBLE) == 1.0
    assert type(convert_to_typed_value(True, DataType.DOUBLE)) is float
    assert convert_to_typed_value(False, DataType.FLOAT) == 0.0
    assert convert_to_typed_value(True, DataType.STRING) == "true"
    assert convert_to_typed_value(False, DataType.STRING) == "false"
    assert convert_to_typed_value(True, DataType.BOOL) is True


def test_number_to_bool_is_nonzero() -> None:
    assert convert_to_typed_value(5, DataType.BOOL) is True
    assert convert_to_typed_value(0, DataType.BOOL) is False
    assert convert_to_typed_value(0.0, DataType.BOOL) is False
    assert convert_to_typed_value(-0.5, DataType.BOOL) is True
    with pytest.raises(ValueError, match="cannot convert from type 'string' to 'bool'"):
        convert_to_typed_value("true", DataType.BOOL)


def test_number_to_string_uses_go_formatting() -> None:
    assert convert_to_typed_value(5, DataType.STRING) == "5"
    assert convert_to_typed_value(-12345, DataType.STRING) == "-12345"
    assert convert_to_typed_value(25.34, DataType.STRING) == "2.534e+01"


def test_raw_only_converts_to_raw() -> None:
    assert convert_to_typed_value(b"\x01\x02", DataType.RAW) == b"\x01\x02"
    converted = convert_to_typed_value(bytearray(b"\x01"), DataType.RAW)
    assert converted == b"\x01"
    assert type(converted) is bytes
    with pytest.raises(ValueError, match="cannot convert from type 'raw' to 'string'"):
        convert_to_typed_value(b"ab", DataType.STRING)
    with pytest.raises(ValueError, match="cannot convert from type 'string' to 'raw'"):
        convert_to_typed_value("ab", DataType.RAW)


def test_unsupported_destination_and_unsupported_source() -> None:
    with pytest.raises(ValueError, match="unsupported data type"):
        convert_to_typed_value(5, DataType.UNDEFINED)
    with pytest.raises(ValueError, match="nil value is not supported"):
        convert_to_typed_value(None, DataType.DOUBLE)
    with pytest.raises(ValueError, match="unsupported value type"):
        convert_to_typed_value(object(), DataType.STRING)
    with pytest.raises(ValueError, match="unsupported value type"):
        convert_to_typed_value({"a": 1}, DataType.STRING)


def test_datetime_is_refused_with_a_hint_to_format_it() -> None:
    with pytest.raises(ValueError, match="datetime is not supported"):
        convert_to_typed_value(datetime(2026, 1, 1, tzinfo=UTC), DataType.STRING)


# -----------------------------------------------------------------------------
# PortFieldData: the {type, value} string form (mock config / device facing)
# -----------------------------------------------------------------------------


def test_port_field_data_wire_shape_is_type_and_value_only() -> None:
    field = PortFieldData(type=DataType.INT64, value="42")
    assert field.to_dict() == {"type": "int64", "value": "42"}
    # A legacy payload carrying "format" decodes by ignoring it.
    assert (
        PortFieldData.from_dict({"type": "int64", "format": "int64", "value": "42"}) == field
    )
    assert PortFieldData.from_dict({}) == PortFieldData.empty()
    assert PortFieldData.from_dict({"type": "bogus", "value": "1"}).type == DataType.UNDEFINED


def test_empty_port_field_data_is_undefined() -> None:
    empty = PortFieldData.empty()
    assert empty.type == DataType.UNDEFINED
    assert empty.value == ""


def test_new_with_string_validates_against_the_declared_type() -> None:
    assert PortFieldData.new_with_string("25", DataType.INT16) == PortFieldData(
        DataType.INT16, "25"
    )
    # The original text is kept verbatim, not normalized.
    assert PortFieldData.new_with_string("0025", DataType.INT16).value == "0025"
    with pytest.raises(ValueError, match="not compatible with type"):
        PortFieldData.new_with_string("x", DataType.INT16)
    with pytest.raises(ValueError, match="out of range"):
        PortFieldData.new_with_string("70000", DataType.INT16)
    with pytest.raises(ValueError, match="unsupported data type"):
        PortFieldData.new_with_string("x", DataType.UNDEFINED)


def test_new_with_any_converts_then_stringifies() -> None:
    assert PortFieldData.new_with_any(42, DataType.INT64).value == "42"
    assert PortFieldData.new_with_any(25.5, DataType.DOUBLE).value == "2.55e+01"
    assert PortFieldData.new_with_any(25.34, DataType.FLOAT).value == "2.534e+01"
    assert PortFieldData.new_with_any(True, DataType.INT32).value == "1"
    assert PortFieldData.new_with_any(b"hello", DataType.RAW).value == "aGVsbG8="
    with pytest.raises(ValueError, match="nil value is not supported"):
        PortFieldData.new_with_any(None, DataType.INT64)
    with pytest.raises(ValueError, match="out of range"):
        PortFieldData.new_with_any(-1, DataType.UINT16)
    with pytest.raises(ValueError, match="NaN"):
        PortFieldData.new_with_any(math.nan, DataType.INT64)


def test_get_any_value_reads_the_string_back_in_its_type() -> None:
    assert PortFieldData(DataType.INT64, "42").get_any_value() == 42
    assert PortFieldData(DataType.RAW, "aGVsbG8=").get_any_value() == b"hello"
    assert PortFieldData(DataType.STRING, "0025").get_any_value() == "0025"
    # float fields come back as the shortest decimal the float32 carries, not
    # as the widened 25.34000015258789.
    assert PortFieldData(DataType.FLOAT, "25.34").get_any_value() == 25.34
    assert PortFieldData(DataType.FLOAT, "2.534e+01").get_any_value() == 25.34
    assert PortFieldData(DataType.DOUBLE, "2.534e+01").get_any_value() == 25.34
    with pytest.raises(ValueError, match="unsupported destination type"):
        PortFieldData.empty().get_any_value()


def test_bool_string_parsing_only_accepts_the_exact_literal() -> None:
    # The trap: anything but "true" reads back as False, and it never fails.
    assert PortFieldData(DataType.BOOL, "true").get_any_value() is True
    assert PortFieldData(DataType.BOOL, "TRUE").get_any_value() is False
    assert PortFieldData(DataType.BOOL, "1").get_any_value() is False
    assert PortFieldData(DataType.BOOL, "garbage").get_any_value() is False
    assert convert_value_by_type("oops", DataType.BOOL) is False


def test_f6_string_float_parsing_matches_go_parsefloat() -> None:
    """F6: the {type, value} channel (mock configs, device-facing code) parses
    exactly like Go ParseFloat — NaN/Inf literals, digit-separating underscores
    and hex-float forms all parse."""
    assert math.isnan(convert_value_by_type("nan", DataType.DOUBLE))
    assert math.isnan(convert_value_by_type("NaN", DataType.FLOAT))
    assert math.isinf(convert_value_by_type("+Inf", DataType.DOUBLE))
    assert convert_value_by_type("1_000.5", DataType.DOUBLE) == 1000.5
    assert convert_value_by_type("0x1p+3", DataType.DOUBLE) == 8.0


@pytest.mark.parametrize("text", [" 1.5", "1.5 ", "abc", "", "1.2.3", "0x1.8"])
def test_f6_string_float_parsing_is_still_strict_about_syntax(text: str) -> None:
    """F6: permissiveness stops at Go's grammar — surrounding whitespace stays
    a syntax error, and a hex float without its mandatory p exponent too."""
    with pytest.raises(ValueError, match="invalid syntax"):
        convert_value_by_type(text, DataType.DOUBLE)


@pytest.mark.parametrize("dest", [DataType.FLOAT, DataType.DOUBLE], ids=["float", "double"])
@pytest.mark.parametrize("text", ["NaN", "nan", "+Inf", "-Inf"])
def test_f6_the_wire_string_path_still_refuses_nan_and_inf(text: str, dest: DataType) -> None:
    """F6: only the {type, value} channel is permissive. convert.py's string
    path feeds the wire, where NaN/Inf are unpublishable in both SDKs."""
    with pytest.raises(ValueError, match="NaN and Inf"):
        convert_to_typed_value(text, dest)


def test_convert_to_same_type_short_circuits_before_validation() -> None:
    unparseable = PortFieldData(DataType.INT64, "not-an-int")
    assert unparseable.convert_to(DataType.INT64) == unparseable
    # string -> string keeps the text as stored, no re-normalization.
    assert PortFieldData(DataType.STRING, "0025").convert_to(DataType.STRING).value == "0025"


def test_convert_to_string_renormalizes_numbers() -> None:
    # Matches Go ConvertTo: the destination decides the rendering.
    assert PortFieldData(DataType.INT16, "0025").convert_to(DataType.STRING).value == "25"
    assert (
        PortFieldData(DataType.DOUBLE, "2.534e+01").convert_to(DataType.STRING).value
        == "2.534e+01"
    )
    assert PortFieldData(DataType.BOOL, "true").convert_to(DataType.STRING).value == "true"


def test_convert_to_number_and_bool_destinations() -> None:
    assert PortFieldData(DataType.INT16, "25").convert_to(DataType.DOUBLE).value == "2.5e+01"
    assert PortFieldData(DataType.INT16, "25").convert_to(DataType.FLOAT).value == "2.5e+01"
    assert PortFieldData(DataType.STRING, "2.5e1").convert_to(DataType.DOUBLE).value == "2.5e+01"
    assert PortFieldData(DataType.STRING, "25").convert_to(DataType.INT16).value == "25"
    assert PortFieldData(DataType.BOOL, "true").convert_to(DataType.INT32).value == "1"
    assert PortFieldData(DataType.BOOL, "false").convert_to(DataType.DOUBLE).value == "0e+00"
    assert PortFieldData(DataType.DOUBLE, "2.9e+01").convert_to(DataType.INT16).value == "29"
    assert PortFieldData(DataType.INT16, "0").convert_to(DataType.BOOL).value == "false"
    assert PortFieldData(DataType.INT16, "5").convert_to(DataType.BOOL).value == "true"
    assert PortFieldData(DataType.DOUBLE, "0e+00").convert_to(DataType.BOOL).value == "false"


@pytest.mark.parametrize("text", ["0000", "+0", "-0"])
def test_f3_every_spelling_of_zero_converts_to_false(text: str) -> None:
    """F3: the bool destination reads the parsed *number*, not the raw text —
    "0000"/"+0"/"-0" are zero and therefore false (they used to come back
    true because only the exact literal "0" was recognised)."""
    assert PortFieldData(DataType.INT16, text).convert_to(DataType.BOOL).value == "false"


def test_f3_nonzero_spellings_still_convert_to_true() -> None:
    assert PortFieldData(DataType.INT16, "0001").convert_to(DataType.BOOL).value == "true"
    assert PortFieldData(DataType.INT16, "-1").convert_to(DataType.BOOL).value == "true"


@pytest.mark.parametrize(
    "dest",
    [DataType.INT32, DataType.DOUBLE, DataType.BOOL, DataType.STRING],
    ids=lambda t: f"to-{t.name}",
)
def test_f5_convert_to_parses_its_source_as_strictly_as_the_wire(dest: DataType) -> None:
    """F5: convert_to has no lenient parse of its own any more — an int16 field
    holding "1_0" is a syntax error whichever destination it is asked for."""
    with pytest.raises(ValueError, match="invalid syntax"):
        PortFieldData(DataType.INT16, "1_0").convert_to(dest)


def test_f5_surrounding_whitespace_and_unsigned_signs_are_refused() -> None:
    """F5: Go ParseInt accepts no surrounding whitespace, and ParseUint accepts
    no sign at all."""
    with pytest.raises(ValueError, match="invalid syntax"):
        PortFieldData(DataType.INT16, " 5 ").convert_to(DataType.INT32)
    with pytest.raises(ValueError, match="invalid syntax"):
        PortFieldData(DataType.UINT16, "+5").convert_to(DataType.INT32)


def test_convert_to_refuses_what_the_matrix_refuses() -> None:
    with pytest.raises(ValueError, match="cannot convert from type 'string' to 'bool'"):
        PortFieldData(DataType.STRING, "true").convert_to(DataType.BOOL)
    with pytest.raises(ValueError, match="cannot convert from type 'raw' to 'string'"):
        PortFieldData(DataType.RAW, "AQI=").convert_to(DataType.STRING)
    with pytest.raises(ValueError, match="cannot convert from type 'string' to 'raw'"):
        PortFieldData(DataType.STRING, "ab").convert_to(DataType.RAW)
    with pytest.raises(ValueError, match="out of range"):
        PortFieldData(DataType.DOUBLE, "1e+300").convert_to(DataType.FLOAT)
    with pytest.raises(ValueError, match="out of range"):
        PortFieldData(DataType.INT32, "70000").convert_to(DataType.INT16)


def test_get_value_and_cast_checks_the_python_type() -> None:
    assert get_value_and_cast(PortFieldData(DataType.INT64, "42"), int) == 42
    with pytest.raises(TypeError, match="cannot cast"):
        get_value_and_cast(PortFieldData(DataType.INT64, "42"), str)


def test_c3_get_value_and_cast_never_hands_a_bool_to_an_int_target() -> None:
    """C3: bool is an int subclass in Python, but the wire types are unrelated.
    A plain isinstance check would let a bool field satisfy an int cast, where
    Go's type assertion fails — so the bool case is checked before it. `object`
    is the counterpart of Go's `any` and takes everything."""
    boolean = PortFieldData(DataType.BOOL, "true")
    assert get_value_and_cast(boolean, bool) is True
    assert get_value_and_cast(boolean, object) is True
    with pytest.raises(TypeError, match="cannot cast"):
        get_value_and_cast(boolean, int)
    # And the reverse direction stays closed too.
    with pytest.raises(TypeError, match="cannot cast"):
        get_value_and_cast(PortFieldData(DataType.INT64, "1"), bool)


# -----------------------------------------------------------------------------
# C1 / C5: convert_value_by_type edge cases
# -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    ["!!!", "A Q I =", "AQI=extra"],
    ids=["non-alphabet", "embedded-spaces", "data-after-padding"],
)
def test_c1_raw_base64_decoding_is_strict(text: str) -> None:
    """C1: Go's base64.StdEncoding refuses anything outside the alphabet and
    anything trailing the padding. Python's default b64decode silently *skips*
    those characters, which would let a corrupt mock value decode to plausible
    bytes; validate=True is what closes the gap."""
    with pytest.raises(ValueError):
        convert_value_by_type(text, DataType.RAW)


def test_c1_well_formed_base64_still_decodes() -> None:
    assert convert_value_by_type("AQI=", DataType.RAW) == b"\x01\x02"
    assert convert_value_by_type("aGVsbG8=", DataType.RAW) == b"hello"
    assert convert_value_by_type("", DataType.RAW) == b""


def test_c5_a_float_out_of_range_says_both_what_failed_and_why() -> None:
    """C5: the two halves of the message are load-bearing — "cannot parse
    '<value>' as '<type>'" identifies the input, "value out of range"
    separates it from a syntax error (Go reports the same two kinds)."""
    with pytest.raises(ValueError) as excinfo:
        convert_value_by_type("1e400", DataType.DOUBLE)
    message = str(excinfo.value)
    assert "cannot parse" in message
    assert "value out of range" in message
    assert "'1e400'" in message and "'double'" in message

    # Same value, syntax intact, but out of float32 range for the narrower type.
    with pytest.raises(ValueError, match="cannot parse '1e400' as 'float': value out of range"):
        convert_value_by_type("1e400", DataType.FLOAT)


# -----------------------------------------------------------------------------
# convert_any_value: python value -> (wire text, detected type)
# -----------------------------------------------------------------------------


def test_convert_any_value_detects_the_wire_type() -> None:
    assert convert_any_value(True) == ("true", DataType.BOOL)
    assert convert_any_value(42) == ("42", DataType.INT64)
    assert convert_any_value(25.5) == ("2.55e+01", DataType.DOUBLE)
    assert convert_any_value(1e300) == ("1e+300", DataType.DOUBLE)
    assert convert_any_value("neoedgex") == ("neoedgex", DataType.STRING)
    assert convert_any_value(b"hello") == ("aGVsbG8=", DataType.RAW)


@pytest.mark.parametrize(
    "value", [datetime(2026, 3, 22, 10, 30, tzinfo=UTC), {"a": 1}, [1, 2], object()]
)
def test_convert_any_value_refuses_values_with_no_wire_type(value: Any) -> None:
    with pytest.raises(ValueError, match="unsupported value type"):
        convert_any_value(value)


def test_convert_any_value_rejects_none() -> None:
    with pytest.raises(ValueError, match="nil value is not supported"):
        convert_any_value(None)


def test_c4_integers_above_int64_are_reported_as_uint64_and_read_back() -> None:
    """C4: Python ints are unbounded, the wire domain is not. Above MaxInt64 the
    detected type switches to uint64 — which is what Go reports — and the pair
    it returns must survive a round trip through its own reported type,
    otherwise the value is unreadable to every consumer."""
    assert convert_any_value(2**63) == ("9223372036854775808", DataType.UINT64)
    assert convert_any_value(2**64 - 1) == ("18446744073709551615", DataType.UINT64)
    assert convert_any_value(2**63 - 1) == ("9223372036854775807", DataType.INT64)
    assert convert_any_value(-(2**63)) == ("-9223372036854775808", DataType.INT64)

    for value in (2**63, 2**64 - 1, -(2**63)):
        text, detected = convert_any_value(value)
        assert convert_value_by_type(text, detected) == value


@pytest.mark.parametrize(
    "value", [2**64, 2**70, -(2**63) - 1], ids=["max-uint64+1", "far-above", "min-int64-1"]
)
def test_c4_integers_outside_the_wire_domain_have_no_readable_pair(value: int) -> None:
    """C4: outside [MinInt64, MaxUint64] no declarable type can read the value
    back, and Go cannot even represent it — so the conversion is refused rather
    than emitting a string nothing can parse."""
    with pytest.raises(ValueError, match="value out of range"):
        convert_any_value(value)


# -----------------------------------------------------------------------------
# schema / event models
# -----------------------------------------------------------------------------


def test_port_field_schema_is_type_only_and_tolerates_legacy_format() -> None:
    schema = PortFieldSchema.from_dict({"key": "temp", "type": "double", "format": "double"})
    assert schema == PortFieldSchema(key="temp", type=DataType.DOUBLE)
    assert schema.to_dict() == {"key": "temp", "type": "double"}
    assert PortFieldSchema.from_dict({"key": "x", "type": "jsonObject"}).type == (
        DataType.UNDEFINED
    )


def test_event_to_dict_always_includes_detail_even_when_empty() -> None:
    assert Event(code="E001").to_dict() == {"code": "E001", "detail": "", "updatedAt": 0}
    assert Event(code="E002", detail="broke", updated_at=42).to_dict() == {
        "code": "E002",
        "detail": "broke",
        "updatedAt": 42,
    }


def test_node_unmarshal_ignores_legacy_position_field() -> None:
    node = Node.from_dict(
        {
            "id": "node-1",
            "type": "custom",
            "position": {"x": 12.5, "y": 34.5},
            "data": {
                "name": "demo",
                "description": "test node",
                "inputs": {"input1": [{"key": "a", "type": "int64"}]},
                "outputs": {},
                "application": {"key": "app", "version": "2.0.0"},
                "settings": {"threshold": 3},
            },
        }
    )
    assert node.id == "node-1"
    assert node.type == "custom"
    assert node.data.name == "demo"
    assert node.data.inputs["input1"][0] == PortFieldSchema(key="a", type=DataType.INT64)
    assert node.data.application.version == "2.0.0"
    assert node.data.settings == {"threshold": 3}
