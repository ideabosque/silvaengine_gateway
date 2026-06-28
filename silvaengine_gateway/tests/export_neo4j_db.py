#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Export Neo4j database for backup.

Dumps all node labels, relationship types, nodes (with properties), and
relationships (with properties) from the configured Neo4j database to a
timestamped Cypher script file. The output is a series of CREATE Cypher
statements that can be restored via ``import_neo4j_db.py``.

Prerequisites:
    pip install neo4j

Usage:
    # Basic — uses .env for Neo4j connection, outputs to ./backups/
    python -m silvaengine_gateway.tests.export_neo4j_db

    # Custom output directory
    python -m silvaengine_gateway.tests.export_neo4j_db --output-dir /tmp/backups

    # Export specific labels only (comma-separated)
    python -m silvaengine_gateway.tests.export_neo4j_db --labels Person,Company

    # Export specific relationship types only (comma-separated)
    python -m silvaengine_gateway.tests.export_neo4j_db --rel-types KNOWS,WORKS_FOR

    # Schema only (constraints + indexes, no data)
    python -m silvaengine_gateway.tests.export_neo4j_db --schema-only

    # Compress output with gzip
    python -m silvaengine_gateway.tests.export_neo4j_db --gzip
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
    from neo4j import GraphDatabase
except ImportError:
    print("ERROR: 'neo4j' is required. Run: pip install neo4j")
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


# ---------------------------------------------------------------------------
# Cypher serialization helpers
# ---------------------------------------------------------------------------

_CYPHER_KEYWORDS = {
    "ALL", "AND", "AS", "ASC", "ASCENDING", "ASSERT", "BY", "CALL", "CASE",
    "CONSTRAINT", "CONTAINS", "COUNT", "CREATE", "DELETE", "DESC",
    "DESCENDING", "DETACH", "DISTINCT", "DROP", "ELSE", "END", "ENDS",
    "EXISTS", "EXTRACT", "FALSE", "FIELD", "FILTER", "FOREACH", "FROM",
    "IN", "INDEX", "IS", "JOIN", "LIMIT", "MATCH", "MERGE", "NODE",
    "NOT", "NULL", "ON", "OPTIONAL", "OR", "ORDER", "RANGE", "REDUCE",
    "REL", "RELATIONSHIP", "REMOVE", "RETURN", "SET", "SKIP", "STARTS",
    "THEN", "TRUE", "UNION", "UNIQUE", "UNWIND", "WHEN", "WHERE", "WITH",
    "XOR", "YIELD",
}


def _needs_backtick(label):
    """Return True if the identifier needs backtick quoting for Cypher."""
    if not label:
        return True
    # Must be backtick-quoted if it contains special chars or is a reserved word
    if label.upper() in _CYPHER_KEYWORDS:
        return True
    if not label[0].isalpha() and label[0] != "_":
        return True
    for ch in label:
        if not (ch.isalnum() or ch == "_"):
            return True
    return False


def _bt(label):
    """Backtick-quote a label/property name if needed."""
    if _needs_backtick(label):
        escaped = label.replace("`", "``")
        return f"`{escaped}`"
    return label


