import argparse
import unittest

from sync_x5_channel_monthly import classify, index_lark, key


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


if __name__ == "__main__":
    unittest.main()
