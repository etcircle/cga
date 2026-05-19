"""Single registration point for CGA tools."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

ToolFn = Callable[[dict[str, Any]], dict[str, Any]]


@dataclass(frozen=True)
class RegisteredTool:
    name: str
    description: str
    input_schema: dict[str, Any]
    fn: ToolFn


class ToolRegistry:
    """Name -> schema/function registry; intentionally no handler tier."""

    def __init__(self) -> None:
        self._tools: dict[str, RegisteredTool] = {}

    def register(
        self,
        name: str,
        description: str,
        input_schema: dict[str, Any],
        fn: ToolFn,
    ) -> None:
        if name in self._tools:
            raise ValueError(f"Tool already registered: {name}")
        self._tools[name] = RegisteredTool(name, description, input_schema, fn)

    def get(self, name: str) -> RegisteredTool | None:
        return self._tools.get(name)

    def list_tools(self) -> list[dict[str, Any]]:
        return [
            {
                "name": tool.name,
                "description": tool.description,
                "inputSchema": tool.input_schema,
            }
            for tool in self._tools.values()
        ]

    def dispatch(self, name: str, arguments: dict[str, Any] | None) -> dict[str, Any]:
        tool = self.get(name)
        if tool is None:
            return {"status": "error", "warnings": [f"Unknown tool: {name}"], "results": []}
        try:
            return tool.fn(arguments or {})
        except Exception as exc:
            return {"status": "error", "warnings": [str(exc)], "results": []}
