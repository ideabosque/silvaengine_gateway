#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Call the RFQ Engine inquire_catalog endpoint through the SilvaEngine Gateway.

inquire_catalog routes a search query through KGE (knowledge_graph_engine) and
returns matching catalog items.

The ``query`` argument must be a JSON object with at least a ``queryText`` key.
Additional keys mirror KGE's search variables:

    queryText       – free-text search (required)
    searchMode      – "text2cypher" (default) | "vector" | "keyword"
    indexName       – index to search (default: "vector")
    topK            – max results (default: 10)
    page            – page number (default: 1)
    limit           – page size  (default: 10)

Usage:
    # Default search (text2cypher, namespace DEFAULT):
    python -m silvaengine_gateway.tests.call_inquire_catalog

    # Search with a custom query text:
    python -m silvaengine_gateway.tests.call_inquire_catalog \\
        --query-text "flights from CDG to JFK"

    # Search in a specific namespace with vector mode:
    python -m silvaengine_gateway.tests.call_inquire_catalog \\
        --namespace FLIGHT --search-mode vector --query-text "business class"

    # Full JSON query (--query overrides --query-text / --search-mode etc.):
    python -m silvaengine_gateway.tests.call_inquire_catalog \\
        --query '{"queryText": "hotels in Paris", "topK": 5}'

    # Raw GraphQL (full control):
    python -m silvaengine_gateway.tests.call_inquire_catalog \\
        --graphql '{"query": "{ inquireCatalog(...) { ... } }"}'

