#!/usr/bin/env bash
# The UI verifies the release and passes the selected tag to the portable installer.
set -euo pipefail
cd "$(dirname "$0")/.."
printf 'Utilisez Paramètres → Mises à jour pour vérifier et installer une release précise.\n'
exit 1
