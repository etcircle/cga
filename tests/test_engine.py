import asyncio
from pathlib import Path
from uuid import uuid4

import pytest

from cga.config import Config
from cga.db.database import FalkorDBManager
from cga.engine.graph_builder import GraphBuilder, TreeSitterParser
from cga.jobs import JobManager


FIXTURE = Path(__file__).parent / "fixtures" / "sample_py"


def _config(tmp_path: Path) -> Config:
    return Config(
        data_dir=tmp_path,
        falkordb_host="127.0.0.1",
        falkordb_port=6379,
        index_ignore=("__pycache__", ".git"),
    )


def _manager_or_skip(config: Config) -> FalkorDBManager:
    manager = FalkorDBManager(config=config, graph_name=f"test_engine_{uuid4().hex}")
    try:
        manager.get_driver()
    except Exception as exc:
        message = str(exc).lower()
        if "connection refused" in message or "error 61" in message:
            pytest.skip("FalkorDB server unreachable at 127.0.0.1:6379")
        raise
    return manager


def _single(session, query: str, **params):
    record = session.run(query, **params).single()
    assert record is not None, query
    return record


def test_engine_indexes_python_fixture_against_frozen_schema(tmp_path: Path) -> None:
    config = _config(tmp_path)
    manager = _manager_or_skip(config)

    try:
        builder = GraphBuilder(config, manager, JobManager())
        asyncio.run(builder.build_graph_from_path_async(FIXTURE))

        driver = manager.get_driver()
        main_path = str((FIXTURE / "main.py").resolve())
        helper_path = str((FIXTURE / "helper.py").resolve())
        models_path = str((FIXTURE / "models.py").resolve())

        with driver.session() as session:
            file_row = _single(
                session,
                """
                MATCH (f:File {path: $path})
                RETURN f.name AS name, f.path AS path, f.relative_path AS relative_path
                """,
                path=main_path,
            )
            assert file_row["name"] == "main.py"
            assert file_row["path"] == main_path
            assert file_row["relative_path"] == "main.py"

            function_row = _single(
                session,
                """
                MATCH (fn:Function {name: 'call_helper', path: $path})
                RETURN fn.name AS name, fn.path AS path, fn.line_number AS line_number,
                       fn.end_line AS end_line, fn.args AS args, fn.lang AS lang
                """,
                path=main_path,
            )
            assert function_row["name"] == "call_helper"
            assert function_row["path"] == main_path
            assert function_row["line_number"] == 6
            assert function_row["end_line"] >= 7
            assert function_row["args"] == ["number"]
            assert function_row["lang"] == "python"

            class_row = _single(
                session,
                """
                MATCH (c:Class {name: 'Child', path: $path})
                RETURN c.name AS name, c.path AS path, c.line_number AS line_number,
                       c.bases AS bases, c.lang AS lang
                """,
                path=models_path,
            )
            assert class_row["name"] == "Child"
            assert class_row["path"] == models_path
            assert class_row["line_number"] == 6
            assert class_row["bases"] == ["Base"]
            assert class_row["lang"] == "python"

            contains_function = _single(
                session,
                """
                MATCH (:File {path: $path})-[:CONTAINS]->(fn:Function {name: 'call_helper'})
                RETURN count(fn) AS count
                """,
                path=main_path,
            )
            assert contains_function["count"] == 1

            contains_method = _single(
                session,
                """
                MATCH (:Class {name: 'Child', path: $path})-[:CONTAINS]->
                      (fn:Function {name: 'child_method', path: $path})
                RETURN count(fn) AS count
                """,
                path=models_path,
            )
            assert contains_method["count"] == 1

            imports_row = _single(
                session,
                """
                MATCH (:File {path: $path})-[r:IMPORTS]->(m:Module {name: 'math'})
                RETURN count(r) AS count, m.name AS module_name
                """,
                path=main_path,
            )
            assert imports_row["count"] == 1
            assert imports_row["module_name"] == "math"

            inherits_row = _single(
                session,
                """
                MATCH (:Class {name: 'Child', path: $path})-[r:INHERITS]->
                      (:Class {name: 'Base', path: $path})
                RETURN count(r) AS count
                """,
                path=models_path,
            )
            assert inherits_row["count"] == 1

            calls_row = _single(
                session,
                """
                MATCH (:Function {name: 'call_helper', path: $caller_path})-[r:CALLS]->
                      (:Function {name: 'helper', path: $callee_path})
                RETURN count(r) AS count, r.line_number AS line_number,
                       r.full_call_name AS full_call_name
                """,
                caller_path=main_path,
                callee_path=helper_path,
            )
            assert calls_row["count"] == 1
            assert calls_row["line_number"] == 7
            assert calls_row["full_call_name"] == "helper"
    finally:
        if manager._graph is not None:
            manager._graph.delete()
        manager.close_driver()


def test_js_and_ts_parsers_parse_trivial_snippets(tmp_path: Path) -> None:
    js_file = tmp_path / "sample.js"
    ts_file = tmp_path / "sample.ts"
    js_file.write_text("function hello(name) { return name; }\n", encoding="utf-8")
    ts_file.write_text("function typed(name: string): string { return name; }\n", encoding="utf-8")

    js_data = TreeSitterParser("javascript").parse(js_file)
    ts_data = TreeSitterParser("typescript").parse(ts_file)

    assert "error" not in js_data
    assert "error" not in ts_data
    assert any(function["name"] == "hello" for function in js_data["functions"])
    assert any(function["name"] == "typed" for function in ts_data["functions"])


