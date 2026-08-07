from __future__ import annotations

import math
import re
import struct


class FloatSyntaxError(ValueError):
    pass


class FloatRangeError(ValueError):
    pass


# Go strconv.ParseFloat syntax: leading sign only on numbers and inf (a signed
# nan is rejected), digit-separating underscores, and hex-float forms with a
# mandatory p exponent.
_GO_INF_RE = re.compile(r"[+-]?(?i:inf(?:inity)?)")
_GO_NAN_RE = re.compile(r"(?i:nan)")
_DEC_DIGITS = r"[0-9](?:_?[0-9])*"
_HEX_DIGITS = r"[0-9a-fA-F](?:_?[0-9a-fA-F])*"
_GO_FLOAT_RE = re.compile(
    rf"[+-]?(?:"
    rf"(?:{_DEC_DIGITS}(?:\.(?:{_DEC_DIGITS})?)?|\.{_DEC_DIGITS})"
    rf"(?:[eE][+-]?{_DEC_DIGITS})?"
    rf"|0[xX](?:_?{_HEX_DIGITS}(?:\.(?:{_HEX_DIGITS})?)?|\.{_HEX_DIGITS})"
    rf"[pP][+-]?{_DEC_DIGITS}"
    rf")"
)


def parse_go_float(value: str, bits: int) -> float:
    if _GO_INF_RE.fullmatch(value):
        return float("-inf") if value.startswith("-") else float("inf")
    if _GO_NAN_RE.fullmatch(value):
        return float("nan")
    if _GO_FLOAT_RE.fullmatch(value) is None:
        raise FloatSyntaxError("invalid syntax")
    text = value.replace("_", "")
    if text.lstrip("+-")[:2].lower() == "0x":
        try:
            parsed = float.fromhex(text)
        except OverflowError:
            raise FloatRangeError("value out of range") from None
    else:
        parsed = float(text)
    if math.isinf(parsed):
        raise FloatRangeError("value out of range")
    if bits == 32:
        if float32_out_of_range(parsed):
            raise FloatRangeError("value out of range")
        return restore_float32(parsed)
    return parsed


def float32_out_of_range(value: float) -> bool:
    # Overflow is judged after round-to-nearest narrowing, not by comparing
    # against the exact double value of MaxFloat32: the restored float32
    # maximum (3.4028235e+38) exceeds that constant yet narrows back to a
    # finite float32, and the SDK must re-accept its own decoded output.
    if math.isnan(value) or math.isinf(value):
        return False
    try:
        return math.isinf(round_float32(value))
    except OverflowError:
        return True


def round_float32(value: float) -> float:
    return struct.unpack("!f", struct.pack("!f", value))[0]


def float32_bits(value: float) -> int:
    return struct.unpack("!I", struct.pack("!f", round_float32(value)))[0]


def float64_bits(value: float) -> int:
    return struct.unpack("!Q", struct.pack("!d", value))[0]


def restore_float32(value: float) -> float:
    # Rounding to float32 and back yields the widened double
    # (25.34000015258789); the shortest-decimal round trip restores the value
    # the float32 was meant to carry (25.34).
    return float(shortest_float_string(value, 32))


def shortest_float_string(value: float, bits: int) -> str:
    if bits == 32:
        rounded = round_float32(value)
        max_precision = 9
        for precision in range(1, max_precision + 1):
            candidate = format(rounded, f".{precision}g")
            try:
                candidate_bits = float32_bits(float(candidate))
            except OverflowError:
                continue
            if candidate_bits == float32_bits(rounded):
                return candidate
        return format(rounded, f".{max_precision}g")

    max_precision = 17
    for precision in range(1, max_precision + 1):
        candidate = format(value, f".{precision}g")
        if float64_bits(float(candidate)) == float64_bits(value):
            return candidate
    return format(value, f".{max_precision}g")


def to_scientific_notation(value: str) -> str:
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
