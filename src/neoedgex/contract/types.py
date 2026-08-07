from __future__ import annotations

from enum import Enum
from typing import Any


class DataType(str, Enum):
    UNDEFINED = ""
    BOOL = "bool"
    INT16 = "int16"
    INT32 = "int32"
    INT64 = "int64"
    UINT16 = "uint16"
    UINT32 = "uint32"
    UINT64 = "uint64"
    FLOAT = "float"
    DOUBLE = "double"
    STRING = "string"
    RAW = "raw"

    def is_number(self) -> bool:
        return self in {
            DataType.INT16,
            DataType.INT32,
            DataType.INT64,
            DataType.UINT16,
            DataType.UINT32,
            DataType.UINT64,
            DataType.FLOAT,
            DataType.DOUBLE,
        }

    def is_supported(self) -> bool:
        return self in SUPPORTED_TYPES

    def can_convert_to(self, dest: "DataType") -> bool:
        if dest.is_number():
            return self.is_number() or self in {DataType.BOOL, DataType.STRING}
        if dest == DataType.BOOL:
            # string is deliberately excluded: "true" is rejected, not parsed.
            return self.is_number() or self == DataType.BOOL
        if dest == DataType.STRING:
            return self.is_number() or self in {DataType.BOOL, DataType.STRING}
        if dest == DataType.RAW:
            return self == DataType.RAW
        return False


SUPPORTED_TYPES = {
    DataType.BOOL,
    DataType.INT16,
    DataType.INT32,
    DataType.INT64,
    DataType.UINT16,
    DataType.UINT32,
    DataType.UINT64,
    DataType.FLOAT,
    DataType.DOUBLE,
    DataType.STRING,
    DataType.RAW,
}


class ErrorCode(str, Enum):
    INITIALIZATION_ERROR = "INITIALIZATION_ERROR"
    NETWORK_ERROR = "NETWORK_ERROR"
    PROCESS_ERROR = "PROCESS_ERROR"


def coerce_data_type(raw: Any) -> DataType:
    try:
        return DataType(raw)
    except ValueError:
        return DataType.UNDEFINED


def get_data_type(any_value: Any) -> DataType:
    if isinstance(any_value, bool):
        return DataType.BOOL
    if isinstance(any_value, int):
        return DataType.INT64
    if isinstance(any_value, float):
        return DataType.DOUBLE
    if isinstance(any_value, str):
        return DataType.STRING
    if isinstance(any_value, (bytes, bytearray)):
        return DataType.RAW
    return DataType.UNDEFINED
