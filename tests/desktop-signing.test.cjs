// The desktop connector signs the way get4agent verifies: AAuth -11.
//
// Until 27.11.2026 it signed the Web Bot Auth way (components
// `@method @authority @path signature-agent`, keyid/alg/expires/tag) and then ADDED
// `Signature-Key: sig1=jwt;…`. With Signature-Key present the server takes the AAuth
// path and requires `signature-key` to be covered (plus content-digest and content-type
// with a body): every signed request was answered 401.
//
// This file checks the wire shape and the signature with Node alone: the signature base
// is rebuilt here from the headers that were sent, independently of index.js, and the
// signature is verified with the public key the connector published at bootstrap.
// tests/test_signing.py goes further: it gives what this connector emits to the
// regent-httpsig verifier itself and compares it byte for byte with that library's signer.

const { test } = require('node:test');
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const { load, tempHome } = require('./fixtures/harness.cjs');

const TOKEN_1 = 'aaa.bbb.ccc';
const TOKEN_2 = 'ddd.eee.fff';
const LISTING = '0b9f7f0e-6c1d-4d8e-9a53-2f1c4f6f7a10';

// A connector with a fresh key in a temp home. `refuseFirstRegister` answers the first
// signed request 401 (a stale passport) to exercise the re-bootstrap path.
function connector({ refuseFirstRegister = false } = {}) {
  const home = tempHome();
  let bootstraps = 0;
  let registers = 0;
  const c = load({ home, respond: (request) => {
    if (request.url.endsWith('/v1/agents/bootstrap')) {
      bootstraps += 1;
      return { status: 200, json: { agent_token: bootstraps === 1 ? TOKEN_1 : TOKEN_2 } };
    }
    if (request.url.endsWith('/v1/agents/register')) {
      registers += 1;
      if (refuseFirstRegister && registers === 1) return { status: 401, json: { detail: {} } };
      return { status: 201, json: { status: 'pending' } };
    }
    if (request.url.includes('/v1/call/')) {
      return { status: 200, json: { result: { delivery: 'CODE' },
        receipt: { amount: '100', currency: 'KZT' }, decision_id: 'd1' } };
    }
    return { status: 200, json: { status: 'approved' } };
  } });
  return { ...c, home, cleanup: () => fs.rmSync(home, { recursive: true, force: true }) };
}

const signed = (c) => c.requests.filter((r) => !r.url.endsWith('/v1/agents/bootstrap'));
const publicKeyOf = (c) => {
  const bootstrap = c.requests.find((r) => r.url.endsWith('/v1/agents/bootstrap'));
  return crypto.createPublicKey({ key: JSON.parse(bootstrap.body).jwk, format: 'jwk' });
};

// RFC 9421 §2.5, written here from the specification rather than shared with index.js.
function verifyRequest(request, publicKey) {
  const h = request.headers;
  const m = /^([a-z0-9]+)=(\(([^)]*)\);created=(\d+))$/.exec(h['Signature-Input']);
  assert.ok(m, `Signature-Input shape: ${h['Signature-Input']}`);
  const [, label, params, list, created] = m;
  const covered = list.split(' ').map((c) => JSON.parse(c));
  const u = new URL(request.url);
  const value = (name) => {
    if (name === '@method') return request.method;
    if (name === '@authority') return u.host;
    if (name === '@path') return u.pathname;
    const key = Object.keys(h).find((k) => k.toLowerCase() === name);
    assert.ok(key, `covered header ${name} is not in the request`);
    return h[key];
  };
  const base = covered.map((c) => `"${c}": ${value(c)}`).join('\n') +
    `\n"@signature-params": ${params}`;
  const sig = new RegExp(`^${label}=:([A-Za-z0-9+/=]+):$`).exec(h['Signature']);
  assert.ok(sig, `Signature shape: ${h['Signature']}`);
  const ok = crypto.verify(null, Buffer.from(base), publicKey, Buffer.from(sig[1], 'base64'));
  return { ok, label, covered, created: Number(created) };
}

