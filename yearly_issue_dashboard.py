"""Pure aggregation helpers for the yearly issue dashboard."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timezone
from typing import Any, Iterable


GOOD_BUG_REASONS = {
    "fixed",
    "product change",
    "3rd party",
    "workaround",
    "duplicate root cause",
    "unknown fix",
}
WIFI_CFE_MEMBERS = {
    "BRENTON WU",
    "JONATHAN TSAO",
    "KJ FANG",
    "ZHIWEI HE",
    "FRANK LEE",
    "FRANK YANG",
    "NICKY CHEN",
    "CHARLES CHU",
    "ZHIQIANG CAI",
    "TIMDAWAY LAI",
    "ZHANYING GAO",
    "JACKX LEE",
    "LYDIAX CHIEN",
    "JOHNSONX SU",
    "XIHAOX YANG",
    "HENRYX SU",
}
BT_CFE_MEMBERS = {
    "BINGYUE SUN",
    "BING CHANG",
    "LEAWEIX CHEN",
    "LEO CHIANG",
    "STEVEN1 CHEN",
    "WESLEY KUO",
    "TONYX YEH",
    "JUAN ZOU",
    "MATT CHEN",
    "YU-WEI CHEN",
}
TURNAROUND_DURATION_FIELDS = (
    "ips_tat_till_jira_hours",
    "jira_sighting_hours",
    "jira_pending_hours",
)


def _text(value: Any) -> str:
    text = "" if value is None else str(value).strip()
    return "" if text.upper() == "NA" else text


def _normalized(value: Any) -> str:
    return _text(value).casefold()


def bug_status_custom(row: dict[str, Any]) -> str:
    jira_status = "" if row.get("jira_status") is None else str(row["jira_status"]).strip().casefold()
    ips_status = "" if row.get("ips_status") is None else str(row["ips_status"]).strip().casefold()
    ips_sub_status = "" if row.get("ips_sub_status") is None else str(row["ips_sub_status"]).strip().casefold()
    hsd_status = "" if row.get("hsd_status_reason") is None else str(row["hsd_status_reason"]).strip().casefold()

    jira_is_closed = jira_status == "closed"
    jira_is_open = jira_status in {"open", "in progress", "pending", "verify", "implemented"}
    ips_is_closed = ips_status == "closed" or ips_sub_status in {
        "closed",
        "close-pending",
        "close pending",
        "close_pending",
    }
    hsd_is_closed = hsd_status in {"closed", "complete", "implemented", "rejected"}
    ips_is_openish = ips_status in {"open", "investigating", "na"} or ips_sub_status in {
        "open",
        "investigating",
        "na",
    }
    hsd_is_openish = hsd_status in {"open", "investigating", "na"}

    if jira_is_closed or ips_is_closed or hsd_is_closed:
        return "closed"
    if jira_is_open:
        return "open"
    if ips_is_openish or hsd_is_openish:
        return "open"
    return "closed"


def bug_origin(row: dict[str, Any]) -> str:
    ips_number = _text(row.get("ips_case_number"))
    try:
        has_ips = float(ips_number) > 0
    except ValueError:
        has_ips = False
    if has_ips:
        return "IPS"
    if row.get("hsd_submitted_date") is not None:
        return "HSD"
    return "JIRA"


def bug_created_date(row: dict[str, Any]) -> Any | None:
    origin = bug_origin(row)
    ips_date = row.get("ips_created_date")
    hsd_date = row.get("hsd_submitted_date")
    jira_date = row.get("jira_created_date")

    if origin == "IPS" and ips_date is not None:
        return ips_date
    if origin == "HSD" and hsd_date is not None:
        return hsd_date
    if jira_date is not None:
        return jira_date
    return None


def bug_created_quarter(row: dict[str, Any]) -> str | None:
    created = bug_created_date(row)
    if isinstance(created, datetime):
        month = created.month
    elif isinstance(created, date):
        month = created.month
    else:
        try:
            month = date.fromisoformat(str(created)[:10]).month
        except (TypeError, ValueError):
            return None
    return f"Q{(month - 1) // 3 + 1}"


def cfe_team(row: dict[str, Any]) -> str:
    reporter = "" if row.get("reporter") is None else str(row["reporter"]).strip().upper()
    assignee = "" if row.get("jira_assignee") is None else str(row["jira_assignee"]).strip().upper()
    if reporter in WIFI_CFE_MEMBERS or assignee in WIFI_CFE_MEMBERS:
        return "WiFi CFE"
    if reporter == "SAM HSU" or assignee == "SAM HSU":
        return "CIE CFE"
    if reporter in BT_CFE_MEMBERS or assignee in BT_CFE_MEMBERS:
        return "BT CFE"
    return "Other"


def customer_custom(row: dict[str, Any]) -> str:
    customer = "" if row.get("customer") is None else str(row["customer"])
    reporter = "" if row.get("reporter") is None else str(row["reporter"])

    if customer == "LENOVO" and reporter in {"Juan Zou", "Zhiqiang Cai"}:
        return "LENOVO Ideapad"
    if customer == "LENOVO" and reporter in {"Wesley Kuo", "Timdaway Lai"}:
        return "LENOVO Thinkpad"
    if customer == "NA" and reporter == "Ling-yuan Tu":
        return "ICS"
    if customer == "NA" and reporter == "Dale Cronau":
        return "Field Issue"
    return customer


def filter_oem_view_records(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    def is_not_na(value: Any) -> bool:
        return value is None or str(value).strip().upper() != "NA"

    return [
        row
        for row in records
        if is_not_na(row.get("customer")) and is_not_na(row.get("reporter"))
        and cfe_team(row) in {"BT CFE", "WiFi CFE"}
    ]


def turnaround_issue_records(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    selected = []
    for row in records:
        if not _valid_ips_id(row.get("ips_case_number")):
            continue
        if _normalized(row.get("ips_jira_promo_status")) not in {"promoted", "done"}:
            continue
        if _normalized(row.get("jira_found_by")) != "customer found":
            continue
        if bug_status_custom(row) != "closed":
            continue

        customer = customer_custom(row)
        selected.append({
            "reporter": _text(row.get("reporter")) or "Unassigned",
            "customer": customer or "Unassigned",
            "cfe_team": cfe_team(row),
            "bug_project": _text(row.get("bug_project")) or "(Blank)",
            "is_sw_change": _is_true(row.get("jira_is_sw_change")),
            "has_ips_title": bool(_text(row.get("ips_title"))),
            "has_jira_title": bool(_text(row.get("jira_summary"))),
            **{field: row.get(field) for field in TURNAROUND_DURATION_FIELDS},
        })
    return selected


def _datetime_value(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, datetime.min.time())
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def closed_unpromoted_ips_records(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    selected = []
    for row in records:
        if not _valid_ips_id(row.get("ips_case_number")):
            continue
        if _normalized(row.get("ips_jira_promo_status")) in {"promoted", "done"}:
            continue
        if _normalized(row.get("ips_status")) != "closed" and _normalized(row.get("ips_sub_status")) != "closed":
            continue

        created = _datetime_value(row.get("ips_created_date"))
        closed = _datetime_value(row.get("ips_closed_date"))
        if created is None or closed is None or closed < created or closed > datetime.now():
            continue

        customer = customer_custom(row)
        selected.append({
            "reporter": _text(row.get("reporter")) or "Unassigned",
            "customer": customer or "Unassigned",
            "cfe_team": cfe_team(row),
            "bug_project": _text(row.get("bug_project")) or "(Blank)",
            "ips_only_tat_hours": (closed - created).total_seconds() / 3600,
        })
    return selected


def _group_name(row: dict[str, Any], group_field: str) -> str:
    if group_field == "customer":
        return customer_custom(row) or "Unassigned"
    return _text(row.get(group_field)) or "Unassigned"


def _valid_id(value: Any) -> str:
    return _text(value)


def _valid_ips_id(value: Any) -> str:
    text = _text(value)
    return text if text.isdigit() and int(text) > 0 else ""


def _is_true(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return _normalized(value) in {"true", "t", "yes", "y", "1"}


def _ratio(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


def _age_days(value: Any, today: date) -> int | None:
    if isinstance(value, datetime):
        value = value.date()
    if isinstance(value, date):
        return (today - value).days
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _in_year(value: Any, year: int) -> bool:
    if isinstance(value, (date, datetime)):
        return value.year == year
    try:
        return date.fromisoformat(str(value)[:10]).year == year
    except (TypeError, ValueError):
        return False


def _technology(row: dict[str, Any]) -> str:
    technology = _text(row.get("technology"))
    if technology:
        return technology
    return {
        "wifi": "WiFi",
        "bt": "BT",
        "cie": "Software",
        "wot": "Tools",
    }.get(_normalized(row.get("bug_project")), "Unknown")


def active_weight_rows(
    records: Iterable[dict[str, Any]],
    group_field: str,
    today: date | None = None,
    year: int | None = None,
) -> list[dict[str, Any]]:
    """Return deduplicated IPS, Jira, and HSD rows eligible for current loading."""
    today = today or date.today()
    candidates: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in records:
        row_in_year = year is None or _in_year(bug_created_date(row), year)
        group_name = _group_name(row, group_field)
        title = _text(row.get("ips_title")) or _text(row.get("jira_summary")) or _text(row.get("hsd_title"))
        technology = _technology(row)
        sub_status = _normalized(row.get("ips_sub_status"))
        ips_status = _normalized(row.get("ips_status"))
        jira_status = _normalized(row.get("jira_status"))
        jira_key = _valid_id(row.get("jira_id")) or _valid_id(row.get("ips_jira_id"))
        promo_status = _normalized(row.get("ips_jira_promo_status"))
        if promo_status:
            unpromoted_ips = promo_status in {"not yet promoted", "failed"}
        elif row.get("is_ips_promoted_to_jira") is not None:
            unpromoted_ips = not _is_true(row.get("is_ips_promoted_to_jira"))
        else:
            unpromoted_ips = not jira_key

        modified_age = _age_days(row.get("ips_last_modified_days"), today)
        if modified_age is None:
            modified_age = _age_days(row.get("ips_last_modified_date"), today)
        open_age = _age_days(row.get("ips_open_days"), today)
        if open_age is None:
            open_age = _age_days(row.get("ips_created_date"), today)
        stale_ips = (
            jira_status in {"", "na", "closed", "verify", "implemented"}
            and ((modified_age is not None and modified_age > 21) or (open_age is not None and open_age > 30))
            and sub_status != "close-pending"
        )

        ips_id = _valid_ips_id(row.get("ips_case_number"))
        if row_in_year and ips_id and unpromoted_ips and not jira_key and ips_status != "closed" and sub_status != "closed":
            if not stale_ips and sub_status != "close-pending":
                key = (group_name.casefold(), "valid_ips", ips_id)
                candidates.setdefault(key, {
                    "group_name": group_name,
                    "source_type": "valid_ips",
                    "ips_case_number": ips_id,
                    "jira_id": jira_key,
                    "hsd_id": _valid_id(row.get("hsd_id")),
                    "title": title,
                    "technology": technology,
                    "is_field_issue": "field issue" in title.casefold(),
                })

        if row_in_year and jira_key and ips_status != "closed" and sub_status not in {"close-pending", "pending-closed"}:
            key = (group_name.casefold(), "jira", jira_key.casefold())
            candidates.setdefault(key, {
                "group_name": group_name,
                "source_type": "jira",
                "ips_case_number": ips_id,
                "jira_id": jira_key,
                "hsd_id": _valid_id(row.get("hsd_id")),
                "title": title,
                "technology": technology,
                "is_field_issue": "field issue" in title.casefold(),
            })

        hsd_id = _valid_id(row.get("hsd_id"))
        hsd_status = _normalized(row.get("hsd_status_reason"))
        hsd_promoted_id = _valid_id(row.get("hsd_promoted_id"))
        hsd_owner = _normalized(row.get("hsd_owner"))
        group_owner = _normalized(row.get("engineer"))
        owner_matches = group_field != "engineer" or (hsd_owner and hsd_owner == group_owner)
        hsd_age = _age_days(row.get("hsd_submitted_date"), today)
        stale_hsd = hsd_age is not None and hsd_age > 30
        if (
            row_in_year
            and hsd_id
            and not hsd_promoted_id
            and hsd_status not in {"closed", "complete", "implemented", "rejected"}
            and not stale_hsd
            and owner_matches
        ):
            key = (group_name.casefold(), "hsd", hsd_id.casefold())
            candidates.setdefault(key, {
                "group_name": group_name,
                "source_type": "hsd",
                "ips_case_number": ips_id,
                "jira_id": jira_key,
                "hsd_id": hsd_id,
                "title": title,
                "technology": technology,
                "is_field_issue": "field issue" in title.casefold(),
            })

    return list(candidates.values())


def yearly_weight_rows(
    records: Iterable[dict[str, Any]], group_field: str, year: int | None = None
) -> list[dict[str, Any]]:
    """Return deduplicated source rows contributing to annual issue counts."""
    jira_rows: dict[tuple[str, str], dict[str, Any]] = {}
    ips_rows: dict[tuple[str, str], dict[str, Any]] = {}
    promoted_ips: set[tuple[str, str]] = set()
    hsd_rows: dict[tuple[str, str], dict[str, Any]] = {}
    promoted_hsd: set[tuple[str, str]] = set()

    for row in records:
        if year is not None and not _in_year(bug_created_date(row), year):
            continue
        group_name = _group_name(row, group_field)
        group_key = group_name.casefold()
        title = _text(row.get("ips_title")) or _text(row.get("jira_summary")) or _text(row.get("hsd_title"))
        common = {
            "group_name": group_name,
            "title": title,
            "technology": _technology(row),
            "is_field_issue": "field issue" in title.casefold(),
        }

        jira_id = _valid_id(row.get("jira_id")) or _valid_id(row.get("ips_jira_id"))
        if jira_id:
            jira_rows.setdefault((group_key, jira_id.casefold()), {**common, "source_type": "jira"})

        ips_id = _valid_ips_id(row.get("ips_case_number"))
        if ips_id:
            key = (group_key, ips_id)
            ips_rows.setdefault(key, {**common, "source_type": "ips"})
            if _normalized(row.get("ips_jira_promo_status")) in {"promoted", "done"}:
                promoted_ips.add(key)

        hsd_id = _valid_id(row.get("hsd_id"))
        if hsd_id:
            key = (group_key, hsd_id.casefold())
            hsd_rows.setdefault(key, {**common, "source_type": "hsd"})
            if _valid_id(row.get("hsd_promoted_id")):
                promoted_hsd.add(key)

    return [
        *jira_rows.values(),
        *(row for key, row in ips_rows.items() if key not in promoted_ips),
        *(row for key, row in hsd_rows.items() if key not in promoted_hsd),
    ]


def _metrics(records: list[dict[str, Any]], year: int | None = None) -> dict[str, Any]:
    year_records = records if year is None else [row for row in records if _in_year(bug_created_date(row), year)]
    ips_records = year_records
    jira_records = year_records
    hsd_records = year_records

    ips_ids = {_valid_ips_id(row.get("ips_case_number")) for row in ips_records}
    ips_ids.discard("")
    promoted_ips = {
        _valid_ips_id(row.get("ips_case_number"))
        for row in ips_records
        if _normalized(row.get("ips_jira_promo_status")) in {"promoted", "done"}
    }
    promoted_ips.discard("")

    hsd_ids = {_valid_id(row.get("hsd_id")) for row in hsd_records}
    hsd_ids.discard("")
    promoted_hsd_rows = sum(bool(_valid_id(row.get("hsd_promoted_id"))) for row in hsd_records)

    jira_ids = {
        _valid_id(row.get("jira_id")) or _valid_id(row.get("ips_jira_id"))
        for row in jira_records
    }
    jira_ids.discard("")

    fixed_jira = sum(_is_true(row.get("jira_is_sw_change")) for row in jira_records)
    good_bug_denominator = 0
    good_bug_count = 0
    for row in jira_records:
        bug_status = bug_status_custom(row)
        is_closed_cae_customer_bug = (
            bug_status == "closed"
            and _normalized(row.get("jira_team")) == "cae"
            and _normalized(row.get("jira_found_by")) == "customer found"
        )
        if not is_closed_cae_customer_bug:
            continue
        good_bug_denominator += 1
        if _normalized(row.get("jira_state_reason")) in GOOD_BUG_REASONS:
            good_bug_count += 1

    total_ips = len(ips_ids)
    promoted_ips_count = len(ips_ids & promoted_ips)
    total_issue_count = (
        len(jira_ids)
        + max(0, total_ips - promoted_ips_count)
        + len(hsd_ids)
        - promoted_hsd_rows
    )
    return {
        "total_ips": total_ips,
        "promoted_ips": promoted_ips_count,
        "not_promoted_ips": max(0, total_ips - promoted_ips_count),
        "total_hsd": len(hsd_ids),
        "not_promoted_hsd": len(hsd_ids) - promoted_hsd_rows,
        "jira_count": len(jira_ids),
        "total_issue_count": total_issue_count,
        "jira_triage_rate": _ratio(promoted_ips_count, total_ips),
        "driver_fixed_rate": _ratio(fixed_jira, total_issue_count),
        "fixed_jira": fixed_jira,
        "good_bug_count": good_bug_count,
        "good_bug_denominator": good_bug_denominator,
        "good_bug_ratio": _ratio(good_bug_count, good_bug_denominator),
    }


def aggregate_yearly_records(
    records: Iterable[dict[str, Any]], group_field: str, year: int | None = None
) -> dict[str, Any]:
    """Aggregate source rows by engineer/customer and return a DAX-style total."""
    source_rows = list(records)
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in source_rows:
        group_name = _group_name(row, group_field)
        buckets[group_name].append(row)

    groups = [
        {"name": name, **_metrics(group_rows, year)}
        for name, group_rows in sorted(buckets.items(), key=lambda item: item[0].casefold())
    ]
    return {"groups": groups, "total": {"name": "Total", **_metrics(source_rows, year)}}