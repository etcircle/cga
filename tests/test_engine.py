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
