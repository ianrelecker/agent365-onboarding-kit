#Requires -Version 7.0
<#
.SYNOPSIS
    Register a third-party agent for Microsoft Agent 365 governance.

.DESCRIPTION
    Creates or reuses an Entra agent identity blueprint, its blueprint principal,
    and an agent identity with a human sponsor. Idempotent, and it stops on
    -WhatIf before any write.

    Credentials are not created unless you ask. Production agents should use the
    user-assigned managed identity from infra/entra-agent-blueprint.bicep as a
    federated credential on the blueprint. -IssueDevelopmentSecret is a local
    development shortcut and is refused together with a managed identity.

    Graph contracts this script calls (v1.0, verified against Microsoft Learn):
      POST/GET /applications/microsoft.graph.agentIdentityBlueprint
      POST/GET /servicePrincipals/microsoft.graph.agentIdentityBlueprintPrincipal
      POST/GET /servicePrincipals/microsoft.graph.agentIdentity
    A blueprint holds the credential. The agent identity does not. Token
    acquisition is the two-step fmi_path exchange in src/sample_agent/identity.py,
    not a single client-credentials call.

.PARAMETER DisplayName
    Display name used for the blueprint and, unless -IdentityDisplayName is set,
    the agent identity.

.PARAMETER IdentityDisplayName
    Display name of the agent identity. Defaults to -DisplayName. Use a distinct
    value to add another identity under an existing blueprint of the same name.

.PARAMETER SponsorUpn
    UPN of the human sponsor. Required on both the blueprint and the identity.

.PARAMETER ManagedIdentityPrincipalId
    Object id of the user-assigned managed identity that will federate to the
    blueprint. Pair with -ManagedIdentityClientId.

.PARAMETER ManagedIdentityClientId
    Client id of that managed identity. Stored in the output so the agent host
    can request an IMDS token. The federated credential's subject is the
    principal id, not this client id.

.PARAMETER IssueDevelopmentSecret
    Add a short-lived client secret to the blueprint and print it once.
    Development tenants only. Do not use this for a production agent.

.EXAMPLE
    ./Register-Agent365.ps1 -DisplayName "Contoso Support Agent" -SponsorUpn "jane@contoso.com"

.EXAMPLE
    ./Register-Agent365.ps1 -DisplayName "Contoso Support Agent" -SponsorUpn "jane@contoso.com" -WhatIf
#>
[CmdletBinding(SupportsShouldProcess)]
param(
    [Parameter(Mandatory)]
    [ValidateLength(1, 120)]
    [string] $DisplayName,

    [ValidateLength(1, 120)]
    [string] $IdentityDisplayName,

    [Parameter(Mandatory)]
    [string] $SponsorUpn,

    [string] $ManagedIdentityPrincipalId,

    [string] $ManagedIdentityClientId,

    [switch] $IssueDevelopmentSecret,

    [ValidateRange(1, 30)]
    [int] $DevelopmentSecretLifetimeDays = 7
)

$ErrorActionPreference = 'Stop'

function ConvertTo-ODataStringLiteral {
    param([Parameter(Mandatory)][string] $Value)
    if ($Value -match '[\u0000-\u001F]') {
        throw 'Value contains a control character and cannot be used in a Graph filter.'
    }
    return $Value.Replace("'", "''")
}

function ConvertTo-ODataFilterEquals {
    param(
        [Parameter(Mandatory)][string] $Property,
        [Parameter(Mandatory)][string] $Value
    )
    if ($Property -notmatch '^[A-Za-z][A-Za-z0-9.]*$') {
        throw "Refusing to build a filter for property '$Property'."
    }
    $literal = ConvertTo-ODataStringLiteral $Value
    return [uri]::EscapeDataString("$Property eq '$literal'")
}

function Test-GuidValue {
    param([Parameter(Mandatory)][string] $Value, [Parameter(Mandatory)][string] $Name)
    if ($Value -notmatch '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$') {
        throw "$Name must be a GUID."
    }
}

