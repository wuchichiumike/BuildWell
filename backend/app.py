"""Canonical ASGI application entry point.

`backend.api` owns the HTTP adapter.  Keeping this tiny module preserves the
`backend.app:app` command used by the Makefile without creating a second
business-service implementation.
"""

from .api import app, create_app

__all__ = ["app", "create_app"]
