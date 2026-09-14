"""
sqldb2lite.dialects.base - Abstract base class and data streamer protocol for dialect handlers.
"""

from abc import ABC, abstractmethod
from typing import Dict, List, Tuple, Any, Optional
import sqlite3
import re


def clean_identifier(name: str) -> str:
    """Strip quotes and schema qualifiers (e.g., public.users -> users, `users` -> users)."""
    name = name.strip()
    if "." in name:
        name = name.split(".")[-1]
    name = name.strip("`\"' []")
    return name


class BaseDataStreamer(ABC):
    """Abstract state machine for streaming data lines into SQLite."""

    def __init__(self, cursor: sqlite3.Cursor, conn: sqlite3.Connection, batch_size: int = 5000):
        self.cursor = cursor
        self.conn = conn
        self.batch_size = batch_size
        self.total_rows = 0

    @abstractmethod
    def process_line(self, line: str, stripped: str) -> None:
        """Process a single line from the dump file."""
        pass

    @abstractmethod
    def flush(self) -> None:
        """Flush any pending batches or transactions."""
        pass


class BaseDialectHandler(ABC):
    """Base class for database-specific syntax transformations."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Dialect identifier (e.g., 'mysql', 'postgres', 'oracle')."""
        pass

    @property
    @abstractmethod
    def sqlglot_dialect(self) -> str:
        """Dialect string to pass to sqlglot.transpile(..., read=...)."""
        pass

    @abstractmethod
    def clean_table_schema(self, raw_sql: str, table_name: str) -> Tuple[str, List[str]]:
        """
        Pre-process CREATE TABLE statements before sqlglot:
        - Strip dialect-specific storage engine or sequence options
        - Extract inline indexes to be created separately
        Returns: (cleaned_sql, separate_indexes)
        """
        pass

    @abstractmethod
    def clean_sqlite_output(self, sqlite_sql: str, table_name: str) -> str:
        """
        Post-process sqlglot SQLite output to fix any unsupported SQLite constructs
        (e.g., custom types, nested array types, inline indexes).
        """
        pass

    @abstractmethod
    def process_alter_statement(
        self,
        alter_sql: str,
        target_table: str,
        table_constraints: Dict[str, List[str]],
        separate_indexes: List[str]
    ) -> None:
        """
        Parse dialect-specific ALTER TABLE statements to extract:
        - Foreign keys
        - Primary keys
        - Unique constraints
        """
        pass

    @abstractmethod
    def create_data_streamer(self, cursor: sqlite3.Cursor, conn: sqlite3.Connection, batch_size: int = 5000) -> BaseDataStreamer:
        """Create a data streamer instance for bulk data ingestion."""
        pass
