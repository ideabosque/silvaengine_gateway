# -*- coding: utf-8 -*-
"""
SilvaEngine Gateway — FastAPI gateway with auth, module routing, and dispatch.

This package provides:
- FastAPI app factory with configurable route manifest
- JWT auth (local and AWS Cognito)
- Dynamic module routing via routes.yaml or GATEWAY_ROUTES_CONFIG env var
- Background task management for long-running operations
"""

from .app import create_app, run_gateway

__all__ = ["create_app", "run_gateway"]