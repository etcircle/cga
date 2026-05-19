"""CGA MCP stdio server and tool coordinator."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from mcp import types
from mcp.server import NotificationOptions, Server
from mcp.server.models import InitializationOptions
from mcp.server.stdio import stdio_server

from cga.config import Config
from cga.db.database import FalkorDBManager
from cga.engine.graph_builder import GraphBuilder
from cga.jobs import JobInfo, JobManager, JobStatus
from cga.query.core import QueryCore
from cga.tools.registry import ToolRegistry

SOURCE_SUFFIXES = frozenset({".py", ".ipynb", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx"})


def resolve_repo_root() -> Path:
    return Path(os.environ.get("CGA_REPO", os.getcwd())).expanduser().resolve()


def repo_identity(repo_root: Path) -> tuple[str, str]:
    remote = _git_remote_url(repo_root)
    source = remote or str(repo_root)
    repo_id = hashlib.sha256(source.encode("utf-8")).hexdigest()[:16]
    return repo_id, source


def graph_name_for_repo(repo_id: str) -> str:
    return f"cga_{repo_id}"


def _git_remote_url(repo_root: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), "config", "--get", "remote.origin.url"],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except Exception:
        return None
    remote = result.stdout.strip()
    return remote or None


def _progress(job: JobInfo) -> dict[str, Any]:
    return {
        "status": job.status.value,
        "total_files": job.total_files,
        "processed_files": job.processed_files,
        "current_file": job.current_file,
        "percentage": job.progress_percentage,
    }


class CGAMCPServer:
    """Thin MCP-facing coordinator for one served repository."""

    def __init__(
        self,
        repo_root: Path | None = None,
        config: Config | None = None,
        graph_name: str | None = None,
    ) -> None:
        self.repo_root = (repo_root or resolve_repo_root()).resolve()
        self.repo_id, self.repo_identity_source = repo_identity(self.repo_root)
        self.graph_name = graph_name or graph_name_for_repo(self.repo_id)
        self.config = config or Config.from_env()
        self.config.ensure_dirs()
        self.db_manager = FalkorDBManager(self.config, graph_name=self.graph_name)
        self.job_manager = JobManager()
        self.graph_builder = GraphBuilder(self.config, self.db_manager, self.job_manager)
        self.query_core = QueryCore(self.db_manager)
        self.registry = ToolRegistry()
        self._index_lock = threading.Lock()
        self._register_tools()

    def _register_tools(self) -> None:
        self.registry.register(
            "find_symbol",
            "Find a function, class, variable, interface, or explicit module by name.",
            {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "kind": {"type": "string"},
                    "lang": {"type": "string"},
                },
                "required": ["name"],
                "additionalProperties": False,
            },
            self.find_symbol_tool,
        )
        self.registry.register(
            "find_callers",
            "Find functions/classes/files that call a named symbol.",
            {
                "type": "object",
                "properties": {"name": {"type": "string"}, "lang": {"type": "string"}},
                "required": ["name"],
                "additionalProperties": False,
            },
            self.find_callers_tool,
        )
        self.registry.register(
            "find_references",
            "Find v1 graph references to a named symbol.",
            {
                "type": "object",
                "properties": {"name": {"type": "string"}, "lang": {"type": "string"}},
                "required": ["name"],
                "additionalProperties": False,
            },
            self.find_references_tool,
        )
        self.registry.register(
            "index_status",
            "Report whether this repo is empty, indexing, or indexed.",
            {"type": "object", "properties": {}, "additionalProperties": False},
            self.index_status_tool,
        )

    def find_symbol_tool(self, arguments: dict[str, Any]) -> dict[str, Any]:
        gate = self._ensure_indexed()
        if gate is not None:
            return gate
        refresh = self._refresh_stale_files()
        if refresh is not None:
            return refresh
        return self.query_core.find_symbol(
            arguments["name"], kind=arguments.get("kind"), lang=arguments.get("lang")
        )

    def find_callers_tool(self, arguments: dict[str, Any]) -> dict[str, Any]:
        gate = self._ensure_indexed()
        if gate is not None:
            return gate
        refresh = self._refresh_stale_files()
        if refresh is not None:
            return refresh
        return self.query_core.find_callers(arguments["name"], lang=arguments.get("lang"))

    def find_references_tool(self, arguments: dict[str, Any]) -> dict[str, Any]:
        gate = self._ensure_indexed()
        if gate is not None:
            return gate
        refresh = self._refresh_stale_files()
        if refresh is not None:
            return refresh
        return self.query_core.find_references(arguments["name"], lang=arguments.get("lang"))

    def index_status_tool(self, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        del arguments
        active = self.job_manager.find_active_job_by_path(str(self.repo_root))
        if active is not None:
            return {"status": "indexing", "job_id": active.job_id, "progress": _progress(active)}

        file_count = self._file_count()
        if file_count is None:
            return {"status": "error", "warnings": ["FalkorDB is unreachable"], "results": []}
        if file_count > 0:
            return {"status": "indexed", "files": file_count}
        return {"status": "empty"}

    def _ensure_indexed(self) -> dict[str, Any] | None:
        status = self.index_status_tool({})
        if status["status"] == "indexing":
            return status
        if status["status"] == "error":
            return status
        if status["status"] == "empty":
            job_id = self._start_cold_index_thread()
            return {"status": "indexing", "job_id": job_id}
        return None

    def _start_cold_index_thread(self) -> str:
        with self._index_lock:
            active = self.job_manager.find_active_job_by_path(str(self.repo_root))
            if active is not None:
                return active.job_id
            job_id = self.job_manager.create_job(str(self.repo_root), is_dependency=False)

            thread = threading.Thread(
                target=self._run_cold_index_thread,
                args=(job_id,),
                name=f"cga-index-{self.repo_id}",
                daemon=True,
            )
            thread.start()
            return job_id

    def _run_cold_index_thread(self, job_id: str) -> None:
        thread_db = FalkorDBManager(self.config, graph_name=self.graph_name)
        try:
            thread_builder = GraphBuilder(self.config, thread_db, self.job_manager)
            asyncio.run(
                thread_builder.build_graph_from_path_async(
                    self.repo_root, is_dependency=False, job_id=job_id
                )
            )
            job = self.job_manager.get_job(job_id)
            if job and job.status == JobStatus.COMPLETED:
                self._write_mtime_sidecar(self._scan_source_mtimes())
        except Exception as exc:
            self.job_manager.update_job(
                job_id, status=JobStatus.FAILED, end_time=datetime.now(), errors=[str(exc)]
            )
        finally:
            thread_db.close_driver()

    def _refresh_stale_files(self) -> dict[str, Any] | None:
        try:
            previous = self._read_mtime_sidecar()
            current = self._scan_source_mtimes()
            changed = sorted(path for path, mtime in current.items() if previous.get(path) != mtime)
            deleted = sorted(path for path in previous if path not in current)

            if changed or deleted:
                imports_map = self.graph_builder._pre_scan_for_imports(  # noqa: SLF001
                    [Path(path) for path in current]
                )
                for path in deleted:
                    self._delete_file_from_graph(Path(path))
                for path in changed:
                    self._delete_file_from_graph(Path(path))
                    self._index_one_file(Path(path), imports_map)
                self._write_mtime_sidecar(current)
        except Exception as exc:
            return {"status": "error", "warnings": [str(exc)], "results": []}
        return None

    def _index_one_file(self, path: Path, imports_map: dict[str, Any]) -> None:
        file_data = self.graph_builder.parse_file(self.repo_root, path, is_dependency=False)
        if "error" in file_data:
            self.graph_builder.add_minimal_file_node(path, self.repo_root, is_dependency=False)
        else:
            self.graph_builder.add_file_to_graph(file_data, self.repo_root.name, imports_map)
            self.graph_builder._create_all_inheritance_links([file_data], imports_map)  # noqa: SLF001
            self.graph_builder._create_all_function_calls([file_data], imports_map)  # noqa: SLF001

    def _delete_file_from_graph(self, path: Path) -> None:
        with self.db_manager.get_driver().session() as session:
            session.run(
                """
                MATCH (f:File {path: $path})
                OPTIONAL MATCH (f)-[:CONTAINS*]->(n)
                DETACH DELETE n, f
                """,
                path=str(path.resolve()),
            )

    def _file_count(self) -> int | None:
        try:
            with self.db_manager.get_driver().session() as session:
                row = session.run("MATCH (f:File) RETURN count(f) AS count").single()
            return int(row["count"] if row else 0)
        except Exception:
            return None

    def _scan_source_mtimes(self) -> dict[str, float]:
        mtimes: dict[str, float] = {}
        for path in self.repo_root.rglob("*"):
            if not path.is_file() or path.suffix not in SOURCE_SUFFIXES:
                continue
            if _is_ignored_path(path, self.repo_root, self.config.index_ignore):
                continue
            mtimes[str(path.resolve())] = path.stat().st_mtime
        return mtimes

    def _sidecar_path(self) -> Path:
        return self.config.data_dir / f"mtime-{self.repo_id}.json"

    def _read_mtime_sidecar(self) -> dict[str, float]:
        path = self._sidecar_path()
        if not path.exists():
            current = self._scan_source_mtimes()
            self._write_mtime_sidecar(current)
            return current
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
        if isinstance(data, dict):
            return {str(key): float(value) for key, value in data.items()}
        return {}

    def _write_mtime_sidecar(self, mtimes: dict[str, float]) -> None:
        self.config.data_dir.mkdir(parents=True, exist_ok=True)
        path = self._sidecar_path()
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(mtimes, sort_keys=True, indent=2), encoding="utf-8")
        tmp.replace(path)


async def run_stdio_server(coordinator: CGAMCPServer | None = None) -> None:
    coordinator = coordinator or CGAMCPServer()
    server = Server("cga")

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return [
            types.Tool(
                name=tool["name"],
                description=tool["description"],
                inputSchema=tool["inputSchema"],
            )
            for tool in coordinator.registry.list_tools()
        ]

    @server.call_tool(validate_input=True)
    async def call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return coordinator.registry.dispatch(name, arguments)

    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            InitializationOptions(
                server_name="cga",
                server_version="0.1.0.dev0",
                capabilities=server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                ),
                instructions="CGA serves find_symbol, find_callers, find_references, and index_status for one repo.",
            ),
        )


def _is_ignored_path(path: Path, repo_root: Path, ignore_dirs: tuple[str, ...]) -> bool:
    ignored = {item.strip().lower() for item in ignore_dirs if item.strip()}
    if not ignored:
        return False
    try:
        parts = {part.lower() for part in path.relative_to(repo_root).parent.parts}
    except ValueError:
        return False
    return bool(parts.intersection(ignored))


def main() -> None:
    asyncio.run(run_stdio_server())


if __name__ == "__main__":
    main()
