"""CGA command-line entry point.

v1.0 builds the MCP client (``cga mcp``). The full standalone CLI client is
v1.1 (see ROADMAP.md).
"""

from __future__ import annotations

import argparse
import sys

from cga import __version__


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cga", description="CodeGraphAgent (CGA)")
    parser.add_argument("--version", action="version", version=f"cga {__version__}")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("mcp", help="run the MCP server client (v1.0, in progress — task T6)")

    args = parser.parse_args(argv)
    if args.command == "mcp":
        print("cga mcp: not yet implemented — v1.0 task T6 (see ROADMAP.md)", file=sys.stderr)
        return 1
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