function Invoke-AgentGraph {
    param(
        [Parameter(Mandatory)][string] $Method,
        [Parameter(Mandatory)][string] $Uri,
        [string] $Body
    )
    $params = @{
        Method  = $Method
        Uri     = $Uri
        Headers = @{ 'OData-Version' = '4.0' }
    }
    if ($PSBoundParameters.ContainsKey('Body')) {
        $params.Body = $Body
        $params.ContentType = 'application/json'
    }
    try {
        return Invoke-MgGraphRequest @params
    }
    catch {
        $detail = $_.ErrorDetails.Message
        if (-not $detail) { $detail = $_.Exception.Message }
        if ($detail -and $detail.Length -gt 600) { $detail = $detail.Substring(0, 600) }
        $status = $null
        $response = $_.Exception.Response
        if ($response -and $response.StatusCode) {
            try { $status = [int]$response.StatusCode } catch { $status = $null }
        }
        $kind = if ($status) { "HTTP $status" } else { $_.Exception.Message }
        throw "Graph $Method failed ($kind): $detail"
    }
}

function Test-MissingBlueprintPath {
    param([Parameter(Mandatory)][string] $Message)
    return $Message -match 'HTTP 404|Request_ResourceNotFound|ResourceNotFound'
}

function Get-BlueprintCollection {
    param(
        [Parameter(Mandatory)][string] $TypedUri,
        [Parameter(Mandatory)][string] $InheritedUri
    )
    try {
        return Get-GraphPaged -Uri $TypedUri
    }
    catch {
        if (-not (Test-MissingBlueprintPath $_.Exception.Message)) { throw }
        Write-Warning 'Typed blueprint path was not found. Reading the inherited application path instead.'
        return Get-GraphPaged -Uri $InheritedUri
    }
}

function Invoke-BlueprintWrite {
    param(
        [Parameter(Mandatory)][string] $TypedUri,
        [Parameter(Mandatory)][string] $InheritedUri,
        [Parameter(Mandatory)][string] $Body
    )
    try {
        return Invoke-AgentGraph -Method POST -Uri $TypedUri -Body $Body
    }
    catch {
        # A 404 means the typed path does not exist, so the write did not happen.
        if (-not (Test-MissingBlueprintPath $_.Exception.Message)) { throw }
        Write-Warning 'Typed blueprint path was not found. Writing the inherited application path instead.'
        return Invoke-AgentGraph -Method POST -Uri $InheritedUri -Body $Body
    }
}

function Get-GraphPaged {
    param([Parameter(Mandatory)][string] $Uri)
    $items = [System.Collections.Generic.List[object]]::new()
    $next = $Uri
    for ($page = 0; $next; $page++) {
        if ($page -ge 500) { throw "Stopped paging Graph after 500 pages." }
        $response = Invoke-AgentGraph -Method GET -Uri $next
        foreach ($item in @($response.value)) {
            if ($null -ne $item) { $items.Add($item) }
        }
        $next = $response.'@odata.nextLink'
        if (-not $next -and $response -is [System.Collections.IDictionary]) {
            $next = $response['@odata.nextLink']
        }
    }
    return @($items.ToArray())
}

function Get-ExactMatches {
    param($Items, [string] $Property, [string] $Expected)
    $matched = [System.Collections.Generic.List[object]]::new()
    foreach ($item in @($Items)) {
        if ($null -ne $item -and [string]$item.$Property -eq [string]$Expected) {
            $matched.Add($item)
        }
    }
    return ,$matched.ToArray()
}

function Assert-SingleMatch {
    param($Matches, [string] $Label, [string] $Name)
    $list = [System.Collections.Generic.List[object]]::new()
    foreach ($item in @($Matches)) {
        if ($null -ne $item) { $list.Add($item) }
    }
    if ($list.Count -gt 1) {
        throw "Multiple $Label objects are named '$Name'. Refusing to guess."
    }
    if ($list.Count -eq 1) { return $list[0] }
    return $null
}

if ($DisplayName -ne $DisplayName.Trim() -or $DisplayName -match '[\u0000-\u001F]') {
    throw 'DisplayName must not have surrounding whitespace or control characters.'
}
if (-not $IdentityDisplayName) { $IdentityDisplayName = $DisplayName }
if ($IdentityDisplayName -ne $IdentityDisplayName.Trim() -or $IdentityDisplayName -match '[\u0000-\u001F]') {
    throw 'IdentityDisplayName must not have surrounding whitespace or control characters.'
}
if ($SponsorUpn -notmatch '^[^@\s]+@[^@\s]+\.[^@\s]+$') {
    throw 'SponsorUpn must be a user principal name.'
}
$wantsFederated = [bool]($ManagedIdentityPrincipalId -or $ManagedIdentityClientId)
if ($wantsFederated -and ((-not $ManagedIdentityPrincipalId) -or (-not $ManagedIdentityClientId))) {
    throw 'Set both -ManagedIdentityPrincipalId and -ManagedIdentityClientId, or neither.'
}
if ($wantsFederated) {
    Test-GuidValue $ManagedIdentityPrincipalId 'ManagedIdentityPrincipalId'
    Test-GuidValue $ManagedIdentityClientId 'ManagedIdentityClientId'
}
if ($IssueDevelopmentSecret -and $wantsFederated) {
    throw 'Choose one credential. A federated managed identity is the production credential. -IssueDevelopmentSecret is only for a development tenant.'
}

