"""
sqldb2lite.dialects.mysql - MySQL dialect handler for sqldb2lite.
"""

import re
import sqlite3
from typing import Dict, List, Tuple, Any
from .base import BaseDialectHandler, BaseDataStreamer, clean_identifier


class MySQLDataStreamer(BaseDataStreamer):
    """Streams multiline MySQL INSERT INTO statements with quote cleanup."""

    def __init__(self, cursor: sqlite3.Cursor, conn: sqlite3.Connection, batch_size: int = 5000):
        super().__init__(cursor, conn, batch_size)
        self.in_insert = False
        self.insert_buffer: List[str] = []
        self.insert_count = 0
        self.conn.execute("BEGIN TRANSACTION;")

    def process_line(self, line: str, stripped: str) -> None:
        # Ignore comments & server commands
        if not stripped or stripped.startswith(("--", "/*")):
            return
        if re.match(r"(?i)^(SET|LOCK|UNLOCK|CREATE\s+DATABASE|USE\s+)", stripped):
            return

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

    def _execute_buffered_insert(self) -> None:
        full_insert = "".join(self.insert_buffer)
        # Clean backticks to standard SQLite double-quotes
        full_insert = full_insert.replace("`", '"')
        # Clean MySQL escaped single-quotes (\' -> '')
        full_insert = full_insert.replace(r"\'", "''")

        try:
            self.cursor.execute(full_insert)
            self.insert_count += 1
            rc = self.cursor.rowcount if self.cursor.rowcount > 0 else 1
            self.total_rows += rc
        except Exception:
            pass

        self.insert_buffer = []
        self.in_insert = False

        if self.insert_count % 500 == 0:
            self.conn.commit()
            self.conn.execute("BEGIN TRANSACTION;")

    def flush(self) -> None:
        if self.in_insert and self.insert_buffer:
            self._execute_buffered_insert()
        self.conn.commit()


class MySQLDialectHandler(BaseDialectHandler):
    """Handler for MySQL / MariaDB dumps."""

    @property
    def name(self) -> str:
        return "mysql"

    @property
    def sqlglot_dialect(self) -> str:
        return "mysql"

    def clean_table_schema(self, raw_sql: str, table_name: str) -> Tuple[str, List[str]]:
        separate_indexes: List[str] = []

        # 1. Extract and remove inline MySQL KEY / INDEX lines
        def extract_key(match):
            idx_name = clean_identifier(match.group(1))
            idx_cols = match.group(2)
            separate_indexes.append(f'CREATE INDEX IF NOT EXISTS "{idx_name}" ON "{table_name}" ({idx_cols});')
            return ""

        sql = re.sub(r"(?i),\s*\b(?:KEY|INDEX)\s+([`\"'\w]+)\s*\(([^\)]+)\)", extract_key, raw_sql)

        # 2. Clean MySQL table trailer: ) ENGINE=InnoDB DEFAULT CHARSET=...;
        trailer_match = re.search(r"\)\s*(?:ENGINE|DEFAULT\s+CHARSET|AUTO_INCREMENT|COLLATE)[^;]*;", sql, re.IGNORECASE)
        if trailer_match:
            sql = sql[:trailer_match.start()] + ");"

        return sql, separate_indexes

    def clean_sqlite_output(self, sqlite_sql: str, table_name: str) -> str:
        # Strip any residual INDEX foo (bar) clauses inside CREATE TABLE
        sqlite_sql = re.sub(r"(?i),\s*INDEX\s+([`\"'\w]+)\s*\(([^\)]+)\)", "", sqlite_sql)
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

        # Foreign Key: ADD CONSTRAINT ... FOREIGN KEY (...) REFERENCES ...
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
        return MySQLDataStreamer(cursor, conn, batch_size)
