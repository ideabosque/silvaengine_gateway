#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Generate a JWT for the SilvaEngine Gateway from .env settings.

Reuses the gateway's own auth code, so the token is byte-for-byte what the
running gateway would issue from POST /auth/token — no server needs to be up
(for local auth).

Usage:
    # Admin token using ADMIN_USERNAME / ADMIN_PASSWORD from .env:
    python -m silvaengine_gateway.tests.gen_token

    # A specific user (must exist in LOCAL_USER_FILE):
    python -m silvaengine_gateway.tests.gen_token --username alice --password secret

    # Craft a local token directly, no login (local provider only):
    python -m silvaengine_gateway.tests.gen_token --custom --username svc --roles admin,user
    python -m silvaengine_gateway.tests.gen_token --custom --username svc --forever

    # Print only the raw token (pipeable):  TOKEN=$(python -m ... gen_token --raw)
    python -m silvaengine_gateway.tests.gen_token --raw

    # Cognito provider (needs --password; calls AWS):
    python -m silvaengine_gateway.tests.gen_token --username u --password p

All connection/auth params are read from the .env file (defaults to the .env
in this directory).
"""

from __future__ import print_function

__author__ = "silvaengine"

import argparse
import logging
import os
import sys
from pathlib import Path

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from dotenv import load_dotenv


def _promote_editable_finders() -> None:
    import sys as _sys
    from importlib.machinery import PathFinder

    meta_path = _sys.meta_path
    editable = [
        f
        for f in meta_path
        if hasattr(f, "__name__") and f.__name__ == "_EditableFinder"
    ]
    if not editable:
        return
    pf_index = next((i for i, f in enumerate(meta_path) if f is PathFinder), None)
    if pf_index is None:
        return
    if all(meta_path.index(f) < pf_index for f in editable):
        return
    for f in editable:
        meta_path.remove(f)
    for i, f in enumerate(meta_path):
        if f is PathFinder:
            pf_index = i
            break
    for f in reversed(editable):
        meta_path.insert(pf_index, f)


_promote_editable_finders()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate a gateway JWT from .env settings"
    )
    parser.add_argument(
        "--dotenv",
        type=str,
        default=None,
        help="Path to .env (default: <this dir>/.env)",
    )
    parser.add_argument(
        "--username",
        type=str,
        default=None,
        help="Username (default: ADMIN_USERNAME from .env)",
    )
    parser.add_argument(
        "--password",
        type=str,
        default=None,
        help="Password (default: ADMIN_PASSWORD from .env)",
    )
    parser.add_argument(
        "--custom",
        action="store_true",
        help="Craft a local token directly, skipping the login "
        "flow (local provider only)",
    )
    parser.add_argument(
        "--roles",
        type=str,
        default="admin",
        help="Comma-separated roles for --custom (default: admin)",
    )
    parser.add_argument(
        "--forever",
        action="store_true",
        help="With --custom: non-expiring (perm) token",
    )
    parser.add_argument(
        "--raw", action="store_true", help="Print only the token (pipeable)"
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    # Keep stdout clean so --raw output is just the token.
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)

    env_file = args.dotenv or str(Path(__file__).parent / ".env")
    if Path(env_file).exists():
        load_dotenv(env_file, override=True)
        if not args.raw:
            print(f"Loaded .env from: {env_file}", file=sys.stderr)
    elif not args.raw:
        print(f"WARNING: .env not found at {env_file}", file=sys.stderr)

    # Initialize GatewayConfig from the full env-derived setting, reusing the
    # gateway's own builder so secret/algorithm/expiry/users/cognito match.
    from silvaengine_gateway.app import build_setting_from_env
    from silvaengine_gateway.config import GatewayConfig

    GatewayConfig.initialize(logging.getLogger("gen_token"), build_setting_from_env())

    username = args.username or GatewayConfig.admin_username or "admin"
    password = args.password or GatewayConfig.admin_password or "admin123"

    if args.custom:
        if GatewayConfig.auth_provider == "cognito":
            print(
                "ERROR: --custom is for local tokens only; the cognito "
                "provider issues tokens via AWS.",
                file=sys.stderr,
            )
            sys.exit(2)
        from silvaengine_gateway.auth.jwt_local import create_local_jwt

        roles = [r.strip() for r in args.roles.split(",") if r.strip()]
        token = create_local_jwt(
            {"username": username, "roles": roles}, forever=args.forever
        )
        meta = (
            f"custom local token  user={username} roles={roles} forever={args.forever}"
        )
    elif GatewayConfig.auth_provider == "cognito":
        from silvaengine_gateway.routes.auth import _get_cognito_token

        token = _get_cognito_token(username, password)["access_token"]
        meta = f"cognito access token  user={username}"
    else:
        from silvaengine_gateway.routes.auth import _get_local_token

        token = _get_local_token(username, password)["access_token"]
        meta = f"local token  user={username}  provider=local"

    if args.raw:
        print(token)
        return

    print(f"\n{'='*60}", file=sys.stderr)
    print(f"  Gateway JWT — {meta}", file=sys.stderr)
    print(f"{'='*60}", file=sys.stderr)
    print(token)
    print(f"\nAuthorization: Bearer {token}", file=sys.stderr)
    print(
        "\nExample:\n"
        f'  curl -H "Authorization: Bearer {token[:24]}..." \\\n'
        "       http://localhost:8765/health",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
