#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Export PostgreSQL database for backup.

Dumps all tables (with data) from the configured PostgreSQL database to a
timestamped SQL file. Uses psycopg2 for connectivity and generates a portable
pg_dump-compatible SQL script (CREATE TABLE + INSERT statements) without
requiring the pg_dump binary.

Prerequisites:
    pip install psycopg2

Usage:
    # Basic — uses .env for PG connection, outputs to ./backups/
    python -m silvaengine_gateway.tests.export_pg_db

    # Custom output directory
    python -m silvaengine_gateway.tests.export_pg_db --output-dir /tmp/backups

    # Export schema only (no data)
    python -m silvaengine_gateway.tests.export_pg_db --schema-only

    # Export specific tables only (comma-separated)
    python -m silvaengine_gateway.tests.export_pg_db --tables aace_agents,aace_llms

    # Compress output with gzip
    python -m silvaengine_gateway.tests.export_pg_db --gzip
"""
from __future__ import print_function

import argparse
import gzip
import json
import os
import sys
from datetime import datetime
from pathlib import Path

try:
    import psycopg2
except ImportError:
    print("ERROR: 'psycopg2' is required. Run: pip install psycopg2-binary")
    sys.exit(1)


def load_env():
    """Load .env from tests/ directory."""
    env_path = Path(__file__).parent / ".env"
    if env_path.exists():
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key, value = key.strip(), value.strip()
                # Strip inline comments
                value = value.split("#")[0].strip()
                if key and key not in os.environ:
                    os.environ[key] = value


def _quote_sql_string(value):
    """Escape a Python value for safe inclusion in a SQL INSERT statement.

    For dict/list values (psycopg2 returns JSONB columns as Python objects),
    serialize with json.dumps() so the output is valid JSON that PostgreSQL
    can cast back to JSONB. Using str() would produce Python repr with single
    quotes (e.g. {'key': 'value'}), which is invalid JSON.

    Backslashes are NOT doubled because standard_conforming_strings=on
    (PostgreSQL default since 9.1) treats backslashes as literal in
    regular SQL string literals. Only single quotes are escaped (via '').
    """
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (dict, list)):
        # JSONB column — serialize to valid JSON, then SQL-escape single quotes
        json_str = json.dumps(value, ensure_ascii=False)
        escaped = json_str.replace("'", "''")
        return f"'{escaped}'"
    # Plain text — escape single quotes only (backslashes are literal
    # under standard_conforming_strings=on)
    escaped = str(value).replace("'", "''")
    return f"'{escaped}'"


def export_table(cur, table_name, schema_only=False):
    """Export a single table as CREATE TABLE + INSERT statements."""
    lines = []

    # Get column definitions
    cur.execute("""
        SELECT column_name, data_type, character_maximum_length,
               is_nullable, column_default
        FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = %s
        ORDER BY ordinal_position
    """, (table_name,))
    columns = cur.fetchall()

    if not columns:
        return ""

    col_defs = []
    col_names = []
    for col_name, data_type, max_len, nullable, default in columns:
        col_names.append(col_name)
        type_str = data_type
        if max_len:
            type_str = f"{data_type}({max_len})"
        col_def = f'    "{col_name}" {type_str}'
        if nullable == "NO":
            col_def += " NOT NULL"
        if default:
            col_def += f" DEFAULT {default}"
        col_defs.append(col_def)

    # Get primary key
    cur.execute("""
        SELECT kcu.column_name
        FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu
          ON tc.constraint_name = kcu.constraint_name
         AND tc.table_schema = kcu.table_schema
        WHERE tc.table_schema = 'public' AND tc.table_name = %s
          AND tc.constraint_type = 'PRIMARY KEY'
        ORDER BY kcu.ordinal_position
    """, (table_name,))
    pk_cols = [r[0] for r in cur.fetchall()]
    if pk_cols:
        pk_list = ", ".join('"' + c + '"' for c in pk_cols)
        col_defs.append(f'    PRIMARY KEY ({pk_list})')

    # Get indexes
    cur.execute("""
        SELECT indexname, indexdef
        FROM pg_indexes
        WHERE schemaname = 'public' AND tablename = %s
        AND indexname NOT IN (
            SELECT conname FROM pg_constraint
            WHERE conrelid = %s::regclass AND contype = 'p'
        )
        ORDER BY indexname
    """, (table_name, table_name))
    indexes = cur.fetchall()

    lines.append(f"-- Table: {table_name}")
    lines.append(f'DROP TABLE IF EXISTS "{table_name}" CASCADE;')
    lines.append(f'CREATE TABLE "{table_name}" (')
    lines.append(",\n".join(col_defs))
    lines.append(");")
    lines.append("")

    # Indexes
    for idx_name, idx_def in indexes:
        lines.append(f"{idx_def};")
    lines.append("")

    if not schema_only:
        # Export data
        col_list = ", ".join('"' + c + '"' for c in col_names)
        cur.execute(f'SELECT {col_list} FROM "{table_name}"')
        rows = cur.fetchall()
        if rows:
            lines.append(f"-- Data: {table_name} ({len(rows)} rows)")
            col_list = ", ".join(f'"{c}"' for c in col_names)
            for row in rows:
                values = ", ".join(_quote_sql_string(v) for v in row)
                lines.append(
                    f'INSERT INTO "{table_name}" ({col_list}) VALUES ({values});'
                )
            lines.append("")

    return "\n".join(lines)


def export_database(args):
    """Main export function."""
    load_env()

    pg_host = os.getenv("PG_HOST", "localhost")
    pg_port = int(os.getenv("PG_PORT", "5432"))
    pg_user = os.getenv("PG_USER", "silvaengine")
    pg_password = os.getenv("PG_PASSWORD", "silvaengine")
    pg_db = os.getenv("PG_DB", "silvaengine")

    # Connect
    print(f"Connecting to PostgreSQL: {pg_user}@{pg_host}:{pg_port}/{pg_db}")
    conn = psycopg2.connect(
        host=pg_host, port=pg_port,
        user=pg_user, password=pg_password,
        dbname=pg_db, connect_timeout=10,
    )
    cur = conn.cursor()

    # Get all tables in public schema
    if args.tables:
        table_list = [t.strip() for t in args.tables.split(",")]
    else:
        cur.execute("""
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
            ORDER BY table_name
        """)
        table_list = [r[0] for r in cur.fetchall()]

    print(f"Found {len(table_list)} table(s): {', '.join(table_list)}")

    # Detect installed extensions so the backup can recreate them
    cur.execute("""
        SELECT extname FROM pg_extension WHERE extname != 'plpgsql'
        ORDER BY extname
    """)
    extensions = [r[0] for r in cur.fetchall()]
    if extensions:
        print(f"Extensions: {', '.join(extensions)}")

    # Build output path
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    suffix = ".sql.gz" if args.gzip else ".sql"
    output_file = output_dir / f"pg_backup_{pg_db}_{timestamp}{suffix}"

    # Export
    print(f"Exporting to: {output_file}")
    header = (
        f"-- PostgreSQL Database Backup\n"
        f"-- Database: {pg_db}\n"
        f"-- Host: {pg_host}:{pg_port}\n"
        f"-- User: {pg_user}\n"
        f"-- Date: {datetime.now().isoformat()}\n"
        f"-- Tables: {', '.join(table_list)}\n"
        f"-- Schema only: {args.schema_only}\n"
        f"-- Extensions: {', '.join(extensions)}\n"
        f"-- Generated by: export_pg_db.py\n\n"
        f"SET session_replication_role = 'replica';\n\n"
    )
    # Emit CREATE EXTENSION statements so the backup is self-contained
    for ext in extensions:
        header += f'CREATE EXTENSION IF NOT EXISTS "{ext}";\n'
    header += "\n"

    open_func = gzip.open if args.gzip else open
    mode = "wt" if args.gzip else "w"

    with open_func(str(output_file), mode, encoding="utf-8") as f:
        f.write(header)
        for table in table_list:
            print(f"  Exporting: {table} ...", end=" ", flush=True)
            sql = export_table(cur, table, schema_only=args.schema_only)
            f.write(sql)
            row_count = sql.count("INSERT INTO")
            print(f"{row_count} rows")

    footer = "\nSET session_replication_role = 'origin';\n"
    with open_func(str(output_file), "a" if not args.gzip else "at", encoding="utf-8") as f:
        f.write(footer)

    cur.close()
    conn.close()

    file_size = output_file.stat().st_size
    print(f"\nBackup complete: {output_file} ({file_size / 1024:.1f} KB)")


def main():
    parser = argparse.ArgumentParser(
        description="Export PostgreSQL database for backup"
    )
    parser.add_argument(
        "--output-dir",
        default=str(Path(__file__).parent / "backups"),
        help="Output directory (default: ./backups/)",
    )
    parser.add_argument(
        "--schema-only",
        action="store_true",
        help="Export schema only (no data)",
    )
    parser.add_argument(
        "--tables",
        default=None,
        help="Comma-separated table names (default: all tables)",
    )
    parser.add_argument(
        "--gzip",
        action="store_true",
        help="Compress output with gzip",
    )
    args = parser.parse_args()
    export_database(args)


if __name__ == "__main__":
    main()