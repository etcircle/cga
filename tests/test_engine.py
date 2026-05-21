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


def test_v1_2_add_file_batch_preserves_fixture_node_and_edge_counts(tmp_path: Path) -> None:
    """Cold-index of the shared ``sample_py`` fixture produces a known graph.

    The exact counts of File / Function / Class / Module / IMPORTS / CALLS /
    INHERITS / HAS_PARAMETER nodes and edges are pinned here so any future
    refactor of the batched add-file path that drops or duplicates a row
    fails this test instead of producing a silently wrong graph. The numbers
    come from running this test once against the v1.2 P1a implementation on
    the shared ``sample_py`` fixture and locking in what the implementation
    produces; they have to stay stable as the engine evolves.
    """
    config = _config(tmp_path)
    manager = _manager_or_skip(config)

    try:
        builder = GraphBuilder(config, manager, JobManager())
        asyncio.run(builder.build_graph_from_path_async(FIXTURE))

        driver = manager.get_driver()
        with driver.session() as session:
            # 4 .py files (main, helper, models, duplicate) + 1 auto-created
            # .cgcignore (which goes through add_minimal_file_node, not the
            # batched path).
            files_total = _single(session, "MATCH (f:File) RETURN count(f) AS c")
            assert files_total["c"] == 5
            py_files = _single(
                session,
                """
                MATCH (f:File)
                WHERE f.name ENDS WITH '.py'
                RETURN count(f) AS c
                """,
            )
            assert py_files["c"] == 4

            # 8 Functions: duplicate.py:call_helper, main.py:{call_helper,
            # make_child, use_math}, helper.py:{helper, extra},
            # models.py:{base_method, child_method}. The two call_helpers
            # are distinct nodes because (name, path, line_number) is the
            # uniqueness key — locking this count guards against the
            # cross-file dedup logic accidentally collapsing same-named
            # functions into one node.
            fn_row = _single(
                session,
                "MATCH (fn:Function) RETURN count(fn) AS c",
            )
            assert fn_row["c"] == 8

            cls_row = _single(
                session,
                "MATCH (c:Class) RETURN count(c) AS c, collect(DISTINCT c.name) AS names",
            )
            assert cls_row["c"] == 2
            assert set(cls_row["names"]) == {"Base", "Child"}

            inherits_row = _single(
                session,
                "MATCH ()-[r:INHERITS]->() RETURN count(r) AS c",
            )
            assert inherits_row["c"] == 1  # Child INHERITS Base.

            # CALLS: main.py:call_helper→helper.py:helper,
            # main.py:make_child→models.py:Child (Class fall-through via
            # COALESCE(tF, tInit, tC)), helper.py:extra→helper.py:helper,
            # models.py:child_method→models.py:base_method (self resolution).
            # main.py:use_math→math.sqrt is dropped because sqrt has no
            # node in the graph (external unresolved).
            calls_row = _single(
                session,
                "MATCH ()-[r:CALLS]->() RETURN count(r) AS c",
            )
            assert calls_row["c"] == 4

            # HAS_PARAMETER: every Function has its declared args wired to
            # Parameter nodes. 7 functions each have exactly 1 param; only
            # make_child has zero.
            has_param_row = _single(
                session,
                "MATCH ()-[r:HAS_PARAMETER]->() RETURN count(r) AS c",
            )
            assert has_param_row["c"] == 7

            # Modules + IMPORTS: main.py is the only file with imports
            # (math, helper, models). Loose floor — the exact set depends
            # on whether the parser emits an entry per `from X import Y`
            # row or one per module — but at minimum we expect the math
            # import to materialize.
            mod_row = _single(
                session,
                "MATCH (m:Module) RETURN count(m) AS c",
            )
            assert mod_row["c"] >= 1
            imports_row = _single(
                session,
                "MATCH ()-[r:IMPORTS]->() RETURN count(r) AS c",
            )
            assert imports_row["c"] >= 1

            # CONTAINS edges fan out across Repository, Files, and Classes
            # with methods. Floor check rather than exact because the
            # parsers vary in how they expose nested elements; the goal
            # here is to catch a refactor that drops the CONTAINS payload
            # entirely, not to pin every entry.
            contains_row = _single(
                session,
                "MATCH ()-[r:CONTAINS]->() RETURN count(r) AS c",
            )
            assert contains_row["c"] >= 10
    finally:
        if manager._graph is not None:
            manager._graph.delete()
        manager.close_driver()


def test_v1_2_name_only_fallback_lane_via_inscope_batch(tmp_path: Path) -> None:
    """Promoted from /tmp/cga_smoke_batching.py case 4 (the v1.1 anti-pattern).

    A Function call that doesn't resolve through imports or local scope falls
    into the ``OPTIONAL MATCH (fbF:Function {name: item.called_name}) WHERE
    exact IS NULL`` lane in ``_INSCOPE_BATCH_CYPHER``. If a Function with the
    same name exists anywhere in the graph, that lane creates the CALLS edge
    to it. v1.1 validated this in a /tmp smoke and discarded the test; v1.2
    P1 promotes it so the lane is covered before P1b restructures the query
    into separate exact and fallback buckets.
    """
    fixture = tmp_path / "fallback"
    fixture.mkdir()
    (fixture / "other.py").write_text(
        "def dangling():\n    return 1\n",
        encoding="utf-8",
    )
    (fixture / "caller.py").write_text(
        "def call_dangling():\n    dangling()\n",
        encoding="utf-8",
    )

    config = _config(tmp_path)
    manager = _manager_or_skip(config)

    try:
        builder = GraphBuilder(config, manager, JobManager())
        asyncio.run(builder.build_graph_from_path_async(fixture))

        driver = manager.get_driver()
        caller_path = str((fixture / "caller.py").resolve())
        other_path = str((fixture / "other.py").resolve())

        with driver.session() as session:
            row = _single(
                session,
                """
                MATCH (caller:Function {name: 'call_dangling', path: $caller})
                      -[r:CALLS]->
                      (target:Function {name: 'dangling', path: $other})
                RETURN count(r) AS c, r.full_call_name AS full_call_name
                """,
                caller=caller_path,
                other=other_path,
            )
            assert row["c"] == 1
            assert row["full_call_name"] == "dangling"
    finally:
        if manager._graph is not None:
            manager._graph.delete()
        manager.close_driver()


