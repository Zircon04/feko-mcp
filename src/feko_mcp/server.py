"""Local stdio MCP bridge for CADFEKO, FEKO, and POSTFEKO."""
from pathlib import Path
import html
import json
import math
import re
import subprocess
import sys
import threading
import uuid

from mcp.server.fastmcp import FastMCP
from .settings import EXAMPLES, Settings

ROOT = Path(__file__).resolve().parent
SETTINGS = Settings.load()
DOCS = SETTINGS.api_docs
JOBS = SETTINGS.workspace / "jobs"
JOBS.mkdir(parents=True, exist_ok=True)
mcp = FastMCP("FEKO local bridge", log_level="WARNING", instructions=
    "Use CADFEKO Lua API for construction, a separate solver job for computation, "
    "and POSTFEKO Lua API for exports. Poll job status; do not assume process startup "
    "means successful computation. The supplied antenna example is a preliminary "
    "reconstruction with explicit assumptions in MODEL_NOTES.md.")

def job_directory(job_id):
    if job_id == "example":
        return EXAMPLES
    if not re.fullmatch(r"[a-f0-9]{12}", job_id):
        raise ValueError("Invalid job ID")
    directory = JOBS / job_id
    if not directory.is_dir():
        raise ValueError("Job does not exist")
    return directory

def start_stage(directory, stage, command, files=None):
    for status in directory.glob("status_*.json"):
        record = json.loads(status.read_text(encoding="utf-8"))
        if record["state"] in {"starting", "running"}:
            raise ValueError("This job already has a running stage")
    lock = directory / ".stage.lock"
    try:
        with lock.open("x"):
            pass
    except FileExistsError:
        raise ValueError("This job already has a starting/running stage") from None
    try:
        for filename, content in (files or {}).items():
            (directory / only_filename(filename)).write_text(content, encoding="utf-8")
        request = directory / f"request_{stage}.json"
        request.write_text(json.dumps({"stage": stage, "command": command}), encoding="utf-8")
        (directory / f"status_{stage}.json").write_text(
            json.dumps({"stage": stage, "state": "starting"}), encoding="utf-8")
        with (directory / f"worker_{stage}.log").open("w", encoding="utf-8") as log:
            process = subprocess.Popen([sys.executable, str(ROOT / "worker.py"), str(request)],
                cwd=directory, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            threading.Thread(target=process.wait, daemon=True).start()
    except Exception as exc:
        (directory / f"status_{stage}.json").write_text(
            json.dumps({"stage": stage, "state": "failed", "error": str(exc)}), encoding="utf-8")
        lock.unlink(missing_ok=True)
        raise
    return {"job_id": directory.name, "stage": stage, "state": "starting", "worker_pid": process.pid}

def new_job():
    directory = JOBS / uuid.uuid4().hex[:12]
    directory.mkdir()
    return directory

def only_filename(filename):
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", filename) or filename.endswith("."):
        raise ValueError("Use a simple filename containing letters, numbers, underscores, dots or hyphens")
    if filename.split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL", *[f"COM{i}" for i in range(1,10)], *[f"LPT{i}" for i in range(1,10)]}:
        raise ValueError("Reserved device filename")
    return filename

@mcp.tool()
def get_environment() -> dict:
    """Locate FEKO components, the supplied model and its documented assumptions."""
    return {"installation": str(SETTINGS.installation) if SETTINGS.installation else None,
        "executables": {name: {"path": command, "exists": SETTINGS.executable_exists(name)}
                        for name, command in SETTINGS.commands.items()},
        "workspace": str(SETTINGS.workspace), "max_cores": SETTINGS.max_cores,
        "api_docs": str(DOCS) if DOCS else None,
        "example_directory": str(EXAMPLES), "example_job_id": "example",
        "model_notes": (EXAMPLES / "MODEL_NOTES.md").read_text(encoding="utf-8")}

@mcp.tool()
def run_cad_script(script_text: str, assets: dict[str, str] | None = None) -> dict:
    """Run CADFEKO Lua asynchronously in a new job directory; provide Lua/text assets by filename."""
    command = SETTINGS.require("cadfeko")
    files = {}
    if assets:
        for filename, content in assets.items():
            filename = only_filename(filename)
            if filename.lower() in {"build.lua"} or filename.startswith(("status_", "request_", "worker_")):
                raise ValueError("Reserved asset filename")
            files[filename] = content
    directory = new_job()
    script = directory / "build.lua"
    files["build.lua"] = script_text
    return start_stage(directory, "build", [command, "--non-interactive",
                                            "--run-script", str(script)], files)

@mcp.tool()
def create_preliminary_model(parameters: dict[str, float | str] | None = None) -> dict:
    """Build a fresh parameterised copy of the antenna example. Overrides are in mm/Hz and SI material units."""
    allowed = {"L1","L4","L5","W1","W2","W6","W","L","h","trace_width","radiator_depth",
               "er","tand","feed_overhang","pin_radius","mesh_edge","frequency_start",
               "frequency_end","frequency_count","frequency_mode"}
    parameter_text = ['local p=dofile("defaults.lua")']
    for key, value in (parameters or {}).items():
        if key not in allowed:
            raise ValueError("Unknown parameter: " + key)
        if key == "frequency_mode":
            if value not in {"adaptive", "discrete"}:
                raise ValueError("frequency_mode must be adaptive or discrete")
            expression = '"' + str(value) + '"'
        else:
            value = float(value)
            if not math.isfinite(value) or value < 0 or (value == 0 and key != "tand"):
                raise ValueError("Numeric parameters must be finite and positive; tand may be zero")
            if key == "frequency_count" and (value < 2 or value != int(value)):
                raise ValueError("frequency_count must be an integer of at least 2")
            expression = str(value)
        parameter_text.append(f'p["{key}"]={expression}')
    parameter_text.append("return p")
    return run_cad_script((EXAMPLES / "build_model.lua").read_text(encoding="utf-8"), {
        "defaults.lua": (EXAMPLES / "parameters.lua").read_text(encoding="utf-8"),
        "parameters.lua": "\n".join(parameter_text)})

@mcp.tool()
def start_solver(job_id: str, model_filename: str = "mba_preliminary.cfx", cores: int = min(4, SETTINGS.max_cores)) -> dict:
    """Start FEKO in a completed CAD job. Return immediately, then poll get_job_status."""
    directory = job_directory(job_id)
    if job_id == "example":
        raise ValueError("Use a new job to avoid overwriting the delivered example")
    model_filename = only_filename(model_filename)
    if Path(model_filename).suffix.lower() not in {".cfx", ".pre", ".fek"}:
        raise ValueError("Supported model types: cfx, pre, fek")
    if not (directory / model_filename).is_file():
        raise ValueError("Model file does not exist in this job")
    if isinstance(cores, bool) or not isinstance(cores, int) or not 1 <= cores <= SETTINGS.max_cores:
        raise ValueError(f"cores must be an integer between 1 and {SETTINGS.max_cores}")
    return start_stage(directory, "solve", [SETTINGS.require("runfeko"), model_filename, "-np", str(cores)])

@mcp.tool()
def run_post_script(job_id: str, script_text: str = "", model_base: str = "mba_preliminary") -> dict:
    """Run POSTFEKO Lua in a solved job. An empty script uses the tested S11/impedance exporter."""
    directory = job_directory(job_id)
    if job_id == "example":
        raise ValueError("Use a new job to avoid overwriting the delivered example")
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", model_base):
        raise ValueError("Invalid model base name")
    if not (directory / (model_base + ".bof")).is_file():
        raise ValueError("Solver result bof file does not exist")
    script = directory / "export.lua"
    return start_stage(directory, "post", [SETTINGS.require("postfeko"), "--non-interactive", "--run-script",
        str(script), "--configure-script", 'model_base="' + model_base + '"'],
        {"export.lua": script_text or (EXAMPLES / "export_results.lua").read_text(encoding="utf-8")})

@mcp.tool()
def get_job_status(job_id: str) -> dict:
    """Read stage status, next-stage readiness, and recent FEKO diagnostics."""
    directory = job_directory(job_id)
    statuses = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(directory.glob("status_*.json"))]
    logs = {}
    for p in directory.glob("*.log"):
        logs[p.name] = p.read_text(encoding="utf-8", errors="replace")[-5000:].replace("\b", "")
    # Read the lock after statuses: terminal publication precedes worker cleanup.
    # This is a polling snapshot, not a reservation; start_stage still owns admission.
    ready = bool(statuses) and all(r.get("state") in {"completed", "failed"} for r in statuses) \
        and not (directory / ".stage.lock").exists()
    return {"job_id": job_id, "directory": str(directory), "stages": statuses,
            "log_tails": logs, "ready_for_next_stage": ready}

