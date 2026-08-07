"""The type table, executable.

This file is documentation first: it builds one message the way an upstream
node would produce it and states, key by key and as a literal, what the
handler receives and in which Python type. It is the counterpart of the Go
SDK's ``ExampleMessage_ToMap`` output block — when the implementation drifts
from the documented table, this file goes red.

Reading guide for each row: *wire value* is what the sender put on the wire
(``testutil.Single`` = single-precision 0xfa, a bare ``float`` = 0xfb),
*declared* is the type the receiving node's input schema declares for that key
(``UNDECLARED`` = the schema does not mention the key, so it arrives in its
natural CBOR domain), and the last two columns are the delivered value and its
type.
"""

from __future__ import annotations

from typing import Any, NamedTuple

import cbor2
import pytest

from neoedgex import testutil
from neoedgex.contract import SUPPORTED_TYPES, DataType, Node, NodeData, PortFieldSchema


class Row(NamedTuple):
    key: str
    wire_value: Any
    declared: Any
    delivered: Any
    delivered_type: type | None  # None = undefined (delivered as None)


TYPE_TABLE: list[Row] = [
    # --- the eleven declarable types, one field each -------------------------
    Row("bool", True, DataType.BOOL, True, bool),
    Row("int16", -12345, DataType.INT16, -12345, int),
    Row("int32", -2147483648, DataType.INT32, -2147483648, int),
    Row("int64", -9223372036854775808, DataType.INT64, -9223372036854775808, int),
    Row("uint16", 65535, DataType.UINT16, 65535, int),
    Row("uint32", 4294967295, DataType.UINT32, 4294967295, int),
    Row("uint64", 18446744073709551615, DataType.UINT64, 18446744073709551615, int),
    Row("float", testutil.Single(25.34), DataType.FLOAT, 25.34, float),
    Row("double", 25.34, DataType.DOUBLE, 25.34, float),
    Row("string", "sensor-1", DataType.STRING, "sensor-1", str),
    Row("raw", b"\x01\x02", DataType.RAW, b"\x01\x02", bytes),
    # --- float width crossings ----------------------------------------------
    # A single-precision wire value in a double field is restored to the
    # decimal it was meant to carry, not widened to 25.34000015258789.
    Row("singleIntoDouble", testutil.Single(25.34), DataType.DOUBLE, 25.34, float),
    # A double wire value in a float field is narrowed through the matrix.
    Row("doubleIntoFloat", 25.34, DataType.FLOAT, 25.34, float),
    # ... and refused when it does not fit float32, leaving the field undefined.
    Row("tooLargeForFloat", 1e300, DataType.FLOAT, None, None),
    # An explicit null on the wire is undefined, like an absent key.
    Row("nullValue", None, DataType.DOUBLE, None, None),
    # --- keys the input schema does not declare (bypass path) ---------------
    # No declared width to restore, so a single-precision value arrives
    # widened, exactly as CBOR decoded it.
    Row("bypassSingle", testutil.Single(25.34), testutil.UNDECLARED, 25.34000015258789, float),
    Row(
        "bypassNegative",
        -9223372036854775808,
        testutil.UNDECLARED,
        -9223372036854775808,
        int,
    ),
    # Above MaxInt64 the natural domain is uint64; still a plain Python int.
    Row(
        "bypassBigUnsigned",
        18446744073709551615,
        testutil.UNDECLARED,
        18446744073709551615,
        int,
    ),
    Row("bypassBytes", b"\x01\x02", testutil.UNDECLARED, b"\x01\x02", bytes),
]


@pytest.fixture(scope="module")
def delivered() -> dict[str, Any]:
    message = testutil.new_message(
        "input1", {row.key: (row.wire_value, row.declared) for row in TYPE_TABLE}
    )
    return message.to_dict()


@pytest.mark.parametrize("row", TYPE_TABLE, ids=[row.key for row in TYPE_TABLE])
def test_type_table_row(row: Row, delivered: dict[str, Any]) -> None:
    got = delivered[row.key]
    if row.delivered_type is None:
        assert got is None, f"{row.key} must be delivered as undefined"
        return
    if row.delivered_type is bool:
        assert got is row.delivered
        return
    assert type(got) is row.delivered_type
    assert got == row.delivered


def test_type_table_delivers_exactly_the_declared_and_carried_keys(
    delivered: dict[str, Any],
) -> None:
    assert set(delivered) == {row.key for row in TYPE_TABLE}


def test_type_table_covers_every_declarable_type() -> None:
    covered = {row.declared for row in TYPE_TABLE if isinstance(row.declared, DataType)}
    assert covered == SUPPORTED_TYPES, (
        "a type in SUPPORTED_TYPES has no row in the type table; add one here"
    )


def test_declared_key_absent_from_the_wire_is_delivered_as_none() -> None:
    """A key the schema declares but the upstream never produced: present in
    the delivered mapping, valued None, and absent from the wire itself."""
    env = testutil.MockNodeEnv(
        config=Node(
            id="node-1",
            type="demo",
            data=NodeData(
                name="demo-node",
                inputs={
                    "input1": [
                        PortFieldSchema(key="present", type=DataType.INT64),
                        PortFieldSchema(key="absent", type=DataType.DOUBLE),
                    ]
                },
            ),
        )
    )
    message = env.new_message("input1", {"present": 7})

    assert message.to_dict() == {"present": 7, "absent": None}
    # The None above is the schema speaking, not the wire: "absent" never made
    # it onto the wire at all, while "present" did.
    assert set(cbor2.loads(message.raw)) == {"present"}
