"""Detached FEKO job worker: keep progress available while MCP serves requests."""
from pathlib import Path
import datetime
import json
import subprocess
import sys
import traceback

request_file = Path(sys.argv[1]).resolve()
request = json.loads(request_file.read_text(encoding="utf-8"))
directory = request_file.parent
stage = request["stage"]
status_file = directory / f"status_{stage}.json"

def write_status(state, **extra):
    record = {"stage": stage, "state": state,
              "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(), **extra}
    temporary = status_file.with_suffix(".tmp")
    temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(status_file)

write_status("running")
try:
    with (directory / f"{stage}.log").open("w", encoding="utf-8") as log:
        proc = subprocess.Popen(request["command"], cwd=directory, stdout=log,
                                stderr=subprocess.STDOUT,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        write_status("running", pid=proc.pid)
        code = proc.wait()
    write_status("completed" if code == 0 else "failed", exit_code=code)
except Exception as exc:
    (directory / f"{stage}.log").write_text(traceback.format_exc(), encoding="utf-8")
    write_status("failed", error=str(exc))
finally:
    (directory / ".stage.lock").unlink(missing_ok=True)