@mcp.tool()
def get_artifacts(job_id: str = "example") -> dict:
    """List model, result, table and preview files produced by the job."""
    directory = job_directory(job_id)
    extensions = {".cfx", ".fek", ".bof", ".pfs", ".csv", ".s1p", ".png", ".out", ".md", ".json"}
    return {"job_id": job_id, "files": [{"name": p.name, "path": str(p), "bytes": p.stat().st_size}
        for p in sorted(directory.iterdir()) if p.is_file() and p.suffix.lower() in extensions]}

@mcp.tool()
def read_result_text(job_id: str, filename: str, max_chars: int = 16000) -> str:
    """Read exported CSV, Touchstone, notes or diagnostics for analysis in the assistant."""
    path = job_directory(job_id) / only_filename(filename)
    if path.suffix.lower() not in {".csv", ".s1p", ".md", ".json", ".txt", ".out", ".log"}:
        raise ValueError("Use get_artifacts for binary files")
    return path.read_text(encoding="utf-8", errors="replace")[:max(1, min(max_chars, 40000))]

@mcp.tool()
def read_api(application: str, relative_path: str, search_term: str = "") -> str:
    """Read installed CADFEKO/POSTFEKO 2026.1 API, e.g. application=cadfeko, relative_path=object/rectangle.htm."""
    if application not in {"cadfeko", "postfeko"}:
        raise ValueError("application must be cadfeko or postfeko")
    if DOCS is None:
        raise FileNotFoundError("API documentation not found. Set FEKO_HOME or FEKO_MCP_API_DOCS.")
    root = (DOCS / ("api_" + application + "_auto_generated")).resolve()
    path = (root / relative_path).resolve()
    if not path.is_relative_to(root) or path.suffix.lower() != ".htm":
        raise ValueError("Path must point inside the installed API documentation")
    raw = path.read_text(encoding="utf-8")
    body = re.search(r"<article.*?</article>", raw, re.S)
    text = html.unescape(re.sub(r"<[^>]+>", " ", body[0] if body else raw))
    text = re.sub(r"\s+", " ", text)
    if search_term:
        hits = list(re.finditer(re.escape(search_term), text, re.I))
        return "\n".join(text[max(0,m.start()-80):m.start()+1600] for m in hits[:8]) or "No matches"
    return text[:24000]

if __name__ == "__main__":
    mcp.run(transport="stdio")
