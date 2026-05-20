
# src/cga/engine/graph_builder.py
import asyncio
import os
import pathspec
import time
from pathlib import Path
from typing import Dict, Optional, Tuple
from datetime import datetime

from cga.config import Config
from cga.db.database import FalkorDBManager
from cga.jobs import JobManager, JobStatus
from cga.utils.debug_log import debug_log, info_logger, error_logger, warning_logger

# New imports for tree-sitter (using tree-sitter-language-pack)
from tree_sitter import Language, Parser
from cga.utils.tree_sitter_manager import get_tree_sitter_manager
 
DEFAULT_IGNORE_PATTERNS = [
    "*.png",
    "*.jpg",
    "*.jpeg",
    "*.gif",
    "*.svg",
    "*.mp4",
    "*.mp3",
    "*.zip",
    "*.tar",
    "*.gz",
]

class TreeSitterParser:
    """A generic parser wrapper for a specific language using tree-sitter."""

    def __init__(self, language_name: str):
        self.language_name = language_name
        self.ts_manager = get_tree_sitter_manager()
        
        # Get the language (cached) and create a new parser for this instance
        self.language: Language = self.ts_manager.get_language_safe(language_name)
        # In tree-sitter 0.25+, Parser takes language in constructor
        self.parser = Parser(self.language)

        self.language_specific_parser = None
        if self.language_name == "python":
            from .languages.python import PythonTreeSitterParser
            self.language_specific_parser = PythonTreeSitterParser(self)
        elif self.language_name == "javascript":
            from .languages.javascript import JavascriptTreeSitterParser
            self.language_specific_parser = JavascriptTreeSitterParser(self)
        elif self.language_name == "typescript":
            from .languages.typescript import TypescriptTreeSitterParser
            self.language_specific_parser = TypescriptTreeSitterParser(self)
        elif self.language_name == "typescriptjsx":
            from .languages.typescriptjsx import TypescriptJSXTreeSitterParser
            self.language_specific_parser = TypescriptJSXTreeSitterParser(self)




    def parse(self, path: Path, is_dependency: bool = False, **kwargs) -> Dict:
        """Dispatches parsing to the language-specific parser."""
        if self.language_specific_parser:
            return self.language_specific_parser.parse(path, is_dependency, **kwargs)
        else:
            raise NotImplementedError(f"No language-specific parser implemented for {self.language_name}")

