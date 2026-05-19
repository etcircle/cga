import asyncio
import shutil
import time
from pathlib import Path
from uuid import uuid4

import pytest

from cga.config import Config
from cga.db.database import FalkorDBManager
from cga.engine.graph_builder import GraphBuilder
from cga.jobs import JobManager
from cga.mcp.server import CGAMCPServer

FIXTURE = Path(__file__).parent / "fixtures" / "sample_py"


def _config(tmp_path: Path) -> Config:
    return Config(
        data_dir=tmp_path,
        falkordb_host="127.0.0.1",
        falkordb_port=6379,
        index_ignore=("__pycache__", ".git"),
    )


def _manager_or_skip(config: Config, graph_name: str) -> FalkorDBManager:
    manager = FalkorDBManager(config=config, graph_name=graph_name)
    try:
        manager.get_driver()
    except Exception as exc:
        message = str(exc).lower()
        if "connection refused" in message or "error 61" in message:
            pytest.skip("FalkorDB server unreachable at 127.0.0.1:6379")
        raise
    return manager


@pytest.fixture
def coordinator(tmp_path: Path):
    config = _config(tmp_path)
    graph_name = f"test_mcp_{uuid4().hex}"
    manager = _manager_or_skip(config, graph_name)
    manager.close_driver()

    server = CGAMCPServer(repo_root=FIXTURE, config=config, graph_name=graph_name)
    try:
        yield server
    finally:
        if server.db_manager._graph is not None:
            server.db_manager._graph.delete()
        server.db_manager.close_driver()


def _index_fixture(server: CGAMCPServer) -> None:
    builder = GraphBuilder(server.config, server.db_manager, JobManager())
    asyncio.run(builder.build_graph_from_path_async(server.repo_root))
    server._write_mtime_sidecar(server._scan_source_mtimes())


def test_index_status_empty_and_indexed(coordinator: CGAMCPServer) -> None:
    assert coordinator.registry.dispatch("index_status", {}) == {"status": "empty"}

    _index_fixture(coordinator)

    result = coordinator.registry.dispatch("index_status", {})
    assert result["status"] == "indexed"
    assert result["files"] >= 4


def test_query_tools_return_results_through_registry(coordinator: CGAMCPServer) -> None:
    _index_fixture(coordinator)

    symbol = coordinator.registry.dispatch("find_symbol", {"name": "helper"})
    assert symbol["status"] == "ok"
    assert symbol["results"][0]["path"] == str((FIXTURE / "helper.py").resolve())

    callers = coordinator.registry.dispatch("find_callers", {"name": "helper"})
    assert callers["status"] == "ok"
    assert {item["caller"]["name"] for item in callers["results"]} == {"call_helper", "extra"}

    references = coordinator.registry.dispatch("find_references", {"name": "Base"})
    assert references["status"] == "ok"
    assert any(item["ref_kind"] == "inherits" for item in references["results"])


def test_cold_index_async_poll_then_reissue(coordinator: CGAMCPServer) -> None:
    first = coordinator.registry.dispatch("find_symbol", {"name": "helper"})
    assert first["status"] == "indexing"
    assert first["job_id"]

    deadline = time.time() + 20
    status = coordinator.registry.dispatch("index_status", {})
    while status["status"] == "indexing" and time.time() < deadline:
        time.sleep(0.1)
        status = coordinator.registry.dispatch("index_status", {})

    assert status["status"] == "indexed"
    result = coordinator.registry.dispatch("find_symbol", {"name": "helper"})
    assert result["status"] == "ok"
    assert result["results"][0]["name"] == "helper"


def test_staleness_refreshes_changed_file(tmp_path: Path) -> None:
    repo_copy = tmp_path / "sample_copy"
    shutil.copytree(FIXTURE, repo_copy)
    config = _config(tmp_path / "data")
    graph_name = f"test_mcp_stale_{uuid4().hex}"
    manager = _manager_or_skip(config, graph_name)
    manager.close_driver()

    server = CGAMCPServer(repo_root=repo_copy, config=config, graph_name=graph_name)
    try:
        _index_fixture(server)
        assert server.registry.dispatch("find_symbol", {"name": "brand_new"})["status"] == "empty"

        helper = repo_copy / "helper.py"
        helper.write_text(
            helper.read_text(encoding="utf-8")
            + "\n\ndef brand_new(value):\n    return helper(value)\n",
            encoding="utf-8",
        )

        result = server.registry.dispatch("find_symbol", {"name": "brand_new"})
        assert result["status"] == "ok"
        assert result["results"][0]["path"] == str(helper.resolve())
    finally:
        if server.db_manager._graph is not None:
            server.db_manager._graph.delete()
        server.db_manager.close_driver()
