"""Helpers for building Azure AI Search OData filter expressions."""
from __future__ import annotations


def quote(value: str) -> str:
    """Escape a value for use inside a single-quoted OData string literal."""
    return value.replace("'", "''")
