import base64
import json
from datetime import datetime, timedelta, timezone

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from identity import (
    TOKEN_HOST,
    AgentIdentity,
    CertificateCredential,
    ClientSecret,
    IdentityError,
    ManagedIdentityCredential,
    UrllibTransport,
    _ErrorBody,
    _raise_token_failure,
    acquire_token,
    build_client_assertion,
    load_env_file,
    load_identity,
)

TENANT = "11111111-1111-1111-1111-111111111111"
BLUEPRINT = "22222222-2222-2222-2222-222222222222"
AGENT = "33333333-3333-3333-3333-333333333333"
MANAGED = "44444444-4444-4444-4444-444444444444"


def _production(**overrides):
    env = {
        "AGENT365_TENANT_ID": TENANT,
        "AGENT365_BLUEPRINT_CLIENT_ID": BLUEPRINT,
        "AGENT365_AGENT_CLIENT_ID": AGENT,
        "AGENT365_MANAGED_IDENTITY_CLIENT_ID": MANAGED,
    }
    env.update(overrides)
    return env


class FakeTransport:
    def __init__(self, responses=None, error=None):
        self.responses = list(responses or [])
        self.error = error
        self.calls = []

    def post_form(self, url, form, timeout):
        self.calls.append((url, dict(form), timeout))
        if self.error:
            raise self.error
        return self.responses.pop(0)

    def get_imds(self, url, timeout):
        self.calls.append(("imds", url, timeout))
        return {"access_token": "mi-token"}


def test_demo_mode_is_only_when_identity_is_unset():
    identity = load_identity({})
    assert identity.demo_mode is True
    assert acquire_token(identity) == "DEMO-TOKEN-NOT-A-REAL-TOKEN"


def test_partial_configuration_does_not_fall_back_to_demo():
    with pytest.raises(IdentityError):
        load_identity({"AGENT365_TENANT_ID": TENANT})


def test_legacy_client_id_is_refused():
    with pytest.raises(IdentityError, match="AGENT365_BLUEPRINT_CLIENT_ID"):
        load_identity({"AGENT365_CLIENT_ID": AGENT, "AGENT365_TENANT_ID": TENANT})


def test_blueprint_and_agent_client_ids_must_differ():
    with pytest.raises(IdentityError, match="fmi_path"):
        load_identity(_production(AGENT365_AGENT_CLIENT_ID=BLUEPRINT))


def test_client_secret_is_refused_unless_opted_in():
    env = _production(AGENT365_MANAGED_IDENTITY_CLIENT_ID="")
    env["AGENT365_CLIENT_SECRET"] = "super-secret"
    with pytest.raises(IdentityError, match="ALLOW_CLIENT_SECRET"):
        load_identity(env)


def test_ambiguous_credentials_are_refused():
    env = _production()
    env["AGENT365_CLIENT_CERT_PATH"] = "/tmp/cert.pem"
    with pytest.raises(IdentityError, match="only one"):
        load_identity(env)


def test_secret_exchange_uses_fmi_path_and_does_not_echo_the_secret():
    env = _production(AGENT365_MANAGED_IDENTITY_CLIENT_ID="")
    env["AGENT365_CLIENT_SECRET"] = "super-secret"
    env["AGENT365_ALLOW_CLIENT_SECRET"] = "true"
    identity = load_identity(env)
    transport = FakeTransport(
        responses=[
            {"access_token": "exchange-token", "expires_in": 3600},
            {"error": "invalid_client", "error_description": "rejected super-secret"},
        ]
    )
    with pytest.raises(IdentityError) as caught:
        acquire_token(identity, scope="https://graph.microsoft.com/.default", transport=transport)
    message = str(caught.value)
    assert "super-secret" not in message
    assert "exchange-token" not in message
    first_url, first_form, _timeout = transport.calls[0]
    assert first_url == f"https://{TOKEN_HOST}/{TENANT}/oauth2/v2.0/token"
    assert first_form["client_id"] == BLUEPRINT
    assert first_form["fmi_path"] == AGENT
    assert first_form["client_secret"] == "super-secret"
    assert "client_assertion" not in first_form
    assert first_form["scope"] == "api://AzureADTokenExchange/.default"