All connection params (base_url, endpoint_id, part_id, auth credentials) are
read from the .env file in the same directory as this script.
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
_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent.parent)
_RFQ_ROOT = str(Path(__file__).resolve().parent.parent.parent.parent / "rfq_engine")
for _p in [_PROJECT_ROOT, _RFQ_ROOT]:
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
        description="Call RFQ Engine inquire_catalog through the SilvaEngine Gateway"
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

    # ── Inquire Catalog ──────────────────────────────────────────
    parser.add_argument(
        "--namespace", "-n", type=str, default=None,
        help="Catalog namespace (default: from .env INQUIRE_NAMESPACE or 'DEFAULT')"
    )
    parser.add_argument(
        "--query-text", "-qt", type=str, default=None,
        help="Search query text, e.g. 'flights from CDG to JFK'"
    )
    parser.add_argument(
        "--search-mode", "-sm", type=str, default=None,
        choices=["text2cypher", "vector", "keyword"],
        help="KGE search mode (default: text2cypher)"
    )
    parser.add_argument(
        "--top-k", type=int, default=None,
        help="Max results from KGE search (default: 10)"
    )
    parser.add_argument(
        "--page", type=int, default=None,
        help="Page number for KGE search (default: 1)"
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Page size for KGE search (default: 10)"
    )
    parser.add_argument(
        "--query", "-q", type=str, default=None,
        help="Full query as JSON string (overrides --query-text / --search-mode etc.)"
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


def build_graphql_payload(args: argparse.Namespace) -> dict:
    """Build the GraphQL request body from CLI args.

    Uses GraphQL variables for JSONCamelCase scalars to avoid quoting issues.
    The ``query`` arg on ``inquireCatalog`` is type JSONCamelCase — a scalar
    that parses both inline object literals and JSON strings via variables.
    """
    if args.graphql:
        return json.loads(args.graphql)

    # ── Build the query dict ──────────────────────────────────────
    # If --query was given, use it directly as the query object.
    # Otherwise, assemble from --query-text / --search-mode / etc.
    if args.query:
        query_obj = json.loads(args.query) if isinstance(args.query, str) else args.query
    else:
        # queryText is required by the handler — fall back to .env or a default
        query_text = args.query_text or os.getenv("INQUIRE_QUERY_TEXT", "long haul international flights with meals included")
        query_obj = {"queryText": query_text}
        if args.search_mode:
            query_obj["searchMode"] = args.search_mode
        if args.top_k is not None:
            query_obj["topK"] = args.top_k
        if args.page is not None:
            query_obj["page"] = args.page
        if args.limit is not None:
            query_obj["limit"] = args.limit

    # ── Build GraphQL query with variables ─────────────────────────
    # Using variables avoids quoting issues with JSONCamelCase scalars.
    # inquireCatalog(namespace: String, nodeId: String, query: JSONCamelCase)
    #   -> CatalogInquiryResultType {
    #        namespace, nodeId, payload, fetchedAt, ttlSeconds,
    #        errorCode, errorMessage
    #      }
    namespace = args.namespace or os.getenv("INQUIRE_NAMESPACE", "DEFAULT")

    # NOTE: nodeId is NOT supported by the handler yet (raises
    #       OperationUnsupportedError), so we intentionally omit it.

    query = """query InquireCatalog($namespace: String, $query: JSONCamelCase) {
  inquireCatalog(namespace: $namespace, query: $query) {
    namespace
    nodeId
    payload
    fetchedAt
    ttlSeconds
    errorCode
    errorMessage
  }
}"""

    return {
        "query": query,
        "variables": {
            "namespace": namespace,
            "query": query_obj,
        },
    }


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
    namespace = args.namespace or os.getenv("INQUIRE_NAMESPACE", "DEFAULT")

    # ── Authenticate ────────────────────────────────────────────────
    if args.token:
        token = args.token
    else:
        username = args.username or os.getenv("ADMIN_USERNAME", "admin")
        password = args.password or os.getenv("ADMIN_PASSWORD", "admin123")
        print(f"Authenticating as {username}...")
        token = get_token(base_url, username, password)

    # ── Build request ────────────────────────────────────────────────
    # RFQ Engine GraphQL endpoint
    graphql_path = f"/{endpoint_id}/rfq_graphql"
    url = f"{base_url}{graphql_path}"

    payload = build_graphql_payload(args)

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Part-Id": part_id,
    }

    query_label = "inquire_catalog"
    if args.graphql:
        query_label = "raw-GraphQL"

    # Extract query text for display
    display_query_text = args.query_text or os.getenv("INQUIRE_QUERY_TEXT", "long haul international flights with meals included")
    if args.query:
        display_query_text = args.query

    print(f"\n{'='*60}")
    print(f"  {query_label} Query")
    print(f"  URL: {url}")
    print(f"  Endpoint: {endpoint_id} / Partition: {part_id}")
    print(f"  Namespace: {namespace}")
    print(f"  Query text: {display_query_text}")
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

    # ── Handle GraphQL errors ─────────────────────────────────────────
    if "errors" in data:
        print("GraphQL Errors:")
        for err in data["errors"]:
            print(f"  - {err.get('message', err)}")
            if err.get("path"):
                print(f"    path: {err['path']}")
            if err.get("extensions"):
                print(f"    extensions: {json.dumps(err['extensions'], indent=6)}")
        return

    # ── Pretty print result ──────────────────────────────────────────
    result = data.get("data", {}).get("inquireCatalog") or data.get("data", {})

    if not result:
        print("No result returned.")
        print(f"Raw response: {json.dumps(data, indent=2)}")
        return

    # Check for in-band errors
    error_code = result.get("errorCode")
    error_message = result.get("errorMessage")
    if error_code:
        print(f"Error: [{error_code}] {error_message}")
        print(f"  namespace: {result.get('namespace', '?')}")
        print(f"  nodeId: {result.get('nodeId', '?')}")
        return

    # Success — print structured result
    print(f"Namespace: {result.get('namespace', '?')}")
    print(f"Node ID: {result.get('nodeId', '?')}")

    fetched_at = result.get("fetchedAt")
    ttl = result.get("ttlSeconds")
    if fetched_at:
        print(f"Fetched at: {fetched_at}")
    if ttl is not None:
        print(f"TTL (seconds): {ttl}")

    payload_data = result.get("payload")
    if payload_data is not None:
        print("\nPayload:")
        _print_payload(payload_data)
    else:
        print("\nPayload: (none)")

    print()


