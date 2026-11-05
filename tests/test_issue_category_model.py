import unittest

import issue_category_model
from category_tuning_source.train_issue_category_model import _normalise_label


class PredictedCategoryNormalizationTests(unittest.TestCase):
    def test_stale_model_categories_route_to_need_triage(self):
        self.assertEqual(issue_category_model._normalize_predicted_category("Schematic"), "Need-Triage")
        self.assertEqual(issue_category_model._normalize_predicted_category("Layout"), "Need-Triage")

    def test_current_categories_are_preserved(self):
        self.assertEqual(issue_category_model._normalize_predicted_category("Connectivity"), "Connectivity")

    def test_inf_category_is_normalized_and_defined_for_prediction(self):
        self.assertEqual(_normalise_label("Inf"), "INF")
        self.assertEqual(_normalise_label("inf"), "INF")
        self.assertEqual(issue_category_model._normalize_predicted_category("INF"), "INF")
        self.assertEqual(issue_category_model._normalize_predicted_category("inf"), "INF")
        prompt = issue_category_model._build_llm_system_prompt(["INF"])
        self.assertIn("- INF: Use when the confirmed cause is a missing, incorrect, or defective driver INF file", prompt)


if __name__ == "__main__":
    unittest.main()
