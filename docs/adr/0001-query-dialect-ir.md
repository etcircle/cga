# ADR-0001 — Query dialect handled by an IR with per-dialect renderers

**Status:** accepted. Applies to v1.1; v1.0 emits FalkorDB Cypher directly
behind a render seam.

## Context

The query core builds graph queries. CGA's default backend is FalkorDB Lite;
Neo4j is an optional second backend. The query builder is also intended to back
a future `graph_query` escape-hatch tool and the phase-2 explorer's query
console — i.e. it becomes a public surface, not just internal plumbing.

## Decision

The builder accumulates a dialect-neutral query representation (the IR); a
per-dialect renderer walks it to concrete Cypher. Chosen over a lighter
fragment-patching seam because the builder is also a public surface, so it
warrants a real IR rather than a patched single-dialect string.

Bounded by three rules so the IR cannot become an unbounded query compiler:

1. The common ~95% (match / filter / traverse / return) is shared structure
   that both renderers walk.
2. The ~5 genuinely divergent constructs (fulltext search, recursive paths,
   label access) are explicit IR nodes; each renderer handles them its own way.
3. A `RawFragment` IR node carries per-dialect literal strings as an escape
   hatch — non-negotiable; it keeps the IR from having to model every query.

## v1.0 scope

v1.0 ships a single backend (FalkorDB Lite) and no `graph_query` tool, so the
IR's justification does not yet apply. v1.0's builder emits FalkorDB Cypher
directly, behind a thin **render seam**; the IR and per-dialect renderers land
in v1.1 alongside `graph_query` and Neo4j support.

## Consequences

v1.1 inserts the IR layer under the render seam without changing the query
core's intent-method interface — call sites are untouched.
