from __future__ import annotations

import json
import os
import urllib.request
from typing import Any

import neoedgex


class ExampleApp:
    def handle(self, ctx: neoedgex.NodeEnv) -> None:
        http_endpoint = os.getenv("HTTP_ENDPOINT", "")
        if not http_endpoint:
            ctx.report_error(
                neoedgex.CodeProcessError,
                RuntimeError("HTTP_ENDPOINT environment variable is not set"),
            )
            return

        for msg in ctx.messages():
            if msg.handle != "input1":
                continue

            temperature = _read_temperature(ctx, msg.data)
            if temperature is None:
                continue

            if ctx.context().is_set():
                break

            try:
                response_status = _post_temperature(http_endpoint, temperature)
            except Exception as exc:
                ctx.report_error(
                    neoedgex.CodeProcessError,
                    RuntimeError(f"failed to POST to HTTP_ENDPOINT {http_endpoint}: {exc}"),
                )
                continue
            try:
                ctx.publish(
                    {
                        "temperature": temperature,
                        "response_status": response_status,
                    }
                )
            except Exception as exc:
                ctx.report_error(neoedgex.CodeProcessError, exc)

def _read_temperature(ctx: neoedgex.NodeEnv, data: dict[str, Any]) -> int | None:
    if "temperature" not in data:
        ctx.report_error(
            neoedgex.CodeProcessError,
            RuntimeError("temperature is not defined in input schema"),
        )
        return None

    value = data["temperature"]
    if value is None:
        ctx.report_error(neoedgex.CodeProcessError, RuntimeError("no temperature value in message"))
        return None
    if not isinstance(value, int):
        ctx.report_error(
            neoedgex.CodeProcessError,
            RuntimeError("temperature is not defined as int in input schema"),
        )
        return None
    return value


def _post_temperature(http_endpoint: str, temperature: int) -> int:
    payload = json.dumps({"number": temperature}).encode("utf-8")
    request = urllib.request.Request(
        http_endpoint,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return response.status
