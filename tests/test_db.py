from pathlib import Path
from uuid import uuid4

import pytest

from cga.config import Config
from cga.db.database import FalkorDBManager


def test_falkordb_server_round_trip(tmp_path: Path) -> None:
    config = Config(data_dir=tmp_path, falkordb_host="127.0.0.1", falkordb_port=6379)
    config.ensure_dirs()
    graph_name = f"test_codegraph_{uuid4().hex}"
    manager = FalkorDBManager(config=config, graph_name=graph_name)

    try:
        try:
            driver = manager.get_driver()
        except Exception as exc:
            if "connection refused" in str(exc).lower() or "error 61" in str(exc).lower():
                pytest.skip("FalkorDB server unreachable at 127.0.0.1:6379")
            raise

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
