// get4agent buyer MCP — Node, zero dependencies (bundled as a one-click .mcpb).
//
// Your agent's signing key is generated HERE, on this machine, and never leaves it.
// Onboarding: bootstrap passport (identity-only) -> signed self-registration ->
// the OWNER approves via the claim e-mail. Every purchase is an RFC 9421-signed
// request governed by the owner's mandate, balance and kill switch.

'use strict';
const crypto = require('node:crypto');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const readline = require('node:readline');

const MARKET = 'https://get4agent.com';
const DIR = path.join(os.homedir(), '.get4agent');
const KEY_FILE = path.join(DIR, 'agent-node.pem');
const PASSPORT_FILE = path.join(DIR, 'passport-node.jwt');

// ── key material ─────────────────────────────────────────────────────────────
const b64u = (buf) => Buffer.from(buf).toString('base64url');

function loadKey() {
  if (!fs.existsSync(KEY_FILE)) {
    fs.mkdirSync(DIR, { recursive: true });
    const { privateKey } = crypto.generateKeyPairSync('ed25519');
    fs.writeFileSync(KEY_FILE, privateKey.export({ type: 'pkcs8', format: 'pem' }), { mode: 0o600 });
  }
  const priv = crypto.createPrivateKey(fs.readFileSync(KEY_FILE));
  const pubRaw = crypto.createPublicKey(priv).export({ type: 'spki', format: 'der' }).subarray(-32);
  const jwk = { kty: 'OKP', crv: 'Ed25519', x: b64u(pubRaw) };
  // RFC 7638 thumbprint: canonical JSON of {crv,kty,x}, sha256, base64url.
  const canon = `{"crv":"Ed25519","kty":"OKP","x":"${jwk.x}"}`;
  const keyid = b64u(crypto.createHash('sha256').update(canon).digest());
  return { priv, jwk, keyid };
}

async function passport(key) {
  if (fs.existsSync(PASSPORT_FILE)) return fs.readFileSync(PASSPORT_FILE, 'utf8').trim();
  const r = await fetch(`${MARKET}/v1/agents/bootstrap`, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ jwk: key.jwk }),
  });
  if (!r.ok) throw new Error(`bootstrap failed: HTTP ${r.status}`);
  const token = (await r.json()).agent_token;
  fs.writeFileSync(PASSPORT_FILE, token, { mode: 0o600 });
  return token;
}

// ── RFC 9421 signing, the AAuth way (draft-hardt-oauth-aauth-protocol-11 §11.3.3) ──
// Mirrors regent-httpsig `EgressSigner.sign_aauth` byte for byte (tests/test_signing.py
// compares the two on the same key, token, time and requests, and gives what this file
// emits to that library's verifier, the one get4agent runs): the agent token
// rides in Signature-Key under the `jwt` scheme; the signature covers
// `@method @authority @path signature-key`, plus `content-digest content-type` when
// there is a body (and a Content-Digest header is sent). `created` only: no keyid,
// no alg, no tag, no Signature-Agent — the key is the token's cnf.jwk.
//
// Until 27.11.2026 this signed the Web Bot Auth way and added Signature-Key afterwards.
// get4agent has required the -11 form since 06.10.2026 and answered every such request
// 401 (invalid_input: signature does not cover signature-key).
const LABEL = 'sig';

function contentDigest(bodyText) {
  const hash = crypto.createHash('sha256').update(Buffer.from(bodyText, 'utf8')).digest('base64');
  return `sha-256=:${hash}:`;
}

