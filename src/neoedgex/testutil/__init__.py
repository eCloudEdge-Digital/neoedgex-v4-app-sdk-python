from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any, Iterable

from neoedgex.contract import ErrorCode, Logger, Message, Node


class NoopLogger:
    def __init__(self, tag: str = "test") -> None:
        self._tag = tag

    def tag(self) -> str:
        return self._tag

    def debug(self, _msg: str, *_args: Any) -> None:
        return None

    def info(self, _msg: str, *_args: Any) -> None:
        return None

    def warn(self, _msg: str, *_args: Any) -> None:
        return None

    def error(self, _msg: str, *_args: Any) -> None:
        return None


@dataclass(slots=True)
class PublishedMessage:
    handle: str
    data: dict[str, Any]


@dataclass(slots=True)
class ReportedError:
    code: ErrorCode
    err: BaseException | None


class MockNodeEnv:
    def __init__(
        self,
        *,
        config: Node | None = None,
        message_iterable: Iterable[Message] | None = None,
        done_event: threading.Event | None = None,
        mock_logger: Logger | None = None,
        publish_error: BaseException | None = None,
    ) -> None:
        self.config = config or Node()
        self.message_iterable = message_iterable or ()
        self.done_event = done_event or threading.Event()
        self.mock_logger = mock_logger or NoopLogger()
        self.publish_error = publish_error

        self.published_data: list[PublishedMessage] = []
        self.reported_errors: list[ReportedError] = []
        self.stop_called = False

    def node_config(self) -> Node:
        return self.config

    def messages(self) -> Iterable[Message]:
        return self.message_iterable

    def context(self) -> threading.Event:
        return self.done_event

    def logger(self) -> Logger:
        return self.mock_logger

    def publish(self, handle: str, data: dict[str, Any]) -> None:
        self.published_data.append(PublishedMessage(handle=handle, data=data))
        if self.publish_error is not None:
            raise self.publish_error

    def report_error(self, code: ErrorCode, err: BaseException | None) -> None:
        self.reported_errors.append(ReportedError(code=code, err=err))

    def stop(self) -> None:
        self.stop_called = True
        self.done_event.set()


__all__ = ["MockNodeEnv", "NoopLogger", "PublishedMessage", "ReportedError"]
