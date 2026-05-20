from __future__ import annotations

from datetime import UTC, datetime

import pytest

from neoedgex.contract import (
    DataFormat,
    DataType,
    Event,
    Node,
    PortFieldData,
    convert_any_value,
    convert_value_by_format,
)


def test_new_port_field_data_with_any_rejects_none() -> None:
    with pytest.raises(ValueError, match="nil value is not supported"):
        PortFieldData.new_with_any(None, DataFormat.INT64)


def test_convert_any_value_rejects_none() -> None:
    with pytest.raises(ValueError, match="nil value is not supported"):
        convert_any_value(None)


def test_convert_any_value_bool_int_float_string_datetime_and_bytes() -> None:
    assert convert_any_value(True) == ("true", DataFormat.BOOL)
    assert convert_any_value(42) == ("42", DataFormat.INT64)
    assert convert_any_value(25.5) == ("2.55e+01", DataFormat.DOUBLE)
    assert convert_any_value("neoedgex") == ("neoedgex", DataFormat.STRING)
    assert convert_any_value(datetime(2026, 3, 22, 10, 30, tzinfo=UTC)) == (
        "2026-03-22T10:30:00Z",
        DataFormat.DATETIME,
    )
    assert convert_any_value(b"hello") == ("aGVsbG8=", DataFormat.BASE64)


def test_convert_value_by_format_round_trips_core_types() -> None:
    assert convert_value_by_format("42", DataFormat.INT64) == 42
    assert convert_value_by_format("true", DataFormat.BOOL) is True
    assert convert_value_by_format("aGVsbG8=", DataFormat.BASE64) == b"hello"
    assert convert_value_by_format("2026-03-22T10:30:00Z", DataFormat.DATETIME) == datetime(
        2026, 3, 22, 10, 30, tzinfo=UTC
    )


def test_convert_value_by_format_matches_go_bool_and_datetime_parsing() -> None:
    assert convert_value_by_format("3", DataFormat.INT16) == 3
    assert convert_value_by_format("3", DataFormat.FLOAT) == pytest.approx(3.0)
    assert convert_value_by_format("3.14", DataFormat.DOUBLE) == pytest.approx(3.14)
    assert convert_value_by_format("oops", DataFormat.BOOL) is False
    assert convert_value_by_format("NaN", DataFormat.DOUBLE) != convert_value_by_format(
        "NaN", DataFormat.DOUBLE
    )
    assert convert_value_by_format("Inf", DataFormat.DOUBLE) == float("inf")

    with pytest.raises(ValueError):
        convert_value_by_format("3.14", DataFormat.INT16)
    with pytest.raises(ValueError, match="RFC3339"):
        convert_value_by_format("2026-03-22 10:30:00+00:00", DataFormat.DATETIME)
    with pytest.raises(ValueError, match="RFC3339"):
        convert_value_by_format("2026-03-22T10:30:00", DataFormat.DATETIME)


def test_float_and_double_scientific_formatting_matches_go_cases() -> None:
    assert PortFieldData.new_with_any(42, DataFormat.FLOAT).value == "4.2e+01"
    assert PortFieldData.new_with_any(42, DataFormat.DOUBLE).value == "4.2e+01"
    assert PortFieldData.new_with_any(25.5, DataFormat.FLOAT).value == "2.55e+01"
    assert PortFieldData.new_with_any(25.5, DataFormat.DOUBLE).value == "2.55e+01"
    assert PortFieldData.new_with_any(0.1, DataFormat.FLOAT).value == "1e-01"
    assert PortFieldData.new_with_any(0.1, DataFormat.DOUBLE).value == "1e-01"
    assert PortFieldData.new_with_any(1.234567, DataFormat.FLOAT).value == "1.234567e+00"
    assert PortFieldData.new_with_any(1.234567, DataFormat.DOUBLE).value == "1.234567e+00"
    assert PortFieldData.new_with_any(16777217.0, DataFormat.FLOAT).value == "1.6777216e+07"
    assert PortFieldData.new_with_any(16777217.0, DataFormat.DOUBLE).value == "1.6777217e+07"
    assert PortFieldData.new_with_any(3.4028235e38, DataFormat.FLOAT).value == "3.4028235e+38"
    assert PortFieldData.new_with_any(3.4028235e38, DataFormat.DOUBLE).value == "3.4028235e+38"
    assert (
        PortFieldData.new_with_any(1.17549435e-38, DataFormat.FLOAT).value == "1.1754944e-38"
    )
    assert (
        PortFieldData.new_with_any(1.17549435e-38, DataFormat.DOUBLE).value
        == "1.17549435e-38"
    )


