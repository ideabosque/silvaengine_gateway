"""Resolve consistent local-admin role claims from settings or environment."""

import json
import os


def resolve_admin_roles(setting):
    """Settings override env; the default is role=admin, roles=[admin].

    ADMIN_ROLES accepts CSV, a JSON array, or a list in a settings dict.
    With only roles configured, the first role is primary. An explicitly
    configured primary must belong to the list, avoiding contradictory claims.
    """
    primary = setting.get("admin_role")
    if primary is None:
        primary = os.getenv("ADMIN_ROLE")
    if primary is not None:
        if not isinstance(primary, str):
            raise ValueError("ADMIN_ROLE must be a string")
        primary = primary.strip() or None

    raw = setting.get("admin_roles")
    if raw is None:
        raw = os.getenv("ADMIN_ROLES")
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        primary = primary or "admin"
        return primary, [primary]
    if isinstance(raw, str):
        raw = raw.strip()
        if raw.startswith("["):
            try:
                raw = json.loads(raw)
            except ValueError as error:
                raise ValueError(
                    "ADMIN_ROLES must be a valid JSON array or CSV list"
                ) from error
        else:
            raw = [role.strip() for role in raw.split(",") if role.strip()]
    if not isinstance(raw, (list, tuple)) or not raw:
        raise ValueError("ADMIN_ROLES must be a nonempty list of role names")
    roles = []
    for role in raw:
        if not isinstance(role, str) or not role.strip():
            raise ValueError("ADMIN_ROLES entries must be nonempty strings")
        role = role.strip()
        if role not in roles:
            roles.append(role)
    primary = primary or roles[0]
    if primary not in roles:
        raise ValueError("ADMIN_ROLE must also appear in ADMIN_ROLES")
    return primary, roles
