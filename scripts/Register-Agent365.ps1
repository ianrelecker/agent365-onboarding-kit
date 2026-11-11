<#
.SYNOPSIS
    Register a third-party agent for Microsoft Agent 365 governance:
    agent identity blueprint + agent identity + human sponsor, via Microsoft Graph.

.DESCRIPTION
    Idempotent: safe to re-run. If a blueprint with the same display name exists
    it is reused; the same for the agent identity under it. Supports -WhatIf.

    ACCURACY NOTE (read before first use in a tenant):
    The Entra "agent identity blueprint / agent identity" Graph surface was
    still evolving at Agent 365 GA (May 2026). The calls in the ADAPTER section
    below represent the intended shape of that surface. They are isolated in
    functions precisely so you can adjust them in one place against the current
    Microsoft Learn documentation for "agent identity blueprint" Graph APIs.
    Everything else in this script (prereq checks, idempotency, output) is
    tenant-independent. Do not treat the adapter as verified until you have run
    it in a dev tenant.

.PARAMETER DisplayName
    Display name for the agent (blueprint and identity share it).

.PARAMETER SponsorUpn
    UPN of the human sponsor accountable for this agent. Required — an agent
    without a sponsor is exactly the shadow-IT problem this kit exists to fix.

.EXAMPLE
    ./Register-Agent365.ps1 -DisplayName "Contoso Support Agent" -SponsorUpn "jane@contoso.com"

.EXAMPLE
    ./Register-Agent365.ps1 -DisplayName "Contoso Support Agent" -SponsorUpn "jane@contoso.com" -WhatIf
#>
[CmdletBinding(SupportsShouldProcess)]
param(
    [Parameter(Mandatory)]
    [string] $DisplayName,

    [Parameter(Mandatory)]
    [string] $SponsorUpn
)

$ErrorActionPreference = 'Stop'

# ---------------------------------------------------------------------------
# Prereqs
# ---------------------------------------------------------------------------
$scopes = @(
    'Organization.Read.All'
    'Directory.ReadWrite.All'   # blueprint/identity creation; tighten per your tenant's guidance
    'User.Read.All'
)
if (-not (Get-MgContext)) {
    Connect-MgGraph -Scopes $scopes | Out-Null
}

$sponsor = Get-MgUser -UserId $SponsorUpn -ErrorAction Stop
if (-not $sponsor.Id) { throw "Sponsor '$SponsorUpn' did not resolve to a user object." }
Write-Host "Sponsor: $($sponsor.DisplayName) <$($sponsor.UserPrincipalName)>"

# ---------------------------------------------------------------------------
# ADAPTER — agent identity blueprint / identity calls.
# VERIFY against Microsoft Learn ("Agent identity blueprint" Graph reference)
# before first production use; preview surfaces change. Functions return
# objects with at least: Id, DisplayName, and (for the identity) AppId /
# client id where the surface exposes it.
# ---------------------------------------------------------------------------
function Get-AgentBlueprint {
    param([string] $Name)
    # TODO(verify): endpoint/shape for listing agent identity blueprints.
    # Intended: GET /beta/agentIdentityBlueprints?$filter=displayName eq '<Name>'
    $uri = "https://graph.microsoft.com/beta/agentIdentityBlueprints?`$filter=displayName eq '$([uri]::EscapeDataString($Name))'"
    $resp = Invoke-MgGraphRequest -Method GET -Uri $uri
    return $resp.value | Select-Object -First 1
}

function New-AgentBlueprint {
    param([string] $Name)
    # TODO(verify): POST body for blueprint creation.
    $body = @{ displayName = $Name } | ConvertTo-Json
    return Invoke-MgGraphRequest -Method POST -Uri 'https://graph.microsoft.com/beta/agentIdentityBlueprints' -Body $body -ContentType 'application/json'
}

function Get-AgentIdentity {
    param([string] $BlueprintId, [string] $Name)
    # TODO(verify): identities are expected to hang off the blueprint.
    $uri = "https://graph.microsoft.com/beta/agentIdentityBlueprints/$BlueprintId/agentIdentities?`$filter=displayName eq '$([uri]::EscapeDataString($Name))'"
    $resp = Invoke-MgGraphRequest -Method GET -Uri $uri
    return $resp.value | Select-Object -First 1
}

function New-AgentIdentity {
    param([string] $BlueprintId, [string] $Name, [string] $SponsorId)
    # TODO(verify): POST body, incl. how the sponsor is attached (property vs
    # relationship endpoint). If the surface uses a relationship, POST to
    # .../agentIdentities/{id}/sponsors/$ref instead and delete `sponsors`.
    $body = @{
        displayName = $Name
        sponsors    = @(@{ '@odata.id' = "https://graph.microsoft.com/v1.0/users/$SponsorId" })
    } | ConvertTo-Json -Depth 4
    return Invoke-MgGraphRequest -Method POST -Uri "https://graph.microsoft.com/beta/agentIdentityBlueprints/$BlueprintId/agentIdentities" -Body $body -ContentType 'application/json'
}
# ---------------------------------------------------------------------------
# End adapter
# ---------------------------------------------------------------------------

# Blueprint: get or create (idempotent)
$blueprint = Get-AgentBlueprint -Name $DisplayName
if (-not $blueprint) {
    if ($PSCmdlet.ShouldProcess($DisplayName, 'Create agent identity blueprint')) {
        $blueprint = New-AgentBlueprint -Name $DisplayName
        Write-Host "Created blueprint: $($blueprint.id)"
    }
} else {
    Write-Host "Reusing existing blueprint: $($blueprint.id)"
}

# Agent identity: get or create (idempotent)
$identity = Get-AgentIdentity -BlueprintId $blueprint.id -Name $DisplayName
if (-not $identity) {
    if ($PSCmdlet.ShouldProcess($DisplayName, 'Create agent identity + assign sponsor')) {
        $identity = New-AgentIdentity -BlueprintId $blueprint.id -Name $DisplayName -SponsorId $sponsor.Id
        Write-Host "Created agent identity: $($identity.id)"
    }
} else {
    Write-Host "Reusing existing agent identity: $($identity.id)"
}

# ---------------------------------------------------------------------------
# Output — this is what goes into the agent's environment / secret store.
# ---------------------------------------------------------------------------
[pscustomobject]@{
    BlueprintObjectId = $blueprint.id
    AgentObjectId     = $identity.id
    AgentClientId     = $identity.appId   # property name per adapter; verify
    SponsorUpn        = $sponsor.UserPrincipalName
    NextSteps         = 'Create a client secret or certificate for the agent identity (portal or Graph), store it in Key Vault, and set AGENT365_CLIENT_ID / AGENT365_CLIENT_SECRET / AGENT365_TENANT_ID for the agent. Then complete runbook steps 4-8.'
} | Format-List