if (-not (Get-Module -ListAvailable -Name Microsoft.Graph.Authentication)) {
    throw 'Install the Microsoft Graph PowerShell SDK: Install-Module Microsoft.Graph -Scope CurrentUser'
}
Import-Module Microsoft.Graph.Authentication

$scopes = @(
    'User.Read.All'
    'AgentIdentityBlueprint.Read.All'
    'AgentIdentityBlueprint.Create'
    'AgentIdentityBlueprintPrincipal.Create'
    'AgentIdentity.Read.All'
    'AgentIdentity.Create.All'
)
if ($IssueDevelopmentSecret -or $wantsFederated) {
    $scopes += 'AgentIdentityBlueprint.AddRemoveCreds.All'
}

$context = Get-MgContext
if (-not $context) {
    Connect-MgGraph -Scopes $scopes -NoWelcome | Out-Null
    $context = Get-MgContext
}
if (-not $context.TenantId) {
    throw 'Connect-MgGraph did not return a tenant id.'
}
Test-GuidValue $context.TenantId 'Connected tenant id'
$have = @($context.Scopes)
$missing = @($scopes | Where-Object { $have -notcontains $_ })
if ($missing.Count) {
    $joined = $missing -join ', '
    throw "This Graph connection is missing scopes: $joined. Reconnect with Connect-MgGraph -Scopes '$($scopes -join "','")'."
}

$sponsorUri = 'https://graph.microsoft.com/v1.0/users/{0}?$select=id,displayName,userPrincipalName,accountEnabled' -f [uri]::EscapeDataString($SponsorUpn)
$sponsor = Invoke-AgentGraph -Method GET -Uri $sponsorUri
if (-not $sponsor.id) { throw "Sponsor '$SponsorUpn' did not resolve to a user." }
if ($sponsor.accountEnabled -eq $false) { throw "Sponsor '$SponsorUpn' is disabled. Choose an active user." }
Write-Host "Sponsor: $($sponsor.displayName) <$($sponsor.userPrincipalName)>"

$blueprintFilter = ConvertTo-ODataFilterEquals -Property 'displayName' -Value $DisplayName
$blueprintUri = "https://graph.microsoft.com/v1.0/applications/microsoft.graph.agentIdentityBlueprint?`$select=id,appId,displayName&`$top=100&`$filter=$blueprintFilter"
try {
    $blueprintRows = Get-GraphPaged -Uri $blueprintUri
}
catch {
    Write-Warning "Server-side blueprint filter failed. Paging the blueprint collection instead. $($_.Exception.Message)"
    $blueprintRows = Get-GraphPaged -Uri 'https://graph.microsoft.com/v1.0/applications/microsoft.graph.agentIdentityBlueprint?$select=id,appId,displayName&$top=100'
}
$blueprint = Assert-SingleMatch (Get-ExactMatches $blueprintRows 'displayName' $DisplayName) 'blueprint' $DisplayName
if (-not $blueprint) {
    if (-not $PSCmdlet.ShouldProcess($DisplayName, 'Create agent identity blueprint')) {
        Write-Host "WhatIf: would create blueprint '$DisplayName', its principal, and agent identity '$IdentityDisplayName'."
        exit 0
    }
    $body = @{
        displayName            = $DisplayName
        'sponsors@odata.bind' = @("https://graph.microsoft.com/v1.0/users/$($sponsor.id)")
    } | ConvertTo-Json -Depth 6
    $blueprint = Invoke-AgentGraph -Method POST -Uri 'https://graph.microsoft.com/v1.0/applications/microsoft.graph.agentIdentityBlueprint' -Body $body
    Write-Host "Created blueprint: $($blueprint.id)"
}
else {
    Write-Host "Reusing blueprint: $($blueprint.id)"
}
if (-not $blueprint.appId -or -not $blueprint.id) {
    throw 'Blueprint response did not include id and appId.'
}
Test-GuidValue $blueprint.appId 'Blueprint appId'

