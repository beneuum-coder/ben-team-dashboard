import argparse
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from sync_x5_channel_monthly import classify, cpa_rows, index_lark, key, main, run_ib


class MonthlyChannelPreviewTests(unittest.TestCase):
    def test_business_key_has_four_segments(self):
        self.assertEqual(key({"month":"2026-09","sales":"Ben","type":"IB","channelId":"1270785"}), ("2026-09", "Ben", "IB", "1270785"))
    def test_lark_placeholder_is_ignored_and_valid_key_is_indexed(self):
        records = [
            {"record_id": "blank", "fields": {"月份": "2026-09-01"}},
            {"record_id": "ib", "fields": {"月份": "2026-09-01", "销售": "Ben", "类型": "IB", "渠道ID": "1"}},
        ]
        indexed, issues = index_lark(records, "2026-09")
        self.assertEqual(len(indexed), 1)
        self.assertEqual(issues[0]["classification"], "IGNORED_PLACEHOLDER")

    def test_metrics_not_timestamp_control_update(self):
        source = {"month": "2026-09", "sales": "Ben", "type": "IB", "channelId": "1", "channelName": "A",
                  "registration": 2, "ftd": 1, "gross": 100, "withdrawal": 20, "net": 80}
        existing = {("2026-09", "Ben", "IB", "1"): {"record_id": "old", "fields": {"Registration": 2, "FTD": 1, "Gross Deposit": 100, "Withdrawal": 20, "Net": 80, "更新时间": 1}}}
        self.assertEqual(classify([source], existing)[0]["classification"], "UNCHANGED")
        source["gross"] = 101
        self.assertEqual(classify([source], existing)[0]["classification"], "UPDATE")

    def test_missing_cpa_month_is_never_created(self):
        row = {"month": "2026-09", "sales": "Rita", "type": "CPA", "channelId": "37216", "channelName": "thomas28",
               "classification": "SKIPPED_NO_MONTHLY_DATA"}
        result = classify([row], {})[0]
        self.assertEqual(result["classification"], "SKIPPED_NO_MONTHLY_DATA")

    def test_invalid_sales_does_not_block_valid_sales(self):
        valid = {"month":"2026-09","sales":"Kelly","type":"IB","channelId":"1","channelName":"A","registration":1,"ftd":1,"gross":10,"withdrawal":0,"net":10}
        invalid = {**valid, "sales":"Rita", "channelId":""}
        result = classify([valid, invalid], {})
        self.assertEqual(result[0]["classification"], "CREATE")
        self.assertEqual(result[1]["classification"], "INVALID_KEY")

    def test_unmapped_manager_cannot_be_classified_as_action(self):
        unmapped = {"classification":"UNMAPPED_MANAGER","channelId":"1"}
        self.assertNotIn(unmapped["classification"], {"CREATE", "UPDATE"})

    def test_apply_default_is_off(self):
        parser = argparse.ArgumentParser(); parser.add_argument("--apply", action="store_true")
        self.assertFalse(parser.parse_args([]).apply)

    def test_partial_ben_ib_is_not_accepted(self):
        response = {"status": "partial", "month": "2026-10", "report": []}
        with patch("sync_x5_channel_monthly.subprocess.check_output", return_value=json.dumps(response)) as command:
            with self.assertRaisesRegex(RuntimeError, "Ben IB source is incomplete"):
                run_ib("2026-10")
            command.assert_called_once()

    def test_partial_team_ib_is_not_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            previous = os.getcwd()
            os.chdir(directory)
            try:
                def command(args, text):
                    if "ben_ib_monthly_preview.py" in args[1]:
                        return json.dumps({"status": "complete", "month": "2026-10", "report": []})
                    result = Path("work/x5-monthly/x5-monthly-fixed/team/runs/x5-monthly-fixed")
                    result.mkdir(parents=True)
                    (result / "partial-results.json").write_text(json.dumps({"status": "calculation_partial", "month": "2026-10", "channels": [], "failures": []}))
                    return ""
                with patch("sync_x5_channel_monthly.uuid.uuid4", return_value=Mock(hex="fixed")), patch("sync_x5_channel_monthly.subprocess.check_output", side_effect=command):
                    with self.assertRaisesRegex(RuntimeError, "Team IB source is incomplete"):
                        run_ib("2026-10")
            finally:
                os.chdir(previous)

    def test_invalid_cpa_response_is_not_empty_success(self):
        response = Mock()
        response.json.return_value = {}
        with patch("sync_x5_channel_monthly.cx_headers", return_value={}), patch("sync_x5_channel_monthly.requests.get", return_value=response):
            with self.assertRaisesRegex(RuntimeError, "invalid or empty CellXpert affiliate list"):
                cpa_rows("2026-10")

    def test_cpa_failure_blocks_apply_before_lark_connection(self):
        with patch("sys.argv", ["sync_x5_channel_monthly.py", "--apply"]), \
             patch("sync_x5_channel_monthly.run_ib", return_value=([], {"IB/Ben": {"status": "PASS"}})), \
             patch("sync_x5_channel_monthly.cpa_rows", side_effect=RuntimeError("source unavailable")), \
             patch("sync_x5_channel_monthly.LarkTransport") as transport:
            with self.assertRaisesRegex(RuntimeError, "failed IB or CPA source"):
                main()
            transport.assert_not_called()

    def test_invalid_source_key_blocks_batch_write(self):
        invalid = {"month": "2026-10", "sales": "Ben", "type": "IB", "channelId": "", "channelName": "A",
                   "registration": 1, "ftd": 1, "gross": 10, "withdrawal": 0, "net": 10}
        lark = Mock()
        with patch("sys.argv", ["sync_x5_channel_monthly.py", "--apply"]), \
             patch("sync_x5_channel_monthly.run_ib", return_value=([invalid], {"IB/Ben": {"status": "PASS"}})), \
             patch("sync_x5_channel_monthly.cpa_rows", return_value=([], [])), \
             patch("sync_x5_channel_monthly.LarkTransport", return_value=lark), \
             patch("sync_x5_channel_monthly.lark_records", return_value=[]):
            with self.assertRaisesRegex(RuntimeError, "invalid source key or metrics"):
                main()
            lark.request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
