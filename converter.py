"""
sqldb2lite.converter - Core hybrid SQL dump to SQLite transpiler and bulk ingester.
Orchestrates schema parsing, out-of-line foreign key stitching, sqlglot transpilation,
and delegates dialect-specific logic to modular dialect handlers.
"""

import re
import os
import sqlite3
from typing import Dict, List, Tuple, Any, Optional
import sqlglot

try:
    from .dialects import get_dialect_handler, BaseDialectHandler
    from .dialects.base import clean_identifier
except (ImportError, ValueError):
    from dialects import get_dialect_handler, BaseDialectHandler
    from dialects.base import clean_identifier


def _parse_dump_schema_and_constraints(
    dump_path: str,
    handler: BaseDialectHandler
) -> Tuple[Dict[str, str], List[str], Dict[str, List[str]]]:
    """
    Scans the dump file for CREATE TABLE and ALTER TABLE statements,
    delegating dialect-specific constraint parsing to the handler.
    Returns: (raw_tables_dict, separate_indexes_list, table_constraints_dict)
    """
    raw_tables: Dict[str, str] = {}
    separate_indexes: List[str] = []
    table_constraints: Dict[str, List[str]] = {}

    in_create_table = False
    current_table = ""
    table_buffer: List[str] = []

    alter_buffer: List[str] = []
    in_alter_table = False
    target_table = ""

    create_table_re = re.compile(r"(?i)^CREATE\s+TABLE(?:\s+IF\s+NOT\s+EXISTS)?\s+([`\"'\w\.]+)", re.IGNORECASE)
    alter_table_re = re.compile(r"(?i)^ALTER\s+TABLE\s+(?:ONLY\s+)?([`\"'\w\.]+)", re.IGNORECASE)

    with open(dump_path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            stripped = line.strip()

            # Skip comments
            if stripped.startswith("--") or (stripped.startswith("/*") and stripped.endswith("*/")):
                continue

            # --- CREATE TABLE scanning ---
            if not in_create_table:
                match = create_table_re.match(stripped)
                if match:
                    in_create_table = True
                    current_table = clean_identifier(match.group(1))
                    table_buffer = [line]
                    if ";" in stripped and "(" in stripped and ")" in stripped:
                        raw_tables[current_table] = "".join(table_buffer)
                        in_create_table = False
                        table_buffer = []
                    continue
            else:
                table_buffer.append(line)
                if ";" in stripped:
                    raw_tables[current_table] = "".join(table_buffer)
                    in_create_table = False
                    table_buffer = []
                continue

            # --- ALTER TABLE constraint scanning ---
            if not in_alter_table:
                match = alter_table_re.match(stripped)
                if match:
                    in_alter_table = True
                    target_table = clean_identifier(match.group(1))
                    alter_buffer = [line]
                    if ";" in stripped:
                        full_alter = "".join(alter_buffer)
                        handler.process_alter_statement(full_alter, target_table, table_constraints, separate_indexes)
                        in_alter_table = False
                        alter_buffer = []
                    continue
            else:
                alter_buffer.append(line)
                if ";" in stripped:
                    full_alter = "".join(alter_buffer)
                    handler.process_alter_statement(full_alter, target_table, table_constraints, separate_indexes)
                    in_alter_table = False
                    alter_buffer = []
                continue

    return raw_tables, separate_indexes, table_constraints


def transpile_create_table(
    raw_sql: str,
    table_name: str,
    handler: BaseDialectHandler,
    extra_constraints: List[str]
) -> Tuple[str, List[str]]:
    """
    Converts a single CREATE TABLE block to SQLite dialect using sqlglot and the dialect handler.
    """
    # 1. Dialect-specific pre-cleanup
    sql, separate_indexes = handler.clean_table_schema(raw_sql, table_name)

    # 2. Inject out-of-line constraints (Foreign Keys & Primary Keys) before the final closing paren
    if extra_constraints:
        closing_idx = sql.rfind(")")
        if closing_idx != -1:
            constraints_str = ",\n  " + ",\n  ".join(extra_constraints)
            sql = sql[:closing_idx] + constraints_str + sql[closing_idx:]

    # 3. Transpile via sqlglot
    try:
        sqlite_sql = sqlglot.transpile(sql, read=handler.sqlglot_dialect, write="sqlite")[0]
    except Exception:
        try:
            sqlite_sql = sqlglot.transpile(sql, read=None, write="sqlite")[0]
        except Exception:
            sqlite_sql = sql

    # 4. Dialect-specific post-cleanup
    sqlite_sql = handler.clean_sqlite_output(sqlite_sql, table_name)

    return sqlite_sql, separate_indexes


def inspect_sqlite_db(db_path: str) -> Dict[str, Any]:
    """
    Inspects an SQLite database to verify all tables, rows, foreign key relationships,
    and relational integrity.
    """
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    cursor.execute("PRAGMA foreign_keys = ON;")
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%';")
    tables = [row[0] for row in cursor.fetchall()]

    summary: Dict[str, Any] = {
        "tables": {},
        "total_tables": len(tables),
        "total_rows": 0,
        "total_foreign_keys": 0,
        "fk_violations": []
    }

    for table in tables:
        cursor.execute(f'SELECT COUNT(*) FROM "{table}";')
        count = cursor.fetchone()[0]
        summary["total_rows"] += count

        cursor.execute(f'PRAGMA foreign_key_list("{table}");')
        fk_rows = cursor.fetchall()
        fks = []
        for fk in fk_rows:
            fks.append({
                "target_table": fk[2],
                "from_column": fk[3],
                "to_column": fk[4],
                "on_update": fk[5],
                "on_delete": fk[6]
            })
            summary["total_foreign_keys"] += 1

        summary["tables"][table] = {
            "row_count": count,
            "foreign_keys": fks
        }

    # Verify relational integrity
    cursor.execute("PRAGMA foreign_key_check;")
    violations = cursor.fetchall()
    summary["fk_violations"] = violations

    conn.close()
    return summary


def convert_dump_to_sqlite(
    dump_path: str,
    sqlite_db_path: str,
    dialect: str,
    batch_size: int = 5000,
    overwrite: bool = True
) -> Dict[str, Any]:
    """
    Master hybrid conversion pipeline:
    1. Obtains the dialect handler for the required dialect.
    2. Scans for CREATE TABLE, ALTER TABLE foreign keys & primary keys.
    3. Stitches constraints and transpiles schemas into SQLite via sqlglot.
    4. Creates tables and indexes in SQLite with foreign keys temporarily disabled during ingestion.
    5. Streams data blocks using the dialect-specific streamer in high-speed transactions.
    6. Re-enables foreign keys and validates relational integrity.
    """
    if not dialect:
        raise ValueError("The 'dialect' parameter is required. Specify 'mysql' or 'postgres'.")

    if not os.path.isfile(dump_path):
        raise FileNotFoundError(f"Dump file not found: {dump_path}")

    # Ensure target parent directory exists
    os.makedirs(os.path.dirname(os.path.abspath(sqlite_db_path)), exist_ok=True)

    if overwrite and os.path.exists(sqlite_db_path):
        os.remove(sqlite_db_path)

    handler = get_dialect_handler(dialect)

    # 1. Extract schemas and constraints
    raw_tables, extra_indexes, table_constraints = _parse_dump_schema_and_constraints(dump_path, handler)

    # 2. SQLite setup with performance pragmas
    conn = sqlite3.connect(sqlite_db_path)
    conn.execute("PRAGMA foreign_keys = OFF;")
    conn.execute("PRAGMA synchronous = NORMAL;")
    conn.execute("PRAGMA journal_mode = MEMORY;")

    cursor = conn.cursor()

    # 3. Create tables & separate indexes
    generated_indexes = list(extra_indexes)
    for table_name, raw_sql in raw_tables.items():
        constraints = table_constraints.get(table_name, [])
        sqlite_create_sql, idxs = transpile_create_table(raw_sql, table_name, handler, constraints)
        generated_indexes.extend(idxs)

        try:
            cursor.execute(sqlite_create_sql)
        except Exception as e:
            fallback_sql, _ = transpile_create_table(raw_sql, table_name, handler, [])
            cursor.execute(fallback_sql)

    for idx_sql in generated_indexes:
        try:
            cursor.execute(idx_sql)
        except Exception:
            pass

    conn.commit()

    # 4. Stream data using dialect streamer
    streamer = handler.create_data_streamer(cursor, conn, batch_size=batch_size)
    with open(dump_path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            streamer.process_line(line, line.strip())
    streamer.flush()

    # 5. Re-enable foreign keys
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.close()

    # 6. Report
    report = inspect_sqlite_db(sqlite_db_path)
    report["source_dialect"] = handler.name
    report["db_path"] = sqlite_db_path
    return report
