"""Cypher render seam for CGA's v1.0 query core.

This module is the only place in CGA's query core where concrete Cypher strings
are produced. v1.1 replaces these method bodies with a dialect-IR -> renderer
pipeline (ADR-0001) without changing ``core.py`` call sites.
"""

from __future__ import annotations

from typing import ClassVar


class CypherBuilder:
    """Build read-only FalkorDB Cypher for each v1.0 query intent."""

    SYMBOL_LABELS: ClassVar[dict[str, str]] = {
        "function": "Function",
        "class": "Class",
        "variable": "Variable",
        "interface": "Interface",
        "module": "Module",
    }

    @classmethod
    def symbol(cls, name: str, kind: str | None = None, lang: str | None = None) -> tuple[str, dict]:
        labels = cls._labels_for_kind(kind)
        parts = []
        for label in labels:
            kind_name = label.lower()
            parts.append(
                f"""
                MATCH (n:{label} {{name: $name}})
                WHERE $lang IS NULL OR n.lang = $lang
                RETURN n.name AS name, '{kind_name}' AS kind, n.path AS path,
                       n.line_number AS line, n.lang AS lang
                """
            )
        return cls._union(parts), {"name": name, "lang": lang}

    @classmethod
    def callers(cls, name: str, lang: str | None = None) -> tuple[str, dict]:
        cypher = """
        MATCH (caller)-[c:CALLS]->(target)
        WHERE (target:Function OR target:Class)
          AND target.name = $name
          AND ($lang IS NULL OR target.lang = $lang)
          AND (caller:File OR caller:Function OR caller:Class)
        RETURN DISTINCT
               caller.name AS caller_name,
               CASE
                   WHEN caller:File THEN 'file'
                   WHEN caller:Function THEN 'function'
                   WHEN caller:Class THEN 'class'
                   ELSE 'unknown'
               END AS caller_kind,
               caller.path AS caller_path,
               caller.line_number AS caller_line,
               c.line_number AS call_site_line,
               target.name AS target_name,
               target.path AS target_path,
               target.line_number AS target_line,
               CASE
                   WHEN target:Function THEN 'function'
                   WHEN target:Class THEN 'class'
                   ELSE 'unknown'
               END AS target_kind
        UNION
        MATCH (class_target:Class {name: $name})-[:CONTAINS]->(target:Function)
        WHERE target.name IN ['__init__', 'constructor']
          AND ($lang IS NULL OR class_target.lang = $lang)
        MATCH (caller)-[c:CALLS]->(target)
        WHERE caller:File OR caller:Function OR caller:Class
        RETURN DISTINCT
               caller.name AS caller_name,
               CASE
                   WHEN caller:File THEN 'file'
                   WHEN caller:Function THEN 'function'
                   WHEN caller:Class THEN 'class'
                   ELSE 'unknown'
               END AS caller_kind,
               caller.path AS caller_path,
               caller.line_number AS caller_line,
               c.line_number AS call_site_line,
               target.name AS target_name,
               target.path AS target_path,
               target.line_number AS target_line,
               'function' AS target_kind
        ORDER BY target_path, target_line, caller_path, caller_line, call_site_line
        """
        return cypher.strip(), {"name": name, "lang": lang}

    @classmethod
    def references(cls, name: str, lang: str | None = None) -> tuple[str, dict]:
        cypher = """
        MATCH (source)-[:CALLS]->(target)
        WHERE (target:Function OR target:Class)
          AND target.name = $name
          AND ($lang IS NULL OR target.lang = $lang)
          AND (source:File OR source:Function OR source:Class)
        RETURN DISTINCT
               'calls' AS ref_kind,
               source.name AS source_name,
               CASE
                   WHEN source:File THEN 'file'
                   WHEN source:Function THEN 'function'
                   WHEN source:Class THEN 'class'
                   ELSE 'unknown'
               END AS source_kind,
               source.path AS source_path,
               source.line_number AS source_line,
               target.name AS target_name,
               CASE
                   WHEN target:Function THEN 'function'
                   WHEN target:Class THEN 'class'
                   ELSE 'unknown'
               END AS target_kind,
               target.path AS target_path,
               target.line_number AS target_line
        UNION
        MATCH (class_target:Class {name: $name})-[:CONTAINS]->(target:Function)
        WHERE target.name IN ['__init__', 'constructor']
          AND ($lang IS NULL OR class_target.lang = $lang)
        MATCH (source)-[:CALLS]->(target)
        WHERE source:File OR source:Function OR source:Class
        RETURN DISTINCT
               'calls' AS ref_kind,
               source.name AS source_name,
               CASE
                   WHEN source:File THEN 'file'
                   WHEN source:Function THEN 'function'
                   WHEN source:Class THEN 'class'
                   ELSE 'unknown'
               END AS source_kind,
               source.path AS source_path,
               source.line_number AS source_line,
               target.name AS target_name,
               'function' AS target_kind,
               target.path AS target_path,
               target.line_number AS target_line
        UNION
        MATCH (source:Class)-[:INHERITS]->(target:Class {name: $name})
        WHERE $lang IS NULL OR target.lang = $lang
        RETURN DISTINCT
               'inherits' AS ref_kind,
               source.name AS source_name,
               'class' AS source_kind,
               source.path AS source_path,
               source.line_number AS source_line,
               target.name AS target_name,
               'class' AS target_kind,
               target.path AS target_path,
               target.line_number AS target_line
        UNION
        MATCH (source:Class)-[:IMPLEMENTS]->(target:Interface {name: $name})
        WHERE $lang IS NULL OR target.lang = $lang
        RETURN DISTINCT
               'implements' AS ref_kind,
               source.name AS source_name,
               'class' AS source_kind,
               source.path AS source_path,
               source.line_number AS source_line,
               target.name AS target_name,
               'interface' AS target_kind,
               target.path AS target_path,
               target.line_number AS target_line
        UNION
        MATCH (source:File)-[:IMPORTS]->(target:Module {name: $name})
        WHERE $lang IS NULL OR target.lang = $lang
        RETURN DISTINCT
               'imports' AS ref_kind,
               source.name AS source_name,
               'file' AS source_kind,
               source.path AS source_path,
               source.line_number AS source_line,
               target.name AS target_name,
               'module' AS target_kind,
               target.path AS target_path,
               target.line_number AS target_line
        ORDER BY ref_kind, target_path, target_line, source_path, source_line
        """
        return cypher.strip(), {"name": name, "lang": lang}

    @classmethod
    def _labels_for_kind(cls, kind: str | None) -> list[str]:
        if kind is None:
            # Module nodes represent imports in the frozen schema, not local definitions.
            # Keep default exact symbol lookup focused on defined code symbols; callers can
            # still ask for kind="module" explicitly.
            return ["Function", "Class", "Variable", "Interface"]
        label = cls.SYMBOL_LABELS.get(kind.lower())
        if label is None:
            raise ValueError(f"Unsupported symbol kind: {kind}")
        return [label]

    @staticmethod
    def _union(parts: list[str]) -> str:
        return "\nUNION\n".join(part.strip() for part in parts)