def test_successful_exchange_caches_the_resource_token():
    identity = AgentIdentity(
        blueprint_client_id=BLUEPRINT,
        agent_client_id=AGENT,
        tenant_id=TENANT,
        demo_mode=False,
        credential=ClientSecret("super-secret"),
    )
    transport = FakeTransport(
        responses=[
            {"access_token": "exchange-token", "expires_in": 300},
            {"access_token": "resource-token", "expires_in": 3600},
        ]
    )
    assert acquire_token(identity, scope="scope/.default", transport=transport) == "resource-token"
    assert acquire_token(identity, scope="scope/.default", transport=transport) == "resource-token"
    assert len(transport.calls) == 2
    _url, second_form, _timeout = transport.calls[1]
    assert second_form["client_id"] == AGENT
    assert second_form["client_assertion"] == "exchange-token"
    assert second_form["scope"] == "scope/.default"
    assert "client_secret" not in second_form


def test_managed_identity_assertion_is_the_blueprint_credential():
    identity = load_identity(_production())
    transport = FakeTransport(
        responses=[
            {"access_token": "exchange-token", "expires_in": 300},
            {"access_token": "resource-token", "expires_in": 300},
        ]
    )
    assert acquire_token(identity, transport=transport) == "resource-token"
    assert transport.calls[0][0] == "imds"
    assert "client_id=" + MANAGED in transport.calls[0][1]
    assert "api-version=2018-02-01" in transport.calls[0][1]
    form = transport.calls[1][1]
    assert form["client_assertion"] == "mi-token"
    assert "client_secret" not in form


def test_http_error_body_is_redacted():
    identity = AgentIdentity(
        blueprint_client_id=BLUEPRINT,
        agent_client_id=AGENT,
        tenant_id=TENANT,
        demo_mode=False,
        credential=ClientSecret("super-secret"),
    )
    body = b'{"error":"invalid_client","error_description":"bad super-secret eyJaaa.bbbbbbbb.ccccccc"}'
    error = IdentityError("HTTP 400")
    error.__cause__ = _ErrorBody(body, 400)
    transport = FakeTransport(error=error)
    with pytest.raises(IdentityError) as caught:
        acquire_token(identity, transport=transport)
    text = str(caught.value)
    assert "super-secret" not in text
    assert "eyJaaa" not in text
    assert "HTTP 400" in text


def test_raise_token_failure_redacts_known_secrets():
    with pytest.raises(IdentityError) as caught:
        _raise_token_failure({"error_description": "nope SECRET"}, ["SECRET"], status=401)
    assert "SECRET" not in str(caught.value)


def test_token_client_refuses_unexpected_hosts():
    client = UrllibTransport()
    with pytest.raises(IdentityError, match="unexpected host"):
        client.post_form("https://evil.example/oauth2/v2.0/token", {}, 1)
    with pytest.raises(IdentityError, match="unexpected host"):
        client.get_imds("http://169.254.169.254/latest/meta-data", 1)


def _self_signed_pem(key=None, valid_from=None, valid_until=None) -> tuple[str, str]:
    key = key or rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "agent365-test")])
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(valid_from or (now - timedelta(minutes=1)))
        .not_valid_after(valid_until or (now + timedelta(days=1)))
        .sign(key, hashes.SHA256())
    )
    pem = (
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode()
        + cert.public_bytes(serialization.Encoding.PEM).decode()
    )
    return pem, cert.fingerprint(hashes.SHA1()).hex()


