from __future__ import annotations

import neoedgex
from example_app import ExampleApp
from example_app import app as app_module
from neoedgex.testutil import MockNodeEnv


def test_example_app_reports_missing_endpoint(monkeypatch) -> None:
    monkeypatch.delenv("HTTP_ENDPOINT", raising=False)
    ctx = MockNodeEnv()

    ExampleApp().handle(ctx)

    assert len(ctx.reported_errors) == 1
    assert ctx.reported_errors[0].code == neoedgex.CodeProcessError
    assert ctx.published_data == []


def test_example_app_publishes_temperature(monkeypatch) -> None:
    calls: list[tuple[str, int]] = []

    def fake_post_temperature(http_endpoint: str, temperature: int) -> int:
        calls.append((http_endpoint, temperature))
        return 201

    monkeypatch.setattr(app_module, "_post_temperature", fake_post_temperature)
    monkeypatch.setenv("HTTP_ENDPOINT", "https://api.example.com/ingest")
    ctx = MockNodeEnv(
        message_iterable=[
            neoedgex.Message(
                handle="input1",
                data={"temperature": 25},
                source="source-node",
            )
        ]
    )

    ExampleApp().handle(ctx)

    assert calls == [("https://api.example.com/ingest", 25)]
    assert ctx.reported_errors == []
    assert ctx.published_data == [{"temperature": 25, "response_status": 201}]
