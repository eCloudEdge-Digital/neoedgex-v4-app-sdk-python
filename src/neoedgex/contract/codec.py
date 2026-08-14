from __future__ import annotations

import math
import struct
from typing import Any

import cbor2

from ._float import restore_float32
from ._limits import INTEGER_TYPES, MAX_INT64, MAX_UINT64, MIN_INT64
from .convert import convert_to_typed_value
from .types import DataType

_MAJOR_UNSIGNED = 0
_MAJOR_NEGATIVE = 1
_MAJOR_BYTES = 2
_MAJOR_TEXT = 3
_MAJOR_ARRAY = 4
_MAJOR_MAP = 5
_MAJOR_TAG = 6
_MAJOR_SIMPLE = 7

_CBOR_NULL = b"\xf6"
_CBOR_FALSE = b"\xf4"
_CBOR_TRUE = b"\xf5"
_SINGLE_FLOAT_HEAD = 0xFA
_BREAK = 0xFF


def encode_field(value: Any, data_type: DataType) -> bytes:
    if value is None:
        return _CBOR_NULL
    if data_type == DataType.BOOL:
        return _CBOR_TRUE if value else _CBOR_FALSE
    if data_type in INTEGER_TYPES:
        return _encode_int(value)
    if data_type == DataType.FLOAT:
        return b"\xfa" + struct.pack(">f", value)
    if data_type == DataType.DOUBLE:
        return b"\xfb" + struct.pack(">d", value)
    if data_type == DataType.STRING:
        return _encode_text(value)
    if data_type == DataType.RAW:
        return _encode_head(_MAJOR_BYTES, len(value)) + bytes(value)
    raise ValueError(f"unsupported data type '{data_type.value}'")


def encode_data_map(entries: list[tuple[str, DataType, Any]]) -> bytes:
    out = bytearray(_encode_head(_MAJOR_MAP, len(entries)))
    for key, data_type, value in entries:
        out += _encode_text(key)
        out += encode_field(value, data_type)
    return bytes(out)


def encode_neoflow_message(source: str, timestamp: str, data: bytes) -> bytes:
    return (
        b"\xa3"
        + _encode_text("source")
        + _encode_text(source)
        + _encode_text("timestamp")
        + _encode_text(timestamp)
        + _encode_text("data")
        + data
    )


def _encode_head(major: int, argument: int) -> bytes:
    if argument < 24:
        return bytes([major << 5 | argument])
    if argument <= 0xFF:
        return bytes([major << 5 | 24, argument])
    if argument <= 0xFFFF:
        return struct.pack(">BH", major << 5 | 25, argument)
    if argument <= 0xFFFFFFFF:
        return struct.pack(">BI", major << 5 | 26, argument)
    return struct.pack(">BQ", major << 5 | 27, argument)


def _encode_int(value: int) -> bytes:
    if value >= 0:
        return _encode_head(_MAJOR_UNSIGNED, value)
    return _encode_head(_MAJOR_NEGATIVE, -1 - value)


def _encode_text(value: str) -> bytes:
    encoded = value.encode("utf-8")
    return _encode_head(_MAJOR_TEXT, len(encoded)) + encoded


def decode_neoflow_envelope(payload: bytes) -> tuple[str, str, bytes]:
    spans = scan_data_map(payload)
    return (
        _text_value(spans.get("source"), "source"),
        _text_value(spans.get("timestamp"), "timestamp"),
        spans.get("data", b""),
    )


def _text_value(span: bytes | None, key: str) -> str:
    if span is None:
        return ""
    value = cbor2.loads(span)
    # CBOR null yields "" like Go's codec (null into a string field is the
    # zero value); any other non-text value is an unmarshal error there, so
    # it raises here and the whole message is dropped.
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"envelope key '{key}' is not a text string")
    return value


