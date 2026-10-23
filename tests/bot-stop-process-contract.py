#!/usr/bin/env python3
"""Offline stop contracts. Real Linux children are inert Python sleepers.

No account files, cookies, workers, platform APIs or network are involved.
"""
from __future__ import annotations

import os
import selectors
import signal
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
import bot_manager


class StopSignalContract(unittest.TestCase):
    def test_popen_none_return_waits_for_confirmed_exit(self):
        proc = Mock()
        proc.poll.side_effect = [None, 0]
        proc.send_signal.return_value = None
        proc.wait.return_value = 0
        proc.identity_matches.return_value = True
        self.assertEqual(bot_manager._terminate_process(proc), (True, "stopped"))
        proc.send_signal.assert_called_once_with(signal.SIGTERM)
        proc.wait.assert_called_once_with(timeout=15)

    def test_adopted_explicit_false_is_not_treated_as_success(self):
        proc = Mock()
        proc.poll.return_value = None
        proc.identity_matches.return_value = True
        proc.send_signal.return_value = False
        self.assertEqual(bot_manager._terminate_process(proc), (False, "stop_failed"))
        proc.wait.assert_not_called()

    def test_unconfirmed_exit_after_sigkill_remains_failed(self):
        proc = Mock()
        proc.poll.return_value = None
        proc.identity_matches.return_value = True
        proc.send_signal.return_value = None
        proc.wait.side_effect = subprocess.TimeoutExpired(["synthetic-sleeper"], 5)
        self.assertEqual(bot_manager._terminate_process(proc), (False, "stop_timeout"))
        self.assertEqual([call.args[0] for call in proc.send_signal.call_args_list], [signal.SIGTERM, signal.SIGKILL])
        self.assertEqual(proc.wait.call_count, 2)


@unittest.skipUnless(sys.platform.startswith("linux"), "Real stop integration runs inside Linux")
class LinuxProcessStopContract(unittest.TestCase):
    def spawn_sleeper(self, *, ignore_term=False):
        code = "import signal,time; "
        if ignore_term:
            code += "signal.signal(signal.SIGTERM,signal.SIG_IGN); "
        code += "print('synthetic-ready',flush=True); time.sleep(60)"
        proc = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, start_new_session=True, text=True)
        def cleanup():
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=5)
            if proc.stdout:
                proc.stdout.close()
        self.addCleanup(cleanup)
        with selectors.DefaultSelector() as selector:
            selector.register(proc.stdout, selectors.EVENT_READ)
            self.assertTrue(selector.select(timeout=5), "Synthetic child did not initialize")
            self.assertEqual(proc.stdout.readline().strip(), "synthetic-ready")
        return proc

    def test_real_popen_sigterm_clears_supervisor_transition(self):
        proc = self.spawn_sleeper()
        key = (902, "default")
        with bot_manager._lock:
            bot_manager._procs[key] = proc
            bot_manager._modes[key] = "rules"
        def cleanup_state():
            with bot_manager._lock:
                for mapping in (bot_manager._procs, bot_manager._modes, bot_manager._transitions,
                                bot_manager._desired_running, bot_manager._generations):
                    mapping.pop(key, None)
        self.addCleanup(cleanup_state)
        with patch.object(bot_manager, "revoke_token"):
            self.assertEqual(bot_manager.stop(902), (True, "stopped"))
        self.assertEqual(proc.returncode, -signal.SIGTERM)
        self.assertNotIn(key, bot_manager._transitions)
        self.assertFalse(bot_manager.is_running(902))

    def test_real_popen_ignored_sigterm_escalates_and_confirms_sigkill(self):
        proc = self.spawn_sleeper(ignore_term=True)
        self.assertEqual(bot_manager._terminate_process(proc), (True, "stopped"))
        self.assertEqual(proc.returncode, -signal.SIGKILL)
        self.assertIsNotNone(proc.poll())

    @unittest.skipUnless(hasattr(os, "pidfd_open") and hasattr(signal, "pidfd_send_signal"), "pidfd unavailable")
    def test_real_adopted_pidfd_retains_boolean_signal_contract(self):
        proc = self.spawn_sleeper()
        identity = bot_manager._proc_start_time(proc.pid)
        self.assertTrue(identity)
        adopted = bot_manager._AdoptedProcess(proc.pid, identity, os.pidfd_open(proc.pid))
        self.addCleanup(adopted._close_pidfd)
        self.assertEqual(bot_manager._terminate_process(adopted), (True, "stopped"))
        proc.wait(timeout=5)
        self.assertEqual(proc.returncode, -signal.SIGTERM)
        self.assertIsNotNone(adopted.poll())


if __name__ == "__main__":
    unittest.main()