def test_publish_conversion_handles_numeric_and_time_formats() -> None:
    assert PortFieldData.new_with_any(42, DataFormat.DOUBLE).value == "4.2e+01"
    assert PortFieldData.new_with_any(True, DataFormat.INT32).value == "1"
    assert PortFieldData.new_with_any(1711094400.9, DataFormat.SECOND).value == "1711094400"
    assert PortFieldData.new_with_any(
        datetime(2026, 3, 22, 10, 30, tzinfo=UTC), DataFormat.DATETIME
    ).value == "2026-03-22T10:30:00Z"


def test_publish_conversion_time_magnitude_and_local_timezone_match_go() -> None:
    expected_local = datetime.fromtimestamp(1711094400).astimezone().replace(microsecond=0)
    expected_local_string = expected_local.isoformat().replace("+00:00", "Z")

    assert PortFieldData.new_with_any(1711094400, DataFormat.SECOND).value == "1711094400"
    assert PortFieldData.new_with_any(1711094400000, DataFormat.SECOND).value == "1711094400"
    assert PortFieldData.new_with_any(1711094400000000, DataFormat.SECOND).value == "1711094400"
    assert (
        PortFieldData.new_with_any(1711094400000000000, DataFormat.SECOND).value
        == "1711094400"
    )

    assert PortFieldData.new_with_any(1711094400, DataFormat.DATETIME).value == expected_local_string
    assert (
        PortFieldData.new_with_any(1711094400000, DataFormat.DATETIME).value
        == expected_local_string
    )
    assert (
        PortFieldData.new_with_any(1711094400000000, DataFormat.DATETIME).value
        == expected_local_string
    )
    assert (
        PortFieldData.new_with_any(1711094400000000000, DataFormat.DATETIME).value
        == expected_local_string
    )

    assert convert_value_by_format("1711094400", DataFormat.SECOND) == expected_local
    assert convert_value_by_format("1711094400000", DataFormat.MILLISECOND) == expected_local


def test_publish_conversion_rejects_nan_and_out_of_range() -> None:
    with pytest.raises(ValueError, match="NaN"):
        PortFieldData.new_with_any(float("nan"), DataFormat.INT64)
    with pytest.raises(ValueError, match="out of range"):
        PortFieldData.new_with_any(-1, DataFormat.UINT16)


def test_missing_field_can_use_empty_field() -> None:
    empty = PortFieldData.empty()
    assert empty.type == DataType.UNDEFINED
    assert empty.format == DataFormat.UNDEFINED
    assert empty.value == ""


def test_event_to_dict_always_includes_detail_even_when_empty() -> None:
    event = Event(code="E001")
    payload = event.to_dict()
    assert payload == {"code": "E001", "detail": "", "updatedAt": 0}
    assert "detail" in payload


def test_event_to_dict_passes_through_non_empty_detail() -> None:
    event = Event(code="E002", detail="something broke", updated_at=42)
    assert event.to_dict() == {
        "code": "E002",
        "detail": "something broke",
        "updatedAt": 42,
    }


### FormatJson #################################################################


def test_new_port_field_data_accepts_map_as_json() -> None:
    field = PortFieldData.new_with_any({"foo": "bar"}, DataFormat.JSON)
    assert field.type == DataType.STRING
    assert field.format == DataFormat.JSON
    assert field.value == '{"foo": "bar"}'


def test_new_port_field_data_accepts_list_as_json() -> None:
    field = PortFieldData.new_with_any([1, "two", True], DataFormat.JSON)
    assert field.format == DataFormat.JSON
    assert field.value == '[1, "two", true]'


