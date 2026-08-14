# NeoEdgeX App SDK Python v4 Design

This document records the Python SDK architecture and runtime contract. External developers should still start from the developer guides in [`docs/`](./docs/): everything an app author needs — the type table, the conversion rules, the message-reading API — lives there and is not repeated here.

## Purpose

The SDK lets third-party developers build NeoEdgeX node applications in Python while reusing the same runtime model — and, since 2.0.0, the same wire contract — as the Go SDK (Go v2.2.0):

- receive NeoFlow messages through `ctx.messages()`
- read raw node configuration through `ctx.node_config()`
- publish output through `ctx.publish(handle, ...)`
- report platform-visible errors through `ctx.report_error(...)`

The SDK owns the platform-facing shell:

- MQTT transport integration
- node lifecycle supervision
- heartbeat publication
- signal handling and graceful shutdown
- mock-mode execution

## Architecture

```text
neoedgex/                 public entrypoint
neoedgex/contract/        contract models, wire codec, value conversion
  types.py                DataType enum and the cross-type conversion matrix
  convert.py              native-value conversion engine (shared by publish and decode)
  _float.py               float32 narrowing and shortest-decimal restore helpers
  codec.py                CBOR wire codec: field/map/envelope encoders, raw-span
                          scanner, schema-driven and natural-domain field decoders
  models.py               Message (lazy decode), Node / schema / runtime models
  values.py               PortFieldData: stringified type/value pairs for mock
                          config and device-facing code (not a wire type)
neoedgex/mock/            public mock config loader
neoedgex/testutil/        public handler unit-test helpers (wire-realistic messages)
neoedgex/_internal/       private runtime implementation
template/                 example Python app project
```

### Public Surface

- `neoedgex.App`
- `neoedgex.new(handler)`
- `neoedgex.load_mock_config(...)`
- `neoedgex.NodeHandler`
- `neoedgex.NodeEnv`
- `neoedgex.Message` — `to_dict()`, `to_dataclass(...)`, `raw`
- `neoedgex.Logger`
- `neoedgex.DataType`
- `neoedgex.ErrorCode`
- `neoedgex.convert_to_typed_value(...)`
- `neoedgex.contract.PortFieldSchema`, `neoedgex.contract.NodeData`
- `neoedgex.mock.load_config(...)`, `neoedgex.mock.MockConfig`
- `neoedgex.testutil.MockNodeEnv`, `neoedgex.testutil.new_message`, `UNDECLARED`, `Single`

Everything under `neoedgex._internal` is intentionally unstable.

## Wire Codec and Lazy Message Decoding

A NeoFlow data message is a CBOR map `{source, timestamp, data}`; `data` maps field keys to native CBOR values (see the developer guide for the app-facing contract). The codec in `contract/codec.py` splits the work with `cbor2`:

- **Encoding is fully self-written.** `encode_field` emits each declared type deterministically — the `float` tag deliberately as single precision (`0xfa`), `double` as `0xfb`, integers in their shortest CBOR form — and `encode_neoflow_message` pins the envelope layout. This keeps the emitted bytes identical to the Go SDK's, which the golden fixture checks byte for byte.
- **Decoding scans first, decodes later.** `scan_data_map` walks the top-level map and returns `key -> raw byte span` without decoding values; it handles nested and indefinite-length items, tags, and duplicate keys. Field decoding then routes on the span's head byte: float-headed spans (`0xf9`/`0xfa`/`0xfb`) are unpacked width-aware, everything else goes through `cbor2.loads` and the conversion matrix.

The scanner exists because of float width: `cbor2` widens every float to a Python double on decode, erasing the `0xfa` vs `0xfb` distinction. Restoring the shortest decimal for a single-precision wire value (`25.34` instead of `25.34000015258789`) is only possible while the head byte is still visible — after `cbor2.loads` the information is gone.

`Message` is therefore raw-bytes based and decodes lazily:

- `msg.raw` keeps the still-encoded `data` span, sliced out of the envelope at receive time (`decode_neoflow_envelope` decodes only `source` and `timestamp` eagerly).
- `msg.to_dict()` decodes on first call against the input-schema plan (`key -> DataType`) captured when the message was built, and caches the decoded map — each call returns a fresh shallow copy of it, so repeated calls decode once but never share the returned dict.
- `msg.to_dataclass(...)` decodes a span directly off the raw bytes when its head byte matches the field's annotation (declaration wins, = Go `ToStruct`), and reads the `to_dict()` result for the rest — another read the cached span scan serves without a second decode.