def test_v1_1_batch_resolves_function_to_class_and_self_calls(tmp_path: Path) -> None:
    """The v1.1 polymorphic batch must keep v1.0's Function→Class fall-through.

    The shared fixture has two call sites that exercise the COALESCE chain:
      * ``main.py:make_child()`` calls ``Child()`` — no ``__init__`` on Child,
        so the new batch's ``COALESCE(tF, tInit, tC)`` must fall through to
        ``tC`` (the Class node) and produce a Function→Class CALLS edge.
      * ``models.py:child_method()`` calls ``self.base_method()`` — direct
        self-call, resolution stays inside ``models.py`` and produces a
        Function→Function edge to ``Base.base_method``.
    """
    config = _config(tmp_path)
    manager = _manager_or_skip(config)

    try:
        builder = GraphBuilder(config, manager, JobManager())
        asyncio.run(builder.build_graph_from_path_async(FIXTURE))

        driver = manager.get_driver()
        main_path = str((FIXTURE / "main.py").resolve())
        models_path = str((FIXTURE / "models.py").resolve())

        with driver.session() as session:
            # Function (call_helper-style caller) → Class (Child) — Class fall-through
            # because Child has no __init__ method in the fixture.
            class_call = _single(
                session,
                """
                MATCH (caller:Function {name: 'make_child', path: $main})
                      -[r:CALLS]->
                      (target:Class {name: 'Child', path: $models})
                RETURN count(r) AS c, r.full_call_name AS full_call_name
                """,
                main=main_path,
                models=models_path,
            )
            assert class_call["c"] == 1
            assert class_call["full_call_name"] == "Child"

            # Function → Function via self.method() — same-file resolution.
            self_call = _single(
                session,
                """
                MATCH (caller:Function {name: 'child_method', path: $models})
                      -[r:CALLS]->
                      (target:Function {name: 'base_method', path: $models})
                RETURN count(r) AS c, r.full_call_name AS full_call_name
                """,
                models=models_path,
            )
            assert self_call["c"] == 1
            assert self_call["full_call_name"] == "self.base_method"
    finally:
        if manager._graph is not None:
            manager._graph.delete()
        manager.close_driver()


def test_v1_1_batch_creates_file_scope_calls_edge(tmp_path: Path) -> None:
    """Module-level call expressions must produce a File→Function CALLS edge.

    Exercises the second batch template (``_FILESCOPE_BATCH_CYPHER``): the
    caller has no enclosing function/class, so the buffer entry omits
    ``caller_name`` and the batch matches ``(:File {path: ...})`` directly.
    """
    fixture = tmp_path / "filescope"
    fixture.mkdir()
    (fixture / "lib.py").write_text("def util(x):\n    return x + 1\n", encoding="utf-8")
    (fixture / "entry.py").write_text(
        "from lib import util\n\nutil(5)\n",
        encoding="utf-8",
    )

    config = _config(tmp_path)
    manager = _manager_or_skip(config)

    try:
        builder = GraphBuilder(config, manager, JobManager())
        asyncio.run(builder.build_graph_from_path_async(fixture))

        driver = manager.get_driver()
        entry_path = str((fixture / "entry.py").resolve())
        lib_path = str((fixture / "lib.py").resolve())

        with driver.session() as session:
            file_call = _single(
                session,
                """
                MATCH (caller:File {path: $entry})
                      -[r:CALLS]->
                      (target:Function {name: 'util', path: $lib})
                RETURN count(r) AS c, r.line_number AS line_number,
                       r.full_call_name AS full_call_name
                """,
                entry=entry_path,
                lib=lib_path,
            )
            assert file_call["c"] == 1
            assert file_call["line_number"] == 3
            assert file_call["full_call_name"] == "util"
    finally:
        if manager._graph is not None:
            manager._graph.delete()
        manager.close_driver()


def test_v1_1_batch_chunks_across_multiple_flushes(tmp_path: Path) -> None:
    """With ``calls_batch_size=2`` and >2 calls, every edge must still land.

    Forces ``_flush_call_batches`` to issue multiple UNWIND/MERGE statements
    during the cold-index pass (threshold-triggered mid-loop and final). If
    the chunk slicing or buffer reset is wrong, edges go missing or duplicate.
    """
    fixture = tmp_path / "chunked"
    fixture.mkdir()
    (fixture / "lib.py").write_text(
        "def a(x):\n    return x\n\n"
        "def b(x):\n    return x\n\n"
        "def c(x):\n    return x\n\n"
        "def d(x):\n    return x\n\n"
        "def e(x):\n    return x\n",
        encoding="utf-8",
    )
    (fixture / "callers.py").write_text(
        "from lib import a, b, c, d, e\n\n"
        "def fan_out(n):\n"
        "    a(n)\n"
        "    b(n)\n"
        "    c(n)\n"
        "    d(n)\n"
        "    e(n)\n",
        encoding="utf-8",
    )

    config = Config(
        data_dir=tmp_path,
        falkordb_host="127.0.0.1",
        falkordb_port=6379,
        index_ignore=("__pycache__", ".git"),
        calls_batch_size=2,
    )
    manager = _manager_or_skip(config)

    try:
        builder = GraphBuilder(config, manager, JobManager())
        asyncio.run(builder.build_graph_from_path_async(fixture))

        driver = manager.get_driver()
        callers_path = str((fixture / "callers.py").resolve())
        lib_path = str((fixture / "lib.py").resolve())

        with driver.session() as session:
            row = _single(
                session,
                """
                MATCH (caller:Function {name: 'fan_out', path: $callers})
                      -[r:CALLS]->
                      (target:Function {path: $lib})
                RETURN count(r) AS c, count(DISTINCT target.name) AS distinct_names
                """,
                callers=callers_path,
                lib=lib_path,
            )
            assert row["c"] == 5
            assert row["distinct_names"] == 5
    finally:
        if manager._graph is not None:
            manager._graph.delete()
        manager.close_driver()
