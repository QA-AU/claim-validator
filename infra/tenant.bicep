// One deployment per user — the silo model demonstrated locally earlier:
// each tenant gets its own storage share, its own database (on the shared
// server from shared.bicep), its own Key Vault, and its own Container App,
// so one user's data is never reachable through another's credentials.
//
// Database access is Azure AD only, matching shared.bicep's server
// (passwordAuth disabled there) — there is no DB password anywhere in
// this file, in Key Vault, or in the Container App's own configuration.
//
// The Container App uses a pre-created USER-ASSIGNED identity (appIdentity,
// below), not a system-assigned one — deliberately, and for the same reason
// pg-admin-identity.bicep is its own separate deployment: a system-assigned
// identity's principalId doesn't exist until the Container App resource
// itself is created, so anything granting that identity a permission
// (AcrPull, the Postgres role, Key Vault access) would only run AFTER the
// Container App succeeds — but the Container App's first revision can't
// succeed without exactly those permissions already in place. Found live:
// this was a real, reproducible deadlock, not just a slow-propagation race
// — two full deployment attempts both failed with zero of those three
// permission resources ever even attempted, because Bicep never got past
// the failed Container App to reach them. A user-assigned identity's
// principalId is known the moment it's declared, independent of the
// Container App, so every permission below is granted BEFORE the Container
// App is created (see containerApp's own dependsOn), and its first revision
// starts already fully permissioned.
//
// At runtime, db/database.py's Azure AD auth path (DefaultAzureCredential)
// fetches a fresh access token per connection through this identity —
// AZURE_CLIENT_ID (set on the container below) is what tells
// DefaultAzureCredential which identity to use, since that resolution is
// ambiguous by default when the only identity attached is user-assigned
// rather than system-assigned.
//
//   az deployment group create -g <rg> -f infra/tenant.bicep \
//     --parameters tenantName=usera \
//                  containerAppsEnvName=<from shared.bicep output> \
//                  postgresServerName=<from shared.bicep output> \
//                  logAnalyticsWorkspaceId=<from shared.bicep output> \
//                  pgAdminIdentityId=<from pg-admin-identity.bicep output, "id"> \
//                  pgAdminIdentityClientId=<same, "clientId" output> \
//                  pgAdminIdentityName=<same, "name" output> \
//                  anthropicApiKey=<this tenant's own key> \
//                  apiToken=<a real random value you generate — see apiToken's
//                            own @description for why nothing auto-fills this> \
//                  containerImage=<registry>/claim-validator:<tag> \
//                  containerRegistryName=<registry name, e.g. "myregistry">

@description('Short, unique identifier for this tenant (e.g. "usera") — prefixes every resource name and becomes the database name.')
@minLength(3)
@maxLength(20)
param tenantName string

@description('Azure region — should match shared.bicep.')
// Defaults to australiaeast, not resourceGroup().location — see
// pg-admin-identity.bicep's location param for why (a permanent,
// harmless metadata mismatch on this project's real resource group;
// the default used to silently target the wrong region).
param location string = 'australiaeast'

@description('Name of the Container Apps Environment created by shared.bicep.')
param containerAppsEnvName string

@description('Name of the PostgreSQL Flexible Server created by shared.bicep.')
param postgresServerName string

@description('Resource ID of the Log Analytics workspace created by shared.bicep (its "logAnalyticsWorkspaceId" output) — file-share operations are logged here.')
param logAnalyticsWorkspaceId string

@description('Resource ID of the Postgres AAD admin identity (pg-admin-identity.bicep\'s "id" output) — used here only to run the role-granting deployment script below.')
param pgAdminIdentityId string

@description('Client ID of that same identity (its "clientId" output) — the deployment script logs in as it explicitly.')
param pgAdminIdentityClientId string

@description('Name of that same identity (its "name" output) — the Postgres role the script connects as.')
param pgAdminIdentityName string

@secure()
@description('This tenant\'s own Anthropic API key — kept separate per tenant by design (see the shared-vs-separate-keys discussion this was built from).')
param anthropicApiKey string

@secure()
@description('This tenant\'s own bearer token for the shared-secret auth fallback — generate a real random value yourself (e.g. `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`) and pass it here. Not auto-generated: it used to be computed as uniqueString(resourceGroup().id, tenantName, deployment().name), which is a deterministic hash, not a secret — two of its three inputs (tenantName, the deployment name convention) are already public in this repo\'s own docs, leaving only the subscription ID standing between "public" and "guessable by formula." Found live, on a repo that had just gone public with this API confirmed reachable from the open internet.')
param apiToken string

