"""The buyer connectors sign the way get4agent verifies (AAuth -11).

Both connectors (``agent.py`` and the desktop extension ``mcpb/server/index.js``)
are made to produce their real signed requests, and each request is handed to
``regent_httpsig.HttpsigVerifier`` — the verifier get4agent runs — with a default
configuration. A request passes only if the verifier returns a verified ``aauth``
signature whose keyid is the thumbprint of the connector's own key.

Background: until 27.11.2026 both connectors signed the Web Bot Auth way
(``@method @authority @path signature-agent``) and added ``Signature-Key``
afterwards. With ``Signature-Key`` present the server takes the AAuth path, which
requires ``signature-key`` to be covered (and ``content-digest`` /
``content-type`` with a body): every signed request got 401. The first test
keeps that old shape as a negative control.

Needs ``regent-httpsig[aauth]`` (and ``httpx``) importable — e.g. run with the
get4agent backend's virtualenv — and, for the desktop connector, ``node`` on PATH.
Nothing is sent anywhere: HTTP is replaced, keys are throwaway and live in a temp
directory.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import types
import unittest
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
DRIVER = ROOT / "tests" / "fixtures" / "node_sign.cjs"
ISS = "https://issuer.example"
LISTING = "0b9f7f0e-6c1d-4d8e-9a53-2f1c4f6f7a10"
AAUTH_BASE = ["@method", "@authority", "@path", "signature-key"]
AAUTH_BODY = [*AAUTH_BASE, "content-digest", "content-type"]

try:
    import httpx
    import jwt as pyjwt
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from regent_httpsig import (
        EgressSigner,
        HttpsigConfig,
        HttpsigVerifier,
        VerificationError,
        VerifiedSignature,
        generate_seed,
    )
    from regent_httpsig.jwk import b64url, jwk_thumbprint
    from regent_httpsig.sfv import parse_signature_input
    from regent_httpsig.verify import _register_fully_specified_algs
    HAVE_VERIFIER = True
except ImportError:  # pragma: no cover - reported as a skip, with the reason
    HAVE_VERIFIER = False

NODE = shutil.which("node")


def issuer_docs(jwk: dict[str, Any]) -> dict[str, Any]:
    return {f"{ISS}/.well-known/aauth-agent.json": {"issuer": ISS, "jwks_uri": f"{ISS}/jwks"},
            f"{ISS}/jwks": {"keys": [jwk]}}


def verify(docs: dict[str, Any], method: str, url: str, headers: dict[str, str],
           body: bytes | None) -> Any:
    """What get4agent's ``verify_request`` does with a request, minus the HTTP fetch."""
    verifier = HttpsigVerifier(HttpsigConfig())

    async def fetch(doc_url: str) -> dict[str, Any] | None:
        return docs.get(doc_url)

    verifier._fetch_json = fetch  # the issuer's two documents, served locally
    return asyncio.run(verifier.verify_detailed(method, url, dict(headers), body))


def covered(headers: dict[str, str]) -> list[str]:
    (entry,) = parse_signature_input(headers["Signature-Input"]).values()
    return entry[0]


class Issuer:
    """A throwaway AAuth agent-token issuer with the claim shape of get4agent's."""

    def __init__(self) -> None:
        _register_fully_specified_algs()
        self._key = Ed25519PrivateKey.generate()
        self.jwk = {"kty": "OKP", "crv": "Ed25519", "kid": "iss-1", "alg": "Ed25519",
                    "x": b64url(self._key.public_key().public_bytes_raw())}
        self.docs = issuer_docs(self.jwk)

    def mint(self, agent_jwk: dict[str, Any]) -> str:
        now = int(time.time())
        cnf = {k: agent_jwk[k] for k in ("kty", "crv", "x")} | {"alg": "Ed25519"}
        return pyjwt.encode(
            {"iss": ISS, "dwk": "aauth-agent.json", "sub": "key:test", "iat": now,
             "jti": uuid.uuid4().hex,  # as get4agent's issuer: every token is distinct
             "exp": now + 600, "cnf": {"jwk": cnf}, "market_status": "unclaimed"},
            self._key, algorithm="Ed25519", headers={"typ": "aa-agent+jwt", "kid": "iss-1"})


@unittest.skipUnless(HAVE_VERIFIER, "regent-httpsig[aauth] and httpx are required")
class OldShapeIsRefused(unittest.TestCase):
    def test_web_bot_auth_shape_plus_signature_key_is_refused(self) -> None:
        """The negative control: what both connectors sent before the fix."""
        issuer = Issuer()
        signer = EgressSigner(seed=generate_seed(), signature_agent="https://regentprotocol.org")
        url = "https://get4agent.com/v1/agents/registration"
        headers = signer.sign("GET", url, {"Content-Type": "application/json"})
        headers["Signature-Key"] = f'sig1=jwt;jwt="{issuer.mint(signer.public_jwk)}"'
        outcome = verify(issuer.docs, "GET", url, headers, None)
        self.assertIsInstance(outcome, VerificationError)
        self.assertEqual(outcome.code, "invalid_input")
        self.assertIn("signature-key", outcome.detail)


