# Infrastructure

Three Bicep templates, deployed in order — matching the silo model
demonstrated locally earlier in this project (separate DB, storage, and
secrets per user, sharing only the compute environment and database
server) plus a hard requirement: **database access is Azure AD only**.
There is no Postgres admin password anywhere in this project, in Key
Vault, or in any Container App's configuration — `passwordAuth` is
disabled on the server entirely.

## Resolved: `usera` tenant's image pull failure

For an extended stretch, `usera-claimval`'s Container App could not pull
`<registry-name>.azurecr.io/claim-validator:latest` — `ImagePullBackOff`,
persistently, with a 401 from ACR's own token-exchange endpoint. RBAC,
identity, and network configuration were all confirmed correct (the
`AcrPull` role assignment was right on three separate identity
incarnations including a full remove/re-add; a genuinely new revision
failed identically; plain ACR admin username/password also failed from
inside Container Apps despite those same credentials succeeding when
tested directly against ACR's token endpoint from outside; no VNet
restriction existed). All of that investigation was real, but pointed at
the wrong layer.

**The actual cause: the pushed image was `linux/arm64`-only.** It was
built on an Apple Silicon Mac with a bare `docker build`/`docker buildx
build`, which defaults to the host's own architecture — never
cross-compiled for `linux/amd64`, which is what Container Apps requires.
Confirmed by creating a disposable second Container Apps Environment
(`cv-env-diag`, since deleted) and pulling the identical image there with
the identical managed-identity mechanism: it failed too, but with a
*different*, far more legible error — `no child with platform
linux/amd64 in index` — instead of the misleading "unauthorized" `cv-env`
had been reporting the whole time for what was apparently the same
underlying problem. Rebuilding with `docker buildx build --platform
linux/amd64 --push` and forcing a new revision
(`az containerapp update --revision-suffix ...`) fixed both the
throwaway environment and `usera-claimval` immediately — pull, Key Vault
secret sync, and the app's own `/api/ping` all confirmed working within
the same minute.

**Lesson carried into the Dockerfile below**: never `docker build`/`push`
for this project from an Apple Silicon Mac (or any non-amd64 host)
without `--platform linux/amd64` — the build succeeds and pushes
"successfully" either way, and the platform mismatch only surfaces later,
at pull time, as a confusing and seemingly unrelated auth-shaped error.

## What's verified

All three templates compile cleanly with `az bicep build`, and all three
have been deployed for real against a live subscription, in order
(`pg-admin-identity.bicep` → `shared.bicep` → `tenant.bicep`), ending in
a running Container App confirmed reachable over HTTPS, with Key Vault
secret sync and the Postgres AAD grant both confirmed working end to
end. Real problems surfaced only at the real-deployment stage — neither
`az bicep build` nor `az deployment group what-if` caught any of them:

- `australiacentral` (`claim-validator-rg`'s own reported region) doesn't
  support `Microsoft.App/managedEnvironments` — every real resource here
  has always lived in `australiaeast` instead. That's two related facts,
  not one: which region actually supports Container Apps, and what this
  specific resource group's own location metadata says (a one-time
  mismatch from finding the first fact out live, permanent because Azure
  treats a resource group's location as immutable after creation — no
  `az group update --location`, no way to fix the resource group itself
  short of creating a new one and migrating every resource into it, wildly
  disproportionate to a metadata field that causes no functional harm).
  Fixed at the template level instead: all three `location` parameters
  now default to `'australiaeast'` directly rather than
  `resourceGroup().location`, so a bare deploy with no `--parameters
  location=...` override lands in the right place instead of failing with
  `InvalidResourceLocation` (found live: it did, the first time this
  mismatch actually bit a deploy). Override explicitly only if this is
  ever deployed into a genuinely different resource group.
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
- A second, independent tenant (`userb`) was deployed against the same
  shared environment and registry, confirming the silo pattern actually
  holds for more than one tenant. It reproduced the first-deploy
  chicken-and-egg pull race above exactly — same fix, same recovery. It
  also hit a *different*, previously unseen failure on the retry: KEDA
  (the scaler behind scale-to-zero) got stuck unable to resolve an
  internal secret reference, repeatedly assigning and discarding
  replicas with `containers: []` — no image pull attempted at all — and
  no application code ever ran. Almost certainly leftover internal state
  from the first, failed deployment attempt on that same Container App
  name. Fixed the same way as the credential-staleness issues above:
  `az containerapp update --revision-suffix <anything>` to force a
  genuinely new revision, which cleared it immediately. If a fresh
  tenant's replicas keep cycling with no container ever starting, this
  is the fix to try before assuming anything about RBAC or the image.
