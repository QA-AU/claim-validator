# Infrastructure

Two Bicep templates, deployed in order — matching the silo model demonstrated
locally earlier in this project (separate DB, storage, and secrets per user,
sharing only the compute environment and database server).

## What's verified, and what isn't

Both templates compile cleanly with `az bicep build` — real type-checking
against the actual Azure resource schemas for each API version used, which
catches a meaningful class of real mistakes (an invalid property name, a
resource-name length that exceeds Azure's own limit — `storageAccountName`
and `keyVaultName` were both sized by hand after `az bicep build` flagged
the storage account case).

What that check can't do: verify an actual deployment succeeds. No Azure
subscription was available to run this against — no real resource has
been created, no `what-if` diff has been run against a live resource
group. Run `az deployment group what-if` before the first real deploy of
either template.

## 1. `shared.bicep` — once per environment, not once per tenant

Provisions the Container Apps Environment and one PostgreSQL Flexible
Server. Safe to share across tenants: no tenant data lives here, only the
compute/networking boundary and a database *server* — each tenant gets
its own separate *database* on it (see below), which is where the actual
isolation lives.

```bash
az deployment group create -g <resource-group> -f infra/shared.bicep \
  --parameters postgresAdminPassword=<a real secret>
```

Note the `containerAppsEnvName` and `postgresServerName` outputs — both
are needed by every `tenant.bicep` deployment that follows.

## 2. `tenant.bicep` — once per user

Provisions everything specific to one tenant: a database on the shared
server, a Storage Account + Azure Files share (mounted into the
container), a dedicated Key Vault holding that tenant's own Anthropic key,
DB connection string, and API token, and the Container App itself —
scaled to zero when idle, system-assigned managed identity granted
read-only ("Key Vault Secrets User") access to only its own vault.

```bash
az deployment group create -g <resource-group> -f infra/tenant.bicep \
  --parameters tenantName=usera \
               containerAppsEnvName=<from shared.bicep output> \
               postgresServerName=<from shared.bicep output> \
               postgresAdminPassword=<same value used for shared.bicep> \
               anthropicApiKey=<this tenant's own key> \
               containerImage=<registry>/claim-validator:<tag>
```

Repeat for each additional user with a different `tenantName` and a
different `anthropicApiKey` — per the separate-keys-per-user decision.

To use Azure AD (Entra ID) auth for a tenant instead of the generated
shared-secret token (`claimvalidator/azure_auth.py`), also pass
`azureAdTenantId` and `azureAdClientId` for that tenant's own app
registration.

## What isn't automated yet

- Pushing the container image to a registry the Container App can pull
  from — `containerImage` assumes one already exists.
- Creating the tenant's own Azure AD app registration, if using Entra ID
  auth — that's a separate `az ad app create` step per tenant, not
  something Bicep provisions.
- A resource group itself — both templates assume one already exists
  (`az group create` first).
