"""Incremental indexing coordinator (v1.3, design doc §5).

Wraps the warm whole-edge re-index primitive: parse changed files,
rebuild imports_map, replace changed nodes, wipe all cross-edges,
recreate from the complete cache. Provides a correctness floor that
MCP staleness refresh and the watcher can both call.
"""
from __future__ import annotations

from pathlib import Path

from cga.engine.graph_builder import GraphBuilder
from cga.utils.debug_log import info_logger, warning_logger


class IncrementalIndexCoordinator:
    """Per-repo coordinator. Owns an in-memory file_data + imports_map
    cache and exposes a single refresh_warm(changed_paths) entry point.
    """

    def __init__(self, graph_builder: GraphBuilder, repo_path: Path) -> None:
        self.graph_builder = graph_builder
        self.repo_path = repo_path.resolve()
        self._all_file_data: dict[str, dict] = {}
        self._imports_map: dict[str, list[str]] = {}
        self._cache_populated = False

    def _populate_cache(self) -> None:
        """Parse every supported file in the repo into the cache.
        Called lazily on first refresh_warm; subsequent refreshes
        update only the changed paths.
        """
        files = self.graph_builder.collect_indexable_file_paths(self.repo_path)
        supported = [f for f in files if f.suffix in self.graph_builder.parsers]
        self._all_file_data = {}
        for f in supported:
            data = self.graph_builder.parse_file(self.repo_path, f)
            if "error" not in data:
                self._all_file_data[str(f.resolve())] = data
        self._imports_map = self.graph_builder._pre_scan_for_imports(
            [Path(p) for p in self._all_file_data]
        )
        self._cache_populated = True
        info_logger(
            f"Coordinator cache populated: {len(self._all_file_data)} files"
        )

    def _wipe_cross_edges(self) -> None:
        # NOTE: assumes one-graph-per-repo (CGA's MCP model -- see
        # server.py:graph_name_for_repo). If a future caller ever shares a
        # single graph across repos (legacy CodeWatcher pattern), this wipe
        # must be scoped to nodes whose path is under self.repo_path.
        with self.graph_builder.driver.session() as session:
            session.run("MATCH ()-[r:CALLS]->() DELETE r")
            session.run("MATCH ()-[r:INHERITS]->() DELETE r")

    def refresh_warm(self, changed_paths: set[str]) -> None:
        """Warm whole-edge re-index. For each changed_path: re-parse if
        it exists, drop from cache if it doesn't. Replace node-level
        state via update_file_in_graph. Then wipe ALL cross-edges and
        recreate from the complete cache with is_cold_index=True (strict
        flush -- silent chunk failures re-raise).

        Correctness contract: after this call, the graph's CALLS +
        INHERITS == a fresh cold index of the current on-disk repo
        state.
        """
        if not self._cache_populated:
            self._populate_cache()

        # Apply changes to the file-data cache.
        for path_str in changed_paths:
            resolved = str(Path(path_str).resolve())
            p = Path(resolved)
            if p.exists() and p.is_file() and p.suffix in self.graph_builder.parsers:
                data = self.graph_builder.parse_file(self.repo_path, p)
                if "error" not in data:
                    self._all_file_data[resolved] = data
                else:
                    warning_logger(
                        f"Coordinator: parse error for {resolved}; dropping from cache"
                    )
                    self._all_file_data.pop(resolved, None)
            else:
                self._all_file_data.pop(resolved, None)

        # Rebuild imports_map from the current cache, ordered.
        self._imports_map = self.graph_builder._pre_scan_for_imports(
            [Path(p) for p in self._all_file_data]
        )

        # Replace node-level state for each changed path. Supported files
        # go through update_file_in_graph (delete + re-add). Unsupported
        # files get a minimal File node so warm refresh matches cold (cold
        # adds minimal nodes for unparseable files; update_file_in_graph
        # does not).
        for path_str in changed_paths:
            p = Path(path_str)
            resolved = str(p.resolve())
            if p.exists() and p.is_file():
                if p.suffix in self.graph_builder.parsers:
                    self.graph_builder.update_file_in_graph(
                        p, self.repo_path, self._imports_map
                    )
                else:
                    self.graph_builder.delete_file_from_graph(resolved)
                    self.graph_builder.add_minimal_file_node(p, self.repo_path)
            else:
                self.graph_builder.delete_file_from_graph(resolved)

        # Wipe all cross-edges and recreate from the complete cache,
        # strict mode (silent chunk failures re-raise -- design doc §2.2).
        self._wipe_cross_edges()
        all_data = list(self._all_file_data.values())
        self.graph_builder._create_all_function_calls(
            all_data, self._imports_map, is_cold_index=True
        )
        self.graph_builder._create_all_inheritance_links(
            all_data, self._imports_map
        )
        info_logger(
            f"Coordinator refresh_warm complete: {len(changed_paths)} changed, "
            f"{len(self._all_file_data)} cached"
        )
