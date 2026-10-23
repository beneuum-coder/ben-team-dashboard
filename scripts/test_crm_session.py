import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from sync_x5_daily_gross import crm_login


class CrmSessionTests(unittest.TestCase):
    def test_private_cookie_file_is_reused_by_another_process(self):
        with tempfile.TemporaryDirectory() as directory:
            cookie_file = Path(directory) / "crm.cookie"
            session = Mock()
            session.cookies.get.side_effect = {"JSESSIONID": "test-session", "jsId": "test-js"}.get
            session.post.side_effect = [Mock(status_code=200), Mock(status_code=302)]
            environment = {"CRM_COOKIE_FILE": str(cookie_file), "CRM_USER": "test-user",
                           "CRM_PASS": "test-password", "CRM_TOTP": "JBSWY3DPEHPK3PXP"}
            with patch.dict(os.environ, environment, clear=True), patch("sync_x5_daily_gross.requests.Session", return_value=session):
                headers = crm_login()
            self.assertEqual(stat.S_IMODE(cookie_file.stat().st_mode), 0o600)
            self.assertEqual(cookie_file.read_text(), headers["Cookie"])
            self.assertNotIn(environment["CRM_PASS"], cookie_file.read_text())
            child_environment = {"PATH": os.environ.get("PATH", ""), "CRM_COOKIE_FILE": str(cookie_file),
                                 "PYTHONPATH": str(Path(__file__).parent)}
            subprocess.run([sys.executable, "-c", "from sync_x5_daily_gross import crm_login; assert crm_login()['Cookie'] == 'JSESSIONID=test-session; jsId=test-js'"],
                           env=child_environment, check=True, capture_output=True)

    def test_direct_cookie_still_takes_priority(self):
        with patch.dict(os.environ, {"CRM_COOKIE": "Cookie: direct-cookie"}, clear=True), patch("sync_x5_daily_gross.requests.Session") as session:
            self.assertEqual(crm_login()["Cookie"], "direct-cookie")
            session.assert_not_called()


if __name__ == "__main__":
    unittest.main()
