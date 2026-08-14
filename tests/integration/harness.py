"""Real-broker cross-language integration harness.

Not collected by pytest — run it manually from the repo root:

    .venv/bin/python tests/integration/harness.py

See tests/integration/README.md for prerequisites, scenarios and flags.
"""

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import threading
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import cbor2
import paho.mqtt.client as mqtt

from neoedgex.contract import DataType
from neoedgex.contract.codec import (
    decode_neoflow_envelope,
    encode_data_map,
    encode_field,
    encode_neoflow_message,
    scan_data_map,
)
from neoedgex.contract.convert import convert_to_typed_value

INTEGRATION_DIR = Path(__file__).resolve().parent
MOSQUITTO_CONTAINER = "neoedgex-itest-mosquitto"
GO_NODE = "go-node"
PY_NODE = "py-node"
STARTUP_TIMEOUT = 600.0
RESULT_TIMEOUT = 30.0
RECOVERY_LIMIT = 15.0

# The NeoFlow engine's out->in routing, played by this harness.
BRIDGE_ROUTES = {
    f"neoedgex/neoflow/out/{GO_NODE}/output1": ("go_to_py", f"neoedgex/neoflow/in/{PY_NODE}/input1"),
    f"neoedgex/neoflow/out/{PY_NODE}/output1": ("py_to_go", f"neoedgex/neoflow/in/{GO_NODE}/input1"),
}

# key, publisher-declared type, published native value, expected value string,
# expected Python type name (receiver declares f_float=double / f_double=float),
# expected Go type string. f_missing is never provided; f_overflow (70000 into
# int16) goes out as null plus a node error.
BATTERY = [
    ("f_bool", DataType.BOOL, True, "true", "bool", "bool"),
    ("f_int16", DataType.INT16, 32767, "32767", "int", "int16"),
    ("f_int32", DataType.INT32, -123456, "-123456", "int", "int32"),
    ("f_int64", DataType.INT64, -(2**63), "-9223372036854775808", "int", "int64"),
    ("f_uint16", DataType.UINT16, 65535, "65535", "int", "uint16"),
    ("f_uint32", DataType.UINT32, 4294967295, "4294967295", "int", "uint32"),
    ("f_uint64", DataType.UINT64, 2**64 - 1, "18446744073709551615", "int", "uint64"),
    ("f_float", DataType.FLOAT, 25.34, "25.34", "float", "float64"),
    ("f_double", DataType.DOUBLE, 25.34, "25.34", "float", "float32"),
    ("f_string", DataType.STRING, "hello-neoedgex", "hello-neoedgex", "str", "string"),
    ("f_raw", DataType.RAW, b"\x01\x02\xfe\xff", "0102feff", "bytes", "[]uint8"),
    ("f_missing", DataType.DOUBLE, None, "", "NoneType", "nil"),
    ("f_overflow", DataType.INT16, None, "", "NoneType", "nil"),
]


