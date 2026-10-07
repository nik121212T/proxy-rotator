# Product

## Platform
web

## Purpose and users
Local proxy pool management for its operator on macOS and Windows. Import
credentials, inspect health and choose rotation. SPEC.md remains authoritative.

## Confirmed constraints
Phase 4 uses FastAPI, server-rendered Jinja2 and HTMX, with no SPA or external
CSS/CDN. A minimal light interface must work on mobile. Credentials must never
appear in output. Gateway protocols are intentionally unimplemented until Phase 5.

## Evidence
All data comes from the local pool. No fabricated production proxies or activity.
