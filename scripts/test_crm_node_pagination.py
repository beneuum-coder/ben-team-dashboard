import unittest
from unittest.mock import patch

from ben_ib_monthly_preview import all_ben_nodes
from team_ib_phase1b import sales_nodes


class CrmNodePaginationTests(unittest.TestCase):
    def test_ben_pages_with_verified_total(self):
        pages = [
            {"rows": [{"user_id": "1"}, {"user_id": "2"}], "total": 3},
            {"rows": [{"user_id": "3"}], "total": 3},
        ]
        with patch("ben_ib_monthly_preview.post", side_effect=pages) as request:
            self.assertEqual(len(all_ben_nodes({}, page_size=2)), 3)
            self.assertEqual(request.call_count, 2)
            self.assertFalse(request.call_args_list[0].args[2]["skipCount"])
            self.assertEqual(request.call_args_list[1].args[2]["pagination"]["offset"], 2)

    def test_team_pages_with_verified_total(self):
        pages = [
            {"rows": [{"user_id": "1"}, {"user_id": "2"}], "total": 3},
            {"rows": [{"user_id": "3"}], "total": 3},
        ]
        with patch("team_ib_phase1b.post", side_effect=pages):
            self.assertEqual(len(sales_nodes({}, "sale-id", page_size=2)), 3)

    def test_repeated_page_is_not_accepted(self):
        repeated = {"rows": [{"user_id": "1"}, {"user_id": "2"}], "total": 4}
        with patch("ben_ib_monthly_preview.post", return_value=repeated):
            with self.assertRaisesRegex(RuntimeError, "duplicate IDs"):
                all_ben_nodes({}, page_size=2)

    def test_empty_first_page_is_not_treated_as_complete(self):
        with patch("team_ib_phase1b.post", return_value={"rows": [], "total": 0}):
            with self.assertRaisesRegex(RuntimeError, "returned no rows"):
                sales_nodes({}, "sale-id", page_size=2)

    def test_missing_total_is_not_treated_as_complete(self):
        with patch("ben_ib_monthly_preview.post", return_value={"rows": [{"user_id": "1"}]}):
            with self.assertRaisesRegex(RuntimeError, "no total count"):
                all_ben_nodes({}, page_size=2)


if __name__ == "__main__":
    unittest.main()
