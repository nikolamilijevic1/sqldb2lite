"""
sqldb2lite.cli - Command-line interface for the sqldb2lite SQL to SQLite converter.
"""

import sys
import os
import argparse
import time
try:
    from .converter import convert_dump_to_sqlite
    from .dialects import list_supported_dialects
except (ImportError, ValueError):
    from converter import convert_dump_to_sqlite
    from dialects import list_supported_dialects


def main():
    supported = list_supported_dialects()
    parser = argparse.ArgumentParser(
        prog="sqldb2lite",
        description="Convert MySQL and PostgreSQL dumps into SQLite databases with foreign key preservation."
    )
    parser.add_argument("dump_file", help="Path to the input .sql dump file")
    parser.add_argument(
        "-d", "--dialect",
        required=True,
        choices=supported,
        help=f"Source SQL dialect (required). Choices: {', '.join(supported)}"
    )
    parser.add_argument("-o", "--output", help="Path to the destination .sqlite / .db file (defaults to <dump_name>.db)")
    parser.add_argument("-b", "--batch-size", type=int, default=5000, help="Row batch size for streaming inserts (default: 5000)")
    parser.add_argument("-q", "--quiet", action="store_true", help="Suppress verbose output")

    args = parser.parse_args()

    if not os.path.isfile(args.dump_file):
        print(f"Error: File '{args.dump_file}' does not exist.", file=sys.stderr)
        sys.exit(1)

    if not args.output:
        base_name = os.path.splitext(os.path.basename(args.dump_file))[0]
        args.output = f"{base_name}.db"

    if not args.quiet:
        print("=" * 60)
        print("  sqldb2lite - Hybrid SQL Dump to SQLite3 Converter")
        print("=" * 60)
        print(f"  Input Dump      : {args.dump_file}")
        print(f"  Target SQLite DB: {args.output}")
        print(f"  Source Dialect  : {args.dialect.upper()}")
        print("-" * 60)
        print("  Starting conversion...")

    start_time = time.time()

    try:
        report = convert_dump_to_sqlite(
            dump_path=args.dump_file,
            sqlite_db_path=args.output,
            dialect=args.dialect,
            batch_size=args.batch_size
        )
    except Exception as e:
        print(f"\n[ERROR] Conversion failed: {e}", file=sys.stderr)
        sys.exit(1)

    duration = time.time() - start_time

    if not args.quiet:
        print(f"  Conversion completed in {duration:.2f} seconds!\n")
        print("  SUMMARY OF CONVERTED DATABASE:")
        print(f"  - Total Tables      : {report['total_tables']}")
        print(f"  - Total Rows        : {report['total_rows']:,}")
        print(f"  - Total Foreign Keys: {report['total_foreign_keys']}")
        print(f"  - FK Violations     : {len(report['fk_violations'])}")

        print("\n  TABLE DETAILS:")
        print(f"  {'Table Name':<25} {'Rows':<12} {'Foreign Keys':<15}")
        print("  " + "-" * 52)
        for tbl, info in report["tables"].items():
            fks = len(info["foreign_keys"])
            print(f"  {tbl:<25} {info['row_count']:<12,} {fks:<15}")

        if report["fk_violations"]:
            print("\n  [WARNING] Foreign key violations found:")
            for v in report["fk_violations"]:
                print(f"    - {v}")
        else:
            print("\n  [OK] Relational integrity verified. Zero foreign key violations.")
        print("=" * 60)


if __name__ == "__main__":
    main()
