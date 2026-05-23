import asyncio
import shutil
from pathlib import Path
from uuid import uuid4

import pytest

from cga.config import Config
from cga.db.database import FalkorDBManager
from cga.engine.graph_builder import GraphBuilder
from cga.engine.index_coordinator import IncrementalIndexCoordinator
from cga.jobs import JobManager


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


def test_warm_coordinator_matches_post_edit_cold_index(tmp_path: Path) -> None:
    config = Config(
        data_dir=tmp_path,
        falkordb_host="127.0.0.1",
        falkordb_port=6379,
        index_ignore=("__pycache__", ".git"),
    )
    config.ensure_dirs()

    repo = (Path("/tmp") / f"cga_index_coordinator_{uuid4().hex}").resolve()
    graph_a = f"test_index_coordinator_a_{uuid4().hex}"
    graph_b = f"test_index_coordinator_b_{uuid4().hex}"
    manager_a = _manager_or_skip(config, graph_a)
    manager_b = _manager_or_skip(config, graph_b)

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
        # Unsupported file -- cold creates a minimal File node for it;
        # warm refresh must preserve that behavior on mutation.
        (repo / "README.md").write_text("# original\n")

        builder_a = GraphBuilder(config, manager_a, JobManager())
        builder_b = GraphBuilder(config, manager_b, JobManager())

        asyncio.run(builder_a.build_graph_from_path_async(repo))
        assert _counts(manager_a)["CALLS"] == 2

        coordinator = IncrementalIndexCoordinator(builder_a, repo)
        file_a = (repo / "file_a.py").resolve()
        with file_a.open("a") as f:
            f.write("\ndef added():\n    return 2\n")
        readme = (repo / "README.md").resolve()
        readme.write_text("# updated\n")

        coordinator.refresh_warm({str(file_a), str(readme)})
        asyncio.run(builder_b.build_graph_from_path_async(repo))

        counts_a = _counts(manager_a)
        counts_b = _counts(manager_b)
        assert counts_a == counts_b
    finally:
        for manager in (manager_a, manager_b):
            if manager._graph is not None:
                manager._graph.delete()
            manager.close_driver()
        shutil.rmtree(repo, ignore_errors=True)
