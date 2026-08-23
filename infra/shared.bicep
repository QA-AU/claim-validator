// Deployed once per environment (not once per tenant), after
// pg-admin-identity.bicep. Provisions the Container Apps hosting
// environment and one PostgreSQL Flexible Server — infrastructure
// genuinely safe to share across tenants, since no tenant DATA lives
// here: the environment is just a compute/networking boundary, and the
// Postgres server hosts one separate DATABASE per tenant (added by
// tenant.bicep), not one shared database. Isolation is still real —
// Postgres enforces database-level separation — this just avoids paying
// for and operating N database servers for N tenants.
//
// Database access is Azure AD only — passwordAuth is disabled on the
// server below, so there is no admin password to leak, rotate, or store
// anywhere, ever. pg-admin-identity.bicep's identity becomes the server's
// AAD administrator specifically so tenant.bicep's deployment script can
// use it to grant each tenant's own Container App identity a Postgres
// role, headlessly, without a human's own credentials in the loop.
//
//   az deployment group create -g <rg> -f infra/shared.bicep \
//     --parameters pgAdminIdentityId=<from pg-admin-identity.bicep output> \
//                  pgAdminIdentityPrincipalId=<same, principalId output> \
//                  pgAdminIdentityName=<same, name output>
//
// Run infra/tenant.bicep once per user after this exists.

@description('Azure region for all resources.')
param location string = resourceGroup().location

@description('Short, unique prefix for resource names (e.g. "cv").')
param namePrefix string = 'cv'

@description('PostgreSQL Flexible Server SKU — B1ms is the smallest burstable tier, adequate for a handful of low-traffic tenants.')
param postgresSkuName string = 'Standard_B1ms'

@description('Resource ID of the identity from pg-admin-identity.bicep (its "id" output).')
param pgAdminIdentityId string

@description('Principal (object) ID of that same identity (its "principalId" output) — becomes the Postgres AAD administrator.')
param pgAdminIdentityPrincipalId string

@description('Name of that same identity (its "name" output).')
param pgAdminIdentityName string

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
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${pgAdminIdentityId}': {}
    }
  }
  properties: {
    version: '16'
    authConfig: {
      activeDirectoryAuth: 'Enabled'
      passwordAuth: 'Disabled'
    }
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

// The one AAD principal that can create/grant Postgres roles for other
// identities — see tenant.bicep's deployment script, which uses this same
// identity to onboard each tenant's Container App. Unlike the identity's
// own creation, this is safe to reference by parameter here: the
// principalId already exists by the time this deploys (it was created in
// the prior, separate pg-admin-identity.bicep deployment), so it's an
// ordinary known value, not something computed mid-deployment.
resource postgresAadAdmin 'Microsoft.DBforPostgreSQL/flexibleServers/administrators@2024-08-01' = {
  parent: postgresServer
  name: pgAdminIdentityPrincipalId
  properties: {
    principalType: 'ServicePrincipal'
    principalName: pgAdminIdentityName
    tenantId: subscription().tenantId
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
