# Infrastructure

Three Bicep templates, deployed in order — matching the silo model
demonstrated locally earlier in this project (separate DB, storage, and
secrets per user, sharing only the compute environment and database
server) plus a hard requirement: **database access is Azure AD only**.
There is no Postgres admin password anywhere in this project, in Key
Vault, or in any Container App's configuration — `passwordAuth` is
disabled on the server entirely.

## Known open issue: `usera` tenant can't pull its image

The `usera` tenant deployed successfully in every other respect (DB,
storage, Key Vault, secrets, the Postgres AAD grant) but its Container
App cannot currently serve traffic — `ImagePullBackOff` against
`cvacr8b4cb977.azurecr.io/claim-validator:latest`, persistently, with a
401 from ACR's own token-exchange endpoint. This was investigated
exhaustively and the cause is **not** in this repo's IaC or app config:

- The `AcrPull` role assignment was confirmed correct (right role
  definition ID, right scope, right principal) on three separate
  identity incarnations, including a completely fresh one created via
  identity remove/re-add.
- A genuinely new revision (forced via `revisionSuffix`, not reusing any
  cached per-revision state) failed identically.
- Plain ACR admin username/password (no managed identity, no RBAC,
  bypassing Azure AD entirely) **also** failed with the same
  "unauthorized" pull error from inside Container Apps.
- Those same admin credentials, tested directly against
  `https://cvacr8b4cb977.azurecr.io/oauth2/token` from outside Container
  Apps entirely, returned a valid pull-scoped token (HTTP 200) — proving
  the registry, the image, and the credentials are all genuinely
  healthy, and isolating the fault specifically to Container Apps'
  own pull path to this registry/environment/region.
- The Container Apps Environment has no VNet integration and
  `publicNetworkAccess: Enabled` — nothing found there that would
  explain a network-level block either.

This looks like a genuine Azure platform-side issue, most likely needing
a Microsoft support ticket (server-side telemetry this repo's tooling
can't see) rather than a client-side config fix. Current state:
reverted to the secure, managed-identity pull config (ACR admin auth
disabled again, no password secret on the Container App) — correct per
this project's design, but non-functional until the underlying platform
issue is understood. Don't re-run the identity remove/re-add or
admin-credential diagnostic again without new information; both were
already tried and ruled out the client side conclusively.

## What's verified

All three templates compile cleanly with `az bicep build`, and all three
have been deployed for real against a live subscription, in order
(`pg-admin-identity.bicep` → `shared.bicep` → `tenant.bicep`), ending in
a running Container App reachable over HTTPS (before the pull issue
above reappeared on a later cold start). Real problems surfaced only at
the real-deployment stage — neither `az bicep build` nor
`az deployment group what-if` caught any of them:

- `australiacentral` (this resource group's default region) doesn't
  support `Microsoft.App/managedEnvironments` — `shared.bicep` and
  `tenant.bicep` both take an explicit `location` parameter rather than
  relying on `resourceGroup().location`; pass a region that supports
  Container Apps (`australiaeast` was used here).
- First-deploy chicken-and-egg on the registry pull: the Container App's
  own `AcrPull` role assignment can't exist until the Container App
  itself exists (its `principalId` isn't known before then), but the
  Container App's first revision needs that role assignment to pull the
  image — a first-ever tenant deploy can fail its initial revision with
  "Operation expired" while waiting on a pull it doesn't have permission
  for yet. Fix: re-run the same `az deployment group create` — the
  Container App resource itself will already exist from the failed
  attempt (just in a `Failed` provisioning state), and the retry's own
  `AcrPull` grant will exist in time for the second pull attempt.
- The `grantContainerAppDbAccess` deployment script's container image
  isn't a fixed OS across `azCliVersion`s — `2.60.0` runs on Azure
  Linux (`tdnf`), not Debian (`apt-get`); the script now detects
  `tdnf`/`apk`/`apt-get` in that order instead of assuming one.
- `az login --identity` errors by default when the identity has zero
  Azure RBAC (ARM) role assignments — expected here, since this
  identity only ever needs Postgres data-plane AAD admin rights, not an
  ARM role. Fixed with `--allow-no-subscriptions`.
- `pgaadauth_create_principal_with_oid`'s real signature is
  `(roleName, objectId, objectType, isAdmin, isMfa)` — role name first,
  then object ID, then a required `objectType` (`'service'` for a
  managed identity). The first version of this script had the first two
  arguments swapped and `objectType` missing entirely, confirmed wrong
  by a live "function ... does not exist" error and then corrected
  against [Microsoft's own docs](https://learn.microsoft.com/en-us/azure/postgresql/security/security-manage-entra-users).
- A Container App's system-assigned identity's `principalId` is **not**
  permanently stable — confirmed live: removing and re-adding the
  identity (or, apparently, certain platform-side events) changes it.
  This orphans any AAD-to-role mapping keyed to the old value.
  `grantContainerAppDbAccess` is now idempotent against this: it only
  calls `pgaadauth_create_principal_with_oid` (which does an
  unconditional `CREATE ROLE` and fails if the role already exists) for
  a genuinely new role, and re-points an existing role's AAD mapping via
  `SECURITY LABEL for "pgaadauth" on role ... is 'aadauth,oid=...'`
  otherwise.
- Identity changes and `registries`/`secrets` config changes don't by
  themselves force a new Container Apps revision, since they live
  outside `properties.template` — an *existing* revision can keep
  resolving pull/secret credentials against stale state indefinitely.
  `template.revisionSuffix` is now set to a fresh value
  (`'r${deployTimestamp}'`, fed by a `utcNow()`-defaulted parameter) on
  every deploy specifically to force a new revision and rule this out as
  a cause when debugging pull/secret failures.

Each of these is documented inline at its fix site in the relevant
`.bicep` file, not just here.

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
               containerImage=<registry>/claim-validator:<tag> \
               containerRegistryName=<registry name, e.g. "cvacr8b4cb977">
```

`containerRegistryName` is the registry's short name, not its login server —
the template looks up `loginServer` itself via an `existing` resource
reference, and grants the Container App's own system-assigned identity
`AcrPull` on that registry (scoped to just it), since the registry has admin
auth disabled and there's no username/password to configure. One caveat:
on a first-ever deploy, the Container App's first revision can attempt its
image pull before that role assignment has finished propagating — if the
first deploy fails on the initial pull, re-run the same `az deployment
group create` once the role assignment exists; nothing else needs to
change.

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

- Pushing the container image to the registry — `docker push` is still a
  manual step before the first deploy (or a CI job); Bicep only wires up
  pulling an image that already exists there.
- Creating the tenant's own Azure AD app registration, if using Entra ID
  *API* auth — that's a separate `az ad app create` step per tenant, not
  something Bicep provisions.
- A resource group itself — all three templates assume one already
  exists (`az group create` first).
- A human Azure AD administrator on the Postgres server, if one is ever
  wanted alongside the automation identity — only the one identity from
  step 1 is registered as an admin today.