@unittest.skipUnless(HAVE_VERIFIER, "regent-httpsig[aauth] and httpx are required")
class PythonConnectorSigning(unittest.TestCase):
    """``agent.py``: its real tool functions, HTTP replaced, keys in a temp dir."""

    def setUp(self) -> None:
        class Registration:
            def __init__(self, name: str) -> None: pass
            def tool(self): return lambda fn: fn
        sdk = types.ModuleType("mcp.server.mcpserver")
        sdk.MCPServer = Registration
        with patch.dict(sys.modules, {"mcp.server.mcpserver": sdk}):
            spec = importlib.util.spec_from_file_location("buyer_signing", ROOT / "agent.py")
            self.buyer = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(self.buyer)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.buyer.KEY_PATH = Path(self.tmp.name) / ".get4agent" / "agent.seed"
        self.buyer.PASSPORT_PATH = Path(self.tmp.name) / ".get4agent" / "passport.jwt"
        self.issuer = Issuer()
        self.requests: list[dict[str, Any]] = []
        self.bootstraps: list[dict[str, Any]] = []
        self.refuse_next = 0

    def _respond(self, method: str, url: str, **kw: Any) -> httpx.Response:
        request = httpx.Request(method, url)
        if url.endswith("/v1/agents/bootstrap"):
            self.bootstraps.append(kw)
            return httpx.Response(
                200, json={"agent_token": self.issuer.mint(kw["json"]["jwk"])}, request=request)
        self.requests.append({"method": method, "url": url, "headers": dict(kw["headers"]),
                              "body": kw.get("content")})
        if self.refuse_next:
            self.refuse_next -= 1
            return httpx.Response(401, json={"detail": {"code": "SIGNATURE_INVALID"}},
                                  request=request)
        if "/v1/call/" in url:
            return httpx.Response(200, request=request, json={
                "result": {"delivery": "CODE"}, "receipt": {"amount": "100", "currency": "KZT"}})
        return httpx.Response(200, json={"status": "pending"}, request=request)

    def _run(self, fn, *args: Any, **kw: Any) -> Any:
        with patch.object(self.buyer.httpx, "post",
                          side_effect=lambda url, **k: self._respond("POST", url, **k)), \
             patch.object(self.buyer.httpx, "get",
                          side_effect=lambda url, **k: self._respond("GET", url, **k)):
            return fn(*args, **kw)

    def _keyid(self) -> str:
        return jwk_thumbprint(self.bootstraps[0]["json"]["jwk"])

    def _assert_verified(self, request: dict[str, Any], expect_covered: list[str]) -> None:
        outcome = verify(self.issuer.docs, request["method"], request["url"],
                         request["headers"], request["body"])
        self.assertIsInstance(outcome, VerifiedSignature, getattr(outcome, "detail", outcome))
        self.assertEqual(outcome.scheme, "aauth")
        self.assertEqual(outcome.keyid, self._keyid())
        self.assertEqual(outcome.agent, ISS)
        self.assertEqual(covered(request["headers"]), expect_covered)
        lowered = {k.lower() for k in request["headers"]}
        self.assertNotIn("signature-agent", lowered)
        for gone in ("keyid", "tag=", "alg=", "expires", "sig1"):
            self.assertNotIn(gone, request["headers"]["Signature-Input"])

    def test_setup_status_and_buy_are_accepted_by_the_servers_verifier(self) -> None:
        self._run(self.buyer.setup, "owner@example.com", "shopper")
        self._run(self.buyer.status)
        self._run(self.buyer.buy, LISTING, game_user_id="Айдар-5512")
        register, status, buy = self.requests
        self.assertEqual((register["method"], register["url"]),
                         ("POST", "https://get4agent.com/v1/agents/register"))
        self.assertEqual((status["method"], status["url"], status["body"]),
                         ("GET", "https://get4agent.com/v1/agents/registration", None))
        self.assertEqual(buy["url"], f"https://get4agent.com/v1/call/{LISTING}")
        self._assert_verified(register, AAUTH_BODY)
        self._assert_verified(status, AAUTH_BASE)
        self._assert_verified(buy, AAUTH_BODY)
        self.assertEqual(json.loads(buy["body"]),
                         {"agent_id": "mcp-buyer", "gameUserId": "Айдар-5512"})
        self.assertEqual(len(self.bootstraps), 1)  # one token, cached

    def test_the_signature_binds_the_body_and_the_path(self) -> None:
        self._run(self.buyer.buy, LISTING, game_user_id="5512")
        (buy,) = self.requests
        self._assert_verified(buy, AAUTH_BODY)
        swapped = verify(self.issuer.docs, "POST", buy["url"], buy["headers"],
                         b'{"agent_id":"mcp-buyer","gameUserId":"someone-else"}')
        self.assertIsInstance(swapped, VerificationError)
        moved = verify(self.issuer.docs, "POST", buy["url"] + "0", buy["headers"], buy["body"])
        self.assertIsInstance(moved, VerificationError)
        elsewhere = verify(self.issuer.docs, "POST", buy["url"].replace(
            "get4agent.com", "evil.example"), buy["headers"], buy["body"])
        self.assertIsInstance(elsewhere, VerificationError)

    def test_buy_refuses_a_listing_id_that_is_not_a_uuid_and_sends_nothing(self) -> None:
        """C1 (review of 27.11.2026): a ``listing_id`` taken from seller text must
        not be able to aim this agent's signature at another route. Anything that
        is not a UUID creates no key, mints no token and sends nothing."""
        hostile = [
            "../budget/token", "%2e%2e/budget/token", "%2E%2E%2Fbudget%2Ftoken",
            "x/../../v1/agents/passport", "../agents/register", "..", ".", "",
            f"{LISTING}/../../budget/token", f"{LISTING}/..", f"{LISTING}?x=1",
            f"{LISTING}#frag", f"{LISTING}\n", f" {LISTING}",
            f"{LISTING}%2f..%2f..%2fbudget%2ftoken", "..\\budget\\token",
            "//evil.example/v1/call", "@evil.example/x", "https://evil.example/",
            LISTING.replace("-", ""), "{" + LISTING + "}", f"urn:uuid:{LISTING}",
            12345, None, [LISTING],
        ]
        for listing_id in hostile:
            with self.subTest(listing_id=listing_id):
                with self.assertRaisesRegex(ValueError, "listing_id must be a listing UUID"):
                    self._run(self.buyer.buy, listing_id, game_user_id="5512")
                self.assertEqual(self.requests, [])
                self.assertEqual(self.bootstraps, [])
                self.assertFalse(self.buyer.KEY_PATH.parent.exists(), "a key was created")

    def test_buy_with_a_uuid_still_goes_to_the_call_route_only(self) -> None:
        for listing_id in (LISTING, LISTING.upper()):
            self.requests.clear()
            self._run(self.buyer.buy, listing_id)
            (buy,) = self.requests
            self.assertEqual(buy["url"], f"https://get4agent.com/v1/call/{listing_id}")
            self._assert_verified(buy, AAUTH_BODY)

    def test_no_helper_signs_a_path_that_would_be_rewritten(self) -> None:
        """Defence in depth under the UUID rule: the signing helpers themselves
        refuse a path with dot segments, a query or a fragment."""
        for path in ("/v1/call/../budget/token", "/v1/call/./x", "/v1/call/x?y=1",
                     "/v1/call/x#y", "/v1/call/.."):
            with self.subTest(path=path):
                with self.assertRaisesRegex(ValueError, "refusing to sign"):
                    self._run(self.buyer._post_signed, path, {})
                with self.assertRaisesRegex(ValueError, "refusing to sign"):
                    self._run(self.buyer._get_signed, path)
        self.assertEqual(self.requests, [])
        self.assertEqual(self.bootstraps, [])
        self.assertFalse(self.buyer.KEY_PATH.parent.exists())

    def test_key_files_are_private_from_the_moment_they_exist(self) -> None:
        """The seed and the token are created with mode 0600 by ``os.open``, not
        written and then narrowed: with a permissive umask and ``chmod`` disabled
        they are still 0600, and an existing file is never overwritten."""
        old_umask = os.umask(0)
        self.addCleanup(os.umask, old_umask)
        with patch.object(Path, "chmod", side_effect=AssertionError("chmod must not be needed")):
            self._run(self.buyer.status)
        for path in (self.buyer.KEY_PATH, self.buyer.PASSPORT_PATH):
            self.assertEqual(path.stat().st_mode & 0o777, 0o600, path.name)
        self.assertEqual(self.buyer.KEY_PATH.parent.stat().st_mode & 0o777, 0o700)
        seed = self.buyer.KEY_PATH.read_text()
        self.assertFalse(self.buyer._write_private(self.buyer.KEY_PATH, "another-seed"))
        self.assertEqual(self.buyer.KEY_PATH.read_text(), seed)

    def test_bootstrap_sends_only_the_public_key_and_the_seed_stays_private(self) -> None:
        self._run(self.buyer.status)
        (bootstrap,) = self.bootstraps
        self.assertEqual(set(bootstrap), {"json", "timeout"})
        self.assertEqual(set(bootstrap["json"]), {"jwk"})
        self.assertLessEqual(set(bootstrap["json"]["jwk"]), {"kty", "crv", "x", "alg"})
        seed = self.buyer.KEY_PATH.read_text().strip()
        self.assertNotIn(seed, repr(self.requests) + repr(self.bootstraps))
        self.assertEqual(self.buyer.KEY_PATH.stat().st_mode & 0o077, 0)

    def test_a_refused_passport_is_replaced_and_the_retry_verifies(self) -> None:
        self.refuse_next = 1
        text = self._run(self.buyer.setup, "owner@example.com")
        self.assertTrue(text.startswith("Registered (status: pending)"), text)
        first, second = self.requests
        self.assertNotEqual(first["headers"]["Signature-Key"], second["headers"]["Signature-Key"])
        self._assert_verified(second, AAUTH_BODY)
        self.assertEqual(len(self.bootstraps), 2)


