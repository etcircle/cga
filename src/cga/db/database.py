# src/cga/db/database.py
"""FalkorDB server connection and Neo4j-like compatibility wrappers."""

import importlib.util
import re
import threading
from typing import Optional, Tuple

from cga.config import Config
from cga.utils.debug_log import error_logger, info_logger


class FalkorDBManager:
    """Manages the FalkorDB server connection."""

    def __init__(self, config: Config, graph_name: str = "codegraph"):
        """Initialize the manager from injected CGA configuration."""
        self.config = config
        self.graph_name = graph_name
        self._driver = None
        self._graph = None
        self._lock = threading.Lock()

    def get_driver(self):
        """
        Gets the FalkorDB connection.

        Returns:
            A FalkorDB graph instance wrapped in a Neo4j-like driver interface.
        """
        if self._driver is None:
            with self._lock:
                if self._driver is None:
                    try:
                        from falkordb import FalkorDB

                        info_logger(
                            "Connecting to FalkorDB server at "
                            f"{self.config.falkordb_host}:{self.config.falkordb_port}"
                        )
                        driver = FalkorDB(
                            host=self.config.falkordb_host,
                            port=self.config.falkordb_port,
                        )
                        graph = driver.select_graph(self.graph_name)

                        graph.query("RETURN 1")
                        self._driver = driver
                        self._graph = graph
                        info_logger("FalkorDB connection established successfully")
                        info_logger(f"Graph name: {self.graph_name}")
                    except ImportError as e:
                        error_logger(
                            "FalkorDB client is not installed. Install it with:\n"
                            "  pip install falkordb"
                        )
                        raise ValueError("FalkorDB client missing.") from e
                    except Exception as e:
                        error_logger(f"Failed to initialize FalkorDB: {e}")
                        raise

        return FalkorDBDriverWrapper(self._graph)

    def close_driver(self):
        """Closes the connection."""
        if self._driver is not None:
            info_logger("Closing FalkorDB connection")
            self._driver = None
            self._graph = None

    def is_connected(self) -> bool:
        """Checks if the database connection is currently active."""
        if self._graph is None:
            return False
        try:
            self._graph.query("RETURN 1")
            return True
        except Exception:
            return False

    def get_backend_type(self) -> str:
        """Returns the database backend type."""
        return "falkordb"

    @staticmethod
    def validate_config(db_path: str = None) -> Tuple[bool, Optional[str]]:
        """
        Validates FalkorDB configuration parameters.

        The server-mode client has no local storage path to validate; this method
        remains for compatibility with older call-sites.
        """
        return True, None

    @staticmethod
    def test_connection(db_path: str = None) -> Tuple[bool, Optional[str]]:
        """Tests that the FalkorDB client package is available."""
        try:
            if importlib.util.find_spec("falkordb") is None:
                raise ImportError("falkordb")
            return True, None
        except ImportError:
            return False, "FalkorDB client is not installed.\nInstall it with: pip install falkordb"


class FalkorDBDriverWrapper:
    """
    Wrapper class to provide Neo4j driver-like interface for FalkorDB.

    This allows existing code to work with minimal changes.
    """

    def __init__(self, graph):
        self.graph = graph

    def session(self):
        """Returns a session-like object for FalkorDB."""
        return FalkorDBSessionWrapper(self.graph)

    def close(self):
        """FalkorDB doesn't need explicit close for sessions."""
        pass