def _print_payload(payload_data, indent=0):
    """Recursively pretty-print payload with product-aware formatting."""
    prefix = "  " * (indent + 1)
    if isinstance(payload_data, dict):
        # If it looks like a KGE search result, highlight key fields
        total = payload_data.get("total")
        page = payload_data.get("page")
        limit = payload_data.get("limit")
        results = payload_data.get("results")

        if total is not None:
            print(f"{prefix}Total results: {total}")
            if page is not None:
                print(f"{prefix}Page: {page}")
            if limit is not None:
                print(f"{prefix}Limit: {limit}")

        if isinstance(results, list):
            print(f"{prefix}Results ({len(results)} items):\n")
            for i, item in enumerate(results):
                _print_catalog_item(item, i, indent + 1)
            # Don't also print results raw
            _skip_keys = {"results", "total", "page", "limit"}
            remaining = {k: v for k, v in payload_data.items() if k not in _skip_keys}
            if remaining:
                print(f"{prefix}Other fields:")
                for k, v in remaining.items():
                    _print_value(k, v, indent + 2)
        else:
            # Generic dict
            for k, v in payload_data.items():
                _print_value(k, v, indent + 1)
    elif isinstance(payload_data, list):
        print(f"{prefix}({len(payload_data)} items)")
        for i, item in enumerate(payload_data):
            print(f"{prefix}[{i}]:")
            _print_payload(item, indent + 1)
    else:
        print(f"{prefix}{payload_data}")


# ── Product-type formatters ──────────────────────────────────────────────
# Each formatter receives the node's ``properties`` dict and prints
# a human-friendly summary.  Add new labels here as the catalog grows.

_FLIGHT_LABELS = {"Flight", "FLIGHT"}
_HOTEL_LABELS = {"Hotel", "HOTEL"}


def _format_flight(props: dict, prefix: str) -> None:
    """Pretty-print a Flight node's properties."""
    route = props.get("route", "?")
    cabin = props.get("cabinClass", "?")
    operator = props.get("operatedBy", "?")
    baggage = props.get("baggageAllowance", "?")
    meal = props.get("mealIncluded", "?")

    meal_icon = "✓" if meal is True else ("✗" if meal is False else str(meal))
    print(f"{prefix}┌─ ✈  Flight ──────────────────────────────────")
    print(f"{prefix}│  Route:              {route}")
    print(f"{prefix}│  Cabin Class:        {cabin}")
    print(f"{prefix}│  Operated By:        {operator}")
    print(f"{prefix}│  Baggage Allowance:  {baggage} kg")
    print(f"{prefix}│  Meal Included:      {meal_icon}")
    print(f"{prefix}└──────────────────────────────────────────────")


def _format_hotel(props: dict, prefix: str) -> None:
    """Pretty-print a Hotel node's properties."""
    name = props.get("name", props.get("hotelName", "?"))
    location = props.get("location", props.get("city", "?"))
    stars = props.get("starRating", props.get("stars", "?"))
    amenities = props.get("amenities", "?")
    price = props.get("pricePerNight", props.get("price", "?"))

    print(f"{prefix}┌─ 🏨  Hotel ───────────────────────────────────")
    print(f"{prefix}│  Name:               {name}")
    print(f"{prefix}│  Location:           {location}")
    print(f"{prefix}│  Star Rating:        {stars}")
    print(f"{prefix}│  Price/Night:        {price}")
    if isinstance(amenities, (list,)):
        print(f"{prefix}│  Amenities:          {', '.join(str(a) for a in amenities)}")
    else:
        print(f"{prefix}│  Amenities:          {amenities}")
    print(f"{prefix}└──────────────────────────────────────────────")


