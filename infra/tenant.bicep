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
//                  containerImage=<registry>/claim-validator:<tag>

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

@description('Azure AD tenant ID, if this tenant should use Entra ID auth instead of the shared-secret fallback. Leave empty to use the generated CLAIMVAL_API_TOKEN instead.')
param azureAdTenantId string = ''

@description('Azure AD app (client) ID — this tenant\'s own app registration, required if azureAdTenantId is set.')
param azureAdClientId string = ''

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

// Grants the Container App's own identity a Postgres role and access to
// only this tenant's database — run once, at deploy time, using the
// shared AAD admin identity (never the Container App's own identity,
// which has no admin rights on the server and shouldn't). Not
// Azure RBAC: Postgres Flexible Server's AAD integration uses its own
// role system, bridged to an AAD object via a special server-side
// function, not an Azure roleAssignment the way Key Vault access above
// is.
//
// pgaadauth_create_principal_with_oid's exact signature is the one piece
// of this whole file not verified against a live server — no Azure
// subscription was available to actually run this deployment script.
// Confirm the current signature against Microsoft's own Azure AD
// Postgres Flexible Server documentation before the first real deploy.
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
      apt-get update -qq && apt-get install -y -qq postgresql-client >/dev/null

      az login --identity --username "$PG_ADMIN_CLIENT_ID" >/dev/null

      export PGPASSWORD=$(az account get-access-token \
        --resource https://ossrdbms-aad.database.windows.net \
        --query accessToken -o tsv)

      CONN="host=$PG_HOST port=5432 dbname=postgres user=$PG_ADMIN_NAME sslmode=require"

      psql "$CONN" -v ON_ERROR_STOP=1 -c \
        "SELECT * FROM pgaadauth_create_principal_with_oid('$APP_PRINCIPAL_ID', '$APP_ROLE_NAME', false, false);"

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
