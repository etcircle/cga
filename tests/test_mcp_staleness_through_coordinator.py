import asyncio
import shutil
from pathlib import Path
from uuid import uuid4

import pytest

from cga.config import Config
from cga.db.database import FalkorDBManager
from cga.engine.graph_builder import GraphBuilder
from cga.jobs import JobManager
from cga.mcp.server import CGAMCPServer


def _manager_or_skip(config: Config, graph_name: str) -> FalkorDBManager:
    manager = FalkorDBManager(config=config, graph_name=graph_name)
    try:
        manager.get_driver()
    except Exception as exc:
        message = str(exc).lower()
        if (
            "connection refused" in message
            or "error 61" in message
            or "operation not permitted" in message
            or "error 1 connecting" in message
        ):
            pytest.skip("FalkorDB server unreachable at 127.0.0.1:6379")
        raise
    return manager


def _counts(manager: FalkorDBManager) -> dict[str, int]:
    driver = manager.get_driver()
    queries = {
        "File": "MATCH (f:File) RETURN count(f) AS c",
        "Function": "MATCH (fn:Function) RETURN count(fn) AS c",
        "CALLS": "MATCH ()-[r:CALLS]->() RETURN count(r) AS c",
        "INHERITS": "MATCH ()-[r:INHERITS]->() RETURN count(r) AS c",
    }
    with driver.session() as session:
        return {
            name: session.run(query).single()["c"]
            for name, query in queries.items()
        }


def test_mcp_staleness_floor_refresh_matches_post_edit_cold_index(tmp_path: Path) -> None:
    config = Config(
        data_dir=tmp_path,
        falkordb_host="127.0.0.1",
        falkordb_port=6379,
        index_ignore=("__pycache__", ".git"),
    )
    config.ensure_dirs()

    repo = (Path("/tmp") / f"cga_mcp_staleness_{uuid4().hex}").resolve()
    graph_a = f"test_mcp_floor_a_{uuid4().hex}"
    graph_b = f"test_mcp_floor_b_{uuid4().hex}"
    probe = _manager_or_skip(config, graph_a)
    probe.close_driver()

    mcp: CGAMCPServer | None = None
    manager_b: FalkorDBManager | None = None
    try:
        repo.mkdir(parents=True)
        (repo / ".cgcignore").write_text("")
        (repo / "file_a.py").write_text(
            "def hub():\n"
            "    return 1\n"
            "\n"
            "def helper():\n"
            "    return hub()\n"
        )
        (repo / "file_b.py").write_text(
            "from file_a import hub\n"
            "\n"
            "def caller():\n"
            "    return hub()\n"
        )
        (repo / "README.md").write_text("# original\n")

        mcp = CGAMCPServer(repo_root=repo, config=config, graph_name=graph_a)
        asyncio.run(mcp.graph_builder.build_graph_from_path_async(repo))

        mcp._write_mtime_sidecar(mcp._scan_source_mtimes())
        before_counts = _counts(mcp.db_manager)
        assert mcp._refresh_stale_files() is None
        assert _counts(mcp.db_manager) == before_counts

        with (repo / "file_a.py").open("a") as f:
            f.write("\ndef added():\n    pass\n")
        (repo / "README.md").write_text("# updated\n")

        assert mcp._refresh_stale_files() is None

        manager_b = FalkorDBManager(config=config, graph_name=graph_b)
        builder_b = GraphBuilder(config, manager_b, JobManager())
        asyncio.run(builder_b.build_graph_from_path_async(repo))

        counts_a = _counts(mcp.db_manager)
        counts_b = _counts(manager_b)
        assert counts_a == counts_b
    finally:
        if mcp is not None:
            if mcp.db_manager._graph is not None:
                mcp.db_manager._graph.delete()
            mcp.db_manager.close_driver()
        if manager_b is not None:
            if manager_b._graph is not None:
                manager_b._graph.delete()
            manager_b.close_driver()
        shutil.rmtree(repo, ignore_errors=True)
