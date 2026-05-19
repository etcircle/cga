import asyncio
from pathlib import Path
from uuid import uuid4

import pytest

from cga.config import Config
from cga.db.database import FalkorDBManager
from cga.engine.graph_builder import GraphBuilder
from cga.jobs import JobManager
from cga.query.builder import CypherBuilder
from cga.query.core import QueryCore

FIXTURE = Path(__file__).parent / "fixtures" / "sample_py"


def _config(tmp_path: Path) -> Config:
    return Config(
        data_dir=tmp_path,
        falkordb_host="127.0.0.1",
        falkordb_port=6379,
        index_ignore=("__pycache__", ".git"),
    )


def _manager_or_skip(config: Config) -> FalkorDBManager:
    manager = FalkorDBManager(config=config, graph_name=f"test_query_{uuid4().hex}")
    try:
        manager.get_driver()
    except Exception as exc:
        message = str(exc).lower()
        if "connection refused" in message or "error 61" in message:
            pytest.skip("FalkorDB server unreachable at 127.0.0.1:6379")
        raise
    return manager


@pytest.fixture
def query_core(tmp_path: Path):
    config = _config(tmp_path)
    manager = _manager_or_skip(config)
    try:
        builder = GraphBuilder(config, manager, JobManager())
        asyncio.run(builder.build_graph_from_path_async(FIXTURE))
        yield QueryCore(manager)
    finally:
        if manager._graph is not None:
            manager._graph.delete()
        manager.close_driver()


def test_builder_symbol_targets_labels_and_params() -> None:
    cypher, params = CypherBuilder.symbol("helper", kind="function", lang="python")

    assert "MATCH (n:Function {name: $name})" in cypher
    assert "n.lang = $lang" in cypher
    assert "Function|Class" not in cypher
    assert params == {"name": "helper", "lang": "python"}


def test_builder_symbol_default_defined_labels_and_explicit_module() -> None:
    cypher, params = CypherBuilder.symbol("Base")

    for label in ("Function", "Class", "Variable", "Interface"):
        assert f"MATCH (n:{label} {{name: $name}})" in cypher
    assert "MATCH (n:Module {name: $name})" not in cypher
    assert cypher.count("UNION") == 3
    assert params == {"name": "Base", "lang": None}

    module_cypher, module_params = CypherBuilder.symbol("math", kind="module")
    assert "MATCH (n:Module {name: $name})" in module_cypher
    assert module_params == {"name": "math", "lang": None}


def test_builder_callers_targets_calls_and_polymorphic_callers() -> None:
    cypher, params = CypherBuilder.callers("helper", lang="python")

    assert "[c:CALLS]->(target)" in cypher
    assert "target:Function OR target:Class" in cypher
    assert "caller:File OR caller:Function OR caller:Class" in cypher
    assert "__init__" in cypher and "constructor" in cypher
    assert params == {"name": "helper", "lang": "python"}


def test_builder_references_targets_v1_relationships() -> None:
    cypher, params = CypherBuilder.references("Base")

    for relationship in (":CALLS", ":INHERITS", ":IMPLEMENTS", ":IMPORTS"):
        assert relationship in cypher
    for ref_kind in ("calls", "inherits", "implements", "imports"):
        assert f"'{ref_kind}' AS ref_kind" in cypher
    assert params == {"name": "Base", "lang": None}


def test_find_symbol_statuses_filters_and_ambiguity(query_core: QueryCore) -> None:
    helper = query_core.find_symbol("helper")
    assert helper["status"] == "ok"
    assert helper["warnings"] == []
    assert helper["results"] == [
        {
            "name": "helper",
            "kind": "function",
            "path": str((FIXTURE / "helper.py").resolve()),
            "line": 1,
            "lang": "python",
        }
    ]

    assert query_core.find_symbol("missing") == {"results": [], "status": "empty", "warnings": []}

    by_kind = query_core.find_symbol("Base", kind="class")
    assert by_kind["status"] == "ok"
    assert by_kind["results"][0]["kind"] == "class"

    assert query_core.find_symbol("Base", kind="function")["status"] == "empty"
    module = query_core.find_symbol("math", kind="module")
    assert module["status"] == "ok"
    assert module["results"][0]["kind"] == "module"
    assert query_core.find_symbol("helper", lang="javascript")["status"] == "empty"
    assert query_core.find_symbol("call_helper")["status"] == "ambiguous"


def test_find_callers_statuses_and_targets(query_core: QueryCore) -> None:
    callers = query_core.find_callers("helper")

    assert callers["status"] == "ok"
    caller_names = {item["caller"]["name"] for item in callers["results"]}
    assert caller_names == {"call_helper", "extra"}
    assert {item["caller"]["kind"] for item in callers["results"]} == {"function"}
    assert {item["target"]["name"] for item in callers["results"]} == {"helper"}
    assert {item["call_site_line"] for item in callers["results"]} == {6, 7}

    assert query_core.find_callers("use_math")["status"] == "empty"


def test_find_references_returns_inherits_and_calls(query_core: QueryCore) -> None:
    base_refs = query_core.find_references("Base")

    assert base_refs["status"] == "ok"
    inherits = [item for item in base_refs["results"] if item["ref_kind"] == "inherits"]
    assert len(inherits) == 1
    assert inherits[0]["source"] == {
        "name": "Child",
        "kind": "class",
        "path": str((FIXTURE / "models.py").resolve()),
        "line": 6,
    }
    assert inherits[0]["target"]["name"] == "Base"
    assert inherits[0]["target"]["kind"] == "class"

    helper_refs = query_core.find_references("helper")
    assert helper_refs["status"] == "ok"
    call_sources = {item["source"]["name"] for item in helper_refs["results"] if item["ref_kind"] == "calls"}
    assert call_sources == {"call_helper", "extra"}

    assert query_core.find_references("missing")["status"] == "empty"


def test_query_core_returns_error_status_instead_of_raising(tmp_path: Path) -> None:
    # A manager pointed at a port with no FalkorDB server: the query core must
    # surface status:"error" and never raise — the v1.0 "tools never throw" contract.
    config = Config(data_dir=tmp_path, falkordb_host="127.0.0.1", falkordb_port=6399)
    core = QueryCore(FalkorDBManager(config=config, graph_name="unreachable"))

    response = core.find_symbol("anything")
    assert response["status"] == "error"
    assert response["results"] == []
    assert response["warnings"]
