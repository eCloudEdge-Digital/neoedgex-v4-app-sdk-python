from __future__ import annotations

import base64
import math
import re
import struct
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, TypeVar

from .types import DataFormat, DataType, coerce_data_format, get_data_type

_INT_LIMITS = {
    DataFormat.INT16: (-(2**15), 2**15 - 1),
    DataFormat.INT32: (-(2**31), 2**31 - 1),
    DataFormat.INT64: (-(2**63), 2**63 - 1),
}

_UINT_MAX = {
    DataFormat.UINT16: 2**16 - 1,
    DataFormat.UINT32: 2**32 - 1,
    DataFormat.UINT64: 2**64 - 1,
}

_FLOAT_BITS = {
    DataFormat.FLOAT: 32,
    DataFormat.DOUBLE: 64,
}

_RFC3339_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)
_LOCAL_TZ = datetime.now().astimezone().tzinfo or UTC
_T = TypeVar("_T")


@dataclass(slots=True)
class PortFieldData:
    type: DataType = DataType.UNDEFINED
    format: DataFormat = DataFormat.UNDEFINED
    value: str = ""

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "PortFieldData":
        return cls(
            type=DataType(payload.get("type", "")) if payload.get("type", "") in DataType._value2member_map_ else DataType.UNDEFINED,
            format=coerce_data_format(payload.get("format", "")),
            value=str(payload.get("value", "")),
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "type": self.type.value,
            "format": self.format.value,
            "value": self.value,
        }

    @classmethod
    def new_with_string(cls, value: str, data_format: DataFormat) -> "PortFieldData":
        if not data_format.get_type().is_supported():
            raise ValueError(f"unsupported data format '{data_format.value}'")
        try:
            convert_value_by_format(value, data_format)
        except Exception as exc:
            raise ValueError(
                f"value '{value}' is not compatible with format '{data_format.value}': {exc}"
            ) from exc
        return cls(type=data_format.get_type(), format=data_format, value=value)

    @classmethod
    def new_with_any(cls, any_value: Any, dest_format: DataFormat) -> "PortFieldData":
        if not dest_format.get_type().is_supported():
            raise ValueError(f"unsupported data format '{dest_format.value}'")
        if _is_nil_any_value(any_value):
            raise ValueError("nil value is not supported for conversion")
        value, src_format = convert_any_value(any_value)
        if not src_format.can_convert_to(dest_format):
            raise ValueError(
                f"cannot convert from format '{src_format.value}' to '{dest_format.value}'"
            )
        return cls(value=value, type=src_format.get_type(), format=src_format).convert_to(dest_format)

    @classmethod
    def empty(cls) -> "PortFieldData":
        return cls()

    def get_any_value(self) -> Any:
        return convert_value_by_format(self.value, self.format)

    def convert_to(self, dest_format: DataFormat) -> "PortFieldData":
        if not self.format.can_convert_to(dest_format):
            raise ValueError(
                f"cannot convert from format '{self.format.value}' to '{dest_format.value}'"
            )
        if self.format == dest_format:
            return PortFieldData(type=self.type, format=self.format, value=self.value)

        src_value = self.value
        src_format = self.format

        if dest_format in {
            DataFormat.INT16,
            DataFormat.INT32,
            DataFormat.INT64,
            DataFormat.UINT16,
            DataFormat.UINT32,
            DataFormat.UINT64,
            DataFormat.FLOAT,
            DataFormat.DOUBLE,
        }:
            if src_format in {
                DataFormat.INT16,
                DataFormat.INT32,
                DataFormat.INT64,
                DataFormat.UINT16,
                DataFormat.UINT32,
                DataFormat.UINT64,
            }:
                new_value = _convert_int_format_to_number_format(src_value, src_format, dest_format)
            elif src_format in {DataFormat.FLOAT, DataFormat.DOUBLE}:
                new_value = _convert_float_format_to_number_format(src_value, src_format, dest_format)
            elif src_format == DataFormat.BOOL:
                new_value = _convert_bool_format_to_number_format(src_value, dest_format)
            elif src_format == DataFormat.STRING:
                parsed = convert_value_by_format(src_value, dest_format)
                new_value = _stringify_value_for_format(parsed, dest_format)
            else:
                raise ValueError(
                    f"internal error: unsupported destination format '{dest_format.value}'"
                )

        elif dest_format in {DataFormat.SECOND, DataFormat.MILLISECOND, DataFormat.DATETIME}:
            if src_format in {
                DataFormat.INT16,
                DataFormat.INT32,
                DataFormat.INT64,
                DataFormat.UINT16,
                DataFormat.UINT32,
                DataFormat.UINT64,
                DataFormat.FLOAT,
                DataFormat.DOUBLE,
            }:
                new_value = _convert_number_format_to_time_format(src_value, src_format, dest_format)
            elif src_format in {DataFormat.SECOND, DataFormat.MILLISECOND, DataFormat.DATETIME}:
                new_value = _convert_time_format_to_time_format(src_value, src_format, dest_format)
            else:
                raise ValueError(
                    f"internal error: unsupported destination format '{dest_format.value}'"
                )

        elif dest_format == DataFormat.BOOL:
            if src_format in {
                DataFormat.INT16,
                DataFormat.INT32,
                DataFormat.INT64,
                DataFormat.UINT16,
                DataFormat.UINT32,
                DataFormat.UINT64,
                DataFormat.FLOAT,
                DataFormat.DOUBLE,
            }:
                new_value = _convert_number_format_to_bool_format(src_value, src_format)
            elif src_format == DataFormat.BOOL:
                new_value = src_value
            else:
                raise ValueError(
                    f"internal error: unsupported destination format '{dest_format.value}'"
                )

        elif dest_format == DataFormat.STRING:
            if src_format in {
                DataFormat.INT16,
                DataFormat.INT32,
                DataFormat.INT64,
                DataFormat.UINT16,
                DataFormat.UINT32,
                DataFormat.UINT64,
                DataFormat.FLOAT,
                DataFormat.DOUBLE,
                DataFormat.BOOL,
                DataFormat.STRING,
            }:
                new_value = src_value
            else:
                raise ValueError(
                    f"internal error: unsupported destination format '{dest_format.value}'"
                )

        elif dest_format == DataFormat.BASE64:
            if src_format == DataFormat.BASE64:
                new_value = src_value
            else:
                raise ValueError(
                    f"internal error: unsupported destination format '{dest_format.value}'"
                )
        else:
            raise ValueError(
                f"internal error: unsupported destination format '{dest_format.value}'"
            )

        return PortFieldData(type=dest_format.get_type(), format=dest_format, value=new_value)


