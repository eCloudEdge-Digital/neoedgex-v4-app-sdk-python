# Real-broker integration test (Go SDK v2.1.0 ↔ Python SDK 2.1.0)

Runs both SDKs against a real mosquitto broker over the full production path:
config files mounted at `/opt/neoedgex/config`, the hard-wired broker name
`neoedgex-messenger:1883` resolved via a docker network alias, and the NeoFlow
engine's out→in routing played by a host-side bridge. **Not collected by
pytest** — run it manually.

## Prerequisites

- Docker with the compose plugin; images `eclipse-mosquitto:2`, `golang:1.24`
  and `python:3.13-slim` (pulled automatically when missing).
- Network access on the first run (`go mod tidy` module downloads, `pip
  install`); afterwards cached in named volumes.
- The Go SDK repo checked out next to this repo as
  `../neoedgex-v4-app-sdk-go` (override with `GO_SDK_PATH=<path>`); it is
  mounted read-only.
- Host port 1883 free.
- The repo venv (`.venv/`) with the SDK installed editable — the harness
  reuses its `neoedgex` codec and `paho-mqtt`.

## Run

```sh
.venv/bin/python tests/integration/harness.py
```

Flags: `--keep-up` leaves the containers running for debugging; `--purge`
also removes the build/module cache volumes on teardown.

## What it does

Both apps declare the same node config (14-field battery schema; the *input*
side declares `f_float` as `double` and `f_double` as `float` — the deliberate
float-width crossing). Each app publishes the battery when the harness sends a
`trigger` message and dumps every decoded `input1` message (value + concrete
type) as JSON to `itest/results/<lang>`.

- **A** Go→Python: trigger go-node, bridge forwards to py-node, assert every
  decoded key's value and type (expectations pinned from the golden fixture).
- **B** Python→Go: the mirror image.
- **C** Byte level: per-field comparison of the raw CBOR data-map spans both
  languages published for the same logical values (incl. `f_float` head byte
  `0xfa`), each also checked against the codec's expected encoding.
- **D** Liveness: empty heartbeats every ~5 s from both nodes, and the
  `f_overflow` (70000 into int16) field producing a JSON error event with keys
  `code`/`detail`/`updatedAt` on `neoedgex/neoflow/error/<node>`.
- **E** Reconnect: `docker restart` the broker, assert both apps' heartbeats
  return within ~15 s and a fresh battery round succeeds in both directions.

## Expected output

A `PASS` line for each of the five scenarios and exit code 0:

```
PASS  A go->py typed battery
PASS  B py->go typed battery
PASS  C byte-level encode parity
PASS  D heartbeat + error-topic liveness
PASS  E broker-restart recovery
```

Raw bridged payloads and the report land in `.artifacts/run-<timestamp>/`
(gitignored). Teardown removes the containers and network; the harness prints
the leftover-container check.
