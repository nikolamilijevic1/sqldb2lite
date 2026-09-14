"""
sqldb2lite.dialects.postgres - PostgreSQL dialect handler for sqldb2lite.
"""

import re
import sqlite3
from typing import Dict, List, Tuple, Any
from .base import BaseDialectHandler, BaseDataStreamer, clean_identifier


class PostgresDataStreamer(BaseDataStreamer):
    """Streams Postgres COPY ... FROM stdin blocks and standard INSERT statements."""

    def __init__(self, cursor: sqlite3.Cursor, conn: sqlite3.Connection, batch_size: int = 5000):
        super().__init__(cursor, conn, batch_size)
        self.in_copy = False
        self.copy_table = ""
        self.copy_cols: List[str] = []
        self.copy_batch: List[List[Any]] = []

        self.in_insert = False
        self.insert_buffer: List[str] = []
        self.conn.execute("BEGIN TRANSACTION;")

    def process_line(self, line: str, stripped: str) -> None:
        # 1. Active COPY block
        if self.in_copy:
            if stripped == "\\." or stripped.startswith("\\."):
                self._flush_copy_batch()
                self.in_copy = False
                return

            cols = stripped.split("\t")
            row_vals = []
            for val in cols:
                if val == "\\N":
                    row_vals.append(None)
                elif val == "t":
                    row_vals.append(1)
                elif val == "f":
                    row_vals.append(0)
                else:
                    row_vals.append(val)

            self.copy_batch.append(row_vals)

            if len(self.copy_batch) >= self.batch_size:
                self._flush_copy_batch()
            return

        # 2. Check for start of COPY
        if re.match(r"(?i)^COPY\s+", stripped):
            copy_match = re.match(r"(?i)^COPY\s+([`\"'\w\.]+)\s*\(([^\)]+)\)\s+FROM\s+stdin;", stripped)
            if copy_match:
                self.in_copy = True
                self.copy_table = clean_identifier(copy_match.group(1))
                self.copy_cols = [clean_identifier(c) for c in copy_match.group(2).split(",")]
                self.copy_batch = []
                return

        # 3. Handle standard INSERT INTO (if dumped with --inserts)
        if self.in_insert:
            self.insert_buffer.append(line)
            if stripped.endswith(";"):
                self._execute_buffered_insert()
            return

        if re.match(r"(?i)^INSERT\s+INTO\s+", stripped):
            self.in_insert = True
            self.insert_buffer = [line]
            if stripped.endswith(";"):
                self._execute_buffered_insert()
            return

        # 4. Skip comments and server management commands
        if not stripped or stripped.startswith(("--", "/*")):
            return
        if re.match(r"(?i)^(SET|SELECT\s+pg_|CREATE\s+SEQUENCE|ALTER\s+SEQUENCE)", stripped):
            return

    def _flush_copy_batch(self) -> None:
        if not self.copy_batch:
            return
        placeholders = ", ".join(["?"] * len(self.copy_cols))
        cols_str = ", ".join([f'"{c}"' for c in self.copy_cols])
        self.cursor.executemany(f'INSERT INTO "{self.copy_table}" ({cols_str}) VALUES ({placeholders})', self.copy_batch)
        self.total_rows += len(self.copy_batch)
        self.copy_batch = []

    def _execute_buffered_insert(self) -> None:
        full_insert = "".join(self.insert_buffer)
        full_insert = re.sub(r'(?i)INSERT\s+INTO\s+"?public"?\.', 'INSERT INTO ', full_insert)
        try:
            self.cursor.execute(full_insert)
            rc = self.cursor.rowcount if self.cursor.rowcount > 0 else 1
            self.total_rows += rc
        except Exception:
            pass
        self.insert_buffer = []
        self.in_insert = False

    def flush(self) -> None:
        if self.in_copy:
            self._flush_copy_batch()
            self.in_copy = False
        if self.in_insert and self.insert_buffer:
            self._execute_buffered_insert()
        self.conn.commit()