- An ontology directory `usera`'s own Container App had written vanished
  from its file share with no explanation findable anywhere — confirmed
  gone via a direct `az storage file list` against the real share, not
  an API-layer bug (the same job's report and source-document files, in
  sibling directories, were untouched). No root cause was found: no
  audit trail existed for file-share operations at the time (the
  standard Activity Log only covers ARM/control-plane calls), and a
  deliberate reproduction attempt — building a fresh ontology, then
  forcing the same kind of revision update that preceded the original
  loss — did **not** reproduce it, ruling out "any revision update" as a
  reliable cause. Most likely a one-off, transient Azure Files anomaly.
  Since ontologies are immutable and insert-only by design, the worst
  case is a wasted rebuild, not corruption — but the missing audit trail
  itself was a real gap, closed by the `fileServiceDiagnostics` resource
  now in `tenant.bicep` (see "Auditing file share activity" below), so a
  recurrence would have an actual answer next time.

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

Note the `containerAppsEnvName`, `postgresServerName`, and
`logAnalyticsWorkspaceId` outputs — all three are needed by every
`tenant.bicep` deployment that follows.

## 3. `tenant.bicep` — once per user

Provisions everything specific to one tenant: a database on the shared
server, a Storage Account + Azure Files share (mounted into the
container), a dedicated Key Vault holding that tenant's own Anthropic key
and API token (no DB credential — there isn't one), the Container App
itself (scaled to zero when idle, system-assigned managed identity
granted read-only "Key Vault Secrets User" access to only its own
vault), a deployment script that grants that same identity a Postgres
role and access to only this tenant's database — run using the shared
AAD admin identity from step 1, never the tenant's own — and a
diagnostic setting that streams the file share's own read/write/delete
operations to the shared Log Analytics workspace, so a question like
"what happened to this file" has an actual answer (see "Auditing file
share activity" below).

```bash
az deployment group create -g <resource-group> --name tenant-usera -f infra/tenant.bicep \
  --parameters tenantName=usera \
               containerAppsEnvName=<from shared.bicep output> \
               postgresServerName=<from shared.bicep output> \
               logAnalyticsWorkspaceId=<from shared.bicep output> \
               pgAdminIdentityId=<from pg-admin-identity.bicep, "id"> \
               pgAdminIdentityClientId=<same, "clientId" output> \
               pgAdminIdentityName=<same, "name" output> \
               anthropicApiKey=<this tenant's own key> \
               apiToken=<a real random value, e.g. `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`> \
               containerImage=<registry>/claim-validator:<tag> \
               containerRegistryName=<registry name, e.g. "myregistry">
```

**`apiToken` isn't auto-generated, and that's deliberate.** It used to be
computed as `uniqueString(resourceGroup().id, tenantName, deployment().name)`
— found live, after this repo went public: that's a *deterministic hash*,
not a secret, and two of its three inputs (the tenant name, the
`--name tenant-usera` deployment-name convention right above) are already
public in this file. The only thing standing between "public" and
"derivable by formula" was the subscription ID — never meant to function
as a secret in the first place. Generate a real random value yourself and
pass it explicitly; there's no safe way to auto-fill a bearer token that
guards a publicly reachable API.

**Always pass an explicit `--name`, unique per tenant** (`tenant-usera`,
`tenant-userb`, ...) — found live: `az deployment group create` defaults
the deployment's own name to the template's filename (`tenant`), so
deploying two tenants at the same time in the same resource group without
`--name` collides on that shared default and the second one fails with
`DeploymentActive`, even though the two deployments touch completely
disjoint resources. Sequential deploys without `--name` don't hit this
(each finishes before the next starts), but there's no reason to leave
the footgun in place for anyone who *does* run them in parallel.

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
`apiToken` shared-secret parameter (`claimvalidator/azure_auth.py` — a
separate concern from the *database* RBAC this file is about), also pass
`azureAdTenantId` and `azureAdClientId` for that tenant's own app
registration.

### Redeploying an existing tenant: `RoleAssignmentExists` is usually harmless

Found live rotating both tenants' `apiToken`: redeploying `tenant.bicep`
against a tenant that already exists can fail with
`RoleAssignmentExists`, naming a role assignment's own ID — even though
that assignment is already correct (right principal, right role, right
scope; confirmed by reading it back directly, not assumed). This is a
known ARM platform quirk, not a template bug:
`Microsoft.Authorization/roleAssignments` isn't always treated as
idempotent on redeploy the way most resource types are, even when
Bicep computes the exact same deterministic name (`guid(...)`) both
times.

The deployment reports `"status": "Failed"` regardless — worth knowing
before assuming the whole redeploy did nothing. In both cases so far,
every resource *before* the role-assignment step in the dependency
graph — the Key Vault secret this rotation actually cared about, and
the Container App with a freshly forced revision — had already applied
successfully. Check the thing you actually changed directly (`az
keyvault secret show`, `az containerapp revision list`) rather than
trusting the top-level status alone. If the real resource didn't apply,
that's a genuine failure worth investigating — this specific error, by
itself, isn't one.

## Auditing file share activity

Every read, write, and delete against a tenant's file share lands in the
shared `cv-logs` Log Analytics workspace, in the `StorageFileLogs` table
(the `fileServiceDiagnostics` resource in `tenant.bicep` is what wires
this up — see its own comment for why it exists). Query with:

```bash
WORKSPACE_ID=$(az monitor log-analytics workspace show -g <rg> -n cv-logs --query customerId -o tsv)
az monitor log-analytics query \
  --workspace "$WORKSPACE_ID" \
  --analytics-query "StorageFileLogs | where TimeGenerated > ago(1h) | where OperationName in ('DeleteFile','DeleteDirectory') | order by TimeGenerated desc" \
  -o table
```

Drop the `OperationName` filter to see everything, including reads and
writes — useful for confirming an ontology build actually persisted, not
just that the app claimed it did. Diagnostic settings only capture
activity from the point they're created onward, so this has no
retroactive value for anything that happened before a given tenant's
`tenant.bicep` deployment first included this resource.

## Auditing database activity

Same gap, same fix, at the Postgres layer — `shared.bicep`'s
`postgresDiagnostics` resource streams the server's own log stream and
per-session connection detail to the same `cv-logs` workspace, since the
server is shared across tenants rather than per-tenant like the file
share is.

```bash
WORKSPACE_ID=$(az monitor log-analytics workspace show -g <rg> -n cv-logs --query customerId -o tsv)
az monitor log-analytics query \
  --workspace "$WORKSPACE_ID" \
  --analytics-query "AzureDiagnostics | where Category in ('PostgreSQLLogs', 'PostgreSQLFlexSessions') | where TimeGenerated > ago(1h) | order by TimeGenerated desc" \
  -o table
```

A query's tenant is identifiable from which database or role it ran
against — already visible in these logs — so one shared diagnostic
setting on the server covers every tenant, unlike the per-tenant file
share setting above.

## Attempted and reverted: scoping the Postgres firewall down (real outage)

A routine `check the postgres audit logs` pass (the section above) turned
up 115 rejected connection attempts over 7 days from 4 external IPs —
malformed protocol-version handshakes and default-`postgres`-user login
attempts, the standard signature of internet-wide port scanners. None got
past `pg_hba.conf` or authentication (AAD-only auth — `passwordAuth:
Disabled` — means there's no password to guess even if one did), but the
firewall rule that let them reach that far at all (`AllowAzureServices`,
`0.0.0.0`–`0.0.0.0`) is far broader than anything actually needed — it
opens the server to all of Azure's own IP space, not just this project's
own traffic.

Tried replacing it with a rule scoped to the Container Apps environment's
own static IP (`containerAppsEnv.properties.staticIp`) — reasoning that
this was the one address real app traffic actually connects from. It
wasn't. **Both tenants went unresponsive within a minute of deploying
this** — every request hung until it timed out, because the app's own
startup sequence blocks on its first Postgres connection, and that
connection was now being silently firewalled. Root cause, confirmed
afterward: on a Consumption-only Container Apps environment (no VNet
integration, which is what this project uses), Azure does **not**
guarantee that the `staticIp` property is the actual outbound IP for all
traffic — Microsoft's own guidance is that outbound IPs on the Consumption
plan can vary and aren't reliably the one the portal/API shows. Reverted
within minutes; both tenants recovered immediately once the broad rule was
restored — no data loss, no lasting damage, just a real, self-inflicted,
short outage.

**Where this leaves the actual finding**: still open. The only
Microsoft-documented way to get a real, firewall-able static outbound IP
here is VNet integration + a NAT Gateway — which needs a workload-profiles
environment, not this Consumption-only one, and is a materially bigger
change than the currently measured risk justifies (zero successful
unauthorized connections ever occurred; AAD-only auth is the boundary
that's actually doing the work). Left as the natural next step if this
project ever needs tighter network isolation than that — attempted the
cheap version first, and it wasn't actually cheap.

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
