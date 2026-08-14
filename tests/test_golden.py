"""Cross-language equivalence guard.

Every row of ``testdata/golden/neoflow_v2_1_0.json`` — produced by really
running the Go SDK v2.1.0 (bdfcd09) — is replayed through the Python wire
path. This is the main protection against the two SDKs drifting apart:

* ``decode_cases`` are wired through ``Message.to_dict()``, i.e. the same
  entry point an application uses, with the fixture's ``wire_hex`` placed in a
  one-entry CBOR data map under key ``k`` and ``declared`` feeding the decode
  plan. ``expect`` is the Go delivery; where a row also carries ``py_expect``
  the design deliberately deviates (DEV-1 / DEV-2) and ``py_expect`` wins.
* ``encode_cases`` go through ``convert_to_typed_value`` + ``encode_field``
  and are compared byte for byte; ``expect_error`` rows must raise instead.
  ``py_only`` marks a row Go has no way to express (an int wider than uint64)
  and is provenance, not a control flag — such a row still says what it
  expects through ``expect_error``.
* ``envelope`` pins the message layout byte for byte in both directions. A
  second fixture, ``neoflow_v2_2_0.json``, adds the millisecond-UTC envelope
  the pending Go v2.2.0 release publishes; the two stamps straddle CBOR's
  24-byte boundary, so between them they pin a 1-byte and a 2-byte text head.
  Only the v2.1.0 file carries the conversion matrix — the timestamp work did
  not touch it.
"""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from neoedgex.contract import DataType, Message, convert_to_typed_value
from neoedgex.contract.codec import (
    decode_neoflow_envelope,
    encode_field,
    encode_neoflow_message,
    scan_data_map,
)

GOLDEN_DIR = Path(__file__).parent / "testdata" / "golden"
GOLDEN_PATH = GOLDEN_DIR / "neoflow_v2_1_0.json"
GOLDEN: dict[str, Any] = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))

GOLDEN_MS_PATH = GOLDEN_DIR / "neoflow_v2_2_0.json"
GOLDEN_MS: dict[str, Any] = json.loads(GOLDEN_MS_PATH.read_text(encoding="utf-8"))

DECODE_CASES: list[dict[str, Any]] = GOLDEN["decode_cases"]
ENCODE_CASES: list[dict[str, Any]] = GOLDEN["encode_cases"]

# The v2.1.0 envelope keeps a second-precision stamp on purpose: it is the
# evidence that a current SDK still reads an older peer's envelope.
ENVELOPE_FIXTURES = [
    pytest.param(GOLDEN, id="v2_1_0_second_precision"),
    pytest.param(GOLDEN_MS, id="v2_2_0_millisecond_utc"),
]

_MAP_HEAD_ONE = bytes.fromhex("a1")
_MAP_HEAD_EMPTY = bytes.fromhex("a0")
_KEY_K = bytes.fromhex("616b")  # text(1) "k"
_KEY = "k"


def _data_map(case: dict[str, Any]) -> bytes:
    if case.get("absent"):
        return _MAP_HEAD_EMPTY
    return _MAP_HEAD_ONE + _KEY_K + bytes.fromhex(case["wire_hex"])


def _expected_value(case: dict[str, Any]) -> tuple[Any, type | None]:
    """(value, python type) the row expects; a ``None`` type means undefined.

    ``py_expect`` takes precedence over ``expect``: it is the design-ruled
    deviation from the Go delivery (see the deviation list in the run PLAN).
    """
    spec = case.get("py_expect", case["expect"])
    kind = spec["kind"]
    if kind == "undefined":
        return None, None
    raw = spec["value"]
    if kind in ("int", "uint"):
        return int(raw), int
    if kind in ("float32", "float64"):
        if raw == "NaN":
            return math.nan, float
        if raw == "+Inf":
            return math.inf, float
        if raw == "-Inf":
            return -math.inf, float
        return float(raw), float
    if kind == "bool":
        return raw == "true", bool
    if kind == "string":
        return raw, str
    if kind == "bytes":
        return bytes.fromhex(raw), bytes
    raise AssertionError(f"unknown expectation kind {kind!r} in row {case['name']!r}")


@pytest.mark.parametrize(
    "case", DECODE_CASES, ids=[case["name"] for case in DECODE_CASES]
)
def test_decode_case(case: dict[str, Any]) -> None:
    declared = case["declared"]
    plan = {_KEY: DataType(declared)} if declared else None
    message = Message(
        source="upstream-node",
        timestamp="2026-03-31T09:10:11Z",
        handle="input1",
        raw=_data_map(case),
        plan=plan,
    )

    decoded = message.to_dict()
    assert _KEY in decoded, "the key must always be delivered, undefined included"
    got = decoded[_KEY]
    want, want_type = _expected_value(case)

    if want_type is None:
        assert got is None
    elif want_type is bool:
        assert got is want
    elif want_type is float:
        assert isinstance(got, float)
        if math.isnan(want):
            assert math.isnan(got)
        else:
            assert got == want
    else:
        # ``type(...) is`` on purpose: bool is an int subclass, and a bool
        # delivered where an int is expected is a defect, not a pass.
        assert type(got) is want_type
        assert got == want


