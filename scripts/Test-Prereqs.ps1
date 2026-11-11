<#
.SYNOPSIS
    Prerequisite checker for Agent 365 onboarding of a third-party agent.

.DESCRIPTION
    Verifies what a script can verify (Graph connection, your roles) and prints
    a checklist for what it cannot (licensing, Defender for Endpoint coverage,
    sponsor choice). Run this before Register-Agent365.ps1.

    Exit code 0 = scriptable checks passed (checklist may still have manual
    items). Exit code 1 = a scriptable check failed.

.EXAMPLE
    ./Test-Prereqs.ps1
#>
[CmdletBinding()]
param()

$failures = @()

Write-Host "`n=== Scriptable checks ===" -ForegroundColor Cyan

# Graph connection ----------------------------------------------------------
try {
    $ctx = Get-MgContext -ErrorAction Stop
    if (-not $ctx) { throw "No Graph context. Run Connect-MgGraph first." }
    Write-Host "[OK]   Connected to Graph as $($ctx.Account) (tenant $($ctx.TenantId))" -ForegroundColor Green
    $needed = @('Organization.Read.All', 'Directory.Read.All')
    $missing = $needed | Where-Object { $ctx.Scopes -notcontains $_ }
    if ($missing) {
        Write-Host "[WARN] Connected without: $($missing -join ', ') — some checks/steps may fail. Reconnect with the scopes Register-Agent365.ps1 requests." -ForegroundColor Yellow
    }
}
catch {
    $failures += "Graph connection: $($_.Exception.Message)"
    Write-Host "[FAIL] Graph connection: $($_.Exception.Message)" -ForegroundColor Red
}

# Signed-in user's roles ----------------------------------------------------
# VERIFY: role display names for agent administration were still settling at
# GA. This checks the two that unambiguously suffice; absence of both does not
# prove you lack a sufficient agent-admin role in your tenant — check the
# Entra admin center (Roles and administrators) before concluding anything.
try {
    $me = Get-MgUser -UserId 'me' -ErrorAction Stop
    $roles = Get-MgUserMemberOf -UserId $me.Id -All -ErrorAction Stop |
        Where-Object { $_.AdditionalProperties.displayName } |
        ForEach-Object { $_.AdditionalProperties.displayName }
    $sufficient = @('Global Administrator', 'Agent ID Administrator', 'Agent ID Admin')
    $hit = $roles | Where-Object { $sufficient -contains $_ }
    if ($hit) {
        Write-Host "[OK]   Role(s) that suffice for blueprint/identity work: $($hit -join ', ')" -ForegroundColor Green
    } else {
        Write-Host "[WARN] None of ($($sufficient -join ', ')) found in your role memberships. You may hold an eligible role via PIM (not active) — activate it and re-run." -ForegroundColor Yellow
    }
}
catch {
    Write-Host "[WARN] Could not enumerate roles: $($_.Exception.Message)" -ForegroundColor Yellow
}

Write-Host "`n=== Manual checklist (a script cannot verify these) ===" -ForegroundColor Cyan
@(
    "Licensing: Agent 365 assigned to the sponsor and to users who will interact with / manage this agent (standalone `$15/user/mo or via M365 E7). Enterprise standalone requires an M365 E5 base — confirm tenant eligibility in the current licensing FAQ.",
    "Sponsor: a named, licensed, active human who will own this agent's alerts and access reviews.",
    "Defender for Endpoint: onboarded on every device where this agent will run locally (endpoint discovery depends on it).",
    "Secret store: a Key Vault (see infra/entra-agent-blueprint.bicep) or equivalent ready for the agent identity credential. Nothing secret goes in the repo.",
    "Change window: Conditional Access for agents goes in report-only first; schedule enforcement outside business-critical hours."
) | ForEach-Object { Write-Host "  [ ] $_" }

if ($failures.Count) {
    Write-Host "`nFAILED checks: $($failures.Count). Fix the [FAIL] items and re-run." -ForegroundColor Red
    exit 1
}
Write-Host "`nScriptable checks passed. Work the manual checklist before registering." -ForegroundColor Green
exit 0