def get_value_and_cast(value: PortFieldData, cast_type: type[_T]) -> _T:
    any_value = value.get_any_value()
    if not isinstance(any_value, cast_type):
        raise TypeError(f"cannot cast value of type '{type(any_value)!r}' to target type")
    return any_value


def convert_any_value(any_value: Any) -> tuple[str, DataFormat]:
    if _is_nil_any_value(any_value):
        raise ValueError("nil value is not supported for conversion")

    data_type = get_data_type(any_value)
    if data_type == DataType.INT64:
        return str(int(any_value)), DataFormat.INT64
    if data_type == DataType.DOUBLE:
        return _format_scientific(float(any_value), 64), DataFormat.DOUBLE
    if data_type == DataType.STRING:
        if isinstance(any_value, datetime):
            return _format_datetime(any_value), DataFormat.DATETIME
        return str(any_value), DataFormat.STRING
    if data_type == DataType.BOOL:
        return ("true" if bool(any_value) else "false"), DataFormat.BOOL
    if data_type == DataType.RAW:
        raw_bytes = bytes(any_value)
        return base64.b64encode(raw_bytes).decode("ascii"), DataFormat.BASE64
    raise ValueError(f"unsupported value type '{type(any_value)!r}' for conversion")


def convert_value_by_format(value: str, src_format: DataFormat) -> Any:
    if src_format == DataFormat.INT16:
        parsed = int(value, 10)
        _ensure_signed_range(parsed, src_format)
        return parsed
    if src_format == DataFormat.INT32:
        parsed = int(value, 10)
        _ensure_signed_range(parsed, src_format)
        return parsed
    if src_format == DataFormat.INT64:
        parsed = int(value, 10)
        _ensure_signed_range(parsed, src_format)
        return parsed
    if src_format == DataFormat.UINT16:
        parsed = int(value, 10)
        _ensure_unsigned_range(parsed, src_format)
        return parsed
    if src_format == DataFormat.UINT32:
        parsed = int(value, 10)
        _ensure_unsigned_range(parsed, src_format)
        return parsed
    if src_format == DataFormat.UINT64:
        parsed = int(value, 10)
        _ensure_unsigned_range(parsed, src_format)
        return parsed
    if src_format == DataFormat.FLOAT:
        return _round_float32(float(value))
    if src_format == DataFormat.DOUBLE:
        return float(value)
    if src_format == DataFormat.STRING:
        return value
    if src_format == DataFormat.SECOND:
        return datetime.fromtimestamp(int(value), tz=_LOCAL_TZ)
    if src_format == DataFormat.MILLISECOND:
        return datetime.fromtimestamp(int(value) / 1000.0, tz=_LOCAL_TZ)
    if src_format == DataFormat.DATETIME:
        return _parse_datetime(value)
    if src_format == DataFormat.BASE64:
        return base64.b64decode(value.encode("ascii"))
    if src_format == DataFormat.BOOL:
        return value == "true"
    raise ValueError(f"unsupported destination format '{src_format.value}' for conversion")


