import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import pytest

from cga.config import Config
from cga.db.database import FalkorDBManager
from cga.engine.graph_builder import GraphBuilder
from cga.jobs import JobManager
from cga.query.core import QueryCore

FIXTURE = Path(__file__).parent / "fixtures" / "sample_py"


def _manager_or_skip(config: Config, graph_name: str) -> FalkorDBManager:
    manager = FalkorDBManager(config=config, graph_name=graph_name)
    try:
        manager.get_driver()
    except Exception as exc:
        message = str(exc).lower()
        if "connection refused" in message or "error 61" in message:
            pytest.skip("FalkorDB server unreachable at 127.0.0.1:6379")
        raise
    return manager


def test_falkordb_server_round_trip(tmp_path: Path) -> None:
    config = Config(data_dir=tmp_path, falkordb_host="127.0.0.1", falkordb_port=6379)
    config.ensure_dirs()
    graph_name = f"test_codegraph_{uuid4().hex}"
    manager = _manager_or_skip(config, graph_name)

    try:
        driver = manager.get_driver()
        assert manager.is_connected()

        with driver.session() as session:
            session.run("CREATE (n:Probe {name:'hello'})").consume()
            record = session.run("MATCH (n:Probe) RETURN n.name").single()

        assert record is not None
        assert record["n.name"] == "hello"
    finally:
        if manager._graph is not None:
            manager._graph.delete()
        manager.close_driver()


def test_two_falkordb_managers_query_same_graph_concurrently(tmp_path: Path) -> None:
    config = Config(data_dir=tmp_path, falkordb_host="127.0.0.1", falkordb_port=6379)
    config.ensure_dirs()
    graph_name = f"test_codegraph_concurrent_{uuid4().hex}"
    writer = _manager_or_skip(config, graph_name)
    reader_a = FalkorDBManager(config=config, graph_name=graph_name)
    reader_b = FalkorDBManager(config=config, graph_name=graph_name)

    try:
        builder = GraphBuilder(config, writer, JobManager())
        asyncio.run(builder.build_graph_from_path_async(FIXTURE))

        def query(manager: FalkorDBManager) -> dict:
            result = QueryCore(manager).find_symbol("helper")
            assert result["status"] == "ok"
            assert result["results"][0]["name"] == "helper"
            return result

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(query, (reader_a, reader_b)))

        assert len(results) == 2
        assert {result["results"][0]["path"] for result in results} == {
            str((FIXTURE / "helper.py").resolve())
        }
    finally:
        if writer._graph is not None:
            writer._graph.delete()
        writer.close_driver()
        reader_a.close_driver()
        reader_b.close_driver()