$principalFilter = ConvertTo-ODataFilterEquals -Property 'appId' -Value $blueprint.appId
$principalUri = "https://graph.microsoft.com/v1.0/servicePrincipals/microsoft.graph.agentIdentityBlueprintPrincipal?`$select=id,appId,displayName&`$top=100&`$filter=$principalFilter"
try {
    $principalRows = Get-GraphPaged -Uri $principalUri
}
catch {
    Write-Warning "Server-side principal filter failed. Paging the principal collection instead. $($_.Exception.Message)"
    $principalRows = Get-GraphPaged -Uri 'https://graph.microsoft.com/v1.0/servicePrincipals/microsoft.graph.agentIdentityBlueprintPrincipal?$select=id,appId,displayName&$top=100'
}
$principal = Assert-SingleMatch (Get-ExactMatches $principalRows 'appId' $blueprint.appId) 'blueprint principal' $blueprint.appId
if (-not $principal) {
    if (-not $PSCmdlet.ShouldProcess($blueprint.appId, 'Create agent identity blueprint principal')) {
        Write-Host 'WhatIf: would create the blueprint principal and the agent identity.'
        exit 0
    }
    $principalBody = @{ appId = $blueprint.appId } | ConvertTo-Json
    try {
        $principal = Invoke-AgentGraph -Method POST -Uri 'https://graph.microsoft.com/v1.0/servicePrincipals/microsoft.graph.agentIdentityBlueprintPrincipal' -Body $principalBody
        Write-Host "Created blueprint principal: $($principal.id)"
    }
    catch {
        throw "Could not create or read the blueprint principal for appId $($blueprint.appId). $_"
    }
}
else {
    Write-Host "Reusing blueprint principal: $($principal.id)"
}
if (-not $principal.id) { throw 'Blueprint principal response did not include an object id.' }

$identityFilter = ConvertTo-ODataFilterEquals -Property 'displayName' -Value $IdentityDisplayName
$identityUri = "https://graph.microsoft.com/v1.0/servicePrincipals/microsoft.graph.agentIdentity?`$select=id,appId,displayName,agentIdentityBlueprintId,accountEnabled&`$top=100&`$filter=$identityFilter"
try {
    $identityRows = Get-GraphPaged -Uri $identityUri
}
catch {
    Write-Warning "Server-side identity filter failed. Paging the agent identity collection instead. $($_.Exception.Message)"
    $identityRows = Get-GraphPaged -Uri 'https://graph.microsoft.com/v1.0/servicePrincipals/microsoft.graph.agentIdentity?$select=id,appId,displayName,agentIdentityBlueprintId,accountEnabled&$top=100'
}
$identityMatches = @($identityRows | Where-Object {
        $_.displayName -eq $IdentityDisplayName -and $_.agentIdentityBlueprintId -eq $blueprint.appId
    })
$objectIdMatches = @($identityRows | Where-Object {
        $_.displayName -eq $IdentityDisplayName -and $_.agentIdentityBlueprintId -eq $blueprint.id
    })
if ($objectIdMatches.Count -and -not $identityMatches.Count) {
    throw "An agent identity named '$IdentityDisplayName' points agentIdentityBlueprintId at the blueprint object id. The documented property is the blueprint appId. Refusing to create a second identity."
}
$identity = Assert-SingleMatch $identityMatches 'agent identity' $IdentityDisplayName
if (-not $identity) {
    if (-not $PSCmdlet.ShouldProcess($IdentityDisplayName, 'Create agent identity')) {
        Write-Host "WhatIf: would create agent identity '$IdentityDisplayName' under blueprint $($blueprint.appId)."
        exit 0
    }
    $identityBody = @{
        displayName               = $IdentityDisplayName
        agentIdentityBlueprintId  = $blueprint.appId
        'sponsors@odata.bind'     = @("https://graph.microsoft.com/v1.0/users/$($sponsor.id)")
    } | ConvertTo-Json -Depth 6
    $created = Invoke-AgentGraph -Method POST -Uri 'https://graph.microsoft.com/v1.0/servicePrincipals/microsoft.graph.agentIdentity' -Body $identityBody
    if (-not $created.id) { throw 'Agent identity creation did not return an object id.' }
    $identity = Invoke-AgentGraph -Method GET -Uri ("https://graph.microsoft.com/v1.0/servicePrincipals/{0}?`$select=id,appId,displayName,accountEnabled,agentIdentityBlueprintId" -f $created.id)
    Write-Host "Created agent identity: $($identity.id)"
}
else {
    Write-Host "Reusing agent identity: $($identity.id)"
}
if ($identity.accountEnabled -eq $false) {
    throw "Agent identity $($identity.id) is disabled. Re-enable it instead of creating another."
}
if (-not $identity.appId) { throw 'Agent identity response did not include an appId. Token exchange needs that appId as fmi_path.' }
Test-GuidValue $identity.appId 'Agent identity appId'