def test_v1_2_add_file_batch_chunks_across_multiple_flushes(tmp_path: Path) -> None:
    """With ``add_file_batch_size=2`` and 5 files, every node + CONTAINS lands.

    Forces ``_flush_file_batches`` to issue at least three mid-loop flushes
    (after files 2 and 4, plus a final force at end). If the buffer slicing,
    cross-file dedup of the directory walk, depth ordering, or per-label
    UNWIND payload is wrong, nodes go missing or CONTAINS edges break.
    """
    fixture = tmp_path / "chunked_add_file"
    fixture.mkdir()
    sub = fixture / "subdir"
    sub.mkdir()
    (fixture / "a.py").write_text(
        "def fa(x):\n    return x\nclass CA:\n    pass\n",
        encoding="utf-8",
    )
    (fixture / "b.py").write_text(
        "import os\ndef fb():\n    return os.getpid()\n",
        encoding="utf-8",
    )
    (fixture / "c.py").write_text(
        "def fc(x, y):\n    return x * y\n",
        encoding="utf-8",
    )
    (sub / "d.py").write_text(
        "def fd():\n    return 0\n",
        encoding="utf-8",
    )
    (sub / "e.py").write_text(
        "import json\ndef fe():\n    return json.dumps([])\n",
        encoding="utf-8",
    )

    config = Config(
        data_dir=tmp_path,
        falkordb_host="127.0.0.1",
        falkordb_port=6379,
        index_ignore=("__pycache__", ".git"),
        add_file_batch_size=2,
    )
    manager = _manager_or_skip(config)

    try:
        builder = GraphBuilder(config, manager, JobManager())
        asyncio.run(builder.build_graph_from_path_async(fixture))

        driver = manager.get_driver()
        sub_path = str(sub.resolve())
        repo_path = str(fixture.resolve())

        with driver.session() as session:
            # Every .py file materialized as a File node.
            files_row = _single(
                session,
                """
                MATCH (f:File)
                WHERE f.name ENDS WITH '.py'
                RETURN count(f) AS c
                """,
            )
            assert files_row["c"] == 5

            # Every Function landed via the batched per-label UNWIND.
            fn_row = _single(
                session,
                "MATCH (fn:Function) RETURN count(fn) AS c, collect(fn.name) AS names",
            )
            assert fn_row["c"] == 5
            assert set(fn_row["names"]) == {"fa", "fb", "fc", "fd", "fe"}

            # Class CA landed (class without methods exercises the per-label
            # path without the class_contains follow-up).
            class_row = _single(
                session,
                "MATCH (c:Class {name: 'CA'}) RETURN count(c) AS c",
            )
            assert class_row["c"] == 1

            # subdir Directory was deduped across d.py and e.py and CONTAINS
            # both of them. If the depth-walk dedup is broken, this fails.
            sub_dir_row = _single(
                session,
                """
                MATCH (d:Directory {path: $subpath})-[:CONTAINS]->(f:File)
                WHERE f.name IN ['d.py', 'e.py']
                RETURN count(f) AS c
                """,
                subpath=sub_path,
            )
            assert sub_dir_row["c"] == 2

            # Repository CONTAINS the three root .py files and the subdir.
            # (The auto-created .cgcignore is also under Repository but goes
            #  through add_minimal_file_node, not the batched path — counted
            #  separately so the assertion doesn't depend on that path.)
            top_py_row = _single(
                session,
                """
                MATCH (r:Repository {path: $repo})-[:CONTAINS]->(f:File)
                WHERE f.name ENDS WITH '.py'
                RETURN count(f) AS c
                """,
                repo=repo_path,
            )
            assert top_py_row["c"] == 3  # a.py, b.py, c.py
            top_subdir_row = _single(
                session,
                """
                MATCH (r:Repository {path: $repo})-[:CONTAINS]->(d:Directory)
                RETURN count(d) AS c
                """,
                repo=repo_path,
            )
            assert top_subdir_row["c"] == 1

            # IMPORTS edges land for both files that import (os from b.py,
            # json from e.py). If the per-language imports buffer split is
            # wrong, one of these vanishes.
            imports_row = _single(
                session,
                """
                MATCH (:File)-[r:IMPORTS]->(m:Module)
                WHERE m.name IN ['os', 'json']
                RETURN count(r) AS c, collect(DISTINCT m.name) AS mods
                """,
            )
            assert imports_row["c"] == 2
            assert set(imports_row["mods"]) == {"os", "json"}

            # HAS_PARAMETER edges land for fc(x, y) via the batched
            # parameters query.
            param_row = _single(
                session,
                """
                MATCH (fn:Function {name: 'fc'})-[:HAS_PARAMETER]->(p:Parameter)
                RETURN count(p) AS c, collect(p.name) AS names
                """,
            )
            assert param_row["c"] == 2
            assert set(param_row["names"]) == {"x", "y"}
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