A consequence of deferring the decode: a payload whose `data` section is not a CBOR map is not rejected at receive time (the Go SDK drops it there); the handler still gets the `Message`, and `to_dict()` warns and returns `{}`. This and the other deliberate Go/Python differences are listed in the developer guide's "Known Differences" section and pinned in the golden fixture.

Both conversion directions share `contract/convert.py` (`convert_to_typed_value`): publish converts app values to declared types before encoding, and schema decoding converts wire values that arrive as a different type than declared. `contract/_float.py` holds the float32 machinery (narrowing, range check, shortest-decimal restore) both paths use.

## Lifecycle

`App.run()` follows the same high-level lifecycle as the Go SDK:

1. create the SDK runtime
2. optionally enable mock mode
3. initialize node config and messenger state
4. start one node instance per matched node
5. connect the messenger and block until shutdown
6. stop instances and disconnect cleanly

Each matched node gets its own handler execution path. If the handler raises or returns early while the node is still active, the SDK treats that as a crash and restarts it with exponential backoff from 1 second up to 30 seconds. If the handler stays healthy for 30 seconds or more, the backoff resets.

## Message and Topic Contract

Topic compatibility is kept aligned with the Go SDK:

- input subscribe: `neoedgex/neoflow/in/{nodeID}/+`
- output publish: `neoedgex/neoflow/out/{nodeID}/{handle}`
- node error: `neoedgex/neoflow/error/{nodeID}`
- heartbeat: `neoedgex/neoflow/heartbeat/{nodeID}`

`ctx.publish(handle, ...)` looks up the output schema for the handle, converts each value with `convert_to_typed_value`, and encodes the CBOR envelope with a current UTC RFC3339 timestamp, rendered to millisecond precision by `_format_rfc3339` (fixed three-digit fraction, sub-millisecond truncated, always `Z` because the clock is read as UTC — byte-identical to the Go SDK's `2006-01-02T15:04:05.000Z07:00` layout for the same instant; the renderer itself still emits a numeric offset for a non-UTC datetime passed in directly). Missing keys, explicit `None`, and per-field conversion failures all encode as CBOR null; a conversion failure additionally reports a node error, but the publish itself still goes out. Only data messages are CBOR: the error topic payload stays JSON and heartbeats stay empty.

Inbound payloads have their envelope decoded eagerly (`source`, `timestamp`); the `data` section is kept as raw bytes on the `Message` and decoded when the handler asks (see the previous section).

Cross-language equivalence is guarded by `tests/testdata/golden/` — decode rows, encode rows, and envelope vectors produced by actually running the Go SDK (the v2.1.0 fixture from released v2.1.0, the v2.2.0 fixture from the pending v2.2.0 release) — replayed by `tests/test_golden.py`, field-encoded bytes compared exactly.

## Mock Mode

Mock mode uses `neoedgex.mock.load_config(...)` plus `App.enable_mock(config)`.

Behavior:

- the SDK swaps MQTT for an in-memory messenger
- configured mock messages are injected in round-robin order; the interval defaults to 3 seconds if missing or invalid
- publishes are surfaced through the SDK logger with a `[MOCK PUBLISH]` prefix

The injection path is where `PortFieldData` earns its place: mock config entries are stringified `{type, value}` pairs (a legacy `format` key is tolerated and ignored). At injection time each entry is parsed to its native value, encoded through the same `encode_data_map` / `encode_neoflow_message` path production publishing uses (source `"mock"`, empty timestamp), and delivered through the normal subscriber queue — so the handler exercises the real decode path, not a shortcut.

`neoedgex.load_mock_config(...)` is a top-level convenience wrapper for `neoedgex.mock.load_config(...)`.

## Test Utility and Template

`neoedgex.testutil` builds wire-realistic messages for handler unit tests: `new_message(handle, fields)` encodes each wire value by its Python type (with `Single` forcing single-precision encoding and `UNDECLARED` keeping a key out of the decode plan), and `MockNodeEnv.new_message(handle, data)` reads the declared types from the configured input schema instead. `MockNodeEnv` records `published_data`, `reported_errors`, and `stop_called`.

The `template/` project mirrors the public app shape at a practical Python level:

- production entrypoint
- mock-neoedgex entrypoint with mock config
- handler unit tests using `MockNodeEnv`

## Mount Path

The default runtime mount path is `/opt/neoedgex`.

Expected files:

- `config/config.json`: node config array
- `config/messenger.json`: messenger username/password
- `common/parameters.json`: documented platform file, not currently consumed by runtime
