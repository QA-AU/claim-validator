// Deployed once per environment (not once per tenant). Provisions the
// Container Apps hosting environment and one PostgreSQL Flexible Server —
// infrastructure genuinely safe to share across tenants, since no tenant
// DATA lives here: the environment is just a compute/networking boundary,
// and the Postgres server hosts one separate DATABASE per tenant (added by
// tenant.bicep), not one shared database. Isolation is still real —
// Postgres enforces database-level separation — this just avoids paying
// for and operating N database servers for N tenants.
//
//   az deployment group create -g <rg> -f infra/shared.bicep \
//     --parameters postgresAdminPassword=<secret>
//
// Run infra/tenant.bicep once per user after this exists.

@description('Azure region for all resources.')
param location string = resourceGroup().location

@description('Short, unique prefix for resource names (e.g. "cv").')
param namePrefix string = 'cv'

@description('Administrator username for the shared PostgreSQL server.')
param postgresAdminLogin string = 'claimval_admin'

@secure()
@description('Administrator password for the shared PostgreSQL server.')
param postgresAdminPassword string

@description('PostgreSQL Flexible Server SKU — B1ms is the smallest burstable tier, adequate for a handful of low-traffic tenants.')
param postgresSkuName string = 'Standard_B1ms'

var logAnalyticsName = '${namePrefix}-logs'
var containerAppsEnvName = '${namePrefix}-env'
var postgresServerName = '${namePrefix}-pg-${uniqueString(resourceGroup().id)}'

resource logAnalytics 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: logAnalyticsName
  location: location
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    retentionInDays: 30
  }
}

resource containerAppsEnv 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: containerAppsEnvName
  location: location
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logAnalytics.properties.customerId
        sharedKey: logAnalytics.listKeys().primarySharedKey
      }
    }
  }
}

resource postgresServer 'Microsoft.DBforPostgreSQL/flexibleServers@2024-08-01' = {
  name: postgresServerName
  location: location
  sku: {
    name: postgresSkuName
    tier: 'Burstable'
  }
  properties: {
    version: '16'
    administratorLogin: postgresAdminLogin
    administratorLoginPassword: postgresAdminPassword
    storage: {
      storageSizeGB: 32
    }
    backup: {
      backupRetentionDays: 7
      geoRedundantBackup: 'Disabled'
    }
    highAvailability: {
      mode: 'Disabled'
    }
  }
}

// Allows Azure services (Container Apps included) to reach the server —
// tightened to specific subnets via VNet integration is the natural next
// step once this actually needs to be locked down beyond two tenants.
resource postgresFirewallAzure 'Microsoft.DBforPostgreSQL/flexibleServers/firewallRules@2024-08-01' = {
  parent: postgresServer
  name: 'AllowAzureServices'
  properties: {
    startIpAddress: '0.0.0.0'
    endIpAddress: '0.0.0.0'
  }
}

output containerAppsEnvId string = containerAppsEnv.id
output containerAppsEnvName string = containerAppsEnv.name
output postgresServerName string = postgresServer.name
output postgresServerFqdn string = postgresServer.properties.fullyQualifiedDomainName
