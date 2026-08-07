import json
import threading
import time
from typing import Any

import paho.mqtt.client as mqtt

import neoedgex

RESULTS_TOPIC = "itest/results/py"
BROKER = "neoedgex-messenger"


def _fmt(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (bytes, bytearray)):
        return value.hex()
    if isinstance(value, float):
        return repr(value)
    return str(value)


class BatteryHandler:
    """Publishes the type battery on "trigger" messages and dumps every
    decoded "input1" message to the results topic. The results channel uses
    its own MQTT client because NodeEnv.publish only reaches schema-typed
    output topics, never an arbitrary one."""

    def __init__(self, results: mqtt.Client) -> None:
        self._results = results

    def handle(self, ctx: neoedgex.NodeEnv) -> None:
        logger = ctx.logger()
        for msg in ctx.messages():
            if msg.handle == "trigger":
                round_no = msg.to_dict().get("round")
                logger.info("trigger received, publishing battery round %s", round_no)
                data = {
                    "f_bool": True,
                    "f_int16": 32767,
                    "f_int32": -123456,
                    "f_int64": -9223372036854775808,
                    "f_uint16": 65535,
                    "f_uint32": 4294967295,
                    "f_uint64": 18446744073709551615,
                    "f_float": 25.34,
                    "f_double": 25.34,
                    "f_string": "hello-neoedgex",
                    "f_raw": bytes([0x01, 0x02, 0xFE, 0xFF]),
                    "f_overflow": 70000,
                    "f_round": round_no,
                }
                try:
                    ctx.publish("output1", data)
                except Exception as exc:
                    ctx.report_error(neoedgex.CodeProcessError, exc)
            elif msg.handle == "input1":
                decoded = msg.to_dict()
                dump = {
                    "source": msg.source,
                    "timestamp": msg.timestamp,
                    "handle": msg.handle,
                    "fields": {
                        key: {"type": type(value).__name__, "value": _fmt(value)}
                        for key, value in decoded.items()
                    },
                }
                info = self._results.publish(RESULTS_TOPIC, json.dumps(dump), qos=1)
                info.wait_for_publish(timeout=5.0)


def _connect_results_client() -> mqtt.Client:
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="itest-results-py")
    client.reconnect_delay_set(min_delay=1, max_delay=2)
    connected = threading.Event()
    client.on_connect = lambda *_args, **_kwargs: connected.set()
    while True:
        try:
            client.connect(BROKER, 1883, keepalive=30)
            break
        except OSError:
            time.sleep(1.0)
    client.loop_start()
    connected.wait(timeout=30.0)
    return client


def main() -> None:
    results = _connect_results_client()
    neoedgex.new(BatteryHandler(results)).run()


if __name__ == "__main__":
    main()