// `bodyText` is the exact string that will be sent (or undefined/'' for no body).
function signHeaders(key, method, urlStr, token, bodyText) {
  const u = new URL(urlStr);
  const created = Math.floor(Date.now() / 1000);
  const headers = { 'Signature-Key': `${LABEL}=jwt;jwt="${token}"` };
  const covered = ['@method', '@authority', '@path', 'signature-key'];
  if (bodyText) {
    headers['Content-Digest'] = contentDigest(bodyText);
    headers['Content-Type'] = 'application/json';
    covered.push('content-digest', 'content-type');
  }
  const value = {
    '@method': method.toUpperCase(),
    '@authority': u.host,          // lower-case host, default port omitted (WHATWG URL)
    '@path': u.pathname || '/',
    'signature-key': headers['Signature-Key'],
    'content-digest': headers['Content-Digest'],
    'content-type': headers['Content-Type'],
  };
  const params = `(${covered.map((c) => `"${c}"`).join(' ')});created=${created}`;
  const base = covered.map((c) => `"${c}": ${value[c]}`).join('\n') +
    `\n"@signature-params": ${params}`;
  const sig = crypto.sign(null, Buffer.from(base, 'utf8'), key.priv);
  headers['Signature-Input'] = `${LABEL}=${params}`;
  headers['Signature'] = `${LABEL}=:${sig.toString('base64')}:`;
  return headers;
}

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

// The path of a purchase. `listing_id` comes from the model, which may have read it in
// seller-written text: anything that is not a UUID is refused BEFORE a key is loaded or a
// byte is sent. Otherwise `../budget/token` would become a correctly signed
// `POST /v1/budget/token` (`new URL()` resolves dot segments before the path is signed).
// The segment is percent-encoded as well, so the rule does not rest on the pattern alone.
function callPath(listingId) {
  if (typeof listingId !== 'string' || !UUID_RE.test(listingId)) {
    throw new Error('listing_id must be a listing UUID from search_catalog ' +
      '(xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx). Nothing was sent.');
  }
  return `/v1/call/${encodeURIComponent(listingId)}`;
}

async function signedFetch(key, method, pathStr, body) {
  const url = `${MARKET}${pathStr}`;
  // Sign only what was meant: if URL parsing would change the path (dot segments, a
  // query, a fragment, another host), nothing is signed and nothing is sent.
  const parsed = new URL(url);
  if (parsed.origin !== MARKET || parsed.pathname !== pathStr || parsed.search || parsed.hash) {
    throw new Error('refusing to sign a request whose path is not the intended one. ' +
      'Nothing was sent.');
  }
  // Serialized once: the digest is over exactly the bytes that are sent.
  const bodyText = body ? JSON.stringify(body) : undefined;
  const headers = signHeaders(key, method, url, await passport(key), bodyText);
  return fetch(url, { method, headers, body: bodyText });
}

// ── tools ────────────────────────────────────────────────────────────────────
const TOOLS = [
  { name: 'setup',
    description: 'One-time onboarding, fully automatic: generates this agent\'s own key ' +
      '(never leaves this machine), gets an identity passport and self-registers at ' +
      'get4agent. The owner then approves via the claim e-mail — authority always comes ' +
      'from a human click.',
    inputSchema: { type: 'object', required: ['contact_email'], properties: {
      contact_email: { type: 'string', description: 'Owner e-mail for the claim link' },
      agent_name: { type: 'string', description: 'How this agent should be listed' } } } },
  { name: 'status',
    description: 'Registration and approval state of this agent.',
    inputSchema: { type: 'object', properties: {} } },
  { name: 'search_catalog',
    description: 'Search get4agent\'s catalog of real goods (public).',
    inputSchema: { type: 'object', properties: {
      query: { type: 'string' }, category: { type: 'string', description: 'Optional category slug; omit to search all available categories' } } } },
  { name: 'buy',
    description: 'Buy a listing. Signed by this agent\'s key; the owner\'s mandate ' +
      '(per-purchase and daily limits) is enforced BEFORE any money moves. Returns the ' +
      'delivered key/code and receipt. game_user_id only for direct game top-ups.',
    inputSchema: { type: 'object', required: ['listing_id'], properties: {
      listing_id: { type: 'string' },
      game_user_id: { type: 'string' }, game_server_id: { type: 'string' } } } },
];

