"""
sqldb2lite.dialects - Dialect registry and factory for sqldb2lite.
"""

from typing import Dict, Type
from .base import BaseDialectHandler
from .mysql import MySQLDialectHandler
from .postgres import PostgresDialectHandler

_DIALECT_REGISTRY: Dict[str, Type[BaseDialectHandler]] = {
    "mysql": MySQLDialectHandler,
    "mariadb": MySQLDialectHandler,
    "postgres": PostgresDialectHandler,
    "postgresql": PostgresDialectHandler,
}


def get_dialect_handler(dialect: str) -> BaseDialectHandler:
    """
    Returns an instance of the requested dialect handler.
    Raises ValueError if dialect is unsupported.
    """
    normalized = dialect.strip().lower()
    handler_cls = _DIALECT_REGISTRY.get(normalized)
    if not handler_cls:
        supported = ", ".join(sorted(set(_DIALECT_REGISTRY.keys())))
        raise ValueError(f"Unsupported SQL dialect: '{dialect}'. Supported dialects: {supported}")
    return handler_cls()


def list_supported_dialects() -> list:
    """Returns a sorted list of supported dialect names."""
    return sorted(set(_DIALECT_REGISTRY.keys()))


__all__ = ["BaseDialectHandler", "get_dialect_handler", "list_supported_dialects"]
