import asyncio
import json
import os
import sys
import tempfile
import unittest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

class StdioTests(unittest.TestCase):
    def test_tools_and_example_notes_without_feko(self):
        async def check():
            with tempfile.TemporaryDirectory() as temporary:
                environment = {**os.environ, "FEKO_MCP_WORKSPACE": temporary}
                parameters = StdioServerParameters(command=sys.executable, args=["-m", "feko_mcp"], env=environment)
                async with stdio_client(parameters) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        tools = await session.list_tools()
                        self.assertEqual({tool.name for tool in tools.tools}, {
                            "get_environment", "read_api", "run_cad_script", "create_preliminary_model",
                            "start_solver", "get_job_status", "run_post_script", "get_artifacts", "read_result_text"})
                        result = await session.call_tool("read_result_text", {"job_id": "example", "filename": "MODEL_NOTES.md"})
                        self.assertFalse(result.isError)
                        self.assertIn("not reproduced", result.content[0].text)
                        status = await session.call_tool("get_job_status", {"job_id": "example"})
                        self.assertFalse(status.isError)
                        payload = status.structuredContent
                        if payload is None:
                            payload = json.loads(status.content[0].text)
                        self.assertIs(payload["ready_for_next_stage"], False)
                        self.assertEqual(payload["stages"], [])
        asyncio.run(asyncio.wait_for(check(), timeout=30))
