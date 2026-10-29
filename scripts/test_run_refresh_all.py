#!/usr/bin/env python3
"""Checks for the single-machine refresh runner."""
import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path

import run_refresh_all as runner


class EnvFileTests(unittest.TestCase):
    def write(self, text):
        handle = tempfile.NamedTemporaryFile("w", suffix=".env", delete=False, encoding="utf-8")
        handle.write(text)
        handle.close()
        self.addCleanup(os.unlink, handle.name)
        return handle.name

    def test_plain_pairs(self):
        values = runner.load_env_file(self.write("CRM_USER=ben\nCRM_PASS=secret\n"))
        self.assertEqual(values, {"CRM_USER": "ben", "CRM_PASS": "secret"})

    def test_comments_blanks_and_export_prefix(self):
        values = runner.load_env_file(self.write("# comment\n\nexport CX_USER=cx\nCX_PASS = pw \n"))
        self.assertEqual(values, {"CX_USER": "cx", "CX_PASS": "pw"})

    def test_quotes_are_stripped(self):
        values = runner.load_env_file(self.write('LARK_APP_ID="abc"\nLARK_APP_SECRET=\'def\'\n'))
        self.assertEqual(values, {"LARK_APP_ID": "abc", "LARK_APP_SECRET": "def"})

    def test_line_without_equals_is_rejected(self):
        with self.assertRaises(SystemExit):
            runner.load_env_file(self.write("CRM_USER ben\n"))


class ChildEnvTests(unittest.TestCase):
    def test_forces_utf8_for_children(self):
        env = runner.child_env({"CRM_USER": "ben"})
        self.assertEqual(env["PYTHONUTF8"], "1")
        self.assertEqual(env["PYTHONIOENCODING"], "utf-8")
        self.assertEqual(env["CRM_USER"], "ben")

    def test_existing_env_is_preserved(self):
        os.environ["X5_TEST_MARKER"] = "kept"
        self.addCleanup(os.environ.pop, "X5_TEST_MARKER", None)
        self.assertEqual(runner.child_env({})["X5_TEST_MARKER"], "kept")


class PathTests(unittest.TestCase):
    def test_repo_root_is_parent_of_scripts(self):
        # The checkout directory name differs per machine; only the layout matters.
        self.assertTrue((runner.ROOT / "scripts" / "run_refresh_all.py").exists())
        self.assertTrue((runner.ROOT / "scripts" / "sync_x5_daily_gross.py").exists())

    def test_published_files_are_the_two_deployables(self):
        self.assertEqual(runner.DATA_FILE, "outputs/lark-dashboard-data.js")
        self.assertIn("outputs/", runner.HTML_FILE)
        self.assertTrue(runner.HTML_FILE.endswith(".html"))


class LockTests(unittest.TestCase):
    """The lock keeps two windows from refreshing into Lark and git at once."""

    def setUp(self):
        workdir = tempfile.mkdtemp(prefix="x5-lock-")
        self.addCleanup(shutil.rmtree, workdir, True)
        self.saved = (runner.LOCK_FILE, runner.STATUS_FILE)
        runner.LOCK_FILE = Path(workdir) / ".refresh.lock"
        runner.STATUS_FILE = Path(workdir) / "REFRESH-STATUS.md"
        self.addCleanup(self.restore)

    def restore(self):
        runner.LOCK_FILE, runner.STATUS_FILE = self.saved

    def test_second_acquire_refuses_while_holder_is_alive(self):
        runner.acquire_lock()
        self.addCleanup(runner.release_lock)
        with self.assertRaises(SystemExit) as raised:
            runner.acquire_lock()
        self.assertIn("already running", str(raised.exception))

    def test_dead_pid_lock_is_taken_over(self):
        runner.LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
        runner.LOCK_FILE.write_text(json.dumps({"pid": 999999, "phase": "stale"}), encoding="utf-8")
        runner.acquire_lock()
        self.addCleanup(runner.release_lock)
        self.assertEqual(json.loads(runner.LOCK_FILE.read_text(encoding="utf-8"))["pid"], os.getpid())

    def test_release_leaves_a_foreign_lock_alone(self):
        runner.LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
        runner.LOCK_FILE.write_text(json.dumps({"pid": 999999}), encoding="utf-8")
        runner.release_lock()
        self.assertTrue(runner.LOCK_FILE.exists())

    def test_update_phase_only_by_holder(self):
        runner.acquire_lock()
        self.addCleanup(runner.release_lock)
        runner.update_lock("sync channel to Lark")
        self.assertEqual(json.loads(runner.LOCK_FILE.read_text(encoding="utf-8"))["phase"],
                         "sync channel to Lark")

    def test_fresh_unreadable_lock_is_not_stolen(self):
        runner.LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
        runner.LOCK_FILE.write_bytes(b'{"pid": 12')  # half-written payload
        with self.assertRaises(SystemExit) as raised:
            runner.acquire_lock()
        self.assertIn("unreadable", str(raised.exception))

    def test_old_unreadable_lock_is_taken_over(self):
        runner.LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
        runner.LOCK_FILE.write_bytes(b"not json at all")
        old = time.time() - 3600
        os.utime(runner.LOCK_FILE, (old, old))
        runner.acquire_lock()
        self.addCleanup(runner.release_lock)
        self.assertEqual(json.loads(runner.LOCK_FILE.read_text(encoding="utf-8"))["pid"], os.getpid())

    def test_bom_prefixed_lock_is_understood(self):
        runner.LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
        runner.LOCK_FILE.write_bytes(b"\xef\xbb\xbf" + json.dumps({"pid": 999999}).encode())
        runner.acquire_lock()  # a dead pid is still a readable lock → takeover
        self.addCleanup(runner.release_lock)
        self.assertEqual(json.loads(runner.LOCK_FILE.read_text(encoding="utf-8"))["pid"], os.getpid())


class StatusFileTests(unittest.TestCase):
    def setUp(self):
        workdir = tempfile.mkdtemp(prefix="x5-status-")
        self.addCleanup(shutil.rmtree, workdir, True)
        self.saved = runner.STATUS_FILE
        runner.STATUS_FILE = Path(workdir) / "REFRESH-STATUS.md"
        self.addCleanup(self.restore)

    def restore(self):
        runner.STATUS_FILE = self.saved

    def test_current_block_and_history_accumulate(self):
        runner.write_status("running", pid=10, phase="sync gross to Lark", log="logs/a.log")
        text = runner.STATUS_FILE.read_text(encoding="utf-8")
        self.assertIn("状态：**running**", text)
        self.assertIn("sync gross to Lark", text)

        runner.write_status("idle", record="| a | b | 成功 | df820c8 | t | n |")
        text = runner.STATUS_FILE.read_text(encoding="utf-8")
        self.assertIn("状态：**idle**", text)
        self.assertIn("df820c8", text)

        # A later run must not drop the earlier history.
        runner.write_status("running", pid=11, phase="export Lark data")
        runner.write_status("idle", record="| c | d | 成功 | e1 | t2 | n2 |")
        text = runner.STATUS_FILE.read_text(encoding="utf-8")
        self.assertIn("df820c8", text)
        self.assertIn("| e1 |", text)
        self.assertIn("状态：**idle**", text)


if __name__ == "__main__":
    unittest.main()
