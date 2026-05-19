from pathlib import Path

from cga.config import Config
from cga.db.database import FalkorDBManager


def test_falkordb_lite_round_trip_over_socket(tmp_path: Path) -> None:
    config = Config(
        data_dir=tmp_path,
        falkordb_socket_path=tmp_path / "falkordb.sock",
        falkordb_db_path=tmp_path / "falkordb" / "cga.db",
    )
    config.ensure_dirs()

    manager = FalkorDBManager(config=config, graph_name="test_codegraph")
    try:
        driver = manager.get_driver()

        assert manager._process is not None
        assert manager._process.poll() is None
        assert config.falkordb_socket_path.exists()

        with driver.session() as session:
            session.run("CREATE (n:Probe {name:'hello'})").consume()
            record = session.run("MATCH (n:Probe) RETURN n.name").single()

        assert record is not None
        assert record["n.name"] == "hello"
    finally:
        manager.shutdown()
