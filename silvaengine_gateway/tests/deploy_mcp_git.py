#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Deploy (install/refresh/check) an MCP module from a Git repository via the
SilvaEngine Gateway.

Flow: authenticate → installMcpPackageFromGit → verify. Optionally run a
version check (checkMcpGitPackageVersion) or a refresh
(refreshMcpGitPackage) using metadata persisted from a previous install.

Usage:
    # Start the gateway (terminal 1):
    python -m silvaengine_gateway.tests.run_daemon

    # Install a module from a public GitHub repo at a pinned tag:
    python -m silvaengine_gateway.tests.deploy_mcp_git \\
        --git-url https://github.com/org/mcp_my_connector.git \\
        --git-ref v1.2.3

    # Private repo over HTTPS with a token (GIT_TOKEN is read from .env;
    # the daemon injects it into ls-remote and pip automatically):
    #   export GIT_TOKEN=ghp_...      (in the GATEWAY's environment, not here)

    # Private repo over SSH (git@host:org/repo.git):
    #   SSH auth uses the system key / baked-in deploy key / GIT_SSH_KEY
    #   configured in the gateway's environment — not this client script.

    # Explicit module/package names + setting overrides:
    python -m silvaengine_gateway.tests.deploy_mcp_git \\
        --git-url https://github.com/org/mcp_my_connector.git \\
        --git-ref main \\
        --module-name mcp_my_connector \\
        --package-name mcp_my_connector \\
        --variables '{"apiKey": "xxx", "baseUrl": "https://sandbox.example.com/api/"}'

    # Subdirectory packages (repo root is not the package root):
    python -m silvaengine_gateway.tests.deploy_mcp_git \\
        --git-url https://github.com/org/monorepo.git \\
        --git-ref v2.0.0 \\
        --git-subdirectory packages/mcp_my_connector \\
        --distribution-name mcp-my-connector

    # Version check only (no install):
    python -m silvaengine_gateway.tests.deploy_mcp_git --check \\
        --module-name mcp_my_connector

    # Force a refresh of an already-installed module:
    python -m silvaengine_gateway.tests.deploy_mcp_git --refresh \\
        --module-name mcp_my_connector --updated-by alice

    # Force reinstall even if the remote commit is unchanged:
    python -m silvaengine_gateway.tests.deploy_mcp_git \\
        --git-url ... --git-ref v1.2.3 --force

    # Custom base URL / endpoint / partition:
    python -m silvaengine_gateway.tests.deploy_mcp_git \\
        --git-url ... --base-url http://localhost:8765 \\
        --endpoint-id gpt --part-id nestaging

