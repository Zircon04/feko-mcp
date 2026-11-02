import json
from pathlib import Path
import runpy
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch


WORKER = Path(__file__).resolve().parents[1] / "src/feko_mcp/worker.py"


class WorkerTests(unittest.TestCase):
    def check_worker(self, exit_code=None, launch_error=None):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            request = directory / "request_solve.json"
            request.write_text(json.dumps({"stage": "solve", "command": ["fake-solver"]}),
                               encoding="utf-8")
            lock = directory / ".stage.lock"
            lock.touch()
            status = directory / "status_solve.json"
            cleanup_records = []
            real_unlink = Path.unlink

            def observe_cleanup(path, *args, **kwargs):
                if path == lock:
                    cleanup_records.append(json.loads(status.read_text(encoding="utf-8")))
                return real_unlink(path, *args, **kwargs)

            process = Mock(pid=1234)
            process.wait.return_value = exit_code
            with patch.object(sys, "argv", [str(WORKER), str(request)]), \
                    patch("subprocess.Popen", return_value=process,
                          side_effect=launch_error) as launch, \
                    patch.object(Path, "unlink", observe_cleanup):
                runpy.run_path(str(WORKER), run_name="__main__")

            record = json.loads(status.read_text(encoding="utf-8"))
            expected_state = "completed" if launch_error is None and exit_code == 0 else "failed"
            self.assertEqual(record["state"], expected_state)
            self.assertEqual(record["stage"], "solve")
            self.assertFalse(lock.exists())
            launch.assert_called_once()
            if launch_error is None:
                process.wait.assert_called_once_with()
                self.assertEqual(record["exit_code"], exit_code)
            else:
                process.wait.assert_not_called()
                self.assertEqual(record["error"], str(launch_error))
                self.assertIn(str(launch_error), (directory / "solve.log").read_text(encoding="utf-8"))
            # Every cleanup must see terminal status, and there must be no
            # second unlink that could remove a successor stage's lock.
            self.assertEqual(cleanup_records, [record])

    def test_success_publishes_terminal_status_before_single_cleanup(self):
        self.check_worker(exit_code=0)

    def test_nonzero_exit_publishes_terminal_status_before_single_cleanup(self):
        self.check_worker(exit_code=7)

    def test_launch_failure_publishes_terminal_status_before_single_cleanup(self):
        self.check_worker(launch_error=OSError("solver launch failed"))
