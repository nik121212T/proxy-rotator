#!/bin/bash
# Push committed changes using the macOS Keychain for authentication.
set -euo pipefail
set +x
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"
unset GH_DEBUG GIT_TRACE GIT_TRACE_CURL GIT_CURL_VERBOSE

finish() {
  result=$?
  unset PUSH_TOKEN stored_credentials
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
branch=$(git branch --show-current)
if [ -z "$branch" ]; then
  printf 'Aucune branche active (HEAD détachée). Revenez sur votre branche de travail avant de relancer.\n'
  exit 1
fi
if [ -n "$(git status --porcelain)" ]; then
  printf 'Attention : des modifications ne sont pas encore commitées.\n'
  printf 'Seuls les commits existants seront envoyés.\n\n'
fi
printf 'Dépôt : nik121212T/proxy-rotator — branche %s\n' "$branch"
credential_identity='protocol=https
host=github.com
username=nik121212T'
# Capture credentials without displaying them, and release the shell copy immediately.
stored_credentials=$(printf '%s\n\n' "$credential_identity" | git credential-osxkeychain get) || {
  printf 'Impossible d’accéder au trousseau macOS. Déverrouillez-le puis réessayez.\n'
  exit 1
}
case "$stored_credentials" in
  *password=*)
    unset stored_credentials
    printf 'Autorisation retrouvée dans le trousseau macOS.\n'
    ;;
  *)
    unset stored_credentials
    printf 'Première connexion : le token sera mémorisé dans le trousseau macOS.\n'
    printf 'Token GitHub (saisie masquée) : '
    IFS= read -r -s PUSH_TOKEN < /dev/tty
    printf '\n'
    if [ -z "$PUSH_TOKEN" ]; then
      printf 'Token vide : envoi annulé.\n'
      exit 1
    fi
    printf '%s\npassword=%s\n\n' "$credential_identity" "$PUSH_TOKEN" | git credential-osxkeychain store
    unset PUSH_TOKEN
    ;;
esac
repository='https://github.com/nik121212T/proxy-rotator.git'
if git remote get-url origin >/dev/null 2>&1; then
  git remote set-url origin "$repository"
else
  git remote add origin "$repository"
fi
# Use only the native Keychain helper, scoped to this account.
git -c credential.helper= -c credential.helper=osxkeychain \
  -c credential.username=nik121212T push -u origin "$branch"
printf '\nPush terminé avec succès.\n'