def test_new_port_field_data_accepts_dataclass_as_json() -> None:
    from dataclasses import asdict, dataclass

    @dataclass
    class Sample:
        name: str
        age: int

    # json.dumps does not accept arbitrary dataclasses directly; the
    # idiomatic Python path is to dump asdict(...). The SDK still accepts
    # the dict-form (any value that json.dumps can serialise).
    field = PortFieldData.new_with_any(asdict(Sample(name="x", age=7)), DataFormat.JSON)
    assert field.value == '{"name": "x", "age": 7}'


def test_new_port_field_data_accepts_empty_object_and_array() -> None:
    empty_obj = PortFieldData.new_with_any({}, DataFormat.JSON)
    assert empty_obj.value == "{}"

    empty_arr = PortFieldData.new_with_any([], DataFormat.JSON)
    assert empty_arr.value == "[]"


def test_new_port_field_data_passes_string_as_raw_json() -> None:
    # Pre-serialised JSON object as a Python str passes through verbatim.
    input_str = '{"foo":"bar","id":9007199254740993}'
    field = PortFieldData.new_with_any(input_str, DataFormat.JSON)
    assert field.value == input_str


def test_new_port_field_data_passes_bytes_as_raw_json() -> None:
    field = PortFieldData.new_with_any(b"[1,2,3]", DataFormat.JSON)
    assert field.value == "[1,2,3]"


def test_new_port_field_data_passes_bytearray_as_raw_json() -> None:
    field = PortFieldData.new_with_any(bytearray(b'{"foo":"bar"}'), DataFormat.JSON)
    assert field.value == '{"foo":"bar"}'


def test_new_port_field_data_rejects_scalar_marshal_as_json() -> None:
    # json.dumps of these scalars produces JSON primitives like "42",
    # "3.14", "true" — all rejected by the shape check.
    for value in [42, 3.14, True, False]:
        with pytest.raises(ValueError, match="not a JSON object or array"):
            PortFieldData.new_with_any(value, DataFormat.JSON)


def test_new_port_field_data_rejects_scalar_string_as_json() -> None:
    # A plain quoted-string is valid JSON, but not an object/array.
    with pytest.raises(ValueError, match="not a JSON object or array"):
        PortFieldData.new_with_any('"hello"', DataFormat.JSON)


def test_new_port_field_data_rejects_invalid_json_string() -> None:
    with pytest.raises(ValueError, match="not valid JSON"):
        PortFieldData.new_with_any("{not valid json}", DataFormat.JSON)


def test_new_port_field_data_rejects_invalid_json_bytes() -> None:
    with pytest.raises(ValueError, match="not valid JSON"):
        PortFieldData.new_with_any(b"{not valid json}", DataFormat.JSON)


def test_new_port_field_data_rejects_empty_string_as_json() -> None:
    with pytest.raises(ValueError, match="not valid JSON"):
        PortFieldData.new_with_any("", DataFormat.JSON)


def test_new_port_field_data_rejects_non_marshalable_as_json() -> None:
    with pytest.raises(ValueError, match="cannot encode"):
        PortFieldData.new_with_any({1, 2, 3}, DataFormat.JSON)


def test_new_port_field_data_accepts_json_with_whitespace() -> None:
    # Shape check trims leading whitespace; stored value preserves the original.
    input_str = '  \n  {"foo":"bar"}  \n  '
    field = PortFieldData.new_with_any(input_str, DataFormat.JSON)
    assert field.value == input_str


def test_convert_value_by_format_returns_raw_json_string() -> None:
    # Decode path is a no-op: the wire string is handed back as-is.
    raw = '{"id":9007199254740993,"label":"demo"}'
    got = convert_value_by_format(raw, DataFormat.JSON)
    assert isinstance(got, str)
    assert got == raw