class GraphBuilder:
    """Module for building and managing the Neo4j code graph."""

    # v1.1 cold-index batching — see docs/plans/v1.1-cold-index-batching.md.
    # Two templates, one per caller bucket. Both end in a single polymorphic
    # MERGE so each UNWIND item produces at most one CALLS edge, preserving
    # v1.0's mid-cascade short-circuit semantics without a round-trip cascade.
    # Note: the MERGE clause does not RETURN — FalkorDB's `count(r)` after
    # MERGE counts invocations, not new edges; relying on it for telemetry
    # is a footgun, so the templates stay write-only.
    _INSCOPE_BATCH_CYPHER = """
    UNWIND $batch AS item
    OPTIONAL MATCH (cF:Function {name: item.caller_name, path: item.caller_path})
    OPTIONAL MATCH (cC:Class    {name: item.caller_name, path: item.caller_path})
    WITH item, COALESCE(cF, cC) AS caller
    OPTIONAL MATCH (tF:Function {name: item.called_name, path: item.called_path})
    OPTIONAL MATCH (tC:Class    {name: item.called_name, path: item.called_path})
    OPTIONAL MATCH (tC)-[:CONTAINS]->(tInit:Function)
      WHERE tInit.name IN ['__init__', 'constructor']
    WITH item, caller, COALESCE(tF, tInit, tC) AS exact
    OPTIONAL MATCH (fbF:Function {name: item.called_name})
      WHERE exact IS NULL
    WITH item, caller, COALESCE(exact, fbF) AS called
    WHERE caller IS NOT NULL AND called IS NOT NULL
    MERGE (caller)-[:CALLS {
      line_number:    item.line_number,
      full_call_name: item.full_call_name,
      args:           item.args
    }]->(called)
    """

    _FILESCOPE_BATCH_CYPHER = """
    UNWIND $batch AS item
    OPTIONAL MATCH (caller:File {path: item.caller_path})
    OPTIONAL MATCH (tF:Function {name: item.called_name, path: item.called_path})
    OPTIONAL MATCH (tC:Class    {name: item.called_name, path: item.called_path})
    OPTIONAL MATCH (tC)-[:CONTAINS]->(tInit:Function)
      WHERE tInit.name IN ['__init__', 'constructor']
    WITH item, caller, COALESCE(tF, tInit, tC) AS exact
    OPTIONAL MATCH (fbF:Function {name: item.called_name})
      WHERE exact IS NULL
    WITH item, caller, COALESCE(exact, fbF) AS called
    WHERE caller IS NOT NULL AND called IS NOT NULL
    MERGE (caller)-[:CALLS {
      line_number:    item.line_number,
      full_call_name: item.full_call_name,
      args:           item.args
    }]->(called)
    """

    def __init__(self, config: Config, db_manager: FalkorDBManager, job_manager: JobManager):
        self.config = config
        self.db_manager = db_manager
        self.job_manager = job_manager
        self.driver = self.db_manager.get_driver()
        self.parsers = {
            ".py": TreeSitterParser("python"),
            ".ipynb": TreeSitterParser("python"),
            ".js": TreeSitterParser("javascript"),
            ".jsx": TreeSitterParser("javascript"),
            ".mjs": TreeSitterParser("javascript"),
            ".cjs": TreeSitterParser("javascript"),
            ".ts": TreeSitterParser("typescript"),
            ".tsx": TreeSitterParser("typescriptjsx"),
        }
        # Cross-file buffers populated by _create_function_calls and drained by
        # _flush_call_batches. Cleared/forced at the start/end of every
        # _create_all_function_calls invocation, including watcher/MCP deltas.
        self._inscope_buf: list[dict] = []
        self._filescope_buf: list[dict] = []
        self.create_schema()

    # A general schema creation based on common features across languages
    def create_schema(self):
        """Create constraints and indexes in Neo4j."""
        # When adding a new node type with a unique key, add its constraint here.
        with self.driver.session() as session:
            try:
                session.run("CREATE CONSTRAINT repository_path IF NOT EXISTS FOR (r:Repository) REQUIRE r.path IS UNIQUE")
                session.run("CREATE CONSTRAINT path IF NOT EXISTS FOR (f:File) REQUIRE f.path IS UNIQUE")
                session.run("CREATE CONSTRAINT directory_path IF NOT EXISTS FOR (d:Directory) REQUIRE d.path IS UNIQUE")
                session.run("CREATE CONSTRAINT function_unique IF NOT EXISTS FOR (f:Function) REQUIRE (f.name, f.path, f.line_number) IS UNIQUE")
                session.run("CREATE CONSTRAINT class_unique IF NOT EXISTS FOR (c:Class) REQUIRE (c.name, c.path, c.line_number) IS UNIQUE")
                session.run("CREATE CONSTRAINT interface_unique IF NOT EXISTS FOR (i:Interface) REQUIRE (i.name, i.path, i.line_number) IS UNIQUE")
                session.run("CREATE CONSTRAINT variable_unique IF NOT EXISTS FOR (v:Variable) REQUIRE (v.name, v.path, v.line_number) IS UNIQUE")
                session.run("CREATE CONSTRAINT module_name IF NOT EXISTS FOR (m:Module) REQUIRE m.name IS UNIQUE")
                session.run("CREATE CONSTRAINT parameter_unique IF NOT EXISTS FOR (p:Parameter) REQUIRE (p.name, p.path, p.function_line_number) IS UNIQUE")
                
                # Indexes for language attribute
                session.run("CREATE INDEX function_lang IF NOT EXISTS FOR (f:Function) ON (f.lang)")
                session.run("CREATE INDEX class_lang IF NOT EXISTS FOR (c:Class) ON (c.lang)")
                session.run("""
                    CREATE FULLTEXT INDEX code_search_index IF NOT EXISTS
                    FOR (n:Function|Class|Variable)
                    ON EACH [n.name, n.source, n.docstring]
                """)
                
                info_logger("Database schema verified/created successfully")
            except Exception as e:
                warning_logger(f"Schema creation warning: {e}")


    def _pre_scan_for_imports(self, files: list[Path]) -> dict:
        """Dispatches pre-scan to the v1.0 language-specific implementations."""
        imports_map = {}

        files_by_lang: dict[str, list[Path]] = {}
        for file in files:
            if file.suffix in self.parsers:
                files_by_lang.setdefault(file.suffix, []).append(file)

        if ".py" in files_by_lang:
            from .languages import python as python_lang_module
            imports_map.update(
                python_lang_module.pre_scan_python(files_by_lang[".py"], self.parsers[".py"])
            )
        if ".ipynb" in files_by_lang:
            from .languages import python as python_lang_module
            imports_map.update(
                python_lang_module.pre_scan_python(files_by_lang[".ipynb"], self.parsers[".ipynb"])
            )
        for ext in (".js", ".jsx", ".mjs", ".cjs"):
            if ext in files_by_lang:
                from .languages import javascript as js_lang_module
                imports_map.update(
                    js_lang_module.pre_scan_javascript(files_by_lang[ext], self.parsers[ext])
                )
        if ".ts" in files_by_lang:
            from .languages import typescript as ts_lang_module
            imports_map.update(
                ts_lang_module.pre_scan_typescript(files_by_lang[".ts"], self.parsers[".ts"])
            )
        if ".tsx" in files_by_lang:
            from .languages import typescriptjsx as tsx_lang_module
            imports_map.update(
                tsx_lang_module.pre_scan_typescript(files_by_lang[".tsx"], self.parsers[".tsx"])
            )

        return imports_map


    # Language-agnostic method
    def add_repository_to_graph(self, repo_path: Path, is_dependency: bool = False):
        """Adds a repository node using its absolute path as the unique key."""
        repo_name = repo_path.name
        repo_path_str = str(repo_path.resolve())
        with self.driver.session() as session:
            session.run(
                """
                MERGE (r:Repository {path: $path})
                SET r.name = $name, r.is_dependency = $is_dependency
                """,
                path=repo_path_str,
                name=repo_name,
                is_dependency=is_dependency,
            )

    # First pass to add file and its contents
    def add_file_to_graph(self, file_data: Dict, repo_name: str, imports_map: dict):
        calls_count = len(file_data.get('function_calls', []))
        debug_log(f"Executing add_file_to_graph for {file_data.get('path', 'unknown')} - Calls found: {calls_count}")
        """Adds a file and its contents within a single, unified session."""
        file_path_str = str(Path(file_data['path']).resolve())
        file_name = Path(file_path_str).name
        is_dependency = file_data.get('is_dependency', False)

        with self.driver.session() as session:
            try:
                # Match repository by path, not name, to avoid conflicts with same-named folders at different locations
                repo_result = session.run("MATCH (r:Repository {path: $repo_path}) RETURN r.path as path", repo_path=str(Path(file_data['repo_path']).resolve())).single()
                relative_path = str(Path(file_path_str).relative_to(Path(repo_result['path']))) if repo_result else file_name
            except ValueError:
                relative_path = file_name

            session.run("""
                MERGE (f:File {path: $path})
                SET f.name = $name, f.relative_path = $relative_path, f.is_dependency = $is_dependency
            """, path=file_path_str, name=file_name, relative_path=relative_path, is_dependency=is_dependency)

            file_path_obj = Path(file_path_str)
            if repo_result:
                repo_path_obj = Path(repo_result['path'])
            else:
                # Fallback to the path we queried for
                warning_logger(f"Repository node not found for {file_data['repo_path']} during indexing of {file_name}. Using original path.")
                repo_path_obj = Path(file_data['repo_path']).resolve()
            
            relative_path_to_file = file_path_obj.relative_to(repo_path_obj)
            
            parent_path = str(repo_path_obj)
            parent_label = 'Repository'

            for part in relative_path_to_file.parts[:-1]:
                current_path = Path(parent_path) / part
                current_path_str = str(current_path)
                
                session.run(f"""
                    MATCH (p:{parent_label} {{path: $parent_path}})
                    MERGE (d:Directory {{path: $current_path}})
                    SET d.name = $part
                    MERGE (p)-[:CONTAINS]->(d)
                """, parent_path=parent_path, current_path=current_path_str, part=part)

                parent_path = current_path_str
                parent_label = 'Directory'

            session.run(f"""
                MATCH (p:{parent_label} {{path: $parent_path}})
                MATCH (f:File {{path: $path}})
                MERGE (p)-[:CONTAINS]->(f)
            """, parent_path=parent_path, path=file_path_str)

            # CONTAINS relationships for functions, classes, variables, etc.
            # Batch writes per label instead of issuing one query per node.
            item_mappings = [
                (file_data.get('functions', []), 'Function'),
                (file_data.get('classes', []), 'Class'),
                (file_data.get('variables', []), 'Variable'),
                (file_data.get('interfaces', []), 'Interface'),
            ]
            for item_data, label in item_mappings:
                items_payload = []
                for item in item_data:
                    item_props = dict(item)
                    if label == 'Function' and 'cyclomatic_complexity' not in item_props:
                        item_props['cyclomatic_complexity'] = 1
                    items_payload.append({
                        'path': file_path_str,
                        'name': item['name'],
                        'line_number': item['line_number'],
                        'props': item_props,
                    })

                if items_payload:
                    session.run(f"""
                        MATCH (f:File {{path: $path}})
                        UNWIND $items as item
                        MERGE (n:{label} {{name: item.name, path: item.path, line_number: item.line_number}})
                        SET n += item.props
                        MERGE (f)-[:CONTAINS]->(n)
                    """, path=file_path_str, items=items_payload)

            function_parameters = [
                {
                    'func_name': func['name'],
                    'line_number': func['line_number'],
                    'arg_name': arg_name,
                }
                for func in file_data.get('functions', [])
                for arg_name in func.get('args', [])
            ]
            if function_parameters:
                session.run("""
                    UNWIND $params as param
                    MATCH (fn:Function {name: param.func_name, path: $path, line_number: param.line_number})
                    MERGE (p:Parameter {name: param.arg_name, path: $path, function_line_number: param.line_number})
                    MERGE (fn)-[:HAS_PARAMETER]->(p)
                """, path=file_path_str, params=function_parameters)

            modules_payload = [{'name': mod['name']} for mod in file_data.get('modules', []) if mod.get('name')]
            if modules_payload:
                session.run("""
                    UNWIND $modules as module_item
                    MERGE (mod:Module {name: module_item.name})
                    ON CREATE SET mod.lang = $lang
                    ON MATCH SET mod.lang = coalesce(mod.lang, $lang)
                """, modules=modules_payload, lang=file_data.get('lang'))

            nested_functions = [
                {
                    'context': item['context'],
                    'name': item['name'],
                    'line_number': item['line_number'],
                }
                for item in file_data.get('functions', [])
                if item.get('context_type') == 'function_definition' and item.get('context')
            ]
            if nested_functions:
                session.run("""
                    UNWIND $nested_functions as rel
                    MATCH (outer:Function {name: rel.context, path: $path})
                    MATCH (inner:Function {name: rel.name, path: $path, line_number: rel.line_number})
                    MERGE (outer)-[:CONTAINS]->(inner)
                """, path=file_path_str, nested_functions=nested_functions)

            lang = file_data.get('lang')
            if lang == 'javascript':
                javascript_imports = []
                for imp in file_data.get('imports', []):
                    module_name = imp.get('source')
                    if not module_name:
                        continue
                    rel_props = {'imported_name': imp.get('name', '*')}
                    if imp.get('alias'):
                        rel_props['alias'] = imp.get('alias')
                    if imp.get('line_number'):
                        rel_props['line_number'] = imp.get('line_number')
                    javascript_imports.append({'module_name': module_name, 'rel_props': rel_props})

                if javascript_imports:
                    session.run("""
                        MATCH (f:File {path: $path})
                        UNWIND $imports as imp
                        MERGE (m:Module {name: imp.module_name})
                        MERGE (f)-[r:IMPORTS]->(m)
                        SET r += imp.rel_props
                    """, path=file_path_str, imports=javascript_imports)
            else:
                imports_payload = []
                for imp in file_data.get('imports', []):
                    rel_props = {}
                    if imp.get('line_number'):
                        rel_props['line_number'] = imp.get('line_number')
                    if imp.get('alias'):
                        rel_props['alias'] = imp.get('alias')
                    imports_payload.append({
                        'module_name': imp.get('name'),
                        'full_import_name': imp.get('full_import_name'),
                        'rel_props': rel_props,
                    })

                if imports_payload:
                    session.run("""
                        MATCH (f:File {path: $path})
                        UNWIND $imports as imp
                        WITH f, imp
                        WHERE imp.module_name IS NOT NULL
                        MERGE (m:Module {name: imp.module_name})
                        SET m.full_import_name = coalesce(imp.full_import_name, m.full_import_name)
                        MERGE (f)-[r:IMPORTS]->(m)
                        SET r += imp.rel_props
                    """, path=file_path_str, imports=imports_payload)

            class_contains_payload = [
                {
                    'class_name': func['class_context'],
                    'func_name': func['name'],
                    'func_line': func['line_number'],
                }
                for func in file_data.get('functions', [])
                if func.get('class_context')
            ]
            if class_contains_payload:
                session.run("""
                    UNWIND $contains_rows as rel
                    MATCH (c:Class {name: rel.class_name, path: $path})
                    MATCH (fn:Function {name: rel.func_name, path: $path, line_number: rel.func_line})
                    MERGE (c)-[:CONTAINS]->(fn)
                """, path=file_path_str, contains_rows=class_contains_payload)

            # Class inheritance is handled in a separate pass after all files are processed.
            # Function calls are also handled in a separate pass after all files are processed.

    # Second pass to create relationships that depend on all files being present like call functions and class inheritance
    def _safe_run_create(self, session, query, params) -> bool:
        """Run a creation query and return whether it created/matched rows."""
        try:
            result = session.run(query, **params)
            row = result.single()
            return row is not None and row.get("created", 0) > 0
        except Exception as e:
            warning_logger(f"Failed to create graph relationship: {e}")
            return False


    def _create_function_calls(self, file_data: Dict, imports_map: dict):
        """Buffer CALLS-edge specs for the next batch flush.

        v1.0 issued up to 5 sequential MERGE round-trips per call site through a
        Function/Class/File label cascade. v1.1 resolves each call to a single
        (caller_label-agnostic, callee_label-agnostic) spec in pure Python and
        appends it to a cross-file buffer; the polymorphic UNWIND/MERGE in
        :meth:`_flush_call_batches` then collapses all of v1.0's cascade
        branches into one Cypher statement per chunk. The resolution algorithm
        below is unchanged — the only difference is the persistence path.
        """
        caller_file_path = str(Path(file_data['path']).resolve())
        num_calls = len(file_data.get('function_calls', []))
        if num_calls > 0:
            debug_log(f"Buffering function calls for {caller_file_path} (Count: {num_calls})")
        
        local_names = {f['name'] for f in file_data.get('functions', [])} | \
                      {c['name'] for c in file_data.get('classes', [])}
        local_imports = {imp.get('alias') or imp['name'].split('.')[-1]: imp['name'] 
                        for imp in file_data.get('imports', [])}
        
        # Check if we should skip external resolution attempts - 
        skip_external = self.config.skip_external_resolution
        
        for call in file_data.get('function_calls', []):
            called_name = call['name']
            # debug_log(f"Processing call: {called_name}")
            if called_name in __builtins__:
                continue

            resolved_path = None
            full_call = call.get('full_name', called_name)
            base_obj = full_call.split('.')[0] if '.' in full_call else None
            
            # For chained calls like self.graph_builder.method(), we need to look up 'method'
            # For direct calls like self.method(), we can use the caller's file
            is_chained_call = full_call.count('.') > 1 if '.' in full_call else False
            
            # Determine the lookup name:
            # - For chained calls (self.attr.method), use the actual method name
            # - For direct calls (self.method or module.function), use the base object
            if is_chained_call and base_obj in ('self', 'this', 'super', 'super()', 'cls', '@'):
                lookup_name = called_name  # Use the actual method name for lookup
            else:
                lookup_name = base_obj if base_obj else called_name

            # 1. Check for local context keywords/direct local names
            # Only resolve to caller_file_path for DIRECT self/this calls, not chained ones
            if base_obj in ('self', 'this', 'super', 'super()', 'cls', '@') and not is_chained_call:
                resolved_path = caller_file_path
            elif lookup_name in local_names:
                resolved_path = caller_file_path
            
            # 2. Check inferred type if available
            elif call.get('inferred_obj_type'):
                obj_type = call['inferred_obj_type']
                possible_paths = imports_map.get(obj_type, [])
                if len(possible_paths) > 0:
                    resolved_path = possible_paths[0]
            
            # 3. Check imports map with validation against local imports
            if not resolved_path:
                possible_paths = imports_map.get(lookup_name, [])
                if len(possible_paths) == 1:
                    resolved_path = possible_paths[0]
                elif len(possible_paths) > 1:
                    if lookup_name in local_imports:
                        full_import_name = local_imports[lookup_name]
                        
                        # Optimization: Check if the FQN is directly in imports_map (from pre-scan)
                        if full_import_name in imports_map:
                             direct_paths = imports_map[full_import_name]
                             if direct_paths and len(direct_paths) == 1:
                                 resolved_path = direct_paths[0]
                        
                        if not resolved_path:
                            for path in possible_paths:
                                if full_import_name.replace('.', '/') in path:
                                    resolved_path = path
                                    break
            
            if not resolved_path:
                # Only log warning if we're not skipping external resolution
                if not skip_external:
                    warning_logger(f"Could not resolve call {called_name} (lookup: {lookup_name}) in {caller_file_path}")
                # Track that this was an unresolved external call
                is_unresolved_external = True
            else:
                is_unresolved_external = False
            # else:
            #      info_logger(f"Resolved call {called_name} -> {resolved_path}")
            
            # Legacy fallback block (was mis-indented)
            if not resolved_path:
                possible_paths = imports_map.get(lookup_name, [])
                if len(possible_paths) > 0:
                     # Final fallback: global candidate
                     # Check if it was imported explicitly, otherwise risky
                     if lookup_name in local_imports:
                         # We already tried specific matching above, but if we are here
                         # it means we had ambiguity without matching path?
                         pass
                     else:
                        # Fallback to first available if not imported? Or skip?
                        # Original logic: resolved_path = possible_paths[0]
                        # But wait, original code logic was:
                        pass
            if not resolved_path:
                if called_name in local_names:
                    resolved_path = caller_file_path
                    is_unresolved_external = False  # This is a local call, not external
                elif called_name in imports_map and imports_map[called_name]:
                    # Check if any path in imports_map for called_name matches current file's imports
                    candidates = imports_map[called_name]
                    for path in candidates:
                        for imp_name in local_imports.values():
                            if imp_name.replace('.', '/') in path:
                                resolved_path = path
                                is_unresolved_external = False  # Found a match
                                break
                        if resolved_path:
                            break
                    if not resolved_path:
                        resolved_path = candidates[0]
                else:
                    resolved_path = caller_file_path
            
            # Skip creating CALLS relationship for unresolved external calls when skip_external is enabled
            if skip_external and is_unresolved_external:
                continue

            # Buffer the spec; the polymorphic UNWIND/MERGE in
            # _flush_call_batches handles every label combination v1.0's
            # cascade used to handle one round-trip at a time.
            spec = {
                "caller_path": caller_file_path,
                "called_name": called_name,
                "called_path": resolved_path,
                "line_number": call['line_number'],
                "args": call.get('args', []),
                "full_call_name": call.get('full_name', called_name),
            }
            caller_context = call.get('context')
            if caller_context and len(caller_context) == 3 and caller_context[0] is not None:
                caller_name, _, _caller_line_number = caller_context
                spec["caller_name"] = caller_name
                self._inscope_buf.append(spec)
            else:
                # File-level caller: no caller_name; the file-scope batch
                # template matches (:File) by caller_path directly.
                self._filescope_buf.append(spec)

    def _emit_phase_profile(
        self, phase_total: dict[str, float], elapsed: float, file_count: int
    ) -> None:
        """Print a phase-time breakdown for a cold-index pass.

        Output goes straight to stderr so it shows up regardless of how the
        host configures Python's logging level — this is an ops diagnostic,
        not an application log line. Format: one row per phase with seconds,
        share of total, and ms/file; a trailing row sums the unaccounted gap.
        """
        if not phase_total:
            return
        import sys as _sys

        lines = ["=== cold-index phase profile ==="]
        lines.append(f"  files indexed: {file_count}")
        lines.append(f"  wall time:     {elapsed:.2f} s")
        for name in (
            "repository_node",
            "discovery",
            "pre_scan_imports",
            "parse_file",
            "add_file_to_graph",
            "minimal_file_node",
            "inheritance_links",
            "function_calls",
        ):
            sec = phase_total.get(name, 0.0)
            if sec == 0.0:
                continue
            share = (sec / elapsed * 100.0) if elapsed > 0 else 0.0
            per_file = (sec * 1000.0 / file_count) if file_count > 0 else 0.0
            lines.append(f"  {name:22} {sec:8.2f} s  {share:5.1f}%  {per_file:7.2f} ms/file")
        accounted = sum(phase_total.values())
        unaccounted = max(0.0, elapsed - accounted)
        if elapsed > 0:
            lines.append(
                f"  {'unaccounted':22} {unaccounted:8.2f} s  {unaccounted / elapsed * 100.0:5.1f}%"
            )
        _sys.stderr.write("\n".join(lines) + "\n")
        _sys.stderr.flush()

    def _flush_call_batches(self, session, *, force: bool = False) -> None:
        """Drain the CALLS-edge buffers into FalkorDB.

        Each bucket is flushed in chunks of ``Config.calls_batch_size`` via a
        single polymorphic UNWIND/MERGE per chunk. With ``force=False`` only
        chunks of at least one full ``calls_batch_size`` are sent (called
        between files during cold indexing to bound memory); ``force=True``
        drains everything that remains, including a partial last chunk
        (called once at the end of :meth:`_create_all_function_calls`).
        """
        chunk = self.config.calls_batch_size
        while self._inscope_buf and (force or len(self._inscope_buf) >= chunk):
            head = self._inscope_buf[:chunk]
            del self._inscope_buf[:chunk]
            try:
                session.run(self._INSCOPE_BATCH_CYPHER, batch=head)
            except Exception as exc:
                warning_logger(
                    f"In-scope CALLS batch flush failed (size={len(head)}): {exc}"
                )
        while self._filescope_buf and (force or len(self._filescope_buf) >= chunk):
            head = self._filescope_buf[:chunk]
            del self._filescope_buf[:chunk]
            try:
                session.run(self._FILESCOPE_BATCH_CYPHER, batch=head)
            except Exception as exc:
                warning_logger(
                    f"File-scope CALLS batch flush failed (size={len(head)}): {exc}"
                )

    def _create_all_function_calls(self, all_file_data: list[Dict], imports_map: dict):
        """Create CALLS relationships for all functions after all files have been processed.

        Two-phase: per-file resolution + threshold flush, then a final force
        flush. The buffers are cleared at entry so MCP single-file deltas and
        the watcher's incremental refreshes never inherit state from a prior
        call.
        """
        debug_log(f"_create_all_function_calls called with {len(all_file_data)} files")
        self._inscope_buf.clear()
        self._filescope_buf.clear()
        with self.driver.session() as session:
            for idx, file_data in enumerate(all_file_data):
                debug_log(f"Processing file {idx+1}/{len(all_file_data)}: {file_data.get('path', 'unknown')}")
                self._create_function_calls(file_data, imports_map)
                self._flush_call_batches(session, force=False)
            self._flush_call_batches(session, force=True)

    def _create_inheritance_links(self, session, file_data: Dict, imports_map: dict):
        """Create INHERITS relationships with a more robust resolution logic."""
        caller_file_path = str(Path(file_data['path']).resolve())
        local_class_names = {c['name'] for c in file_data.get('classes', [])}
        # Create a map of local import aliases/names to full import names
        local_imports = {imp.get('alias') or imp['name'].split('.')[-1]: imp['name']
                         for imp in file_data.get('imports', [])}

        for class_item in file_data.get('classes', []):
            if not class_item.get('bases'):
                continue

            for base_class_str in class_item['bases']:
                if base_class_str == 'object':
                    continue

                resolved_path = None
                target_class_name = base_class_str.split('.')[-1]

                # Handle qualified names like module.Class or alias.Class
                if '.' in base_class_str:
                    lookup_name = base_class_str.split('.')[0]
                    
                    # Case 1: The prefix is a known import
                    if lookup_name in local_imports:
                        full_import_name = local_imports[lookup_name]
                        possible_paths = imports_map.get(target_class_name, [])
                        # Find the path that corresponds to the imported module
                        for path in possible_paths:
                            if full_import_name.replace('.', '/') in path:
                                resolved_path = path
                                break
                # Handle simple names
                else:
                    lookup_name = base_class_str
                    # Case 2: The base class is in the same file
                    if lookup_name in local_class_names:
                        resolved_path = caller_file_path
                    # Case 3: The base class was imported directly (e.g., from module import Parent)
                    elif lookup_name in local_imports:
                        full_import_name = local_imports[lookup_name]
                        possible_paths = imports_map.get(target_class_name, [])
                        for path in possible_paths:
                            if full_import_name.replace('.', '/') in path:
                                resolved_path = path
                                break
                    # Case 4: Fallback to global map (less reliable)
                    elif lookup_name in imports_map:
                        possible_paths = imports_map[lookup_name]
                        if len(possible_paths) == 1:
                            resolved_path = possible_paths[0]
                
                # If a path was found, create the relationship
                if resolved_path:
                    session.run("""
                        MATCH (child:Class {name: $child_name, path: $path})
                        MATCH (parent:Class {name: $parent_name, path: $resolved_parent_file_path})
                        MERGE (child)-[:INHERITS]->(parent)
                    """,
                    child_name=class_item['name'],
                    path=caller_file_path,
                    parent_name=target_class_name,
                    resolved_parent_file_path=resolved_path)


    def _create_all_inheritance_links(self, all_file_data: list[Dict], imports_map: dict):
        """Create INHERITS relationships for all classes after all files have been processed."""
        with self.driver.session() as session:
            for file_data in all_file_data:
                self._create_inheritance_links(session, file_data, imports_map)


    def delete_file_from_graph(self, path: str):
        """Deletes a file and all its contained elements and relationships."""
        file_path_str = str(Path(path).resolve())
        with self.driver.session() as session:
            parents_res = session.run("""
                MATCH (f:File {path: $path})<-[:CONTAINS*]-(d:Directory)
                RETURN d.path as path ORDER BY d.path DESC
            """, path=file_path_str)
            parent_paths = [record["path"] for record in parents_res]

            session.run(
                """
                MATCH (f:File {path: $path})
                OPTIONAL MATCH (f)-[:CONTAINS]->(element)
                DETACH DELETE f, element
                """,
                path=file_path_str,
            )
            info_logger(f"Deleted file and its elements from graph: {file_path_str}")

            for path in parent_paths:
                session.run("""
                    MATCH (d:Directory {path: $path})
                    WHERE NOT (d)-[:CONTAINS]->()
                    DETACH DELETE d
                """, path=path)

    def delete_repository_from_graph(self, repo_path: str) -> bool:
        """Deletes a repository and all its contents from the graph. Returns True if deleted, False if not found."""
        repo_path_str = str(Path(repo_path).resolve())
        with self.driver.session() as session:
            # Check if it exists
            result = session.run("MATCH (r:Repository {path: $path}) RETURN count(r) as cnt", path=repo_path_str).single()
            if not result or result["cnt"] == 0:
                warning_logger(f"Attempted to delete non-existent repository: {repo_path_str}")
                return False

            session.run("""MATCH (r:Repository {path: $path})
                          OPTIONAL MATCH (r)-[:CONTAINS*]->(e)
                          DETACH DELETE r, e""", path=repo_path_str)
            info_logger(f"Deleted repository and its contents from graph: {repo_path_str}")
            return True

    def delete_edges_for_file(self, file_path: str):
        """Delete all CALLS and INHERITS edges originating FROM nodes in this file."""
        with self.driver.session() as session:
            session.run("""
                MATCH (n {path: $path})-[r:CALLS]->()
                DELETE r
            """, path=file_path)
            session.run("""
                MATCH (n {path: $path})-[r:INHERITS]->()
                DELETE r
            """, path=file_path)

    def acquire_repo_lock(self, repo_path: str, ttl_minutes: int = 5) -> bool:
        """Set a lock node in Neo4j to prevent concurrent index + watch races.
        Uses a TTL so crashed processes don't hold the lock forever."""
        with self.driver.session() as session:
            # First, clean up any expired locks
            session.run("""
                MATCH (lock:_Lock {path: $path})
                WHERE lock.locked_at < datetime() - duration({minutes: $ttl})
                DELETE lock
            """, path=repo_path, ttl=ttl_minutes)

            result = session.run("""
                MERGE (lock:_Lock {path: $path})
                ON CREATE SET lock.locked_at = datetime(), lock.pid = $pid
                ON MATCH SET lock._exists = true
                RETURN lock.pid as holder_pid, lock.locked_at as locked_at
            """, path=repo_path, pid=os.getpid())
            record = result.single()
            return record is not None and record["holder_pid"] == os.getpid()

    def release_repo_lock(self, repo_path: str):
        """Release the advisory lock for a repo path."""
        with self.driver.session() as session:
            session.run("MATCH (lock:_Lock {path: $path, pid: $pid}) DELETE lock",
                        path=repo_path, pid=os.getpid())

    def update_file_in_graph(self, path: Path, repo_path: Path, imports_map: dict):
        """Updates a single file's nodes in the graph."""
        file_path_str = str(path.resolve())
        repo_name = repo_path.name
        
        self.delete_file_from_graph(file_path_str)

        if path.exists():
            file_data = self.parse_file(repo_path, path)
            
            if "error" not in file_data:
                self.add_file_to_graph(file_data, repo_name, imports_map)
                return file_data
            else:
                error_logger(f"Skipping graph add for {file_path_str} due to parsing error: {file_data['error']}")
                return None
        else:
            return {"deleted": True, "path": file_path_str}

    def parse_file(self, repo_path: Path, path: Path, is_dependency: bool = False) -> Dict:
        """Parses a file with the appropriate language parser and extracts code elements."""
        parser = self.parsers.get(path.suffix)
        if not parser:
            warning_logger(f"No parser found for file extension {path.suffix}. Skipping {path}")
            return {"path": str(path), "error": f"No parser for {path.suffix}"}

        debug_log(f"[parse_file] Starting parsing for: {path} with {parser.language_name} parser")
        try:
            index_source = self.config.index_source
            if parser.language_name == 'python':
                is_notebook = path.suffix == '.ipynb'
                file_data = parser.parse(
                    path,
                    is_dependency,
                    is_notebook=is_notebook,
                    index_source=index_source
                )
            else:
                file_data = parser.parse(
                    path,
                    is_dependency,
                    index_source=index_source
                )
            file_data['repo_path'] = str(repo_path)
            return file_data
        except Exception as e:
            error_logger(f"Error parsing {path} with {parser.language_name} parser: {e}")
            debug_log(f"[parse_file] Error parsing {path}: {e}")
            return {"path": str(path), "error": str(e)}

    def collect_indexable_file_paths(self, path: Path) -> list[Path]:
        """Return files CGC should represent in the graph for a repo or file path."""
        path = Path(path).resolve()
        all_files = [path] if path.is_file() else [f for f in path.rglob("*") if f.is_file()]

        ignore_root = path if path.is_dir() else path.parent
        cgcignore_path = None
        curr = ignore_root
        while True:
            candidate = curr / ".cgcignore"
            if candidate.exists():
                cgcignore_path = candidate
                ignore_root = curr
                break
            if curr.parent == curr:
                break
            curr = curr.parent

        ignore_patterns = list(DEFAULT_IGNORE_PATTERNS)
        if cgcignore_path:
            user_patterns = [
                line.strip()
                for line in cgcignore_path.read_text().splitlines()
                if line.strip() and not line.strip().startswith("#")
            ]
            ignore_patterns.extend(user_patterns)
        spec = pathspec.PathSpec.from_lines("gitignore", ignore_patterns)

        ignore_dirs = {d.strip().lower() for d in self.config.index_ignore if d.strip()}

        files = []
        base_for_dirs = path if path.is_dir() else path.parent
        for f in all_files:
            try:
                rel_parent_parts = {part.lower() for part in f.relative_to(base_for_dirs).parent.parts}
                if ignore_dirs and rel_parent_parts.intersection(ignore_dirs):
                    continue
            except ValueError:
                pass

            try:
                rel_path = f.relative_to(ignore_root)
            except ValueError:
                rel_path = f
            if spec.match_file(str(rel_path)):
                continue
            files.append(f)

        return sorted(files)

    def get_indexed_file_paths(self, repo_path: Path) -> set[str]:
        """Return File node paths currently attached to a repository in the graph."""
        repo_path_str = str(Path(repo_path).resolve())
        with self.driver.session() as session:
            result = session.run("""
                MATCH (r:Repository {path: $repo_path})-[:CONTAINS*]->(f:File)
                RETURN f.path AS path
            """, repo_path=repo_path_str)
            return {row["path"] for row in result if row.get("path")}

    def verify_repository_index(self, repo_path: Path, sample_limit: int = 20) -> Dict:
        """Compare indexable filesystem files against File nodes in the graph."""
        expected = {str(path.resolve()) for path in self.collect_indexable_file_paths(repo_path)}
        indexed = {str(Path(path).resolve()) for path in self.get_indexed_file_paths(repo_path)}
        missing = sorted(expected - indexed)
        extra = sorted(indexed - expected)
        return {
            "repo_path": str(Path(repo_path).resolve()),
            "expected_count": len(expected),
            "indexed_count": len(indexed),
            "missing_count": len(missing),
            "extra_count": len(extra),
            "missing_paths": missing[:sample_limit],
            "extra_paths": extra[:sample_limit],
            "is_clean": not missing and not extra,
        }

    def estimate_processing_time(self, path: Path) -> Optional[Tuple[int, float]]:
        """Estimate processing time and file count"""
        try:
            supported_extensions = self.parsers.keys()
            if path.is_file():
                if path.suffix in supported_extensions:
                    files = [path]
                else:
                    return 0, 0.0 # Not a supported file type
            else:
                all_files = path.rglob("*")
                files = [f for f in all_files if f.is_file() and f.suffix in supported_extensions]

                # Filter default ignored directories
                ignore_dirs = {d.strip().lower() for d in self.config.index_ignore if d.strip()}
                if ignore_dirs:
                    kept_files = []
                    for f in files:
                        try:
                            parts = set(p.lower() for p in f.relative_to(path).parent.parts)
                            if not parts.intersection(ignore_dirs):
                                kept_files.append(f)
                        except ValueError:
                            kept_files.append(f)
                    files = kept_files
            
            total_files = len(files)
            estimated_time = total_files * 0.05 # tree-sitter is faster
            return total_files, estimated_time
        except Exception as e:
            error_logger(f"Could not estimate processing time for {path}: {e}")
            return None

    async def build_graph_from_path_async(
        self, path: Path, is_dependency: bool = False, job_id: str = None
    ):
        """Builds graph from a directory or file path.

        When ``Config.profile_phases`` is set, accumulates wall-time-per-phase
        and prints the breakdown at the end. The instrumentation is gated so
        the default cold-index path pays no measurable cost.
        """
        # v1.2 P0 phase-timing scaffold — see docs/v1.2-profile.md.
        profile = self.config.profile_phases
        phase_total: dict[str, float] = {}
        # Wall-time start for sanity-checking that phase sums ≈ total elapsed.
        _t_start = time.monotonic() if profile else 0.0

        def _phase_start() -> float:
            return time.monotonic() if profile else 0.0

        def _phase_end(name: str, started: float) -> None:
            if profile:
                phase_total[name] = phase_total.get(name, 0.0) + (time.monotonic() - started)

        try:
            # Tree-sitter pipeline.
            if job_id:
                self.job_manager.update_job(job_id, status=JobStatus.RUNNING)

            _t = _phase_start()
            self.add_repository_to_graph(path, is_dependency)
            _phase_end("repository_node", _t)
            repo_name = path.name

            _t_discovery = _phase_start()

            # Search for .cgcignore upwards
            cgcignore_path = None
            ignore_root = path.resolve()
            
            # Start search from path (or parent if path is file)
            curr = path.resolve()
            if not curr.is_dir():
                curr = curr.parent

            # Walk up looking for .cgcignore
            while True:
                candidate = curr / ".cgcignore"
                if candidate.exists():
                    cgcignore_path = candidate
                    ignore_root = curr
                    debug_log(f"Found .cgcignore at {ignore_root}")
                    break
                if curr.parent == curr: # Root hit
                    break
                curr = curr.parent

            spec = None
            if cgcignore_path:
                with open(cgcignore_path) as f:
                    user_patterns = [line.strip() for line in f.read().splitlines() if line.strip() and not line.strip().startswith('#')]
                ignore_patterns = DEFAULT_IGNORE_PATTERNS + user_patterns
                spec = pathspec.PathSpec.from_lines('gitignore', ignore_patterns)
            else:
                # No .cgcignore found — create one in the project root with default patterns
                # so the user can see and customize what's being ignored
                project_root = path.resolve() if path.is_dir() else path.resolve().parent
                new_cgcignore = project_root / ".cgcignore"
                try:
                    cgcignore_content = "# Auto-generated by CGA\n"
                    cgcignore_content += "# Default ignore patterns for binary/media files\n"
                    cgcignore_content += "# Add your own patterns below\n\n"
                    cgcignore_content += "\n".join(DEFAULT_IGNORE_PATTERNS) + "\n"
                    new_cgcignore.write_text(cgcignore_content)
                    info_logger(f"Created default .cgcignore at {new_cgcignore}")
                except OSError as e:
                    warning_logger(f"Could not create .cgcignore at {new_cgcignore}: {e}")
                spec = pathspec.PathSpec.from_lines('gitignore', DEFAULT_IGNORE_PATTERNS)

            all_files = path.rglob("*") if path.is_dir() else [path]

            # Previously only files with supported extensions were indexed.
            # Updated to include all files so that unsupported file types
            # can still be represented as minimal File nodes in the graph.
            files = [f for f in all_files if f.is_file()]

            # Filter default ignored directories
            ignore_dirs = {d.strip().lower() for d in self.config.index_ignore if d.strip()}
            if ignore_dirs and path.is_dir():
                    kept_files = []
                    for f in files:
                        try:
                            # Check if any parent directory in the relative path is in ignore list
                            parts = set(p.lower() for p in f.relative_to(path).parent.parts)
                            if not parts.intersection(ignore_dirs):
                                kept_files.append(f)
                            else:
                                # debug_log(f"Skipping default ignored file: {f}")
                                pass
                        except ValueError:
                             kept_files.append(f)
                    files = kept_files
            
            if spec:
                filtered_files = []
                for f in files:
                    try:
                        # Match relative to the directory containing .cgcignore
                        rel_path = f.relative_to(ignore_root)
                        if not spec.match_file(str(rel_path)):
                            filtered_files.append(f)
                        else:
                            debug_log(f"Ignored file based on .cgcignore: {rel_path}")
                    except ValueError:
                        # Should not happen if ignore_root is a parent, but safety fallback
                        filtered_files.append(f)
                files = filtered_files
            _phase_end("discovery", _t_discovery)

            if job_id:
                self.job_manager.update_job(job_id, total_files=len(files))

            debug_log("Starting pre-scan to build imports map...")
            _t = _phase_start()
            imports_map = self._pre_scan_for_imports(files)
            _phase_end("pre_scan_imports", _t)
            debug_log(f"Pre-scan complete. Found {len(imports_map)} definitions.")

            all_file_data = []

            processed_count = 0
            for file in files:
                if file.is_file():
                    if job_id:
                        self.job_manager.update_job(job_id, current_file=str(file))
                    repo_path = path.resolve() if path.is_dir() else file.parent.resolve()
                    _t = _phase_start()
                    file_data = self.parse_file(repo_path, file, is_dependency)
                    _phase_end("parse_file", _t)
                    # Previously only files with supported extensions were indexed.
                    # Updated to include all files so that unsupported file types
                    # can still be represented as minimal File nodes in the graph.
                    if "error" not in file_data:
                        _t = _phase_start()
                        self.add_file_to_graph(file_data, repo_name, imports_map)
                        _phase_end("add_file_to_graph", _t)
                        all_file_data.append(file_data)

                    # Previously only files with supported extensions were indexed.
                    # Updated to include all files so that unsupported file types
                    # can still be represented as minimal File nodes in the graph.
                    else:
                        # create minimal node if parser not available
                        _t = _phase_start()
                        self.add_minimal_file_node(file, repo_path, is_dependency)
                        _phase_end("minimal_file_node", _t)
                    processed_count += 1

                    if job_id:
                        self.job_manager.update_job(job_id, processed_files=processed_count)
                    await asyncio.sleep(0.01)

            _t = _phase_start()
            self._create_all_inheritance_links(all_file_data, imports_map)
            _phase_end("inheritance_links", _t)
            _t = _phase_start()
            self._create_all_function_calls(all_file_data, imports_map)
            _phase_end("function_calls", _t)

            if profile:
                self._emit_phase_profile(phase_total, time.monotonic() - _t_start, len(all_file_data))
            
            if job_id:
                self.job_manager.update_job(job_id, status=JobStatus.COMPLETED, end_time=datetime.now())
        except Exception as e:
            error_message=str(e)
            error_logger(f"Failed to build graph for path {path}: {error_message}")
            if job_id:
                '''checking if the repo got deleted '''
                if "no such file found" in error_message or "deleted" in error_message or "not found" in error_message:
                    status=JobStatus.CANCELLED
                    
                else:
                    status=JobStatus.FAILED

                self.job_manager.update_job(
                    job_id, status=status, end_time=datetime.now(), errors=[str(e)]
                )

    # Create a minimal File node for unsupported file types.
    # These files do not contain parsed entities but should still
    # appear in the repository graph as requested in issue #707.
    def add_minimal_file_node(self, file_path: Path, repo_path: Path, is_dependency: bool = False):

        file_path_str = str(file_path.resolve())
        file_name = file_path.name
        repo_name = repo_path.name
        repo_path_str = str(repo_path.resolve())

        with self.driver.session() as session:

            session.run(
                """
                MERGE (r:Repository {path: $repo_path})
                SET r.name = $repo_name
                """,
                repo_path=repo_path_str,
                repo_name=repo_name
            )

            session.run(
                """
                MERGE (f:File {path: $file_path})
                SET f.name = $file_name,
                    f.is_dependency = $is_dependency
                """,
                file_path=file_path_str,
                file_name=file_name,
                is_dependency=is_dependency
            )

            # Establish directory structure
            file_path_obj = Path(file_path_str)
            repo_path_obj = Path(repo_path_str)
            try:
                relative_path_to_file = file_path_obj.relative_to(repo_path_obj)
            except ValueError:
                # Fallback if not relative
                relative_path_to_file = Path(file_path_obj.name)
            
            parent_path = repo_path_str
            parent_label = 'Repository'

            for part in relative_path_to_file.parts[:-1]:
                current_path = Path(parent_path) / part
                current_path_str = str(current_path)
                
                session.run(f"""
                    MATCH (p:{parent_label} {{path: $parent_path}})
                    MERGE (d:Directory {{path: $current_path}})
                    SET d.name = $part
                    MERGE (p)-[:CONTAINS]->(d)
                """, parent_path=parent_path, current_path=current_path_str, part=part)

                parent_path = current_path_str
                parent_label = 'Directory'

            session.run(f"""
                MATCH (p:{parent_label} {{path: $parent_path}})
                MATCH (f:File {{path: $file_path}})
                MERGE (p)-[:CONTAINS]->(f)
            """, parent_path=parent_path, file_path=file_path_str)
