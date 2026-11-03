from datetime import date, datetime, timedelta

from yearly_issue_dashboard import (
    active_weight_rows,
    aggregate_yearly_records,
    bug_created_date,
    bug_created_quarter,
    bug_origin,
    bug_status_custom,
    cfe_team,
    closed_unpromoted_ips_records,
    customer_custom,
    filter_oem_view_records,
    turnaround_issue_records,
)


def test_scorecard_deduplicates_issue_ids_and_uses_dax_rates():
    rows = [
        {
            "engineer": "Engineer A",
            "customer": "Customer A",
            "ips_case_number": "101",
            "ips_jira_promo_status": "PROMOTED",
            "jira_id": "WIFI-1",
            "jira_is_sw_change": True,
            "hsd_id": "NA",
            "hsd_promoted_id": "NA",
            "jira_state_reason": "Fixed",
            "jira_team": "CAE",
            "jira_found_by": "Customer Found",
            "bug_status_custom": "Closed",
        },
        {
            "engineer": "Engineer A",
            "customer": "Customer A",
            "ips_case_number": "101",
            "ips_jira_promo_status": "PROMOTED",
            "jira_id": "WIFI-1",
            "jira_is_sw_change": True,
            "hsd_id": "HSD-1",
            "hsd_promoted_id": "WIFI-1",
            "jira_state_reason": "Fixed",
            "jira_team": "CAE",
            "jira_found_by": "Customer Found",
            "bug_status_custom": "Closed",
        },
        {
            "engineer": "Engineer B",
            "customer": "Customer B",
            "ips_case_number": "102",
            "ips_jira_promo_status": "NOT YET PROMOTED",
            "jira_id": "NA",
            "ips_jira_id": "BT-2",
            "jira_is_sw_change": False,
            "hsd_id": "HSD-2",
            "hsd_promoted_id": "NA",
        },
    ]

    result = aggregate_yearly_records(rows, "engineer")
    customer_result = aggregate_yearly_records(rows, "customer")

    assert result["groups"][0]["name"] == "Engineer A"
    assert result["groups"][0]["total_ips"] == 1
    assert result["groups"][0]["promoted_ips"] == 1
    assert result["groups"][0]["jira_count"] == 1
    assert result["groups"][0]["total_hsd"] == 1
    assert result["groups"][0]["not_promoted_hsd"] == 0
    assert result["groups"][0]["driver_fixed_rate"] == 2
    assert result["groups"][0]["good_bug_ratio"] == 1

    assert result["total"]["total_ips"] == 2
    assert result["total"]["not_promoted_ips"] == 1
    assert result["total"]["jira_count"] == 2
    assert result["total"]["total_hsd"] == 2
    assert result["total"]["not_promoted_hsd"] == 1
    assert result["total"]["total_issue_count"] == 4
    assert result["total"]["jira_triage_rate"] == 0.5
    assert [row["name"] for row in customer_result["groups"]] == ["Customer A", "Customer B"]
    assert customer_result["total"]["total_issue_count"] == 4


def test_zero_denominators_and_unassigned_group_are_safe():
    result = aggregate_yearly_records(
        [{"engineer": "NA", "jira_is_sw_change": True}], "engineer"
    )

    assert result["groups"][0]["name"] == "Unassigned"
    assert result["groups"][0]["jira_triage_rate"] == 0
    assert result["groups"][0]["driver_fixed_rate"] == 0
    assert result["groups"][0]["good_bug_ratio"] == 0
    assert result["total"]["total_issue_count"] == 0


def test_driver_fixed_rate_uses_total_issue_count_as_denominator():
    rows = [
        {"engineer": "Engineer A", "jira_id": "WIFI-1", "jira_is_sw_change": True}
    ]
    rows.extend(
        {
            "engineer": "Engineer A",
            "ips_case_number": str(number),
            "ips_jira_promo_status": "NOT YET PROMOTED",
        }
        for number in range(1, 10)
    )

    result = aggregate_yearly_records(rows, "engineer")

    assert result["groups"][0]["total_issue_count"] == 10
    assert result["groups"][0]["fixed_jira"] == 1
    assert result["groups"][0]["driver_fixed_rate"] == 0.1
    assert result["total"]["driver_fixed_rate"] == 0.1


