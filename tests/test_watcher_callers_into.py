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
        if (
            "connection refused" in message
            or "error 61" in message
            or "operation not permitted" in message
        ):
            pytest.skip("FalkorDB server unreachable at 127.0.0.1:6379")
        raise
    return manager


def _cancel_watcher_timers(handler: RepositoryEventHandler) -> None:
    for attr in ("_timer", "_health_timer", "_reconcile_timer"):
        timer = getattr(handler, attr)
        if timer is not None:
            timer.cancel()


def test_callers_into_returns_callers_before_changed_file_update(tmp_path: Path) -> None:
    repo_root = (tmp_path / "watcher_callers_repo").resolve()
    repo_root.mkdir()
    (repo_root / "file_a.py").write_text("def hub():\n    pass\n")
    (repo_root / "file_b.py").write_text(
        "from file_a import hub\n\n"
        "def caller():\n"
        "    hub()\n"
    )

    config = _config(tmp_path)
    config.ensure_dirs()
    graph_name = f"test_watcher_callers_{uuid4().hex}"
    manager = _manager_or_skip(config, graph_name)

    try:
        builder = GraphBuilder(config, manager, JobManager())
        asyncio.run(builder.build_graph_from_path_async(repo_root))

        handler = RepositoryEventHandler(
            builder,
            repo_root,
            perform_initial_scan=False,
        )
        _cancel_watcher_timers(handler)

        file_a = str((repo_root / "file_a.py").resolve())
        file_b = str((repo_root / "file_b.py").resolve())

        assert file_b in handler._callers_into({file_a})
        assert handler._callers_into(set()) == set()
    finally:
        if manager._graph is not None:
            manager._graph.delete()
        manager.close_driver()
        shutil.rmtree(repo_root, ignore_errors=True)