@description('Container image, e.g. myregistry.azurecr.io/claim-validator:latest.')
param containerImage string

@description('Name (not login server) of the Azure Container Registry containerImage is hosted in — the Container App pulls from it using its own pre-created identity (appIdentity, below), granted AcrPull, since the registry has admin auth disabled.')
param containerRegistryName string

@description('Azure AD tenant ID, if this tenant should use Entra ID auth instead of the shared-secret fallback. Leave empty to use the apiToken parameter as CLAIMVAL_API_TOKEN instead.')
param azureAdTenantId string = ''

@description('Azure AD app (client) ID — this tenant\'s own app registration, required if azureAdTenantId is set.')
param azureAdClientId string = ''

@description('Not intended to be passed explicitly — utcNow() is only valid as a parameter default. Feeds template.revisionSuffix so every deploy forces a genuinely new revision; see that property\'s own comment for why.')
param deployTimestamp string = utcNow('yyyyMMddHHmmss')

var resourceName = '${tenantName}cv'
// Storage accounts and Key Vaults both cap names at 24 characters —
// uniqueString() always returns exactly 13, so the prefix taken from
// resourceName has to leave room for that plus any literal suffix
// (storage: 24 - 13 = 11; vault: 24 - 13 - 2 for "kv" = 9). The compiler
// only flags the storage account case (BCP335) since Bicep's type index
// doesn't carry Key Vault's length constraint the same way — checked and
// sized both by hand rather than trusting the linter to catch it.
var storageAccountName = toLower('${take(resourceName, 11)}${uniqueString(resourceGroup().id, tenantName)}')
var fileShareName = 'data'
var keyVaultName = toLower('${take(resourceName, 9)}kv${uniqueString(resourceGroup().id, tenantName)}')
var containerAppName = '${tenantName}-claimval'
var apiTokenSecretName = 'claimval-api-token'
var anthropicKeySecretName = 'anthropic-api-key'
// No password anywhere in this string — CLAIMVAL_DB_AAD_AUTH=true (set
// on the Container App below) tells db/database.py to supply a fresh
// Azure AD token as the password at connect time instead.
var dbUrl = 'postgresql://${containerAppName}@${postgresServer.properties.fullyQualifiedDomainName}:5432/${tenantName}?sslmode=require'

resource containerAppsEnv 'Microsoft.App/managedEnvironments@2024-03-01' existing = {
  name: containerAppsEnvName
}

resource postgresServer 'Microsoft.DBforPostgreSQL/flexibleServers@2024-08-01' existing = {
  name: postgresServerName
}

resource containerRegistry 'Microsoft.ContainerRegistry/registries@2023-07-01' existing = {
  name: containerRegistryName
}

// A separate database per tenant on the one shared server — not a separate
// server. See shared.bicep's comment for why that's still real isolation.
resource tenantDatabase 'Microsoft.DBforPostgreSQL/flexibleServers/databases@2024-08-01' = {
  parent: postgresServer
  name: tenantName
}

resource storageAccount 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: storageAccountName
  location: location
  sku: {
    name: 'Standard_LRS'
  }
  kind: 'StorageV2'
  properties: {
    minimumTlsVersion: 'TLS1_2'
    allowBlobPublicAccess: false
  }
}

resource fileService 'Microsoft.Storage/storageAccounts/fileServices@2023-05-01' = {
  parent: storageAccount
  name: 'default'
}

resource fileShare 'Microsoft.Storage/storageAccounts/fileServices/shares@2023-05-01' = {
  parent: fileService
  name: fileShareName
  properties: {
    shareQuota: 100
  }
}

// File-share data-plane operations (reads, writes, deletes) have no audit
// trail at all without this — the standard Activity Log only records
// ARM/control-plane operations, never SMB file operations. Found live: an
// ontology directory this tenant's own Container App had written vanished
// from the share with no explanation findable anywhere, because this
// diagnostic setting didn't exist yet. Scoped to the fileServices
// sub-resource specifically, since that's where file-share request logs
// are actually emitted — a diagnostic setting on the storage account
// itself doesn't capture them.
resource fileServiceDiagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  scope: fileService
  name: '${tenantName}-file-audit'
  properties: {
    workspaceId: logAnalyticsWorkspaceId
    logs: [
      { category: 'StorageRead', enabled: true }
      { category: 'StorageWrite', enabled: true }
      { category: 'StorageDelete', enabled: true }
    ]
  }
}

