from __future__ import annotations

from pathlib import Path

import neoedgex
from example_app import ExampleApp


if __name__ == "__main__":
    config = neoedgex.load_mock_config(Path(__file__).with_name("mock-config.json"))
    neoedgex.new(ExampleApp()).enable_mock(config).run()
