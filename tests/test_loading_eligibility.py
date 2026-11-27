import json
import os
import unittest
from datetime import date

import psycopg2
from psycopg2.extras import RealDictCursor

from APIs.Sherlock import PostgresCustomerEngineeringDb as config
import offload_reporter_issues as offload


class LoadingTitleTests(unittest.TestCase):
    def test_linked_jira_summary_precedes_ips_title(self):
        self.assertEqual(
            offload._title_expr({"ips_title", "jira_summary"}),
            "COALESCE(NULLIF(NULLIF(TRIM(jira_summary), ''), 'NA'), "
            "NULLIF(NULLIF(TRIM(ips_title), ''), 'NA'))",
        )

    def test_linked_jira_and_hsd_titles_precede_ips_title(self):
        self.assertEqual(
            offload._title_expr({"ips_title", "jira_summary", "hsd_title"}),
            "COALESCE(NULLIF(NULLIF(TRIM(jira_summary), ''), 'NA'), "
            "NULLIF(NULLIF(TRIM(hsd_title), ''), 'NA'), "
            "NULLIF(NULLIF(TRIM(ips_title), ''), 'NA'))",
        )

    def test_linked_hsd_title_precedes_ips_title(self):
        self.assertEqual(
            offload._title_expr({"ips_title", "hsd_title"}),
            "COALESCE(NULLIF(NULLIF(TRIM(hsd_title), ''), 'NA'), "
            "NULLIF(NULLIF(TRIM(ips_title), ''), 'NA'))",
        )

    def test_title_expression_supports_optional_columns(self):
        for column in ("ips_title", "jira_summary", "hsd_title", "jira_title"):
            with self.subTest(column=column):
                self.assertEqual(
                    offload._title_expr({column}),
                    f"NULLIF(NULLIF(TRIM({column}), ''), 'NA')",
                )
        self.assertEqual(offload._title_expr(set()), "NULL")

    def test_low_jira_exposure_is_excluded_when_column_exists(self):
        self.assertEqual(
            offload._jira_exposure_allowed_expr({"jira_exposure"}),
            "LOWER(TRIM(COALESCE(jira_exposure::text, ''))) <> '4-low'",
        )
        self.assertEqual(offload._jira_exposure_allowed_expr(set()), "TRUE")


class LoadingSummaryChartTests(unittest.TestCase):
    def test_group_charts_highlight_wifi_and_bt_leaders(self):
        html = offload._loading_group_charts_html([
            {"name": "Frank Lee", "weighted_loading_value": 8.25},
            {"name": "KJ Fang", "weighted_loading_value": 14.50},
            {"name": "Brenton Wu", "weighted_loading_value": 10.0},
            {"name": "Matt Chen", "weighted_loading_value": 18.75},
            {"name": "Juan Zou", "weighted_loading_value": 6.0},
        ])
        self.assertIn("Wi-Fi Loading", html)
        self.assertIn("BT Loading", html)
        self.assertIn("KJ Fang</b> at <b>14.50", html)
        self.assertIn("Matt Chen</b> at <b>18.75", html)
        self.assertEqual(html.count("Brenton Wu"), 2)
        self.assertEqual(html.count("MOST LOADED"), 2)