def test_json_is_isolated_from_other_formats() -> None:
    other_formats = [
        DataFormat.BOOL,
        DataFormat.INT16,
        DataFormat.INT32,
        DataFormat.INT64,
        DataFormat.UINT16,
        DataFormat.UINT32,
        DataFormat.UINT64,
        DataFormat.FLOAT,
        DataFormat.DOUBLE,
        DataFormat.STRING,
        DataFormat.SECOND,
        DataFormat.MILLISECOND,
        DataFormat.DATETIME,
        DataFormat.BASE64,
    ]
    for fmt in other_formats:
        assert not DataFormat.JSON.can_convert_to(fmt), (
            f"json should not be convertible to {fmt.value}"
        )
        assert not fmt.can_convert_to(DataFormat.JSON), (
            f"{fmt.value} should not be convertible to json"
        )
    assert DataFormat.JSON.can_convert_to(DataFormat.JSON)


def test_port_field_data_get_any_value_round_trips_json() -> None:
    field = PortFieldData.new_with_any({"k": "v"}, DataFormat.JSON)
    assert field.get_any_value() == '{"k": "v"}'


def test_new_with_string_accepts_valid_json() -> None:
    cases = {
        "object": '{"foo":"bar"}',
        "array": "[1,2,3]",
    }
    for name, input_str in cases.items():
        field = PortFieldData.new_with_string(input_str, DataFormat.JSON)
        assert field.type == DataType.STRING, name
        assert field.format == DataFormat.JSON, name
        assert field.value == input_str, name


def test_new_with_string_rejects_invalid_json() -> None:
    # new_with_string must validate FormatJson the same way new_with_any
    # does — otherwise the public string constructor silently lets through
    # malformed or scalar JSON, because convert_value_by_format is a no-op
    # passthrough for JSON.
    reject_cases = {
        "malformed": "{not valid json}",
        "quoted scalar": '"hello"',
        "scalar number": "42",
        "scalar bool": "true",
        "json null": "null",
        "empty string": "",
        "plain string": "hello",
    }
    for name, input_str in reject_cases.items():
        with pytest.raises(ValueError):
            PortFieldData.new_with_string(input_str, DataFormat.JSON)


def test_new_with_any_rejects_non_finite_floats_in_json_dict() -> None:
    # json.dumps would otherwise emit non-standard tokens (NaN, Infinity,
    # -Infinity) that strict downstream parsers reject. With allow_nan=False
    # encode-side fails loudly.
    for bad in [float("nan"), float("inf"), float("-inf")]:
        with pytest.raises(ValueError, match="cannot encode"):
            PortFieldData.new_with_any({"x": bad}, DataFormat.JSON)
        with pytest.raises(ValueError, match="cannot encode"):
            PortFieldData.new_with_any([bad], DataFormat.JSON)


def test_new_with_any_rejects_non_standard_json_tokens_in_str_passthrough() -> None:
    # Python's json.loads is permissive by default and accepts NaN /
    # Infinity / -Infinity tokens. The SDK must reject them so the str
    # / bytes passthrough path is consistent with the encode path's
    # allow_nan=False guard, and so wire payloads stay parseable by
    # strict downstream JSON parsers.
    bad_inputs = [
        '{"x": NaN}',
        '{"x": Infinity}',
        '{"x": -Infinity}',
        '[NaN, Infinity, -Infinity]',
    ]
    for raw in bad_inputs:
        with pytest.raises(ValueError, match="non-standard JSON token"):
            PortFieldData.new_with_any(raw, DataFormat.JSON)
        with pytest.raises(ValueError, match="non-standard JSON token"):
            PortFieldData.new_with_any(raw.encode("utf-8"), DataFormat.JSON)
        with pytest.raises(ValueError, match="non-standard JSON token"):
            PortFieldData.new_with_string(raw, DataFormat.JSON)


### ############################################################################


def test_node_unmarshal_ignores_legacy_position_field() -> None:
    node = Node.from_dict(
        {
            "id": "node-1",
            "type": "custom",
            "position": {"x": 12.5, "y": 34.5},
            "data": {
                "name": "demo",
                "description": "test node",
                "inputs": {},
                "outputs": {},
                "application": {"key": "app", "version": "1.0.0"},
                "settings": {},
            },
        }
    )
    assert node.id == "node-1"
    assert node.type == "custom"
    assert node.data.name == "demo"