class PostgresDialectHandler(BaseDialectHandler):
    """Handler for PostgreSQL dumps."""

    @property
    def name(self) -> str:
        return "postgres"

    @property
    def sqlglot_dialect(self) -> str:
        return "postgres"

    def clean_table_schema(self, raw_sql: str, table_name: str) -> Tuple[str, List[str]]:
        # 1. Clean schema names like public.table -> table
        sql = re.sub(r"(?i)CREATE\s+TABLE(?:\s+IF\s+NOT\s+EXISTS)?\s+[`\"']?public[`\"']?\.", "CREATE TABLE ", raw_sql)
        sql = re.sub(r"\bpublic\.", "", sql)

        # 2. Clean Postgres-specific defaults and casts
        sql = re.sub(r"(?i)DEFAULT\s+nextval\([^)]+\)", "", sql)
        sql = re.sub(r"(?i)DEFAULT\s+\([^)]*now[^)]*\)", "DEFAULT CURRENT_TIMESTAMP", sql)
        sql = re.sub(r"(?i)DEFAULT\s+now\(\)", "DEFAULT CURRENT_TIMESTAMP", sql)
        sql = re.sub(r"::[a-zA-Z0-9_\.]+", "", sql)

        return sql, []

    def clean_sqlite_output(self, sqlite_sql: str, table_name: str) -> str:
        # Clean schema prefix and custom types
        sqlite_sql = re.sub(r"\bpublic\.", "", sqlite_sql)
        sqlite_sql = re.sub(r"(?i)\bARRAY<[^>]+>", "TEXT", sqlite_sql)
        sqlite_sql = re.sub(r"(?i)\btsvector\b", "TEXT", sqlite_sql)
        sqlite_sql = re.sub(r"(?i)\bUSER-DEFINED\b", "TEXT", sqlite_sql)

        sqlite_sql = sqlite_sql.strip()
        if not sqlite_sql.endswith(";"):
            sqlite_sql += ";"
        return sqlite_sql

    def process_alter_statement(
        self,
        alter_sql: str,
        target_table: str,
        table_constraints: Dict[str, List[str]],
        separate_indexes: List[str]
    ) -> None:
        one_line = " ".join(alter_sql.split())

        # Foreign Key
        fk_match = re.search(
            r"(?i)ADD\s+CONSTRAINT\s+([`\"'\w]+)\s+FOREIGN\s+KEY\s*\(([^\)]+)\)\s*REFERENCES\s+([`\"'\w\.]+)\s*\(([^\)]+)\)(.*?);",
            one_line
        )
        if fk_match:
            fk_name = clean_identifier(fk_match.group(1))
            local_cols = ", ".join([clean_identifier(c) for c in fk_match.group(2).split(",")])
            ref_table = clean_identifier(fk_match.group(3))
            ref_cols = ", ".join([clean_identifier(c) for c in fk_match.group(4).split(",")])
            extra_rules = fk_match.group(5).strip()

            rule_clause = ""
            if extra_rules:
                clean_rules = re.findall(
                    r"(?i)ON\s+(?:DELETE|UPDATE)\s+(?:CASCADE|RESTRICT|SET\s+NULL|SET\s+DEFAULT|NO\s+ACTION)",
                    extra_rules
                )
                if clean_rules:
                    rule_clause = " " + " ".join(clean_rules)

            fk_constraint = f"CONSTRAINT {fk_name} FOREIGN KEY ({local_cols}) REFERENCES {ref_table}({ref_cols}){rule_clause}"
            table_constraints.setdefault(target_table, []).append(fk_constraint)
            return

        # Primary Key
        pk_match = re.search(r"(?i)ADD\s+CONSTRAINT\s+[`\"'\w]+\s+PRIMARY\s+KEY\s*\(([^\)]+)\)", one_line)
        if not pk_match:
            pk_match = re.search(r"(?i)ADD\s+PRIMARY\s+KEY\s*\(([^\)]+)\)", one_line)
        if pk_match:
            pk_cols = ", ".join([clean_identifier(c) for c in pk_match.group(1).split(",")])
            table_constraints.setdefault(target_table, []).append(f"PRIMARY KEY ({pk_cols})")
            return

        # Unique
        unique_match = re.search(r"(?i)ADD\s+CONSTRAINT\s+[`\"'\w]+\s+UNIQUE\s*\(([^\)]+)\)", one_line)
        if unique_match:
            u_cols = ", ".join([clean_identifier(c) for c in unique_match.group(1).split(",")])
            table_constraints.setdefault(target_table, []).append(f"UNIQUE ({u_cols})")

    def create_data_streamer(self, cursor: sqlite3.Cursor, conn: sqlite3.Connection, batch_size: int = 5000) -> BaseDataStreamer:
        return PostgresDataStreamer(cursor, conn, batch_size)
