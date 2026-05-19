from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Iterable, Protocol, runtime_checkable

from neoedgex.contract import ErrorCode, Logger, Message, Node, PortFieldData
from neoedgex.mock import MockConfig, load_config

from ._internal.node import NodeInstance
from ._internal.sdk import SDK


@runtime_checkable
class NodeEnv(Protocol):
    def node_config(self) -> Node: ...
    def messages(self) -> Iterable[Message]: ...
    def context(self) -> threading.Event: ...
    def logger(self) -> Logger: ...
    def publish(self, handle: str, data: dict[str, Any]) -> None: ...
    def report_error(self, code: ErrorCode, err: BaseException | None) -> None: ...
    def stop(self) -> None: ...


@runtime_checkable
class NodeHandler(Protocol):
    def handle(self, ctx: NodeEnv) -> None: ...


class App:
    def __init__(self, handler: NodeHandler) -> None:
        if handler is None:
            raise TypeError("handler must not be None")
        self._handler = handler
        self._mock_config: MockConfig | None = None
        self._disable_sdk_log = False

    def enable_mock(self, config: MockConfig) -> "App":
        self._mock_config = config
        return self

    def disable_sdk_log(self) -> "App":
        self._disable_sdk_log = True
        return self

    def run(self) -> None:
        sdk = SDK(log_enabled=not self._disable_sdk_log)
        if self._mock_config is not None:
            sdk.enable_mock(self._mock_config)
        sdk.initialize()
        sdk.start_message_injection()

        threads: list[threading.Thread] = []
        instances: list[NodeInstance] = []
        logger = sdk.new_logger("App")

        # Handler threads are spawned only after the messenger is connected:
        # handlers may publish immediately on startup (e.g. via report_error
        # from an early return), so the connection must be fully established
        # first. A failed connect skips this callback entirely and no handler
        # threads are started.
        def on_connected() -> None:
            for node_config in sdk.node_configs():
                try:
                    instance = NodeInstance(sdk, node_config)
                except Exception as exc:
                    logger.warn("Skipping node %s: %s", node_config.data.name, exc)
                    continue
                instances.append(instance)
                thread = threading.Thread(
                    target=instance.run,
                    args=(lambda instance=instance: self._handler.handle(instance),),
                    daemon=False,
                )
                thread.start()
                threads.append(thread)

        run_error: BaseException | None = None
        try:
            sdk.run(on_connected)
        except BaseException as exc:
            run_error = exc
            sdk.shutdown()
            for instance in instances:
                instance.shutdown()
        finally:
            for thread in threads:
                thread.join()

        if run_error is not None:
            raise run_error


def new(handler: NodeHandler) -> App:
    return App(handler)


def load_mock_config(path: str | Path) -> MockConfig:
    return load_config(path)


PortField = PortFieldData
CodeInitializationError = ErrorCode.INITIALIZATION_ERROR
CodeNetworkError = ErrorCode.NETWORK_ERROR
CodeProcessError = ErrorCode.PROCESS_ERROR
