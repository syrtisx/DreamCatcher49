"""Run from the repo root: python -m unittest tests.test_parser"""
import unittest

from src.agent import parse_recommendation


class ParseRecommendation(unittest.TestCase):
    def test_id_in_backticks(self):
        self.assertEqual(parse_recommendation("RECOMMENDATION: `P-004`"), "P-004")

    def test_product_name_before_id(self):
        self.assertEqual(parse_recommendation("RECOMMENDATION: Norvane CIP-123 (P-004)"), "P-004")

    def test_trailing_prose_line_is_ignored(self):
        text = "RECOMMENDATION: P-004\nNote: my recommendation: reconsider"
        self.assertEqual(parse_recommendation(text), "P-004")

    def test_none(self):
        self.assertEqual(parse_recommendation("RECOMMENDATION: NONE"), "NONE")


if __name__ == "__main__":
    unittest.main()