def test_certificate_assertion_identifies_the_blueprint(tmp_path):
    pem, thumbprint = _self_signed_pem()
    path = tmp_path / "blueprint.pem"
    path.write_text(pem)
    env = _production(AGENT365_MANAGED_IDENTITY_CLIENT_ID="")
    env["AGENT365_CLIENT_CERT_PATH"] = str(path)
    identity = load_identity(env)
    assert isinstance(identity.credential, CertificateCredential)
    assertion = build_client_assertion(identity.credential, BLUEPRINT, TENANT, now=1_700_000_000)
    header_b64, payload_b64, _signature = assertion.split(".")
    header = json.loads(_b64decode(header_b64))
    payload = json.loads(_b64decode(payload_b64))
    assert header["alg"] == "RS256"
    assert header["x5t"]
    assert payload["iss"] == BLUEPRINT
    assert payload["sub"] == BLUEPRINT
    assert payload["aud"] == f"https://{TOKEN_HOST}/{TENANT}/oauth2/v2.0/token"
    assert payload["nbf"] == 1_700_000_000 - 120
    assert payload["exp"] == payload["nbf"] + 600
    transport = FakeTransport(
        responses=[
            {"access_token": "exchange-token", "expires_in": 60},
            {"access_token": "resource-token", "expires_in": 60},
        ]
    )
    assert acquire_token(identity, transport=transport) == "resource-token"
    assert transport.calls[0][1]["client_assertion"].count(".") == 2
    assert "super-secret" not in repr(identity.credential)


def test_cached_token_is_not_kept_for_more_than_an_hour():
    identity = AgentIdentity(
        blueprint_client_id=BLUEPRINT,
        agent_client_id=AGENT,
        tenant_id=TENANT,
        demo_mode=False,
        credential=ClientSecret("super-secret"),
    )
    transport = FakeTransport(
        responses=[
            {"access_token": "exchange-token", "expires_in": 300},
            {"access_token": "resource-token", "expires_in": 100000},
            {"access_token": "exchange-token-2", "expires_in": 300},
            {"access_token": "resource-token-2", "expires_in": 300},
        ]
    )
    clock = {"now": 1000.0}
    assert acquire_token(identity, transport=transport, now=lambda: clock["now"]) == "resource-token"
    clock["now"] = 4599.0
    assert acquire_token(identity, transport=transport, now=lambda: clock["now"]) == "resource-token"
    clock["now"] = 4600.0
    assert acquire_token(identity, transport=transport, now=lambda: clock["now"]) == "resource-token-2"


def test_non_rsa_and_expired_certificates_are_refused(tmp_path):
    from cryptography.hazmat.primitives.asymmetric import ec

    ec_key = ec.generate_private_key(ec.SECP256R1())
    ec_pem, _thumbprint = _self_signed_pem(key=ec_key)
    ec_path = tmp_path / "ec.pem"
    ec_path.write_text(ec_pem)
    env = _production(AGENT365_MANAGED_IDENTITY_CLIENT_ID="")
    env["AGENT365_CLIENT_CERT_PATH"] = str(ec_path)
    with pytest.raises(IdentityError, match="RSA"):
        load_identity(env)

    now = datetime.now(timezone.utc)
    expired_pem, _thumbprint = _self_signed_pem(
        valid_from=now - timedelta(days=3),
        valid_until=now - timedelta(days=1),
    )
    expired_path = tmp_path / "expired.pem"
    expired_path.write_text(expired_pem)
    env["AGENT365_CLIENT_CERT_PATH"] = str(expired_path)
    with pytest.raises(IdentityError, match="expired"):
        load_identity(env)


def test_env_file_does_not_override_or_repeat_bad_lines(tmp_path):
    path = tmp_path / ".env"
    path.write_text("NOT A LINE\nEXISTING=from-file\n")
    env = {"EXISTING": "already"}
    with pytest.raises(IdentityError) as caught:
        load_env_file(path, env)
    assert "NOT A LINE" not in str(caught.value)
    assert env == {"EXISTING": "already"}


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
