"""Interop against the official MCP 1.x Python client (test dependency only)."""
import asyncio
import json
import sys
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from test_router import RouterTests


import unittest


class OfficialMCPTests(unittest.TestCase):
    setUp = RouterTests.setUp
    tearDown = RouterTests.tearDown
    def test_official_sdk_initialize_and_generate(self):
        async def run():
            params = StdioServerParameters(command=sys.executable, args=['-m', 'aimp_router.mcp',
                '--token-file', str(self.token), '--url', self.client.base_url])
            async with stdio_client(params) as (reader, writer):
                async with ClientSession(reader, writer) as session:
                    info = await session.initialize()
                    self.assertEqual(info.serverInfo.name, 'aimp-model-router')
                    tools = await session.list_tools()
                    self.assertEqual(len(tools.tools), 4)
                    result = await session.call_tool('generate', {'messages': self.messages})
                    self.assertFalse(result.isError)
                    self.assertEqual(json.loads(result.content[0].text)['choices'][0]['message']['content'], 'ok')
                    result = await session.call_tool('preview_route', {'messages': self.messages, 'routing': {'tier': 'simple'}})
                    self.assertEqual(json.loads(result.content[0].text)['candidates'][0], 'DeepSeek Flash')
        asyncio.run(run())
