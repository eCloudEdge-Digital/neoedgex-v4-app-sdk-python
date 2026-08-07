"""CBOR codec unit tests.

The value-level behaviour is pinned by the golden fixture; this file covers
the structural work the fixture cannot express: the top-level map scanner
(which must skip nested and indefinite-length items without decoding them),
the integer head-width boundaries, and the envelope edges.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Any

import cbor2
import pytest

from neoedgex.contract import DataType, convert_to_typed_value
from neoedgex.contract.codec import (
    decode_field_with_schema,
    decode_natural,
    decode_neoflow_envelope,
    encode_data_map,
    encode_field,
    encode_neoflow_message,
    is_undefined,
    scan_data_map,
)

# -----------------------------------------------------------------------------
# scan_data_map: key -> raw value span
# -----------------------------------------------------------------------------


def test_scan_returns_raw_spans_in_wire_order() -> None:
    payload = cbor2.dumps({"a": 1, "b": "x", "c": True})
    spans = scan_data_map(payload)
    assert list(spans) == ["a", "b", "c"]
    assert spans == {"a": b"\x01", "b": cbor2.dumps("x"), "c": b"\xf5"}


def test_scan_skips_nested_containers_without_decoding_them() -> None:
    payload = cbor2.dumps({"arr": [1, 2], "map": {"x": [3]}, "tail": 1})
    spans = scan_data_map(payload)
    assert spans["arr"] == cbor2.dumps([1, 2])
    assert spans["map"] == cbor2.dumps({"x": [3]})
    # The field after the nested containers proves the skip landed exactly on
    # the next key.
    assert spans["tail"] == b"\x01"


def test_scan_empty_map() -> None:
    assert scan_data_map(bytes.fromhex("a0")) == {}


@pytest.mark.parametrize(
    ("name", "payload_hex", "key", "span_hex"),
    [
        # value is an indefinite-length text string ("ab" in two chunks)
        ("indefinite-text", "a161617f62616260ff", "a", "7f62616260ff"),
        # value is an indefinite-length array
        ("indefinite-array", "a161619f0102ff", "a", "9f0102ff"),
        # value is an indefinite-length map
        ("indefinite-map", "a16161bf616201ff", "a", "bf616201ff"),
        # value is a tagged item, and a tag wrapping another tag
        ("tag", "a16161d82a01", "a", "d82a01"),
        ("nested-tag", "a16161d82ad82a01", "a", "d82ad82a01"),
        # the *key* is an indefinite-length text string ("ab" in two chunks)
        ("indefinite-key", "a17f62616260ff01", "ab", "01"),
    ],
)
def test_scan_handles_indefinite_lengths_and_tags(
    name: str, payload_hex: str, key: str, span_hex: str
) -> None:
    spans = scan_data_map(bytes.fromhex(payload_hex))
    assert spans == {key: bytes.fromhex(span_hex)}


def test_scan_reads_an_indefinite_length_top_level_map() -> None:
    assert scan_data_map(bytes.fromhex("bf616101ff")) == {"a": b"\x01"}
    assert scan_data_map(bytes.fromhex("bfff")) == {}


def test_scan_last_duplicate_key_wins() -> None:
    # {"a": 1, "a": 2}
    assert scan_data_map(bytes.fromhex("a2616101616102")) == {"a": b"\x02"}


@pytest.mark.parametrize(
    ("name", "payload_hex", "message"),
    [
        ("array-top-level", "820102", "CBOR map expected"),
        ("scalar-top-level", "01", "CBOR map expected"),
        ("integer-key", "a10101", "map key is not a text string"),
        ("bytes-key", "a1410101", "map key is not a text string"),
        ("trailing-bytes", "a161610101", "extraneous data after CBOR map"),
        ("truncated-value", "a16161", "unexpected end of CBOR data"),
        ("truncated-string", "a161616441", "unexpected end of CBOR data"),
        ("unterminated-indefinite", "bf616101", "unexpected end of CBOR data"),
        ("stray-break", "a16161ff", "unexpected CBOR break code"),
        ("reserved-additional-info", "a161611c", "invalid CBOR additional information"),
    ],
)
def test_scan_rejects_malformed_input(name: str, payload_hex: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        scan_data_map(bytes.fromhex(payload_hex))


# -----------------------------------------------------------------------------
# encode_field / encode_data_map
# -----------------------------------------------------------------------------

_INT_ENCODINGS = [
    (0, "00"),
    (1, "01"),
    (23, "17"),  # last value inlined in the head
    (24, "1818"),  # first 1-byte argument
    (255, "18ff"),
    (256, "190100"),  # first 2-byte argument
    (65535, "19ffff"),
    (65536, "1a00010000"),  # first 4-byte argument
    (2**32 - 1, "1affffffff"),
    (2**32, "1b0000000100000000"),  # first 8-byte argument
    (2**64 - 1, "1bffffffffffffffff"),
    (-1, "20"),
    (-24, "37"),
    (-25, "3818"),
    (-(2**63), "3b7fffffffffffffff"),
]


@pytest.mark.parametrize(
    ("value", "expect_hex"), _INT_ENCODINGS, ids=[str(case[0]) for case in _INT_ENCODINGS]
)
def test_integers_use_the_shortest_head(value: int, expect_hex: str) -> None:
    assert encode_field(value, DataType.INT64).hex() == expect_hex
    assert encode_field(value, DataType.UINT64).hex() == expect_hex


def test_scalar_encodings() -> None:
    assert encode_field(True, DataType.BOOL).hex() == "f5"
    assert encode_field(False, DataType.BOOL).hex() == "f4"
    assert encode_field(None, DataType.DOUBLE).hex() == "f6"
    assert encode_field(None, DataType.RAW).hex() == "f6"
    # float is deliberately narrowed to single precision, double is not.
    assert encode_field(25.34, DataType.FLOAT).hex() == "fa41cab852"
    assert encode_field(25.34, DataType.DOUBLE).hex() == "fb4039570a3d70a3d7"
    assert encode_field("", DataType.STRING).hex() == "60"
    # utf-8 byte length, not character count
    assert encode_field("héllo", DataType.STRING).hex() == "6668c3a96c6c6f"
    assert encode_field(b"", DataType.RAW).hex() == "40"
    assert encode_field(b"\x01\x02", DataType.RAW).hex() == "420102"
    with pytest.raises(ValueError, match="unsupported data type"):
        encode_field(1, DataType.UNDEFINED)


def test_data_map_keeps_field_order_and_widens_its_head() -> None:
    assert encode_data_map([]).hex() == "a0"
    payload = encode_data_map(
        [("b", DataType.INT64, 1), ("a", DataType.STRING, "x"), ("c", DataType.BOOL, None)]
    )
    assert payload.hex() == "a3616201616161786163f6"
    assert list(scan_data_map(payload)) == ["b", "a", "c"]

    many = encode_data_map([(f"k{i:02d}", DataType.INT64, i) for i in range(24)])
    assert many[:2].hex() == "b818"  # map head switches to a 1-byte argument
    assert len(scan_data_map(many)) == 24


def test_envelope_layout_is_source_timestamp_data() -> None:
    data = encode_data_map([("k", DataType.INT64, 1)])
    payload = encode_neoflow_message("node-a", "2026-03-31T09:10:11Z", data)
    assert payload[0] == 0xA3
    assert list(scan_data_map(payload)) == ["source", "timestamp", "data"]
    # The data map is spliced in verbatim, never re-encoded.
    assert data in payload


def test_f11_envelope_decode_defaults_and_type_check() -> None:
    """F11 (CR-P5): a *null* source/timestamp is the Go zero value ``""`` — the
    Go codec unmarshals CBOR null into a string field as the empty string, and
    D7 ("input only a non-SDK publisher can produce needs no defence") was
    struck, so the envelope now follows Go instead of dropping the message.
    A non-text value stays an unmarshal error there, so it still raises here
    and the whole message is dropped."""
    # No keys at all: every field is its zero value.
    assert decode_neoflow_envelope(bytes.fromhex("a0")) == ("", "", b"")
    # {"source": null} and {"timestamp": null} — carried, but null.
    assert decode_neoflow_envelope(bytes.fromhex("a166736f75726365f6")) == ("", "", b"")
    assert decode_neoflow_envelope(bytes.fromhex("a16974696d657374616d70f6")) == ("", "", b"")
    # A non-text value is a different story: {"source": 5} / {"timestamp": 5}.
    with pytest.raises(ValueError, match="envelope key 'source' is not a text string"):
        decode_neoflow_envelope(bytes.fromhex("a166736f7572636505"))
    with pytest.raises(ValueError, match="envelope key 'timestamp' is not a text string"):
        decode_neoflow_envelope(bytes.fromhex("a16974696d657374616d7005"))


# -----------------------------------------------------------------------------
# is_undefined / decode_natural
# -----------------------------------------------------------------------------


def test_is_undefined_covers_null_and_cbor_undefined_only() -> None:
    assert is_undefined(b"\xf6") is True
    assert is_undefined(b"\xf7") is True
    assert is_undefined(b"\xf5") is False
    assert is_undefined(b"\xf4") is False
    assert is_undefined(b"\x00") is False
    assert is_undefined(b"") is False
    assert is_undefined(b"\xf6\xf6") is False


def test_decode_natural_delivers_the_natural_domain() -> None:
    assert decode_natural(b"\xf6") is None
    assert decode_natural(b"\xf5") is True
    assert decode_natural(b"\x05") == 5
    assert decode_natural(bytes.fromhex("1bffffffffffffffff")) == 2**64 - 1
    assert decode_natural(bytes.fromhex("3b7fffffffffffffff")) == -(2**63)
    assert decode_natural(cbor2.dumps("x")) == "x"
    assert decode_natural(cbor2.dumps(b"x")) == b"x"
    assert decode_natural(bytes.fromhex("fa41cab852")) == 25.34000015258789


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("beyond-int64-negative", cbor2.dumps(-(2**63) - 1)),
        ("beyond-uint64", cbor2.dumps(2**64)),
        ("array", cbor2.dumps([1, 2])),
        ("map", cbor2.dumps({"a": 1})),
        ("datetime", cbor2.dumps(datetime(2026, 1, 1, tzinfo=UTC))),
        ("unknown-tag", bytes.fromhex("d82a01")),
    ],
)
def test_decode_natural_refuses_values_outside_the_natural_domain(
    name: str, value: bytes
) -> None:
    with pytest.raises(ValueError, match="unsupported value type"):
        decode_natural(value)


# -----------------------------------------------------------------------------
# decode_field_with_schema: float width and the asymmetric NaN/Inf routing
# -----------------------------------------------------------------------------


def test_single_precision_is_restored_to_its_shortest_decimal() -> None:
    single = bytes.fromhex("fa41cab852")
    assert decode_field_with_schema(single, DataType.FLOAT) == 25.34
    assert decode_field_with_schema(single, DataType.DOUBLE) == 25.34
    # Half precision widens naturally; no restore, the value is exact.
    assert decode_field_with_schema(bytes.fromhex("f94e57"), DataType.FLOAT) == 25.359375


def test_f2_max_float32_survives_decode_convert_encode_byte_identical() -> None:
    """F2: the largest finite float32 must come back on the wire unchanged.
    Restoring 0x7f7fffff yields 3.4028235e+38 — a double slightly above the
    exact MaxFloat32 — so a strict upper-bound check made the SDK refuse the
    value its own decoder had just produced."""
    span = bytes.fromhex("fa7f7fffff")
    restored = decode_field_with_schema(span, DataType.FLOAT)
    assert restored == 3.4028235e38
    assert encode_field(convert_to_typed_value(restored, DataType.FLOAT), DataType.FLOAT) == span
    # The same wire value read into a double tag restores the same decimal.
    assert decode_field_with_schema(span, DataType.DOUBLE) == 3.4028235e38


@pytest.mark.parametrize(
    ("name", "span_hex", "tag_type", "passes_through"),
    [
        # Mirrors Go v2.1.0: only the two width-mismatch combinations it routes
        # through the conversion matrix reject NaN/Inf. Everything else that
        # decodes straight into a float tag delivers them as-is.
        ("f32-nan-into-float", "fa7fc00000", DataType.FLOAT, True),
        ("f32-nan-into-double", "fa7fc00000", DataType.DOUBLE, False),
        ("f64-nan-into-double", "fb7ff8000000000000", DataType.DOUBLE, True),
        ("f64-nan-into-float", "fb7ff8000000000000", DataType.FLOAT, False),
        ("f16-nan-into-float", "f97e00", DataType.FLOAT, True),
        ("f16-nan-into-double", "f97e00", DataType.DOUBLE, True),
        ("f64-inf-into-double", "fb7ff0000000000000", DataType.DOUBLE, True),
        ("f32-neginf-into-float", "faff800000", DataType.FLOAT, True),
    ],
)
def test_nan_and_inf_routing_is_asymmetric(
    name: str, span_hex: str, tag_type: DataType, passes_through: bool
) -> None:
    span = bytes.fromhex(span_hex)
    if not passes_through:
        with pytest.raises(ValueError, match="NaN and Inf are not supported"):
            decode_field_with_schema(span, tag_type)
        return
    got: Any = decode_field_with_schema(span, tag_type)
    assert math.isnan(got) or math.isinf(got)


def test_non_float_tags_go_through_the_conversion_matrix() -> None:
    assert decode_field_with_schema(bytes.fromhex("623235"), DataType.INT16) == 25
    assert decode_field_with_schema(b"\x05", DataType.STRING) == "5"
    with pytest.raises(ValueError, match="cannot convert"):
        decode_field_with_schema(cbor2.dumps(b"\x01"), DataType.STRING)
