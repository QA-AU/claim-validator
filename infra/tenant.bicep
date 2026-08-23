// One deployment per user — the silo model demonstrated locally earlier:
// each tenant gets its own storage share, its own database (on the shared
// server from shared.bicep), its own Key Vault, and its own Container App,
// so one user's data is never reachable through another's credentials.
//
//   az deployment group create -g <rg> -f infra/tenant.bicep \
//     --parameters tenantName=usera \
//                  containerAppsEnvName=<from shared.bicep output> \
//                  postgresServerName=<from shared.bicep output> \
//                  postgresAdminPassword=<same one used in shared.bicep> \
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

@description('Admin login used when shared.bicep provisioned the Postgres server.')
param postgresAdminLogin string = 'claimval_admin'

@secure()
@description('Admin password used when shared.bicep provisioned the Postgres server — needed here once, to create this tenant\'s own database and role.')
param postgresAdminPassword string

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
var dbUrlSecretName = 'claimval-db-url'
var generatedApiToken = uniqueString(resourceGroup().id, tenantName, deployment().name)

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

resource dbUrlSecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: keyVault
  name: dbUrlSecretName
  properties: {
    value: 'postgresql://${postgresAdminLogin}:${postgresAdminPassword}@${postgresServer.properties.fullyQualifiedDomainName}:5432/${tenantName}'
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
        {
          name: dbUrlSecretName
          keyVaultUrl: dbUrlSecret.properties.secretUri
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
              name: 'CLAIMVAL_DB_URL'
              secretRef: dbUrlSecretName
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
// else. Scoped to this tenant's own vault only.
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

output containerAppFqdn string = containerApp.properties.configuration.ingress.fqdn
output keyVaultName string = keyVault.name
output generatedApiTokenSecretName string = apiTokenSecretName
