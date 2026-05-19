"""Tests for cga.config — the injected-config foundation (v1.0 task T1)."""

from pathlib import Path

import pytest

from cga.config import Config


def test_from_env_defaults(tmp_path, monkeypatch):
    monkeypatch.delenv("CGA_FALKORDB_SOCKET", raising=False)
    monkeypatch.delenv("CGA_FALKORDB_DB", raising=False)
    monkeypatch.delenv("CGA_INDEX_IGNORE", raising=False)
    cfg = Config.from_env(data_dir=tmp_path)
    assert cfg.data_dir == tmp_path
    assert cfg.falkordb_socket_path == tmp_path / "falkordb.sock"
    assert cfg.falkordb_db_path == tmp_path / "falkordb" / "cga.db"
    assert cfg.index_ignore == ()


def test_from_env_overrides(tmp_path, monkeypatch):
    monkeypatch.setenv("CGA_FALKORDB_SOCKET", "/tmp/custom.sock")
    monkeypatch.setenv("CGA_INDEX_IGNORE", "*.min.js, vendor/ ,")
    cfg = Config.from_env(data_dir=tmp_path)
    assert cfg.falkordb_socket_path == Path("/tmp/custom.sock")
    assert cfg.index_ignore == ("*.min.js", "vendor/")


def test_config_is_frozen():
    cfg = Config.from_env(data_dir=Path("/tmp/cga-test"))
    with pytest.raises(Exception, match="(?i)frozen|cannot assign"):
        cfg.data_dir = Path("/other")  # type: ignore[misc]


def test_ensure_dirs(tmp_path):
    cfg = Config.from_env(data_dir=tmp_path / "nested" / "cga")
    cfg.ensure_dirs()
    assert cfg.data_dir.is_dir()
    assert cfg.falkordb_db_path.parent.is_dir()