def _format_generic(label: str, props: dict, prefix: str) -> None:
    """Fallback formatter for any node label."""
    icon = "📦"
    print(f"{prefix}┌─ {icon}  {label} ───────────────────────────────────")
    for k, v in props.items():
        val_str = str(v)
        if len(val_str) > 80:
            val_str = val_str[:77] + "..."
        print(f"{prefix}│  {_pretty_key(k)}:  {val_str}")
    print(f"{prefix}└──────────────────────────────────────────────")


def _pretty_key(key: str) -> str:
    """Convert camelCase key to Title Case for display."""
    import re
    # Insert space before uppercase letters
    spaced = re.sub(r'(?<=[a-z])([A-Z])', r' \1', key)
    # Capitalize first letter
    return spaced[0].upper() + spaced[1:] if spaced else key


def _print_catalog_item(item: dict, index: int, indent: int) -> None:
    """Print a single catalog result item with product-type-aware formatting.

    Each KGE search result has the structure:
        { content: str, metadata: { <key>: { elementId, label, properties, ... } } }

    The metadata key is a short letter (f=Flight, d=Document, h=Hotel, etc.)
    and ``label`` identifies the product type.

    KGE text2cypher may also return flat dot-notation keys like
    ``f.route``, ``f.cabinClass`` — these are reconstructed into
    a synthetic node for display.
    """
    prefix = "  " * (indent + 1)

    # Extract product node from metadata
    metadata = item.get("metadata", {})
    content = item.get("content", "")

    # Find the first metadata entry that has a 'label' (skip bare 'metadata' sub-dicts)
    node = None
    node_label = None
    for key, val in metadata.items():
        if isinstance(val, dict) and "label" in val and "properties" in val:
            node = val
            node_label = val["label"]
            break

    if node is None:
        # Try dot-notation reconstruction: KGE text2cypher sometimes returns
        # flat keys like "f.route", "f.cabinClass", "f.operatedBy" etc.
        # Group them by prefix and treat the prefix as a node label hint.
        dot_groups: dict = {}
        for key, val in metadata.items():
            if isinstance(val, dict):
                continue  # skip nested dicts (e.g. bare "metadata": {})
            if "." in key:
                prefix_key, prop_key = key.split(".", 1)
                if prefix_key not in dot_groups:
                    dot_groups[prefix_key] = {}
                dot_groups[prefix_key][prop_key] = val

        if dot_groups:
            # Use the first dot-prefix group as the product node
            _pg_key, props = next(iter(dot_groups.items()))
            # Infer label from prefix: f→Flight, h→Hotel, d→Document, etc.
            _label_hints = {"f": "Flight", "h": "Hotel", "d": "Document"}
            inferred_label = _label_hints.get(_pg_key, _pg_key.capitalize())
            _format_generic(inferred_label, props, prefix)
            if content:
                content_preview = content[:200] + "..." if len(content) > 200 else content
                print(f"{prefix}  Content: {content_preview}")
            print()
            return

        # Fallback: generic print
        print(f"{prefix}[{index}] (unstructured)")
        _print_payload(item, indent + 1)
        return

    props = node.get("properties", {})

    # Dispatch to type-specific formatter
    if node_label in _FLIGHT_LABELS:
        _format_flight(props, prefix)
    elif node_label in _HOTEL_LABELS:
        _format_hotel(props, prefix)
    else:
        _format_generic(node_label, props, prefix)

    # Append content if non-empty
    if content:
        content_preview = content[:200] + "..." if len(content) > 200 else content
        print(f"{prefix}  Content: {content_preview}")

    print()


def _print_value(key, value, indent):
    prefix = "  " * (indent + 1)
    if isinstance(value, (dict, list)):
        print(f"{prefix}{key}:")
        _print_payload(value, indent + 1)
    else:
        val_str = str(value)
        if len(val_str) > 200:
            val_str = val_str[:200] + "..."
        print(f"{prefix}{key}: {val_str}")


if __name__ == "__main__":
    main()