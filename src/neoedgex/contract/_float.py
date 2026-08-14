from __future__ import annotations

import math
import re
import struct
from decimal import Decimal
from fractions import Fraction


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
        return _narrow_text_to_float32(text, parsed)
    return parsed


# The float32 finite ceiling and the overflow boundary ParseFloat(s, 32)
# rounds against: a magnitude below the midpoint between MaxFloat32 and 2^128
# rounds down to MaxFloat32, at or above it overflows. Both constants are
# exact float64 values — the magnitudes of two adjacent float32 lattice
# points sum in 25 significant bits.
_MAX_FLOAT32 = struct.unpack("!f", struct.pack("!I", 0x7F7FFFFF))[0]
_FLOAT32_OVERFLOW_MIDPOINT = (_MAX_FLOAT32 + 2.0**128) / 2.0


def _narrow_text_to_float32(text: str, parsed: float) -> float:
    # Go's ParseFloat(s, 32) rounds the text ONCE, directly to float32.
    # ``parsed`` has already been rounded to float64, and narrowing that
    # rounds a second time — double rounding, which picks the wrong
    # float32 exactly when the float64 landed on a float32 rounding
    # boundary and the tie-break disagrees with the side the text is
    # really on ("7.038531e-26" must give bits 0x15ae43fd, not ..fe).
    # Every float32 boundary is exactly representable in float64, so the
    # collisions are exactly detectable, and only there is the text's
    # exact value consulted.
    magnitude, sign = abs(parsed), math.copysign(1.0, parsed)

    if float32_out_of_range(parsed):
        # The one recoverable overflow: the float64 sits exactly on the
        # overflow boundary but the text is below it — Go rounds down to
        # MaxFloat32.
        if magnitude == _FLOAT32_OVERFLOW_MIDPOINT and _exact_magnitude(
            text
        ) < Fraction(_FLOAT32_OVERFLOW_MIDPOINT):
            return math.copysign(restore_float32(_MAX_FLOAT32), sign)
        raise FloatRangeError("value out of range")

    rounded = round_float32(magnitude)
    if rounded != magnitude:
        neighbor = _adjacent_float32_magnitude(rounded, magnitude)
        if magnitude == (rounded + neighbor) / 2:
            exact, boundary = _exact_magnitude(text), Fraction(magnitude)
            if exact > boundary:
                rounded = max(rounded, neighbor)
            elif exact < boundary:
                rounded = min(rounded, neighbor)
            # exact == boundary is a true tie: round-half-to-even already
            # picked ``rounded``.
    return math.copysign(restore_float32(rounded), sign)


def _adjacent_float32_magnitude(rounded: float, magnitude: float) -> float:
    # The float32 lattice point on the other side of ``magnitude``, as its
    # widened float64. Both arguments are non-negative and rounded is finite,
    # so stepping the bit pattern by one walks the lattice, zero included
    # (bits(0.0) + 1 is the smallest subnormal).
    bits = struct.unpack("!I", struct.pack("!f", rounded))[0]
    bits += 1 if magnitude > rounded else -1
    return struct.unpack("!f", struct.pack("!I", bits))[0]


def _exact_magnitude(text: str) -> Fraction:
    # The exact value spelled by an (underscore-free) unsigned-or-signed
    # numeric text, decimal or Go hex-float form.
    body = text.lstrip("+-")
    if body[:2].lower() == "0x":
        mantissa, exponent = re.split("[pP]", body[2:], maxsplit=1)
        integer, _, fraction = mantissa.partition(".")
        whole = int(integer, 16) if integer else 0
        part = Fraction(int(fraction, 16), 16 ** len(fraction)) if fraction else Fraction(0)
        return (whole + part) * Fraction(2) ** int(exponent)
    return Fraction(Decimal(body))


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


def to_fixed_notation(value: str) -> str:
    """Expand a decimal string (exponent form included) to fixed-point
    notation, digits preserved — Go's 'f' verb applied to a shortest-form
    mantissa. The notation never switches to an exponent, so "1e+21" spells
    out all 22 digits, matching the platform's formula engine and forwarder
    payload rendering."""
    sign = ""
    if value.startswith(("+", "-")):
        sign = "-" if value[0] == "-" else ""
        value = value[1:]

    exponent = 0
    if "e" in value or "E" in value:
        mantissa, exponent_part = re.split(r"[eE]", value, maxsplit=1)
        exponent = int(exponent_part)
    else:
        mantissa = value

    integer, _, fraction = mantissa.partition(".")
    digits = integer + fraction
    if not digits or set(digits) == {"0"}:
        return f"{sign}0"

    point = len(integer) + exponent
    stripped = digits.lstrip("0")
    point -= len(digits) - len(stripped)
    digits = stripped

    if point <= 0:
        return sign + "0." + "0" * (-point) + digits
    if point >= len(digits):
        return sign + digits + "0" * (point - len(digits))
    return f"{sign}{digits[:point]}.{digits[point:]}"
