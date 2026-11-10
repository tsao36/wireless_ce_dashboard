import unittest
from unittest.mock import patch

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


class BatchedDualFeatureInferenceTests(unittest.TestCase):
    def test_batch_matches_single_row_predictions_with_one_model_call(self):
        class FakeModel:
            classes_ = issue_category_model.np.asarray(["Connectivity", "Performance"])

            def __init__(self):
                self.calls = 0

            def predict_proba(self, records):
                self.calls += 1
                return issue_category_model.np.asarray([
                    [0.8, 0.2] if "disconnect" in record["title_text"].casefold() else [0.1, 0.9]
                    for record in records
                ])

        title_model = FakeModel()
        detail_model = FakeModel()
        model_bundle = {
            "feature_mode": "dual_structured_v2",
            "model_title_only": title_model,
            "model_with_details": detail_model,
        }
        rows = [
            {"title": "Disconnect after resume", "technology": "WiFi", "is_field_issue": False},
            {"title": "Low throughput while connected", "technology": "WiFi", "is_field_issue": False},
        ]
        expected = [
            issue_category_model._predict_dual_feature_model(
                model_bundle,
                row["title"],
                row["technology"],
                issue_category_model.extract_structured_ips_context(row["title"], "", ""),
            )[:2]
            for row in rows
        ]
        title_model.calls = 0
        detail_model.calls = 0

        with patch.object(issue_category_model, "_PREDICT_BACKEND", "ml"):
            result = issue_category_model.classify_and_weight_rows(
                rows,
                model_bundle=model_bundle,
                category_weights={"connectivity": 1.5, "performance": 0.4},
                category_technology_weights={},
                default_weight=1.0,
            )

        actual = [
            (row["predicted_human_category"], row["category_confidence"])
            for row in result
        ]
        self.assertEqual(actual, [(category, round(confidence, 4)) for category, confidence in expected])
        self.assertEqual(title_model.calls, 1)
        self.assertEqual(detail_model.calls, 0)


if __name__ == "__main__":
    unittest.main()