def _is_nil_any_value(any_value: Any) -> bool:
    return any_value is None


def _convert_int_format_to_number_format(
    value: str,
    _src: DataFormat,
    dest: DataFormat,
) -> str:
    parsed = int(value, 10)
    if dest in _INT_LIMITS:
        _ensure_signed_range(parsed, dest)
        return str(parsed)
    if dest in _UINT_MAX:
        _ensure_unsigned_range(parsed, dest)
        return str(parsed)
    if dest in _FLOAT_BITS:
        return _format_scientific(float(parsed), _FLOAT_BITS[dest])
    raise ValueError(
        f"internal error: unsupported destination format '{dest.value}' in int-to-number conversion"
    )


def _convert_float_format_to_number_format(
    value: str,
    src: DataFormat,
    dest: DataFormat,
) -> str:
    if dest == DataFormat.FLOAT:
        parsed = float(value)
        return _format_scientific(_round_float32(parsed), 32)
    if dest == DataFormat.DOUBLE:
        return _format_scientific(float(value), 64)

    parsed = float(value)
    if math.isnan(parsed):
        raise ValueError(
            f"cannot convert '{src.value}' value '{value}' to format '{dest.value}': value is NaN"
        )
    if math.isinf(parsed):
        raise ValueError(
            f"cannot convert '{src.value}' value '{value}' to format '{dest.value}': value is Inf"
        )
    parsed = math.trunc(parsed)

    if dest in _INT_LIMITS:
        _ensure_signed_range(parsed, dest)
        return str(parsed)
    if dest in _UINT_MAX:
        _ensure_unsigned_range(parsed, dest)
        return str(parsed)
    raise ValueError(
        f"internal error: unsupported destination format '{dest.value}' in float-to-number conversion"
    )


def _convert_bool_format_to_number_format(value: str, dest: DataFormat) -> str:
    is_true = value == "true"
    if dest in _INT_LIMITS or dest in _UINT_MAX:
        return "1" if is_true else "0"
    if dest in _FLOAT_BITS:
        return _format_scientific(1.0 if is_true else 0.0, _FLOAT_BITS[dest])
    raise ValueError(
        f"internal error: unsupported destination format '{dest.value}' in bool-to-number conversion"
    )


def _convert_number_format_to_bool_format(value: str, src: DataFormat) -> str:
    if src in {
        DataFormat.INT16,
        DataFormat.INT32,
        DataFormat.INT64,
        DataFormat.UINT16,
        DataFormat.UINT32,
        DataFormat.UINT64,
    }:
        return "false" if value == "0" else "true"
    if src in {DataFormat.FLOAT, DataFormat.DOUBLE}:
        return "false" if float(value) == 0.0 else "true"
    raise ValueError(
        f"internal error: unsupported source format '{src.value}' in number-to-bool conversion"
    )


def _convert_number_format_to_time_format(
    value: str,
    src: DataFormat,
    dest: DataFormat,
) -> str:
    if src in {
        DataFormat.INT16,
        DataFormat.INT32,
        DataFormat.INT64,
        DataFormat.UINT16,
        DataFormat.UINT32,
        DataFormat.UINT64,
    }:
        int64_value = int(_convert_int_format_to_number_format(value, src, DataFormat.INT64), 10)
        dt = _convert_int64_to_datetime(int64_value)
    elif src in {DataFormat.FLOAT, DataFormat.DOUBLE}:
        int64_value = int(_convert_float_format_to_number_format(value, src, DataFormat.INT64), 10)
        dt = _convert_int64_to_datetime(int64_value)
    else:
        raise ValueError(
            f"internal error: unsupported source format '{src.value}' in number-to-time conversion"
        )
    return _stringify_value_for_format(dt, dest)


def _convert_time_format_to_time_format(
    value: str,
    src: DataFormat,
    dest: DataFormat,
) -> str:
    dt = convert_value_by_format(value, src)
    if not isinstance(dt, datetime):
        raise ValueError("internal error: parsed value is not datetime")
    return _stringify_value_for_format(dt, dest)


def _ensure_signed_range(value: int, data_format: DataFormat) -> None:
    minimum, maximum = _INT_LIMITS[data_format]
    if value < minimum or value > maximum:
        raise ValueError("value out of range")


def _ensure_unsigned_range(value: int, data_format: DataFormat) -> None:
    maximum = _UINT_MAX[data_format]
    if value < 0 or value > maximum:
        raise ValueError("value out of range")


def _round_float32(value: float) -> float:
    return struct.unpack("!f", struct.pack("!f", value))[0]


def _format_scientific(value: float, bits: int) -> str:
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "+Inf" if value > 0 else "-Inf"
    shortest = _shortest_float_string(value, bits)
    return _to_scientific_notation(shortest)