def _now_rfc3339() -> str:
    # Deliberately second-precision, unlike the millisecond stamp both SDKs now
    # publish: the harness plays an upstream peer on the older format, so every
    # scenario below doubles as proof that a second-precision publisher still
    # interoperates with both SDKs over a real broker.
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Hub:
    """Single host-side MQTT client: NeoFlow bridge, heartbeat/error/results
    listener, and raw-payload recorder for the byte-level scenario."""

    def __init__(self, artifacts_dir: Path) -> None:
        self._artifacts_dir = artifacts_dir
        self._lock = threading.Lock()
        self.connected = threading.Event()
        self.heartbeats: dict[str, list[tuple[float, int]]] = defaultdict(list)
        self.errors: dict[str, list[tuple[float, bytes]]] = defaultdict(list)
        self.results: dict[str, list[tuple[float, dict[str, Any]]]] = defaultdict(list)
        self.captures: dict[str, list[tuple[float, bytes]]] = defaultdict(list)
        self._capture_seq = 0
        self._client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="itest-harness")
        self._client.reconnect_delay_set(min_delay=1, max_delay=2)
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message = self._on_message

    def connect(self, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while True:
            try:
                self._client.connect("127.0.0.1", 1883, keepalive=30)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(1.0)
        self._client.loop_start()
        if not self.connected.wait(timeout=timeout):
            raise TimeoutError("harness MQTT client did not connect")

    def stop(self) -> None:
        try:
            self._client.disconnect()
        finally:
            self._client.loop_stop()

    def publish(self, topic: str, payload: bytes, qos: int) -> None:
        info = self._client.publish(topic, payload=payload, qos=qos)
        info.wait_for_publish(timeout=10.0)

    def trigger(self, node: str, round_no: int) -> None:
        payload = encode_neoflow_message(
            "harness", _now_rfc3339(), encode_data_map([("round", DataType.INT64, round_no)])
        )
        self.publish(f"neoedgex/neoflow/in/{node}/trigger", payload, qos=2)

    def _on_connect(self, client: Any, *_args: Any, **_kwargs: Any) -> None:
        subscriptions = [(topic, 2) for topic in BRIDGE_ROUTES]
        subscriptions += [
            ("neoedgex/neoflow/heartbeat/+", 0),
            ("neoedgex/neoflow/error/+", 0),
            ("itest/results/#", 1),
        ]
        client.subscribe(subscriptions)
        self.connected.set()

    def _on_disconnect(self, *_args: Any, **_kwargs: Any) -> None:
        self.connected.clear()

    def _on_message(self, _client: Any, _userdata: Any, msg: Any) -> None:
        now = time.monotonic()
        payload = bytes(msg.payload)
        route = BRIDGE_ROUTES.get(msg.topic)
        if route is not None:
            direction, dest_topic = route
            self._client.publish(dest_topic, payload=payload, qos=2)
            with self._lock:
                self.captures[direction].append((now, payload))
                self._capture_seq += 1
                seq = self._capture_seq
            capture_path = self._artifacts_dir / "captures" / f"{direction}-{seq:03d}.bin"
            capture_path.parent.mkdir(parents=True, exist_ok=True)
            capture_path.write_bytes(payload)
            return
        parts = msg.topic.split("/")
        if msg.topic.startswith("neoedgex/neoflow/heartbeat/"):
            with self._lock:
                self.heartbeats[parts[3]].append((now, len(payload)))
        elif msg.topic.startswith("neoedgex/neoflow/error/"):
            with self._lock:
                self.errors[parts[3]].append((now, payload))
        elif msg.topic.startswith("itest/results/"):
            try:
                dump = json.loads(payload.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                dump = {"_malformed": payload.hex()}
            with self._lock:
                self.results[parts[2]].append((now, dump))

    def snapshot(self, attribute: str) -> dict[str, list[Any]]:
        with self._lock:
            return {key: list(value) for key, value in getattr(self, attribute).items()}


def wait_for(predicate: Any, timeout: float, interval: float = 0.2) -> Any:
    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if value:
            return value
        if time.monotonic() >= deadline:
            return None
        time.sleep(interval)


def wait_result(hub: Hub, lang: str, round_no: int, timeout: float) -> dict[str, Any] | None:
    def probe() -> dict[str, Any] | None:
        for _ts, dump in hub.snapshot("results").get(lang, []):
            fields = dump.get("fields", {})
            if fields.get("f_round", {}).get("value") == str(round_no):
                return dump
        return None

    return wait_for(probe, timeout)


def check_result(dump: dict[str, Any], lang: str, source_node: str, round_no: int) -> list[str]:
    problems: list[str] = []
    if dump.get("source") != source_node:
        problems.append(f"source={dump.get('source')!r}, want {source_node!r}")
    if not dump.get("timestamp"):
        problems.append("timestamp is empty")
    fields = dump.get("fields", {})
    round_type = "int" if lang == "py" else "int32"
    expected = {key: (value_str, py_t if lang == "py" else go_t) for key, _dt, _v, value_str, py_t, go_t in BATTERY}
    expected["f_round"] = (str(round_no), round_type)
    if set(fields) != set(expected):
        problems.append(f"key set mismatch: got {sorted(fields)}")
    for key, (want_value, want_type) in expected.items():
        got = fields.get(key)
        if got is None:
            problems.append(f"{key}: missing")
            continue
        if got.get("value") != want_value or got.get("type") != want_type:
            problems.append(
                f"{key}: got {got.get('value')!r}/{got.get('type')}, want {want_value!r}/{want_type}"
            )
    return problems


def find_capture(hub: Hub, direction: str, round_no: int) -> tuple[str, dict[str, bytes]] | None:
    for _ts, payload in hub.snapshot("captures").get(direction, []):
        try:
            source, _timestamp, data_span = decode_neoflow_envelope(payload)
            fields = scan_data_map(data_span)
            round_span = fields.get("f_round")
            if round_span is not None and cbor2.loads(round_span) == round_no:
                return source, fields
        except Exception:
            continue
    return None


def scenario_bytes(hub: Hub, round_no: int) -> list[str]:
    problems: list[str] = []
    go_capture = find_capture(hub, "go_to_py", round_no)
    py_capture = find_capture(hub, "py_to_go", round_no)
    if go_capture is None or py_capture is None:
        return [f"missing captures for round {round_no}: go={go_capture is not None} py={py_capture is not None}"]
    (go_source, go_fields), (py_source, py_fields) = go_capture, py_capture
    if go_source != GO_NODE or py_source != PY_NODE:
        problems.append(f"envelope sources: go={go_source!r} py={py_source!r}")
    for key, data_type, value, *_rest in BATTERY:
        expected_span = (
            b"\xf6" if value is None else encode_field(convert_to_typed_value(value, data_type), data_type)
        )
        go_span, py_span = go_fields.get(key), py_fields.get(key)
        if go_span != py_span or go_span != expected_span:
            problems.append(
                f"{key}: go={go_span.hex() if go_span else None} "
                f"py={py_span.hex() if py_span else None} want={expected_span.hex()}"
            )
    if go_fields.get("f_round") != py_fields.get("f_round"):
        problems.append("f_round spans differ between languages")
    for lang, fields in (("go", go_fields), ("py", py_fields)):
        span = fields.get("f_float", b"")
        if not span or span[0] != 0xFA:
            problems.append(f"{lang} f_float head is not 0xfa: {span.hex() if span else 'missing'}")
    return problems


def check_error_event(hub: Hub, node: str, after: float) -> list[str]:
    events = [payload for ts, payload in hub.snapshot("errors").get(node, []) if ts >= after]
    if not events:
        return [f"no error event on neoedgex/neoflow/error/{node}"]
    problems: list[str] = []
    matched = False
    for payload in events:
        try:
            event = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            problems.append(f"error payload is not JSON: {payload[:80]!r}")
            continue
        if "f_overflow" not in event.get("detail", ""):
            continue
        matched = True
        if set(event) != {"code", "detail", "updatedAt"}:
            problems.append(f"error event keys: {sorted(event)}")
        if event.get("code") != "PROCESS_ERROR":
            problems.append(f"error event code: {event.get('code')!r}")
        if not isinstance(event.get("updatedAt"), int):
            problems.append(f"error event updatedAt: {event.get('updatedAt')!r}")
        break
    if not matched:
        problems.append(f"no error event mentioning f_overflow on {node} ({len(events)} events seen)")
    return problems


def heartbeat_stats(hub: Hub, node: str, until: float) -> tuple[list[str], str]:
    beats = [(ts, size) for ts, size in hub.snapshot("heartbeats").get(node, []) if ts <= until]
    if len(beats) < 2:
        return [f"{node}: only {len(beats)} heartbeats seen"], ""
    problems = []
    if any(size != 0 for _ts, size in beats):
        problems.append(f"{node}: non-empty heartbeat payload")
    gaps = [b[0] - a[0] for a, b in zip(beats, beats[1:])]
    median_gap = statistics.median(gaps)
    if not 3.5 <= median_gap <= 6.5:
        problems.append(f"{node}: heartbeat median gap {median_gap:.2f}s not ~5s")
    return problems, f"{node}: {len(beats)} beats, median gap {median_gap:.2f}s"


def compose(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", "compose", *args], cwd=INTEGRATION_DIR, capture_output=True, text=True
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keep-up", action="store_true", help="leave the containers running afterwards")
    parser.add_argument("--purge", action="store_true", help="also remove the build/module cache volumes")
    args = parser.parse_args()

    artifacts_dir = INTEGRATION_DIR / ".artifacts" / f"run-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    artifacts_dir.mkdir(parents=True)
    report: list[tuple[str, bool, list[str]]] = []
    hub = Hub(artifacts_dir)

    print("[harness] docker compose up -d ...")
    up = compose("up", "-d")
    if up.returncode != 0:
        print(up.stdout + up.stderr)
        return 1

    try:
        hub.connect(timeout=120.0)
        print("[harness] connected to broker; waiting for both apps' heartbeats "
              f"(up to {STARTUP_TIMEOUT:.0f}s — first run compiles the Go app and installs the SDK) ...")
        ready = wait_for(
            lambda: hub.snapshot("heartbeats").get(GO_NODE) and hub.snapshot("heartbeats").get(PY_NODE),
            timeout=STARTUP_TIMEOUT,
            interval=1.0,
        )
        if not ready:
            report.append(("startup", False, ["apps never published heartbeats"]))
            raise RuntimeError("startup failed")
        print("[harness] both apps alive")
        time.sleep(2.0)

        # Scenario A: Go -> Python typed battery.
        a_start = time.monotonic()
        hub.trigger(GO_NODE, 1)
        dump = wait_result(hub, "py", 1, RESULT_TIMEOUT)
        problems = ["no result from py-app"] if dump is None else check_result(dump, "py", GO_NODE, 1)
        report.append(("A go->py typed battery", not problems, problems))

        # Scenario B: Python -> Go typed battery.
        b_start = time.monotonic()
        hub.trigger(PY_NODE, 1)
        dump = wait_result(hub, "go", 1, RESULT_TIMEOUT)
        problems = ["no result from go-app"] if dump is None else check_result(dump, "go", PY_NODE, 1)
        report.append(("B py->go typed battery", not problems, problems))

        # Scenario C: per-field byte equality of both round-1 envelopes.
        problems = scenario_bytes(hub, 1)
        report.append(("C byte-level encode parity", not problems, problems))

        # Scenario D: liveness — heartbeats plus the JSON error events caused
        # by f_overflow in rounds A and B.
        wait_for(lambda: len(hub.snapshot("heartbeats").get(GO_NODE, [])) >= 4, timeout=25.0, interval=1.0)
        now = time.monotonic()
        problems, notes = [], []
        for node in (GO_NODE, PY_NODE):
            node_problems, note = heartbeat_stats(hub, node, until=now)
            problems += node_problems
            if note:
                notes.append(note)
        problems += check_error_event(hub, GO_NODE, after=a_start - 1.0)
        problems += check_error_event(hub, PY_NODE, after=b_start - 1.0)
        report.append(("D heartbeat + error-topic liveness", not problems, problems or notes))

        # Scenario E: broker restart, reconnect within RECOVERY_LIMIT, then a
        # fresh battery round in both directions.
        print("[harness] restarting mosquitto ...")
        subprocess.run(["docker", "restart", MOSQUITTO_CONTAINER], check=True, capture_output=True)
        broker_up = time.monotonic()
        problems, notes = [], []
        recovery: dict[str, float | None] = {}
        for node in (GO_NODE, PY_NODE):
            beat = wait_for(
                lambda node=node: next(
                    (ts for ts, _size in hub.snapshot("heartbeats").get(node, []) if ts > broker_up), None
                ),
                timeout=RECOVERY_LIMIT + 5.0,
                interval=0.5,
            )
            recovery[node] = None if beat is None else beat - broker_up
        for node, seconds in recovery.items():
            if seconds is None:
                problems.append(f"{node}: no heartbeat within {RECOVERY_LIMIT + 5.0:.0f}s of broker restart")
            elif seconds > RECOVERY_LIMIT:
                problems.append(f"{node}: recovered after {seconds:.1f}s (> {RECOVERY_LIMIT:.0f}s)")
            else:
                notes.append(f"{node}: heartbeat back {seconds:.1f}s after restart")
        if not hub.connected.wait(timeout=30.0):
            problems.append("harness client did not reconnect")
        time.sleep(3.0)
        hub.trigger(GO_NODE, 2)
        hub.trigger(PY_NODE, 2)
        for lang, source in (("py", GO_NODE), ("go", PY_NODE)):
            dump = wait_result(hub, lang, 2, RESULT_TIMEOUT)
            if dump is None:
                problems.append(f"no round-2 result from {lang}-app")
            else:
                problems += check_result(dump, lang, source, 2)
                notes.append(f"round-2 battery via {source} -> {lang} OK")
        report.append(("E broker-restart recovery", not problems, problems or notes))
    except Exception as exc:
        report.append(("harness", False, [f"aborted: {exc!r}"]))
    finally:
        hub.stop()
        if not args.keep_up:
            down_args = ["down", "--remove-orphans"]
            if args.purge:
                down_args.append("-v")
            down = compose(*down_args)
            print(down.stdout + down.stderr)
            leftovers = subprocess.run(
                ["docker", "ps", "-a", "--filter", "name=neoedgex-itest", "--format", "{{.Names}} {{.Status}}"],
                capture_output=True,
                text=True,
            )
            print("[cleanup] containers left:", leftovers.stdout.strip() or "(none)")

    lines = ["", "=" * 72]
    for name, passed, details in report:
        lines.append(f"{'PASS' if passed else 'FAIL'}  {name}")
        for detail in details:
            lines.append(f"      - {detail}")
    lines.append("=" * 72)
    text = "\n".join(lines)
    print(text)
    (artifacts_dir / "report.txt").write_text(text + "\n", encoding="utf-8")
    return 0 if all(passed for _name, passed, _details in report) else 1


if __name__ == "__main__":
    sys.exit(main())
