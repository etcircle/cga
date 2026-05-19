# CONTEXT — CGA domain vocabulary

The shared language for CodeGraphAgent. Use these terms exactly in code,
comments, commits, and docs.

- **Engine** — the transplanted, unchanged indexing half: tree-sitter parsers
  plus the graph builder. Writes the graph; never queried directly.
- **Query core** — the rewritten read module: intent methods over a private
  query builder. Read-only; ignorant of indexing.
- **Render seam** — the point inside the builder where a query becomes concrete
  Cypher. v1.0 emits FalkorDB Cypher directly here; v1.1 slots a dialect IR in
  under the seam (see `docs/adr/0001-query-dialect-ir.md`).
- **Tool registry** — the single point mapping a tool name to its schema and
  function. No handler tier.
- **Client** — a thin adapter (the MCP client; later a CLI client) that holds
  no graph logic; it runs the query core in-process.
- **FalkorDB Lite worker** — the embedded graph store, run as an auto-spawned
  worker subprocess on a unix socket. The concurrency point: multiple `cga`
  client processes connect to it.
- **repo_id** — stable per-repo identity (git remote URL, else hashed repo
  path). One FalkorDB graph per `repo_id`.
- **Cold index** — the first full index of a repo. Runs async; the agent polls
  `index_status` until it completes.
- **Staleness floor** — the cheap per-query mtime scan that catches changes a
  watcher would miss. v1.0's only freshness mechanism (a live watcher is v1.1).
- **Config** — the immutable settings object built once at the process edge and
  injected into engine components. Engine code never reads ambient config.