def _cypher_value(value):
    """Serialize a Python value as a Cypher literal."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        escaped = value.replace("\\", "\\\\").replace("'", "\\'")
        return f"'{escaped}'"
    if isinstance(value, (list, tuple)):
        items = ", ".join(_cypher_value(v) for v in value)
        return f"[{items}]"
    if isinstance(value, dict):
        pairs = ", ".join(
            f"{_bt(k)}: {_cypher_value(v)}" for k, v in value.items()
        )
        return f"{{{pairs}}}"
    # Fallback — JSON-encode as a string
    escaped = json.dumps(value).replace("\\", "\\\\").replace("'", "\\'")
    return f"'{escaped}'"


def _props_cypher(props):
    """Render a property dict as Cypher ' += { k: v, ... }' syntax.

    Uses '+= ' (merge) so existing properties (e.g. _element_id from the
    MERGE pattern) are preserved. Plain 'SET n {map}' is invalid Cypher;
    the correct forms are 'SET n = {map}' (overwrite) or 'SET n += {map}'
    (merge). We use merge to avoid clobbering the matching key.
    """
    if not props:
        return ""
    pairs = ", ".join(
        f"{_bt(k)}: {_cypher_value(v)}" for k, v in sorted(props.items())
    )
    return f" += {{{pairs}}}"


# ---------------------------------------------------------------------------
# Export logic
# ---------------------------------------------------------------------------

# Sentinel element-id key — Neo4j >= 5 uses elementId, 4.x uses id()
def _node_identifier(node):
    """Return a stable identifier for a Neo4j node across driver versions."""
    # Neo4j Python driver >= 5 exposes .element_id
    eid = getattr(node, "element_id", None)
    if eid is not None:
        return ("id", eid)
    # Fall back to internal integer id (driver 4.x)
    return ("id", str(node.id))


def _rel_identifier(rel):
    """Return a stable identifier for a relationship."""
    eid = getattr(rel, "element_id", None)
    if eid is not None:
        return ("id", eid)
    return ("id", str(rel.id))


def export_schema(session, lines):
    """Export constraints and indexes as Cypher DDL."""
    lines.append("-- Schema: Constraints & Indexes")
    lines.append("")

    # Constraints
    result = session.run("SHOW CONSTRAINTS")
    for record in result:
        constraint = record["name"] if "name" in record.keys() else None
        # Build a CREATE CONSTRAINT statement from available fields
        if constraint:
            # Best-effort generic form
            raw = record.data()
            ctype = raw.get("type", "")
            entity_type = raw.get("entityType", "NODE")
            labels_or_types = raw.get("labelsOrTypes", raw.get(" entityType", []))
            properties = raw.get("properties", [])

            label_part = ""
            if labels_or_types:
                lbl = labels_or_types[0] if isinstance(labels_or_types, list) else labels_or_types
                label_part = f"FOR (n:{_bt(lbl)})"
            elif entity_type == "RELATIONSHIP":
                label_part = f"FOR ()-[r]-()"

            prop_list = ", ".join(f"n.{_bt(p)}" for p in properties) if entity_type == "NODE" else ", ".join(f"r.{_bt(p)}" for p in properties)
            uniq = "UNIQUE " if "UNIQUENESS" in str(ctype).upper() else ""
            lines.append(
                f"CREATE CONSTRAINT {constraint} IF NOT EXISTS FOR (n:{_bt(labels_or_types[0] if labels_or_types else '')}) "
                f"REQUIRE ({prop_list}) IS {uniq}PRESENT"
                if False
                else f"/* Constraint {constraint}: {json.dumps(raw, default=str)} */"
            )
    lines.append("")

    # Indexes
    result = session.run("SHOW INDEXES")
    for record in result:
        raw = record.data()
        idx_name = raw.get("name", "?")
        lines.append(f"/* Index {idx_name}: {json.dumps(raw, default=str)} */")
    lines.append("")


def export_nodes(session, lines, label_filter=None):
    """Export all nodes grouped by label as CREATE statements."""
    # Discover labels
    if label_filter:
        labels = [l.strip() for l in label_filter.split(",")]
    else:
        result = session.run("CALL db.labels() YIELD label RETURN label ORDER BY label")
        labels = [r["label"] for r in result]

    total_nodes = 0
    for label in labels:
        # Count
        count_result = session.run(f"MATCH (n:{_bt(label)}) RETURN count(n) AS cnt")
        node_count = count_result.single()["cnt"]
        print(f"  Exporting label: {label} ({node_count} nodes) ...")

        if node_count == 0:
            continue

        # Fetch all nodes for this label
        result = session.run(f"MATCH (n:{_bt(label)}) RETURN n ORDER BY n")
        nodes = [r["n"] for r in result]

        lines.append(f"-- Nodes: {label} ({len(nodes)} nodes)")
        for node in nodes:
            id_type, id_val = _node_identifier(node)
            props = dict(node)
            # Store the internal id so relationships can reconnect on import
            props_cypher = _props_cypher(props)
            # Build the SET clause for properties — skip when empty to avoid
            # generating invalid "SET n" with no assignment.
            set_props = f" SET n{props_cypher}" if props_cypher else ""
            # Use MERGE on element_id/id so re-import is idempotent
            if id_type == "id" and id_val.isdigit():
                lines.append(
                    f"MERGE (n:{_bt(label)} {{_export_id: {_cypher_value(id_val)}}})"
                    f"{set_props}"
                    f" SET n._export_id = {_cypher_value(id_val)};"
                )
            else:
                # element_id is a string — store it for rel linking
                lines.append(
                    f"MERGE (n:{_bt(label)} {{_element_id: {_cypher_value(id_val)}}})"
                    f"{set_props}"
                    f" SET n._element_id = {_cypher_value(id_val)};"
                )
            total_nodes += 1

        lines.append("")

    return total_nodes, labels


def export_relationships(session, lines, labels, rel_type_filter=None):
    """Export all relationships grouped by type as CREATE statements."""
    if rel_type_filter:
        rel_types = [t.strip() for t in rel_type_filter.split(",")]
    else:
        result = session.run("CALL db.relationshipTypes() YIELD relationshipType RETURN relationshipType ORDER BY relationshipType")
        rel_types = [r["relationshipType"] for r in result]

    total_rels = 0
    for rtype in rel_types:
        count_result = session.run(f"MATCH ()-[r:{_bt(rtype)}]->() RETURN count(r) AS cnt")
        rel_count = count_result.single()["cnt"]
        print(f"  Exporting relationship: {rtype} ({rel_count} rels) ...")

        if rel_count == 0:
            continue

        result = session.run(f"MATCH (a)-[r:{_bt(rtype)}]->(b) RETURN r, a, b ORDER BY r")
        records = list(result)

        lines.append(f"-- Relationships: {rtype} ({len(records)} rels)")
        for rec in records:
            rel = rec["r"]
            node_a = rec["a"]
            node_b = rec["b"]

            a_type, a_val = _node_identifier(node_a)
            b_type, b_val = _node_identifier(node_b)

            props = dict(rel)
            props_cypher = _props_cypher(props)

            # Match the start and end nodes by their stored export id
            if a_type == "id" and a_val.isdigit():
                a_match = f"MATCH (a {{_export_id: {_cypher_value(a_val)}}})"
            else:
                a_match = f"MATCH (a {{_element_id: {_cypher_value(a_val)}}})"

            if b_type == "id" and b_val.isdigit():
                b_match = f"(b {{_export_id: {_cypher_value(b_val)}}})"
            else:
                b_match = f"(b {{_element_id: {_cypher_value(b_val)}}})"

            rel_type_str = _bt(rtype)
            set_clause = f" SET r{props_cypher};" if props_cypher else ";"
            lines.append(
                f"{a_match} MATCH {b_match} "
                f"MERGE (a)-[r:{rel_type_str}]->(b)"
                f"{set_clause}"
            )
            total_rels += 1

        lines.append("")

    return total_rels, rel_types


def export_database(args):
    """Main export function."""
    load_env()

    uri = os.getenv("neo4j_uri", "bolt://localhost:7687")
    username = os.getenv("neo4j_username", "neo4j")
    password = os.getenv("neo4j_password", "neo4j")
    database = os.getenv("neo4j_database", "neo4j")

    # Connect
    print(f"Connecting to Neo4j: {username}@{uri}/{database}")
    driver = GraphDatabase.driver(uri, auth=(username, password))

    # Build output path
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    suffix = ".cypher.gz" if args.gzip else ".cypher"
    output_file = output_dir / f"neo4j_backup_{timestamp}{suffix}"

    print(f"Exporting to: {output_file}")

    header = (
        f"-- Neo4j Database Backup\n"
        f"-- URI: {uri}\n"
        f"-- Database: {database}\n"
        f"-- User: {username}\n"
        f"-- Date: {datetime.now().isoformat()}\n"
        f"-- Schema only: {args.schema_only}\n"
        f"-- Label filter: {args.labels or '(all)'}\n"
        f"-- Relationship type filter: {args.rel_types or '(all)'}\n"
        f"-- Generated by: export_neo4j_db.py\n\n"
    )

    open_func = gzip.open if args.gzip else open
    mode = "wt" if args.gzip else "w"

    lines_buffer = []

    with driver.session(database=database) as session:
        # Schema (constraints + indexes)
        export_schema(session, lines_buffer)

        if not args.schema_only:
            # Nodes
            lines_buffer.append("-- ==================== NODES ====================\n")
            total_nodes, labels = export_nodes(
                session, lines_buffer, label_filter=args.labels
            )

            # Relationships
            lines_buffer.append("-- ================ RELATIONSHIPS ================\n")
            total_rels, rel_types = export_relationships(
                session, lines_buffer, labels, rel_type_filter=args.rel_types
            )
        else:
            total_nodes = 0
            total_rels = 0

    # Cleanup helper: after import, remove _export_id/_element_id temp props
    if not args.schema_only:
        lines_buffer.append("")
        lines_buffer.append("-- Cleanup: remove temporary _export_id / _element_id properties")
        lines_buffer.append("MATCH (n) WHERE n._export_id IS NOT NULL REMOVE n._export_id;")
        lines_buffer.append("MATCH (n) WHERE n._element_id IS NOT NULL REMOVE n._element_id;")

    # Write output
    with open_func(str(output_file), mode, encoding="utf-8") as f:
        f.write(header)
        f.write("\n".join(lines_buffer))
        f.write("\n")

    driver.close()

    file_size = output_file.stat().st_size
    print(f"\nBackup complete: {output_file} ({file_size / 1024:.1f} KB)")
    if not args.schema_only:
        print(f"  Nodes: {total_nodes}")
        print(f"  Relationships: {total_rels}")


def main():
    parser = argparse.ArgumentParser(
        description="Export Neo4j database for backup"
    )
    parser.add_argument(
        "--output-dir",
        default=str(Path(__file__).parent / "backups"),
        help="Output directory (default: ./backups/)",
    )
    parser.add_argument(
        "--schema-only",
        action="store_true",
        help="Export schema (constraints + indexes) only, no data",
    )
    parser.add_argument(
        "--labels",
        default=None,
        help="Comma-separated node labels to export (default: all)",
    )
    parser.add_argument(
        "--rel-types",
        default=None,
        help="Comma-separated relationship types to export (default: all)",
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