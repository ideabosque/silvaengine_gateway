#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Call the Knowledge Graph Engine search endpoint through the SilvaEngine Gateway.

Supports all 4 search modes:
    - vector      Semantic similarity search
    - text2cypher LLM-generated Cypher query
    - vector_cypher  Vector + custom Cypher traversal
    - hybrid      Vector + fulltext combined

Also supports RAG (retrieval-augmented generation) queries.

Usage:
    # Start the gateway (terminal 1):
    python -m silvaengine_gateway.tests.run_daemon

    # Register Neo4j instance (required once per partition):
    python -m silvaengine_gateway.tests.call_search --register-neo4j

    # Run a search (defaults to --query from .env DEFAULT_SEARCH_QUERY):
    python -m silvaengine_gateway.tests.call_search

    # Search with explicit query/mode:
    python -m silvaengine_gateway.tests.call_search \\
        --query "Find all products" --mode text2cypher

    # Vector search:
    python -m silvaengine_gateway.tests.call_search \\
        --query "safety gear" --mode vector

    # RAG query:
    python -m silvaengine_gateway.tests.call_search \\
        --query "What products are available?" --rag

    # Raw GraphQL (custom query):
    python -m silvaengine_gateway.tests.call_search \\
        --graphql '{ "query": "{ search(queryText: \\\\"hello\\\\") { results } }" }'

