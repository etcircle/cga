"""CGA command-line entry point.

v1.0 builds the MCP client (``cga mcp``). The full standalone CLI client is
v1.1 (see ROADMAP.md).
"""

from __future__ import annotations

import argparse
import asyncio
import signal
import sys
import time
from pathlib import Path

from cga import __version__


def _run_watch(path_arg: str) -> int:
    path = Path(path_arg).expanduser().resolve()
    if not path.is_dir():
        print(f"error: watch path is not a directory: {path}", file=sys.stderr)
        return 1

    try:
        from cga.config import Config

        config = Config.from_env()
        config.ensure_dirs()
    except Exception as exc:
        print(f"error: failed to load config: {exc}", file=sys.stderr)
        return 1

    from cga.db.database import FalkorDBManager
    from cga.engine.graph_builder import GraphBuilder
    from cga.engine.watcher import CodeWatcher
    from cga.jobs import JobManager
    from cga.mcp.server import graph_name_for_repo, repo_identity

    repo_id, source = repo_identity(path)
    graph_name = graph_name_for_repo(repo_id)
    db_manager = FalkorDBManager(config, graph_name=graph_name)
    job_manager = JobManager()
    graph_builder = GraphBuilder(config, db_manager, job_manager)

    print(f"Graph: {graph_name}")
    print(f"Repository identity: {source}")
    print(f"Watching: {path}")

    driver = db_manager.get_driver()
    with driver.session() as session:
        record = session.run("MATCH (n) RETURN count(n) AS c LIMIT 1").single()
    node_count = int(record["c"]) if record is not None else 0

    graph_was_empty = node_count == 0
    if graph_was_empty:
        print(f"Cold indexing {path}...")

        def _raise_kbd_interrupt(signum, _frame):
            raise KeyboardInterrupt(f"signal {signum}")

        prev_term = signal.signal(signal.SIGTERM, _raise_kbd_interrupt)
        try:
            asyncio.run(graph_builder.build_graph_from_path_async(path))
        except KeyboardInterrupt:
            print("\nCold indexing interrupted.", file=sys.stderr)
            return 0
        finally:
            signal.signal(signal.SIGTERM, prev_term)
        print(f"Cold indexing complete for {path}.")
    else:
        print(f"Graph already indexed ({node_count} nodes); resuming watcher.")

    watcher = CodeWatcher(graph_builder)
    if sys.platform == "darwin":
        from watchdog.observers.kqueue import KqueueObserver

        watcher.observer = KqueueObserver()
    watcher.watch_directory(str(path), perform_initial_scan=not graph_was_empty)
    watcher.start()
    print(f"Watcher started for {path}.")

    try:
        while watcher.observer.is_alive():
            time.sleep(1)
    except KeyboardInterrupt:
        watcher.stop()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cga", description="CodeGraphAgent (CGA)")
    parser.add_argument("--version", action="version", version=f"cga {__version__}")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("mcp", help="run the MCP stdio server")
    watch = sub.add_parser("watch", help="watch a repository in the foreground")
    watch.add_argument("path")

    args = parser.parse_args(argv)
    if args.command == "mcp":
        from cga.mcp.server import main as mcp_main

        mcp_main()
        return 0
    if args.command == "watch":
        return _run_watch(args.path)
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
