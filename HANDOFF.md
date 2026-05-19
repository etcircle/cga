---
type: handoff
date: 2026-05-19
status: active
focus: CGA v1.1 — the deferred-from-v1.0 work + the cold-index optimization the di-copilot proving surfaced
---

# CGA — handoff after v1.0

**Status.** v1.0 is **SHIPPED**: `etcircle/cga` `main` @ `ca21b1a` (pushed
2026-05-19). 24 tests pass, `ruff` clean, `uv build` works, CI runs ruff +
pytest against a `falkordb/falkordb` service container. CGA cold-indexes a repo
into FalkorDB and serves four MCP tools — `find_symbol`, `find_callers`,
`find_references`, `index_status`. Proven on `cgc-fork`: 363 files in 65 s,
exact precision on hand-checked ground truth (`docs/v1.0-proving-run.md`).

**Backend — do not reintroduce embedded FalkorDB.** v1.0 connects to a FalkorDB
*server*; embedded `falkordblite` ships arm64-only macOS binaries that do not
run on this Intel Mac. Dev: the `cga-falkordb` container (colima + Docker).
On a fresh boot: `colima start && docker start cga-falkordb`. Reference:
`docs/adr/0002-falkordb-runs-as-a-server.md`.

**Run.** From `~/dev-workspaces/cga`: `uv run pytest` (live FalkorDB required);
`cga mcp` (or `python -m cga.mcp.server`) starts the stdio server for one repo
(CWD or `CGA_REPO`).

## v1.1 — ready to pick up, priority order

1. **(P1) Cold-index optimization.** The di-copilot scale test
   (5017 files / ~90 min, 0.9 files/s vs cgc-fork's 5.6) showed
   `_create_function_calls` is superlinear — per-call FalkorDB round-trips
   dominate. Batch the call resolution (UNWIND + `MERGE` for many CALLS edges
   per round-trip). v1.1's highest-leverage win.
2. **CLI client** — `cga query` / `cga show` alongside `cga mcp`. The query
   core is already a clean in-process library (`cga.query.QueryCore`).
3. **Rest of the tools** — `get_file_structure`, `code_search` (no fulltext on
   FalkorDB — needs its own match path), `get_module_overview`.
4. **`graph_query` escape-hatch** + the **ADR-0001 dialect IR** + a **Neo4j
   renderer**. The v1.0 render seam lives in `src/cga/query/builder.py`; slot
   the IR under it without touching `core.py`.
5. **Live `cga watch`** — `src/cga/engine/watcher.py` is carried but unwired;
   wiring it closes the v1.0 mtime-staleness gap (cross-file `CALLS` edges
   aren't currently refreshed when a file changes).

## Don't get burned

- The graph schema is **frozen and verified** (`docs/v1.0-graph-schema.md`).
  Any schema change re-opens the freeze + re-checks the query core.
- `Module` nodes are global, named after imports — they collide with real
  symbol names; that is why default `find_symbol` excludes `Module`.
- **Keep all Cypher in `query/builder.py`** — `core.py` must stay Cypher-free
  so v1.1's dialect IR slots in under the seam without touching call sites.
- **CC orchestrates, Hermes implements** the per-wave loop. It ran clean for
  v1.0 — five waves, every one caught a real review finding. Keep it.

## Kickoff prompt for the new session

```
Work in ~/dev-workspaces/cga. Read HANDOFF.md, docs/v1.0-proving-run.md, and
docs/adr/0002-falkordb-runs-as-a-server.md, then plan v1.1 starting from the
P1 cold-index optimization (the di-copilot finding) — propose the batching
design before implementing.
```
