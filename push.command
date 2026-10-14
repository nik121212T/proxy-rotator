#!/bin/bash
# Push committed changes using a token held only in process memory.
set -euo pipefail
set +x
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"
unset GH_DEBUG GIT_TRACE GIT_TRACE_CURL GIT_CURL_VERBOSE

finish() {
  result=$?
  unset GH_TOKEN GITHUB_TOKEN
  if [ "$result" -ne 0 ]; then
    printf '\nLe push a échoué. Consultez le message ci-dessus.\n'
  fi
  printf '\nAppuyez sur Entrée pour fermer…'
  read -r _ < /dev/tty || true
  exit "$result"
}
trap finish EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

cd -- "$(dirname -- "$0")"
if ! command -v gh >/dev/null 2>&1; then
  printf 'GitHub CLI manque. Installez-le avec : brew install gh\n'
  exit 1
fi
if [ "$(git branch --show-current)" != main ]; then
  printf 'Passez sur la branche main avant de relancer ce fichier.\n'
  exit 1
fi
if [ -n "$(git status --porcelain)" ]; then
  printf 'Attention : des modifications ne sont pas encore commitées.\n'
  printf 'Seuls les commits existants seront envoyés.\n\n'
fi
printf 'Dépôt : nik121212T/proxy-rotator — branche main\n'
printf 'Token GitHub (saisie masquée, non enregistrée) : '
IFS= read -r -s GH_TOKEN < /dev/tty
printf '\n'
if [ -z "$GH_TOKEN" ]; then
  printf 'Token vide : envoi annulé.\n'
  exit 1
fi
export GH_TOKEN
unset GITHUB_TOKEN
repository='https://github.com/nik121212T/proxy-rotator.git'
if git remote get-url origin >/dev/null 2>&1; then
  git remote set-url origin "$repository"
else
  git remote add origin "$repository"
fi
# An empty helper resets inherited helpers; credentials are never persisted.
git -c credential.helper= -c 'credential.helper=!gh auth git-credential' \
  push -u origin main
unset GH_TOKEN
printf '\nPush terminé avec succès.\n'
