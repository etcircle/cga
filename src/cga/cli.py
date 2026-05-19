"""CGA command-line entry point.

v1.0 builds the MCP client (``cga mcp``). The full standalone CLI client is
v1.1 (see ROADMAP.md).
"""

from __future__ import annotations

import argparse

from cga import __version__


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cga", description="CodeGraphAgent (CGA)")
    parser.add_argument("--version", action="version", version=f"cga {__version__}")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("mcp", help="run the MCP stdio server")

    args = parser.parse_args(argv)
    if args.command == "mcp":
        from cga.mcp.server import main as mcp_main

        mcp_main()
        return 0
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
