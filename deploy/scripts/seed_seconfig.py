#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Seed a se-configdata record from a JSON file.

Writes one DynamoDB row per variable: partition key ``setting_id``
(e.g. ``beta_core_banyan``) + sort key ``variable`` + attribute ``value``.
Values are native JSON types — dicts become ``M`` maps, lists ``L``, and
numbers ``N`` (floats converted to Decimal, which is what DynamoDB
requires) — exactly the shape the gateway's setting provider and the
Lambda chain's ConfigModel read back.

Primary use: seeding DynamoDB Local for fully-offline runs of the
Banyan-hosting gateway (see deploy/docker-compose.yml, ddb-local
variant). Also works against a real table for fresh-environment
bootstrap.

Safety: dry-run by default — pass --apply to write. Secrets come from
the JSON file you point it at; never commit a filled-in copy
(deploy/env/se-configdata.example.json is a placeholder template only).

Usage:
    python seed_seconfig.py --file ../env/se-configdata.local.json
    python seed_seconfig.py --file ../env/se-configdata.local.json \
        --endpoint-url http://127.0.0.1:8001 --apply
"""

from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Seed se-configdata rows from a JSON object {variable: value}."
    )
    parser.add_argument(
        "--file",
        required=True,
        help="JSON file whose root object maps variable name -> value",
    )
    parser.add_argument(
        "--table", default="se-configdata", help="table name (default: se-configdata)"
    )
    parser.add_argument(
        "--setting-id",
        default="beta_core_banyan",
        help="partition key, {stage}_{area}_{endpoint_id} (default: beta_core_banyan)",
    )
    parser.add_argument(
        "--region", default="us-west-2", help="AWS region (default: us-west-2)"
    )
    parser.add_argument(
        "--endpoint-url",
        default=None,
        help="DynamoDB endpoint override, e.g. http://127.0.0.1:8001 for DDB Local",
    )
    parser.add_argument(
        "--profile", default=None, help="optional AWS credential profile"
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="actually write rows (default: dry-run)",
    )
    args = parser.parse_args()

    try:
        with open(args.file, "r", encoding="utf-8") as fh:
            variables = json.load(fh, parse_float=Decimal)
    except (OSError, json.JSONDecodeError) as e:
        print(f"error: cannot read {args.file}: {e}", file=sys.stderr)
        return 2

    if not isinstance(variables, dict) or not variables:
        print(
            "error: JSON root must be a non-empty object {variable: value}",
            file=sys.stderr,
        )
        return 2

    import boto3

    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    if args.endpoint_url:
        resource = session.resource("dynamodb", endpoint_url=args.endpoint_url)
    else:
        resource = session.resource("dynamodb")
    table = resource.Table(args.table)

    action = "WRITE" if args.apply else "DRY-RUN"
    for name, value in sorted(variables.items()):
        print(f"[{action}] {args.setting_id} :: {name} ({type(value).__name__})")
        if args.apply:
            table.put_item(
                Item={
                    "setting_id": args.setting_id,
                    "variable": name,
                    "value": value,
                }
            )
    print(
        f"{len(variables)} variable(s) "
        + ("written" if args.apply else "planned (dry-run — pass --apply to write)")
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())