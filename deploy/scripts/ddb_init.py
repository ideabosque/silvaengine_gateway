#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Idempotent DynamoDB Local bootstrap for the Banyan-hosting gateway.

Runs INSIDE a container (python:3.12-slim + pip install boto3) as the
one-shot ``ddb-init`` compose service (deploy/docker-compose.ddb.yml).
The host side is ``deploy/deploy.sh`` — this script never needs Python
or boto3 on the host.

Sequence (every step idempotent — safe to re-run on each deploy, which
is how deploy.sh heals DynamoDB Local's ``-inMemory`` data loss):

1. wait for the DynamoDB Local endpoint to accept requests;
2. create the se-configdata table if missing, wait until ACTIVE;
3. write one row per JSON variable (put_item overwrite semantics);
4. query the setting_id back and verify the row count matches.

Item shape mirrors scripts/seed_seconfig.py (partition key setting_id +
sort key variable + typed value attribute, floats parsed as Decimal), so
the gateway's setting provider and the Lambda chain's ConfigModel read
identical records.

Credentials: with --endpoint-url (DynamoDB Local) auth is ignored, but
boto3 refuses to sign without any — fixed harmless constants are passed.
Without --endpoint-url the normal AWS credential chain applies.

Exit codes: 0 ok; 1 endpoint/table/verify failure; 2 usage/JSON errors.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import time
from decimal import Decimal
from typing import Any, Dict, List, Optional

EXIT_OK = 0
EXIT_RUNTIME = 1
EXIT_USAGE = 2

# 匹配 <UPPER_CASE> 占位符（含数字，如 <NEO4J_PASSWORD>）
PLACEHOLDER_RE = re.compile(r"<[A-Z][A-Z0-9_]*>")


class ConfigError(Exception):
    """Invalid seed JSON / usage problem (exit code 2)."""


class RuntimeFailure(Exception):
    """Endpoint/table/verify failure (exit code 1)."""


def find_placeholders(value: Any, path: str = "root") -> List[str]:
    """Recursively collect unreplaced <UPPER_CASE> placeholder strings."""
    found: List[str] = []
    if isinstance(value, str):
        match = PLACEHOLDER_RE.search(value)
        if match:
            found.append(f"{path}={match.group(0)}")
    elif isinstance(value, dict):
        for key, item in value.items():
            found.extend(find_placeholders(item, f"{path}.{key}"))
    elif isinstance(value, list):
        for idx, item in enumerate(value):
            found.extend(find_placeholders(item, f"{path}[{idx}]"))
    return found


def load_variables(path: str) -> Dict[str, Any]:
    """Load + validate the seed JSON ({variable: value} root object)."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh, parse_float=Decimal)
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"cannot read seed JSON '{path}': {exc}") from exc
    if not isinstance(data, dict) or not data:
        raise ConfigError(
            f"seed JSON root must be a non-empty object {{variable: value}} "
            f"— got: {type(data).__name__}"
        )
    if "_comment" in data:
        raise ConfigError(
            "seed JSON still contains the '_comment' template line — "
            "remove it (seeding would write it as a junk variable)"
        )
    leftovers = find_placeholders(data)
    if leftovers:
        raise ConfigError(
            "unreplaced <PLACEHOLDER> value(s) in seed JSON: "
            + "; ".join(leftovers)
        )
    return data


def build_resource(endpoint_url: str, region_name: str) -> Any:
    """boto3 dynamodb resource; local endpoints get harmless fixed creds."""
    import boto3  # container-side dependency only

    kwargs: Dict[str, Any] = {"region_name": region_name}
    if endpoint_url:
        kwargs["endpoint_url"] = endpoint_url
        kwargs["aws_access_key_id"] = "local"
        kwargs["aws_secret_access_key"] = "local"
    return boto3.resource("dynamodb", **kwargs)


def wait_for_endpoint(resource: Any, max_wait: int) -> None:
    """Block until the DDB endpoint answers list_tables (or give up)."""
    client = resource.meta.client
    deadline = time.monotonic() + max_wait
    last_error: Optional[Exception] = None
    while time.monotonic() < deadline:
        try:
            client.list_tables()
            return
        except Exception as exc:  # endpoint booting / not reachable yet
            last_error = exc
            time.sleep(2)
    raise RuntimeFailure(
        f"DynamoDB endpoint not reachable within {max_wait}s "
        f"(last error: {last_error})"
    )


def ensure_table(resource: Any, table_name: str, max_wait: int) -> Any:
    """Create se-configdata if missing; return once it is ACTIVE."""
    client = resource.meta.client
    deadline = time.monotonic() + max_wait
    while True:
        try:
            resp = client.describe_table(TableName=table_name)
            if resp["Table"]["TableStatus"] == "ACTIVE":
                return resource.Table(table_name)
        except client.exceptions.ResourceNotFoundException:
            try:
                client.create_table(
                    TableName=table_name,
                    KeySchema=[
                        {"AttributeName": "setting_id", "KeyType": "HASH"},
                        {"AttributeName": "variable", "KeyType": "RANGE"},
                    ],
                    AttributeDefinitions=[
                        {"AttributeName": "setting_id", "AttributeType": "S"},
                        {"AttributeName": "variable", "AttributeType": "S"},
                    ],
                    BillingMode="PAY_PER_REQUEST",
                )
            except client.exceptions.ResourceInUseException:
                pass  # concurrent creator — keep polling for ACTIVE
        if time.monotonic() > deadline:
            raise RuntimeFailure(
                f"table '{table_name}' not ACTIVE within {max_wait}s"
            )
        time.sleep(2)


def seed(table: Any, variables: Dict[str, Any], setting_id: str) -> int:
    """Write one row per variable (put_item overwrite = idempotent)."""
    for name in sorted(variables):
        table.put_item(
            Item={
                "setting_id": setting_id,
                "variable": name,
                "value": variables[name],
            }
        )
        print(f"  seeded {setting_id} :: {name}")
    return len(variables)


def verify(table: Any, variables: Dict[str, Any], setting_id: str) -> None:
    """Query the setting_id back and diff the variable names."""
    names: List[str] = []
    kwargs: Dict[str, Any] = {
        "KeyConditionExpression": "setting_id = :sid",
        "ExpressionAttributeValues": {":sid": setting_id},
    }
    while True:
        resp = table.query(**kwargs)
        for item in resp.get("Items", []):
            if item.get("variable"):
                names.append(str(item["variable"]))
        if "LastEvaluatedKey" not in resp:
            break
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    missing = sorted(set(variables) - set(names))
    extra = sorted(set(names) - set(variables))
    if missing or extra:
        raise RuntimeFailure(
            f"verify failed for setting_id={setting_id}: "
            f"missing={missing} extra={extra}"
        )


def _expect_config_error(payload: str, needle: str) -> bool:
    """Write payload to a temp JSON file; assert ConfigError with needle."""
    handle = tempfile.NamedTemporaryFile(
        "w", suffix=".json", delete=False, encoding="utf-8"
    )
    handle.write(payload)
    handle.close()
    try:
        load_variables(handle.name)
        return False
    except ConfigError as exc:
        return needle in str(exc)
    finally:
        os.remove(handle.name)


def self_test() -> int:
    """Pure-logic checks (no boto3, no container needed)."""
    checks: List[tuple] = []

    payload = '{"region_name": "us-west-2", "ratio": 0.5, "plugins": [{"config": {}}]}'
    handle = tempfile.NamedTemporaryFile(
        "w", suffix=".json", delete=False, encoding="utf-8"
    )
    handle.write(payload)
    handle.close()
    try:
        data = load_variables(handle.name)
        checks.append(("valid JSON loads", len(data) == 3))
        checks.append(("floats become Decimal", isinstance(data["ratio"], Decimal)))
    except ConfigError:
        checks.append(("valid JSON loads", False))
        checks.append(("floats become Decimal", False))
    finally:
        os.remove(handle.name)

    checks.append(
        ("_comment line rejected", _expect_config_error('{"_comment": "x"}', "_comment"))
    )
    checks.append(
        (
            "nested placeholder rejected",
            _expect_config_error(
                '{"neo4j": {"password": "<NEO4J_PASSWORD>"}}', "<NEO4J_PASSWORD>"
            ),
        )
    )
    checks.append(("empty object rejected", _expect_config_error("{}", "non-empty")))
    checks.append(("non-object root rejected", _expect_config_error("[1, 2]", "root")))
    checks.append(
        ("garbage JSON rejected", _expect_config_error("not json", "cannot read"))
    )

    failed = [desc for desc, ok_flag in checks if not ok_flag]
    for desc, ok_flag in checks:
        print(f"  {'PASS' if ok_flag else 'FAIL'}: {desc}")
    if failed:
        print(f"self-test FAILED ({len(failed)}/{len(checks)})")
        return EXIT_RUNTIME
    print(f"self-test OK ({len(checks)}/{len(checks)})")
    return EXIT_OK


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Idempotent DynamoDB Local bootstrap: wait endpoint -> ensure "
            "se-configdata table -> seed rows from JSON -> verify row count."
        )
    )
    parser.add_argument(
        "--file",
        default="/ddbenv/se-configdata.local.json",
        help="seed JSON {variable: value} (default: %(default)s)",
    )
    parser.add_argument(
        "--table", default="se-configdata", help="table (default: %(default)s)"
    )
    parser.add_argument(
        "--setting-id",
        default="beta_core_banyan",
        help="partition key {stage}_{area}_{endpoint_id} (default: %(default)s)",
    )
    parser.add_argument(
        "--region", default="us-west-2", help="AWS region (default: %(default)s)"
    )
    parser.add_argument(
        "--endpoint-url",
        default="http://ddb-local:8000",
        help="DynamoDB endpoint override (DDB Local); empty = real AWS",
    )
    parser.add_argument(
        "--max-wait", type=int, default=180, help="per-step wait budget in seconds"
    )
    parser.add_argument(
        "--self-test", action="store_true", help="run built-in logic tests and exit"
    )
    args = parser.parse_args()

    if args.self_test:
        return self_test()

    try:
        variables = load_variables(args.file)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE

    try:
        resource = build_resource(args.endpoint_url, args.region)
        wait_for_endpoint(resource, args.max_wait)
        table = ensure_table(resource, args.table, args.max_wait)
        count = seed(table, variables, args.setting_id)
        verify(table, variables, args.setting_id)
    except RuntimeFailure as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_RUNTIME
    except Exception as exc:  # boto3/botocore/network errors land here
        print(f"error: se-configdata bootstrap failed: {exc}", file=sys.stderr)
        return EXIT_RUNTIME

    print(
        f"OK: table '{args.table}' ready; {count} variable(s) seeded + verified "
        f"for setting_id={args.setting_id}"
    )
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())