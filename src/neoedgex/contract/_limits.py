from __future__ import annotations

import re

from .types import DataType

SIGNED_LIMITS = {
    DataType.INT16: (-(2**15), 2**15 - 1),
    DataType.INT32: (-(2**31), 2**31 - 1),
    DataType.INT64: (-(2**63), 2**63 - 1),
}

UNSIGNED_MAX = {
    DataType.UINT16: 2**16 - 1,
    DataType.UINT32: 2**32 - 1,
    DataType.UINT64: 2**64 - 1,
}

INTEGER_TYPES = frozenset(SIGNED_LIMITS) | frozenset(UNSIGNED_MAX)

MIN_INT64, MAX_INT64 = SIGNED_LIMITS[DataType.INT64]
MAX_UINT64 = UNSIGNED_MAX[DataType.UINT64]

# int() accepts underscores and surrounding whitespace; the contract parses
# like Go strconv (ParseUint permits no sign), so only these shapes may
# reach it.
STRICT_INT_RE = re.compile(r"[+-]?[0-9]+")
STRICT_UINT_RE = re.compile(r"[0-9]+")
