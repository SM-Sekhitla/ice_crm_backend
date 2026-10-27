"""Process supervision tests. No child processes or network listeners are started."""

import contextlib
import io
import signal
import subprocess
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import run_backend as runner


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.api = Mock(pid=900001)
        self.api.poll.return_value = None
        self.worker = Mock(pid=900002)
        self.worker.poll.return_value = None
        self.output = contextlib.redirect_stdout(io.StringIO())
        self.output.__enter__()
        self.addCleanup(self.output.__exit__, None, None, None)

    def test_starts_worker_after_api_readiness_and_stops_both_on_worker_failure(self):
        events = []

        def start(command):
            events.append("worker" if "app.services.worker" in command else "api")
            return self.worker if "app.services.worker" in command else self.api

        def ready(*args):
            events.append("ready")

        self.worker.poll.return_value = 2
        with (
            patch.object(runner, "start_service", side_effect=start),
            patch.object(runner, "wait_for_api", side_effect=ready),
            patch.object(runner, "stop_services") as stop,
            patch.object(runner.signal, "signal", return_value=signal.SIG_DFL),
        ):
            with self.assertRaisesRegex(RuntimeError, "Mail worker exited with code 2"):
                runner.supervise_services(Path("python"), "127.0.0.1", "5002")
        self.assertEqual(events, ["api", "ready", "worker"])
        stop.assert_called_once_with([self.api, self.worker])

    def test_api_startup_failure_never_starts_worker(self):
        with (
            patch.object(runner, "start_service", return_value=self.api) as start,
            patch.object(runner, "wait_for_api", side_effect=RuntimeError("not ready")),
            patch.object(runner, "stop_services") as stop,
            patch.object(runner.signal, "signal", return_value=signal.SIG_DFL),
        ):
            with self.assertRaisesRegex(RuntimeError, "not ready"):
                runner.supervise_services(Path("python"), "127.0.0.1", "5002")
        start.assert_called_once()
        stop.assert_called_once_with([self.api])

    def test_ctrl_c_cleans_up_both_processes_and_restores_signal_handlers(self):
        with (
            patch.object(runner, "start_service", side_effect=[self.api, self.worker]),
            patch.object(runner, "wait_for_api"),
            patch.object(runner, "stop_services") as stop,
            patch.object(runner.time, "sleep", side_effect=KeyboardInterrupt),
            patch.object(runner.signal, "signal", return_value=signal.SIG_DFL) as handlers,
        ):
            with self.assertRaises(KeyboardInterrupt):
                runner.supervise_services(Path("python"), "127.0.0.1", "5002")
        stop.assert_called_once_with([self.api, self.worker])
        self.assertIn(((signal.SIGINT, signal.SIG_DFL),), [(call.args,) for call in handlers.call_args_list])
        self.assertIn(((signal.SIGTERM, signal.SIG_DFL),), [(call.args,) for call in handlers.call_args_list])

    def test_worker_spawn_failure_stops_api(self):
        with (
            patch.object(runner, "start_service", side_effect=[self.api, OSError("spawn failed")]),
            patch.object(runner, "wait_for_api"),
            patch.object(runner, "stop_services") as stop,
            patch.object(runner.signal, "signal", return_value=signal.SIG_DFL),
        ):
            with self.assertRaises(OSError):
                runner.supervise_services(Path("python"), "127.0.0.1", "5002")
        stop.assert_called_once_with([self.api])

    def test_reload_process_group_is_isolated_and_cleaned_up(self):
        with (
            patch.object(runner.os, "name", "posix"),
            patch.object(runner.subprocess, "Popen", return_value=self.api) as spawn,
        ):
            runner.start_service(["python", "-m", "uvicorn"])
        self.assertTrue(spawn.call_args.kwargs["start_new_session"])
        with patch.object(runner.os, "name", "posix"), patch.object(runner.os, "killpg") as kill:
            runner.signal_service(self.api)
            runner.signal_service(self.api, force=True)
        self.assertEqual(kill.call_args_list[0].args, (self.api.pid, signal.SIGTERM))
        self.assertEqual(kill.call_args_list[1].args, (self.api.pid, signal.SIGKILL))

    def test_unresponsive_child_is_force_stopped(self):
        self.api.wait.side_effect = [subprocess.TimeoutExpired("api", 10), 0]
        with patch.object(runner, "signal_service") as signal_child:
            runner.stop_services([self.api])
        self.assertEqual(signal_child.call_count, 2)
        self.assertEqual(signal_child.call_args.kwargs, {"force": True})

    def test_readiness_uses_a_direct_local_request_without_a_proxy(self):
        response = Mock(status=200)
        context = Mock()
        context.__enter__ = Mock(return_value=response)
        context.__exit__ = Mock(return_value=False)
        opener = Mock()
        opener.open.return_value = context
        with patch.object(runner, "build_opener", return_value=opener):
            runner.wait_for_api(self.api, "0.0.0.0", "5002")
        opener.open.assert_called_once_with("http://127.0.0.1:5002/health/ready", timeout=1)

    def test_readiness_timeout_is_reported(self):
        with (
            patch.object(runner, "build_opener"),
            patch.object(runner.time, "monotonic", side_effect=[0, 31]),
        ):
            with self.assertRaisesRegex(RuntimeError, "did not become ready"):
                runner.wait_for_api(self.api, "127.0.0.1", "5002")
