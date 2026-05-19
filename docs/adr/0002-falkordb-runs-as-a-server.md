# ADR-0002 — FalkorDB runs as a server (D7 revised)

- **Date:** 2026-05-19
- **Status:** Accepted
- **Supersedes:** decision **D7** (and amends **D6**) in
  `docs/v1.0-execution-plan.md`

## Context

The v1.0 plan (D6/D7) assumed FalkorDB Lite — the `falkordblite` embedded
package — would run as an auto-spawned worker subprocess on a unix socket, so
v1.0 needed no separate database server and no bespoke daemon.

Wave 1 / T2 falsified that assumption on the target machine:

- `falkordblite==0.10.0` ships **arm64-only** macOS binaries — both the bundled
  `redis-server` and `falkordb.so` are Mach-O arm64.
- The development machine is an **Intel Mac** (x86_64, Core i5-8500B). An arm64
  binary cannot execute on it; the worker dies with
  `OSError: [Errno 86] Bad CPU type in executable`.
- The CodeGraphContext fork's own venv never had `falkordblite` installed —
  embedded FalkorDB Lite was never actually run on this hardware. D6's claim
  that "FalkorDB Lite is the fork's tested embedded default" was untested on
  the target platform.

The fork already anticipated this: its `docker-compose.template.yml` documents
a `falkordb-remote` mode that connects to a stock `falkordb/falkordb`
container, "recommended for aarch64".

## Decision

**v1.0 connects to a FalkorDB *server* over the Redis/Cypher protocol. It does
not embed or auto-spawn one.**

- The server is the stock `falkordb/falkordb` container. In local development
  it runs via **colima** (a lightweight Docker runtime); in CI it runs as a
  service container.
- `cga.config.Config` carries `falkordb_host` / `falkordb_port`; the client
  (`FalkorDBManager`) connects with `FalkorDB(host, port)`.
- The embedded-worker path is **removed** from v1.0: `falkor_worker.py`, the
  worker-spawn logic in `database.py`, and the `falkordblite` dependency all go.
  v1.0 is uniformly server-mode — one connection path, no dual mode.

## What does NOT change

- The backend is still **FalkorDB**. The Redis/Cypher protocol, the graph
  schema (`docs/v1.0-graph-schema.md`), and the T4 query core are unaffected —
  a FalkorDB container and embedded FalkorDB Lite speak the identical protocol.
- D7's *intent* — "do not build a bespoke daemon" — still holds. The server is
  the stock FalkorDB image; CGA builds no daemon of its own. What changed is
  only that the graph store is a separate process the client connects to,
  rather than an in-process auto-spawned worker.

## Consequences

- Local dev and CI need a running FalkorDB container — a one-time setup step;
  v1.0 is no longer "zero-setup". The MCP client and `index_status` (T6/T7)
  must surface a clear "FalkorDB server unreachable" error, never a silent one.
- The "no manual setup" property in the v1.0 runtime contracts softens to
  "one container, started once".
- A truly-embedded, zero-setup backend (e.g. Kuzu) remains open for a later
  milestone. It was weighed against this option and declined for v1.0: it would
  pull the ADR-0001 dialect work forward. v1.0 keeps the FalkorDB/Cypher path
  intact and unchanged above the connection layer.
