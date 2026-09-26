#!/usr/bin/env bash
# Create .env with fresh random secrets. Refuses to overwrite an existing .env.
set -euo pipefail
cd "$(dirname "$0")/.."
if [[ -f .env ]]; then echo ".env already exists; not overwriting" >&2; exit 1; fi
secret() { python3 -c "import secrets; print(secrets.token_urlsafe(${1:-36}))"; }
admin_password="$(secret 12)"
sed -e "s|^LINEAGE_API_KEY=.*|LINEAGE_API_KEY=lk_$(secret)|" \
    -e "s|^LINEAGE_REDACTION_KEY=.*|LINEAGE_REDACTION_KEY=$(secret)|" \
    -e "s|^SERVICE_TOKEN=.*|SERVICE_TOKEN=$(secret)|" \
    -e "s|^SESSION_SECRET=.*|SESSION_SECRET=$(secret)|" \
    -e "s|^ADMIN_PASSWORD=.*|ADMIN_PASSWORD=${admin_password}|" \
    .env.example > .env
chmod 600 .env
echo "Created .env (admin password: ${admin_password})"
echo "Set OPENROUTER_API_KEY in .env before starting (or LLM_MODE=recorded to run without a model)."
