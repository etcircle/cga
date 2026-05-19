"""Runtime configuration for CGA.

This module is the answer to the engine's old layering inversion. In the
CodeGraphContext fork, engine modules imported ``get_config_value`` from the
CLI layer (``cli/config_manager.py``) — the engine depended on the surface.

CGA inverts that: a :class:`Config` is built once, at the process edge
(:meth:`Config.from_env`), and **injected** into engine components through
their constructors. No engine module imports this one ambiently; every
component receives a ``Config`` explicitly.

See ROADMAP.md task T1.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _default_data_dir() -> Path:
    """Per-user CGA data directory (XDG-aware)."""
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return base / "cga"


@dataclass(frozen=True)
class Config:
    """Immutable runtime configuration, constructed once and injected.

    Built at the process edge via :meth:`from_env`; never read ambiently inside
    engine code — components take a ``Config`` as a constructor argument.
    """

    data_dir: Path
    """Per-user data directory for CGA state."""

    falkordb_host: str = "127.0.0.1"
    """FalkorDB server host."""

    falkordb_port: int = 6379
    """FalkorDB server port."""

    index_ignore: tuple[str, ...] = ()
    """Glob patterns for files/dirs to skip when indexing (cgcignore-style)."""

    @classmethod
    def from_env(cls, data_dir: Path | None = None) -> "Config":
        """Build a :class:`Config` from the environment with sane defaults.

        This is the ONLY place ambient state (environment variables) is read.
        Everything downstream takes the resulting ``Config`` explicitly.
        """
        root = data_dir or _default_data_dir()
        host = os.environ.get("CGA_FALKORDB_HOST", "127.0.0.1")
        port = int(os.environ.get("CGA_FALKORDB_PORT", "6379"))
        ignore = os.environ.get("CGA_INDEX_IGNORE", "")
        return cls(
            data_dir=root,
            falkordb_host=host,
            falkordb_port=port,
            index_ignore=tuple(p.strip() for p in ignore.split(",") if p.strip()),
        )

    def ensure_dirs(self) -> None:
        """Create the data directory this config points at, if missing."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
