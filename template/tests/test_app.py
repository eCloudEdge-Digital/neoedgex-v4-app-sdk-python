from __future__ import annotations

import neoedgex
from example_app import ExampleApp
from example_app import app as app_module
from neoedgex.testutil import MockNodeEnv, PublishedMessage


# 2**53 + 1 — a value that would lose precision if the handler decoded
# the raw JSON payload through a path that round-tripped through float64.
BIG_ID = (1 << 53) + 1


def test_example_app_reports_missing_endpoint(monkeypatch) -> None:
    monkeypatch.delenv("HTTP_ENDPOINT", raising=False)
    ctx = MockNodeEnv()

    ExampleApp().handle(ctx)

    assert len(ctx.reported_errors) == 1
    assert ctx.reported_errors[0].code == neoedgex.CodeProcessError
    assert ctx.published_data == []


def test_example_app_routes_each_input_to_its_own_path(monkeypatch) -> None:
    calls: list[tuple[str, bytes]] = []

    def fake_post(url: str, body: bytes) -> int:
        calls.append((url, body))
        return 201

    monkeypatch.setattr(app_module, "_post", fake_post)
    monkeypatch.setenv("HTTP_ENDPOINT", "https://api.example.com")
    ctx = MockNodeEnv(
        message_iterable=[
            neoedgex.Message(handle="input1", data={"temperature": 25.5}, source="upstream"),
            neoedgex.Message(handle="input2", data={"running": True}, source="upstream"),
            # SDK delivers format=json fields as raw JSON strings; the
            # handler decides how to unmarshal them.
            neoedgex.Message(
                handle="input3",
                data={"payload": f'{{"id":{BIG_ID},"label":"demo"}}'},
                source="upstream",
            ),
        ]
    )

    ExampleApp().handle(ctx)

    assert ctx.reported_errors == []
    assert calls == [
        ("https://api.example.com/temperature", b'{"value": 25.5}'),
        ("https://api.example.com/status", b'{"running": true}'),
        # path encodes the int id; body is the raw JSON payload passed through.
        (
            f"https://api.example.com/payload/{BIG_ID}",
            f'{{"id":{BIG_ID},"label":"demo"}}'.encode("utf-8"),
        ),
    ]
    assert ctx.published_data == [
        PublishedMessage(
            handle="output1",
            data={"api_path": "/temperature", "response_status": 201},
        ),
        PublishedMessage(
            handle="output1",
            data={"api_path": "/status", "response_status": 201},
        ),
        PublishedMessage(
            handle="output1",
            data={"api_path": f"/payload/{BIG_ID}", "response_status": 201},
        ),
    ]


def test_example_app_ignores_unknown_handle(monkeypatch) -> None:
    def fail_post(url: str, body: bytes) -> int:
        raise AssertionError(f"unexpected request to {url}")

    monkeypatch.setattr(app_module, "_post", fail_post)
    monkeypatch.setenv("HTTP_ENDPOINT", "https://api.example.com")
    ctx = MockNodeEnv(
        message_iterable=[
            neoedgex.Message(handle="input999", data={"foo": "bar"}, source="upstream"),
        ]
    )

    ExampleApp().handle(ctx)

    assert ctx.reported_errors == []
    assert ctx.published_data == []
