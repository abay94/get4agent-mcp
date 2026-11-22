const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

// Run the real stdio handler, replacing only host I/O and the HTTP boundary.
function client(upstream, status = 200) {
  let handler;
  const requests = [], replies = [];
  const context = vm.createContext({
    require: (name) => name === 'node:readline' ? {
      createInterface: () => ({ on: (_, fn) => { handler = fn; } }),
    } : name === 'node:fs' ? new Proxy(fs, { get(target, key) {
      if (['writeFileSync', 'mkdirSync', 'readFileSync'].includes(key)) {
        return () => { throw new Error('Public search must not access signing keys'); };
      }
      return target[key];
    } }) : require(name),
    process: { stdin: {}, stdout: { write: (s) => replies.push(JSON.parse(s)) } },
    Buffer, URL,
    fetch: async (url, options) => {
      requests.push({ url, body: JSON.parse(options.body) });
      return { ok: status === 200, status, json: async () => upstream };
    },
  });
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../mcpb/server/index.js'), 'utf8'), context);
  return { requests, replies, call: async (args) => {
    await handler(JSON.stringify({ jsonrpc: '2.0', id: 9, method: 'tools/call',
      params: { name: 'search_catalog', arguments: args } }));
    return replies.at(-1);
  } };
}

test('public desktop search forwards query without a gaming default or key creation', async () => {
  const c = client({ result: { content: [{ type: 'text', text: 'none' }] } });
  const reply = await c.call({ query: 'chairs' });
  assert.equal(reply.result.isError, undefined);
  assert.deepEqual(c.requests[0].body.params.arguments, { query: 'chairs', category: '' });
});

test('explicit category survives and structured response is not truncated', async () => {
  const result = { content: [{ type: 'text', text: 'x'.repeat(5000) }],
    structuredContent: { listings: [], has_more: false }, isError: false };
  const c = client({ result });
  const reply = await c.call({ query: 'weather', category: 'services' });
  assert.equal(c.requests[0].body.params.arguments.category, 'services');
  assert.deepEqual(reply.result, result);
});

test('desktop upstream errors cannot appear as successful searches', async () => {
  for (const [body, status] of [[{ error: { code: -32602, message: 'unknown tool' } }, 200],
    [{ result: { content: [{ type: 'text', text: 'offline' }], isError: true } }, 200],
    [{ detail: 'unavailable' }, 503]]) {
    const c = client(body, status);
    assert.equal((await c.call({ query: 'chairs' })).result.isError, true);
  }
});
