#!/usr/bin/env bash
# Runs the function app locally with the DT_* values loaded from the vault.
set -euo pipefail
cd "$(dirname "$0")"

VAULT_FILE="${VAULT_FILE:-vault/vault.cfg}"
[ -f "$VAULT_FILE" ] || { echo "Vault file not found: $VAULT_FILE" >&2; exit 1; }
set -a
# shellcheck disable=SC1090
source "$VAULT_FILE"
set +a

[ -f local.settings.json ] || cp local.settings.json.example local.settings.json
# shellcheck disable=SC1091
source .venv/bin/activate
exec func start "$@"