// Registers the share with the Container Apps Environment — a prerequisite
// before any Container App in this environment can mount it.
resource envStorage 'Microsoft.App/managedEnvironments/storages@2024-03-01' = {
  parent: containerAppsEnv
  name: '${tenantName}-share'
  properties: {
    azureFile: {
      accountName: storageAccount.name
      accountKey: storageAccount.listKeys().keys[0].value
      shareName: fileShareName
      accessMode: 'ReadWrite'
    }
  }
}

// Pre-created, exactly like pg-admin-identity.bicep, and for the same
// reason: its principalId is known immediately, unlike a system-assigned
// identity's — see the file-level comment above for the deadlock this
// breaks. Never granted any Azure RBAC role itself beyond what's assigned
// to it below; it exists only to be the Container App's own identity.
resource appIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${tenantName}-app-identity'
  location: location
}

resource keyVault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: keyVaultName
  location: location
  properties: {
    sku: {
      family: 'A'
      name: 'standard'
    }
    tenantId: subscription().tenantId
    enableRbacAuthorization: true
  }
}

resource apiTokenSecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: keyVault
  name: apiTokenSecretName
  properties: {
    value: apiToken
  }
}

resource anthropicKeySecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: keyVault
  name: anthropicKeySecretName
  properties: {
    value: anthropicApiKey
  }
}

resource containerApp 'Microsoft.App/containerApps@2024-03-01' = {
  name: containerAppName
  location: location
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${appIdentity.id}': {}
    }
  }
  // Explicit, on top of whatever Bicep infers from property references
  // below: this Container App must not be created until every permission
  // its identity needs (AcrPull, the Key Vault role, the Postgres grant)
  // already exists — the whole point of the pre-created identity above.
  // Without this, Bicep is still free to schedule containerApp in
  // parallel with these three, which would just reintroduce a timing race
  // in place of the hard deadlock this replaces.
  dependsOn: [
    acrPullRole
    keyVaultSecretsUserRole
    grantContainerAppDbAccess
  ]
  properties: {
    managedEnvironmentId: containerAppsEnv.id
    configuration: {
      ingress: {
        external: true
        targetPort: 8000
        allowInsecure: false
      }
      registries: [
        {
          server: containerRegistry.properties.loginServer
          identity: appIdentity.id
        }
      ]
      secrets: [
        {
          name: apiTokenSecretName
          keyVaultUrl: apiTokenSecret.properties.secretUri
          identity: appIdentity.id
        }
        {
          name: anthropicKeySecretName
          keyVaultUrl: anthropicKeySecret.properties.secretUri
          identity: appIdentity.id
        }
      ]
    }
    template: {
      // Forces a genuinely new revision on every deploy. Found live: an
      // identity change (or a registries/secrets config change) doesn't
      // by itself create a new revision, since those live outside
      // `template` — Container Apps revisions are keyed off changes to
      // `template` specifically. Without this, an existing revision keeps
      // resolving pull/Key-Vault-secret credentials against whatever
      // identity was current when *that revision* was first created,
      // even after the app's identity has since changed underneath it —
      // confirmed by an ACR pull and a Key Vault secret sync both still
      // showing the old, already-deleted identity's oid in their auth
      // failures, well after the app-level identity had moved on.
      revisionSuffix: 'r${deployTimestamp}'
      containers: [
        {
          name: 'claim-validator'
          image: containerImage
          resources: {
            cpu: json('0.5')
            memory: '1Gi'
          }
          env: concat([
            {
              name: 'CLAIMVAL_API_TOKEN'
              secretRef: apiTokenSecretName
            }
            {
              name: 'ANTHROPIC_API_KEY'
              secretRef: anthropicKeySecretName
            }
            {
              // Not a secret — see dbUrl's own comment above. No
              // password lives in this value at all.
              name: 'CLAIMVAL_DB_URL'
              value: dbUrl
            }
            {
              name: 'CLAIMVAL_DB_AAD_AUTH'
              value: 'true'
            }
            {
              // Read by azure-identity's DefaultAzureCredential (not this
              // project's own code) to pick which managed identity to use —
              // required because appIdentity is user-assigned, not
              // system-assigned; without this, that resolution is
              // ambiguous and can silently try the wrong (nonexistent)
              // identity. See the file-level comment on appIdentity above.
              name: 'AZURE_CLIENT_ID'
              value: appIdentity.properties.clientId
            }
            {
              name: 'CLAIMVAL_PROVIDER'
              value: 'anthropic'
            }
            {
              name: 'CLAIMVAL_SOURCE_DIR'
              value: '/data/source'
            }
            {
              name: 'CLAIMVAL_STORE_ROOT'
              value: '/data/ontologies'
            }
            {
              name: 'CLAIMVAL_REPORTS_DIR'
              value: '/data/reports'
            }
          ], !empty(azureAdTenantId) ? [
            {
              name: 'CLAIMVAL_AZURE_TENANT_ID'
              value: azureAdTenantId
            }
            {
              name: 'CLAIMVAL_AZURE_CLIENT_ID'
              value: azureAdClientId
            }
          ] : [])
          volumeMounts: [
            {
              volumeName: 'data'
              mountPath: '/data'
            }
          ]
        }
      ]
      volumes: [
        {
          name: 'data'
          storageType: 'AzureFile'
          storageName: envStorage.name
        }
      ]
      scale: {
        minReplicas: 0
        maxReplicas: 2
        rules: [
          {
            name: 'http-scale'
            http: {
              metadata: {
                concurrentRequests: '10'
              }
            }
          }
        ]
      }
    }
  }
}

