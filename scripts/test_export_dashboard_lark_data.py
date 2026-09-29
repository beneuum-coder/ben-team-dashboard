import unittest
from datetime import datetime, timedelta, timezone

from export_dashboard_lark_data import normalize_x5_channel_monthly


SHANGHAI = timezone(timedelta(hours=8))


def monthly_record(**fields):
    base = {
        "月份": int(datetime(2026, 9, 1, tzinfo=SHANGHAI).timestamp() * 1000),
        "销售": "Ben",
        "类型": "IB",
        "渠道ID": "123",
        "IB/CPA账户名称": "Example channel",
        "Registration": 2,
        "FTD": 1,
        "Gross Deposit": 100,
        "Withdrawal": 20,
        "Net": 80,
        "更新时间": int(datetime(2026, 9, 2, tzinfo=SHANGHAI).timestamp() * 1000),
    }
    base.update(fields)
    return {"record_id": "rec", "fields": base}


class X5ChannelMonthlyExportTests(unittest.TestCase):
    def test_normalizes_the_four_part_business_key(self):
        data = normalize_x5_channel_monthly([monthly_record()])
        self.assertEqual(data["month"], "2026-09")
        self.assertEqual(data["qa"]["validBusinessKeys"], 1)
        self.assertEqual(data["records"][0]["channelId"], "123")
        self.assertEqual(data["records"][0]["updatedAt"], "2026-09-01T16:00:00Z")

    def test_ignores_blank_placeholder_without_creating_a_business_key(self):
        placeholder = {"record_id": "blank", "fields": {"月份": monthly_record()["fields"]["月份"]}}
        data = normalize_x5_channel_monthly([monthly_record(), placeholder])
        self.assertEqual(data["qa"]["validBusinessKeys"], 1)
        self.assertEqual(data["qa"]["ignoredPlaceholder"], 1)

    def test_rejects_duplicate_business_keys(self):
        with self.assertRaisesRegex(RuntimeError, "duplicate business key"):
            normalize_x5_channel_monthly([monthly_record(), monthly_record(record_id="different")])

    def test_records_with_partial_keys_are_not_rendered(self):
        data = normalize_x5_channel_monthly([monthly_record(), monthly_record(渠道ID="")])
        self.assertEqual(data["qa"]["validBusinessKeys"], 1)
        self.assertEqual(data["qa"]["invalidKey"], 1)


if __name__ == "__main__":
    unittest.main()
