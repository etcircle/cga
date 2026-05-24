import os
import signal
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from cga.cli import main
from cga.config import Config
from cga.db.database import FalkorDBManager
from cga.mcp.server import graph_name_for_repo, repo_identity


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
            pytest.skip("FalkorDB unreachable")
        raise
    return manager


def test_watch_argparse_smoke() -> None:
    with pytest.raises(SystemExit) as help_exit:
        main(["watch", "--help"])
    assert help_exit.value.code == 0

    with pytest.raises(SystemExit) as missing_path_exit:
        main(["watch"])
    assert missing_path_exit.value.code != 0


def test_watch_cold_indexes_and_exits_on_sigint(tmp_path: Path) -> None:
    repo = tmp_path / f"repo-{uuid.uuid4().hex[:8]}"
    repo.mkdir()
    (repo / "sample.py").write_text("def watched_function():\n    return 1\n")

    repo_id, _source = repo_identity(repo.resolve())
    graph_name = graph_name_for_repo(repo_id)
    config = Config(data_dir=tmp_path / "data", falkordb_host="127.0.0.1", falkordb_port=6379)
    config.ensure_dirs()
    manager = _manager_or_skip(config, graph_name)

    env = os.environ.copy()
    env["CGA_FALKORDB_HOST"] = "127.0.0.1"
    env["CGA_FALKORDB_PORT"] = "6379"
    src_path = str(Path.cwd() / "src")
    env["PYTHONPATH"] = (
        src_path if not env.get("PYTHONPATH") else f"{src_path}{os.pathsep}{env['PYTHONPATH']}"
    )

    proc = subprocess.Popen(
        [sys.executable, "-m", "cga.cli", "watch", str(repo)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    try:
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.send_signal(signal.SIGINT)
            stdout, stderr = proc.communicate(timeout=30)
        else:
            stdout, stderr = proc.communicate(timeout=1)

        assert proc.returncode == 0, f"stdout:\n{stdout}\nstderr:\n{stderr}"

        driver = manager.get_driver()
        with driver.session() as session:
            rows = session.run(
                """
                MATCH (fn:Function)
                RETURN fn.name AS name
                """
            ).data()

        assert "watched_function" in {row["name"] for row in rows}
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
        if manager._graph is not None:
            manager._graph.delete()
        manager.close_driver()