// "Key Vault Secrets User" — read-only access to secret values, nothing
// else. Scoped to this tenant's own vault only. (This is Azure RBAC, for
// Key Vault; the Postgres grant below is a separate mechanism — see its
// own comment.)
resource keyVaultSecretsUserRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(keyVault.id, appIdentity.id, 'Key Vault Secrets User')
  scope: keyVault
  properties: {
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      '4633458b-17de-408a-b874-0445c86b69e6'
    )
    principalId: appIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

// "AcrPull" — lets the Container App's identity pull containerImage from
// the registry, since the registry has admin auth disabled entirely (no
// username/password to leak). Scoped to just this one registry. Granted to
// appIdentity, which exists independently of the Container App (see its
// own comment above) — specifically so this role is guaranteed to exist
// before the Container App's first revision ever attempts a pull, rather
// than racing it. (An earlier version of this file granted the Container
// App's own system-assigned identity instead, which meant this role could
// only be created after the Container App succeeded — but the Container
// App's first revision couldn't succeed without it. Found live: a real,
// reproducible deadlock, not just a slow-propagation race — retrying the
// same deployment did not resolve it.)
resource acrPullRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(containerRegistry.id, appIdentity.id, 'AcrPull')
  scope: containerRegistry
  properties: {
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      '7f951dda-4ed3-4680-a7ca-43fe172d538d'
    )
    principalId: appIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

