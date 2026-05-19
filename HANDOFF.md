---
type: handoff
date: 2026-05-19
status: active
focus: CGA v1.0 build — run the per-wave loop starting at T2
---

# CGA v1.0 — session handoff

**What this is.** CodeGraphAgent (CGA): an agent-first code-graph tool, a fresh
rebuild of the CodeGraphContext fork. Planning is **done**, v1.0 is **locked**.
The last session created this repo and the T1 foundation. This session runs the
build loop.

**State.**
- Repo `etcircle/cga` (private), branch `main`, commit `f2da2d3` + this handoff.
- **Done:** scaffold + `src/cga/config.py` — the injected `Config` (T1
  foundation), 4 tests passing.
- **Authoritative plan:** `docs/v1.0-execution-plan.md` (tasks T1-T10).
  Test plan: `docs/v1.0-test-plan.md`. Vocabulary: `CONTEXT.md`. Tasks: `ROADMAP.md`.

**Next — run the standard per-wave loop, T2 onward.** Build order is **serial**.
Per wave: Hermes implements → peer review → fix nits → test → CC verifies → merge.
- **Wave 1 / T2** — transplant the FalkorDB Lite layer (`database_falkordb.py` +
  `falkor_worker.py` + closure) from `~/dev-workspaces/cgc-fork`; stand it up;
  **freeze the graph schema**.
- **Wave 2 / T3** — transplant the engine (tree-sitter parsers Python+JS/TS +
  `graph_builder`); sever the 4 `get_config_value` inversions onto
  `cga.config.Config` (this completes T1).
- **Wave 3 / T4** — rewrite the query core against the frozen schema.
- **Wave 4 / T5-T7** — ToolRegistry, MCP client, `index_status`.
- **Wave 5 / T8-T10** — tests, packaging + CI, proving run.

**Do not get burned.**
- **Kuzu is vaporware** in the fork — not installed, no tests, `database_kuzu.py`
  is a regex translator. Backend is **FalkorDB Lite**. Do not transplant
  `database_kuzu.py`.
- **No `cga` daemon.** FalkorDB Lite's worker IS the server. Clients run the
  query core in-process.
- **Freeze the FalkorDB schema** (end of T2/T3) **before** writing the query
  core (T4). Transplant and rewrite are serial, not parallel.
- **Proving target** is a small Python-only repo first with numeric bars, not
  di-copilot. di-copilot is a scale test only.
- **Do not edit the dead fork** `~/dev-workspaces/cgc-fork` — transplant *from*
  it (copy), never modify it.
- Heavy implementation (T2-T8) → **delegate to Hermes**; CC orchestrates,
  verifies, merges.

**Kickoff prompt — paste into the fresh session:**

```
Work in ~/dev-workspaces/cga. Read HANDOFF.md and docs/v1.0-execution-plan.md,
then run the standard per-wave loop for CGA v1.0 starting at Wave 1 (T2):
delegate implementation to Hermes, peer-review, test, parent-verify, merge.
```
