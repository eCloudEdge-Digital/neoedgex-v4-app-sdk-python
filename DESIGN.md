# NeoEdgeX App SDK Python v4 Design

This document records the Python SDK architecture and runtime contract. External developers should still start from the developer guides in [`docs/`](./docs/).

## Purpose

The SDK lets third-party developers build NeoEdgeX node applications in Python while reusing the same runtime model as the Go SDK:

- receive NeoFlow messages through `ctx.messages()`
- read raw node configuration through `ctx.node_config()`
- publish output through `ctx.publish(handle, ...)`
- report platform-visible errors through `ctx.report_error(...)`

The SDK owns the platform-facing shell:

- MQTT transport integration
- node lifecycle supervision
- heartbeat and status publication
- signal handling and graceful shutdown
- mock-mode execution

## Architecture

```text
neoedgex/                 public entrypoint
neoedgex/contract/        SDK contract models used by runtime/config loading
neoedgex/mock/            public mock config loader
neoedgex/testutil/        public handler unit-test helpers
neoedgex/_internal/       private runtime implementation
template/                 example Python app project
```

### Public Surface

- `neoedgex.App`
- `neoedgex.new(handler)`
- `neoedgex.load_mock_config(...)`
- `neoedgex.NodeHandler`
- `neoedgex.NodeEnv`
- `neoedgex.Message`
- `neoedgex.Logger`
- `neoedgex.PortField`
- `neoedgex.ErrorCode`
- `neoedgex.mock.load_config(...)`
- `neoedgex.mock.MockConfig`
- `neoedgex.testutil.MockNodeEnv`

Everything under `neoedgex._internal` is intentionally unstable.

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

`ctx.publish(handle, ...)` looks up the matching output schema from the node config:

- missing keys are emitted as empty fields
- explicitly provided `None` is emitted as an empty field
- published messages include a current UTC RFC3339 timestamp
- values are converted into NeoFlow `PortFieldData` using the destination format

Inbound payloads are decoded to Python-native handler values without extra schema validation, matching current Go SDK behavior. Empty, undefined, or malformed input fields become `None`. Inbound timestamps are preserved when present and exposed as `Message.timestamp`; otherwise they are an empty string.

## Mock Mode

Mock mode uses `neoedgex.mock.load_config(...)` plus `App.enable_mock(config)`.

Behavior:

- the SDK swaps MQTT for an in-memory messenger
- configured mock messages are injected in round-robin order
- message interval defaults to 3 seconds if missing or invalid
- publishes are printed to stdout with a `[MOCK PUBLISH]` prefix

`neoedgex.load_mock_config(...)` is a top-level convenience wrapper for `neoedgex.mock.load_config(...)`.

## Test Utility and Template

`neoedgex.testutil.MockNodeEnv` is the public helper for handler unit tests. It can provide node config, message iterables, lifecycle cancellation, logger behavior, publish errors, and records published data, reported errors, and stop calls. Each publish call is recorded as a `PublishedMessage(handle, data)` so tests can assert which output handle was used.

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
