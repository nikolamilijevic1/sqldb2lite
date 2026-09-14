"""
Tests for sqldb2lite converter, modular dialect handlers, and CLI.
"""

import os
import sys
import sqlite3
import pytest

# Ensure both the parent directory and package root are in sys.path
# so tests work whether executed from workspace root or inside sqldb2lite/
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PARENT_DIR = os.path.dirname(_THIS_DIR)
_GRANDPARENT_DIR = os.path.dirname(_PARENT_DIR)

for p in [_GRANDPARENT_DIR, _PARENT_DIR]:
    if p not in sys.path:
        sys.path.insert(0, p)

try:
    from sqldb2lite.converter import convert_dump_to_sqlite, inspect_sqlite_db
    from sqldb2lite.dialects import get_dialect_handler, list_supported_dialects
except ImportError:
    from converter import convert_dump_to_sqlite, inspect_sqlite_db
    from dialects import get_dialect_handler, list_supported_dialects

# Robust path resolution for samples and generated directories
if os.path.isdir(os.path.join(_PARENT_DIR, "samples")):
    SAMPLES_DIR = os.path.join(_PARENT_DIR, "samples")
else:
    SAMPLES_DIR = os.path.join(_THIS_DIR, "samples")

MYSQL_SAMPLES = os.path.join(SAMPLES_DIR, "mysql")
POSTGRES_SAMPLES = os.path.join(SAMPLES_DIR, "postgres")

GENERATED_DIR = os.path.join(_THIS_DIR, "sqlite_generated")
MYSQL_GENERATED = os.path.join(GENERATED_DIR, "mysql")
POSTGRES_GENERATED = os.path.join(GENERATED_DIR, "postgres")


def test_dialect_registry():
    """Verify registry returns correct handlers and validates inputs."""
    mysql_h = get_dialect_handler("mysql")
    assert mysql_h.name == "mysql"

    mariadb_h = get_dialect_handler("mariadb")
    assert mariadb_h.name == "mysql"

    pg_h = get_dialect_handler("postgres")
    assert pg_h.name == "postgres"

    postgresql_h = get_dialect_handler("postgresql")
    assert postgresql_h.name == "postgres"

    with pytest.raises(ValueError, match="Unsupported SQL dialect"):
        get_dialect_handler("unknown_db")


def test_dialect_is_required(tmp_path):
    """Verify that omitting or passing empty dialect raises ValueError."""
    dummy_sql = tmp_path / "dummy.sql"
    dummy_sql.write_text("CREATE TABLE t (id INT);")

    with pytest.raises(ValueError, match="dialect.*required"):
        convert_dump_to_sqlite(str(dummy_sql), str(tmp_path / "out.db"), dialect="")


def test_alter_table_fk_stitching(tmp_path):
    """Verify that ALTER TABLE ADD CONSTRAINT FOREIGN KEY is stitched into CREATE TABLE."""
    test_sql = tmp_path / "test_fk.sql"
    test_sql.write_text("""
    CREATE TABLE users (
        id INT NOT NULL,
        name VARCHAR(50),
        PRIMARY KEY (id)
    );

    CREATE TABLE posts (
        id INT NOT NULL,
        user_id INT,
        title VARCHAR(100),
        PRIMARY KEY (id)
    );

    ALTER TABLE posts ADD CONSTRAINT fk_posts_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE;
    """, encoding="utf-8")

    db_path = str(tmp_path / "test_fk.db")
    report = convert_dump_to_sqlite(str(test_sql), db_path, dialect="mysql")

    assert report["total_tables"] == 2
    assert report["total_foreign_keys"] == 1
    assert "posts" in report["tables"]

    fks = report["tables"]["posts"]["foreign_keys"]
    assert len(fks) == 1
    assert fks[0]["target_table"] == "users"
    assert fks[0]["from_column"] == "user_id"
    assert fks[0]["to_column"] == "id"
    assert fks[0]["on_delete"] == "CASCADE"