def _format_datetime(value: datetime) -> str:
    dt = value.astimezone(value.tzinfo) if value.tzinfo else value.replace(tzinfo=_LOCAL_TZ)
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_datetime(value: str) -> datetime:
    if not _RFC3339_PATTERN.match(value):
        raise ValueError("invalid RFC3339 datetime")
    normalized = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        raise ValueError("invalid RFC3339 datetime")
    return parsed


def _stringify_value_for_format(value: Any, dest_format: DataFormat) -> str:
    if dest_format in _INT_LIMITS or dest_format in _UINT_MAX:
        return str(int(value))
    if dest_format == DataFormat.FLOAT:
        return _format_scientific(float(value), 32)
    if dest_format == DataFormat.DOUBLE:
        return _format_scientific(float(value), 64)
    if dest_format == DataFormat.BOOL:
        return "true" if bool(value) else "false"
    if dest_format == DataFormat.STRING:
        return str(value)
    if dest_format == DataFormat.SECOND:
        if not isinstance(value, datetime):
            raise ValueError("internal error: expected datetime for second format")
        return str(int(value.timestamp()))
    if dest_format == DataFormat.MILLISECOND:
        if not isinstance(value, datetime):
            raise ValueError("internal error: expected datetime for millisecond format")
        return str(int(value.timestamp() * 1000))
    if dest_format == DataFormat.DATETIME:
        if not isinstance(value, datetime):
            raise ValueError("internal error: expected datetime for datetime format")
        return _format_datetime(value)
    if dest_format == DataFormat.BASE64:
        return base64.b64encode(bytes(value)).decode("ascii")
    raise ValueError(f"internal error: unsupported destination format '{dest_format.value}'")


def _convert_int64_to_datetime(int64_value: int) -> datetime:
    if int64_value >= 10**17:
        seconds, nanoseconds = divmod(int64_value, 10**9)
        return datetime.fromtimestamp(seconds, tz=_LOCAL_TZ) + timedelta(
            microseconds=nanoseconds / 1000
        )
    if int64_value >= 10**14:
        return datetime.fromtimestamp(int64_value / 10**6, tz=_LOCAL_TZ)
    if int64_value >= 10**11:
        return datetime.fromtimestamp(int64_value / 1000.0, tz=_LOCAL_TZ)
    return datetime.fromtimestamp(int64_value, tz=_LOCAL_TZ)


def _shortest_float_string(value: float, bits: int) -> str:
    if bits == 32:
        rounded = _round_float32(value)
        max_precision = 9
        for precision in range(1, max_precision + 1):
            candidate = format(rounded, f".{precision}g")
            try:
                candidate_bits = _float32_bits(float(candidate))
            except OverflowError:
                continue
            if candidate_bits == _float32_bits(rounded):
                return candidate
        return format(rounded, f".{max_precision}g")

    max_precision = 17
    for precision in range(1, max_precision + 1):
        candidate = format(value, f".{precision}g")
        if _float64_bits(float(candidate)) == _float64_bits(value):
            return candidate
    return format(value, f".{max_precision}g")


def _to_scientific_notation(value: str) -> str:
    sign = ""
    if value.startswith(("+", "-")):
        sign = "-" if value[0] == "-" else ""
        value = value[1:]

    exponent = 0
    if "e" in value or "E" in value:
        mantissa_part, exponent_part = re.split(r"[eE]", value, maxsplit=1)
        exponent = int(exponent_part)
    else:
        mantissa_part = value

    if "." in mantissa_part:
        integer, fractional = mantissa_part.split(".", 1)
    else:
        integer, fractional = mantissa_part, ""

    digits = integer + fractional
    if not digits or set(digits) == {"0"}:
        return f"{sign}0e+00"

    first_non_zero = next(index for index, char in enumerate(digits) if char != "0")
    digits = digits[first_non_zero:]

    if integer and any(char != "0" for char in integer):
        normalized_exponent = exponent + len(integer.lstrip("0")) - 1
    else:
        leading_fractional_zeros = len(fractional) - len(fractional.lstrip("0"))
        normalized_exponent = exponent - leading_fractional_zeros - 1

    mantissa = digits[0]
    tail = digits[1:].rstrip("0")
    if tail:
        mantissa = f"{mantissa}.{tail}"

    exponent_sign = "+" if normalized_exponent >= 0 else "-"
    exponent_value = str(abs(normalized_exponent)).zfill(2)
    return f"{sign}{mantissa}e{exponent_sign}{exponent_value}"


def _float32_bits(value: float) -> int:
    return struct.unpack("!I", struct.pack("!f", _round_float32(value)))[0]


def _float64_bits(value: float) -> int:
    return struct.unpack("!Q", struct.pack("!d", value))[0]