function Get-SponsorState {
    param([string] $ListUri)
    try {
        $current = Get-GraphPaged -Uri $ListUri
    }
    catch {
        return @{ Known = $false; Present = $false; Error = $_.Exception.Message }
    }
    $present = $false
    foreach ($item in @($current)) {
        if ($null -ne $item -and [string]$item.id -eq [string]$sponsor.id) { $present = $true }
    }
    return @{ Known = $true; Present = $present; Error = '' }
}

function Add-SponsorReference {
    param([string] $AddUri, [string] $Label)
    $ref = @{ '@odata.id' = "https://graph.microsoft.com/v1.0/users/$($sponsor.id)" } | ConvertTo-Json
    try {
        Invoke-AgentGraph -Method POST -Uri $AddUri -Body $ref | Out-Null
        Write-Host "Added sponsor on $Label."
    }
    catch {
        throw "Could not add the sponsor on existing $Label. Delegated tokens often cannot update sponsors after creation. Add $($sponsor.userPrincipalName) in the Entra admin center, then re-run. $_"
    }
}

$blueprintId = $blueprint.id
$identityId = $identity.id
$sponsorChecks = @(
    @{
        Label = 'blueprint'
        List  = "https://graph.microsoft.com/v1.0/applications/$blueprintId/microsoft.graph.agentIdentityBlueprint/sponsors?`$select=id"
        Add   = "https://graph.microsoft.com/v1.0/applications/$blueprintId/microsoft.graph.agentIdentityBlueprint/sponsors/`$ref"
    }
    @{
        Label = 'agent identity'
        List  = "https://graph.microsoft.com/v1.0/servicePrincipals/$identityId/microsoft.graph.agentIdentity/sponsors?`$select=id"
        Add   = "https://graph.microsoft.com/v1.0/servicePrincipals/$identityId/microsoft.graph.agentIdentity/sponsors/`$ref"
    }
)
foreach ($check in $sponsorChecks) {
    $state = Get-SponsorState -ListUri $check.List
    if ($state.Present) {
        Write-Host "Sponsor already on $($check.Label)."
        continue
    }
    if (-not $state.Known) {
        throw "Could not read sponsors on $($check.Label). $($state.Error)"
    }
    if ($PSCmdlet.ShouldProcess($SponsorUpn, "Add sponsor on $($check.Label)")) {
        Add-SponsorReference -AddUri $check.Add -Label $check.Label
    }
    else {
        Write-Host "Sponsor is not on $($check.Label). Re-run without -WhatIf to add $($sponsor.userPrincipalName)."
    }
}