def scan_data_map(data: bytes) -> dict[str, bytes]:
    head, argument, offset = _read_head(data, 0)
    if head >> 5 != _MAJOR_MAP:
        raise ValueError("CBOR map expected")

    spans: dict[str, bytes] = {}

    def read_pair(offset: int) -> int:
        key_start = offset
        key_end = _skip_item(data, offset)
        if data[key_start] >> 5 != _MAJOR_TEXT:
            raise ValueError("CBOR map key is not a text string")
        key = cbor2.loads(data[key_start:key_end])
        value_end = _skip_item(data, key_end)
        spans[key] = bytes(data[key_end:value_end])
        return value_end

    if argument is None:
        while True:
            if offset >= len(data):
                raise ValueError("unexpected end of CBOR data")
            if data[offset] == _BREAK:
                offset += 1
                break
            offset = read_pair(offset)
    else:
        for _ in range(argument):
            offset = read_pair(offset)

    if offset != len(data):
        raise ValueError("extraneous data after CBOR map")
    return spans


def is_undefined(span: bytes) -> bool:
    return len(span) == 1 and span[0] in (0xF6, 0xF7)


def decode_natural(span: bytes) -> Any:
    value = cbor2.loads(span)
    if value is None or isinstance(value, (bool, str, bytes)):
        return value
    if isinstance(value, int):
        if MIN_INT64 <= value <= MAX_UINT64:
            return value
        raise ValueError("unsupported value type")
    if isinstance(value, float):
        return value
    raise ValueError(f"unsupported value type '{type(value).__name__}'")


_FLOAT_HEAD_LAYOUT = {0xF9: (3, ">e"), 0xFA: (5, ">f"), 0xFB: (9, ">d")}


def decode_field_with_schema(span: bytes, tag_type: DataType) -> Any:
    # A single-precision wire value must be caught on the head byte, before
    # any cbor2 decode: cbor2 widens 0xfa payloads to double, and once the
    # width is lost the shortest-decimal restore (float32(25.34) -> 25.34,
    # not 25.34000015258789) is impossible.
    #
    # NaN/Inf routing mirrors Go v2.1.0, which is asymmetric: the two
    # width-mismatch combinations it special-cases into the conversion matrix
    # (0xfa into double, 0xfb into float) reject NaN/Inf, while every other
    # float-head-into-float-tag combination delivers them as-is.
    if tag_type in (DataType.FLOAT, DataType.DOUBLE) and span:
        layout = _FLOAT_HEAD_LAYOUT.get(span[0])
        if layout is not None and len(span) == layout[0]:
            value = struct.unpack(layout[1], span[1:])[0]
            if math.isnan(value) or math.isinf(value):
                if (span[0] == 0xFA and tag_type == DataType.DOUBLE) or (
                    span[0] == 0xFB and tag_type == DataType.FLOAT
                ):
                    source = "float" if span[0] == 0xFA else "double"
                    raise ValueError(
                        f"cannot convert '{source}' value '{value}' to type "
                        f"'{tag_type.value}': NaN and Inf are not supported"
                    )
                return value
            if span[0] == _SINGLE_FLOAT_HEAD:
                return restore_float32(value)

    # A single-precision value reaching a STRING tag needs the same head-byte
    # read. The widening is exact, so nothing is lost numerically, but the
    # shortest decimal that round-trips a double needs every digit of the
    # widened value ("25.34000015258789") while the shortest decimal that
    # round-trips the float32 it came from is "25.34" -- both recover the wire
    # bits, and only the second is the value the sender meant. NaN/Inf falls
    # through to the generic path, which refuses them exactly as before.
    if tag_type == DataType.STRING and span and span[0] == _SINGLE_FLOAT_HEAD:
        layout = _FLOAT_HEAD_LAYOUT[_SINGLE_FLOAT_HEAD]
        if len(span) == layout[0]:
            value = struct.unpack(layout[1], span[1:])[0]
            if not (math.isnan(value) or math.isinf(value)):
                return convert_to_typed_value(restore_float32(value), tag_type)

    return convert_to_typed_value(decode_natural(span), tag_type)


