#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Launch the SilvaEngine Gateway daemon for integration testing.

Usage:
    python -m silvaengine_gateway.tests.run_daemon [--port PORT] [--dotenv PATH]

Reads environment variables from a .env file (defaults to the .env file
in the same directory as this script). Starts the gateway on the given
port and blocks until Ctrl+C.

Typical workflow:
    1. Terminal 1:  python -m silvaengine_gateway.tests.run_daemon
    2. Terminal 2:  python -m silvaengine_gateway.tests.call_search
"""

from __future__ import print_function

__author__ = "silvaengine"
import argparse
import logging
import os
import sys
from pathlib import Path

# ── Ensure project roots are on sys.path ───────────────────────────
# When run via VS Code debugger or `python path/to/run_daemon.py`, the sibling
# package roots aren't on sys.path. Add the gateway plus every engine the route
# manifest dispatches to, so their `dispatch_*` functions resolve at startup —
# otherwise the gateway silently skips those routes and they 404. Roots are
# inserted ahead of the monorepo cwd so the real package wins over the
# namespace-package shadow of the project directory.
_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent.parent)
_MONOREPO = Path(__file__).resolve().parent.parent.parent.parent
_SIBLING_ROOTS = [
    str(_MONOREPO / name)
    for name in (
        "knowledge_graph_engine",
        "ai_rfq_engine",
        "mcp_daemon_engine",
        "ai_agent_core_engine",
        "ai_agent_handler",
    )
]
for _p in [_PROJECT_ROOT, *_SIBLING_ROOTS]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

import uvicorn
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
        description="Launch the SilvaEngine Gateway daemon for integration testing"
    )
    parser.add_argument(
        "--port", type=int, default=None,
        help="Port to listen on (default: from .env or 8765)"
    )
    parser.add_argument(
        "--dotenv", type=str, default=None,
        help="Path to .env file (default: <this_script_dir>/.env)"
    )
    parser.add_argument(
        "--host", type=str, default=None,
        help="Host to bind (default: from .env or 0.0.0.0)"
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    # ── Load .env ───────────────────────────────────────────────────
    env_file = args.dotenv or str(Path(__file__).parent / ".env")
    if not Path(env_file).exists():
        print(f"ERROR: .env file not found: {env_file}")
        print("Copy .env.example to .env and fill in real values.")
        sys.exit(1)

    load_dotenv(env_file, override=True)
    print(f"Loaded environment from: {env_file}")

    # Ensure OPENAI_API_KEY (uppercase) is set for libraries that read it from env
    # (dotenv preserves case, but openai/neo4j_graphrag expect uppercase)
    _oak = os.getenv("OPENAI_API_KEY") or os.getenv("openai_api_key")
    if _oak:
        os.environ["OPENAI_API_KEY"] = _oak

    # ── Resolve CLI overrides (CLI arg > .env > hardcoded default) ──
    host = args.host or os.getenv("GATEWAY_HOST", "0.0.0.0")
    port = args.port or int(os.getenv("GATEWAY_PORT", "8765"))

    # ── Configure logging ───────────────────────────────────────────
    logging.basicConfig(
        stream=sys.stdout,
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    # ── Build setting dict from env (unified with app.py build_setting_from_env) ──
    # Using build_setting_from_env() ensures functs_on_local includes
    # send_data_to_stream and async_insert_update_tool_call for WebSocket
    # streaming routes (ai_agent_core_engine), which a hand-coded dict
    # would miss.  We then apply CLI overrides for host/port.
    from silvaengine_gateway.app import build_setting_from_env

    setting = build_setting_from_env()

    # CLI overrides (CLI arg > .env > default already resolved by build_setting_from_env)
    setting["host"] = host
    setting["port"] = str(port)

    # Ensure OPENAI_API_KEY (uppercase) is set for libraries that read it from env
    # (dotenv preserves case, but openai/neo4j_graphrag expect uppercase)
    _oak = os.getenv("OPENAI_API_KEY") or os.getenv("openai_api_key")
    if _oak:
        os.environ["OPENAI_API_KEY"] = _oak

    # ── Create app ──────────────────────────────────────────────────
    from silvaengine_gateway.app import create_app

    app = create_app(setting)

    print(f"\n{'='*60}")
    print(f"  SilvaEngine Gateway")
    print(f"  http://{host}:{port}")
    print(f"  Auth: {setting.get('auth_provider', 'local')}")
    print(f"  Endpoint: {setting.get('endpoint_id')} / Partition: {setting.get('part_id')}")
    print(f"  Neo4j: {setting.get('neo4j_uri', 'n/a')}")
    ws_routes = [k for k in setting.get("functs_on_local", {}) if k not in ("knowledge_graph_graphql", "ai_rfq_graphql")]
    if ws_routes:
        print(f"  WebSocket streaming: {', '.join(ws_routes)}")
    print(f"{'='*60}\n")

    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()