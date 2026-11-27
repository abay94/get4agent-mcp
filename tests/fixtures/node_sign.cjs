// Driver for tests/test_signing.py: makes the real desktop connector sign requests and
// prints what it would have sent, as JSON, so the Python side can hand it to the same
// verifier get4agent runs (regent-httpsig). Reads one JSON job on stdin:
//
//   { "mode": "vectors", "seed_hex", "created", "token", "cases": [{ "tool", "arguments" }] }
//       fixed key, fixed clock, fixed token → deterministic headers (compared byte for
//       byte with regent-httpsig's EgressSigner.sign_aauth)
//
//   { "mode": "live", "iss", "cases": [{ "tool", "arguments" }] }
//       the connector creates its own key and asks /v1/agents/bootstrap for a token; this
//       driver plays the issuer (a throwaway Ed25519 key, the claim shape of get4agent's
//       issuer) and reports the issuer's public JWK so the verifier can check the token.
//
// No network: `fetch` is answered locally. Keys live in a temp dir that is removed.

'use strict';
const crypto = require('node:crypto');
const fs = require('node:fs');
const { load, tempHome, installSeed, installPassport } = require('./harness.cjs');

const b64u = (buf) => Buffer.from(buf).toString('base64url');

function issuer(iss) {
  const { privateKey, publicKey } = crypto.generateKeyPairSync('ed25519');
  const x = b64u(publicKey.export({ type: 'spki', format: 'der' }).subarray(-32));
  const jwk = { kty: 'OKP', crv: 'Ed25519', x, kid: 'iss-1', alg: 'Ed25519' };
  const mint = (agentJwk) => {
    const now = Math.floor(Date.now() / 1000);
    const header = { alg: 'Ed25519', typ: 'aa-agent+jwt', kid: 'iss-1' };
    const claims = { iss, dwk: 'aauth-agent.json', sub: 'key:test', iat: now, exp: now + 600,
      // what get4agent's issuer does: the posted public key, plus a fully-specified alg
      cnf: { jwk: { kty: agentJwk.kty, crv: agentJwk.crv, x: agentJwk.x, alg: 'Ed25519' } },
      market_status: 'unclaimed' };
    const signingInput = `${b64u(JSON.stringify(header))}.${b64u(JSON.stringify(claims))}`;
    return `${signingInput}.${b64u(crypto.sign(null, Buffer.from(signingInput), privateKey))}`;
  };
  return { jwk, mint };
}

async function main() {
  const job = JSON.parse(fs.readFileSync(0, 'utf8'));
  const home = tempHome();
  try {
    let iss = null;
    const options = { home };
    if (job.mode === 'vectors') {
      installSeed(home, job.seed_hex);
      installPassport(home, job.token);
      options.nowMs = job.created * 1000;
      options.respond = () => ({ status: 200, json: { status: 'pending' } });
    } else {
      iss = issuer(job.iss);
      options.respond = (request) => request.url.endsWith('/v1/agents/bootstrap')
        ? { status: 200, json: { agent_token: iss.mint(JSON.parse(request.body).jwk) } }
        : { status: 200, json: { status: 'pending' } };
    }
    const connector = load(options);
    for (const c of job.cases) await connector.call(c.tool, c.arguments);
    process.stdout.write(JSON.stringify({
      issuer_jwk: iss ? iss.jwk : null, requests: connector.requests }));
  } finally {
    fs.rmSync(home, { recursive: true, force: true });
  }
}

main().catch((e) => { process.stderr.write(String(e && e.stack || e)); process.exit(1); });
