import unittest

import issue_category_model


class PredictedCategoryNormalizationTests(unittest.TestCase):
    def test_stale_model_categories_route_to_need_triage(self):
        self.assertEqual(issue_category_model._normalize_predicted_category("Schematic"), "Need-Triage")
        self.assertEqual(issue_category_model._normalize_predicted_category("Layout"), "Need-Triage")

    def test_current_categories_are_preserved(self):
        self.assertEqual(issue_category_model._normalize_predicted_category("Connectivity"), "Connectivity")


if __name__ == "__main__":
    unittest.main()
