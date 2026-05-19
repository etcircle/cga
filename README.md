# CodeGraphAgent (CGA)

An agent-first code-graph tool. CGA indexes a codebase into a graph and serves
it to coding agents over MCP — find symbols, references, callers — so an agent
gets an "X-ray view" of an unfamiliar codebase instead of grepping blind.

CGA is a slim, agent-first rebuild of
[CodeGraphContext](https://github.com/CodeGraphContext/CodeGraphContext): the
proven indexing engine is transplanted; the query and tool surface are
rewritten for agents. No human visualizer, no setup wizard — terse JSON, built
for the agent's runtime.

## Status

**v1.0 in progress.** See [ROADMAP.md](ROADMAP.md) for the locked v1.0 plan
(tasks T1–T10) and [CONTEXT.md](CONTEXT.md) for the domain vocabulary.

- **Backend:** FalkorDB Lite — an embedded graph store run as an auto-spawned
  worker subprocess on a unix socket. No external database to operate.
- **Surface:** an MCP client with 4 v1.0 tools — `find_symbol`,
  `find_references`, `find_callers`, `index_status`.
- A standalone CLI client, more tools, and a web graph explorer come after v1.0.

## Develop

```sh
uv sync --extra dev
uv run pytest
```

## License

MIT — see [LICENSE](LICENSE). Portions transplanted from CodeGraphContext (MIT).