def _encode_input(case: dict[str, Any]) -> Any:
    kind = case["in_kind"]
    raw = case["in_value"]
    if kind == "int":
        return int(raw)
    if kind == "float":
        if raw == "NaN":
            return math.nan
        if raw == "+Inf":
            return math.inf
        if raw == "-Inf":
            return -math.inf
        return float(raw)
    if kind == "string":
        return raw
    if kind == "bool":
        return raw == "true"
    if kind == "bytes":
        return bytes.fromhex(raw)
    if kind == "datetime":
        # The row only asserts that a datetime is refused, so any aware
        # datetime is representative.
        return datetime(2026, 3, 31, 9, 10, 11, tzinfo=UTC)
    raise AssertionError(f"unknown in_kind {kind!r} in row {case['name']!r}")


@pytest.mark.parametrize(
    "case", ENCODE_CASES, ids=[case["name"] for case in ENCODE_CASES]
)
def test_encode_case(case: dict[str, Any]) -> None:
    declared = DataType(case["declared"])

    if case["in_kind"] == "null":
        # A missing / nil field never reaches the converter: it publishes null.
        assert encode_field(None, declared).hex() == case["expect_hex"]
        return

    value = _encode_input(case)
    if case.get("expect_error"):
        with pytest.raises(ValueError):
            encode_field(convert_to_typed_value(value, declared), declared)
        return

    typed = convert_to_typed_value(value, declared)
    assert encode_field(typed, declared).hex() == case["expect_hex"]


@pytest.mark.parametrize("fixture", ENVELOPE_FIXTURES)
def test_envelope_encode_is_byte_exact(fixture: dict[str, Any]) -> None:
    envelope = fixture["envelope"]
    payload = encode_neoflow_message(
        envelope["source"],
        envelope["timestamp"],
        bytes.fromhex(envelope["data_map_hex"]),
    )
    assert payload.hex() == envelope["message_hex"]


@pytest.mark.parametrize("fixture", ENVELOPE_FIXTURES)
def test_envelope_decode_returns_source_timestamp_and_data_span(fixture: dict[str, Any]) -> None:
    envelope = fixture["envelope"]
    source, timestamp, data = decode_neoflow_envelope(bytes.fromhex(envelope["message_hex"]))
    assert source == envelope["source"]
    assert timestamp == envelope["timestamp"]
    # The data map stays encoded: float width and carried keys are only
    # answerable from the bytes.
    assert data.hex() == envelope["data_map_hex"]


def test_envelope_fixtures_cover_both_timestamp_head_widths() -> None:
    """A 20-byte second-precision stamp and a 24-byte millisecond one sit on
    opposite sides of CBOR's 24-byte boundary, where the text head grows from
    one byte to two. Should both fixtures ever drift onto the same side, the
    wider head would silently stop being covered."""
    heads = {}
    for fixture in (GOLDEN, GOLDEN_MS):
        envelope = fixture["envelope"]
        item = scan_data_map(bytes.fromhex(envelope["message_hex"]))["timestamp"]
        stamp = envelope["timestamp"].encode("utf-8")
        assert item.endswith(stamp)
        heads[len(stamp)] = item[: len(item) - len(stamp)].hex()
    assert heads == {20: "74", 24: "7818"}


@pytest.mark.parametrize(
    "fixture, want_release",
    [
        pytest.param(GOLDEN, "v2.1.0", id="v2_1_0"),
        pytest.param(GOLDEN_MS, "v2.2.0", id="v2_2_0"),
    ],
)
def test_fixture_declares_its_go_source(fixture: dict[str, Any], want_release: str) -> None:
    # Each file claims equivalence with one Go SDK release; if a fixture is ever
    # regenerated from another one, that claim (and the whole file) moves.
    assert want_release in fixture["go_sdk"]
    assert fixture["regen"]


def test_fixture_rows_are_well_formed_and_uniquely_named() -> None:
    # Names are the test ids, so they must be unique inside each section (the
    # two sections share names such as "bool_into_string" on purpose: same
    # conversion, opposite direction).
    for section in (DECODE_CASES, ENCODE_CASES):
        names = [case["name"] for case in section]
        assert len(names) == len(set(names))
    for case in DECODE_CASES:
        assert case.get("absent") or "wire_hex" in case
        assert "expect" in case
    for case in ENCODE_CASES:
        assert case.get("expect_error") or "expect_hex" in case
        assert case["in_kind"]


def test_fixture_rows_carry_self_contained_cbor_items() -> None:
    """Every decode row's ``wire_hex`` must be exactly one complete CBOR item,
    so it can be replayed as a data map entry — a truncated item would make
    the whole map undecodable and turn every row of that message into a false
    "undefined"."""
    broken = set()
    for case in DECODE_CASES:
        if case.get("absent"):
            continue
        try:
            scan_data_map(_data_map(case))
        except Exception:  # noqa: BLE001 — any decode failure counts as broken
            broken.add(case["name"])
    assert broken == set()
