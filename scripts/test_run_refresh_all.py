#!/usr/bin/env python3
"""Checks for the single-machine refresh runner."""
import os
import tempfile
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


if __name__ == "__main__":
    unittest.main()
