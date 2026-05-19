import json
import os
import shutil
import sys
import time
from pathlib import Path
from uuid import uuid4

import pytest
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from cga.config import Config
from cga.db.database import FalkorDBManager
from cga.mcp.server import graph_name_for_repo, repo_identity

FIXTURE = Path(__file__).parent / "fixtures" / "sample_py"


def _skip_if_falkordb_unreachable(tmp_path: Path) -> None:
    manager = FalkorDBManager(
        config=Config(data_dir=tmp_path, falkordb_host="127.0.0.1", falkordb_port=6379),
        graph_name=f"test_mcp_e2e_probe_{uuid4().hex}",
    )
    try:
        manager.get_driver()
    except Exception as exc:
        message = str(exc).lower()
        if "connection refused" in message or "error 61" in message:
            pytest.skip("FalkorDB server unreachable at 127.0.0.1:6379")
        raise
    finally:
        if manager._graph is not None:
            manager._graph.delete()
        manager.close_driver()


def _payload(result) -> dict:
    if result.structuredContent is not None:
        return result.structuredContent
    assert result.content, "tool returned no MCP content"
    return json.loads(result.content[0].text)


async def _call(session: ClientSession, name: str, arguments: dict | None = None) -> dict:
    result = await session.call_tool(name, arguments or {})
    assert not result.isError
    return _payload(result)


@pytest.mark.asyncio
async def test_mcp_stdio_wire_cold_index_flow(tmp_path: Path) -> None:
    _skip_if_falkordb_unreachable(tmp_path / "probe-data")

    repo_copy = tmp_path / "sample_py_copy"
    shutil.copytree(FIXTURE, repo_copy)
    data_home = tmp_path / "xdg-data"

    repo_id, _ = repo_identity(repo_copy.resolve())
    graph_name = graph_name_for_repo(repo_id)
    cleanup_manager = FalkorDBManager(
        config=Config(data_dir=data_home / "cga", falkordb_host="127.0.0.1", falkordb_port=6379),
        graph_name=graph_name,
    )

    env = os.environ.copy()
    env.update(
        {
            "CGA_REPO": str(repo_copy),
            "XDG_DATA_HOME": str(data_home),
            "CGA_FALKORDB_HOST": "127.0.0.1",
            "CGA_FALKORDB_PORT": "6379",
            "CGA_INDEX_IGNORE": "__pycache__,.git",
        }
    )
    env["PYTHONPATH"] = str(Path(__file__).parents[1] / "src")

    server = StdioServerParameters(
        command=sys.executable,
        args=["-m", "cga.mcp.server"],
        env=env,
        cwd=str(Path(__file__).parents[1]),
    )

    try:
        async with stdio_client(server) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()

                listed = await session.list_tools()
                tools = {tool.name: tool for tool in listed.tools}
                assert set(tools) == {
                    "find_symbol",
                    "find_callers",
                    "find_references",
                    "index_status",
                }
                for tool in tools.values():
                    assert tool.inputSchema["type"] == "object"

                first = await _call(session, "find_symbol", {"name": "helper"})
                assert first["status"] == "indexing"
                assert first["job_id"]

                deadline = time.time() + 20
                status = await _call(session, "index_status")
                while status["status"] == "indexing" and time.time() < deadline:
                    time.sleep(0.1)
                    status = await _call(session, "index_status")

                assert status["status"] == "indexed"
                assert status["files"] >= 4

                symbol = await _call(session, "find_symbol", {"name": "helper"})
                assert symbol["status"] == "ok"
                assert symbol["results"][0]["path"] == str((repo_copy / "helper.py").resolve())

                callers = await _call(session, "find_callers", {"name": "helper"})
                assert callers["status"] == "ok"
                assert {item["caller"]["name"] for item in callers["results"]} == {
                    "call_helper",
                    "extra",
                }

                references = await _call(session, "find_references", {"name": "Base"})
                assert references["status"] == "ok"
                assert any(item["ref_kind"] == "inherits" for item in references["results"])
    finally:
        try:
            cleanup_manager.get_driver()
            if cleanup_manager._graph is not None:
                cleanup_manager._graph.delete()
        finally:
            cleanup_manager.close_driver()