$credentialNote = 'No credential was configured. Deploy infra/entra-agent-blueprint.bicep and re-run with -ManagedIdentityPrincipalId and -ManagedIdentityClientId.'
if ($wantsFederated) {
    if (-not $PSCmdlet.ShouldProcess($ManagedIdentityPrincipalId, 'Ensure federated credential on blueprint')) {
        $credentialNote = 'WhatIf: would ensure federated credential a365-managed-identity on the blueprint.'
    }
    else {
        $typedFic = "https://graph.microsoft.com/v1.0/applications/$blueprintId/microsoft.graph.agentIdentityBlueprint/federatedIdentityCredentials"
        $inheritedFic = "https://graph.microsoft.com/v1.0/applications/$blueprintId/federatedIdentityCredentials"
        $existing = @(Get-BlueprintCollection -TypedUri $typedFic -InheritedUri $inheritedFic | Where-Object { $_.name -eq 'a365-managed-identity' })
        $expectedIssuer = "https://login.microsoftonline.com/$($context.TenantId)/v2.0"
        if ($existing.Count -gt 1) { throw "Multiple federated credentials are named a365-managed-identity on blueprint $blueprintId." }
        if ($existing.Count -eq 1) {
            $current = $existing[0]
            $audiences = @($current.audiences)
            $audienceOk = $audiences -contains 'api://AzureADTokenExchange'
            if (([string]$current.subject -ne $ManagedIdentityPrincipalId) -or ([string]$current.issuer -ne $expectedIssuer) -or (-not $audienceOk)) {
                throw 'Federated credential a365-managed-identity already exists and points at a different subject, issuer, or audience. Refusing to replace it.'
            }
            Write-Host 'Reusing federated credential a365-managed-identity.'
        }
        else {
            $ficBody = @{
                name      = 'a365-managed-identity'
                issuer    = $expectedIssuer
                subject   = $ManagedIdentityPrincipalId
                audiences = @('api://AzureADTokenExchange')
            } | ConvertTo-Json -Depth 5
            Invoke-BlueprintWrite -TypedUri $typedFic -InheritedUri $inheritedFic -Body $ficBody | Out-Null
            Write-Host 'Created federated credential a365-managed-identity.'
        }
        $credentialNote = 'Federated credential a365-managed-identity is on the blueprint. Run the agent as that managed identity and set AGENT365_MANAGED_IDENTITY_CLIENT_ID.'
    }
}
elseif ($IssueDevelopmentSecret) {
    if ($PSCmdlet.ShouldProcess($blueprint.appId, 'Add development-only client secret')) {
        $end = [DateTime]::UtcNow.AddDays($DevelopmentSecretLifetimeDays).ToString('yyyy-MM-ddTHH:mm:ssZ')
        $secretBody = @{
            passwordCredential = @{
                displayName = 'agent365-development-only'
                endDateTime = $end
            }
        } | ConvertTo-Json -Depth 5
        $typedSecret = "https://graph.microsoft.com/v1.0/applications/$blueprintId/microsoft.graph.agentIdentityBlueprint/addPassword"
        $inheritedSecret = "https://graph.microsoft.com/v1.0/applications/$blueprintId/addPassword"
        $secret = Invoke-BlueprintWrite -TypedUri $typedSecret -InheritedUri $inheritedSecret -Body $secretBody
        if (-not $secret.secretText) { throw 'Graph did not return the new client secret.' }
        Write-Warning "Development client secret. It will not be shown again. Do not use a client secret for a production blueprint. Expires $end."
        Write-Host "AGENT365_ALLOW_CLIENT_SECRET=true"
        Write-Host "AGENT365_CLIENT_SECRET=$($secret.secretText)"
        $credentialNote = 'A development client secret was printed once above and is intentionally absent from this object.'
    }
}

[pscustomobject]@{
    TenantId                    = $context.TenantId
    BlueprintObjectId           = $blueprint.id
    BlueprintClientId           = $blueprint.appId
    BlueprintPrincipalObjectId  = $principal.id
    AgentObjectId               = $identity.id
    AgentClientId               = $identity.appId
    SponsorUpn                  = $sponsor.userPrincipalName
    SponsorObjectId             = $sponsor.id
    ManagedIdentityClientId     = $ManagedIdentityClientId
    Credential                  = $credentialNote
    NextSteps                   = @(
        'Set AGENT365_TENANT_ID, AGENT365_BLUEPRINT_CLIENT_ID (BlueprintClientId), AGENT365_AGENT_CLIENT_ID (AgentClientId), and AGENT365_CALLER_OBJECT_ID (SponsorObjectId for an autonomous run).'
        'Do not point a single client-credentials app at AgentClientId. The blueprint credential exchanges a token for the agent identity.'
        'Register the agent in the Agent 365 registry (Microsoft 365 Agents SDK or a365 setup all). Inventory without that registration is not governance.'
        'Run the sample once and confirm the export result says the spans were sent. HTTP 200 with rejected results is not ingestion.'
        'Put Conditional Access in report-only, targeted at BlueprintPrincipalObjectId via includeAgentIdServicePrincipals. See policies/conditional-access-for-agents.md.'
    ) -join ' '
} | Format-List
