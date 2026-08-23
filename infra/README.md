# Infrastructure

Three Bicep templates, deployed in order — matching the silo model
demonstrated locally earlier in this project (separate DB, storage, and
secrets per user, sharing only the compute environment and database
server) plus a hard requirement: **database access is Azure AD only**.
There is no Postgres admin password anywhere in this project, in Key
Vault, or in any Container App's configuration — `passwordAuth` is
disabled on the server entirely.

## What's verified, and what isn't

All three templates compile cleanly with `az bicep build` — real
type-checking against the actual Azure resource schemas for each API
version used, which catches a meaningful class of real mistakes (an
invalid property name, a resource-name length that exceeds Azure's own
limit — `storageAccountName` and `keyVaultName` were both sized by hand
after `az bicep build` flagged the storage account case; a resource
`name` that can't reference another resource's runtime output within the
same deployment — `pg-admin-identity.bicep` exists as its own template
specifically because of this, found by hitting BCP120 twice).

What that check can't do: verify an actual deployment succeeds. No Azure
subscription was available to run this against — no real resource has
been created, no `what-if` diff has been run against a live resource
group, and the deployment script's SQL (below) has not run against a
real Postgres server. Run `az deployment group what-if` before the first
real deploy of any of these, and treat the deployment script's exact
`pgaadauth_create_principal_with_oid` signature as the one piece of this
whole setup that needs confirming against Microsoft's current Azure AD
Postgres Flexible Server documentation before relying on it — it's
annotated in `tenant.bicep` as the one thing here that isn't verified.

One cosmetic, understood warning: `az bicep build` flags
`ossrdbms-aad.database.windows.net` (the Azure AD auth endpoint the
deployment script fetches a token against) as a hardcoded environment
URL — correct, and deliberate, since this project targets Azure Public
Cloud only. A `#disable-next-line` sits above the property but can't
reach inside the multi-line script string the linter is actually
scanning; noted here rather than left unexplained.

## 1. `pg-admin-identity.bicep` — once per environment, before everything else

Creates one user-assigned managed identity, and nothing else. Exists
only to become the Postgres server's AAD administrator and to run the
role-granting deployment script in `tenant.bicep` — it never serves
application traffic. Deployed on its own specifically because Bicep
won't let a resource's `name` reference another resource's (or even
another module's) runtime output within the *same* deployment — the
identity has to already exist, with a known `principalId`, before
`shared.bicep` can use it.

```bash
az deployment group create -g <resource-group> -f infra/pg-admin-identity.bicep
```

Note the `id`, `principalId`, and `name` outputs — all three are needed
by both templates that follow.

## 2. `shared.bicep` — once per environment

Provisions the Container Apps Environment and one PostgreSQL Flexible
Server, with Azure AD authentication enabled and password authentication
disabled outright. Safe to share across tenants: no tenant data lives
here, only the compute/networking boundary and a database *server* —
each tenant gets its own separate *database* on it (see below), which is
where the actual isolation lives.

```bash
az deployment group create -g <resource-group> -f infra/shared.bicep \
  --parameters pgAdminIdentityId=<pg-admin-identity.bicep's "id" output> \
               pgAdminIdentityPrincipalId=<same, "principalId" output> \
               pgAdminIdentityName=<same, "name" output>
```

Note the `containerAppsEnvName` and `postgresServerName` outputs — both
are needed by every `tenant.bicep` deployment that follows.

## 3. `tenant.bicep` — once per user

Provisions everything specific to one tenant: a database on the shared
server, a Storage Account + Azure Files share (mounted into the
container), a dedicated Key Vault holding that tenant's own Anthropic key
and API token (no DB credential — there isn't one), the Container App
itself (scaled to zero when idle, system-assigned managed identity
granted read-only "Key Vault Secrets User" access to only its own
vault), and a deployment script that grants that same identity a
Postgres role and access to only this tenant's database — run using the
shared AAD admin identity from step 1, never the tenant's own.

```bash
az deployment group create -g <resource-group> -f infra/tenant.bicep \
  --parameters tenantName=usera \
               containerAppsEnvName=<from shared.bicep output> \
               postgresServerName=<from shared.bicep output> \
               pgAdminIdentityId=<from pg-admin-identity.bicep, "id"> \
               pgAdminIdentityClientId=<same, "clientId" output> \
               pgAdminIdentityName=<same, "name" output> \
               anthropicApiKey=<this tenant's own key> \
               containerImage=<registry>/claim-validator:<tag>
```

Repeat for each additional user with a different `tenantName` and a
different `anthropicApiKey` — per the separate-keys-per-user decision.

To use Azure AD (Entra ID) *API* auth for a tenant instead of the
generated shared-secret token (`claimvalidator/azure_auth.py` — a
separate concern from the *database* RBAC this file is about), also pass
`azureAdTenantId` and `azureAdClientId` for that tenant's own app
registration.

## How runtime DB auth actually works

At connect time, `db/database.py` (when `CLAIMVAL_DB_AAD_AUTH=true`, set
automatically by `tenant.bicep`) fetches a fresh Azure AD access token
via `DefaultAzureCredential` — which resolves to the Container App's own
system-assigned managed identity in Azure — and supplies it as the
Postgres connection password. A new token is fetched on every new
physical connection (not cached at the engine level), since a pooled
connection can easily outlive the roughly hour-long life of one token;
see `db/database.py`'s own comments for why a SQLAlchemy `do_connect`
event listener is the actual mechanism, not just a connection string
built once.

## What isn't automated yet

- Pushing the container image to a registry the Container App can pull
  from — `containerImage` assumes one already exists.
- Creating the tenant's own Azure AD app registration, if using Entra ID
  *API* auth — that's a separate `az ad app create` step per tenant, not
  something Bicep provisions.
- A resource group itself — all three templates assume one already
  exists (`az group create` first).
- A human Azure AD administrator on the Postgres server, if one is ever
  wanted alongside the automation identity — only the one identity from
  step 1 is registered as an admin today.
