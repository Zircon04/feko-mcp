import importlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        with patch.dict(os.environ, {"FEKO_MCP_WORKSPACE": cls.temporary.name}):
            cls.server = importlib.import_module("feko_mcp.server")

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def test_reject_path_traversal_and_device_names(self):
        for name in ("../secret", "..\\secret", "C:secret", "/secret", "CON.txt", "NUL", "file.", ""):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.server.only_filename(name)
        self.assertEqual(self.server.only_filename("model_1.cfx"), "model_1.cfx")
        with self.assertRaises(ValueError):
            self.server.job_directory("../example")

    def test_parameter_validation_before_launch(self):
        for values in ({"mesh_edge": float("nan")}, {"tand": -1}, {"frequency_count": 2.5},
                       {"frequency_mode": "unknown"}, {"not_a_parameter": 1}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                self.server.create_preliminary_model(values)

    def test_api_cannot_escape_documentation(self):
        with tempfile.TemporaryDirectory() as temporary:
            docs = Path(temporary)
            with patch.object(self.server, "DOCS", docs):
                with self.assertRaises(ValueError):
                    self.server.read_api("cadfeko", "../../secret.htm")

    def test_worker_records_success_and_failure(self):
        for code in (0, 7):
            with self.subTest(code=code):
                directory = self.server.new_job()
                processes = []
                real_popen = self.server.subprocess.Popen
                def launch(*args, **kwargs):
                    process = real_popen(*args, **kwargs)
                    processes.append(process)
                    return process
                with patch.object(self.server.subprocess, "Popen", side_effect=launch):
                    started = self.server.start_stage(directory, "solve", [sys.executable, "-c", f"print('diagnostic'); raise SystemExit({code})"])
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    record = json.loads((directory / "status_solve.json").read_text())
                    if record["state"] in {"completed", "failed"} and not (directory / ".stage.lock").exists():
                        break
                    time.sleep(0.05)
                self.assertEqual(record["state"], "completed" if code == 0 else "failed")
                self.assertEqual(record["exit_code"], code)
                # A terminal stage records FEKO completion; the worker itself
                # still needs to close its redirected log before Windows cleanup.
                processes[0].wait(timeout=15)
                self.assertIn("diagnostic", (directory / "solve.log").read_text())
                self.assertEqual(started["job_id"], directory.name)

    def test_busy_job_preserves_files(self):
        directory = self.server.new_job()
        (directory / ".stage.lock").touch()
        (directory / "export.lua").write_text("original")
        with self.assertRaises(ValueError):
            self.server.start_stage(directory, "post", [sys.executable], {"export.lua": "replacement"})
        self.assertEqual((directory / "export.lua").read_text(), "original")

    def test_readiness_requires_terminal_statuses_and_no_lock(self):
        for state in ("completed", "failed", "starting", "running"):
            for locked in (True, False):
                with self.subTest(state=state, locked=locked):
                    directory = self.server.new_job()
                    record = {"stage": "solve", "state": state}
                    (directory / "status_solve.json").write_text(json.dumps(record))
                    if locked:
                        (directory / ".stage.lock").touch()
                    result = self.server.get_job_status(directory.name)
                    self.assertEqual(result["ready_for_next_stage"],
                                     state in {"completed", "failed"} and not locked)
                    self.assertEqual(result["stages"], [record])
                    self.assertEqual((directory / ".stage.lock").exists(), locked)

    def test_empty_and_mixed_job_not_ready(self):
        directory = self.server.new_job()
        self.assertFalse(self.server.get_job_status(directory.name)["ready_for_next_stage"])
        for stage, state in (("build", "completed"), ("solve", "running")):
            (directory / f"status_{stage}.json").write_text(
                json.dumps({"stage": stage, "state": state}))
        self.assertFalse(self.server.get_job_status(directory.name)["ready_for_next_stage"])

    def test_terminal_status_with_lock_rejects_start_until_cleanup(self):
        directory = self.server.new_job()
        (directory / "status_solve.json").write_text(json.dumps(
            {"stage": "solve", "state": "completed", "exit_code": 0}))
        lock = directory / ".stage.lock"
        lock.touch()
        script = directory / "export.lua"
        script.write_text("original")
        with patch.object(self.server.subprocess, "Popen") as launch, \
                patch.object(self.server.threading, "Thread"):
            self.assertFalse(self.server.get_job_status(directory.name)["ready_for_next_stage"])
            with self.assertRaisesRegex(ValueError, "starting/running stage"):
                self.server.start_stage(directory, "post", ["fake-post"],
                                        {"export.lua": "replacement"})
            launch.assert_not_called()
            self.assertEqual(script.read_text(), "original")
            self.assertFalse((directory / "request_post.json").exists())
            self.assertFalse((directory / "status_post.json").exists())
            lock.unlink()
            self.assertTrue(self.server.get_job_status(directory.name)["ready_for_next_stage"])
            started = self.server.start_stage(directory, "post", ["fake-post"],
                                              {"export.lua": "replacement"})
            launch.assert_called_once()
            self.assertEqual(started["state"], "starting")
            self.assertEqual(script.read_text(), "replacement")
            self.assertTrue(lock.exists())
            self.assertFalse(self.server.get_job_status(directory.name)["ready_for_next_stage"])

    def test_solver_uses_basename_and_configurable_core_limit(self):
        directory = self.server.new_job()
        (directory / "model.cfx").touch()
        with patch.object(self.server.SETTINGS.__class__, "require", return_value="runfeko"), patch.object(self.server, "start_stage") as start:
            self.server.start_solver(directory.name, "model.cfx", 1)
            self.assertEqual(start.call_args.args[2], ["runfeko", "model.cfx", "-np", "1"])
        with self.assertRaises(ValueError):
            self.server.start_solver(directory.name, "model.cfx", self.server.SETTINGS.max_cores + 1)