// Grants the Container App's identity (appIdentity, above) a Postgres role
// and access to only this tenant's database — run once, at deploy time,
// using the shared AAD admin identity (never the Container App's own
// identity, which has no admin rights on the server and shouldn't). Not
// Azure RBAC: Postgres Flexible Server's AAD integration uses its own
// role system, bridged to an AAD object via a special server-side
// function, not an Azure roleAssignment the way Key Vault access above
// is. Depends only on tenantDatabase and appIdentity (via the
// APP_PRINCIPAL_ID reference below) — not on containerApp — so this can
// run, and finish, before the Container App is ever created. See the
// file-level comment on appIdentity for why that ordering matters.
//
// pgaadauth_create_principal_with_oid(roleName, objectId, objectType,
// isAdmin, isMfa) — role name first, object ID second, plus a required
// objectType ('service' here, for a managed identity). Verified live
// against a real deployment and against Microsoft's current docs
// (learn.microsoft.com/azure/postgresql/security/security-manage-entra-users):
// the first version of this script had it wrong on both counts (object
// ID first, objectType missing entirely) and failed with "function ...
// does not exist" until corrected.
resource grantContainerAppDbAccess 'Microsoft.Resources/deploymentScripts@2023-08-01' = {
  name: '${tenantName}-grant-db-access'
  location: location
  kind: 'AzureCLI'
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${pgAdminIdentityId}': {}
    }
  }
  properties: {
    azCliVersion: '2.60.0'
    retentionInterval: 'PT1H'
    timeout: 'PT15M'
    cleanupPreference: 'OnSuccess'
    environmentVariables: [
      { name: 'PG_HOST', value: postgresServer.properties.fullyQualifiedDomainName }
      { name: 'PG_ADMIN_NAME', value: pgAdminIdentityName }
      { name: 'PG_ADMIN_CLIENT_ID', value: pgAdminIdentityClientId }
      { name: 'APP_PRINCIPAL_ID', value: appIdentity.properties.principalId }
      { name: 'APP_ROLE_NAME', value: containerAppName }
      { name: 'TENANT_DB', value: tenantName }
    ]
    // The linter flags ossrdbms-aad.database.windows.net inside the script
    // content below as a hardcoded environment URL — correct, and
    // deliberate: this project targets Azure Public Cloud only, nothing
    // here has ever assumed Azure Government or China cloud support, and
    // parameterizing a cloud-specific endpoint nobody asked for would be
    // speculative generality, not a real fix.
    #disable-next-line no-hardcoded-env-urls
    scriptContent: '''
      set -euo pipefail

      # The AzureCLI deployment-script container's base OS isn't fixed
      # across CLI versions (found live: azCliVersion 2.60.0 runs on
      # Azure Linux/tdnf, not Debian/apt-get, which the first version of
      # this script assumed and failed on) — detect the package manager
      # instead of hardcoding one.
      if command -v tdnf >/dev/null 2>&1; then
        tdnf install -y postgresql >/dev/null
      elif command -v apk >/dev/null 2>&1; then
        apk add --no-cache postgresql-client >/dev/null
      elif command -v apt-get >/dev/null 2>&1; then
        apt-get update -qq && apt-get install -y -qq postgresql-client >/dev/null
      else
        echo "No supported package manager found (tried tdnf, apk, apt-get)" >&2
        exit 1
      fi

      # --allow-no-subscriptions: this identity is deliberately not
      # granted any Azure RBAC role (it only needs Postgres data-plane
      # AAD admin rights, granted via administrators@2024-08-01 in
      # shared.bicep, not an ARM roleAssignment) — az login otherwise
      # treats "0 subscriptions visible to this identity" as an error
      # (found live: this was the actual failure, not the earlier
      # apk/tdnf output bundled alongside it in Azure's error report).
      az login --identity --username "$PG_ADMIN_CLIENT_ID" --allow-no-subscriptions >/dev/null

      export PGPASSWORD=$(az account get-access-token \
        --resource https://ossrdbms-aad.database.windows.net \
        --query accessToken -o tsv)

      CONN="host=$PG_HOST port=5432 dbname=postgres user=$PG_ADMIN_NAME sslmode=require"

      # Idempotent against identity churn regardless: this now runs against
      # appIdentity, a user-assigned identity whose principalId is stable
      # across Container App redeploys (unlike the system-assigned identity
      # this originally guarded against, whose principalId was observed live
      # to change after a remove/re-add cycle). Kept idempotent anyway —
      # appIdentity itself could still be deleted and recreated, and this
      # script always runs on every redeploy regardless of whether identity
      # actually changed. pgaadauth_create_principal_with_oid does an
      # unconditional CREATE ROLE and fails on a role name that already
      # exists (as it will on every redeploy after the first), so branch:
      # create fresh only if the role doesn't exist yet, otherwise just
      # re-point its AAD object-ID mapping at the current identity via the
      # SECURITY LABEL form documented for exactly this case (see
      # infra/README.md).
      ROLE_EXISTS=$(psql "$CONN" -tA -v ON_ERROR_STOP=1 -c \
        "SELECT 1 FROM pg_roles WHERE rolname = '$APP_ROLE_NAME';")

      if [ -z "$ROLE_EXISTS" ]; then
        psql "$CONN" -v ON_ERROR_STOP=1 -c \
          "SELECT * FROM pgaadauth_create_principal_with_oid('$APP_ROLE_NAME', '$APP_PRINCIPAL_ID', 'service', false, false);"
      else
        psql "$CONN" -v ON_ERROR_STOP=1 -c \
          "SECURITY LABEL for \"pgaadauth\" on role \"$APP_ROLE_NAME\" is 'aadauth,oid=$APP_PRINCIPAL_ID,type=service';"
      fi

      psql "$CONN" -v ON_ERROR_STOP=1 -c \
        "GRANT ALL PRIVILEGES ON DATABASE \"$TENANT_DB\" TO \"$APP_ROLE_NAME\";"

      psql "host=$PG_HOST port=5432 dbname=$TENANT_DB user=$PG_ADMIN_NAME sslmode=require" \
        -v ON_ERROR_STOP=1 -c \
        "GRANT ALL ON SCHEMA public TO \"$APP_ROLE_NAME\";"
    '''
  }
  dependsOn: [
    tenantDatabase
  ]
}

output containerAppFqdn string = containerApp.properties.configuration.ingress.fqdn
output keyVaultName string = keyVault.name
output apiTokenSecretName string = apiTokenSecretName