Connection defaults: BASE_URL=http://localhost:8765, endpoint_id=gpt,
part_id=nestaging. Values can be overridden by .env or CLI flags.
"""

from __future__ import print_function

__author__ = "silvaengine"

import argparse
import io
import json
import os
import sys
from pathlib import Path

# Windows consoles often default to a legacy code page (cp1252) that cannot
# encode the ✓/→ glyphs used below. Force UTF-8 on the real stdout.
if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

# ── Ensure project roots are on sys.path ───────────────────────────
_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent.parent)
_MCP_ROOT = str(
    Path(__file__).resolve().parent.parent.parent.parent / "mcp_daemon_engine"
)
for _p in [_PROJECT_ROOT, _MCP_ROOT]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

import requests
from dotenv import load_dotenv


def _promote_editable_finders() -> None:
    """Move _EditableFinder entries above PathFinder in sys.meta_path."""
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
    pf_index = next(
        (i for i, f in enumerate(meta_path) if f is PathFinder), None
    )
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

# ── Load .env ──────────────────────────────────────────────────────
_ENV_FILE = str(Path(__file__).resolve().parent / ".env")
if Path(_ENV_FILE).exists():
    load_dotenv(_ENV_FILE, override=True)


# ═══════════════════════════════════════════════════════════════════════
# GraphQL mutations
# ═══════════════════════════════════════════════════════════════════════

_INSTALL_MUTATION = """
mutation InstallFromGit(
    $gitUrl: String!,
    $gitRef: String,
    $gitSubdirectory: String,
    $versionStrategy: String,
    $distributionName: String,
    $forceRefresh: Boolean,
    $moduleName: String!,
    $packageName: String!,
    $variables: JSONCamelCase,
    $updatedBy: String!
) {
    installMcpPackageFromGit(
        gitUrl: $gitUrl
        gitRef: $gitRef
        gitSubdirectory: $gitSubdirectory
        versionStrategy: $versionStrategy
        distributionName: $distributionName
        forceRefresh: $forceRefresh
        moduleName: $moduleName
        packageName: $packageName
        variables: $variables
        updatedBy: $updatedBy
    ) {
        ok
        message
        action
        resolvedCommit
        installedPackageVersion
        stats {
            tools
            resources
            prompts
            modules
            settings
        }
    }
}
"""

_CHECK_MUTATION = """
mutation CheckGitVersion($moduleName: String!, $forceCheck: Boolean) {
    checkMcpGitPackageVersion(moduleName: $moduleName, forceCheck: $forceCheck) {
        ok
        message
        needsRefresh
        localCommit
        remoteCommit
        installedPackageVersion
        latestRemoteVersion
        lastCheckedAt
    }
}
"""

_REFRESH_MUTATION = """
mutation RefreshGitPackage($moduleName: String!, $forceRefresh: Boolean, $updatedBy: String!) {
    refreshMcpGitPackage(moduleName: $moduleName, forceRefresh: $forceRefresh, updatedBy: $updatedBy) {
        ok
        message
        action
        resolvedCommit
        installedPackageVersion
        stats {
            tools
            resources
            prompts
            modules
            settings
        }
    }
}
"""

_MODULE_QUERY = """
query ModuleInfo($moduleName: String!) {
    mcpModule(moduleName: $moduleName) {
        moduleName
        packageName
        source
        updatedAt
    }
}
"""


# ═══════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════


def _auth_headers(token, part_id):
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Part-Id": part_id,
    }


def _get_token(base_url, username, password):
    resp = requests.post(
        f"{base_url}/auth/token",
        data={"username": username, "password": password},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def _post_graphql(url, token, query, variables=None, part_id=None):
    payload = {"query": query}
    if variables:
        payload["variables"] = variables
    resp = requests.post(
        url,
        json=payload,
        headers=_auth_headers(token, part_id),
        timeout=600,
    )
    return resp


def _print_install_result(result):
    if not result.get("ok"):
        print(f"ERROR: {result.get('message', 'unknown')}")
        sys.exit(1)
    print(f"  ok ✓  action={result.get('action')}")
    print(f"  resolvedCommit: {result.get('resolvedCommit', '')[:12]}")
    if result.get("installedPackageVersion"):
        print(f"  installedPackageVersion: {result['installedPackageVersion']}")
    stats = result.get("stats") or {}
    print(
        f"  {stats.get('tools', 0)} tools, {stats.get('resources', 0)} resources, "
        f"{stats.get('prompts', 0)} prompts, {stats.get('modules', 0)} modules, "
        f"{stats.get('settings', 0)} settings"
    )
    if result.get("message"):
        print(f"  {result['message']}")


# ═══════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════


def main():
    parser = argparse.ArgumentParser(
        description="Deploy an MCP module from Git via the SilvaEngine Gateway.",
    )
    parser.add_argument(
        "--git-url",
        default=None,
        help="HTTPS or SSH Git URL (required for install).",
    )
    parser.add_argument(
        "--git-ref",
        default=None,
        help="Branch, tag, or commit SHA to install (required when "
        "GIT_REQUIRE_REF is true, the default).",
    )
    parser.add_argument(
        "--git-subdirectory",
        default=None,
        help="Subdirectory inside the repo containing the package.",
    )
    parser.add_argument(
        "--version-strategy",
        default=None,
        choices=["ref", "latest_tag"],
        help="Version discovery strategy (default: ref).",
    )
    parser.add_argument(
        "--distribution-name",
        default=None,
        help="Distribution name for reading the installed package version.",
    )
    parser.add_argument(
        "--force-refresh",
        action="store_true",
        help="Reinstall even if the remote commit is unchanged.",
    )
    parser.add_argument(
        "--module-name",
        default=None,
        help="Module name (required for check/refresh; defaults to package "
        "name for install).",
    )
    parser.add_argument(
        "--package-name",
        default=None,
        help="Package name (defaults to module name for install).",
    )
    parser.add_argument(
        "--variables",
        default=None,
        help='Setting overrides as JSON string, e.g. \'{"apiKey": "xxx"}\'.',
    )
    parser.add_argument(
        "--updated-by",
        default="deploy_git_script",
        help="updatedBy value (default: deploy_git_script).",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Run checkMcpGitPackageVersion instead of installing.",
    )
    parser.add_argument(
        "--force-check",
        action="store_true",
        help="Bypass the TTL cache in --check.",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Run refreshMcpGitPackage (reuses persisted Git metadata).",
    )
    parser.add_argument(
        "--no-verify",
        action="store_true",
        help="Skip the post-deploy mcpModule verification query.",
    )
    parser.add_argument(
        "--base-url",
        default=os.getenv("BASE_URL", "http://localhost:8765"),
        help="Gateway base URL (default: http://localhost:8765).",
    )
    parser.add_argument(
        "--endpoint-id",
        default=os.getenv("endpoint_id", "gpt"),
        help="Endpoint ID (default: gpt).",
    )
    parser.add_argument(
        "--part-id",
        default=os.getenv("part_id", "nestaging"),
        help="Partition ID, sent via Part-Id header (default: nestaging).",
    )
    parser.add_argument(
        "--username",
        default=os.getenv("ADMIN_USERNAME", "admin"),
        help="Admin username (default: admin).",
    )
    parser.add_argument(
        "--password",
        default=os.getenv("ADMIN_PASSWORD", "admin123"),
        help="Admin password (default: admin123).",
    )
    args = parser.parse_args()

    # ── Mode validation ────────────────────────────────────────────
    mode = "install"
    if args.check and args.refresh:
        parser.error("--check and --refresh are mutually exclusive")
    if args.check:
        mode = "check"
    elif args.refresh:
        mode = "refresh"

    if mode == "install" and not args.git_url:
        parser.error("--git-url is required for install")

    if mode in ("check", "refresh") and not args.module_name:
        parser.error(f"--module-name is required for --{mode}")

    # Derive names for install
    package_name = args.package_name
    module_name = args.module_name
    if mode == "install":
        if not package_name:
            # Derive from URL stem: https://github.com/org/mcp_x.git -> mcp_x
            url_stem = args.git_url.rstrip("/").split("/")[-1]
            if url_stem.endswith(".git"):
                url_stem = url_stem[:-4]
            package_name = url_stem
        if not module_name:
            module_name = package_name

    # Parse variables override
    variables_override = None
    if args.variables:
        try:
            variables_override = json.loads(args.variables)
        except json.JSONDecodeError as e:
            print(f"ERROR: Invalid --variables JSON: {e}")
            sys.exit(1)

    base_url = args.base_url
    endpoint_id = args.endpoint_id
    part_id = args.part_id
    graphql_url = f"{base_url}/{endpoint_id}/mcp_daemon_graphql"

    print(f"{'=' * 60}")
    print(f"  MCP Git Deployment ({mode})")
    print(f"  Gateway:   {base_url}")
    print(f"  Endpoint:  {endpoint_id} / Partition: {part_id}")
    if mode == "install":
        print(f"  Git URL:   {args.git_url}")
        print(f"  Ref:       {args.git_ref or '(default HEAD)'}")
        if args.git_subdirectory:
            print(f"  Subdir:    {args.git_subdirectory}")
        print(f"  Module:    {module_name}")
        print(f"  Package:   {package_name}")
    else:
        print(f"  Module:    {module_name}")
    if variables_override:
        print(f"  Variables: {json.dumps(variables_override)}")
    print(f"{'=' * 60}\n")

    # ── 1. Health check ────────────────────────────────────────────
    try:
        health = requests.get(f"{base_url}/health", timeout=5)
        health.raise_for_status()
        print(f"  Health: {health.status_code} ✓")
    except Exception as e:
        print(f"ERROR: Gateway not reachable at {base_url}: {e}")
        print("Start it with: python -m silvaengine_gateway.tests.run_daemon")
        sys.exit(1)

    # ── 2. Authenticate ────────────────────────────────────────────
    print(f"  Authenticating as {args.username}...")
    try:
        token = _get_token(base_url, args.username, args.password)
    except Exception as e:
        print(f"ERROR: Authentication failed: {e}")
        sys.exit(1)
    print(f"  Token acquired ({len(token)} chars) ✓\n")

    # ── 3. Run the requested operation ─────────────────────────────
    if mode == "check":
        print(f"\n  Checking Git package version...")
        resp = _post_graphql(
            graphql_url,
            token,
            _CHECK_MUTATION,
            variables={
                "moduleName": module_name,
                "forceCheck": args.force_check,
            },
            part_id=part_id,
        )
        if resp.status_code != 200:
            print(f"ERROR: check request failed: HTTP {resp.status_code}")
            print(resp.text[:500])
            sys.exit(1)
        data = resp.json()
        if "errors" in data:
            print(f"ERROR: GraphQL errors: {data['errors']}")
            sys.exit(1)
        result = data["data"]["checkMcpGitPackageVersion"]
        if not result.get("ok"):
            print(f"ERROR: {result.get('message', 'unknown')}")
            sys.exit(1)
        print(f"  needsRefresh : {result.get('needsRefresh')}")
        print(f"  localCommit  : {result.get('localCommit', '')[:12]}")
        print(f"  remoteCommit : {result.get('remoteCommit', '')[:12]}")
        if result.get("installedPackageVersion"):
            print(
                f"  installedPackageVersion: {result['installedPackageVersion']}"
            )
        if result.get("latestRemoteVersion"):
            print(f"  latestRemoteVersion    : {result['latestRemoteVersion']}")
        print(f"  {result.get('message', '')}")

        if result.get("needsRefresh"):
            print(
                f"\n  → Run with --refresh --module-name {module_name} "
                f"to update."
            )
        print(f"\n{'=' * 60}")
        print(f"  Version check complete ✓")
        print(f"{'=' * 60}")
        return

    if mode == "refresh":
        print(f"\n  Refreshing Git package...")
        resp = _post_graphql(
            graphql_url,
            token,
            _REFRESH_MUTATION,
            variables={
                "moduleName": module_name,
                "forceRefresh": args.force_refresh,
                "updatedBy": args.updated_by,
            },
            part_id=part_id,
        )
        if resp.status_code != 200:
            print(f"ERROR: refresh request failed: HTTP {resp.status_code}")
            print(resp.text[:500])
            sys.exit(1)
        data = resp.json()
        if "errors" in data:
            print(f"ERROR: GraphQL errors: {data['errors']}")
            sys.exit(1)
        result = data["data"]["refreshMcpGitPackage"]
        print(f"\n  refreshMcpGitPackage:")
        _print_install_result(result)
        print(f"\n{'=' * 60}")
        print(f"  Refresh complete ✓")
        print(f"{'=' * 60}")
        return

    # ── Install ────────────────────────────────────────────────────
    install_vars = {
        "gitUrl": args.git_url,
        "moduleName": module_name,
        "packageName": package_name,
        "updatedBy": args.updated_by,
        "forceRefresh": args.force_refresh,
    }
    if args.git_ref is not None:
        install_vars["gitRef"] = args.git_ref
    if args.git_subdirectory:
        install_vars["gitSubdirectory"] = args.git_subdirectory
    if args.version_strategy:
        install_vars["versionStrategy"] = args.version_strategy
    if args.distribution_name:
        install_vars["distributionName"] = args.distribution_name
    if variables_override:
        install_vars["variables"] = variables_override

    print(f"\n  [1/2] Installing module from Git...")
    print(
        f"  (pip install runs server-side; this can take up to "
        f"GIT_INSTALL_TIMEOUT seconds)"
    )
    resp = _post_graphql(
        graphql_url,
        token,
        _INSTALL_MUTATION,
        variables=install_vars,
        part_id=part_id,
    )
    if resp.status_code != 200:
        print(f"ERROR: install request failed: HTTP {resp.status_code}")
        print(resp.text[:500])
        sys.exit(1)
    data = resp.json()
    if "errors" in data:
        print(f"ERROR: GraphQL errors: {data['errors']}")
        sys.exit(1)
    result = data["data"]["installMcpPackageFromGit"]
    print(f"\n  installMcpPackageFromGit:")
    _print_install_result(result)

    # ── Verify ─────────────────────────────────────────────────────
    if args.no_verify:
        print(f"\n  Verification skipped (--no-verify).")
    else:
        print(f"\n  [2/2] Verifying deployment...")
        resp = _post_graphql(
            graphql_url,
            token,
            _MODULE_QUERY,
            variables={"moduleName": module_name},
            part_id=part_id,
        )
        if resp.status_code == 200:
            data = resp.json()
            module = data.get("data", {}).get("mcpModule")
            if module:
                print(
                    f"  Module '{module_name}': source={module.get('source')} "
                    f"updatedAt={module.get('updatedAt')}"
                )
                if module.get("source") != "git":
                    print(
                        f"  WARNING: expected source='git', got "
                        f"'{module.get('source')}'"
                    )
            else:
                print(
                    f"  WARNING: Module '{module_name}' not found in mcpModule."
                )
        else:
            print(
                f"  WARNING: Verification query returned HTTP {resp.status_code}."
            )

    print(f"\n{'=' * 60}")
    print(f"  Deployment complete ✓")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()