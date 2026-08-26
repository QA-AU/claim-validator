// One deployment per user — the silo model demonstrated locally earlier:
// each tenant gets its own storage share, its own database (on the shared
// server from shared.bicep), its own Key Vault, and its own Container App,
// so one user's data is never reachable through another's credentials.
//
// Database access is Azure AD only, matching shared.bicep's server
// (passwordAuth disabled there) — there is no DB password anywhere in
// this file, in Key Vault, or in the Container App's own configuration.
// A deployment script (below) grants the Container App's own
// system-assigned identity a Postgres role, using shared.bicep's admin
// identity to do it; at runtime, db/database.py's Azure AD auth path
// fetches a fresh access token per connection through that same identity.
//
//   az deployment group create -g <rg> -f infra/tenant.bicep \
//     --parameters tenantName=usera \
//                  containerAppsEnvName=<from shared.bicep output> \
//                  postgresServerName=<from shared.bicep output> \
//                  pgAdminIdentityId=<from pg-admin-identity.bicep output, "id"> \
//                  pgAdminIdentityClientId=<same, "clientId" output> \
//                  pgAdminIdentityName=<same, "name" output> \
//                  anthropicApiKey=<this tenant's own key> \
//                  containerImage=<registry>/claim-validator:<tag> \
//                  containerRegistryName=<registry name, e.g. "cvacr8b4cb977">

@description('Short, unique identifier for this tenant (e.g. "usera") — prefixes every resource name and becomes the database name.')
@minLength(3)
@maxLength(20)
param tenantName string

@description('Azure region — should match shared.bicep.')
param location string = resourceGroup().location

@description('Name of the Container Apps Environment created by shared.bicep.')
param containerAppsEnvName string

@description('Name of the PostgreSQL Flexible Server created by shared.bicep.')
param postgresServerName string

@description('Resource ID of the Postgres AAD admin identity (pg-admin-identity.bicep\'s "id" output) — used here only to run the role-granting deployment script below.')
param pgAdminIdentityId string

@description('Client ID of that same identity (its "clientId" output) — the deployment script logs in as it explicitly.')
param pgAdminIdentityClientId string

@description('Name of that same identity (its "name" output) — the Postgres role the script connects as.')
param pgAdminIdentityName string

@secure()
@description('This tenant\'s own Anthropic API key — kept separate per tenant by design (see the shared-vs-separate-keys discussion this was built from).')
param anthropicApiKey string

@description('Container image, e.g. myregistry.azurecr.io/claim-validator:latest.')
param containerImage string

@description('Name (not login server) of the Azure Container Registry containerImage is hosted in — the Container App pulls from it using its own system-assigned identity, granted AcrPull below, since the registry has admin auth disabled.')
param containerRegistryName string

@description('Azure AD tenant ID, if this tenant should use Entra ID auth instead of the shared-secret fallback. Leave empty to use the generated CLAIMVAL_API_TOKEN instead.')
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
var generatedApiToken = uniqueString(resourceGroup().id, tenantName, deployment().name)
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
    value: generatedApiToken
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
    type: 'SystemAssigned'
  }
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
          identity: 'System'
        }
      ]
      secrets: [
        {
          name: apiTokenSecretName
          keyVaultUrl: apiTokenSecret.properties.secretUri
          identity: 'System'
        }
        {
          name: anthropicKeySecretName
          keyVaultUrl: anthropicKeySecret.properties.secretUri
          identity: 'System'
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
  name: guid(keyVault.id, containerApp.id, 'Key Vault Secrets User')
  scope: keyVault
  properties: {
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      '4633458b-17de-408a-b874-0445c86b69e6'
    )
    principalId: containerApp.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

// "AcrPull" — lets the Container App's own system-assigned identity pull
// containerImage from the registry, since the registry has admin auth
// disabled entirely (no username/password to leak). Scoped to just this
// one registry. Known ordering caveat: on a first-ever deploy, the
// Container App's initial revision can attempt its first image pull
// before this role assignment has finished propagating (both resources
// deploy in the same operation, and Azure RBAC propagation isn't
// instant) — if that happens, the fix is re-issuing this same deployment
// once the role assignment exists, not a code change.
resource acrPullRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(containerRegistry.id, containerApp.id, 'AcrPull')
  scope: containerRegistry
  properties: {
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      '7f951dda-4ed3-4680-a7ca-43fe172d538d'
    )
    principalId: containerApp.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

// Grants the Container App's own identity a Postgres role and access to
// only this tenant's database — run once, at deploy time, using the
// shared AAD admin identity (never the Container App's own identity,
// which has no admin rights on the server and shouldn't). Not
// Azure RBAC: Postgres Flexible Server's AAD integration uses its own
// role system, bridged to an AAD object via a special server-side
// function, not an Azure roleAssignment the way Key Vault access above
// is.
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
      { name: 'APP_PRINCIPAL_ID', value: containerApp.identity.principalId }
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

      # Idempotent against identity churn: a Container App's system-assigned
      # identity's principalId is not permanently stable — it was observed
      # live to change after an identity remove/re-add cycle, orphaning the
      # previous AAD-to-role mapping. pgaadauth_create_principal_with_oid
      # does an unconditional CREATE ROLE and fails on a role name that
      # already exists (as it will on every redeploy after the first, or
      # after any identity change), so branch: create fresh only if the
      # role doesn't exist yet, otherwise just re-point its AAD object-ID
      # mapping at the current identity via the SECURITY LABEL form
      # documented for exactly this case (see infra/README.md).
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
output generatedApiTokenSecretName string = apiTokenSecretName
