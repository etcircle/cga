"""CodeGraphAgent (CGA) — an agent-first code-graph tool.

CGA indexes a codebase into a graph (FalkorDB Lite) and serves it to coding
agents over MCP — find symbols, references, callers — so an agent gets an
"X-ray view" of an unfamiliar codebase instead of grepping blind.

CGA is a slim, agent-first rebuild of CodeGraphContext: the proven indexing
engine is transplanted; the query and tool surface are rewritten for agents.

See ROADMAP.md for the v1.0 plan and CONTEXT.md for the domain vocabulary.
"""

__version__ = "0.1.0.dev0"
