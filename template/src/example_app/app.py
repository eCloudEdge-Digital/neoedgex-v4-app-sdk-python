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
            # 範例：依 msg.handle 將訊息分派到對應的 input 處理流程，
            # 各自準備不同的 API path 與 request body。
            prepared = _prepare_request(ctx, msg)
            if prepared is None:
                continue
            api_path, request_body = prepared

            if ctx.context().is_set():
                break

            try:
                response_status = _post(http_endpoint + api_path, request_body)
            except Exception as exc:
                ctx.report_error(
                    neoedgex.CodeProcessError,
                    RuntimeError(f"failed to POST {api_path}: {exc}"),
                )
                continue

            try:
                ctx.publish(
                    "output1",
                    {
                        "api_path": api_path,
                        "response_status": response_status,
                    },
                )
            except Exception as exc:
                ctx.report_error(neoedgex.CodeProcessError, exc)


def _prepare_request(
    ctx: neoedgex.NodeEnv, msg: neoedgex.Message
) -> tuple[str, bytes] | None:
    if msg.handle == "input1":
        # 範例：input1 攜帶 temperature (float)
        value = _read_typed_field(ctx, msg.handle, msg.data, "temperature", float)
        if value is None:
            return None
        return "/temperature", json.dumps({"value": value}).encode("utf-8")

    if msg.handle == "input2":
        # 範例：input2 攜帶 running (bool)
        value = _read_typed_field(ctx, msg.handle, msg.data, "running", bool)
        if value is None:
            return None
        return "/status", json.dumps({"running": value}).encode("utf-8")

    if msg.handle == "input3":
        # 範例：input3 攜帶 payload (format=json)，handler 拿到的是 raw JSON 字串，
        # 由 app 自行決定怎麼 unmarshal。
        raw_payload = _read_typed_field(ctx, msg.handle, msg.data, "payload", str)
        if raw_payload is None:
            return None
        try:
            payload = json.loads(raw_payload)
        except ValueError as exc:
            ctx.report_error(
                neoedgex.CodeProcessError,
                RuntimeError(f"payload is not a valid JSON object: {exc}"),
            )
            return None
        if not isinstance(payload, dict):
            ctx.report_error(
                neoedgex.CodeProcessError,
                RuntimeError("payload is not a JSON object"),
            )
            return None
        # 範例：取出 payload.id (int)
        id_value = payload.get("id")
        if not isinstance(id_value, int) or isinstance(id_value, bool):
            ctx.report_error(
                neoedgex.CodeProcessError,
                RuntimeError(f"payload 'id' is not an int, got {type(id_value).__name__}"),
            )
            return None
        # body 直接 passthrough raw payload；path 帶上 id
        return f"/payload/{id_value}", raw_payload.encode("utf-8")

    # 未在 schema 中定義的 handle，忽略即可
    return None


def _read_typed_field(
    ctx: neoedgex.NodeEnv,
    handle: str,
    data: dict[str, Any],
    key: str,
    expected_type: type,
) -> Any:
    if key not in data:
        ctx.report_error(
            neoedgex.CodeProcessError,
            RuntimeError(f"{key} is not defined in {handle} schema"),
        )
        return None
    value = data[key]
    if value is None:
        ctx.report_error(
            neoedgex.CodeProcessError,
            RuntimeError(f"no {key} value in {handle} message"),
        )
        return None
    # bool is a subclass of int — guard against accepting True/False as int.
    if expected_type is int and isinstance(value, bool):
        ctx.report_error(
            neoedgex.CodeProcessError,
            RuntimeError(f"{key} is not defined as int in {handle} schema"),
        )
        return None
    if not isinstance(value, expected_type):
        ctx.report_error(
            neoedgex.CodeProcessError,
            RuntimeError(
                f"{key} is not defined as {expected_type.__name__} in {handle} schema"
            ),
        )
        return None
    return value


def _post(url: str, body: bytes) -> int:
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return response.status
