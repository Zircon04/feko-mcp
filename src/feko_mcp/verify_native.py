"""Verify stdio MCP -> CADFEKO -> Solver -> POSTFEKO on a small dipole."""
from pathlib import Path
import argparse
import asyncio
import json
import math
import os
import sys
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from .settings import Settings
ROOT = Settings.load().workspace
DIPOLE = '''local app=cf.Application.GetInstance()
local project=app:NewProject()
local line=project.Contents.Geometry:AddLine(cf.Point(0,0,-0.028),cf.Point(0,0,0.028))
local port=project.Contents.Ports:AddWirePort(line.Wires[1])
port.Location=cf.Enums.WirePortLocationEnum.Middle
local source=project.Contents.SolutionConfigurations.GlobalSources:AddVoltageSource(port)
source.Impedance="50"
project.Contents.SolutionConfigurations.GlobalFrequency.Start="2.4e9"
project.Mesher.Settings.MeshSizeOption=cf.Enums.MeshSizeOptionEnum.Custom
project.Mesher.Settings.WireSegmentLength="0.003"
project.Mesher.Settings.WireRadius="0.0001"
project.Mesher:Mesh()
app:SaveAs("mcp_check.cfx")
'''

def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)

    def seconds(value):
        number = float(value)
        if not math.isfinite(number) or number < 0:
            raise argparse.ArgumentTypeError("Timeout must be finite and nonnegative")
        return number

    parser.add_argument("--stage-timeout", type=seconds, default=120,
                        help="Total seconds per stage (default: 120); 0 disables the stage deadline")
    parser.add_argument("--request-timeout", type=seconds, default=30,
                        help="Seconds per status request (default: 30; must be positive)")
    args = parser.parse_args(argv)
    if args.request_timeout == 0:
        parser.error("--request-timeout must be positive")
    return args


async def wait_stage(call, job_id, stage, timeout=120, poll_interval=0.5, request_timeout=30):
    """Wait for successful completion and cleanup, bounding even stalled MCP calls."""
    status = None
    try:
        async with asyncio.timeout(timeout):
            while True:
                try:
                    async with asyncio.timeout(request_timeout):
                        status = await call("get_job_status", {"job_id": job_id})
                except TimeoutError as exc:
                    raise AssertionError(
                        f"Status request timeout: job={job_id}, stage={stage}: {status}") from exc
                record = next((r for r in status["stages"] if r["stage"] == stage), {})
                if record.get("state") == "failed":
                    raise AssertionError(f"Stage failed: {stage}: {status}")
                if record.get("state") == "completed" and status.get("ready_for_next_stage", False):
                    return
                await asyncio.sleep(poll_interval)
    except TimeoutError as exc:
        raise AssertionError(f"Stage timeout: {stage}: job={job_id}: {status}") from exc


async def main(argv=None):
    args = parse_args(argv)
    stage_timeout = args.stage_timeout or None
    ROOT.mkdir(parents=True, exist_ok=True)
    parameters = StdioServerParameters(command=sys.executable, args=["-m", "feko_mcp"], env=os.environ.copy())
    async with stdio_client(parameters) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = {tool.name for tool in tools.tools}
            assert {"run_cad_script", "start_solver", "run_post_script", "get_job_status", "read_api"} <= names
            async def call(name, args):
                result = await session.call_tool(name, args)
                assert not result.isError, (name, result.content)
                if result.structuredContent is not None:
                    return result.structuredContent
                return json.loads(result.content[0].text)
            environment = await call("get_environment", {})
            assert all(v["exists"] for v in environment["executables"].values())
            reference = await session.call_tool("read_api", {
                "application": "cadfeko", "relative_path": "object/rectangle.htm"})
            assert not reference.isError and "AddRectangle" in reference.content[0].text
            job = await call("run_cad_script", {"script_text": DIPOLE})
            job_id = job["job_id"]
            await wait_stage(call, job_id, "build", timeout=stage_timeout,
                             request_timeout=args.request_timeout)
            await call("start_solver", {"job_id": job_id, "model_filename": "mcp_check.cfx", "cores": 1})
            await wait_stage(call, job_id, "solve", timeout=stage_timeout,
                             request_timeout=args.request_timeout)
            await call("run_post_script", {"job_id": job_id, "model_base": "mcp_check"})
            await wait_stage(call, job_id, "post", timeout=stage_timeout,
                             request_timeout=args.request_timeout)
            artifacts = await call("get_artifacts", {"job_id": job_id})
            filenames = {p["name"] for p in artifacts["files"]}
            assert {"mcp_check.cfx", "mcp_check.bof", "mcp_check_results.csv", "mcp_check.pfs"} <= filenames
            csv_result = await session.call_tool("read_result_text", {
                "job_id": job_id, "filename": "mcp_check_results.csv"})
            assert not csv_result.isError and "2400000000" in csv_result.content[0].text
            report = {"status": "passed", "transport": "stdio", "tools": sorted(names),
                      "job_id": job_id, "checks": ["initialize", "list_tools", "read installed API",
                      "native CADFEKO build", "native FEKO solve", "native POSTFEKO export", "read artifacts"]}
            (ROOT / "verification-native.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
            print(json.dumps(report, indent=2))

if __name__ == "__main__":
    asyncio.run(main())