def test_yearly_metrics_use_the_matching_issue_creation_date():
    rows = [
        {
            "engineer": "Engineer A",
            "ips_created_date": date(2026, 2, 1),
            "ips_case_number": "101",
            "ips_jira_promo_status": "NOT YET PROMOTED",
        },
        {
            "engineer": "Engineer A",
            "jira_created_date": date(2026, 3, 1),
            "jira_id": "WIFI-1",
            "jira_is_sw_change": True,
        },
        {
            "engineer": "Engineer A",
            "hsd_submitted_date": date(2026, 4, 1),
            "hsd_id": "HSD-1",
            "hsd_promoted_id": "NA",
        },
        {
            "engineer": "Engineer A",
            "ips_created_date": date(2025, 2, 1),
            "ips_case_number": "102",
            "jira_created_date": date(2026, 5, 1),
            "jira_id": "WIFI-2",
        },
        {
            "engineer": "Engineer A",
            "ips_case_number": "NA",
            "jira_created_date": date(2026, 6, 1),
            "jira_id": "WIFI-3",
        },
    ]

    result = aggregate_yearly_records(rows, "engineer", 2026)["groups"][0]

    assert result["total_ips"] == 1
    assert result["jira_count"] == 2
    assert result["total_hsd"] == 1
    assert result["not_promoted_hsd"] == 1
    assert result["total_issue_count"] == 4


def test_bug_origin_created_date_and_quarter_follow_dax_precedence():
    ips_row = {
        "ips_case_number": "101",
        "ips_created_date": date(2026, 2, 1),
        "hsd_submitted_date": date(2026, 7, 1),
        "jira_created_date": date(2026, 8, 1),
    }
    hsd_row = {"ips_case_number": "NA", "hsd_submitted_date": date(2026, 5, 1)}
    jira_row = {"ips_case_number": "0", "jira_created_date": date(2026, 10, 1)}
    ips_fallback_row = {
        "ips_case_number": "101",
        "hsd_submitted_date": date(2026, 7, 1),
        "jira_created_date": date(2026, 8, 1),
    }

    assert bug_origin(ips_row) == "IPS"
    assert bug_created_date(ips_row) == date(2026, 2, 1)
    assert bug_created_quarter(ips_row) == "Q1"
    assert bug_origin(hsd_row) == "HSD"
    assert bug_created_date(hsd_row) == date(2026, 5, 1)
    assert bug_created_quarter(hsd_row) == "Q2"
    assert bug_origin(jira_row) == "JIRA"
    assert bug_created_date(jira_row) == date(2026, 10, 1)
    assert bug_created_quarter(jira_row) == "Q4"
    assert bug_created_date(ips_fallback_row) == date(2026, 8, 1)


def test_good_bug_ratio_uses_computed_status_when_custom_column_is_absent():
    result = aggregate_yearly_records(
        [
            {
                "customer": "Customer A",
                "jira_id": "WIFI-1",
                "jira_status": "Closed",
                "jira_state_reason": "Fixed",
                "jira_team": "CAE",
                "jira_found_by": "Customer Found",
            },
            {
                "customer": "Customer A",
                "jira_id": "WIFI-2",
                "jira_status": "Closed",
                "jira_state_reason": "Open",
                "jira_team": "CAE",
                "jira_found_by": "Customer Found",
            },
        ],
        "customer",
    )

    assert result["groups"][0]["good_bug_ratio"] == 0.5


def test_bug_status_custom_matches_dax_precedence():
    assert bug_status_custom({"jira_status": " Closed ", "ips_status": "Open"}) == "closed"
    assert bug_status_custom({"jira_status": "Pending", "ips_sub_status": "Close_Pending"}) == "closed"
    assert bug_status_custom({"jira_status": "Implemented", "hsd_status_reason": "Rejected"}) == "closed"
    assert bug_status_custom({"jira_status": " Verify "}) == "open"
    assert bug_status_custom({"ips_status": "Investigating"}) == "open"
    assert bug_status_custom({"hsd_status_reason": "NA"}) == "open"
    assert bug_status_custom({}) == "closed"


