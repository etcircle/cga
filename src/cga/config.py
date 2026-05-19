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

    The fields cover what the v1.0 engine and FalkorDB Lite layer need. Built
    at the process edge via :meth:`from_env`; never read ambiently inside
    engine code — components take a ``Config`` as a constructor argument.
    """

    data_dir: Path
    """Per-user data directory: FalkorDB store, sockets, per-repo graph state."""

    falkordb_socket_path: Path
    """Unix socket the FalkorDB Lite worker subprocess listens on."""

    falkordb_db_path: Path
    """On-disk path for the FalkorDB Lite store."""

    index_ignore: tuple[str, ...] = ()
    """Glob patterns for files/dirs to skip when indexing (cgcignore-style)."""

    @classmethod
    def from_env(cls, data_dir: Path | None = None) -> "Config":
        """Build a :class:`Config` from the environment with sane defaults.

        This is the ONLY place ambient state (environment variables) is read.
        Everything downstream takes the resulting ``Config`` explicitly.
        """
        root = data_dir or _default_data_dir()
        socket = os.environ.get("CGA_FALKORDB_SOCKET")
        db_path = os.environ.get("CGA_FALKORDB_DB")
        ignore = os.environ.get("CGA_INDEX_IGNORE", "")
        return cls(
            data_dir=root,
            falkordb_socket_path=Path(socket) if socket else root / "falkordb.sock",
            falkordb_db_path=Path(db_path) if db_path else root / "falkordb" / "cga.db",
            index_ignore=tuple(p.strip() for p in ignore.split(",") if p.strip()),
        )

    def ensure_dirs(self) -> None:
        """Create the data directories this config points at, if missing."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.falkordb_db_path.parent.mkdir(parents=True, exist_ok=True)
