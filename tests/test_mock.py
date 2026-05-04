from pathlib import Path

import pytest

from neoedgex import load_mock_config
from neoedgex.mock import load_config


def test_load_config_valid() -> None:
    config = load_config(Path(__file__).parent / "testdata" / "mock-config.json")
    assert len(config.nodes) == 1
    assert config.nodes[0].id == "test-node-1"
    assert len(config.mock.messages) == 1
    assert config.mock.message_interval == "1s"


def test_top_level_load_mock_config() -> None:
    config = load_mock_config(Path(__file__).parent / "testdata" / "mock-config.json")
    assert config.nodes[0].id == "test-node-1"


def test_load_config_file_not_found() -> None:
    with pytest.raises(ValueError):
        load_config("nonexistent.json")


def test_load_config_empty_nodes(tmp_path: Path) -> None:
    path = tmp_path / "empty.json"
    path.write_text('{"nodes":[]}', encoding="utf-8")
    with pytest.raises(ValueError, match="nodes must not be empty"):
        load_config(path)
