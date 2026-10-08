#!/bin/bash
# macOS Finder launcher; share the existing bootstrap and configuration checks.
set -euo pipefail
cd "$(dirname "$0")"
exec /bin/bash ./scripts/start.sh "$@"