class FalkorDBSessionWrapper:
    """Wrapper class to provide Neo4j session-like interface for FalkorDB."""

    def __init__(self, graph):
        self.graph = graph

    def run(self, query, **parameters):
        """Execute a Cypher query on FalkorDB."""
        query = self._translate_schema_query(query)

        try:
            result = self.graph.query(query, parameters)
            return FalkorDBResultWrapper(result)
        except Exception as e:
            error_msg = str(e).lower()
            if "already exists" in error_msg or "already created" in error_msg:
                return FalkorDBResultWrapper(None)

            error_logger(f"FalkorDB query failed: {query[:100]}... Error: {e}")
            raise

    def _translate_schema_query(self, query: str) -> str:
        """Translate Neo4j schema queries to FalkorDB/RedisGraph syntax."""
        q_upper = query.upper()

        # Handle Fulltext Indexes (Not supported in same syntax, skip for now)
        if "CREATE FULLTEXT INDEX" in q_upper:
            return "RETURN 1"

        # Handle Constraints
        if "CREATE CONSTRAINT" in q_upper:
            # Remove "IF NOT EXISTS"
            query = re.sub(r"\s+IF NOT EXISTS", "", query, flags=re.IGNORECASE)

            # Handle composite keys: (n.p1, n.p2) -> downgrade to INDEX
            if "," in query:
                match_node = re.search(r"FOR\s+(\([^)]+\))", query, flags=re.IGNORECASE)
                match_props = re.search(
                    r"REQUIRE\s+(\([^)]+\))\s+IS UNIQUE", query, flags=re.IGNORECASE
                )

                if match_node and match_props:
                    return f"CREATE INDEX FOR {match_node.group(1)} ON {match_props.group(1)}"

            # Handle simple uniqueness: CREATE CONSTRAINT name FOR (n:Label) REQUIRE n.prop IS UNIQUE
            # TO: CREATE CONSTRAINT ON (n:Label) ASSERT n.prop IS UNIQUE

            # Remove constraint name
            query = re.sub(
                r"CREATE CONSTRAINT\s+\w+\s+", "CREATE CONSTRAINT ", query, flags=re.IGNORECASE
            )
            query = re.sub(r"\s+FOR\s+", " ON ", query, flags=re.IGNORECASE)
            query = re.sub(r"\s+REQUIRE\s+", " ASSERT ", query, flags=re.IGNORECASE)

        # Handle Regular Indexes
        elif "CREATE INDEX" in q_upper:
            # Remove "IF NOT EXISTS"
            query = re.sub(r"\s+IF NOT EXISTS", "", query, flags=re.IGNORECASE)
            # Remove Index Name: CREATE INDEX name FOR -> CREATE INDEX FOR
            query = re.sub(
                r"CREATE INDEX\s+\w+\s+FOR", "CREATE INDEX FOR", query, flags=re.IGNORECASE
            )

        return query

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        pass


class FalkorDBRecord(dict):
    """Dict wrapper that provides a .data() method for compatibility with Neo4j records."""

    def data(self):
        return self


class FalkorDBResultWrapper:
    """Wrapper class to provide Neo4j result-like interface for FalkorDB results."""

    def __init__(self, result):
        self.result = result
        self._consumed = False

    def consume(self):
        """Mark result as consumed (for compatibility)."""
        self._consumed = True
        return self

    def single(self):
        """Return single result record as a FalkorDBRecord."""
        data = self.data()
        return data[0] if data else None

    def data(self):
        """Return all results as list of FalkorDBRecord objects."""
        if not hasattr(self.result, "result_set"):
            return []

        results = []
        if hasattr(self.result, "header") and self.result.header:
            headers = self.result.header
            for row in self.result.result_set:
                row_dict = FalkorDBRecord()
                for i, header in enumerate(headers):
                    if i < len(row):
                        # FalkorDB headers are [column_type, column_name] pairs.
                        if isinstance(header, (list, tuple)) and len(header) > 1:
                            header_name = header[1]
                            if isinstance(header_name, bytes):
                                header_name = header_name.decode("utf-8")
                        else:
                            header_name = str(header)
                        row_dict[header_name] = row[i]
                results.append(row_dict)
        elif hasattr(self.result, "result_set"):
            # Fallback if no header
            for row in self.result.result_set:
                if isinstance(row, (list, tuple)) and len(row) == 1:
                    results.append(FalkorDBRecord({"value": row[0]}))
                else:
                    results.append(FalkorDBRecord({"value": row}))

        return results

    def __iter__(self):
        """Iterate over results as FalkorDBRecord objects."""
        return iter(self.data())