def run_node(job: dict[str, Any]) -> dict[str, Any]:
    done = subprocess.run([NODE, str(DRIVER)], input=json.dumps(job).encode(),
                          capture_output=True, timeout=60, check=False)
    if done.returncode != 0:
        raise AssertionError(f"node driver failed: {done.stderr.decode()[:2000]}")
    return json.loads(done.stdout)


NODE_CASES = [
    {"tool": "setup", "arguments": {"contact_email": "owner@example.com",
                                    "agent_name": "shopper"}},
    {"tool": "status", "arguments": {}},
    {"tool": "buy", "arguments": {"listing_id": LISTING, "game_user_id": "Айдар-5512"}},
]


@unittest.skipUnless(HAVE_VERIFIER, "regent-httpsig[aauth] and httpx are required")
@unittest.skipUnless(NODE, "node is required for the desktop connector")
class DesktopConnectorSigning(unittest.TestCase):
    """``mcpb/server/index.js``: run by node, verified here."""

    def test_what_the_desktop_connector_sends_is_accepted_by_the_servers_verifier(self) -> None:
        out = run_node({"mode": "live", "iss": ISS, "cases": NODE_CASES})
        docs = issuer_docs(out["issuer_jwk"])
        bootstrap, register, status, buy = out["requests"]
        self.assertTrue(bootstrap["url"].endswith("/v1/agents/bootstrap"))
        agent_jwk = json.loads(bootstrap["body"])["jwk"]
        self.assertEqual(set(agent_jwk), {"kty", "crv", "x"})
        keyid = jwk_thumbprint(agent_jwk)
        for request, expect in ((register, AAUTH_BODY), (status, AAUTH_BASE), (buy, AAUTH_BODY)):
            body = request["body"].encode() if request["body"] is not None else None
            outcome = verify(docs, request["method"], request["url"], request["headers"], body)
            self.assertIsInstance(outcome, VerifiedSignature,
                                  (request["url"], getattr(outcome, "detail", outcome)))
            self.assertEqual((outcome.scheme, outcome.keyid, outcome.agent),
                             ("aauth", keyid, ISS))
            self.assertEqual(covered(request["headers"]), expect)
            self.assertNotIn("signature-agent", {k.lower() for k in request["headers"]})
        self.assertEqual(json.loads(buy["body"]),
                         {"agent_id": "mcp-buyer", "gameUserId": "Айдар-5512"})
        # and the verifier really reads the body: another body, same headers → refused
        swapped = verify(docs, "POST", buy["url"], buy["headers"], b'{"agent_id":"x"}')
        self.assertIsInstance(swapped, VerificationError)

    def test_the_desktop_connector_equals_the_library_signer_byte_for_byte(self) -> None:
        """Same key, token, time and requests → the same headers as
        ``EgressSigner.sign_aauth`` (Ed25519 signatures are deterministic). The key
        is the published test key of RFC 8032 section 7.1 (TEST 1) and the token a
        placeholder: neither is a credential."""
        seed_hex = "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60"
        token = "eyJ0eXAiOiJhYS1hZ2VudCtqd3QifQ.eyJub3RlIjoidGVzdC12ZWN0b3IifQ.c2lnbmF0dXJl"
        created = 1795000000
        out = run_node({"mode": "vectors", "seed_hex": seed_hex, "created": created,
                        "token": token, "cases": NODE_CASES})
        signer = EgressSigner(seed=b64url(bytes.fromhex(seed_hex)),
                              signature_agent="https://unused.example")
        self.assertEqual(len(out["requests"]), 3)  # the passport was there: no bootstrap
        for request in out["requests"]:
            body = request["body"].encode() if request["body"] is not None else None
            expected = signer.sign_aauth(request["method"], request["url"], token=token,
                                         body=body, created=created)
            self.assertEqual(request["headers"], expected, request["url"])


if __name__ == "__main__":
    unittest.main()
