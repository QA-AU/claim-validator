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
// Defaults to australiaeast, not resourceGroup().location — see
// pg-admin-identity.bicep's location param for why (a permanent,
// harmless metadata mismatch on this project's real resource group;
// the default used to silently target the wrong region).
param location string = 'australiaeast'

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

// Allows Azure services (Container Apps included) to reach the server.
//
// Tried narrowing this to the Container Apps environment's own static IP
// (`containerAppsEnv.properties.staticIp`) after a Postgres audit-log
// review turned up 115 rejected scan/probe attempts over 7 days — real
// unsolicited traffic, though all of it already blocked pre-auth. That
// change caused a real outage: on a Consumption-only environment (no VNet
// integration), Azure does not guarantee that property is the actual
// outbound IP for all traffic — per Microsoft's own guidance, outbound
// IPs on Consumption plan can vary and aren't reliably the one shown —
// so real app traffic got firewalled out, both tenants hung on startup
// (blocking on their first DB connection) until this rule was restored.
// Reverted within minutes; both tenants recovered immediately once this
// rule was back. See infra/README.md's postmortem for the full story.
//
// The only Microsoft-documented way to get a real static, firewall-able
// outbound IP here is VNet integration + NAT Gateway — which needs a
// workload-profiles environment, not this Consumption-only one, and is a
// materially bigger change than the actual measured risk (zero
// successful unauthorized connections; AAD-only auth is the real
// boundary) currently justifies. Left as the natural next step if this
// project ever needs tighter network isolation than that.
resource postgresFirewallAzure 'Microsoft.DBforPostgreSQL/flexibleServers/firewallRules@2024-08-01' = {
  parent: postgresServer
  name: 'AllowAzureServices'
  properties: {
    startIpAddress: '0.0.0.0'
    endIpAddress: '0.0.0.0'
  }
}

// Same reasoning as tenant.bicep's fileServiceDiagnostics: without this,
// there's no audit trail at all for what happened at the database level
// — no record of who connected, when, or whether an auth attempt failed.
// PostgreSQLLogs is the server's own log stream (connections, auth
// events, errors); PostgreSQLFlexSessions is per-session connection
// detail. Server-wide rather than per-tenant, since it's one shared
// Postgres server — a query's tenant is identifiable from which
// database/role it ran against, already visible in these logs.
resource postgresDiagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  scope: postgresServer
  name: 'postgres-audit'
  properties: {
    workspaceId: logAnalytics.id
    logs: [
      { category: 'PostgreSQLLogs', enabled: true }
      { category: 'PostgreSQLFlexSessions', enabled: true }
    ]
  }
}

output containerAppsEnvId string = containerAppsEnv.id
output containerAppsEnvName string = containerAppsEnv.name
output postgresServerName string = postgresServer.name
output postgresServerFqdn string = postgresServer.properties.fullyQualifiedDomainName
// Needed by tenant.bicep to wire up Storage Analytics diagnostic logging on
// each tenant's own file share — see that file's own comment for why.
output logAnalyticsWorkspaceId string = logAnalytics.id
