import asyncio
import shutil
from pathlib import Path
from uuid import uuid4

import pytest

from cga.config import Config
from cga.db.database import FalkorDBManager
from cga.engine.graph_builder import GraphBuilder
from cga.engine.watcher import RepositoryEventHandler
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


def test_watcher_threshold_fallback_matches_post_edit_cold_index(tmp_path: Path) -> None:
    config = Config(
        data_dir=tmp_path,
        falkordb_host="127.0.0.1",
        falkordb_port=6379,
        index_ignore=("__pycache__", ".git"),
    )
    config.ensure_dirs()

    repo = (Path("/tmp") / f"cga_watcher_warm_fallback_{uuid4().hex}").resolve()
    graph_a = f"test_watcher_warm_a_{uuid4().hex}"
    graph_b = f"test_watcher_warm_b_{uuid4().hex}"
    manager_a: FalkorDBManager | None = None
    manager_b: FalkorDBManager | None = None

    try:
        manager_a = _manager_or_skip(config, graph_a)
        manager_b = _manager_or_skip(config, graph_b)

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

        builder_a = GraphBuilder(config, manager_a, JobManager())
        builder_b = GraphBuilder(config, manager_b, JobManager())

        asyncio.run(builder_a.build_graph_from_path_async(repo))

        handler = RepositoryEventHandler(
            builder_a, repo, perform_initial_scan=False
        )
        for timer in (
            handler._timer,
            handler._health_timer,
            handler._reconcile_timer,
        ):
            if timer is not None:
                timer.cancel()

        handler._affected_set_threshold = 0.01
        supported_files = handler._get_supported_files()
        for file_path in supported_files:
            parsed = builder_a.parse_file(repo, file_path)
            if "error" not in parsed:
                handler.all_file_data[str(file_path.resolve())] = parsed
        handler.imports_map = builder_a._pre_scan_for_imports(supported_files)

        file_a = (repo / "file_a.py").resolve()
        with file_a.open("a") as f:
            f.write("\ndef added(): pass\n")

        handler._pending_paths.add(str(file_a))
        handler._process_batch()

        asyncio.run(builder_b.build_graph_from_path_async(repo))

        counts_a = _counts(manager_a)
        counts_b = _counts(manager_b)
        assert counts_a == counts_b
    finally:
        for manager in (manager_a, manager_b):
            if manager is not None:
                if manager._graph is not None:
                    manager._graph.delete()
                manager.close_driver()
        shutil.rmtree(repo, ignore_errors=True)
