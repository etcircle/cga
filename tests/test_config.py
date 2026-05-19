"""Tests for cga.config — the injected-config foundation (v1.0 task T1)."""

from pathlib import Path

import pytest

from cga.config import Config


def test_from_env_defaults(tmp_path, monkeypatch):
    monkeypatch.delenv("CGA_FALKORDB_HOST", raising=False)
    monkeypatch.delenv("CGA_FALKORDB_PORT", raising=False)
    monkeypatch.delenv("CGA_INDEX_IGNORE", raising=False)
    monkeypatch.delenv("CGA_INDEX_SOURCE", raising=False)
    monkeypatch.delenv("CGA_SKIP_EXTERNAL_RESOLUTION", raising=False)
    cfg = Config.from_env(data_dir=tmp_path)
    assert cfg.data_dir == tmp_path
    assert cfg.falkordb_host == "127.0.0.1"
    assert cfg.falkordb_port == 6379
    assert cfg.index_ignore == ()
    assert cfg.index_source is False
    assert cfg.skip_external_resolution is False


def test_from_env_overrides(tmp_path, monkeypatch):
    monkeypatch.setenv("CGA_FALKORDB_HOST", "10.0.0.5")
    monkeypatch.setenv("CGA_FALKORDB_PORT", "6380")
    monkeypatch.setenv("CGA_INDEX_IGNORE", "*.min.js, vendor/ ,")
    monkeypatch.setenv("CGA_INDEX_SOURCE", "true")
    monkeypatch.setenv("CGA_SKIP_EXTERNAL_RESOLUTION", "true")
    cfg = Config.from_env(data_dir=tmp_path)
    assert cfg.falkordb_host == "10.0.0.5"
    assert cfg.falkordb_port == 6380
    assert cfg.index_ignore == ("*.min.js", "vendor/")
    assert cfg.index_source is True
    assert cfg.skip_external_resolution is True


def test_config_is_frozen():
    cfg = Config.from_env(data_dir=Path("/tmp/cga-test"))
    with pytest.raises(Exception, match="(?i)frozen|cannot assign"):
        cfg.data_dir = Path("/other")  # type: ignore[misc]


def test_ensure_dirs(tmp_path):
    cfg = Config.from_env(data_dir=tmp_path / "nested" / "cga")
    cfg.ensure_dirs()
    assert cfg.data_dir.is_dir()
