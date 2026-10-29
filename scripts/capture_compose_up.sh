#!/usr/bin/env bash
# Captures evidence that the stack from `make up` runs the customised Fineract.
set -uo pipefail

FINERACT=https://localhost:8443/fineract-provider
ADMIN="${FINERACT_API_USERNAME:-mifos}:${FINERACT_API_PASSWORD:-password}"
TENANT_SQL=(docker compose exec -T db psql -U postgres -d fineract_default -At -c)

echo "# make up / docker compose up evidence"
echo "# captured: $(date -u +%Y-%m-%dT%H:%M:%SZ)"

echo; echo '$ docker compose ps'
docker compose ps --format 'table {{.Service}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}'

echo; echo "\$ curl -sk $FINERACT/actuator/health"
curl -sk "$FINERACT/actuator/health"; echo

echo; echo '$ docker compose logs fineract | grep "Started ServerApplication"'
docker compose logs fineract --no-color | grep "Started ServerApplication" | sed 's/^fineract-1  | //'
echo '$ docker compose logs fineract | grep -c " ERROR "'
docker compose logs fineract --no-color | grep -c " ERROR "

echo; echo "# The patch's Liquibase changesets ran on the tenant database:"
echo '$ select id, filename, exectype from databasechangelog where filename like '"'"'%0270%'"'"
"${TENANT_SQL[@]}" "select id, filename, exectype from databasechangelog where filename like '%0270%' order by id"
echo '$ select column_name, data_type from information_schema.columns where table_name = '"'"'acc_gl_account'"'"' and column_name = '"'"'external_reference'"'"
"${TENANT_SQL[@]}" "select column_name, data_type from information_schema.columns where table_name = 'acc_gl_account' and column_name = 'external_reference'"
echo '$ select conname from pg_constraint where conname = '"'"'acc_gl_account_external_reference_uq'"'"
"${TENANT_SQL[@]}" "select conname from pg_constraint where conname = 'acc_gl_account_external_reference_uq'"
echo '$ select code, entity_name, action_name from m_permission where code = '"'"'SYNC_GLACCOUNT'"'"
"${TENANT_SQL[@]}" "select code, entity_name, action_name from m_permission where code = 'SYNC_GLACCOUNT'"

echo; echo "# The patched endpoint answers (404: no account with this reference yet, not 'no such endpoint'):"
echo "\$ curl -sk -u <admin> -H 'Fineract-Platform-TenantId: default' $FINERACT/api/v1/glaccounts/external-reference/NOT-SYNCED"
curl -sk -u "$ADMIN" -H "Fineract-Platform-TenantId: default" -w "\nHTTP %{http_code}\n" "$FINERACT/api/v1/glaccounts/external-reference/NOT-SYNCED"

echo; echo '$ curl -s http://localhost:8001/health'
curl -s http://localhost:8001/health; echo
echo '$ curl -s http://localhost:8000/health'
curl -s http://localhost:8000/health; echo
