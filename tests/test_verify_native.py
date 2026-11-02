import asyncio
import unittest
import contextlib
import io
from unittest.mock import AsyncMock, patch

from feko_mcp.verify_native import parse_args, wait_stage


class TimeoutArgumentsTests(unittest.TestCase):
    def test_defaults_and_configurable_deadlines(self):
        defaults = parse_args([])
        self.assertEqual((defaults.stage_timeout, defaults.request_timeout), (120, 30))
        configured = parse_args(["--stage-timeout", "3600", "--request-timeout", "45"])
        self.assertEqual((configured.stage_timeout, configured.request_timeout), (3600, 45))
        self.assertEqual(parse_args(["--stage-timeout", "0"]).stage_timeout, 0)

    def test_invalid_deadlines(self):
        for flag, value in (("--stage-timeout", "-1"), ("--stage-timeout", "nan"),
                            ("--request-timeout", "0"), ("--request-timeout", "inf"),
                            ("--request-timeout", "-1")):
            with self.subTest(flag=flag, value=value), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    parse_args([flag, value])


class WaitStageTests(unittest.IsolatedAsyncioTestCase):
    def status(self, state, ready):
        return {"stages": [{"stage": "build", "state": state}],
                "ready_for_next_stage": ready, "log_tails": {"build.log": "diagnostic"}}

    async def test_handoff_waits_for_cleanup(self):
        call = AsyncMock(side_effect=[self.status("completed", False),
                                     self.status("completed", True), {}])
        with patch("feko_mcp.verify_native.asyncio.sleep", new_callable=AsyncMock) as sleep:
            await wait_stage(call, "job", "build")
            await call("start_solver", {"job_id": "job"})
        self.assertEqual([c.args[0] for c in call.await_args_list],
                         ["get_job_status", "get_job_status", "start_solver"])
        sleep.assert_awaited_once_with(0.5)

    async def test_configured_and_disabled_deadline_keep_request_bound(self):
        for deadline in (3600, None):
            with self.subTest(deadline=deadline):
                call = AsyncMock(side_effect=[self.status("running", False),
                                             self.status("completed", False),
                                             self.status("completed", True)])
                real_timeout = asyncio.timeout
                with patch("feko_mcp.verify_native.asyncio.timeout", wraps=real_timeout) as timer, \
                        patch("feko_mcp.verify_native.asyncio.sleep", new_callable=AsyncMock):
                    await wait_stage(call, "job", "build", timeout=deadline, request_timeout=45)
                self.assertEqual([c.args[0] for c in timer.call_args_list],
                                 [deadline, 45, 45, 45])
                self.assertEqual(call.await_count, 3)

    async def test_request_timeout_with_or_without_total_deadline(self):
        for deadline in (120, None):
            with self.subTest(deadline=deadline):
                cancelled = asyncio.Event()

                async def stalled_call(*args):
                    try:
                        await asyncio.Future()
                    finally:
                        cancelled.set()

                with self.assertRaisesRegex(AssertionError, "Status request timeout: job=job, stage=build"):
                    await wait_stage(stalled_call, "job", "build", timeout=deadline, request_timeout=0)
                self.assertTrue(cancelled.is_set())

    async def test_failure_stops_immediately_with_or_without_lock(self):
        for ready in (False, True):
            with self.subTest(ready=ready):
                call = AsyncMock(return_value=self.status("failed", ready))
                with patch("feko_mcp.verify_native.asyncio.sleep", new_callable=AsyncMock) as sleep:
                    with self.assertRaisesRegex(AssertionError, "Stage failed: build:.*diagnostic"):
                        await wait_stage(call, "job", "build")
                call.assert_awaited_once()
                sleep.assert_not_awaited()

    async def test_missing_readiness_does_not_advance(self):
        old_status = self.status("completed", False)
        del old_status["ready_for_next_stage"]
        call = AsyncMock(side_effect=[old_status, self.status("completed", True)])
        with patch("feko_mcp.verify_native.asyncio.sleep", new_callable=AsyncMock) as sleep:
            await wait_stage(call, "job", "build")
        self.assertEqual(call.await_count, 2)
        sleep.assert_awaited_once()

    async def test_timeout_bounds_stalled_poll(self):
        async def stalled_call(*args):
            await asyncio.Future()

        with self.assertRaisesRegex(AssertionError, "Stage timeout: build"):
            await wait_stage(stalled_call, "job", "build", timeout=0)

    async def test_timeout_bounds_completed_stage_with_stuck_lock(self):
        call = AsyncMock(return_value=self.status("completed", False))
        with self.assertRaisesRegex(AssertionError, "Stage timeout: build:.*completed"):
            await wait_stage(call, "job", "build", timeout=0, poll_interval=0)
        call.assert_awaited_once()