test('a request without a body covers exactly the four AAuth components', async () => {
  const c = connector();
  try {
    await c.call('status', {});
    const [request] = signed(c);
    assert.equal(request.method, 'GET');
    assert.equal(request.url, 'https://get4agent.com/v1/agents/registration');
    assert.equal(request.body, null);
    assert.deepEqual(Object.keys(request.headers).sort(),
      ['Signature', 'Signature-Input', 'Signature-Key']);
    assert.equal(request.headers['Signature-Key'], `sig=jwt;jwt="${TOKEN_1}"`);
    const v = verifyRequest(request, publicKeyOf(c));
    assert.equal(v.ok, true);
    assert.equal(v.label, 'sig');
    assert.deepEqual(v.covered, ['@method', '@authority', '@path', 'signature-key']);
    assert.ok(Math.abs(v.created - Date.now() / 1000) < 5);
  } finally { c.cleanup(); }
});

test('a request with a body also covers content-digest and content-type', async () => {
  const c = connector();
  try {
    await c.call('buy', { listing_id: LISTING, game_user_id: 'Айдар-5512' });
    const [request] = signed(c);
    assert.equal(request.method, 'POST');
    assert.equal(request.url, `https://get4agent.com/v1/call/${LISTING}`);
    assert.equal(request.body, '{"agent_id":"mcp-buyer","gameUserId":"Айдар-5512"}');
    const digest = crypto.createHash('sha256').update(Buffer.from(request.body, 'utf8'))
      .digest('base64');
    assert.equal(request.headers['Content-Digest'], `sha-256=:${digest}:`);
    assert.equal(request.headers['Content-Type'], 'application/json');
    const v = verifyRequest(request, publicKeyOf(c));
    assert.equal(v.ok, true);
    assert.deepEqual(v.covered, ['@method', '@authority', '@path', 'signature-key',
      'content-digest', 'content-type']);
  } finally { c.cleanup(); }
});

test('nothing of the old Web Bot Auth shape is left', async () => {
  const c = connector();
  try {
    await c.call('setup', { contact_email: 'owner@example.com' });
    await c.call('status', {});
    await c.call('buy', { listing_id: LISTING });
    assert.equal(signed(c).length, 3);
    for (const request of signed(c)) {
      const names = Object.keys(request.headers).map((k) => k.toLowerCase());
      assert.ok(!names.includes('signature-agent'), 'Signature-Agent must not be sent');
      const input = request.headers['Signature-Input'];
      for (const gone of ['keyid', 'alg=', 'tag=', 'expires', 'web-bot-auth', 'signature-agent',
        'sig1']) {
        assert.ok(!input.includes(gone), `${gone} in Signature-Input: ${input}`);
      }
      assert.ok(input.includes('"signature-key"'));
      // one label on all three headers: the verifier pairs them by it
      for (const name of ['Signature-Key', 'Signature-Input', 'Signature']) {
        assert.ok(request.headers[name].startsWith('sig='), `${name}: ${request.headers[name]}`);
      }
      assert.equal(verifyRequest(request, publicKeyOf(c)).ok, true);
    }
  } finally { c.cleanup(); }
});

test('the signature binds method, path, token and body', async () => {
  const c = connector();
  try {
    await c.call('buy', { listing_id: LISTING, game_user_id: '5512' });
    const [request] = signed(c);
    const key = publicKeyOf(c);
    assert.equal(verifyRequest(request, key).ok, true);
    const changed = (patch) => verifyRequest({ ...request, ...patch }, key).ok;
    assert.equal(changed({ method: 'GET' }), false);
    assert.equal(changed({ url: `https://get4agent.com/v1/call/${LISTING}x` }), false);
    assert.equal(changed({ url: `https://evil.example/v1/call/${LISTING}` }), false);
    const withHeader = (name, value) => ({ headers: { ...request.headers, [name]: value } });
    assert.equal(changed(withHeader('Signature-Key', `sig=jwt;jwt="${TOKEN_2}"`)), false);
    assert.equal(changed(withHeader('Content-Type', 'text/plain')), false);
    // another body means another digest, and the digest is covered
    const other = crypto.createHash('sha256').update('{"agent_id":"x"}').digest('base64');
    assert.equal(changed(withHeader('Content-Digest', `sha-256=:${other}:`)), false);
  } finally { c.cleanup(); }
});

