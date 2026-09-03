// Deployed on its own, before shared.bicep — not a module of it. Bicep
// will not let a resource's `name` or `identity` property reference
// another resource's (or even another module's) runtime output within
// the same deployment: those properties have to be resolvable before any
// resource in the template starts deploying, and a just-created
// identity's principalId genuinely isn't known until it exists (found by
// hitting BCP120 twice — once chaining the resource directly, again
// after moving it into a module, which turned out not to be exempt
// either). Deploying the identity as its own prior step sidesteps this
// cleanly: by the time shared.bicep runs, the identity already exists,
// so its principalId is just a value being passed in, not something
// computed mid-deployment.
//
//   az deployment group create -g <rg> -f infra/pg-admin-identity.bicep
//
// Then read the outputs (principalId, clientId, id) and pass them to
// shared.bicep's matching parameters.

@description('Azure region.')
// Defaults to australiaeast, not resourceGroup().location: this
// project's actual resource group (claim-validator-rg) reports
// australiacentral as its own location — a one-time historical
// mismatch from an early region-availability issue, permanent because
// Azure treats a resource group's location as immutable after
// creation (az group update has no --location option). Every real
// resource here has always lived in australiaeast regardless; a bare
// deploy relying on resourceGroup().location silently targeted the
// wrong region until this default was fixed. Override explicitly if
// this is ever deployed into a genuinely different resource group.
param location string = 'australiaeast'

@description('Short, unique prefix for resource names (e.g. "cv").')
param namePrefix string = 'cv'

resource pgAdminIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${namePrefix}-pg-admin-identity'
  location: location
}

output id string = pgAdminIdentity.id
output principalId string = pgAdminIdentity.properties.principalId
output clientId string = pgAdminIdentity.properties.clientId
output name string = pgAdminIdentity.name