def test_customer_custom_matches_oem_dax_mapping():
    assert customer_custom({"customer": "LENOVO", "reporter": "Wesley Kuo"}) == "LENOVO Thinkpad"
    assert customer_custom({"customer": "LENOVO", "reporter": "Juan Zou"}) == "LENOVO Ideapad"
    assert customer_custom({"customer": "LENOVO", "reporter": "Timdaway Lai"}) == "LENOVO Thinkpad"
    assert customer_custom({"customer": "LENOVO", "reporter": "Zhiqiang Cai"}) == "LENOVO Ideapad"
    assert customer_custom({"customer": "NA", "reporter": "Ling-yuan Tu"}) == "ICS"
    assert customer_custom({"customer": "NA", "reporter": "Dale Cronau"}) == "Field Issue"
    assert customer_custom({"customer": "DELL", "reporter": "Wesley Kuo"}) == "DELL"


def test_cfe_team_matches_dax_membership_and_precedence():
    assert cfe_team({"reporter": "bingyue sun"}) == "BT CFE"
    assert cfe_team({"jira_assignee": "Sam Hsu"}) == "CIE CFE"
    assert cfe_team({"reporter": "Bingyue Sun", "jira_assignee": "Brenton Wu"}) == "WiFi CFE"
    assert cfe_team({}) == "Other"


def test_customer_aggregation_uses_custom_oem_label():
    rows = [
        {"customer": "LENOVO", "reporter": "Juan Zou", "ips_case_number": "101"},
        {"customer": "LENOVO", "reporter": "Wesley Kuo", "ips_case_number": "102"},
    ]

    result = aggregate_yearly_records(rows, "customer")

    assert [row["name"] for row in result["groups"]] == [
        "LENOVO Ideapad",
        "LENOVO Thinkpad",
    ]


def test_oem_visual_filters_exclude_na_customer_and_reporter():
    rows = [
        {"customer": "LENOVO", "reporter": "Zhiqiang Cai"},
        {"customer": "NA", "reporter": "Ling-yuan Tu"},
        {"customer": "DELL", "reporter": "NA"},
        {"customer": "HP", "reporter": "Brenton Wu"},
        {"customer": "SAMSUNG", "reporter": "Other Reporter"},
    ]

    assert filter_oem_view_records(rows) == [rows[0], rows[3]]


def test_turnaround_population_requires_promoted_closed_customer_found_ips():
    rows = [
        {
            "ips_case_number": "101",
            "ips_jira_promo_status": "PROMOTED",
            "jira_found_by": " Customer Found ",
            "jira_status": "Closed",
            "reporter": "Juan Zou",
            "jira_assignee": "Brenton Wu",
            "customer": "LENOVO",
            "bug_project": "WiFi CFE",
            "ips_title": "IPS title",
            "jira_summary": "Jira summary",
            "jira_is_sw_change": True,
            "ips_tat_till_jira_hours": 24,
        },
        {
            "ips_case_number": "102",
            "ips_jira_promo_status": "DONE",
            "jira_found_by": "Customer Found",
            "jira_status": "Closed",
            "reporter": "Wesley Kuo",
            "customer": "LENOVO",
            "jira_is_sw_change": False,
        },
        {"ips_case_number": "103", "ips_jira_promo_status": "Not Yet Promoted", "jira_found_by": "Customer Found", "jira_status": "Closed"},
        {"ips_case_number": "104", "ips_jira_promo_status": "PROMOTED", "jira_found_by": "Customer Found", "jira_status": "Open"},
        {"ips_case_number": "105", "ips_jira_promo_status": "PROMOTED", "jira_found_by": "Internal", "jira_status": "Closed"},
        {"ips_case_number": "NA", "ips_jira_promo_status": "PROMOTED", "jira_found_by": "Customer Found", "jira_status": "Closed"},
    ]

    result = turnaround_issue_records(rows)

    assert len(result) == 2
    assert result[0]["customer"] == "LENOVO Ideapad"
    assert result[0]["cfe_team"] == "WiFi CFE"
    assert result[0]["bug_project"] == "WiFi CFE"
    assert result[0]["has_ips_title"] is True
    assert result[0]["has_jira_title"] is True
    assert result[0]["is_sw_change"] is True
    assert result[0]["ips_tat_till_jira_hours"] == 24
    assert result[1]["customer"] == "LENOVO Thinkpad"
    assert result[1]["has_ips_title"] is False
    assert result[1]["has_jira_title"] is False
    assert result[1]["is_sw_change"] is False


