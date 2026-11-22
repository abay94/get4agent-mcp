import importlib.util
import io
import http.client
import json
from pathlib import Path
import unittest
import sys
import types
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('proxy', ROOT / 'server.py')
proxy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(proxy)


class ProxyTests(unittest.TestCase):
    def request(self, method, params, upstream):
        captured = []
        def respond(req, **kwargs):
            captured.append(json.loads(req.data))
            return io.BytesIO(json.dumps(upstream).encode())
        with patch.object(proxy.urllib.request, 'urlopen', side_effect=respond):
            reply = proxy._handle({'jsonrpc': '2.0', 'id': 42,
                                   'method': method, 'params': params})
        self.assertEqual(reply['id'], 42)
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0]['method'], method)
        self.assertEqual(captured[0]['params'], params)
        return reply

    def test_tools_list_remains_available_offline(self):
        with patch.object(proxy.urllib.request, 'urlopen', side_effect=OSError('offline')) as http:
            reply = proxy._handle({'id': 42, 'method': 'tools/list'})
        http.assert_not_called()
        self.assertEqual(reply['id'], 42)
        self.assertIn('search_listings', [tool['name'] for tool in reply['result']['tools']])

    def test_static_list_mirrors_the_hosted_endpoint(self):
        # The names the hosted endpoint answers to. A tool it does not serve must not be
        # advertised here: calling it returns "unknown tool".
        names = [tool['name'] for tool in proxy.TOOLS]
        self.assertEqual(sorted(names), sorted([
            'list_categories', 'search_listings', 'get_listing', 'validate_iin',
            'validate_kz_iban', 'lei_lookup', 'kzt_rates', 'fx_rates', 'weather', 'track_parcel']))
        search = next(t for t in proxy.TOOLS if t['name'] == 'search_listings')
        self.assertLessEqual({'query', 'category', 'limit', 'offset'},
                             set(search['inputSchema']['properties']))

    def test_call_preserves_structured_empty_result(self):
        result = {'content': [{'type': 'text', 'text': 'No matches'}],
                  'structuredContent': {'listings': [], 'has_more': False}, 'isError': False}
        reply = self.request('tools/call', {'name': 'search_listings',
            'arguments': {'query': 'chairs', 'category': 'unsupported', 'offset': 20}},
            {'jsonrpc': '2.0', 'id': 1, 'result': result})
        self.assertEqual(reply['result'], result)

    def test_tool_error_stays_error(self):
        result = {'content': [{'type': 'text', 'text': 'Unavailable'}], 'isError': True}
        self.assertEqual(self.request('tools/call', {'name': 'weather'},
            {'jsonrpc': '2.0', 'id': 1, 'result': result})['result'], result)

    def test_unknown_tool_rpc_error_is_preserved(self):
        error = {'code': -32602, 'message': 'Unknown tool', 'data': {'name': 'gone'}}
        self.assertEqual(self.request('tools/call', {'name': 'gone'},
            {'jsonrpc': '2.0', 'id': 1, 'error': error})['error'], error)

    def test_transport_and_invalid_response_fail_closed(self):
        for response in [OSError('offline'), io.BytesIO(b'not-json'),
                         io.BytesIO(b'{"jsonrpc":"2.0","id":1}')]:
            with self.subTest(response=response):
                kwargs = {'side_effect': response} if isinstance(response, Exception) else {'return_value': response}
                with patch.object(proxy.urllib.request, 'urlopen', **kwargs):
                    reply = proxy._handle({'id': 8, 'method': 'tools/call', 'params': {'name': 'search_listings'}})
                self.assertIn('error', reply)
                self.assertNotIn('result', reply)

    def test_interrupted_http_response_does_not_kill_proxy(self):
        with patch.object(proxy.urllib.request, 'urlopen',
                          side_effect=http.client.IncompleteRead(b'partial')):
            try:
                reply = proxy._handle({'id': 10, 'method': 'tools/call', 'params': {'name': 'search_listings'}})
            except http.client.HTTPException:
                self.fail('Interrupted upstream response escaped the proxy')
        self.assertIn('error', reply)
        self.assertEqual(proxy._handle({'id': 11, 'method': 'ping'})['result'], {})


class BuyerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # The SDK decorator is registration only; execute the real buyer function.
        class Registration:
            def __init__(self, name): pass
            def tool(self): return lambda fn: fn
        sdk = types.ModuleType('mcp.server.mcpserver')
        sdk.MCPServer = Registration
        with patch.dict(sys.modules, {'mcp.server.mcpserver': sdk}):
            spec = importlib.util.spec_from_file_location('buyer', ROOT / 'agent.py')
            cls.buyer = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(cls.buyer)

    def test_query_and_category_forwarding(self):
        import httpx
        for category in [None, 'services']:
            captured = []
            def post(url, **kwargs):
                captured.append(kwargs['json']['params']['arguments'])
                return httpx.Response(200, json={'result': {'content': []}},
                                      request=httpx.Request('POST', url))
            with patch.object(self.buyer.httpx, 'post', side_effect=post):
                self.buyer.search_catalog('chairs', **({} if category is None else {'category': category}))
            self.assertEqual(captured, [{'query': 'chairs', 'category': category or ''}])

    def test_large_search_result_remains_valid_json(self):
        import httpx
        result = {'content': [{'type': 'text', 'text': 'x' * 5000}]}
        response = httpx.Response(200, json={'result': result},
                                  request=httpx.Request('POST', 'https://example.com'))
        with patch.object(self.buyer.httpx, 'post', return_value=response):
            text = self.buyer.search_catalog('chairs')
        self.assertEqual(len(text), len(json.dumps(result)))
        self.assertEqual(json.loads(text), result)

    def test_rpc_error_does_not_become_empty_success(self):
        import httpx
        response = httpx.Response(200, json={'error': {'message': 'Unknown tool'}},
                                  request=httpx.Request('POST', 'https://example.com'))
        with patch.object(self.buyer.httpx, 'post', return_value=response):
            with self.assertRaisesRegex(RuntimeError, 'Unknown tool'):
                self.buyer.search_catalog('chairs')


if __name__ == '__main__':
    unittest.main()
