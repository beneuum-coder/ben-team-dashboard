#!/usr/bin/env python3
"""Guards for the no-op write suppression in the daily Gross sync."""
import unittest

import sync_x5_daily_gross as gross


class SameNumberTests(unittest.TestCase):
    def test_ints_and_floats_match(self):
        self.assertTrue(gross.same_number(100, 100.0))
        self.assertTrue(gross.same_number(100.0, 100))

    def test_numeric_strings_from_lark_match(self):
        self.assertTrue(gross.same_number("100.00", 100.0))
        self.assertTrue(gross.same_number(100.0, "100"))

    def test_cent_level_difference_is_a_change(self):
        self.assertFalse(gross.same_number(100.0, 100.01))

    def test_sub_cent_noise_is_not_a_change(self):
        self.assertTrue(gross.same_number(100.004, 100.0))

    def test_none_vs_value_is_a_change(self):
        self.assertFalse(gross.same_number(None, 0))
        self.assertTrue(gross.same_number(None, None))


class CxDateTests(unittest.TestCase):
    def test_matches_the_gnu_format_but_without_leading_zeros(self):
        from datetime import datetime
        self.assertEqual(gross.cx_date(datetime(2026, 6, 1)), "6/1/2026")
        self.assertEqual(gross.cx_date(datetime(2026, 10, 29)), "10/29/2026")
        self.assertEqual(gross.cx_date(datetime(2026, 12, 31)), "12/31/2026")

    def test_has_no_zero_padding(self):
        from datetime import datetime
        self.assertNotIn("06", gross.cx_date(datetime(2026, 6, 9)).split("/")[0])


class ComparedFieldsTests(unittest.TestCase):
    def test_bookkeeping_timestamp_is_not_compared(self):
        self.assertNotIn("更新时间", gross.COMPARED_FIELDS)

    def test_all_value_fields_are_compared(self):
        self.assertEqual(
            set(gross.COMPARED_FIELDS),
            {"入金(USD)", "出金(USD)", "净入金(USD)", "Master IB", "Sub IB"},
        )


if __name__ == "__main__":
    unittest.main()
