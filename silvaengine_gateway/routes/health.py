# -*- coding: utf-8 -*-
"""Health check and user info routes."""

from __future__ import print_function

__author__ = "silvaengine"

from typing import Dict

from fastapi import APIRouter, Depends

from ..auth.middleware import get_current_user

router = APIRouter(tags=["health"])


@router.get("/health")
def health() -> Dict:
    """Public health check endpoint."""
    return {"status": "ok", "service": "silvaengine-gateway"}


@router.get("/me")
def me(user: dict = Depends(get_current_user)) -> Dict:
    """Return the authenticated user's claims."""
    return user