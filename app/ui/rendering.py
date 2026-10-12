"""Compatibility shim — the renderer lives at ``app.views`` since audit G1."""

from app.views import (
    SORT_OPTIONS,
    feedback,
    ordered_proxies,
    render,
    templates,
)

__all__ = ["SORT_OPTIONS", "feedback", "ordered_proxies", "render", "templates"]
