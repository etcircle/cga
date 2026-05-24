import os
import signal
import subprocess
import sys
import threading
import time
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
    env["XDG_DATA_HOME"] = str(tmp_path / "xdg-data")
    env["CGA_FILE_CACHE_DIR"] = str(tmp_path / "cga-cache")
    env["CGA_HEALTH_DIR"] = str(tmp_path / "cga-health")
    src_path = str(Path.cwd() / "src")
    env["PYTHONPATH"] = (
        src_path if not env.get("PYTHONPATH") else f"{src_path}{os.pathsep}{env['PYTHONPATH']}"
    )

    proc = subprocess.Popen(
        [sys.executable, "-u", "-m", "cga.cli", "watch", str(repo)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=env,
        bufsize=1,
    )

    captured: list[str] = []
    started_event = threading.Event()

    def _drain() -> None:
        for line in iter(proc.stdout.readline, ""):
            captured.append(line)
            if "Watcher started" in line:
                started_event.set()

    drainer = threading.Thread(target=_drain, daemon=True)
    drainer.start()

    try:
        if not started_event.wait(timeout=30):
            proc.kill()
            proc.wait(timeout=5)
            pytest.fail(f"watcher did not start within 30s:\n{''.join(captured)}")
        assert proc.poll() is None, (
            f"process exited (rc={proc.returncode}) before SIGINT:\n{''.join(captured)}"
        )

        proc.send_signal(signal.SIGINT)
        proc.wait(timeout=30)
        drainer.join(timeout=5)

        assert proc.returncode == 0, f"exit {proc.returncode}\n{''.join(captured)}"

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


def test_watch_cold_index_sigint_returns_zero(tmp_path: Path) -> None:
    repo = tmp_path / f"slow-repo-{uuid.uuid4().hex[:8]}"
    repo.mkdir()
    for i in range(50):
        (repo / f"mod_{i}.py").write_text(
            "\n".join(f"def fn_{i}_{j}(): pass" for j in range(40)) + "\n"
        )

    repo_id, _source = repo_identity(repo.resolve())
    graph_name = graph_name_for_repo(repo_id)
    config = Config(data_dir=tmp_path / "data", falkordb_host="127.0.0.1", falkordb_port=6379)
    config.ensure_dirs()
    manager = _manager_or_skip(config, graph_name)

    env = os.environ.copy()
    env["CGA_FALKORDB_HOST"] = "127.0.0.1"
    env["CGA_FALKORDB_PORT"] = "6379"
    env["XDG_DATA_HOME"] = str(tmp_path / "xdg-data")
    env["CGA_FILE_CACHE_DIR"] = str(tmp_path / "cga-cache")
    env["CGA_HEALTH_DIR"] = str(tmp_path / "cga-health")
    src_path = str(Path.cwd() / "src")
    env["PYTHONPATH"] = (
        src_path if not env.get("PYTHONPATH") else f"{src_path}{os.pathsep}{env['PYTHONPATH']}"
    )

    proc = subprocess.Popen(
        [sys.executable, "-u", "-m", "cga.cli", "watch", str(repo)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=env,
        bufsize=1,
    )

    captured: list[str] = []
    cold_started = threading.Event()

    def _drain() -> None:
        for line in iter(proc.stdout.readline, ""):
            captured.append(line)
            if "Cold indexing" in line and "complete" not in line:
                cold_started.set()

    drainer = threading.Thread(target=_drain, daemon=True)
    drainer.start()

    try:
        if not cold_started.wait(timeout=30):
            proc.kill()
            proc.wait(timeout=5)
            pytest.fail(f"cold index never started:\n{''.join(captured)}")

        time.sleep(0.2)
        proc.send_signal(signal.SIGINT)
        proc.wait(timeout=30)
        drainer.join(timeout=5)

        assert proc.returncode == 0, (
            f"SIGINT during cold-index gave exit {proc.returncode}\n{''.join(captured)}"
        )
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
        if manager._graph is not None:
            manager._graph.delete()
        manager.close_driver()
