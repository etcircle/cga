"""Read-only intent methods for CGA's v1.0 query core."""

from __future__ import annotations

from typing import Any

from cga.db.database import FalkorDBManager
from cga.query.builder import CypherBuilder

QueryResponse = dict[str, list[dict[str, Any]] | str | list[str]]


class QueryCore:
    """Execute v1.0 read intents against an injected FalkorDB manager."""

    def __init__(self, db_manager: FalkorDBManager):
        self.db_manager = db_manager

    def find_symbol(
        self, name: str, kind: str | None = None, lang: str | None = None
    ) -> QueryResponse:
        try:
            cypher, params = CypherBuilder.symbol(name, kind=kind, lang=lang)
            rows = self._run(cypher, params)
        except Exception as exc:
            return self._error(exc)

        results = [
            {
                "name": row.get("name"),
                "kind": row.get("kind"),
                "path": row.get("path"),
                "line": row.get("line"),
                "lang": row.get("lang"),
            }
            for row in rows
        ]
        status = "empty" if not results else "ok" if len(results) == 1 else "ambiguous"
        return {"results": results, "status": status, "warnings": []}

    def find_callers(self, name: str, lang: str | None = None) -> QueryResponse:
        try:
            cypher, params = CypherBuilder.callers(name, lang=lang)
            rows = self._run(cypher, params)
        except Exception as exc:
            return self._error(exc)

        results = [
            {
                "caller": {
                    "name": row.get("caller_name"),
                    "kind": row.get("caller_kind"),
                    "path": row.get("caller_path"),
                    "line": row.get("caller_line"),
                },
                "call_site_line": row.get("call_site_line"),
                "target": {
                    "name": row.get("target_name"),
                    "path": row.get("target_path"),
                    "line": row.get("target_line"),
                },
            }
            for row in rows
        ]
        target_ids = {
            (row.get("target_kind"), row.get("target_name"), row.get("target_path"), row.get("target_line"))
            for row in rows
        }
        status = "empty" if not results else "ambiguous" if len(target_ids) > 1 else "ok"
        return {"results": results, "status": status, "warnings": []}

    def find_references(self, name: str, lang: str | None = None) -> QueryResponse:
        try:
            cypher, params = CypherBuilder.references(name, lang=lang)
            rows = self._run(cypher, params)
        except Exception as exc:
            return self._error(exc)

        results = [
            {
                "ref_kind": row.get("ref_kind"),
                "source": {
                    "name": row.get("source_name"),
                    "kind": row.get("source_kind"),
                    "path": row.get("source_path"),
                    "line": row.get("source_line"),
                },
                "target": {
                    "name": row.get("target_name"),
                    "kind": row.get("target_kind"),
                    "path": row.get("target_path"),
                    "line": row.get("target_line"),
                },
            }
            for row in rows
        ]
        status = "empty" if not results else "ok"
        return {"results": results, "status": status, "warnings": []}

    def _run(self, cypher: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        with self.db_manager.get_driver().session() as session:
            return session.run(cypher, **params).data()

    @staticmethod
    def _error(exc: Exception) -> QueryResponse:
        return {"results": [], "status": "error", "warnings": [str(exc)]}
