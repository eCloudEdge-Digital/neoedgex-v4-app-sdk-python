from __future__ import annotations

from datetime import datetime
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


class DataFormat(str, Enum):
    UNDEFINED = ""
    BOOL = "bool"
    INT16 = "int16"
    INT32 = "int32"
    INT64 = "int64"
    SECOND = "second"
    MILLISECOND = "millisecond"
    UINT16 = "uint16"
    UINT32 = "uint32"
    UINT64 = "uint64"
    FLOAT = "float"
    DOUBLE = "double"
    STRING = "string"
    DATETIME = "datetime"
    BASE64 = "base64"
    JSON = "json"

    def get_type(self) -> DataType:
        return TYPE_FORMAT_MAP.get(self, DataType.UNDEFINED)

    def can_convert_to(self, dest_format: "DataFormat") -> bool:
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
            return self in {
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
            }
        if dest_format in {DataFormat.SECOND, DataFormat.MILLISECOND, DataFormat.DATETIME}:
            return self in {
                DataFormat.INT16,
                DataFormat.INT32,
                DataFormat.INT64,
                DataFormat.UINT16,
                DataFormat.UINT32,
                DataFormat.UINT64,
                DataFormat.FLOAT,
                DataFormat.DOUBLE,
                DataFormat.SECOND,
                DataFormat.MILLISECOND,
                DataFormat.DATETIME,
            }
        if dest_format == DataFormat.BOOL:
            return self in {
                DataFormat.INT16,
                DataFormat.INT32,
                DataFormat.INT64,
                DataFormat.UINT16,
                DataFormat.UINT32,
                DataFormat.UINT64,
                DataFormat.FLOAT,
                DataFormat.DOUBLE,
                DataFormat.BOOL,
            }
        if dest_format == DataFormat.STRING:
            return self in {
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
            }
        if dest_format == DataFormat.BASE64:
            return self == DataFormat.BASE64
        if dest_format == DataFormat.JSON:
            return self == DataFormat.JSON
        return False


TYPE_FORMAT_MAP = {
    DataFormat.BOOL: DataType.BOOL,
    DataFormat.INT16: DataType.INT16,
    DataFormat.INT32: DataType.INT32,
    DataFormat.INT64: DataType.INT64,
    DataFormat.SECOND: DataType.INT64,
    DataFormat.MILLISECOND: DataType.INT64,
    DataFormat.UINT16: DataType.UINT16,
    DataFormat.UINT32: DataType.UINT32,
    DataFormat.UINT64: DataType.UINT64,
    DataFormat.FLOAT: DataType.FLOAT,
    DataFormat.DOUBLE: DataType.DOUBLE,
    DataFormat.STRING: DataType.STRING,
    DataFormat.DATETIME: DataType.STRING,
    DataFormat.BASE64: DataType.RAW,
    DataFormat.JSON: DataType.STRING,
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


def coerce_data_format(raw: Any) -> DataFormat:
    try:
        return DataFormat(raw)
    except ValueError:
        return DataFormat.UNDEFINED


def get_data_type(any_value: Any) -> DataType:
    if isinstance(any_value, bool):
        return DataType.BOOL
    if isinstance(any_value, int):
        return DataType.INT64
    if isinstance(any_value, float):
        return DataType.DOUBLE
    if isinstance(any_value, (str, datetime)):
        return DataType.STRING
    if isinstance(any_value, (bytes, bytearray)):
        return DataType.RAW
    return DataType.UNDEFINED
