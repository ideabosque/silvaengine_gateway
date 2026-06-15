# -*- coding: utf-8 -*-
"""Local user model and user file loader."""

from __future__ import print_function

__author__ = "silvaengine"

import json
import os
from typing import Dict, List, Optional


class LocalUser:
    """Simple local user for JWT auth."""

    def __init__(self, username: str, hashed_password: str, roles: List[str] = None):
        self.username = username
        self.hashed_password = hashed_password
        self.roles = roles or []

    def verify(self, password: str) -> bool:
        try:
            from passlib.context import CryptContext

            ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")
            return ctx.verify(password, self.hashed_password)
        except ImportError:
            return password == self.hashed_password


def load_users(filepath: str) -> Dict[str, LocalUser]:
    """
    Load users from a JSON file.

    File format:
    [
        {"username": "admin", "hashed_password": "$2b$12$...", "roles": ["admin"]},
        ...
    ]
    """
    users: Dict[str, LocalUser] = {}

    if not os.path.exists(filepath):
        return users

    try:
        with open(filepath) as f:
            users_data = json.load(f)
        for u in users_data:
            users[u["username"]] = LocalUser(
                username=u["username"],
                hashed_password=u.get("hashed_password", ""),
                roles=u.get("roles", []),
            )
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"Failed to load users from {filepath}: {e}")

    return users