@unittest.skipUnless(
    os.environ.get("RUN_LOADING_DB_TESTS") == "1",
    "Set RUN_LOADING_DB_TESTS=1 to run read-only PostgreSQL fixture queries.",
)
class LoadingEligibilityTests(unittest.TestCase):
    def setUp(self):
        self.connection = psycopg2.connect(
            host=config.host, port=config.port, dbname=config.database,
            user=config.user, password=config.password, connect_timeout=10,
        )
        self.addCleanup(self.connection.close)
        self.connection.set_session(readonly=True, autocommit=True)
        with self.connection.cursor() as cursor:
            cursor.execute("SET statement_timeout = '15000'")
        self.columns = {
            "engineer": "text", "ips_case_number": "integer",
            "ips_jira_id": "text", "ips_jira_promo_status": "text",
            "ips_status": "text", "ips_sub_status": "text",
            "ips_created_date": "timestamp", "jira_id": "text",
            "jira_status": "text", "jira_created_date": "timestamp",
            "ips_title": "text", "bug_project": "text",
        }
        today = date.today()
        self.current = today.isoformat()
        self.previous = date(today.year - 1, 12, 1).isoformat()
        self.rows = []

    def add_row(self, **values):
        row = {
            "engineer": "KJ Fang", "ips_case_number": 0,
            "ips_jira_id": "NA", "ips_jira_promo_status": "NA",
            "ips_status": "NA", "ips_sub_status": "NA",
            "jira_id": "NA", "jira_status": "NA", "ips_title": "NA",
            "bug_project": "WIFI",
        }
        row.update(values)
        self.rows.append(row)

    def query_rows(self, query, params=None):
        if "information_schema.columns" in query:
            return [{"column_name": column} for column in self.columns]
        fields = ", ".join(f"{name} {kind}" for name, kind in self.columns.items())
        fixture = (
            "WITH fixture AS (SELECT * FROM jsonb_to_recordset(%s::jsonb) "
            f"AS source({fields})), "
        )
        self.assertFalse(params)
        query = fixture + query.lstrip()[len("WITH "):].replace("%", "%%")
        with self.connection.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(query, [json.dumps(self.rows), *(params or [])])
            return [dict(row) for row in cursor.fetchall()]

    def test_counts_and_details_use_identical_current_year_membership(self):
        self.add_row(jira_id="WOT-73665", jira_status="Open",
                     jira_created_date=self.current, bug_project="WOT")
        self.add_row(jira_id="WIFI-OLD", jira_status="Open",
                     jira_created_date=self.previous)
        self.add_row(ips_case_number=123, ips_status="New",
                     ips_jira_id="WIFI-578697", ips_jira_promo_status="Done",
                     ips_created_date=self.current)
        self.add_row(jira_id="WIFI-578697", jira_status="Closed",
                     jira_created_date=self.previous)
        for status in ("Verify", "Implemented"):
            self.add_row(jira_id=f"WIFI-{status}", jira_status=status,
                         jira_created_date=self.current)
        self.add_row(ips_case_number=456, ips_status="New",
                     ips_jira_promo_status="Not Yet Promoted",
                     ips_created_date=self.current, ips_title="Active IPS")

        breakdown = offload._get_reporter_current_breakdown(self, "fixture", 21)
        details = offload._get_unpromoted_ips_rows_for_weighting(
            self, "fixture", include_jira=True, include_hsd=True,
        )
        jira = [row for row in details if row["source_type"] == "jira"]
        ips = [row for row in details if row["source_type"] == "valid_ips"]
        self.assertEqual([row["jira_id"] for row in jira], ["WOT-73665"])
        self.assertEqual([row["ips_case_number"] for row in ips], ["456"])
        self.assertEqual(breakdown[0]["num_jira"], len(jira))
        self.assertEqual(breakdown[0]["num_unpromoted_ips"], len(ips))
        counts = offload._get_reporter_current_counts(self, "fixture", 21)
        self.assertEqual(len(counts), 1)
        self.assertEqual(counts[0]["total_current_issue_count"], 2)

    def test_ips_link_without_terminal_jira_record_remains_eligible(self):
        self.add_row(ips_case_number=789, ips_status="New",
                     ips_jira_id="WOT-LINKED", ips_jira_promo_status="Done",
                     ips_created_date=self.current)
        details = offload._get_unpromoted_ips_rows_for_weighting(
            self, "fixture", include_jira=True,
        )
        self.assertEqual([row["jira_id"] for row in details], ["WOT-LINKED"])

    def test_ips_with_jira_id_counts_only_as_jira_when_promotion_failed(self):
        self.add_row(
            ips_case_number=123,
            ips_jira_id="WIFI-123",
            ips_jira_promo_status="Failed",
            ips_status="New",
            ips_created_date=self.current,
            jira_id="WIFI-123",
            jira_status="Open",
            jira_created_date=self.current,
        )

        breakdown = offload._get_reporter_current_breakdown(self, "fixture", 21)
        details = offload._get_unpromoted_ips_rows_for_weighting(
            self, "fixture", include_jira=True,
        )
        counts = offload._get_reporter_current_counts(self, "fixture", 21)

        self.assertEqual(breakdown[0]["num_unpromoted_ips"], 0)
        self.assertEqual(breakdown[0]["num_jira"], 1)
        self.assertEqual(
            [(row["source_type"], row["jira_id"]) for row in details],
            [("jira", "WIFI-123")],
        )
        self.assertEqual(counts[0]["total_current_issue_count"], 1)


if __name__ == "__main__":
    unittest.main()
