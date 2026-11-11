// entra-agent-blueprint.bicep
//
// What Bicep CAN express on the road to an Agent 365 agent identity:
// the supporting Azure-side resources (a Key Vault for the agent's credential,
// with access locked to the operator group). What Bicep CANNOT express:
// the Entra agent identity blueprint and agent identity themselves — those are
// Microsoft Graph constructs, not ARM resources. Create them with
// scripts/Register-Agent365.ps1 (Graph PowerShell SDK). This file deliberately
// stops at the boundary instead of faking a resource type that doesn't exist.

targetScope = 'resourceGroup'

@description('Short name for the agent, used to name supporting resources. Lowercase letters and digits.')
param agentName string

@description('Azure region for supporting resources.')
param location string = resourceGroup().location

@description('Object ID of the Entra group allowed to read the agent credential from Key Vault (e.g. the agent operators group).')
param operatorsGroupObjectId string

@description('Tags applied to supporting resources.')
param tags object = {
  workload: 'agent365'
  agent: agentName
}

resource keyVault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: 'kv-a365-${agentName}'
  location: location
  tags: tags
  properties: {
    sku: {
      family: 'A'
      name: 'standard'
    }
    tenantId: subscription().tenantId
    enableRbacAuthorization: true
    // The agent identity's client secret/certificate lives here — never in the
    // repo, never in app settings in plain text. The agent reads it at runtime
    // via its (human-managed bootstrap) identity, or you inject it via your
    // deployment pipeline's secret store integration.
  }
}

// Key Vault Secrets Officer for the operators group (humans who rotate the
// agent credential). The agent itself gets Secrets User via a separate
// assignment once its identity exists — that assignment is created by the
// registration script because the identity's principal ID doesn't exist yet
// at template time.
resource kvSecretsOfficer 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(keyVault.id, operatorsGroupObjectId, 'secrets-officer')
  scope: keyVault
  properties: {
    principalId: operatorsGroupObjectId
    principalType: 'Group'
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      'b86a8fe4-44ce-4948-aee5-eccb2c155cd7' // Key Vault Secrets Officer
    )
  }
}

output keyVaultName string = keyVault.name
output keyVaultUri string = keyVault.properties.vaultUri
output nextStep string = 'Now run scripts/Register-Agent365.ps1 to create the agent identity blueprint + identity in Entra ID (Graph), then store the credential in this vault.'
