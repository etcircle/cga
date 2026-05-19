# CGA Roadmap

## v1.0 — the proving milestone

Prove the architecture end-to-end: transplanted engine → FalkorDB Lite →
rewritten query core → MCP client → 3 read tools + `index_status`. Proven on a
small Python repo first (numeric pass/fail bars), then di-copilot as a scale
test.

Tasks (locked by /plan-eng-review, 2026-05-19). Build order is serial:
T1 → T2 → T3 → (freeze schema) → T4 → T5/T6/T7 → T8 → T9/T10.

- **T1** — sever the `get_config_value` config inversion; inject config at
  construction. `src/cga/config.py` is the foundation — scaffolded.
- **T2** — transplant the FalkorDB Lite layer (`database_falkordb.py`,
  `falkor_worker.py`); stand it up standalone; **freeze the graph schema**.
- **T3** — transplant the engine: tree-sitter parsers (Python + JS/TS) and the
  graph builder; index a repo into FalkorDB Lite.
- **T4** — rewrite `code_finder` as the query core: intent methods over a
  private builder emitting FalkorDB Cypher, behind a render seam. 3 v1.0
  intent methods.
- **T5** — build the `ToolRegistry` — one registration point, no handler tier.
- **T6** — build the MCP client; async cold-index flow; mtime-floor staleness.
- **T7** — build the `index_status` tool.
- **T8** — full v1.0 test suite (transplanted-parser verification, query-core
  units, MCP E2E, async-index, multi-client concurrency).
- **T9** — packaging + CI (uv/pip wheel, lint, tests, the `cga mcp` entry).
- **T10** — proving run against the numeric bars.

## v1.1 and later

CLI client, the `graph_query` escape-hatch tool, the dialect IR + a Neo4j
renderer, a standalone 24/7 watcher process, branch-aware index caching, a web
graph explorer, and architecture-health tools. None of this is in v1.0.

The full design history and rationale live in the gstack project artifacts
(`/office-hours` → `/improve-codebase-architecture` → `/plan-eng-review`,
2026-05-19).
