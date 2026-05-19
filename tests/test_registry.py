from cga.tools.registry import ToolRegistry


SCHEMA = {"type": "object", "properties": {"value": {"type": "integer"}}}


def test_registry_register_get_list_and_dispatch() -> None:
    registry = ToolRegistry()

    def double(arguments):
        return {"status": "ok", "value": arguments["value"] * 2}

    registry.register("double", "Double a value.", SCHEMA, double)

    tool = registry.get("double")
    assert tool is not None
    assert tool.name == "double"
    assert tool.description == "Double a value."
    assert tool.input_schema == SCHEMA

    assert registry.list_tools() == [
        {"name": "double", "description": "Double a value.", "inputSchema": SCHEMA}
    ]
    assert registry.dispatch("double", {"value": 21}) == {"status": "ok", "value": 42}


def test_registry_rejects_duplicate_names() -> None:
    registry = ToolRegistry()
    registry.register("tool", "first", SCHEMA, lambda arguments: {"status": "ok"})

    try:
        registry.register("tool", "second", SCHEMA, lambda arguments: {"status": "ok"})
    except ValueError as exc:
        assert "Tool already registered" in str(exc)
    else:  # pragma: no cover - defensive assertion style keeps pytest dependency out of unit test
        raise AssertionError("duplicate registration should fail")


def test_registry_unknown_and_exception_are_result_dicts() -> None:
    registry = ToolRegistry()

    assert registry.dispatch("missing", {})["status"] == "error"

    def fails(arguments):
        raise RuntimeError("boom")

    registry.register("fails", "Fails.", SCHEMA, fails)
    result = registry.dispatch("fails", {})
    assert result["status"] == "error"
    assert result["warnings"] == ["boom"]
