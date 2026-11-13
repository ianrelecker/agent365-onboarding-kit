#Requires -Version 7.0
<#
.SYNOPSIS
    Prerequisite checker for Agent 365 onboarding of a third-party agent.

.DESCRIPTION
    Verifies what a script can verify (PowerShell 7, Graph module, connection,
    delegated scopes, and whether your active roles are sufficient) and prints
    the checklist no script can verify. Run this before Register-Agent365.ps1.

    Exit 0 means the scriptable checks passed. The manual checklist can still
    block a rollout. Exit 1 means a scriptable check failed.

.EXAMPLE
    ./Test-Prereqs.ps1
#>
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$failures = [System.Collections.Generic.List[string]]::new()

Write-Host "`n=== Scriptable checks ===" -ForegroundColor Cyan

if ($PSVersionTable.PSVersion.Major -lt 7) {
    $failures.Add('PowerShell 7 or newer is required.')
    Write-Host "[FAIL] PowerShell $($PSVersionTable.PSVersion). Install PowerShell 7." -ForegroundColor Red
}
else {
    Write-Host "[OK]   PowerShell $($PSVersionTable.PSVersion)" -ForegroundColor Green
}

if (-not (Get-Module -ListAvailable -Name Microsoft.Graph.Authentication)) {
    $failures.Add('Microsoft.Graph.Authentication is not installed.')
    Write-Host '[FAIL] Microsoft Graph PowerShell SDK is not installed. Install-Module Microsoft.Graph -Scope CurrentUser' -ForegroundColor Red
}
else {
    Write-Host '[OK]   Microsoft.Graph.Authentication is installed' -ForegroundColor Green
    Import-Module Microsoft.Graph.Authentication
}

$needed = @(
    'User.Read.All'
    'AgentIdentityBlueprint.Read.All'
    'AgentIdentityBlueprint.Create'
    'AgentIdentityBlueprintPrincipal.Create'
    'AgentIdentity.Read.All'
    'AgentIdentity.Create.All'
)

if (Get-Command Get-MgContext -ErrorAction SilentlyContinue) {
    try {
        $ctx = Get-MgContext -ErrorAction Stop
        if (-not $ctx) {
            Write-Host '[WARN] Not connected to Graph yet. Register-Agent365.ps1 connects with the scopes it needs.' -ForegroundColor Yellow
            $ctx = $null
        }
        else {
            Write-Host "[OK]   Connected to Graph as $($ctx.Account) (tenant $($ctx.TenantId))" -ForegroundColor Green
            $missing = @($needed | Where-Object { @($ctx.Scopes) -notcontains $_ })
            if ($missing.Count) {
                Write-Host "[WARN] Connected without: $($missing -join ', ')." -ForegroundColor Yellow
                Write-Host '       Register-Agent365.ps1 will refuse to run until you reconnect with those scopes.' -ForegroundColor Yellow
            }
            else {
                Write-Host '[OK]   Delegated scopes for blueprint, principal, and identity creation are present' -ForegroundColor Green
            }
            if (@($ctx.Scopes) -contains 'Directory.ReadWrite.All') {
                Write-Host '[WARN] This connection still has Directory.ReadWrite.All. The registration script does not need it.' -ForegroundColor Yellow
            }
        }
    }
    catch {
        $failures.Add("Graph connection: $($_.Exception.Message)")
        Write-Host "[FAIL] Graph connection: $($_.Exception.Message)" -ForegroundColor Red
    }
}

Write-Host "`n=== Role check (active memberships only) ===" -ForegroundColor Cyan
if ((Get-Command Invoke-MgGraphRequest -ErrorAction SilentlyContinue) -and (Get-MgContext -ErrorAction SilentlyContinue)) {
    try {
        $response = Invoke-MgGraphRequest -Method GET -Uri 'https://graph.microsoft.com/v1.0/me/transitiveMemberOf/microsoft.graph.directoryRole?$select=displayName' -Headers @{ 'OData-Version' = '4.0' }
        $names = @($response.value | ForEach-Object { $_.displayName })
        $createRoles = @('Global Administrator', 'Agent ID Administrator', 'Agent ID Developer')
        $secretRoles = @('Global Administrator', 'Agent ID Administrator')
        $hit = @($names | Where-Object { $createRoles -contains $_ })
        if ($hit.Count) {
            Write-Host "[OK]   Active role(s) that can create blueprints: $($hit -join ', ')" -ForegroundColor Green
        }
        else {
            Write-Host "[WARN] None of ($($createRoles -join ', ')) are active. Eligible PIM roles do not count until you activate them." -ForegroundColor Yellow
        }
        $secretHit = @($names | Where-Object { $secretRoles -contains $_ })
        if (-not $secretHit.Count) {
            Write-Host '[WARN] A client secret on a blueprint needs Agent ID Administrator or Global Administrator. Federated credentials can be added by Agent ID Developer.' -ForegroundColor Yellow
        }
    }
    catch {
        Write-Host "[WARN] Could not enumerate roles: $($_.Exception.Message)" -ForegroundColor Yellow
    }
}

Write-Host "`n=== Manual checklist ===" -ForegroundColor Cyan
@(
    'Licensing: an Agent 365 or Microsoft 365 E7 license is assigned to at least one user. A SKU that is only present in the tenant does not start telemetry ingestion.'
    'Sponsor: a named, licensed, active human. The sample records that person''s Entra object id as user.id on every span.'
    'Credential: a user-assigned managed identity federated to the blueprint, or a certificate. A client secret is a development-tenant shortcut.'
    'Registry: the blueprint is registered with Agent 365 (Microsoft 365 Agents SDK or a365 setup all). An unregistered instance is rejected when it exports telemetry.'
    'Defender for Endpoint is deployed on devices where this agent runs locally. That is what surfaces the process. It does not govern it.'
    'Conditional Access for agents is report-only first, and it targets agent identities (includeAgentIdServicePrincipals), not includeApplications.'
    'Key Vault from infra/entra-agent-blueprint.bicep has purge protection, RBAC, and a closed network. Turn on private DNS before the agent host depends on it.'
) | ForEach-Object { Write-Host "  [ ] $_" }

if ($failures.Count) {
    Write-Host "`nFAILED checks: $($failures.Count)." -ForegroundColor Red
    exit 1
}
Write-Host "`nScriptable checks passed. Work the manual checklist before registering." -ForegroundColor Green
exit 0
