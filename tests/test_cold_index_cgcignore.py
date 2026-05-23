import asyncio
import shutil
import uuid
from pathlib import Path

import pytest

from cga.config import Config
from cga.db.database import FalkorDBManager
from cga.engine.graph_builder import GraphBuilder
from cga.jobs import JobManager


_IGNORE = (
    "__pycache__",
    ".git",
    "node_modules",
    ".venv",
    "venv",
    "dist",
    "build",
    ".next",
    ".cache",
    ".pytest_cache",
    ".ruff_cache",
    ".claude",
    "htmlcov",
)


def _config(run_dir: Path) -> Config:
    return Config(
        data_dir=run_dir / "data",
        falkordb_host="127.0.0.1",
        falkordb_port=6379,
        index_ignore=_IGNORE,
    )


def _make_builder(config: Config, graph_name: str) -> tuple[FalkorDBManager, GraphBuilder]:
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
    return manager, GraphBuilder(config, manager, JobManager())


def test_cold_index_cgcignore_applies_to_unresolved_tmp_path(tmp_path: Path) -> None:
    repo_root = Path("/tmp") / f"cga-test-{uuid.uuid4().hex[:8]}"
    repo_root.mkdir(parents=True)
    graph_name = f"test_cgcignore_{uuid.uuid4().hex[:8]}"
    manager = None

    try:
        (repo_root / ".cgcignore").write_text("should_skip/\n")
        (repo_root / "ok_file.py").write_text("def hello():\n    pass\n")
        should_skip = repo_root / "should_skip"
        should_skip.mkdir()
        (should_skip / "skipped.py").write_text("def skipped_fn():\n    pass\n")

        config = _config(tmp_path)
        manager, builder = _make_builder(config, graph_name)

        asyncio.run(builder.build_graph_from_path_async(repo_root))

        driver = manager.get_driver()
        with driver.session() as session:
            rows = session.run(
                """
                MATCH (fn:Function)
                RETURN fn.name AS name
                """
            ).data()

        function_names = {row["name"] for row in rows}
        assert "hello" in function_names
        assert "skipped_fn" not in function_names
    finally:
        if manager is not None:
            try:
                manager._graph.delete()
            finally:
                manager.close_driver()
        shutil.rmtree(repo_root, ignore_errors=True)
