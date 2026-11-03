"""Exercise discovery, PKCE, nonce, JWKS/signature and handoff over real HTTP.

The disposable IdP uses Pocket ID's endpoint layout. No real account is used.
"""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from app.db import models
from app.services import oauth_service


def test_full_discovered_oidc_exchange(client, db_session):
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key()))
    jwk.update(kid="test-key", alg="RS256", use="sig")
    observed = {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def respond(self, data):
            body = json.dumps(data).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/.well-known/openid-configuration":
                self.respond(
                    {
                        "issuer": issuer,
                        "authorization_endpoint": issuer + "/authorize",
                        "token_endpoint": issuer + "/api/oidc/token",
                        "userinfo_endpoint": issuer + "/api/oidc/userinfo",
                        "jwks_uri": issuer + "/.well-known/jwks.json",
                        "id_token_signing_alg_values_supported": ["RS256"],
                    }
                )
            elif self.path == "/.well-known/jwks.json":
                self.respond({"keys": [jwk]})
            elif self.path == "/api/oidc/userinfo":
                assert self.headers["Authorization"] == "Bearer idp-access"
                self.respond(
                    {
                        "sub": "pocket-user",
                        "email": "sso@example.com",
                        "name": "SSO User",
                        "groups": [],
                    }
                )
            else:
                self.send_error(404)

        def do_POST(self):
            assert self.path == "/api/oidc/token"
            fields = parse_qs(self.rfile.read(int(self.headers["Content-Length"])).decode())
            assert fields["code"] == ["authorization-code"]
            assert fields["client_secret"] == ["server-only-secret"]
            assert (
                oauth_service.generate_code_challenge(fields["code_verifier"][0])
                == observed["code_challenge"][0]
            )
            assert fields["redirect_uri"] == observed["redirect_uri"]
            token = jwt.encode(
                {
                    "iss": issuer,
                    "sub": "pocket-user",
                    "aud": "obsidian",
                    "exp": int(time.time()) + 60,
                    "iat": int(time.time()),
                    "nonce": observed["nonce"][0],
                    "email": "sso@example.com",
                    "email_verified": True,
                },
                private,
                algorithm="RS256",
                headers={"kid": "test-key"},
            )
            self.respond({"access_token": "idp-access", "id_token": token, "token_type": "Bearer"})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    issuer = f"http://127.0.0.1:{server.server_port}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        provider = models.OAuthProvider(
            name="pocket-id",
            provider_type=models.OAuthProviderType.OIDC,
            issuer_url=issuer,
            client_id="obsidian",
            client_secret_encrypted="server-only-secret",
            enabled=True,
            auto_register=True,
        )
        db_session.add(provider)
        db_session.commit()
        verifier = oauth_service.generate_code_verifier()
        started = client.post(
            "/v1/auth/native/start",
            json={
                "provider": "pocket-id",
                "challenge": oauth_service.generate_code_challenge(verifier),
            },
        )
        assert started.status_code == 200
        flow = started.json()
        observed.update(parse_qs(urlsplit(flow["authorize_url"]).query))
        assert "server-only-secret" not in flow["authorize_url"]
        callback = client.get(
            "/v1/auth/native/callback",
            params={"state": observed["state"][0], "code": "authorization-code"},
        )
        assert callback.status_code == 200, callback.text
        completed = client.post(
            "/v1/auth/native/complete", json={"id": flow["id"], "verifier": verifier}
        )
        assert completed.status_code == 200, completed.text
        assert completed.json()["user_email"] == "sso@example.com"
        claims = jwt.decode(completed.json()["access_token"], options={"verify_signature": False})
        assert claims["session_id"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
