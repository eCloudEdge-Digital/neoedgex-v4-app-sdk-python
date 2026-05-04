from __future__ import annotations

import json
import re
import signal
import threading
import time
from pathlib import Path
from typing import Any, Callable

from neoedgex.contract import Logger, MessengerConfig, MessengerOptions, Node
from neoedgex.mock import MockConfig

from .logger import NoopLogger, SDKLogger
from .messenger import _QUEUE_CLOSED, MQTTMessenger
from .mock_messenger import MockMessenger

DEFAULT_MOUNT_PATH = Path("/opt/neoedgex")


class SDK:
    def __init__(self, log_enabled: bool = True) -> None:
        self._log_enabled = log_enabled
        self._node_configs: list[Node] = []
        self._messenger: Any = None
        self._logger = self.new_logger("SDK")
        self._shutdown_event = threading.Event()
        self._running_lock = threading.Lock()
        self._is_running = False
        self._signal_handlers: dict[int, Any] = {}
        self._mock_messages: list[Any] = []
        self._mock_interval: float | None = None

    def initialize(self, mount_path: Path = DEFAULT_MOUNT_PATH) -> None:
        self._register_signal_handlers()
        if self._messenger is not None:
            return

        config_path = mount_path / "config" / "config.json"
        messenger_path = mount_path / "config" / "messenger.json"

        try:
            node_payload = json.loads(config_path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise RuntimeError(f"failed to read config file: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"failed to unmarshal config file: {exc}") from exc

        try:
            messenger_payload = json.loads(messenger_path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise RuntimeError(f"failed to read messenger config file: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"failed to unmarshal messenger config file: {exc}") from exc

        self._node_configs = [Node.from_dict(item) for item in node_payload]
        messenger_config = MessengerConfig.from_dict(messenger_payload)
        self._messenger = MQTTMessenger(self, MessengerOptions(config=messenger_config))

    def enable_mock(self, config: MockConfig) -> None:
        self._node_configs = list(config.nodes)
        self._messenger = MockMessenger(self.new_logger("Mock"))
        self._mock_messages = list(config.mock.messages)
        self._mock_interval = _parse_duration_seconds(config.mock.message_interval)
        self._logger.info(
            "[MOCK] Mock mode enabled with %s node(s), %s mock message(s)",
            len(config.nodes),
            len(config.mock.messages),
        )

    def start_message_injection(self) -> None:
        if not isinstance(self._messenger, MockMessenger) or not self._mock_messages:
            return
        interval = self._mock_interval if self._mock_interval and self._mock_interval > 0 else 3.0
        thread = threading.Thread(target=self._inject_messages, args=(interval,), daemon=True)
        thread.start()

    def shutdown_event(self) -> threading.Event:
        return self._shutdown_event

    def node_configs(self) -> list[Node]:
        return list(self._node_configs)

    def messenger(self) -> Any:
        return self._messenger

    def new_logger(self, tag: str) -> Logger:
        if not self._log_enabled:
            return NoopLogger(tag)
        return SDKLogger(tag)

    def run(self, on_connected: Callable[[], None] | None = None) -> None:
        """Connect the messenger, invoke ``on_connected`` once the connection is
        established, then block until shutdown is requested.

        The messenger must be connected before the callback fires; this lets
        the callback spawn handler threads that may publish immediately. A
        connect failure propagates to the caller and the callback is never
        invoked. Connect is a single attempt — retries belong inside the
        messenger implementation.
        """
        # Only the goroutine that actually acquires the running flag is
        # allowed to clear it; otherwise a re-entrant ``run()`` call would
        # reset the flag from under the legitimate runner.
        acquired_running = False
        with self._running_lock:
            if self._is_running:
                raise RuntimeError("sdk is already running")
            self._is_running = True
            acquired_running = True
        connected = False
        try:
            self._messenger.connect()
            connected = True

            if on_connected is not None:
                on_connected()

            self._shutdown_event.wait()
            self._logger.info("Context done, exiting run loop")
        finally:
            try:
                if connected and self._messenger is not None:
                    self._messenger.disconnect()
            finally:
                if acquired_running:
                    with self._running_lock:
                        self._is_running = False
                self._restore_signal_handlers()

    def shutdown(self) -> None:
        self._shutdown_event.set()

    def queue_closed_sentinel(self) -> object:
        return _QUEUE_CLOSED

    def _inject_messages(self, interval: float) -> None:
        if not isinstance(self._messenger, MockMessenger):
            return
        if self._shutdown_event.wait(0.5):
            return
        index = 0
        while not self._shutdown_event.is_set():
            if self._shutdown_event.wait(interval):
                return
            message = self._mock_messages[index]
            self._logger.info("[MOCK INJECT] -> node=%s handle=%s", message.node_id, message.handle)
            try:
                self._messenger.inject_neoflow_message(message.node_id, message.handle, message.data)
            except Exception as exc:
                self._logger.warn("[MOCK INJECT] error: %s", exc)
            index = (index + 1) % len(self._mock_messages)

    def _register_signal_handlers(self) -> None:
        if threading.current_thread() is not threading.main_thread():
            return
        # SIGTERM / SIGINT exist on every supported platform.
        # SIGQUIT is POSIX-only (Linux/macOS); SIGBREAK is Windows-only.
        for name in ("SIGTERM", "SIGINT", "SIGQUIT", "SIGBREAK"):
            signum = getattr(signal, name, None)
            if signum is None or signum in self._signal_handlers:
                continue
            try:
                self._signal_handlers[signum] = signal.getsignal(signum)
                signal.signal(signum, self._handle_signal)
            except (OSError, ValueError):
                # Some signals can't be installed on certain platforms or
                # interpreter modes (e.g. embedded, Windows service host).
                pass

    def _handle_signal(self, signum: int, _frame: Any) -> None:
        # First signal triggers a graceful shutdown and restores the previous
        # handler immediately, so a second signal (e.g. impatient user hitting
        # Ctrl-C again) hits the default handler and force-exits the process.
        try:
            previous = self._signal_handlers.pop(signum, None)
            if previous is not None:
                signal.signal(signum, previous)
        except (ValueError, OSError):
            pass
        self.shutdown()

    def _restore_signal_handlers(self) -> None:
        if threading.current_thread() is not threading.main_thread():
            return
        for signum, previous in list(self._signal_handlers.items()):
            try:
                signal.signal(signum, previous)
            except (ValueError, OSError):
                pass
        self._signal_handlers.clear()


_DURATION_PATTERN = re.compile(r"^(-?\d+(?:\.\d+)?)(ns|us|µs|ms|s|m|h)$")
_DURATION_UNIT_SECONDS = {
    "ns": 1e-9,
    "us": 1e-6,
    "µs": 1e-6,
    "ms": 1e-3,
    "s": 1.0,
    "m": 60.0,
    "h": 3600.0,
}


def _parse_duration_seconds(raw: str) -> float | None:
    # Accepts a single magnitude+unit token (e.g. "1.5s", "0.5m", "250ms").
    # Returns None for empty / unparseable input so callers can fall back to
    # their own defaults.
    if not raw:
        return None
    match = _DURATION_PATTERN.match(raw)
    if match is None:
        return None
    try:
        value = float(match.group(1))
    except ValueError:
        return None
    return value * _DURATION_UNIT_SECONDS[match.group(2)]
