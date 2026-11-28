import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from loading_quality_monitor import _ai_candidates, _send_anomaly_emails, audit_snapshot, finding_fingerprint, inspect_title


class LoadingQualityMonitorTests(unittest.TestCase):
    def test_realistic_tag_only_title_is_flagged(self):
        self.assertEqual(
            inspect_title("[HP][Huaqin][OB3 Anyal][TWL][AX211][Win11]"),
            "tag_only_title",
        )

    def test_tag_only_warning_explains_loading_risk_and_action(self):
        payload = {
            "generated_at": "2026-11-26T12:00:00+00:00",
            "rows": [{
                "name": "A",
                "issue_details": [{
                    "ips_case_number": "1035972",
                    "ips_title": "[HP][Huaqin][OB3][TWL][AX211][Win11]",
                    "category": "Audio",
                    "issue_weight": 2.28,
                    "created": "2026-11-20",
                }],
            }],
        }

        report = audit_snapshot(payload, now=datetime(2026, 11, 26, 13, tzinfo=timezone.utc))
        finding = next(item for item in report["findings"] if item["code"] == "tag_only_title")

        self.assertIn("does not state the symptom", finding["message"])
        self.assertIn("Audio", finding["message"])
        self.assertIn("2.28", finding["message"])
        self.assertIn("open the source issue", finding["message"].lower())

    def test_descriptive_title_is_accepted(self):
        self.assertIsNone(inspect_title("[HP][Win11] Wi-Fi disconnects after waking from sleep"))

    def test_missing_titles_are_flagged(self):
        self.assertEqual(inspect_title(""), "missing_title")
        self.assertEqual(inspect_title("WIFI-972814"), "identifier_only_title")
        self.assertEqual(inspect_title("test"), "too_little_title_detail")

    def test_old_and_non_customer_submissions_are_candidates(self):
        payload = {
            "generated_at": "2026-11-26T12:00:00+00:00",
            "rows": [{
                "name": "A",
                "issue_details": [{
                    "source_type": "jira",
                    "jira_id": "WIFI-1",
                    "ips_title": "Test JIRA case promotion as part of a sanity test - Please ignore the case",
                    "category": "Need-Triage",
                    "issue_weight": 1.0,
                    "created": "2026-10-01",
                }],
            }],
        }
        report = audit_snapshot(
            payload,
            now=datetime(2026, 11, 26, 13, tzinfo=timezone.utc),
            old_issue_days=30,
        )
        codes = {finding["code"] for finding in report["findings"]}
        self.assertIn("old_active_issue", codes)
        self.assertIn("suspicious_non_customer_title", codes)

    def test_ips_issue_without_valid_url_is_flagged(self):
        payload = {
            "generated_at": "2026-11-26T12:00:00+00:00",
            "rows": [{
                "name": "A",
                "issue_details": [{
                    "ips_case_number": "1039578",
                    "ips_url": "",
                    "ips_title": "Wi-Fi slow performance during validation",
                    "issue_weight": 2.32,
                }],
            }],
        }
        report = audit_snapshot(payload, now=datetime(2026, 11, 26, 13, tzinfo=timezone.utc))
        self.assertIn("missing_ips_url", {finding["code"] for finding in report["findings"]})

    def test_weighted_load_and_issue_count_are_reconciled(self):
        payload = {
            "generated_at": "2026-11-26T12:00:00+00:00",
            "rows": [{
                "name": "A",
                "weighted_loading_value": 2.0,
                "active_uips": 1,
                "jira": 0,
                "active_hsd": 0,
                "issue_details": [{"ips_case_number": "1", "ips_title": "Wi-Fi disconnects after resume from sleep", "issue_weight": 1.0}],
            }],
        }
        report = audit_snapshot(payload, now=datetime(2026, 11, 26, 13, tzinfo=timezone.utc))
        codes = {finding["code"] for finding in report["findings"]}
        self.assertIn("weighted_load_mismatch", codes)
        self.assertEqual(report["status"], "error")

    def test_duplicate_issue_assignments_are_errors(self):
        issue = {
            "source_type": "jira",
            "ips_case_number": "1033992",
            "ips_title": "Wi-Fi connection drops repeatedly after resume from sleep",
            "jira_id": "WIFI-972814",
            "issue_weight": 1.0,
            "category": "Need-Triage",
        }
        payload = {
            "generated_at": "2026-11-26T12:00:00+00:00",
            "rows": [
                {"name": "A", "issue_details": [dict(issue)]},
                {"name": "B", "issue_details": [dict(issue)]},
            ],
        }
        report = audit_snapshot(payload, now=datetime(2026, 11, 26, 13, tzinfo=timezone.utc))
        duplicate = [finding for finding in report["findings"] if finding["code"] == "duplicate_issue_assignment"]
        self.assertEqual(len(duplicate), 2)
        self.assertNotIn("duplicate_issue_title", {finding["code"] for finding in report["findings"]})
        self.assertEqual(report["status"], "error")

    def test_duplicate_titles_on_distinct_issues_are_ai_candidates(self):
        payload = {
            "generated_at": "2026-11-26T12:00:00+00:00",
            "rows": [{
                "name": "A",
                "issue_details": [
                    {
                        "ips_case_number": "1039550",
                        "ips_url": "https://intel.lightning.force.com/case/1039550",
                        "ips_title": "[NVL][Samsung] Data TPUT performance decreases on 11n 44ch Downlink",
                        "issue_weight": 2.64,
                    },
                    {
                        "ips_case_number": "1039614",
                        "ips_url": "https://intel.lightning.force.com/case/1039614",
                        "ips_title": "  [nvl][samsung]   Data TPUT performance decreases on 11n 44ch Downlink ",
                        "issue_weight": 2.64,
                    },
                ],
            }],
        }

        report = audit_snapshot(payload, now=datetime(2026, 11, 26, 13, tzinfo=timezone.utc))
        duplicates = [finding for finding in report["findings"] if finding["code"] == "duplicate_issue_title"]
        self.assertEqual(len(duplicates), 2)
        self.assertEqual({finding["ips_case_number"] for finding in duplicates}, {"1039550", "1039614"})
        self.assertTrue(all(finding["duplicate_issue_count"] == 2 for finding in duplicates))
        self.assertEqual(len(_ai_candidates(report["findings"])), 2)
        self.assertEqual(report["status"], "warning")

    def test_ai_candidates_merge_multiple_signals_for_one_issue(self):
        findings = [
            {"jira_id": "BT-1", "owner": "A", "title": "Test", "code": "old_active_issue"},
            {"jira_id": "BT-1", "owner": "A", "title": "Test", "code": "too_little_title_detail"},
        ]
        candidates = _ai_candidates(findings)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(len(candidates[0]["signals"]), 2)

    def test_anomaly_email_routes_to_engineer_and_jonathan(self):
        report = {
            "status": "warning",
            "findings": [{"owner": "Frank Lee", "ips_case_number": "1", "message": "Bad title"}],
            "ai_review": [],
        }
        with patch("loading_quality_monitor._email_report") as email_report:
            _send_anomaly_emails(report)
        recipients = email_report.call_args.args[1]
        self.assertIn("frank.lee@intel.com", recipients)
        self.assertIn("jonathan.tsao@intel.com", recipients)

    def test_fingerprint_ignores_snapshot_timestamp(self):
        finding = {"severity": "warning", "code": "tag_only_title", "ips_case_number": "1"}
        first = {"findings": [finding], "checked_at": "2026-11-26T12:00:00"}
        second = {"findings": [finding], "checked_at": "2026-11-26T13:00:00"}
        self.assertEqual(finding_fingerprint(first), finding_fingerprint(second))


if __name__ == "__main__":
    unittest.main()
