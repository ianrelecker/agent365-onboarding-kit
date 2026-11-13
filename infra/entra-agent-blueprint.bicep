// Supporting Azure resources for an Agent 365 agent identity.
//
// Bicep creates the credential boundary the blueprint federates to: a
// user-assigned managed identity, and a Key Vault for anything else the agent
// must read. It does not create the Entra agent identity blueprint. That is a
// Graph object. scripts/Register-Agent365.ps1 creates it and attaches a
// federated credential whose subject is this managed identity's principal id.
//
// The vault is created closed: RBAC authorization, purge protection, no
// trusted-services bypass, and public network access disabled unless you opt
// in. Purge protection cannot be turned off later.

targetScope = 'resourceGroup'

@description('Short name for the agent. 3-17 lowercase letters and digits, used in resource names. Key Vault rejects anything else.')
@minLength(3)
@maxLength(17)
param agentName string

@description('Azure region for supporting resources.')
param location string = resourceGroup().location

@description('Object ID of the Entra group allowed to administer secrets in this vault.')
param operatorsGroupObjectId string

@description('Enabled keeps a public endpoint. Disabled is the production default. Either way, the firewall default action is Deny.')
@allowed([
  'Enabled'
  'Disabled'
])
param publicNetworkAccess string = 'Disabled'

@description('IPv4 CIDR ranges allowed to reach the vault when public network access is Enabled. Example: 203.0.113.10/32.')
param allowedIpAddresses array = []

@description('Subnet resource ID for a private endpoint. Leave empty to skip the private endpoint. Private DNS for privatelink.vaultcore.azure.net is still required and is not created here.')
param privateEndpointSubnetId string = ''

@description('Log Analytics workspace resource ID for Key Vault AuditEvent logs. Leave empty to skip diagnostics.')
param logAnalyticsWorkspaceId string = ''

@description('Tags applied to supporting resources.')
param tags object = {
  workload: 'agent365'
  agent: agentName
}

var uniqueSuffix = take(uniqueString(resourceGroup().id, agentName), 8)
var keyVaultName = 'kva365${take(agentName, 10)}${uniqueSuffix}'

resource managedIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: 'id-a365-${agentName}'
  location: location
  tags: tags
}

resource keyVault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: keyVaultName
  location: location
  tags: tags
  properties: {
    sku: {
      family: 'A'
      name: 'standard'
    }
    tenantId: subscription().tenantId
    enableRbacAuthorization: true
    enableSoftDelete: true
    softDeleteRetentionInDays: 90
    enablePurgeProtection: true
    publicNetworkAccess: publicNetworkAccess
    enabledForDeployment: false
    enabledForDiskEncryption: false
    enabledForTemplateDeployment: false
    networkAcls: {
      defaultAction: 'Deny'
      bypass: 'None'
      ipRules: [for ip in allowedIpAddresses: {
        value: ip
      }]
    }
  }
}

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

resource kvSecretsUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(keyVault.id, managedIdentity.id, 'secrets-user')
  scope: keyVault
  properties: {
    principalId: managedIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      '4633458b-17de-408a-b874-0445c86b69e6' // Key Vault Secrets User
    )
  }
}

resource privateEndpoint 'Microsoft.Network/privateEndpoints@2023-11-01' = if (!empty(privateEndpointSubnetId)) {
  name: 'pe-${keyVaultName}'
  location: location
  tags: tags
  properties: {
    subnet: {
      id: privateEndpointSubnetId
    }
    privateLinkServiceConnections: [
      {
        name: 'vault'
        properties: {
          privateLinkServiceId: keyVault.id
          groupIds: [
            'vault'
          ]
        }
      }
    ]
  }
}

resource vaultDiagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = if (!empty(logAnalyticsWorkspaceId)) {
  name: 'diag-${keyVaultName}'
  scope: keyVault
  properties: {
    workspaceId: logAnalyticsWorkspaceId
    logs: [
      {
        category: 'AuditEvent'
        enabled: true
      }
    ]
  }
}

output keyVaultName string = keyVault.name
output keyVaultUri string = keyVault.properties.vaultUri
output managedIdentityClientId string = managedIdentity.properties.clientId
output managedIdentityPrincipalId string = managedIdentity.properties.principalId
output publicNetworkAccess string = publicNetworkAccess
output nextStep string = 'Run scripts/Register-Agent365.ps1 with -ManagedIdentityPrincipalId and -ManagedIdentityClientId from these outputs. The vault stays closed until you add a private endpoint and private DNS, or explicitly enable public access with allowed IP ranges.'