def test_mysql_inline_key_extraction():
    """Verify that inline KEY clauses are removed from CREATE TABLE and converted to separate CREATE INDEX."""
    handler = get_dialect_handler("mysql")
    raw_sql = """
    CREATE TABLE products (
        productCode VARCHAR(15) NOT NULL,
        productName VARCHAR(70) NOT NULL,
        buyPrice DECIMAL(10,2) NOT NULL,
        PRIMARY KEY (productCode),
        KEY idx_name (productName),
        KEY idx_price (buyPrice)
    );
    """
    clean_sql, indexes = handler.clean_table_schema(raw_sql, "products")

    assert "KEY idx_name" not in clean_sql
    assert len(indexes) == 2
    assert any("idx_name" in idx for idx in indexes)
    assert any("idx_price" in idx for idx in indexes)


def test_mysql_classicmodels_conversion():
    """Test full conversion of MySQL ClassicModels dump, outputting to tests/sqlite_generated/mysql/."""
    dump_path = os.path.join(MYSQL_SAMPLES, "mysql_classicmodels.sql")
    db_path = os.path.join(MYSQL_GENERATED, "classicmodels.db")

    report = convert_dump_to_sqlite(dump_path, db_path, dialect="mysql")

    assert report["total_tables"] == 8
    assert report["total_rows"] == 3864
    assert report["total_foreign_keys"] == 8
    assert report["fk_violations"] == []

    # Check key tables
    assert "customers" in report["tables"]
    assert "employees" in report["tables"]
    assert "orders" in report["tables"]
    assert "orderdetails" in report["tables"]

    # Verify orders -> customers FK
    order_fks = report["tables"]["orders"]["foreign_keys"]
    assert any(fk["target_table"] == "customers" for fk in order_fks)


def test_postgres_periodic_table_conversion():
    """Test Postgres dump with COPY statements, booleans, and nulls, outputting to tests/sqlite_generated/postgres/."""
    dump_path = os.path.join(POSTGRES_SAMPLES, "postgres_periodic_table.sql")
    db_path = os.path.join(POSTGRES_GENERATED, "periodic_table.db")

    report = convert_dump_to_sqlite(dump_path, db_path, dialect="postgres")

    assert report["total_tables"] == 1
    assert report["total_rows"] == 118
    assert report["fk_violations"] == []

    # Verify data content and types
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    c.execute('SELECT "AtomicNumber", "Element", "Symbol", "Natural" FROM periodic_table WHERE "AtomicNumber" = 1;')
    row = c.fetchone()
    assert row[0] == 1
    assert row[1] == "Hydrogen"
    assert row[2] == "H"
    assert row[3] == 1  # 't' converted to 1
    conn.close()


def test_postgres_dvdrental_conversion():
    """Test full Postgres DVD rental database with 15 tables and 18 foreign keys."""
    dump_path = os.path.join(POSTGRES_SAMPLES, "postgres_dvdrental.sql")
    db_path = os.path.join(POSTGRES_GENERATED, "dvdrental.db")

    report = convert_dump_to_sqlite(dump_path, db_path, dialect="postgres")

    assert report["total_tables"] == 15
    assert report["total_rows"] == 44820
    assert report["total_foreign_keys"] == 18
    assert report["fk_violations"] == []

    # Check relationships
    customer_fks = report["tables"]["customer"]["foreign_keys"]
    assert any(fk["target_table"] == "address" for fk in customer_fks)

    film_actor_fks = report["tables"]["film_actor"]["foreign_keys"]
    assert any(fk["target_table"] == "film" for fk in film_actor_fks)
    assert any(fk["target_table"] == "actor" for fk in film_actor_fks)


def test_foreign_key_enforcement():
    """Verify that SQLite actively enforces foreign keys reconstructed by sqldb2lite."""
    db_path = os.path.join(MYSQL_GENERATED, "classicmodels.db")

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON;")
    cursor = conn.cursor()

    # Attempt to insert an order referencing a non-existent customerNumber (999999)
    with pytest.raises(sqlite3.IntegrityError):
        cursor.execute("""
            INSERT INTO "orders" ("orderNumber", "orderDate", "requiredDate", "status", "customerNumber")
            VALUES (99999, '2026-01-01', '2026-01-10', 'Shipped', 999999);
        """)

    conn.close()
