from __future__ import annotations

import base64
import math
from dataclasses import dataclass
from typing import Any, TypeVar

from ._float import (
    FloatRangeError,
    FloatSyntaxError,
    parse_go_float,
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
from .convert import convert_to_typed_value
from .types import DataType, coerce_data_type, get_data_type

_FLOAT_BITS = {
    DataType.FLOAT: 32,
    DataType.DOUBLE: 64,
}

_T = TypeVar("_T")


@dataclass(slots=True)
class PortFieldData:
    type: DataType = DataType.UNDEFINED
    value: str = ""

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "PortFieldData":
        # A legacy "format" key is tolerated and ignored.
        return cls(
            type=coerce_data_type(payload.get("type", "")),
            value=str(payload.get("value", "")),
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "type": self.type.value,
            "value": self.value,
        }

    @classmethod
    def new_with_string(cls, value: str, data_type: DataType) -> "PortFieldData":
        if not data_type.is_supported():
            raise ValueError(f"unsupported data type '{data_type.value}'")
        try:
            convert_value_by_type(value, data_type)
        except Exception as exc:
            raise ValueError(
                f"value '{value}' is not compatible with type '{data_type.value}': {exc}"
            ) from exc
        return cls(type=data_type, value=value)

    @classmethod
    def new_with_any(cls, any_value: Any, dest_type: DataType) -> "PortFieldData":
        typed = convert_to_typed_value(any_value, dest_type)
        value, _ = convert_any_value(typed)
        return cls(type=dest_type, value=value)

    @classmethod
    def empty(cls) -> "PortFieldData":
        return cls()

    def get_any_value(self) -> Any:
        return convert_value_by_type(self.value, self.type)

    def convert_to(self, dest_type: DataType) -> "PortFieldData":
        # Same-type short-circuits BEFORE the matrix check, so an unparseable
        # value is copied through rather than reported (mirrors Go ConvertTo).
        if self.type == dest_type:
            return PortFieldData(type=self.type, value=self.value)
        if not self.type.can_convert_to(dest_type):
            raise ValueError(
                f"cannot convert from type '{self.type.value}' to '{dest_type.value}'"
            )

        src_value = self.value
        src_type = self.type

        if dest_type.is_number():
            if src_type == DataType.STRING:
                # The string itself feeds convert_to_typed_value's string
                # path, which is stricter than convert_value_by_type: NaN/Inf
                # strings are rejected (mirrors Go ConvertTo, which parses a
                # string source through ConvertToTypedValue).
                native: Any = src_value
            else:
                native = convert_value_by_type(src_value, src_type)
            new_value = _stringify_number_for_type(
                convert_to_typed_value(native, dest_type), dest_type
            )

        elif dest_type == DataType.BOOL:
            if src_type.is_number():
                native = convert_value_by_type(src_value, src_type)
                new_value = (
                    "true" if convert_to_typed_value(native, DataType.BOOL) else "false"
                )
            else:
                raise ValueError(
                    f"internal error: unsupported destination type '{dest_type.value}'"
                )

        elif dest_type == DataType.STRING:
            if src_type.is_number() or src_type == DataType.BOOL:
                new_value = convert_to_typed_value(self.get_any_value(), DataType.STRING)
            else:
                raise ValueError(
                    f"internal error: unsupported destination type '{dest_type.value}'"
                )

        else:
            # raw never cross-converts; can_convert_to already rejected
            # everything except the same-type early return above.
            raise ValueError(
                f"internal error: unsupported destination type '{dest_type.value}'"
            )

        return PortFieldData(type=dest_type, value=new_value)


def get_value_and_cast(value: PortFieldData, cast_type: type[_T]) -> _T:
    any_value = value.get_any_value()
    # bool is an int subclass, but the wire types are unrelated: casting a
    # bool value to int must fail, like a Go type assertion would. `object`
    # is the counterpart of Go's `any` and accepts every value.
    if (isinstance(any_value, bool) and cast_type not in (bool, object)) or not isinstance(
        any_value, cast_type
    ):
        raise TypeError(f"cannot cast value of type '{type(any_value)!r}' to target type")
    return any_value


def convert_any_value(any_value: Any) -> tuple[str, DataType]:
    if any_value is None:
        raise ValueError("nil value is not supported for conversion")

    data_type = get_data_type(any_value)
    if data_type == DataType.BOOL:
        return ("true" if any_value else "false"), DataType.BOOL
    if data_type == DataType.INT64:
        parsed = int(any_value)
        if parsed < MIN_INT64 or parsed > MAX_UINT64:
            # No declarable type can read the pair back; Go cannot even
            # represent such a value.
            raise ValueError(
                f"cannot convert 'int64' value '{parsed}': value out of range"
            )
        if parsed > MAX_INT64:
            # The uint64 domain: Go reports these values as TypeUint64.
            return str(parsed), DataType.UINT64
        return str(parsed), DataType.INT64
    if data_type == DataType.DOUBLE:
        return _format_scientific(float(any_value), 64), DataType.DOUBLE
    if data_type == DataType.STRING:
        return any_value, DataType.STRING
    if data_type == DataType.RAW:
        return base64.b64encode(bytes(any_value)).decode("ascii"), DataType.RAW
    raise ValueError(f"unsupported value type '{type(any_value).__name__}' for conversion")


def convert_value_by_type(value: str, src_type: DataType) -> Any:
    if src_type in INTEGER_TYPES:
        pattern = STRICT_INT_RE if src_type in SIGNED_LIMITS else STRICT_UINT_RE
        if pattern.fullmatch(value) is None:
            raise ValueError(f"cannot parse '{value}' as '{src_type.value}': invalid syntax")
        parsed = int(value, 10)
        if src_type in SIGNED_LIMITS:
            _ensure_signed_range(parsed, src_type)
        else:
            _ensure_unsigned_range(parsed, src_type)
        return parsed
    if src_type in _FLOAT_BITS:
        # NaN/Inf strings parse through, as Go ParseFloat's do: this is the
        # mock-config channel, not the wire, and both SDKs' mocks share it.
        try:
            return parse_go_float(value, _FLOAT_BITS[src_type])
        except FloatSyntaxError:
            raise ValueError(
                f"cannot parse '{value}' as '{src_type.value}': invalid syntax"
            ) from None
        except FloatRangeError:
            raise ValueError(
                f"cannot parse '{value}' as '{src_type.value}': value out of range"
            ) from None
    if src_type == DataType.STRING:
        return value
    if src_type == DataType.RAW:
        # validate=True matches Go's strict StdEncoding: non-alphabet
        # characters and data after padding are rejected instead of skipped.
        return base64.b64decode(value, validate=True)
    if src_type == DataType.BOOL:
        # Never fails: "TRUE", "1" and "garbage" all read back as False
        # (mirrors Go ConvertValueByType).
        return value == "true"
    raise ValueError(f"unsupported destination type '{src_type.value}' for conversion")


def _ensure_signed_range(value: int, data_type: DataType) -> None:
    minimum, maximum = SIGNED_LIMITS[data_type]
    if value < minimum or value > maximum:
        raise ValueError("value out of range")


def _ensure_unsigned_range(value: int, data_type: DataType) -> None:
    maximum = UNSIGNED_MAX[data_type]
    if value < 0 or value > maximum:
        raise ValueError("value out of range")


def _stringify_number_for_type(value: Any, dest_type: DataType) -> str:
    if dest_type in INTEGER_TYPES:
        return str(int(value))
    if dest_type == DataType.FLOAT:
        return _format_scientific(float(value), 32)
    if dest_type == DataType.DOUBLE:
        return _format_scientific(float(value), 64)
    raise ValueError(f"internal error: unsupported destination type '{dest_type.value}'")


def _format_scientific(value: float, bits: int) -> str:
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "+Inf" if value > 0 else "-Inf"
    return to_scientific_notation(shortest_float_string(value, bits))