All connection params (base_url, endpoint_id, part_id, auth credentials, default
query/mode) are read from the .env file in the same directory as this script.
CLI flags override .env values.
"""

from __future__ import print_function

__author__ = "silvaengine"

import argparse
import json
import os
import sys
from pathlib import Path

# ── Ensure project roots are on sys.path ───────────────────────────
# When run via VS Code debugger or direct script execution,
# the package roots aren't on sys.path. Add them so both
# `silvaengine_gateway` and `knowledge_graph_engine` can be imported.
_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent.parent)
_KGE_ROOT = str(Path(__file__).resolve().parent.parent.parent.parent / "knowledge_graph_engine")
for _p in [_PROJECT_ROOT, _KGE_ROOT]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

import requests
from dotenv import load_dotenv


def _promote_editable_finders() -> None:
    """Move all _EditableFinder entries above PathFinder in sys.meta_path.

    When running from a monorepo (cwd = .../silvaengine/), PathFinder
    discovers silvaengine_* project-root directories and creates namespace
    packages — shadowing the correct SourceFileLoader specs from pip's
    editable finders.  This fix ensures editable installs resolve first.
    """
    import sys
    from importlib.machinery import PathFinder

    meta_path = sys.meta_path
    # Editable finders are class objects (not instances), so we check
    # f.__name__ rather than type(f).__name__.
    editable = [
        f for f in meta_path
        if hasattr(f, "__name__") and f.__name__ == "_EditableFinder"
    ]
    if not editable:
        return

    pf_index = None
    for i, finder in enumerate(meta_path):
        if finder is PathFinder:
            pf_index = i
            break

    if pf_index is None:
        return

    if all(meta_path.index(f) < pf_index for f in editable):
        return  # Already correct

    for f in editable:
        meta_path.remove(f)
    for i, finder in enumerate(meta_path):
        if finder is PathFinder:
            pf_index = i
            break
    for f in reversed(editable):
        meta_path.insert(pf_index, f)


# Apply fix before any silvaengine imports
_promote_editable_finders()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Call KGE search through the SilvaEngine Gateway"
    )

    # ── Connection ────────────────────────────────────────────────
    parser.add_argument(
        "--base-url", type=str, default=None,
        help="Gateway base URL (default: from .env or http://localhost:8765)"
    )
    parser.add_argument(
        "--dotenv", type=str, default=None,
        help="Path to .env file (default: <this_script_dir>/.env)"
    )

    # ── Auth ─────────────────────────────────────────────────────
    parser.add_argument(
        "--username", type=str, default=None,
        help="Username for auth (default: from .env ADMIN_USERNAME)"
    )
    parser.add_argument(
        "--password", type=str, default=None,
        help="Password for auth (default: from .env ADMIN_PASSWORD)"
    )
    parser.add_argument(
        "--token", type=str, default=None,
        help="Pre-existing JWT token (skips login)"
    )

    # ── Tenant ───────────────────────────────────────────────────
    parser.add_argument(
        "--endpoint-id", type=str, default=None,
        help="Endpoint ID (default: from .env endpoint_id)"
    )
    parser.add_argument(
        "--part-id", type=str, default=None,
        help="Partition ID (default: from .env part_id)"
    )

    # ── Neo4j registration ──────────────────────────────────────
    parser.add_argument(
        "--register-neo4j", action="store_true",
        help="Register (or update) the Neo4j instance for the current partition. "
             "Uses neo4j_uri/neo4j_username/neo4j_password/neo4j_database from .env. "
             "This must be run once per partition before search queries will work."
    )

    # ── Query ────────────────────────────────────────────────────
    parser.add_argument(
        "--query", "-q", type=str, default=None,
        help="Search query text (default: from .env DEFAULT_SEARCH_QUERY, or 'FLIGHT-CDG-JFK-BUS')"
    )
    parser.add_argument(
        "--mode", "-m", type=str, default=None,
        choices=["vector", "text2cypher", "vector_cypher", "hybrid"],
        help="Search mode (default: from .env DEFAULT_SEARCH_MODE, or 'text2cypher')"
    )
    parser.add_argument(
        "--index-name", type=str, default="vector",
        help="Vector index name (default: vector)"
    )
    parser.add_argument(
        "--top-k", type=int, default=10,
        help="Number of results to retrieve (default: 10)"
    )
    parser.add_argument(
        "--page", type=int, default=1,
        help="Result page number (default: 1)"
    )
    parser.add_argument(
        "--limit", type=int, default=10,
        help="Results per page (default: 10)"
    )
    parser.add_argument(
        "--retrieval-query", type=str, default=None,
        help="Custom Cypher retrieval query (vector_cypher mode)"
    )
    parser.add_argument(
        "--filters", type=str, default=None,
        help="JSON filters string (e.g. '{\"status\": \"active\"}')"
    )

    # ── RAG ───────────────────────────────────────────────────────
    parser.add_argument(
        "--rag", action="store_true",
        help="Use RAG query instead of search"
    )
    parser.add_argument(
        "--prompt", type=str, default=None,
        help="Custom RAG prompt template"
    )

    # ── Raw GraphQL ───────────────────────────────────────────────
    parser.add_argument(
        "--graphql", type=str, default=None,
        help="Raw GraphQL query JSON (overrides all other query options)"
    )

    # ── Misc ─────────────────────────────────────────────────────
    parser.add_argument(
        "--raw", action="store_true",
        help="Print raw JSON response without formatting"
    )

    return parser.parse_args()


def get_token(base_url: str, username: str, password: str) -> str:
    """Authenticate and return a JWT access token."""
    resp = requests.post(
        f"{base_url}/auth/token",
        data={"username": username, "password": password},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def register_neo4j(
    base_url: str,
    endpoint_id: str,
    part_id: str,
    token: str,
    neo4j_uri: str,
    neo4j_username: str,
    neo4j_password: str,
    neo4j_database: str,
) -> None:
    """Register the Neo4j instance for the given partition via GraphQL mutation."""
    # ── Pre-flight: verify Neo4j connectivity ──────────────────────
    try:
        from neo4j import GraphDatabase

        driver = GraphDatabase.driver(neo4j_uri, auth=(neo4j_username, neo4j_password))
        driver.verify_connectivity()
        driver.close()
        print(f"Neo4j connectivity check: OK ({neo4j_uri})")
    except ImportError:
        print("WARNING: neo4j driver not installed, skipping connectivity check")
    except Exception as e:
        print(f"ERROR: Cannot connect to Neo4j at {neo4j_uri}: {e}")
        print("Make sure neo4j_password in .env matches your local Neo4j password.")
        print("Test with:")
        print(f'  python -c "from neo4j import GraphDatabase; '
              f'GraphDatabase.driver(\'{neo4j_uri}\', '
              f'auth=(\'{neo4j_username}\', \'YOUR_PASSWORD\')).verify_connectivity(); print(\'OK\')"')
        sys.exit(1)
    graphql_path = f"/{endpoint_id}/{part_id}/knowledge_graph_graphql"
    url = f"{base_url}{graphql_path}"

    mutation = (
        'mutation {'
        '  insertUpdateNeo4jInstance('
        f'    neo4jUri: "{neo4j_uri}", '
        f'    neo4jUsername: "{neo4j_username}", '
        f'    neo4jPassword: "{neo4j_password}", '
        f'    neo4jDatabase: "{neo4j_database}", '
        '    status: "active"'
        '  ) {'
        '    instanceId neo4jUri neo4jDatabase status'
        '  }'
        '}'
    )

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Part-Id": part_id,
    }

    print(f"\n{'='*60}")
    print(f"  Registering Neo4j Instance")
    print(f"  Partition: {part_id}")
    print(f"  URI: {neo4j_uri}")
    print(f"  Database: {neo4j_database}")
    print(f"{'='*60}\n")

    resp = requests.post(url, json={"query": mutation}, headers=headers, timeout=30)
    data = resp.json()

    if "errors" in data:
        print("FAILED — GraphQL errors:")
        for err in data["errors"]:
            print(f"  - {err.get('message', err)}")
        sys.exit(1)

    result = data.get("data", {}).get("insertUpdateNeo4jInstance", {})
    print(f"Registered successfully!")
    print(f"  Instance ID: {result.get('instanceId', '?')}")
    print(f"  URI: {result.get('neo4jUri', '?')}")
    print(f"  Database: {result.get('neo4jDatabase', '?')}")
    print(f"  Status: {result.get('status', '?')}")
    print()


def build_graphql_payload(args: argparse.Namespace) -> dict:
    """Build the GraphQL request body from CLI args."""
    if args.graphql:
        return json.loads(args.graphql)

    # results and context are List(JSONCamelCase) — scalar, no subfields
    if args.rag:
        # RAG query — answer is String, context is [JSONCamelCase] scalar
        rag_args = [
            f'queryText: "{args.query}"',
            f'searchMode: "{args.mode}"',
        ]
        if args.index_name and args.index_name != "vector":
            rag_args.append(f'indexName: "{args.index_name}"')
        if args.top_k != 5:
            rag_args.append(f'topK: {args.top_k}')
        if args.prompt:
            escaped = args.prompt.replace('"', '\\"')
            rag_args.append(f'prompt: "{escaped}"')

        rag_fields = " ".join(rag_args)
        query = f"""{{ rag({rag_fields}) {{ answer context }} }}"""
        return {"query": query}

    # Search query — results is [JSONCamelCase] scalar
    search_args = [
        f'queryText: "{args.query}"',
        f'searchMode: "{args.mode}"',
    ]
    if args.index_name and args.index_name != "vector":
        search_args.append(f'indexName: "{args.index_name}"')
    if args.top_k != 10:
        search_args.append(f'topK: {args.top_k}')
    if args.page != 1:
        search_args.append(f'page: {args.page}')
    if args.limit != 10:
        search_args.append(f'limit: {args.limit}')
    if args.retrieval_query:
        escaped = args.retrieval_query.replace('"', '\\"')
        search_args.append(f'retrievalQuery: "{escaped}"')
    if args.filters:
        pass  # handled via variables below

    search_fields = " ".join(search_args)
    query = f"""{{ search({search_fields}) {{ results total page limit }} }}"""

    payload = {"query": query}
    if args.filters:
        payload["variables"] = {"filters": json.loads(args.filters)}

    return payload


def main() -> None:
    args = parse_args()

    # ── Load .env ───────────────────────────────────────────────────
    env_file = args.dotenv or str(Path(__file__).parent / ".env")
    if not Path(env_file).exists():
        print(f"WARNING: .env file not found at {env_file}")
        print("Some values will use hardcoded defaults.")
        print("Copy .env.example to .env and fill in real values.\n")
    else:
        load_dotenv(env_file, override=True)
        print(f"Loaded .env from: {env_file}")

    # ── Resolve params: CLI > .env > hardcoded fallback ───────────
    base_url = args.base_url or os.getenv("BASE_URL", "http://localhost:8765")
    endpoint_id = args.endpoint_id or os.getenv("endpoint_id", "test-ep")
    part_id = args.part_id or os.getenv("part_id", "test-part")
    args.query = args.query or os.getenv("DEFAULT_SEARCH_QUERY", "FLIGHT-CDG-JFK-BUS")
    args.mode = args.mode or os.getenv("DEFAULT_SEARCH_MODE", "text2cypher")

    # ── Validate query ─────────────────────────────────────────────
    if not args.register_neo4j and not args.graphql and not args.query:
        print("ERROR: --query is required unless --graphql or --register-neo4j is used")
        sys.exit(1)

    # ── Authenticate ────────────────────────────────────────────────
    if args.token:
        token = args.token
    else:
        username = args.username or os.getenv("ADMIN_USERNAME", "admin")
        password = args.password or os.getenv("ADMIN_PASSWORD", "admin123")
        print(f"Authenticating as {username}...")
        token = get_token(base_url, username, password)

    # ── Register Neo4j (if requested) ──────────────────────────────
    if args.register_neo4j:
        neo4j_uri = os.getenv("neo4j_uri", "bolt://localhost:7687")
        neo4j_username = os.getenv("neo4j_username", "neo4j")
        neo4j_password = os.getenv("neo4j_password", "")
        neo4j_database = os.getenv("neo4j_database", "neo4j")

        if not neo4j_password:
            print("ERROR: neo4j_password is required for --register-neo4j. "
                  "Set it in .env or pass NEO4J_PASSWORD env var.")
            sys.exit(1)

        register_neo4j(
            base_url=base_url,
            endpoint_id=endpoint_id,
            part_id=part_id,
            token=token,
            neo4j_uri=neo4j_uri,
            neo4j_username=neo4j_username,
            neo4j_password=neo4j_password,
            neo4j_database=neo4j_database,
        )
        return

    # ── Build request ────────────────────────────────────────────────
    graphql_path = f"/{endpoint_id}/{part_id}/knowledge_graph_graphql"
    url = f"{base_url}{graphql_path}"

    payload = build_graphql_payload(args)

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Part-Id": part_id,
    }

    mode_label = "RAG" if args.rag else "search"
    if args.graphql:
        mode_label = "raw-GraphQL"

    print(f"\n{'='*60}")
    print(f"  {mode_label} Query")
    print(f"  URL: {url}")
    print(f"  Endpoint: {endpoint_id} / Partition: {part_id}")
    print(f"  Mode: {args.mode}")
    print(f"  Query: {args.query or '<raw GraphQL>'}")
    print(f"{'='*60}\n")

    # ── Send request ─────────────────────────────────────────────────
    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=60)
    except requests.ConnectionError:
        print(f"ERROR: Cannot connect to {base_url}")
        print("Is the gateway daemon running? Start it with:")
        print("  python -m silvaengine_gateway.tests.run_daemon")
        sys.exit(1)

    # ── Print response ───────────────────────────────────────────────
    if args.raw:
        print(resp.text)
        return

    try:
        data = resp.json()
    except json.JSONDecodeError:
        print(f"Status: {resp.status_code}")
        print(f"Response (non-JSON): {resp.text[:2000]}")
        return

    print(f"Status: {resp.status_code}")

    if resp.status_code != 200:
        print(f"Error: {json.dumps(data, indent=2)}")
        return

    # ── Pretty print results ─────────────────────────────────────────
    if "errors" in data:
        print("GraphQL Errors:")
        for err in data["errors"]:
            print(f"  - {err.get('message', err)}")
            if err.get("path"):
                print(f"    path: {err['path']}")
            if err.get("extensions"):
                print(f"    extensions: {json.dumps(err['extensions'], indent=6)}")
        return

    # Navigate to the search/rag result
    result_data = data.get("data", {})

    if args.rag:
        rag_result = result_data.get("rag", {})
        answer = rag_result.get("answer", "No answer returned")
        context = rag_result.get("context", [])
        print(f"\nAnswer:\n  {answer}")
        print(f"\nContext items: {len(context)}")
        for i, ctx in enumerate(context):
            print(f"\n  ── Context [{i+1}] ──────────────────────────────")
            if isinstance(ctx, dict):
                for key, val in ctx.items():
                    val_str = json.dumps(val, indent=4, ensure_ascii=False) if isinstance(val, (dict, list)) else str(val)
                    # Truncate very long values but show more than before
                    if len(val_str) > 1000:
                        val_str = val_str[:1000] + f"\n    ... ({len(val_str)} chars total)"
                    print(f"    {key}: {val_str}")
            else:
                val_str = str(ctx)
                if len(val_str) > 1000:
                    val_str = val_str[:1000] + f"\n    ... ({len(val_str)} chars total)"
                print(f"    {val_str}")
    else:
        search_result = result_data.get("search", {})
        results = search_result.get("results", [])
        total = search_result.get("total", 0)
        page = search_result.get("page", "?")
        limit = search_result.get("limit", "?")

        # Show summary with all top-level keys from search_result
        summary_parts = [f"total: {total}", f"page: {page}", f"limit: {limit}"]
        other_keys = [k for k in search_result if k not in ("results", "total", "page", "limit")]
        for k in other_keys:
            summary_parts.append(f"{k}: {search_result[k]}")
        print(f"Results: {len(results)} items ({', '.join(summary_parts)})")

        if not results:
            print("  (no results returned)")
        print()

        for i, item in enumerate(results):
            print(f"  ── Result [{i+1}] ──────────────────────────────")
            if isinstance(item, dict):
                # Extract well-known fields first for structured display
                content = item.get("content", item.get("text"))
                score = item.get("score")
                label = item.get("label")
                metadata = item.get("metadata", {})
                node_id = item.get("id", item.get("nodeId", item.get("node_id")))

                # Structured header
                header_parts = []
                if score is not None:
                    header_parts.append(f"score={score}")
                if label:
                    header_parts.append(f"label={label}")
                if node_id:
                    header_parts.append(f"id={node_id}")
                if header_parts:
                    print(f"    {' | '.join(header_parts)}")

                # Content (full, with smart truncation)
                if content is not None:
                    content_str = str(content)
                    if len(content_str) > 2000:
                        print(f"    content ({len(content_str)} chars):")
                        print(f"      {content_str[:2000]}")
                        print(f"      ... ({len(content_str) - 2000} more chars)")
                    else:
                        print(f"    content: {content_str}")

                # Metadata (structured)
                if metadata and isinstance(metadata, dict):
                    print(f"    metadata:")
                    for mk, mv in metadata.items():
                        mv_str = json.dumps(mv, indent=6, ensure_ascii=False) if isinstance(mv, (dict, list)) else str(mv)
                        if len(mv_str) > 500:
                            mv_str = mv_str[:500] + f" ... ({len(mv_str)} chars total)"
                        print(f"      {mk}: {mv_str}")
                elif metadata:
                    print(f"    metadata: {metadata}")

                # Any other fields not yet printed
                printed_keys = {"content", "text", "score", "label", "metadata", "id", "nodeId", "node_id"}
                other = {k: v for k, v in item.items() if k not in printed_keys}
                if other:
                    print(f"    additional fields:")
                    for ok, ov in other.items():
                        ov_str = json.dumps(ov, indent=6, ensure_ascii=False) if isinstance(ov, (dict, list)) else str(ov)
                        if len(ov_str) > 500:
                            ov_str = ov_str[:500] + f" ... ({len(ov_str)} chars total)"
                        print(f"      {ok}: {ov_str}")
            else:
                val_str = str(item)
                if len(val_str) > 1000:
                    val_str = val_str[:1000] + f" ... ({len(val_str)} chars total)"
                print(f"    {val_str}")

    print()


if __name__ == "__main__":
    main()