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