def test_closed_unpromoted_ips_uses_ips_created_to_closed_elapsed_hours():
    rows = [
        {
            "ips_case_number": "201",
            "ips_jira_promo_status": "NOT YET PROMOTED",
            "ips_status": "Closed",
            "ips_created_date": datetime(2026, 1, 1, 9),
            "ips_closed_date": datetime(2026, 1, 2, 3),
            "reporter": "Kj Fang",
            "customer": "ACER",
            "bug_project": "WIFI",
        },
        {
            "ips_case_number": "202",
            "ips_jira_promo_status": "FAILED",
            "ips_sub_status": "Closed",
            "ips_created_date": "2026-01-01T00:00:00",
            "ips_closed_date": "2026-01-02T12:00:00Z",
        },
        {
            "ips_case_number": "203",
            "ips_jira_promo_status": "DONE",
            "ips_status": "Closed",
            "ips_created_date": datetime(2026, 1, 1),
            "ips_closed_date": datetime(2026, 1, 2),
        },
        {
            "ips_case_number": "204",
            "ips_jira_promo_status": "NOT YET PROMOTED",
            "ips_status": "Open",
            "ips_created_date": datetime(2026, 1, 1),
            "ips_closed_date": datetime(2026, 1, 2),
        },
        {
            "ips_case_number": "205",
            "ips_jira_promo_status": "NOT YET PROMOTED",
            "ips_status": "Closed",
            "ips_created_date": datetime.now() - timedelta(days=2),
            "ips_closed_date": datetime.now() + timedelta(days=2),
        },
    ]

    result = closed_unpromoted_ips_records(rows)

    assert len(result) == 2
    assert result[0]["reporter"] == "Kj Fang"
    assert result[0]["ips_only_tat_hours"] == 18
    assert result[0]["bug_project"] == "WIFI"
    assert result[1]["ips_only_tat_hours"] == 36


def test_promoted_ips_uses_dax_promo_status_not_boolean_flag():
    result = aggregate_yearly_records(
        [
            {
                "engineer": "Engineer A",
                "ips_case_number": "101",
                "ips_jira_promo_status": "NOT YET PROMOTED",
                "is_ips_promoted_to_jira": True,
            }
        ],
        "engineer",
    )

    assert result["groups"][0]["promoted_ips"] == 0
    assert result["groups"][0]["not_promoted_ips"] == 1


def test_active_weight_rows_apply_source_eligibility_and_hsd_owner_match():
    rows = [
        {
            "engineer": "Engineer A",
            "customer": "Customer A",
            "ips_case_number": "101",
            "ips_jira_promo_status": "NOT YET PROMOTED",
            "ips_sub_status": "Open",
            "ips_status": "Open",
            "ips_created_date": date(2026, 11, 1),
            "ips_title": "WiFi reconnect issue",
            "bug_project": "WIFI",
            "hsd_id": "HSD-1",
            "hsd_owner": "Engineer A",
            "hsd_status_reason": "Open",
            "hsd_submitted_date": date(2026, 11, 1),
        },
        {
            "engineer": "Engineer B",
            "customer": "Customer A",
            "ips_case_number": "102",
            "ips_jira_promo_status": "PROMOTED",
            "jira_id": "WIFI-2",
            "ips_status": "Open",
            "ips_title": "Bluetooth audio issue",
            "bug_project": "BT",
            "hsd_id": "HSD-2",
            "hsd_owner": "Engineer A",
            "hsd_status_reason": "Open",
            "hsd_submitted_date": date(2026, 11, 1),
        },
        {
            "engineer": "Engineer A",
            "customer": "Customer A",
            "ips_case_number": "103",
            "ips_jira_promo_status": "NOT YET PROMOTED",
            "ips_sub_status": "Close-Pending",
            "ips_status": "Open",
            "ips_created_date": date(2026, 11, 1),
            "ips_title": "Closed pending issue",
            "hsd_id": "NA",
        },
    ]

    engineer_rows = active_weight_rows(rows, "engineer", today=date(2026, 11, 1))
    customer_rows = active_weight_rows(rows, "customer", today=date(2026, 11, 1))

    assert {(row["source_type"], row["ips_case_number"], row["jira_id"], row["hsd_id"]) for row in engineer_rows} == {
        ("valid_ips", "101", "", "HSD-1"),
        ("jira", "102", "WIFI-2", "HSD-2"),
        ("hsd", "101", "", "HSD-1"),
    }
    assert sum(row["source_type"] == "hsd" for row in customer_rows) == 2