async function callTool(name, args) {
  if (name === 'search_catalog') {
    const r = await fetch(`${MARKET}/mcp`, {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ jsonrpc: '2.0', id: 1, method: 'tools/call',
        params: { name: 'search_listings',
                  arguments: { query: args.query || '', category: args.category || '' } } }),
    });
    if (!r.ok) throw new Error(`Marketplace search failed: HTTP ${r.status}`);
    const j = await r.json();
    if (j.error) throw new Error(j.error.message || 'Marketplace search failed');
    if (!j.result || !Array.isArray(j.result.content)) {
      throw new Error('Invalid marketplace search response');
    }
    return j.result;
  }
  // Validated before the key is touched: a refused listing_id creates and sends nothing.
  const buyPath = name === 'buy' ? callPath(args.listing_id) : null;
  const key = loadKey();
  if (name === 'setup') {
    let r = await signedFetch(key, 'POST', '/v1/agents/register', {
      name: args.agent_name || 'personal shopping agent',
      contact_email: args.contact_email,
      intent: "Buy digital goods on get4agent under my owner's mandate.",
    });
    if (r.status === 401) { // stale passport — mint fresh, retry once
      fs.rmSync(PASSPORT_FILE, { force: true });
      r = await signedFetch(key, 'POST', '/v1/agents/register', {
        name: args.agent_name || 'personal shopping agent',
        contact_email: args.contact_email,
        intent: "Buy digital goods on get4agent under my owner's mandate.",
      });
    }
    const reg = await r.json();
    if (!r.ok) return `Registration failed (HTTP ${r.status}): ${JSON.stringify(reg).slice(0, 300)}`;
    return `Registered (status: ${reg.status}). I created my own key and will never share it. ` +
      `A claim e-mail went to ${args.contact_email} — the OWNER must click it while signed ` +
      `in on get4agent.com. After approval I buy under the owner's mandate.`;
  }
  if (name === 'status') {
    const r = await signedFetch(key, 'GET', '/v1/agents/registration');
    if (r.status === 401) return 'Signature not accepted — run setup to refresh the passport.';
    if (r.status === 404) return 'Not registered yet — run setup.';
    return JSON.stringify(await r.json(), null, 1);
  }
  if (name === 'buy') {
    const body = { agent_id: 'mcp-buyer' };
    if (args.game_user_id) body.gameUserId = args.game_user_id;
    if (args.game_server_id) body.gameServerId = args.game_server_id;
    const r = await signedFetch(key, 'POST', buyPath, body);
    const data = await r.json().catch(() => ({}));
    if (!r.ok) {
      const det = data.detail || data;
      return `REFUSED (${det.code || r.status}): ${det.message || ''} — nothing was charged.`;
    }
    return JSON.stringify({
      delivered: data.result?.delivery, product: data.result?.product,
      order_id: data.result?.orderId,
      paid: `${data.receipt?.amount} ${data.receipt?.currency}`,
      balance_after: data.receipt?.balance_after, decision_id: data.decision_id,
    }, null, 1);
  }
  throw new Error(`unknown tool: ${name}`);
}

// ── minimal MCP stdio server (newline-delimited JSON-RPC) ────────────────────
const out = (msg) => process.stdout.write(JSON.stringify(msg) + '\n');

const rl = readline.createInterface({ input: process.stdin, terminal: false });
rl.on('line', async (line) => {
  line = line.trim();
  if (!line) return;
  let req; try { req = JSON.parse(line); } catch { return; }
  const { id, method, params } = req;
  try {
    if (method === 'initialize') {
      out({ jsonrpc: '2.0', id, result: {
        protocolVersion: params?.protocolVersion || '2024-11-05',
        capabilities: { tools: {} },
        serverInfo: { name: 'get4agent', version: '1.0.2' } } });
    } else if (method === 'tools/list') {
      out({ jsonrpc: '2.0', id, result: { tools: TOOLS } });
    } else if (method === 'tools/call') {
      const value = await callTool(params.name, params.arguments || {});
      const result = params.name === 'search_catalog' ? value :
        { content: [{ type: 'text', text: value }] };
      out({ jsonrpc: '2.0', id, result });
    } else if (method === 'ping') {
      out({ jsonrpc: '2.0', id, result: {} });
    } else if (id !== undefined) {
      out({ jsonrpc: '2.0', id, error: { code: -32601, message: `unknown method ${method}` } });
    } // notifications (no id) are ignored
  } catch (e) {
    if (id !== undefined) {
      out({ jsonrpc: '2.0', id,
            result: { content: [{ type: 'text', text: `Error: ${e.message}` }], isError: true } });
    }
  }
});
