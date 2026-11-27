// Runs the real desktop connector (mcpb/server/index.js) with only its edges replaced:
// stdin/stdout, the home directory (so keys land in a temp dir, never in ~/.get4agent),
// the HTTP boundary (`fetch`) and, when asked, the clock. Used by
// tests/desktop-signing.test.cjs and by tests/fixtures/node_sign.cjs (which the Python
// test drives). Nothing here talks to the network.

'use strict';
const crypto = require('node:crypto');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const vm = require('node:vm');

const INDEX = path.join(__dirname, '../../mcpb/server/index.js');
// PKCS#8 wrapper of a raw 32-byte Ed25519 seed (RFC 8410).
const PKCS8_ED25519_PREFIX = Buffer.from('302e020100300506032b657004220420', 'hex');

function tempHome() {
  return fs.mkdtempSync(path.join(os.tmpdir(), 'get4agent-test-'));
}

// Put a known key where the connector looks for its own (agent-node.pem).
function installSeed(home, seedHex) {
  const der = Buffer.concat([PKCS8_ED25519_PREFIX, Buffer.from(seedHex, 'hex')]);
  const key = crypto.createPrivateKey({ key: der, format: 'der', type: 'pkcs8' });
  const dir = path.join(home, '.get4agent');
  fs.mkdirSync(dir, { recursive: true });
  fs.writeFileSync(path.join(dir, 'agent-node.pem'),
    key.export({ type: 'pkcs8', format: 'pem' }), { mode: 0o600 });
}

function installPassport(home, token) {
  const dir = path.join(home, '.get4agent');
  fs.mkdirSync(dir, { recursive: true });
  fs.writeFileSync(path.join(dir, 'passport-node.jwt'), token, { mode: 0o600 });
}

// `respond(request)` → { status, json } for every outbound request; requests are recorded
// as { url, method, headers, body } with `body` the exact string handed to fetch.
function load({ home, respond, nowMs }) {
  let handler;
  const requests = [];
  const replies = [];
  const sandbox = {
    require: (name) => {
      if (name === 'node:readline') {
        return { createInterface: () => ({ on: (_, fn) => { handler = fn; } }) };
      }
      if (name === 'node:os') return { ...os, homedir: () => home };
      return require(name);
    },
    process: { stdin: {}, stdout: { write: (s) => replies.push(JSON.parse(s)) } },
    Buffer, URL,
    fetch: async (url, options = {}) => {
      const request = { url: String(url), method: options.method || 'GET',
        headers: { ...(options.headers || {}) },
        body: options.body === undefined ? null : String(options.body) };
      requests.push(request);
      const answer = respond(request);
      return { ok: answer.status >= 200 && answer.status < 300, status: answer.status,
        json: async () => answer.json };
    },
  };
  if (nowMs !== undefined) sandbox.Date = { now: () => nowMs };
  vm.runInContext(fs.readFileSync(INDEX, 'utf8'), vm.createContext(sandbox));
  return {
    requests, replies,
    call: async (name, args) => {
      await handler(JSON.stringify({ jsonrpc: '2.0', id: 1, method: 'tools/call',
        params: { name, arguments: args || {} } }));
      return replies.at(-1);
    },
  };
}

module.exports = { load, tempHome, installSeed, installPassport };
