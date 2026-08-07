from __future__ import annotations

from pathlib import Path

import neoedgex
from example_app import ExampleApp
from example_app import app as app_module
from neoedgex.testutil import UNDECLARED, MockNodeEnv, PublishedMessage, new_message

MOCK_CONFIG_PATH = (
    Path(__file__).resolve().parents[1] / "cmd" / "mock_neoedgex" / "mock-config.json"
)


def _node_env() -> MockNodeEnv:
    config = neoedgex.load_mock_config(MOCK_CONFIG_PATH)
    return MockNodeEnv(config=config.nodes[0])


def test_example_app_reports_missing_endpoint(monkeypatch) -> None:
    monkeypatch.delenv("HTTP_ENDPOINT", raising=False)
    ctx = _node_env()

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
    ctx = _node_env()
    ctx.message_iterable = [
        ctx.new_message("input1", {"temperature": 25}),
        ctx.new_message("input2", {"running": True}),
        ctx.new_message("input3", {"message": "hello"}),
    ]

    ExampleApp().handle(ctx)

    assert ctx.reported_errors == []
    assert calls == [
        ("https://api.example.com/temperature", b'{"value": 25}'),
        ("https://api.example.com/status", b'{"running": true}'),
        ("https://api.example.com/event", b'{"message": "hello"}'),
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
            data={"api_path": "/event", "response_status": 201},
        ),
    ]


def test_example_app_ignores_unknown_handle(monkeypatch) -> None:
    def fail_post(url: str, body: bytes) -> int:
        raise AssertionError(f"unexpected request to {url}")

    monkeypatch.setattr(app_module, "_post", fail_post)
    monkeypatch.setenv("HTTP_ENDPOINT", "https://api.example.com")
    ctx = _node_env()
    ctx.message_iterable = [
        new_message("input4", {"foo": ("bar", UNDECLARED)}),
    ]

    ExampleApp().handle(ctx)

    assert ctx.reported_errors == []
    assert ctx.published_data == []
