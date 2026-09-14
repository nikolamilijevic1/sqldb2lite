"""
sqldb2lite - Hybrid SQL Dump to SQLite3 Converter
Powered by sqlglot + AST constraint stitching + modular dialect handlers.
"""

try:
    from .converter import convert_dump_to_sqlite, inspect_sqlite_db
    from .dialects import get_dialect_handler, list_supported_dialects
except (ImportError, ValueError):
    from converter import convert_dump_to_sqlite, inspect_sqlite_db
    from dialects import get_dialect_handler, list_supported_dialects

__all__ = [
    "convert_dump_to_sqlite",
    "inspect_sqlite_db",
    "get_dialect_handler",
    "list_supported_dialects",
]
__version__ = "0.2.0"