// C1 (review of 27.11.2026): `new URL()` resolves dot segments BEFORE the path is signed,
// so `buy` with listing_id "../budget/token" used to become a correctly signed
// POST /v1/budget/token. A listing_id that is not a UUID now creates no key, mints no
// token and sends nothing.
const HOSTILE_LISTING_IDS = [
  '../budget/token',
  '%2e%2e/budget/token',
  '%2E%2E%2Fbudget%2Ftoken',
  'x/../../v1/agents/passport',
  '../agents/register',
  '..',
  '.',
  '',
  `${LISTING}/../../budget/token`,
  `${LISTING}/..`,
  `${LISTING}?x=1`,
  `${LISTING}#frag`,
  `${LISTING}\n`,
  ` ${LISTING}`,
  `${LISTING}%2f..%2f..%2fbudget%2ftoken`,
  '..\\budget\\token',
  '//evil.example/v1/call',
  '@evil.example/x',
  'https://evil.example/',
  '0b9f7f0e6c1d4d8e9a532f1c4f6f7a10',
  12345,
  null,
  undefined,
  { toString: () => LISTING },
  [LISTING],
];

test('buy refuses a listing_id that is not a UUID and sends nothing', async () => {
  for (const id of HOSTILE_LISTING_IDS) {
    const c = connector();
    try {
      const reply = await c.call('buy', { listing_id: id, game_user_id: '5512' });
      const label = JSON.stringify(id) || String(id);
      assert.equal(reply.result.isError, true, label);
      assert.match(reply.result.content[0].text, /listing_id must be a listing UUID/, label);
      assert.equal(c.requests.length, 0, `${label}: a request was sent`);
      assert.equal(fs.existsSync(path.join(c.home, '.get4agent')), false,
        `${label}: a key was created`);
    } finally { c.cleanup(); }
  }
});

test('buy with a UUID still goes to /v1/call/<uuid> and nowhere else', async () => {
  for (const id of [LISTING, LISTING.toUpperCase()]) {
    const c = connector();
    try {
      const reply = await c.call('buy', { listing_id: id });
      assert.equal(reply.result.isError, undefined);
      const [request] = signed(c);
      assert.equal(request.url, `https://get4agent.com/v1/call/${id}`);
      assert.equal(new URL(request.url).pathname, `/v1/call/${id}`);
      assert.equal(verifyRequest(request, publicKeyOf(c)).ok, true);
    } finally { c.cleanup(); }
  }
});

test('bootstrap sends the public key only, unsigned; the private key never leaves', async () => {
  const c = connector();
  try {
    await c.call('setup', { contact_email: 'owner@example.com', agent_name: 'shopper' });
    const bootstrap = c.requests[0];
    assert.equal(bootstrap.url, 'https://get4agent.com/v1/agents/bootstrap');
    assert.deepEqual(Object.keys(JSON.parse(bootstrap.body)), ['jwk']);
    assert.deepEqual(Object.keys(JSON.parse(bootstrap.body).jwk).sort(), ['crv', 'kty', 'x']);
    assert.ok(!Object.keys(bootstrap.headers).some((k) => k.toLowerCase().startsWith('signature')));
    const pem = fs.readFileSync(path.join(c.home, '.get4agent', 'agent-node.pem'), 'utf8');
    const secret = pem.split('\n')[1];
    assert.ok(secret.length > 20);
    assert.ok(!JSON.stringify(c.requests).includes(secret));
    assert.ok(!JSON.stringify(c.replies).includes(secret));
    assert.equal(fs.statSync(path.join(c.home, '.get4agent', 'agent-node.pem')).mode & 0o077, 0);
  } finally { c.cleanup(); }
});

test('a refused passport is replaced and the retry is signed with the new token', async () => {
  const c = connector({ refuseFirstRegister: true });
  try {
    const reply = await c.call('setup', { contact_email: 'owner@example.com' });
    assert.match(reply.result.content[0].text, /^Registered \(status: pending\)/);
    const [first, second] = signed(c);
    assert.equal(first.headers['Signature-Key'], `sig=jwt;jwt="${TOKEN_1}"`);
    assert.equal(second.headers['Signature-Key'], `sig=jwt;jwt="${TOKEN_2}"`);
    assert.equal(first.body, second.body);
    for (const request of [first, second]) {
      assert.equal(verifyRequest(request, publicKeyOf(c)).ok, true);
    }
  } finally { c.cleanup(); }
});
