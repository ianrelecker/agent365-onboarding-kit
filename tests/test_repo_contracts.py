from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_registration_script_uses_the_v1_agent_identity_contracts():
    text = (ROOT / "scripts" / "Register-Agent365.ps1").read_text()
    assert "/applications/microsoft.graph.agentIdentityBlueprint" in text
    assert "/servicePrincipals/microsoft.graph.agentIdentityBlueprintPrincipal" in text
    assert "/servicePrincipals/microsoft.graph.agentIdentity" in text
    assert "sponsors@odata.bind" in text
    assert "agentIdentityBlueprintId" in text
    assert "Directory.ReadWrite.All" not in text
    assert "/beta/agentIdentityBlueprints" not in text
    assert "ConvertTo-ODataStringLiteral" in text
    assert "Replace(\"'\", \"''\")" in text
    assert "-IssueDevelopmentSecret" in text
    assert "api://AzureADTokenExchange" in text
    assert "/microsoft.graph.agentIdentityBlueprint/federatedIdentityCredentials" in text
    assert "/microsoft.graph.agentIdentityBlueprint/addPassword" in text
    assert "https://graph.microsoft.com/v1.0/users/" in text


def test_key_vault_template_fails_closed():
    text = (ROOT / "infra" / "entra-agent-blueprint.bicep").read_text()
    assert "enablePurgeProtection: true" in text
    assert "enableRbacAuthorization: true" in text
    assert "defaultAction: 'Deny'" in text
    assert "bypass: 'None'" in text
    assert "publicNetworkAccess: publicNetworkAccess" in text
    assert "Microsoft.ManagedIdentity/userAssignedIdentities" in text
    assert "4633458b-17de-408a-b874-0445c86b69e6" in text


def test_conditional_access_template_targets_agent_identities():
    text = (ROOT / "policies" / "conditional-access-for-agents.md").read_text()
    assert "includeAgentIdServicePrincipals" in text
    assert "enabledForReportingButNotEnforced" in text
    assert "<agent application / client id>" not in text
    assert "includeApplications" in text
