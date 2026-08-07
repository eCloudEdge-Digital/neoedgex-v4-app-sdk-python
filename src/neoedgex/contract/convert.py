from __future__ import annotations

import math
from datetime import datetime
from typing import Any

from ._float import (
    FloatRangeError,
    FloatSyntaxError,
    float32_out_of_range,
    parse_go_float,
    restore_float32,
    shortest_float_string,
    to_scientific_notation,
)
from ._limits import (
    INTEGER_TYPES,
    MAX_INT64,
    MAX_UINT64,
    MIN_INT64,
    SIGNED_LIMITS,
    STRICT_INT_RE,
    STRICT_UINT_RE,
    UNSIGNED_MAX,
)
from .types import DataType, get_data_type


def convert_to_typed_value(value: Any, dest_type: DataType) -> Any:
    if not dest_type.is_supported():
        raise ValueError(f"unsupported data type '{dest_type.value}'")
    if value is None:
        raise ValueError("nil value is not supported for conversion")
    if isinstance(value, datetime):
        raise ValueError(
            "datetime is not supported; format it to a string first (e.g. value.isoformat())"
        )

    src_type = get_data_type(value)
    if src_type == DataType.UNDEFINED:
        raise ValueError(f"unsupported value type '{type(value).__name__}' for conversion")
    if src_type == DataType.INT64 and not MIN_INT64 <= value <= MAX_UINT64:
        # Python ints are unbounded, but no declarable type can hold a value
        # outside [-2^63, 2^64-1] and CBOR bignums are never emitted, so such
        # a value has no legal source representation at all.
        raise ValueError(
            f"cannot convert '{src_type.value}' value '{value}' to type "
            f"'{dest_type.value}': value out of range"
        )
    if src_type == DataType.DOUBLE and (math.isnan(value) or math.isinf(value)):
        raise ValueError(
            f"cannot convert '{src_type.value}' value '{value}' to type "
            f"'{dest_type.value}': NaN and Inf are not supported"
        )
    if not src_type.can_convert_to(dest_type):
        raise ValueError(
            f"cannot convert from type '{src_type.value}' to '{dest_type.value}'"
        )

    if dest_type in INTEGER_TYPES:
        return _convert_to_integer(value, src_type, dest_type)
    if dest_type in (DataType.FLOAT, DataType.DOUBLE):
        return _convert_to_float(value, src_type, dest_type)
    if dest_type == DataType.BOOL:
        return value if isinstance(value, bool) else value != 0
    if dest_type == DataType.STRING:
        return _convert_to_string(value)
    return bytes(value) if isinstance(value, bytearray) else value


def _convert_to_integer(value: Any, src_type: DataType, dest_type: DataType) -> int:
    if isinstance(value, str):
        pattern = STRICT_INT_RE if dest_type in SIGNED_LIMITS else STRICT_UINT_RE
        if pattern.fullmatch(value) is None:
            raise ValueError(
                f"cannot convert string value '{value}' to type "
                f"'{dest_type.value}': invalid syntax"
            )
        parsed = int(value, 10)
        if _out_of_integer_range(parsed, dest_type):
            raise ValueError(
                f"cannot convert string value '{value}' to type "
                f"'{dest_type.value}': value out of range"
            )
        return parsed
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, float):
        truncated = math.trunc(value)
        if _float_out_of_integer_range(truncated, dest_type):
            raise ValueError(
                f"cannot convert '{src_type.value}' value '{truncated}' to type "
                f"'{dest_type.value}': value out of range"
            )
        return truncated
    if _out_of_integer_range(value, dest_type):
        raise ValueError(
            f"cannot convert '{src_type.value}' value '{value}' to type "
            f"'{dest_type.value}': value out of range"
        )
    return value


def _out_of_integer_range(value: int, dest_type: DataType) -> bool:
    if dest_type in SIGNED_LIMITS:
        minimum, maximum = SIGNED_LIMITS[dest_type]
        return value < minimum or value > maximum
    return value < 0 or value > UNSIGNED_MAX[dest_type]


def _float_out_of_integer_range(truncated: int, dest_type: DataType) -> bool:
    # The 64-bit upper bounds are exclusive: float64 cannot represent
    # 2^63-1 / 2^64-1, the nearest candidates round up to 2^63 / 2^64,
    # which do not fit (mirrors Go outOfIntegerRange).
    if dest_type == DataType.INT64:
        return truncated < MIN_INT64 or truncated > MAX_INT64
    if dest_type == DataType.UINT64:
        return truncated < 0 or truncated > MAX_UINT64
    return _out_of_integer_range(truncated, dest_type)


def _convert_to_float(value: Any, src_type: DataType, dest_type: DataType) -> float:
    if isinstance(value, str):
        try:
            parsed = parse_go_float(value, 32 if dest_type == DataType.FLOAT else 64)
        except FloatSyntaxError:
            raise ValueError(
                f"cannot convert string value '{value}' to type "
                f"'{dest_type.value}': invalid syntax"
            ) from None
        except FloatRangeError:
            raise ValueError(
                f"cannot convert string value '{value}' to type "
                f"'{dest_type.value}': value out of range"
            ) from None
        if math.isnan(parsed) or math.isinf(parsed):
            raise ValueError(
                f"cannot convert string value '{value}' to type "
                f"'{dest_type.value}': NaN and Inf are not supported"
            )
        return parsed
    if isinstance(value, bool):
        converted = 1.0 if value else 0.0
    elif isinstance(value, float):
        converted = value
    else:
        converted = float(value)

    if dest_type == DataType.FLOAT:
        if float32_out_of_range(converted):
            raise ValueError(
                f"cannot convert '{src_type.value}' value '{value}' to type "
                f"'{dest_type.value}': value out of range"
            )
        return restore_float32(converted)
    return converted


def _convert_to_string(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return to_scientific_notation(shortest_float_string(value, 64))
    return str(value)