def wire_matches_declared(head: int, base: type) -> bool:
    """Reports whether a wire value with this head byte decodes directly as
    the annotated type, so the caller can skip the schema route entirely
    (declaration wins, = Go wireMatchesField). Integer majors count as float
    matches: Go's declaration-wins fallback is a plain codec decode, and the
    codec converts integer wire values into float fields under every schema."""
    if base is bool:
        return head in (0xF4, 0xF5)
    if base is int:
        return head >> 5 in (_MAJOR_UNSIGNED, _MAJOR_NEGATIVE)
    if base is float:
        return head >> 5 in (_MAJOR_UNSIGNED, _MAJOR_NEGATIVE) or head in _FLOAT_HEAD_LAYOUT
    if base is str:
        return head >> 5 == _MAJOR_TEXT
    if base is bytes:
        return head >> 5 == _MAJOR_BYTES
    return False


def decode_declared(span: bytes, base: type) -> Any:
    """Decodes a raw span as the annotated type, ignoring the schema. Python
    annotations carry no width, so `int` spans the int64 domain (out of range
    raises, = Go's int64 codec range check) and `float` follows the wire head:
    0xfa is restored to its shortest-decimal float32 value, 0xf9 widens
    naturally, 0xfb is taken as-is."""
    if base is bool:
        return span[0] == 0xF5
    if base is float:
        layout = _FLOAT_HEAD_LAYOUT.get(span[0])
        if layout is not None:
            value = struct.unpack(layout[1], span[1:])[0]
            if span[0] == _SINGLE_FLOAT_HEAD and not (math.isnan(value) or math.isinf(value)):
                return restore_float32(value)
            return value
        return float(cbor2.loads(span))
    value = cbor2.loads(span)
    if base is int and not MIN_INT64 <= value <= MAX_INT64:
        raise ValueError(f"integer {value} out of int64 range")
    return value


def _read_head(buf: bytes, offset: int) -> tuple[int, int | None, int]:
    if offset >= len(buf):
        raise ValueError("unexpected end of CBOR data")
    head = buf[offset]
    offset += 1
    info = head & 0x1F
    if info < 24:
        return head, info, offset
    if info == 24:
        size = 1
    elif info == 25:
        size = 2
    elif info == 26:
        size = 4
    elif info == 27:
        size = 8
    elif info == 31:
        return head, None, offset
    else:
        raise ValueError(f"invalid CBOR additional information {info}")
    if offset + size > len(buf):
        raise ValueError("unexpected end of CBOR data")
    argument = int.from_bytes(buf[offset : offset + size], "big")
    return head, argument, offset + size


def _skip_item(buf: bytes, offset: int) -> int:
    head, argument, offset = _read_head(buf, offset)
    major = head >> 5

    if major in (_MAJOR_UNSIGNED, _MAJOR_NEGATIVE):
        if argument is None:
            raise ValueError("invalid CBOR integer head")
        return offset

    if major in (_MAJOR_BYTES, _MAJOR_TEXT):
        if argument is None:
            return _skip_until_break(buf, offset)
        end = offset + argument
        if end > len(buf):
            raise ValueError("unexpected end of CBOR data")
        return end

    if major == _MAJOR_ARRAY:
        if argument is None:
            return _skip_until_break(buf, offset)
        for _ in range(argument):
            offset = _skip_item(buf, offset)
        return offset

    if major == _MAJOR_MAP:
        if argument is None:
            return _skip_until_break(buf, offset)
        for _ in range(2 * argument):
            offset = _skip_item(buf, offset)
        return offset

    if major == _MAJOR_TAG:
        if argument is None:
            raise ValueError("invalid CBOR tag head")
        return _skip_item(buf, offset)

    # Major 7: the argument bytes (simple value, half/single/double float)
    # were already consumed by _read_head; a bare break byte is only legal
    # inside an indefinite-length container.
    if argument is None:
        raise ValueError("unexpected CBOR break code")
    return offset


def _skip_until_break(buf: bytes, offset: int) -> int:
    while True:
        if offset >= len(buf):
            raise ValueError("unexpected end of CBOR data")
        if buf[offset] == _BREAK:
            return offset + 1
        offset = _skip_item(buf, offset)
