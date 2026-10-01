#!/usr/bin/env bash
# Creates the Azure resources (if missing), applies the Dynatrace settings and publishes the code.
# Requires: az login, Azure Functions Core Tools (func).
set -euo pipefail

# Settings and the token live in the vault file (git-ignored); see vault/vault.cfg.example.
VAULT_FILE="${VAULT_FILE:-$(dirname "$0")/vault/vault.cfg}"
[ -f "$VAULT_FILE" ] || { echo "Vault file not found: $VAULT_FILE" >&2; exit 1; }
set -a
# shellcheck disable=SC1090
source "$VAULT_FILE"
set +a

# Publish from the project folder, wherever the script is called from.
cd "$(dirname "$0")"

: "${APP_NAME:?set APP_NAME (globally unique function app name)}"
: "${DT_OTLP_ENDPOINT:?set DT_OTLP_ENDPOINT in $VAULT_FILE}"
: "${DT_API_TOKEN:?set DT_API_TOKEN in $VAULT_FILE}"

RESOURCE_GROUP="${RESOURCE_GROUP:-rg-dt-otel-test}"
LOCATION="${LOCATION:-swedencentral}"
PYTHON_VERSION="${PYTHON_VERSION:-3.12}"
# Storage account names: 3-24 chars, lowercase letters and digits only.
STORAGE_ACCOUNT="${STORAGE_ACCOUNT:-$(echo "${APP_NAME}" | tr -cd 'a-z0-9' | cut -c1-22)sa}"

# Tags applied to the storage account and the function app (space-separated key=value pairs).
read -r -a TAGS <<<"${TAGS:-Application=${APP_NAME} Purpose=dynatrace-traces DeployedBy=deploy.sh}"

# An existing resource group is left untouched, including its tags.
az group show --name "$RESOURCE_GROUP" --output none 2>/dev/null ||
  az group create --name "$RESOURCE_GROUP" --location "$LOCATION" --tags "${TAGS[@]}" --output none

az storage account show --name "$STORAGE_ACCOUNT" --resource-group "$RESOURCE_GROUP" --output none 2>/dev/null ||
  az storage account create --name "$STORAGE_ACCOUNT" --resource-group "$RESOURCE_GROUP" \
    --location "$LOCATION" --sku Standard_LRS --tags "${TAGS[@]}" --output none

az functionapp show --name "$APP_NAME" --resource-group "$RESOURCE_GROUP" --output none 2>/dev/null ||
  az functionapp create --name "$APP_NAME" --resource-group "$RESOURCE_GROUP" \
    --storage-account "$STORAGE_ACCOUNT" --consumption-plan-location "$LOCATION" \
    --os-type Linux --runtime python --runtime-version "$PYTHON_VERSION" \
    --functions-version 4 --tags "${TAGS[@]}" --output none

# Keep tags current on re-deploys; Merge leaves any other tags in place.
for id in \
  "$(az storage account show --name "$STORAGE_ACCOUNT" --resource-group "$RESOURCE_GROUP" --query id -o tsv)" \
  "$(az functionapp show --name "$APP_NAME" --resource-group "$RESOURCE_GROUP" --query id -o tsv)"; do
  az tag update --resource-id "$id" --operation Merge --tags "${TAGS[@]}" --output none
done

az functionapp config appsettings set --name "$APP_NAME" --resource-group "$RESOURCE_GROUP" --output none \
  --settings \
  "OTEL_SERVICE_NAME=${APP_NAME}" \
  "DT_OTLP_ENDPOINT=${DT_OTLP_ENDPOINT}" \
  "DT_API_TOKEN=${DT_API_TOKEN}"

func azure functionapp publish "$APP_NAME" --python

echo "Try it: https://${APP_NAME}.azurewebsites.net/api/hello?name=dynatrace"
