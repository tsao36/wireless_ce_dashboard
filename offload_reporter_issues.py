"""Offload mechanism for IPS issue load balancing.

Scans reporter workload in Postgres, and when any reporter exceeds the threshold,
identifies the most recently created IPS case among the overloaded reporters and
notifies the group that it should be reassigned to the least-loaded reporter.
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import re
import urllib.error
import urllib.request
from datetime import datetime, timezone
from html import escape
from typing import Any, Dict, List, Optional, Sequence, Tuple

from Wireless_bug_dashboard import DbConnector  # type: ignore
from Meeting_agenda_OneNote import (  # type: ignore
    get_graph_token_app_only,
    get_graph_token_delegated_with_secret,
    load_recipients,
    resolve_graph_client_secret,
    send_mail_via_graph,
)

LOG = logging.getLogger("ips_offload")

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_local_recipients = os.path.join(SCRIPT_DIR, "recipients.json")
_parent_recipients = os.path.join(SCRIPT_DIR, "..", "06_reporting_and_notifications", "recipients.json")
DEFAULT_RECIPIENTS_PATH = os.path.abspath(_local_recipients if os.path.exists(_local_recipients) else _parent_recipients)
DASHBOARD_URL = "https://wireless-ce-dashboard.intel.com/"

_SHARED_LOG_FIELDS = [
    "timestamp_utc",
    "table",
    "event_type",
    "subject",
    "case_number",
    "from_owner",
    "to_owner",
    "status",
    "reason",
    "source_count",
    "target_count",
    "trigger_count",
    "reminder_recipient_count",
    "recommendation_recipient_count",
    "shared_file_url",
]


def _setup_logging(level: str = "INFO") -> None:
    log_level = getattr(logging, str(level or "INFO").upper(), logging.INFO)
    logging.basicConfig(level=log_level, format="%(asctime)s [%(levelname)s] %(message)s")
    LOG.setLevel(log_level)

# Dedupe no longer needed if ips_jira_bugs is unique per key.
USE_DEDUP = False

ALLOWED_REPORTERS = [
    "Brenton Wu",
    "Jonathan Tsao",
    "KJ Fang",
    "Zhiwei He",
    "Frank Lee",
    "Frank Yang",
    "Charles Chu",
    "Zhiqiang Cai",
    "Timdaway Lai",
    "Zhanying Gao",
    "Jackx Lee",
    "Lydiax Chien",
    "Johnsonx Su",
    "Xihaox Yang",
    "Henryx Su",
    "Bingyue Sun",
    "Bing Chang",
    "Leaweix Chen",
    "Steven1 Chen",
    "Wesley Kuo",
    "Tonyx Yeh",
    "Juan Zou",
    "Matt Chen",
    "Yu-wei Chen",
]

NO_OFFLOAD_SOURCE_REPORTERS = {
    "henryx su",
    "jackx lee",
}

# Always exclude these reporters from offload rotation candidate lists.
ALWAYS_EXCLUDED_REPORTERS = {
    "jonathan tsao",
    "joanthan tsao",
}

# Source queue for offload recommendations (new rule: offload from Jonathan queue).
OFFLOAD_QUEUE_SOURCE_REPORTERS = {
    "jonathan tsao",
}

WIFI_GROUP_REPORTERS = {
    "brenton wu",
    "kj fang",
    "frank lee",
    "frank yang",
    "charles chu",
    "zhiqiang cai",
    "timdaway lai",
}

BT_GROUP_REPORTERS = {
    "bingyue sun",
    "steven1 chen",
    "wesley kuo",
    "juan zou",
    "matt chen",
    "yu-wei chen",
    "brenton wu",
}


def _groups_for_reporter(reporter: str) -> set[str]:
    groups: set[str] = set()
    norm = _normalize_reporter(reporter)
    if norm in WIFI_GROUP_REPORTERS:
        groups.add("wifi")
    if norm in BT_GROUP_REPORTERS:
        groups.add("bt")
    return groups


def _is_always_excluded_reporter(reporter: str) -> bool:
    reporter_norm = _normalize_reporter(reporter)
    if not reporter_norm:
        return False
    if reporter_norm in ALWAYS_EXCLUDED_REPORTERS:
        return True

    raw = _env_str("OFFLOAD_ALWAYS_EXCLUDED_ENGINEERS", "")
    if not raw:
        raw = _env_str("OFFLOAD_ALWAYS_EXCLUDED_REPORTERS", "")
    if not raw:
        return False
    extra = {
        _normalize_reporter(part)
        for part in re.split(r"[;,]", raw)
        if _normalize_reporter(part)
    }
    return reporter_norm in extra


def _env_str(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip() or default


def _append_shared_csv_log(
    csv_path: str,
    *,
    table: str,
    event_type: str,
    subject: str = "",
    case_number: str = "",
    from_owner: str = "",
    to_owner: str = "",
    status: str = "",
    reason: str = "",
    source_count: Optional[float] = None,
    target_count: Optional[float] = None,
    trigger_count: int = 0,
    reminder_recipient_count: int = 0,
    recommendation_recipient_count: int = 0,
    shared_file_url: str = "",
) -> None:
    if not csv_path:
        return

    try:
        parent = os.path.dirname(csv_path)
        if parent:
            os.makedirs(parent, exist_ok=True)

        write_header = not os.path.exists(csv_path) or os.path.getsize(csv_path) == 0
        with open(csv_path, "a", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=_SHARED_LOG_FIELDS)
            if write_header:
                writer.writeheader()

            writer.writerow(
                {
                    "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                    "table": table,
                    "event_type": event_type,
                    "subject": subject,
                    "case_number": case_number,
                    "from_owner": from_owner,
                    "to_owner": to_owner,
                    "status": status,
                    "reason": reason,
                    "source_count": "" if source_count is None else f"{float(source_count):.2f}",
                    "target_count": "" if target_count is None else f"{float(target_count):.2f}",
                    "trigger_count": int(trigger_count),
                    "reminder_recipient_count": int(reminder_recipient_count),
                    "recommendation_recipient_count": int(recommendation_recipient_count),
                    "shared_file_url": shared_file_url,
                }
            )
    except Exception as exc:
        LOG.warning("Failed to append shared CSV log %s: %s", csv_path, exc)


def _send_teams_webhook(webhook_url: str, title: str, message: str) -> bool:
    if not webhook_url:
        return False

    payload = {
        "text": f"**{title}**\n\n{message}",
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        webhook_url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            code = int(getattr(resp, "status", 200))
            if code >= 300:
                LOG.warning("Teams webhook returned non-success status: %s", code)
                return False
        return True
    except urllib.error.URLError as exc:
        LOG.warning("Teams webhook send failed: %s", exc)
        return False


def _validate_table_name(name: str) -> None:
    if not re.match(r"^[a-zA-Z0-9_.]+$", name):
        raise ValueError(f"Invalid table name: {name}")


def _split_table(table_name: str) -> Tuple[str, str]:
    if "." in table_name:
        schema, table = table_name.rsplit(".", 1)
    else:
        schema, table = "public", table_name
    return schema.strip().lower(), table.strip().lower()


def _owner_source_column(columns: Optional[set[str]] = None) -> str:
    if columns and _has(columns, "engineer"):
        return "engineer"
    return "reporter"


def _owner_value_expr(columns: Optional[set[str]] = None) -> str:
    owner_col = _owner_source_column(columns)
    base_expr = f"COALESCE({owner_col}::text, '')"

    # Keep offload owner resolution aligned with dashboard engineer rule for Matt Chen.
    if owner_col != "engineer" or not columns:
        return base_expr

    if not _has(columns, "jira_assignee") or not _has(columns, "jira_team"):
        return base_expr

    reporter_expr = "COALESCE(reporter::text, '')" if _has(columns, "reporter") else "''"
    assignee_upper = "UPPER(TRIM(COALESCE(jira_assignee::text, '')))"
    team_lower = "LOWER(TRIM(COALESCE(jira_team::text, '')))"

    matt_from_reporter_or_assignee = (
        f"(UPPER(TRIM({reporter_expr})) = 'MATT CHEN' OR {assignee_upper} = 'MATT CHEN')"
    )

    if _has(columns, "jira_external_assignee"):
        external_upper = "UPPER(TRIM(COALESCE(jira_external_assignee::text, '')))"
        matt_allowed = (
            f"({team_lower} <> 'googledb' OR ({assignee_upper} = 'MATT CHEN' AND {external_upper} = 'MATT CHEN'))"
        )
    else:
        matt_allowed = "TRUE"

    should_be_matt = f"({matt_from_reporter_or_assignee} AND {matt_allowed})"

    return (
        "(CASE "
        f"WHEN {should_be_matt} THEN 'Matt Chen' "
        f"WHEN UPPER(TRIM({base_expr})) = 'MATT CHEN' THEN 'NA' "
        f"ELSE {base_expr} END)"
    )


def _reporter_expr(columns: Optional[set[str]] = None) -> str:
    owner_expr = _owner_value_expr(columns)
    return f"NULLIF(NULLIF(TRIM({owner_expr}), ''), 'NA')"


def _reporter_norm_expr(columns: Optional[set[str]] = None) -> str:
    owner_expr = _owner_value_expr(columns)
    return f"LOWER(TRIM(COALESCE({owner_expr}, '')))"


def _issue_case_expr() -> str:
    return "NULLIF(NULLIF(TRIM(ips_case_number::text), ''), 'NA')"


def _normalize_reporter(name: str) -> str:
    return (name or "").strip().lower()


def _allowed_reporters() -> List[str]:
    return [_normalize_reporter(name) for name in ALLOWED_REPORTERS if _normalize_reporter(name)]


def _allowed_reporters_display_map() -> Dict[str, str]:
    display_map: Dict[str, str] = {}
    for name in ALLOWED_REPORTERS:
        text = str(name or "").strip()
        norm = _normalize_reporter(text)
        if norm and norm not in display_map:
            display_map[norm] = text
    return display_map


def _allowed_reporters_array_sql() -> str:
    reporters = _allowed_reporters()
    if not reporters:
        return "NULL"
    escaped = [name.replace("'", "''") for name in reporters]
    inner = ", ".join([f"'{name}'" for name in escaped])
    return f"ARRAY[{inner}]"


def _sql_literal(value: str) -> str:
    escaped = (value or "").replace("'", "''")
    return f"'{escaped}'"


def _sql_array_literal(values: Sequence[str], *, lower: bool = False) -> str:
    if not values:
        return "ARRAY[]::text[]"
    cleaned = []
    for value in values:
        text = (value or "")
        if lower:
            text = text.lower()
        cleaned.append(_sql_literal(text))
    inner = ", ".join(cleaned)
    return f"ARRAY[{inner}]"


def _resolve_reporter_list(db: DbConnector, table: str, reporter: str) -> List[str]:
    if not reporter:
        return []
    columns = _get_table_columns(db, table)
    owner_expr = _reporter_expr(columns)
    owner_norm_expr = _reporter_norm_expr(columns)
    query = f"""
        SELECT DISTINCT {owner_expr} AS reporter
        FROM {table}
        WHERE {owner_expr} IS NOT NULL
          AND {owner_norm_expr} LIKE %s
        ORDER BY reporter;
    """
    prefix = (reporter or "").strip().lower() + "%"
    rows = db.query_rows(query, [prefix])
    names = [str(r.get("reporter")) for r in rows if r.get("reporter")]
    return names or [reporter]


def _created_date_filter_expr(columns: set[str], start_date: str) -> Optional[str]:
    created_expr = _created_date_expr(columns)
    if created_expr == "NULL":
        return None
    return f"{created_expr} >= DATE '{start_date}'"


def _created_date_filter_alias_expr(start_date: str) -> str:
    return f"created_date >= DATE '{start_date}'"


def _created_date_expr(columns: set[str]) -> str:
    parts = []
    if _has(columns, "ips_created_date"):
        parts.append("ips_created_date::date")
    if _has(columns, "bug_created_date"):
        parts.append("bug_created_date::date")
    if _has(columns, "jira_created_date"):
        parts.append("jira_created_date::date")
    if _has(columns, "hsd_submitted_date"):
        parts.append("hsd_submitted_date::date")
    if not parts:
        return "NULL"
    if len(parts) == 1:
        return parts[0]
    return "COALESCE(" + ", ".join(parts) + ")"


def _title_expr(columns: Optional[set] = None) -> str:
    parts = []
    if columns is None or _has(columns, "ips_title"):
        parts.append("NULLIF(NULLIF(TRIM(ips_title), ''), 'NA')")
    # Only include jira_title when ips_title is absent (jira_title may exist in
    # information_schema but not be selectable in some table configurations)
    if not parts and (columns is None or _has(columns, "jira_title")):
        parts.append("NULLIF(NULLIF(TRIM(jira_title), ''), 'NA')")
    if not parts:
        return "NULL"
    return f"COALESCE({', '.join(parts)})" if len(parts) > 1 else parts[0]


def _jira_key_expr(columns: set[str]) -> str:
    jira_parts: List[str] = []
    if _has(columns, "jira_id"):
        jira_parts.append("NULLIF(NULLIF(TRIM(jira_id::text), ''), 'NA')")
    if _has(columns, "ips_jira_id"):
        jira_parts.append("NULLIF(NULLIF(TRIM(ips_jira_id::text), ''), 'NA')")
    if not jira_parts:
        return "NULL"
    if len(jira_parts) == 1:
        return jira_parts[0]
    return "COALESCE(" + ", ".join(jira_parts) + ")"


def _get_table_columns(db: DbConnector, table: str) -> set[str]:
    schema, name = _split_table(table)
    query = """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = %s AND table_name = %s;
    """
    rows = db.query_rows(query, [schema, name])
    return {str(r.get("column_name") or "").lower() for r in rows if r.get("column_name")}


def _has(columns: set[str], name: str) -> bool:
    return name.lower() in columns


def _status_open_condition(column: str, disallowed: Sequence[str]) -> str:
    # Treat empty/NA as NULL, then allow only if it does NOT contain any disallowed token
    cleaned = f"NULLIF(NULLIF(TRIM({column}), ''), 'NA')"
    cleaned_lower = f"LOWER({cleaned})"
    parts = [f"POSITION('{d}' IN {cleaned_lower}) = 0" for d in disallowed]
    cond = " AND ".join(parts) if parts else "TRUE"
    return f"({cleaned} IS NULL OR ({cond}))"


def _bug_status_custom_open_expr(columns: set[str]) -> str:
    has_jira_col = _has(columns, "jira_status")
    has_ips_sub_col = _has(columns, "ips_sub_status")
    has_ips_status_col = _has(columns, "ips_status")
    has_hsd_col = _has(columns, "hsd_status_reason")

    # If we have none of the status columns, do not filter anything out.
    if not (has_jira_col or has_ips_sub_col or has_ips_status_col or has_hsd_col):
        return "TRUE"

    jira_expr = "''"
    ips_sub_expr = "''"
    ips_status_expr = "''"
    hsd_expr = "''"
    if has_jira_col:
        jira_expr = "LOWER(TRIM(COALESCE(NULLIF(jira_status::text, 'NA'), '')))"
    if has_ips_sub_col:
        ips_sub_expr = "LOWER(TRIM(COALESCE(NULLIF(NULLIF(ips_sub_status::text, ''), 'NA'), 'na')))"
    if has_ips_status_col:
        ips_status_expr = "LOWER(TRIM(COALESCE(NULLIF(NULLIF(ips_status::text, ''), 'NA'), 'na')))"
    if has_hsd_col:
        hsd_expr = "LOWER(TRIM(COALESCE(NULLIF(NULLIF(hsd_status_reason::text, ''), 'NA'), 'na')))"

    has_jira_status = f"({jira_expr} <> '' AND {jira_expr} <> 'na')"
    jira_closed_custom = f"({jira_expr} IN ('closed', 'verify', 'implemented'))"

    has_hsd_status = f"({hsd_expr} <> '' AND {hsd_expr} <> 'na')"
    hsd_closed = f"({hsd_expr} IN ('closed', 'complete', 'implemented', 'rejected'))"
    hsd_openish = f"({hsd_expr} IN ('open', 'investigating', 'na'))"

    # Treat open/investigating/na in either IPS sub-status or IPS status as open-ish; close-pending stays open-ish.
    ips_openish = f"({ips_sub_expr} IN ('open', 'investigating', 'close-pending', 'na') OR {ips_status_expr} IN ('open', 'investigating', 'na'))"
    ips_closed = f"({ips_sub_expr} = 'closed' OR {ips_status_expr} = 'closed')"

    return (
        "(CASE "
        f"WHEN {has_jira_status} AND {jira_closed_custom} THEN FALSE "
        f"WHEN {has_jira_status} AND NOT {jira_closed_custom} THEN TRUE "
        f"WHEN {has_hsd_status} AND {hsd_closed} THEN FALSE "
        f"WHEN {has_hsd_status} AND {hsd_openish} THEN TRUE "
        f"WHEN {ips_closed} THEN FALSE "
        f"WHEN {ips_openish} THEN TRUE "
        "ELSE TRUE END)"
    )


def _dashboard_jira_work_expr(columns: set[str]) -> str:
    # This expression runs on normalized/base CTE rows where jira linkage is projected as jira_id alias.
    jira_linked = "(jira_id IS NOT NULL)"

    if _has(columns, "jira_status"):
        jira_status_expr = "LOWER(TRIM(COALESCE(NULLIF(jira_status::text, 'NA'), '')))"
        jira_open = f"{jira_status_expr} IN ('new', 'open', 'in progress', 'pending')"
    else:
        jira_open = "TRUE"

    if _has(columns, "ips_status"):
        ips_status_expr = "LOWER(TRIM(COALESCE(NULLIF(ips_status::text, 'NA'), '')))"
        ips_open = f"{ips_status_expr} <> 'closed'"
    else:
        ips_open = "TRUE"

    if _has(columns, "ips_sub_status"):
        ips_sub_status_expr = "LOWER(TRIM(COALESCE(NULLIF(ips_sub_status::text, 'NA'), '')))"
        not_close_pending = f"({ips_sub_status_expr} = '' OR {ips_sub_status_expr} <> 'close-pending')"
    else:
        not_close_pending = "TRUE"

    return f"({jira_linked}) AND ({jira_open}) AND ({ips_open}) AND ({not_close_pending})"


def _resolve_count_filter_expr(columns: set[str], count_profile: str) -> str:
    _ = count_profile
    return _bug_status_custom_open_expr(columns)


def _base_filter_expr(columns: set[str], jira_id_expr: str, start_date: str, *, include_hsd: bool = True) -> str:
    base = "TRUE"
    created_filter = _created_date_filter_expr(columns, start_date)
    if created_filter:
        base = f"({base}) AND ({_created_date_filter_alias_expr(start_date)})"
    status_open_expr = _bug_status_custom_open_expr(columns)
    if include_hsd and _has(columns, "hsd_id"):
        hsd_present = "UPPER(TRIM(COALESCE(hsd_id::text, ''))) NOT IN ('', 'NA')"
        status_open_expr = f"({status_open_expr}) OR ({hsd_present})"
    base = f"({base}) AND ({status_open_expr})"
    # Always exclude the DB health sentinel row (used for PowerBI monitoring)
    _title_col = "ips_title" if _has(columns, "ips_title") else ("jira_title" if _has(columns, "jira_title") else None)
    if _title_col:
        base = f"({base}) AND (COALESCE(TRIM({_title_col}::text), '') NOT LIKE '[DB HEALTH]%')"
    return base


def _normalized_filter_expr(ips_case_num_expr: str, jira_id_expr: str) -> str:
    if not USE_DEDUP:
        return "TRUE"
    return (
        f"(({jira_id_expr} IS NOT NULL AND jira_rn = 1) "
        f"OR ({jira_id_expr} IS NULL AND ({ips_case_num_expr} IS NULL OR ips_rn = 1)))"
    )


def _ips_order_expr(columns: set[str]) -> str:
    if _has(columns, "ips_last_modified_date"):
        return "ips_last_modified_date::timestamp"
    if _has(columns, "ips_last_modified_days"):
        return "(CURRENT_DATE - (CASE WHEN ips_last_modified_days::text ~ '^[0-9]+(\\.[0-9]+)?$' THEN ips_last_modified_days::numeric ELSE NULL END) * INTERVAL '1 day')::timestamp"
    if _has(columns, "ips_created_date"):
        return "ips_created_date::timestamp"
    if _has(columns, "bug_created_date"):
        return "bug_created_date::timestamp"
    return "NULL::timestamp"


def _ips_status_rank_expr(columns: set[str]) -> str:
    if not (_has(columns, "ips_status") or _has(columns, "ips_sub_status")):
        return "0"
    parts = []
    if _has(columns, "ips_status"):
        parts.append(f"NOT {_status_open_condition('ips_status', ['closed', 'invalid'])}")
    if _has(columns, "ips_sub_status"):
        parts.append(f"NOT {_status_open_condition('ips_sub_status', ['closed', 'invalid'])}")
    cond = " OR ".join(parts) if parts else "FALSE"
    return f"CASE WHEN {cond} THEN 1 ELSE 0 END"


def _jira_status_rank_expr(columns: set[str]) -> str:
    if not _has(columns, "jira_status"):
        return "0"
    cond = f"NOT {_status_open_condition('jira_status', ['closed', 'implemented', 'verify'])}"
    return f"CASE WHEN {cond} THEN 1 ELSE 0 END"


def _get_reporter_current_counts(
    db: DbConnector,
    table: str,
    stale_days: int,
    *,
    count_profile: str = "offload",
) -> List[Dict[str, Any]]:
    columns = _get_table_columns(db, table)
    profile_is_dashboard = False
    allowed_reporters_array = _allowed_reporters_array_sql()

    created_expr = _created_date_expr(columns)

    if _has(columns, "ips_case_number"):
        ips_case_num_expr = (
            "CASE WHEN ips_case_number::text ~ '^[0-9]+$' "
            "AND ips_case_number::int > 0 "
            "THEN ips_case_number::int ELSE NULL END"
        )
        ips_case_valid_cond = f"{ips_case_num_expr} > 0"
    else:
        ips_case_num_expr = "NULL"
        ips_case_valid_cond = "FALSE"

    ips_sub_status_expr = None
    if _has(columns, "ips_sub_status"):
        ips_sub_status_expr = "LOWER(TRIM(COALESCE(NULLIF(ips_sub_status::text, 'NA'), ''))" + ")"

    ips_sub_status_expr = None
    if _has(columns, "ips_sub_status"):
        ips_sub_status_expr = "LOWER(TRIM(COALESCE(NULLIF(ips_sub_status::text, 'NA'), '')))"

    not_closed_cond = "TRUE"
    if ips_sub_status_expr:
        not_closed_cond = f"{ips_sub_status_expr} <> 'closed'"

    ips_sub_status_expr = None
    if _has(columns, "ips_sub_status"):
        ips_sub_status_expr = "LOWER(TRIM(COALESCE(NULLIF(ips_sub_status::text, 'NA'), '')))"

    ips_sub_status_expr = None
    if _has(columns, "ips_sub_status"):
        ips_sub_status_expr = "LOWER(TRIM(COALESCE(NULLIF(ips_sub_status::text, 'NA'), '')))"

    ips_sub_status_expr = None
    if _has(columns, "ips_sub_status"):
        ips_sub_status_expr = "LOWER(TRIM(COALESCE(NULLIF(ips_sub_status::text, 'NA'), '')))"

    ips_sub_status_expr = None
    if _has(columns, "ips_sub_status"):
        ips_sub_status_expr = "LOWER(TRIM(COALESCE(NULLIF(ips_sub_status::text, 'NA'), '')))"

    if _has(columns, "ips_jira_promo_status"):
        promo_status_expr = "UPPER(TRIM(ips_jira_promo_status))"
        unpromoted_ips_cond = (
            f"{ips_case_valid_cond} AND {promo_status_expr} IN ('NOT YET PROMOTED', 'FAILED') AND {not_closed_cond}"
        )
        promoted_ips_cond = f"{ips_case_valid_cond} AND {promo_status_expr} IN ('PROMOTED', 'DONE')"
    elif _has(columns, "is_ips_promoted_to_jira"):
        unpromoted_ips_cond = f"{ips_case_valid_cond} AND NOT is_ips_promoted_to_jira AND {not_closed_cond}"
        promoted_ips_cond = f"{ips_case_valid_cond} AND is_ips_promoted_to_jira"
    elif _has(columns, "ips_jira_id"):
        unpromoted_ips_cond = f"{ips_case_valid_cond} AND ips_jira_id IS NULL AND {not_closed_cond}"
        promoted_ips_cond = f"{ips_case_valid_cond} AND ips_jira_id IS NOT NULL"
    else:
        unpromoted_ips_cond = f"{ips_case_valid_cond} AND {not_closed_cond}"
        promoted_ips_cond = "FALSE"
    # Close-pending is excluded from loading entirely.
    if ips_sub_status_expr:
        counted_unpromoted_ips_cond = f"({unpromoted_ips_cond}) AND ({ips_sub_status_expr} <> 'close-pending')"
    else:
        counted_unpromoted_ips_cond = unpromoted_ips_cond
    num_unpromoted_ips = f"CASE WHEN {counted_unpromoted_ips_cond} THEN 1 ELSE 0 END"
    num_promoted_ips = f"CASE WHEN {promoted_ips_cond} THEN 1 ELSE 0 END"

    if profile_is_dashboard and _has(columns, "jira_id"):
        # PowerBI DAX total_num_of_jira counts distinct jira_id only.
        jira_id_expr = "NULLIF(NULLIF(TRIM(jira_id::text), ''), 'NA')"
    else:
        jira_id_expr = _jira_key_expr(columns)
    if jira_id_expr != "NULL":
        jira_count_cond = f"{jira_id_expr} IS NOT NULL"
        if _has(columns, "ips_status"):
            ips_status_norm = "LOWER(TRIM(COALESCE(NULLIF(ips_status::text, 'NA'), '')))"
            jira_count_cond = f"({jira_count_cond}) AND ({ips_status_norm} <> 'closed')"
        if _has(columns, "ips_sub_status"):
            ips_sub_status_norm = "LOWER(TRIM(COALESCE(NULLIF(ips_sub_status::text, 'NA'), '')))"
            jira_count_cond = (
                f"({jira_count_cond}) AND "
                f"({ips_sub_status_norm} NOT IN ('close-pending', 'pending-closed'))"
            )
        num_jira = f"CASE WHEN {jira_count_cond} THEN 1 ELSE 0 END"
    else:
        num_jira = "0"

    base_filter = _resolve_count_filter_expr(columns, count_profile)

    # Only count issues created in the current calendar year
    if created_expr != "NULL":
        base_filter = f"({base_filter}) AND (created_date >= DATE_TRUNC('year', CURRENT_DATE))"

    owner_match = "TRUE"
    hsd_presence_expr = "''"
    hsd_not_promoted_cond = "FALSE"
    hsd_not_closed_cond = "TRUE"
    if _has(columns, "hsd_status_reason"):
        hsd_status_norm = "LOWER(TRIM(COALESCE(NULLIF(NULLIF(hsd_status_reason::text, ''), 'NA'), 'na')))"
        hsd_not_closed_cond = f"{hsd_status_norm} NOT IN ('closed', 'complete', 'implemented', 'rejected')"
    if _has(columns, "hsd_id"):
        hsd_presence_expr = "UPPER(TRIM(COALESCE(hsd_id::text, '')))"
        if _has(columns, "hsd_owner"):
            owner_expr = "LOWER(TRIM(COALESCE(hsd_owner::text, '')))"
            reporter_expr = _reporter_norm_expr(columns)
            owner_match = f"({owner_expr} <> '' AND {owner_expr} = {reporter_expr})"
        if _has(columns, "hsd_promoted_id"):
            hsd_promoted_expr = "UPPER(TRIM(COALESCE(hsd_promoted_id::text, '')))"
            hsd_not_promoted_cond = (
                f"{hsd_presence_expr} <> '' AND {hsd_presence_expr} <> 'NA' AND "
                f"({hsd_promoted_expr} = '' OR {hsd_promoted_expr} = 'NA') AND {owner_match} AND {hsd_not_closed_cond}"
            )
            num_unpromoted_hsd = (
                "CASE WHEN "
                f"{hsd_not_promoted_cond} "
                "THEN 1 ELSE 0 END"
            )
        else:
            hsd_not_promoted_cond = (
                f"{hsd_presence_expr} <> '' AND {hsd_presence_expr} <> 'NA' AND {owner_match} AND {hsd_not_closed_cond}"
            )
            num_unpromoted_hsd = (
                "CASE WHEN "
                f"{hsd_not_promoted_cond} "
                "THEN 1 ELSE 0 END"
            )
    else:
        num_unpromoted_hsd = "0"

    if _has(columns, "hsd_id") and _has(columns, "hsd_submitted_date"):
        # stale HSD: unpromoted HSD where submitted date is older than 30 days
        stale_hsd_scope_cond = hsd_not_promoted_cond
        num_stale_hsd = (
            "CASE WHEN "
            f"{stale_hsd_scope_cond} AND "
            "hsd_submitted_date IS NOT NULL AND "
            "(CURRENT_DATE - hsd_submitted_date::date) > 30 "
            "THEN 1 ELSE 0 END"
        )
    else:
        num_stale_hsd = "0"

    hsd_status_reason_expr = "NULL"
    if _has(columns, "hsd_status_reason"):
        hsd_status_reason_expr = "LOWER(TRIM(COALESCE(NULLIF(NULLIF(hsd_status_reason::text, ''), 'NA'), 'na')))"

    hsd_status_reason_expr = "NULL"
    if _has(columns, "hsd_status_reason"):
        hsd_status_reason_expr = "LOWER(TRIM(COALESCE(NULLIF(NULLIF(hsd_status_reason::text, ''), 'NA'), 'na')))"

    num_close_pending = "0"

    jira_stale_gate = "TRUE"
    if _has(columns, "jira_status"):
        jira_status_norm = "LOWER(TRIM(COALESCE(jira_status::text, '')))"
        jira_stale_gate = f"({jira_status_norm} IN ('', 'na', 'closed', 'verify', 'implemented'))"

    stale_conds = []
    if _has(columns, "ips_last_modified_days"):
        last_mod_days = (
            "CASE WHEN ips_last_modified_days::text ~ '^[0-9]+(\\.[0-9]+)?$' "
            "THEN ips_last_modified_days::numeric ELSE NULL END"
        )
    elif _has(columns, "ips_last_modified_date"):
        last_mod_days = "(CURRENT_DATE - ips_last_modified_date::date)"
    else:
        last_mod_days = None

    if _has(columns, "ips_open_days"):
        open_days = (
            "CASE WHEN ips_open_days::text ~ '^[0-9]+(\\.[0-9]+)?$' "
            "THEN ips_open_days::numeric ELSE NULL END"
        )
    elif _has(columns, "ips_created_date"):
        open_days = "(CURRENT_DATE - ips_created_date::date)"
    elif _has(columns, "bug_created_date"):
        open_days = "(CURRENT_DATE - bug_created_date::date)"
    else:
        open_days = None

    if last_mod_days:
        stale_conds.append(f"{last_mod_days} > 21")
    if open_days:
        stale_conds.append(f"{open_days} > 30")
    num_stale_promoted = "0"
    if stale_conds:
        stale_cond = " OR ".join(stale_conds)
        not_close_pending = "TRUE"
        if ips_sub_status_expr:
            not_close_pending = f"{ips_sub_status_expr} <> 'close-pending'"
        if profile_is_dashboard:
            # PowerBI DAX: stale IPS = stale filter applied to num_of_not_promoted_ips (total_ips - promoted_ips).
            num_stale = f"CASE WHEN ({ips_case_valid_cond}) AND ({stale_cond}) AND {not_close_pending} AND {jira_stale_gate} THEN 1 ELSE 0 END"
            num_stale_promoted = (
                f"CASE WHEN ({promoted_ips_cond}) AND ({stale_cond}) AND {not_close_pending} AND {jira_stale_gate} THEN 1 ELSE 0 END"
            )
        else:
            num_stale = f"CASE WHEN ({unpromoted_ips_cond}) AND ({stale_cond}) AND {not_close_pending} AND {jira_stale_gate} THEN 1 ELSE 0 END"
    else:
        num_stale = "0"
    hsd_id_expr = "NULL"
    if _has(columns, "hsd_id"):
        hsd_id_expr = "NULLIF(NULLIF(TRIM(hsd_id::text), ''), 'NA')"

    hsd_status_reason_expr = "NULL"
    if _has(columns, "hsd_status_reason"):
        hsd_status_reason_expr = "LOWER(TRIM(COALESCE(NULLIF(NULLIF(hsd_status_reason::text, ''), 'NA'), 'na')))"

    created_expr = _created_date_expr(columns)
    ips_order_expr = _ips_order_expr(columns)
    ips_status_rank = _ips_status_rank_expr(columns)
    jira_status_rank = _jira_status_rank_expr(columns)
    ips_status_expr = "ips_status" if _has(columns, "ips_status") else "NULL"
    ips_sub_status_expr = "ips_sub_status" if _has(columns, "ips_sub_status") else "NULL"
    jira_status_expr = "jira_status" if _has(columns, "jira_status") else "NULL"
    normalized_filter = "TRUE" if profile_is_dashboard else _normalized_filter_expr(ips_case_num_expr, jira_id_expr)
    query = f"""
        WITH base AS (
            SELECT
                {_reporter_expr(columns)} AS reporter,
                {ips_case_num_expr} AS ips_case_number,
                {jira_id_expr} AS jira_id,
                {hsd_id_expr} AS hsd_id,
                {hsd_status_reason_expr} AS hsd_status_reason,
                {created_expr} AS created_date,
                {ips_status_expr} AS ips_status,
                {ips_sub_status_expr} AS ips_sub_status,
                {jira_status_expr} AS jira_status,
                {num_unpromoted_ips} AS is_unpromoted_ips,
                {num_jira} AS is_jira,
                {num_unpromoted_hsd} AS is_unpromoted_hsd,
                {num_stale_hsd} AS is_stale_hsd,
                {num_promoted_ips} AS is_promoted_ips,
                {num_close_pending} AS is_close_pending,
                {num_stale} AS is_stale,
                {num_stale_promoted} AS is_stale_promoted,
                CASE
                    WHEN {ips_case_num_expr} IS NULL THEN 1
                    ELSE ROW_NUMBER() OVER (
                        PARTITION BY {ips_case_num_expr}
                        ORDER BY {ips_status_rank} DESC, {ips_order_expr} DESC NULLS LAST
                    )
                END AS ips_rn,
                CASE
                    WHEN {jira_id_expr} IS NULL THEN 1
                    ELSE ROW_NUMBER() OVER (
                        PARTITION BY {jira_id_expr}
                        ORDER BY {jira_status_rank} DESC, {ips_order_expr} DESC NULLS LAST
                    )
                END AS jira_rn
            FROM {table}
        ),
                normalized AS (
                        SELECT *
                        FROM base
                    WHERE {normalized_filter}
                            AND {base_filter}
                )
        SELECT
            reporter,
            COALESCE(COUNT(DISTINCT CASE WHEN is_unpromoted_hsd = 1 THEN hsd_id END), 0) AS num_unpromoted_hsd,
            COALESCE(COUNT(DISTINCT CASE WHEN is_stale_hsd = 1 THEN hsd_id END), 0) AS num_stale_hsd,
            CASE
                WHEN {str(profile_is_dashboard).upper()} THEN
                    GREATEST(
                        COALESCE(COUNT(DISTINCT CASE WHEN is_unpromoted_ips = 1 THEN ips_case_number END), 0)
                        - COALESCE(COUNT(DISTINCT CASE WHEN is_promoted_ips = 1 THEN ips_case_number END), 0),
                        0
                    )
                ELSE COALESCE(COUNT(DISTINCT CASE WHEN is_unpromoted_ips = 1 THEN ips_case_number END), 0)
            END AS num_unpromoted_ips,
            COALESCE(COUNT(DISTINCT CASE WHEN is_promoted_ips = 1 THEN ips_case_number END), 0) AS num_promoted_ips,
            COALESCE(COUNT(DISTINCT CASE WHEN is_jira = 1 THEN jira_id END), 0) AS num_jira,
            CASE
                WHEN {str(profile_is_dashboard).upper()} THEN
                    GREATEST(
                        COALESCE(COUNT(DISTINCT CASE WHEN is_stale = 1 THEN ips_case_number END), 0)
                        - COALESCE(COUNT(DISTINCT CASE WHEN is_stale_promoted = 1 THEN ips_case_number END), 0),
                        0
                    )
                ELSE COALESCE(COUNT(DISTINCT CASE WHEN is_stale = 1 THEN ips_case_number END), 0)
            END AS num_stale,
            COALESCE(COUNT(DISTINCT CASE WHEN is_close_pending = 1 THEN ips_case_number END), 0) AS num_close_pending,
            COALESCE(
                COUNT(DISTINCT CASE WHEN is_jira = 1 THEN jira_id END)
                + (
                    COALESCE(COUNT(DISTINCT CASE WHEN is_unpromoted_ips = 1 THEN ips_case_number END), 0)
                    - COALESCE(COUNT(DISTINCT CASE WHEN is_stale = 1 THEN ips_case_number END), 0)
                    + COALESCE(COUNT(DISTINCT CASE WHEN is_unpromoted_hsd = 1 THEN hsd_id END), 0)
                    - COALESCE(COUNT(DISTINCT CASE WHEN is_stale_hsd = 1 THEN hsd_id END), 0)
                ),
                0
            ) AS total_current_issue_count
        FROM normalized
        WHERE reporter IS NOT NULL
            {"AND LOWER(reporter) = ANY(" + allowed_reporters_array + ")" if allowed_reporters_array != "NULL" else ""}
        GROUP BY reporter
        ORDER BY total_current_issue_count DESC, reporter;
    """
    return db.query_rows(query, None)


def _get_reporter_current_breakdown(
    db: DbConnector,
    table: str,
    stale_days: int,
    *,
    filter_allowed: bool = True,
    count_profile: str = "offload",
) -> List[Dict[str, Any]]:
    columns = _get_table_columns(db, table)
    profile_is_dashboard = False
    allowed_reporters_array = _allowed_reporters_array_sql() if filter_allowed else "NULL"

    base_filter = "TRUE"

    if _has(columns, "ips_case_number"):
        ips_case_num_expr = (
            "CASE WHEN ips_case_number::text ~ '^[0-9]+$' "
            "AND ips_case_number::int > 0 "
            "THEN ips_case_number::int ELSE NULL END"
        )
        ips_case_valid_cond = f"{ips_case_num_expr} > 0"
    else:
        ips_case_num_expr = "NULL"
        ips_case_valid_cond = "FALSE"

    ips_sub_status_expr = None
    if _has(columns, "ips_sub_status"):
        ips_sub_status_expr = "LOWER(TRIM(COALESCE(NULLIF(ips_sub_status::text, 'NA'), '')))"

    not_closed_cond = "TRUE"
    if ips_sub_status_expr:
        not_closed_cond = f"{ips_sub_status_expr} <> 'closed'"

    if _has(columns, "ips_jira_promo_status"):
        promo_status_expr = "UPPER(TRIM(ips_jira_promo_status))"
        unpromoted_ips_cond = (
            f"{ips_case_valid_cond} AND {promo_status_expr} IN ('NOT YET PROMOTED', 'FAILED') AND {not_closed_cond}"
        )
        promoted_ips_cond = f"{ips_case_valid_cond} AND {promo_status_expr} IN ('PROMOTED', 'DONE')"
    elif _has(columns, "is_ips_promoted_to_jira"):
        unpromoted_ips_cond = f"{ips_case_valid_cond} AND NOT is_ips_promoted_to_jira AND {not_closed_cond}"
        promoted_ips_cond = f"{ips_case_valid_cond} AND is_ips_promoted_to_jira"
    elif _has(columns, "ips_jira_id"):
        unpromoted_ips_cond = f"{ips_case_valid_cond} AND ips_jira_id IS NULL AND {not_closed_cond}"
        promoted_ips_cond = f"{ips_case_valid_cond} AND ips_jira_id IS NOT NULL"
    else:
        unpromoted_ips_cond = f"{ips_case_valid_cond} AND {not_closed_cond}"
        promoted_ips_cond = "FALSE"
    # Close-pending is excluded from loading entirely.
    if ips_sub_status_expr:
        counted_unpromoted_ips_cond = f"({unpromoted_ips_cond}) AND ({ips_sub_status_expr} <> 'close-pending')"
    else:
        counted_unpromoted_ips_cond = unpromoted_ips_cond
    num_unpromoted_ips = f"CASE WHEN {counted_unpromoted_ips_cond} THEN 1 ELSE 0 END"
    num_promoted_ips = f"CASE WHEN {promoted_ips_cond} THEN 1 ELSE 0 END"

    if profile_is_dashboard and _has(columns, "jira_id"):
        # PowerBI DAX total_num_of_jira counts distinct jira_id only.
        jira_id_expr = "NULLIF(NULLIF(TRIM(jira_id::text), ''), 'NA')"
    else:
        jira_id_expr = _jira_key_expr(columns)
    if jira_id_expr != "NULL":
        jira_count_cond = f"{jira_id_expr} IS NOT NULL"
        if _has(columns, "ips_status"):
            ips_status_norm = "LOWER(TRIM(COALESCE(NULLIF(ips_status::text, 'NA'), '')))"
            jira_count_cond = f"({jira_count_cond}) AND ({ips_status_norm} <> 'closed')"
        if _has(columns, "ips_sub_status"):
            ips_sub_status_norm = "LOWER(TRIM(COALESCE(NULLIF(ips_sub_status::text, 'NA'), '')))"
            jira_count_cond = (
                f"({jira_count_cond}) AND "
                f"({ips_sub_status_norm} NOT IN ('close-pending', 'pending-closed'))"
            )
        num_jira = f"CASE WHEN {jira_count_cond} IS TRUE THEN 1 ELSE 0 END"
    else:
        num_jira = "0"

    base_filter = _resolve_count_filter_expr(columns, count_profile)

    # Only count issues created in the current calendar year
    _breakdown_created_expr = _created_date_expr(columns)
    if _breakdown_created_expr != "NULL":
        base_filter = f"({base_filter}) AND (created_date >= DATE_TRUNC('year', CURRENT_DATE))"

    owner_match = "TRUE"
    hsd_presence_expr = "''"
    hsd_not_promoted_cond = "FALSE"
    hsd_not_closed_cond = "TRUE"
    if _has(columns, "hsd_status_reason"):
        hsd_status_norm = "LOWER(TRIM(COALESCE(NULLIF(NULLIF(hsd_status_reason::text, ''), 'NA'), 'na')))"
        hsd_not_closed_cond = f"{hsd_status_norm} NOT IN ('closed', 'complete', 'implemented', 'rejected')"
    if _has(columns, "hsd_id"):
        hsd_presence_expr = "UPPER(TRIM(COALESCE(hsd_id::text, '')))"
        if _has(columns, "hsd_owner"):
            owner_expr = "LOWER(TRIM(COALESCE(hsd_owner::text, '')))"
            reporter_expr = _reporter_norm_expr(columns)
            owner_match = f"({owner_expr} <> '' AND {owner_expr} = {reporter_expr})"
        if _has(columns, "hsd_promoted_id"):
            hsd_promoted_expr = "UPPER(TRIM(COALESCE(hsd_promoted_id::text, '')))"
            hsd_not_promoted_cond = (
                f"{hsd_presence_expr} <> '' AND {hsd_presence_expr} <> 'NA' AND "
                f"({hsd_promoted_expr} = '' OR {hsd_promoted_expr} = 'NA') AND {owner_match} AND {hsd_not_closed_cond}"
            )
            num_unpromoted_hsd = (
                "CASE WHEN "
                f"{hsd_not_promoted_cond} "
                "THEN 1 ELSE 0 END"
            )
        else:
            hsd_not_promoted_cond = (
                f"{hsd_presence_expr} <> '' AND {hsd_presence_expr} <> 'NA' AND {owner_match} AND {hsd_not_closed_cond}"
            )
            num_unpromoted_hsd = (
                "CASE WHEN "
                f"{hsd_not_promoted_cond} "
                "THEN 1 ELSE 0 END"
            )
    else:
        num_unpromoted_hsd = "0"

    if _has(columns, "hsd_id") and _has(columns, "hsd_submitted_date"):
        # stale HSD: unpromoted HSD where submitted date is older than 30 days
        stale_hsd_scope_cond = hsd_not_promoted_cond
        num_stale_hsd = (
            "CASE WHEN "
            f"{stale_hsd_scope_cond} AND "
            "hsd_submitted_date IS NOT NULL AND "
            "(CURRENT_DATE - hsd_submitted_date::date) > 30 "
            "THEN 1 ELSE 0 END"
        )
    else:
        num_stale_hsd = "0"

    num_close_pending = "0"

    jira_stale_gate = "TRUE"
    if _has(columns, "jira_status"):
        jira_status_norm = "LOWER(TRIM(COALESCE(jira_status::text, '')))"
        jira_stale_gate = f"({jira_status_norm} IN ('', 'na', 'closed', 'verify', 'implemented'))"

    stale_conds = []
    if _has(columns, "ips_last_modified_days"):
        last_mod_days = (
            "CASE WHEN ips_last_modified_days::text ~ '^[0-9]+(\\.[0-9]+)?$' "
            "THEN ips_last_modified_days::numeric ELSE NULL END"
        )
    elif _has(columns, "ips_last_modified_date"):
        last_mod_days = "(CURRENT_DATE - ips_last_modified_date::date)"
    else:
        last_mod_days = None

    if _has(columns, "ips_open_days"):
        open_days = (
            "CASE WHEN ips_open_days::text ~ '^[0-9]+(\\.[0-9]+)?$' "
            "THEN ips_open_days::numeric ELSE NULL END"
        )
    elif _has(columns, "ips_created_date"):
        open_days = "(CURRENT_DATE - ips_created_date::date)"
    elif _has(columns, "bug_created_date"):
        open_days = "(CURRENT_DATE - bug_created_date::date)"
    else:
        open_days = None

    if last_mod_days:
        stale_conds.append(f"{last_mod_days} > 21")
    if open_days:
        stale_conds.append(f"{open_days} > 30")
    num_stale_promoted = "0"
    if stale_conds:
        stale_cond = " OR ".join(stale_conds)
        not_close_pending = "TRUE"
        if ips_sub_status_expr:
            not_close_pending = f"{ips_sub_status_expr} <> 'close-pending'"
        if profile_is_dashboard:
            # PowerBI DAX: stale IPS = stale filter applied to num_of_not_promoted_ips (total_ips - promoted_ips).
            num_stale = f"CASE WHEN ({ips_case_valid_cond}) AND ({stale_cond}) AND {not_close_pending} AND {jira_stale_gate} THEN 1 ELSE 0 END"
            num_stale_promoted = (
                f"CASE WHEN ({promoted_ips_cond}) AND ({stale_cond}) AND {not_close_pending} AND {jira_stale_gate} THEN 1 ELSE 0 END"
            )
        else:
            num_stale = f"CASE WHEN ({unpromoted_ips_cond}) AND ({stale_cond}) AND {not_close_pending} AND {jira_stale_gate} THEN 1 ELSE 0 END"
    else:
        num_stale = "0"

    hsd_id_expr = "NULL"
    if _has(columns, "hsd_id"):
        hsd_id_expr = "NULLIF(NULLIF(TRIM(hsd_id::text), ''), 'NA')"

    hsd_status_reason_expr = "NULL"
    if _has(columns, "hsd_status_reason"):
        hsd_status_reason_expr = "LOWER(TRIM(COALESCE(NULLIF(NULLIF(hsd_status_reason::text, ''), 'NA'), 'na')))"

    created_expr = _created_date_expr(columns)

    ips_order_expr = _ips_order_expr(columns)
    ips_status_rank = _ips_status_rank_expr(columns)
    jira_status_rank = _jira_status_rank_expr(columns)
    ips_status_expr = "ips_status" if _has(columns, "ips_status") else "NULL"
    ips_sub_status_expr = "ips_sub_status" if _has(columns, "ips_sub_status") else "NULL"
    jira_status_expr = "jira_status" if _has(columns, "jira_status") else "NULL"
    normalized_filter = "TRUE" if profile_is_dashboard else _normalized_filter_expr(ips_case_num_expr, jira_id_expr)
    query = f"""
        WITH base AS (
            SELECT
                {_reporter_expr(columns)} AS reporter,
                {ips_case_num_expr} AS ips_case_number,
                {jira_id_expr} AS jira_id,
                {hsd_id_expr} AS hsd_id,
                {hsd_status_reason_expr} AS hsd_status_reason,
                {created_expr} AS created_date,
                {ips_status_expr} AS ips_status,
                {ips_sub_status_expr} AS ips_sub_status,
                {jira_status_expr} AS jira_status,
                {num_unpromoted_ips} AS is_unpromoted_ips,
                {num_jira} AS is_jira,
                {num_unpromoted_hsd} AS is_unpromoted_hsd,
                {num_stale_hsd} AS is_stale_hsd,
                {num_promoted_ips} AS is_promoted_ips,
                {num_close_pending} AS is_close_pending,
                {num_stale} AS is_stale,
                {num_stale_promoted} AS is_stale_promoted,
                CASE
                    WHEN {ips_case_num_expr} IS NULL THEN 1
                    ELSE ROW_NUMBER() OVER (
                        PARTITION BY {ips_case_num_expr}
                        ORDER BY {ips_status_rank} DESC, {ips_order_expr} DESC NULLS LAST
                    )
                END AS ips_rn,
                CASE
                    WHEN {jira_id_expr} IS NULL THEN 1
                    ELSE ROW_NUMBER() OVER (
                        PARTITION BY {jira_id_expr}
                        ORDER BY {jira_status_rank} DESC, {ips_order_expr} DESC NULLS LAST
                    )
                END AS jira_rn
            FROM {table}
        ),
                normalized AS (
                        SELECT *
                        FROM base
                    WHERE {normalized_filter}
                            AND {base_filter}
                )
        SELECT
            reporter,
            COALESCE(COUNT(DISTINCT CASE WHEN is_unpromoted_hsd = 1 THEN hsd_id END), 0) AS num_unpromoted_hsd,
            COALESCE(COUNT(DISTINCT CASE WHEN is_stale_hsd = 1 THEN hsd_id END), 0) AS num_stale_hsd,
            CASE
                WHEN {str(profile_is_dashboard).upper()} THEN
                    GREATEST(
                        COALESCE(COUNT(DISTINCT CASE WHEN is_unpromoted_ips = 1 THEN ips_case_number END), 0)
                        - COALESCE(COUNT(DISTINCT CASE WHEN is_promoted_ips = 1 THEN ips_case_number END), 0),
                        0
                    )
                ELSE COALESCE(COUNT(DISTINCT CASE WHEN is_unpromoted_ips = 1 THEN ips_case_number END), 0)
            END AS num_unpromoted_ips,
            COALESCE(COUNT(DISTINCT CASE WHEN is_promoted_ips = 1 THEN ips_case_number END), 0) AS num_promoted_ips,
            COALESCE(COUNT(DISTINCT CASE WHEN is_jira = 1 THEN jira_id END), 0) AS num_jira,
            CASE
                WHEN {str(profile_is_dashboard).upper()} THEN
                    GREATEST(
                        COALESCE(COUNT(DISTINCT CASE WHEN is_stale = 1 THEN ips_case_number END), 0)
                        - COALESCE(COUNT(DISTINCT CASE WHEN is_stale_promoted = 1 THEN ips_case_number END), 0),
                        0
                    )
                ELSE COALESCE(COUNT(DISTINCT CASE WHEN is_stale = 1 THEN ips_case_number END), 0)
            END AS num_stale,
            COALESCE(COUNT(DISTINCT CASE WHEN is_close_pending = 1 THEN ips_case_number END), 0) AS num_close_pending,
            COALESCE(
                COUNT(DISTINCT CASE WHEN is_jira = 1 THEN jira_id END)
                + (
                    COALESCE(COUNT(DISTINCT CASE WHEN is_unpromoted_ips = 1 THEN ips_case_number END), 0)
                    - COALESCE(COUNT(DISTINCT CASE WHEN is_stale = 1 THEN ips_case_number END), 0)
                    + COALESCE(COUNT(DISTINCT CASE WHEN is_unpromoted_hsd = 1 THEN hsd_id END), 0)
                    - COALESCE(COUNT(DISTINCT CASE WHEN is_stale_hsd = 1 THEN hsd_id END), 0)
                ),
                0
            ) AS total_current_issue_count
        FROM normalized
        WHERE reporter IS NOT NULL
        {"AND LOWER(reporter) = ANY(" + allowed_reporters_array + ")" if allowed_reporters_array != "NULL" else ""}
        GROUP BY reporter
        ORDER BY total_current_issue_count DESC, reporter;
    """
    return db.query_rows(query, None)


def _debug_reporter_issue_lists(
    db: DbConnector, table: str, reporter: str
) -> None:
    if not reporter:
        return
    columns = _get_table_columns(db, table)

    base_filter = "TRUE"

    if _has(columns, "ips_case_number"):
        ips_case_num_expr = (
            "CASE WHEN ips_case_number::text ~ '^[0-9]+$' "
            "AND ips_case_number::int > 0 "
            "THEN ips_case_number::int ELSE NULL END"
        )
        ips_case_valid_cond = f"{ips_case_num_expr} > 0"
    else:
        ips_case_num_expr = "NULL"
        ips_case_valid_cond = "FALSE"

    ips_sub_status_expr = None
    if _has(columns, "ips_sub_status"):
        ips_sub_status_expr = "LOWER(TRIM(COALESCE(NULLIF(ips_sub_status::text, 'NA'), '')))"

    not_closed_cond = "TRUE"
    if ips_sub_status_expr:
        not_closed_cond = f"{ips_sub_status_expr} <> 'closed'"

    if _has(columns, "ips_jira_promo_status"):
        promo_status_expr = "UPPER(TRIM(ips_jira_promo_status))"
        unpromoted_ips_cond = (
            f"{ips_case_valid_cond} AND {promo_status_expr} IN ('NOT YET PROMOTED', 'FAILED') AND {not_closed_cond}"
        )
    elif _has(columns, "is_ips_promoted_to_jira"):
        unpromoted_ips_cond = f"{ips_case_valid_cond} AND NOT is_ips_promoted_to_jira AND {not_closed_cond}"
    elif _has(columns, "ips_jira_id"):
        unpromoted_ips_cond = f"{ips_case_valid_cond} AND ips_jira_id IS NULL AND {not_closed_cond}"
    else:
        unpromoted_ips_cond = f"{ips_case_valid_cond} AND {not_closed_cond}"

    if _has(columns, "jira_id"):
        jira_id_expr = "NULLIF(NULLIF(TRIM(jira_id::text), ''), 'NA')"
        jira_cond = f"{jira_id_expr} IS NOT NULL"
        if _has(columns, "ips_status"):
            ips_status_norm = "LOWER(TRIM(COALESCE(NULLIF(ips_status::text, 'NA'), '')))"
            jira_cond = f"({jira_cond}) AND ({ips_status_norm} <> 'closed')"
        if _has(columns, "ips_sub_status"):
            ips_sub_status_norm = "LOWER(TRIM(COALESCE(NULLIF(ips_sub_status::text, 'NA'), '')))"
            jira_cond = (
                f"({jira_cond}) AND ({ips_sub_status_norm} NOT IN ('close-pending', 'pending-closed'))"
            )
    else:
        jira_id_expr = "NULL"
        jira_cond = "FALSE"

    base_filter = _base_filter_expr(columns, jira_id_expr, "2025-01-01", include_hsd=False)

    hsd_status_reason_expr = "NULL"
    if _has(columns, "hsd_status_reason"):
        hsd_status_reason_expr = "LOWER(TRIM(COALESCE(NULLIF(NULLIF(hsd_status_reason::text, ''), 'NA'), 'na')))"

    reporter_list = _resolve_reporter_list(db, table, reporter)
    reporter_list_sql = ", ".join(f"LOWER({_sql_literal(r)})" for r in reporter_list)
    reporter_filter = f"LOWER(reporter) IN ({reporter_list_sql})"

    jira_stale_gate = "TRUE"
    if _has(columns, "jira_status"):
        jira_status_norm = "LOWER(TRIM(COALESCE(jira_status::text, '')))"
        jira_stale_gate = f"({jira_status_norm} IN ('', 'na', 'closed', 'verify', 'implemented'))"

    stale_conds = []
    if _has(columns, "ips_last_modified_days"):
        last_mod_days = (
            "CASE WHEN ips_last_modified_days::text ~ '^[0-9]+(\\.[0-9]+)?$' "
            "THEN ips_last_modified_days::numeric ELSE NULL END"
        )
    elif _has(columns, "ips_last_modified_date"):
        last_mod_days = "(CURRENT_DATE - ips_last_modified_date::date)"
    else:
        last_mod_days = None

    if _has(columns, "ips_open_days"):
        open_days = (
            "CASE WHEN ips_open_days::text ~ '^[0-9]+(\\.[0-9]+)?$' "
            "THEN ips_open_days::numeric ELSE NULL END"
        )
    elif _has(columns, "ips_created_date"):
        open_days = "(CURRENT_DATE - ips_created_date::date)"
    elif _has(columns, "bug_created_date"):
        open_days = "(CURRENT_DATE - bug_created_date::date)"
    else:
        open_days = None

    if last_mod_days:
        stale_conds.append(f"{last_mod_days} > 21")
    if open_days:
        stale_conds.append(f"{open_days} > 30")
    if stale_conds:
        stale_cond = " OR ".join(stale_conds)
        not_close_pending = "TRUE"
        if ips_sub_status_expr:
            not_close_pending = f"{ips_sub_status_expr} <> 'close-pending'"
        stale_cond = f"({unpromoted_ips_cond}) AND ({stale_cond}) AND {not_close_pending} AND {jira_stale_gate}"
    else:
        stale_cond = "FALSE"

    close_pending_conds = []
    if _has(columns, "ips_status"):
        close_pending_conds.append("UPPER(TRIM(ips_status)) = 'OPEN'")
    if _has(columns, "ips_sub_status"):
        close_pending_conds.append("UPPER(TRIM(ips_sub_status)) = 'CLOSE-PENDING'")
    if close_pending_conds:
        close_pending_cond = f"({unpromoted_ips_cond}) AND (" + " AND ".join(close_pending_conds) + ")"
    else:
        close_pending_cond = "FALSE"

    issue_key_parts = []
    for col in ("ips_case_number", "jira_id", "ips_jira_id", "hsd_id", "hsd_promoted_id"):
        if not _has(columns, col):
            continue
        if col == "ips_case_number":
            issue_key_parts.append(
                f"CASE WHEN {ips_case_num_expr} > 0 THEN {ips_case_num_expr}::text ELSE NULL END"
            )
        else:
            issue_key_parts.append(f"NULLIF(NULLIF(TRIM({col}::text), ''), 'NA')")
    issue_key_expr = "COALESCE(" + ", ".join(issue_key_parts) + ")" if issue_key_parts else "NULL"

    jira_id_expr = "NULL"
    if _has(columns, "jira_id"):
        jira_id_expr = "NULLIF(NULLIF(TRIM(jira_id::text), ''), 'NA')"
    ips_jira_id_expr = "NULL"
    if _has(columns, "ips_jira_id"):
        ips_jira_id_expr = "NULLIF(NULLIF(TRIM(ips_jira_id::text), ''), 'NA')"

    created_expr = _created_date_expr(columns)
    ips_order_expr = _ips_order_expr(columns)
    ips_status_rank = _ips_status_rank_expr(columns)
    jira_status_rank = _jira_status_rank_expr(columns)

    jira_stale_gate = "TRUE"
    if _has(columns, "jira_status"):
        jira_status_norm = "LOWER(TRIM(COALESCE(jira_status::text, '')))"
        jira_stale_gate = f"({jira_status_norm} IN ('', 'na', 'closed', 'verify', 'implemented'))"
    ips_status_expr = "ips_status" if _has(columns, "ips_status") else "NULL"
    ips_sub_status_expr = "ips_sub_status" if _has(columns, "ips_sub_status") else "NULL"
    jira_status_expr = "jira_status" if _has(columns, "jira_status") else "NULL"
    query = f"""
        WITH base AS (
            SELECT
                {_reporter_expr(columns)} AS reporter,
                {issue_key_expr} AS issue_key,
                {ips_case_num_expr} AS ips_case_number,
                {jira_id_expr} AS jira_id,
                {ips_jira_id_expr} AS ips_jira_id,
                {created_expr} AS created_date,
                {hsd_status_reason_expr} AS hsd_status_reason,
                {ips_status_expr} AS ips_status,
                {ips_sub_status_expr} AS ips_sub_status,
                {jira_status_expr} AS jira_status,
                {unpromoted_ips_cond} AS is_unpromoted_ips,
                {jira_cond} AS is_jira,
                {stale_cond} AS is_stale,
                {close_pending_cond} AS is_close_pending,
                CASE
                    WHEN {ips_case_num_expr} IS NULL THEN 1
                    ELSE ROW_NUMBER() OVER (
                        PARTITION BY {ips_case_num_expr}
                        ORDER BY {ips_status_rank} DESC, {ips_order_expr} DESC NULLS LAST
                    )
                END AS ips_rn,
                CASE
                    WHEN {jira_id_expr} IS NULL THEN 1
                    ELSE ROW_NUMBER() OVER (
                        PARTITION BY {jira_id_expr}
                        ORDER BY {jira_status_rank} DESC, {ips_order_expr} DESC NULLS LAST
                    )
                END AS jira_rn
            FROM {table}
        ),
                normalized AS (
                        SELECT *
                        FROM base
                        WHERE {_normalized_filter_expr(ips_case_num_expr, jira_id_expr)}
                            AND {base_filter}
                ),
        per_issue AS (
            SELECT
                reporter,
                issue_key,
                MAX(ips_case_number) AS ips_case_number,
                MAX(jira_id) AS jira_id,
                MAX(ips_jira_id) AS ips_jira_id,
                MAX(CASE WHEN is_unpromoted_ips THEN 1 ELSE 0 END) AS is_unpromoted_ips,
                MAX(CASE WHEN is_jira THEN 1 ELSE 0 END) AS is_jira,
                MAX(CASE WHEN is_stale THEN 1 ELSE 0 END) AS is_stale,
                MAX(CASE WHEN is_close_pending THEN 1 ELSE 0 END) AS is_close_pending
            FROM normalized
            WHERE {reporter_filter}
            GROUP BY reporter, issue_key
        )
        SELECT *
        FROM per_issue;
    """
    rows = db.query_rows(query, None)

    def _list(flag: str) -> List[str]:
        items = []
        for row in rows:
            if row.get(flag):
                if flag == "is_jira":
                    key = row.get("jira_id")
                elif flag in {"is_unpromoted_ips", "is_stale", "is_close_pending"}:
                    key = row.get("ips_case_number")
                else:
                    key = row.get("issue_key") or row.get("ips_case_number") or row.get("jira_id") or row.get("ips_jira_id")
                if key in (None, "", "0", 0, "NA"):
                    continue
                items.append(str(key))
        return sorted(set(items))

    LOG.info("Debug issue lists for %s:", reporter)
    LOG.info("- Unpromoted IPS case numbers: %s", ", ".join(_list("is_unpromoted_ips")) or "(none)")
    LOG.info("- Jira ids: %s", ", ".join(_list("is_jira")) or "(none)")
    LOG.info("- Stale IPS case numbers: %s", ", ".join(_list("is_stale")) or "(none)")
    LOG.info("- Close-Pending IPS case numbers: %s", ", ".join(_list("is_close_pending")) or "(none)")


def _debug_unpromoted_hsd_ids(db: DbConnector, table: str, reporter: str) -> None:
    if not reporter:
        return
    columns = _get_table_columns(db, table)
    if not (_has(columns, "hsd_id") and _has(columns, "hsd_promoted_id") and _has(columns, "hsd_owner")):
        LOG.info("Unpromoted HSD debug skipped (missing hsd_id/hsd_promoted_id/hsd_owner)")
        return

    owner_expr = "LOWER(TRIM(COALESCE(hsd_owner::text, '')))"
    reporter_expr = owner_expr
    if _has(columns, "reporter"):
        reporter_expr = _reporter_norm_expr(columns)

    reporter_list = _resolve_reporter_list(db, table, reporter)
    reporter_list_sql = ", ".join(f"LOWER({_sql_literal(r)})" for r in reporter_list)
    reporter_match = f"({owner_expr} IN ({reporter_list_sql}) OR {reporter_expr} IN ({reporter_list_sql}))"

    created_filter = _created_date_filter_expr(columns, "2025-01-01")
    base_filter = "TRUE"
    if created_filter:
        base_filter = f"({base_filter}) AND ({created_filter})"
    base_filter = f"({base_filter}) AND ({_bug_status_custom_open_expr(columns)})"

    query = f"""
        SELECT DISTINCT hsd_id
        FROM {table}
        WHERE {reporter_match}
          AND {base_filter}
          AND UPPER(TRIM(COALESCE(hsd_id::text, ''))) NOT IN ('', 'NA')
          AND UPPER(TRIM(COALESCE(hsd_promoted_id::text, ''))) IN ('', 'NA')
        ORDER BY hsd_id;
    """
    rows = db.query_rows(query, None)
    if not rows:
        LOG.info("Unpromoted HSD ids for reporter=%s: (none)", reporter)
        return
    hsd_ids = [str(row.get("hsd_id")) for row in rows if row.get("hsd_id") not in (None, "", "NA")]
    LOG.info("Unpromoted HSD ids for reporter=%s: %s", reporter, ", ".join(sorted(set(hsd_ids))))


def _debug_case_details(db: DbConnector, table: str, case_number: str, *, include_raw: bool = False) -> None:
    if not case_number:
        return
    columns = _get_table_columns(db, table)
    if not _has(columns, "ips_case_number"):
        LOG.warning("Debug case requested, but ips_case_number column not found in %s", table)
        return
    jira_id_expr = "NULL"
    if _has(columns, "jira_id"):
        jira_id_expr = "NULLIF(NULLIF(TRIM(jira_id::text), ''), 'NA')"

    base_filter = _base_filter_expr(columns, jira_id_expr, "2025-01-01")

    if _has(columns, "ips_case_number"):
        ips_case_num_expr = (
            "CASE WHEN ips_case_number::text ~ '^[0-9]+$' "
            "AND ips_case_number::int > 0 "
            "THEN ips_case_number::int ELSE NULL END"
        )
        ips_case_valid_cond = f"{ips_case_num_expr} > 0"
    else:
        ips_case_num_expr = "NULL"
        ips_case_valid_cond = "FALSE"

    ips_sub_status_expr = None
    if _has(columns, "ips_sub_status"):
        ips_sub_status_expr = "LOWER(TRIM(COALESCE(NULLIF(ips_sub_status::text, 'NA'), ''))" + ")"  # normalize sub-status (3)

    not_closed_cond = "TRUE"
    if ips_sub_status_expr:
        not_closed_cond = f"{ips_sub_status_expr} <> 'closed'"

    if _has(columns, "ips_jira_promo_status"):
        promo_status_expr = "UPPER(TRIM(ips_jira_promo_status))"
        unpromoted_ips_cond = (
            f"{ips_case_valid_cond} AND {promo_status_expr} IN ('NOT YET PROMOTED', 'FAILED') AND {not_closed_cond}"
        )
        promoted_ips_cond = f"{ips_case_valid_cond} AND {promo_status_expr} IN ('PROMOTED', 'DONE')"
    elif _has(columns, "is_ips_promoted_to_jira"):
        unpromoted_ips_cond = f"{ips_case_valid_cond} AND NOT is_ips_promoted_to_jira AND {not_closed_cond}"
        promoted_ips_cond = f"{ips_case_valid_cond} AND is_ips_promoted_to_jira"
    elif _has(columns, "ips_jira_id"):
        unpromoted_ips_cond = f"{ips_case_valid_cond} AND ips_jira_id IS NULL AND {not_closed_cond}"
        promoted_ips_cond = f"{ips_case_valid_cond} AND ips_jira_id IS NOT NULL"
    else:
        unpromoted_ips_cond = f"{ips_case_valid_cond} AND {not_closed_cond}"
        promoted_ips_cond = "FALSE"

    jira_cond = f"{jira_id_expr} IS NOT NULL"
    if _has(columns, "ips_status"):
        ips_status_norm = "LOWER(TRIM(COALESCE(NULLIF(ips_status::text, 'NA'), '')))"
        jira_cond = f"({jira_cond}) AND ({ips_status_norm} <> 'closed')"
    if _has(columns, "ips_sub_status"):
        ips_sub_status_norm = "LOWER(TRIM(COALESCE(NULLIF(ips_sub_status::text, 'NA'), '')))"
        jira_cond = (
            f"({jira_cond}) AND ({ips_sub_status_norm} NOT IN ('close-pending', 'pending-closed'))"
        )

    created_expr = _created_date_expr(columns)
    ips_order_expr = _ips_order_expr(columns)
    ips_status_rank = _ips_status_rank_expr(columns)
    jira_status_rank = _jira_status_rank_expr(columns)

    jira_stale_gate = "TRUE"
    if _has(columns, "jira_status"):
        jira_status_norm = "LOWER(TRIM(COALESCE(jira_status::text, '')))"
        jira_stale_gate = f"({jira_status_norm} IN ('', 'na', 'closed', 'verify', 'implemented'))"

    stale_conds = []
    if _has(columns, "ips_last_modified_days"):
        last_mod_days = (
            "CASE WHEN ips_last_modified_days::text ~ '^[0-9]+(\\.[0-9]+)?$' "
            "THEN ips_last_modified_days::numeric ELSE NULL END"
        )
    elif _has(columns, "ips_last_modified_date"):
        last_mod_days = "(CURRENT_DATE - ips_last_modified_date::date)"
    else:
        last_mod_days = None

    if _has(columns, "ips_open_days"):
        open_days = (
            "CASE WHEN ips_open_days::text ~ '^[0-9]+(\\.[0-9]+)?$' "
            "THEN ips_open_days::numeric ELSE NULL END"
        )
    elif _has(columns, "ips_created_date"):
        open_days = "(CURRENT_DATE - ips_created_date::date)"
    elif _has(columns, "bug_created_date"):
        open_days = "(CURRENT_DATE - bug_created_date::date)"
    else:
        open_days = None

    if last_mod_days:
        stale_conds.append(f"{last_mod_days} > 21")
    if open_days:
        stale_conds.append(f"{open_days} > 30")
    if stale_conds:
        not_close_pending = "TRUE"
        if ips_sub_status_expr:
            not_close_pending = f"{ips_sub_status_expr} <> 'close-pending'"
        stale_cond = f"({unpromoted_ips_cond}) AND (" + " OR ".join(stale_conds) + f") AND {not_close_pending} AND {jira_stale_gate}"
    else:
        stale_cond = "FALSE"

    if ips_sub_status_expr:
        close_pending_cond = f"{ips_sub_status_expr} = 'close-pending'"
        if _has(columns, "ips_jira_promo_status"):
            close_pending_cond = (
                f"{ips_case_valid_cond} AND {close_pending_cond} AND "
                "UPPER(TRIM(ips_jira_promo_status)) IN ('NOT YET PROMOTED', 'FAILED')"
            )
        else:
            close_pending_cond = f"{ips_case_valid_cond} AND {close_pending_cond}"
    else:
        close_pending_cond = "FALSE"

    case_literal = _sql_literal(str(case_number))
    query = f"""
        WITH base AS (
            SELECT *,
                   {created_expr} AS created_date,
                   CASE
                       WHEN {ips_case_num_expr} IS NULL THEN 1
                       ELSE ROW_NUMBER() OVER (
                           PARTITION BY {ips_case_num_expr}
                           ORDER BY {ips_status_rank} DESC, {ips_order_expr} DESC NULLS LAST
                       )
                   END AS ips_rn,
                   CASE
                       WHEN {jira_id_expr} IS NULL THEN 1
                       ELSE ROW_NUMBER() OVER (
                           PARTITION BY {jira_id_expr}
                           ORDER BY {jira_status_rank} DESC, {ips_order_expr} DESC NULLS LAST
                       )
                   END AS jira_rn
            FROM {table}
            WHERE ips_case_number::text = {case_literal}
        ),
         normalized AS (
             SELECT *,
                 {base_filter} AS base_filter_ok,
                 {ips_case_valid_cond} AS ips_case_valid,
                 {unpromoted_ips_cond} AS is_unpromoted_ips,
                 {promoted_ips_cond} AS is_promoted_ips,
                 {jira_cond} AS is_jira,
                 {stale_cond} AS is_stale,
                 {close_pending_cond} AS is_close_pending
             FROM base
             WHERE {_normalized_filter_expr(ips_case_num_expr, jira_id_expr)}
            AND {base_filter}
         )
        SELECT * FROM normalized
        ORDER BY ips_case_number::text;
    """
    rows = db.query_rows(query, None)
    if not rows:
        LOG.info("No rows found for ips_case_number=%s after normalization", case_number)
        if not include_raw:
            return
    LOG.info("Debug rows for ips_case_number=%s (after normalization: %d rows):", case_number, len(rows))
    for idx, row in enumerate(rows, start=1):
        LOG.info("- Row %d:", idx)
        for key in sorted(row.keys()):
            LOG.info("  %s = %s", key, row.get(key))

    if not include_raw:
        return

    raw_query = f"""
        WITH raw_base AS (
            SELECT *,
                   {created_expr} AS created_date,
                   CASE
                       WHEN {ips_case_num_expr} IS NULL THEN 1
                       ELSE ROW_NUMBER() OVER (
                           PARTITION BY {ips_case_num_expr}
                           ORDER BY {ips_status_rank} DESC, {ips_order_expr} DESC NULLS LAST
                       )
                   END AS ips_rn
            FROM {table}
            WHERE ips_case_number::text = {case_literal}
        )
        SELECT *
        FROM raw_base
        WHERE ips_rn = 1
        ORDER BY ips_case_number::text;
    """
    raw_rows = db.query_rows(raw_query, None)
    if not raw_rows:
        LOG.info("No raw rows found for ips_case_number=%s", case_number)
        return
    LOG.info("Raw row for ips_case_number=%s (collapsed to 1 row):", case_number)
    for key in sorted(raw_rows[0].keys()):
        LOG.info("  %s = %s", key, raw_rows[0].get(key))


def _debug_jira_details(db: DbConnector, table: str, jira_id: str, *, include_raw: bool = False) -> None:
    if not jira_id:
        return
    columns = _get_table_columns(db, table)
    if not _has(columns, "jira_id"):
        LOG.warning("Debug jira requested, but jira_id column not found in %s", table)
        return

    base_filter = _base_filter_expr(columns, "NULLIF(NULLIF(TRIM(jira_id::text), ''), 'NA')", "2025-01-01")

    if _has(columns, "ips_case_number"):
        ips_case_num_expr = (
            "CASE WHEN ips_case_number::text ~ '^[0-9]+$' "
            "AND ips_case_number::int > 0 "
            "THEN ips_case_number::int ELSE NULL END"
        )
    else:
        ips_case_num_expr = "NULL"

    jira_id_expr = "NULLIF(NULLIF(TRIM(jira_id::text), ''), 'NA')"
    created_expr = _created_date_expr(columns)
    ips_order_expr = _ips_order_expr(columns)
    ips_status_rank = _ips_status_rank_expr(columns)
    jira_status_rank = _jira_status_rank_expr(columns)

    jira_literal = _sql_literal(str(jira_id))
    query = f"""
        WITH base AS (
            SELECT *,
                   {created_expr} AS created_date,
                   CASE
                       WHEN {ips_case_num_expr} IS NULL THEN 1
                       ELSE ROW_NUMBER() OVER (
                           PARTITION BY {ips_case_num_expr}
                           ORDER BY {ips_status_rank} DESC, {ips_order_expr} DESC NULLS LAST
                       )
                   END AS ips_rn,
                   CASE
                       WHEN {jira_id_expr} IS NULL THEN 1
                       ELSE ROW_NUMBER() OVER (
                           PARTITION BY {jira_id_expr}
                           ORDER BY {jira_status_rank} DESC, {ips_order_expr} DESC NULLS LAST
                       )
                   END AS jira_rn
            FROM {table}
            WHERE {jira_id_expr} = {jira_literal}
        ),
         normalized AS (
             SELECT *,
                 {base_filter} AS base_filter_ok
             FROM base
             WHERE {_normalized_filter_expr(ips_case_num_expr, jira_id_expr)}
            AND {base_filter}
         )
        SELECT * FROM normalized
        ORDER BY jira_id::text;
    """
    rows = db.query_rows(query, None)
    if not rows:
        LOG.info("No rows found for jira_id=%s after normalization", jira_id)
        if not include_raw:
            return
    LOG.info("Debug rows for jira_id=%s (after normalization: %d rows):", jira_id, len(rows))
    for idx, row in enumerate(rows, start=1):
        LOG.info("- Row %d:", idx)
        for key in sorted(row.keys()):
            LOG.info("  %s = %s", key, row.get(key))

    if not include_raw:
        return

    raw_query = f"""
        WITH raw_base AS (
            SELECT *,
                   CASE
                       WHEN {jira_id_expr} IS NULL THEN 1
                       ELSE ROW_NUMBER() OVER (
                           PARTITION BY {jira_id_expr}
                           ORDER BY {jira_status_rank} DESC, {ips_order_expr} DESC NULLS LAST
                       )
                   END AS jira_rn
            FROM {table}
            WHERE {jira_id_expr} = {jira_literal}
        )
        SELECT *
        FROM raw_base
        WHERE jira_rn = 1
        ORDER BY jira_id::text;
    """
    raw_rows = db.query_rows(raw_query, None)
    if not raw_rows:
        LOG.info("No raw rows found for jira_id=%s", jira_id)
        return
    LOG.info("Raw row for jira_id=%s (collapsed to 1 row):", jira_id)
    for idx, row in enumerate(raw_rows, start=1):
        LOG.info("- Raw Row %d:", idx)
        for key in sorted(row.keys()):
            LOG.info("  %s = %s", key, row.get(key))


def _get_most_recent_overloaded_issue(
    db: DbConnector,
    table: str,
    reporters: Sequence[str],
    *,
    exclude_case_numbers: Optional[Sequence[str]] = None,
) -> Optional[Dict[str, Any]]:
    if not reporters:
        return None
    columns = _get_table_columns(db, table)
    allowed_reporters_array = _allowed_reporters_array_sql()
    reporters_array = _sql_array_literal([str(r) for r in reporters if r], lower=True)
    excluded_cases_array = _sql_array_literal(
        [str(case).strip() for case in (exclude_case_numbers or []) if str(case).strip()]
    )
    excluded_cases_filter = (
        f"AND (issue_id IS NULL OR issue_id <> ALL({excluded_cases_array}))"
        if excluded_cases_array != "ARRAY[]::text[]"
        else ""
    )

    base_filter = "TRUE"
    created_filter = _created_date_filter_expr(columns, "2025-01-01")
    if created_filter:
        base_filter = f"({base_filter}) AND ({_created_date_filter_alias_expr('2025-01-01')})"
    hsd_open_cond: Optional[str] = None
    if _has(columns, "hsd_status_reason"):
        hsd_open_cond = _status_open_condition(
            "hsd_status_reason",
            ["closed", "complete", "implemented", "rejected"],
        )
    if _has(columns, "ips_status"):
        ips_open_cond = _status_open_condition("ips_status", ["closed", "invalid"])
        if hsd_open_cond:
            base_filter = f"({base_filter}) AND (({ips_open_cond}) OR ({hsd_open_cond}))"
        else:
            base_filter = f"({base_filter}) AND ({ips_open_cond})"
    if _has(columns, "ips_sub_status"):
        ips_sub_open_cond = _status_open_condition("ips_sub_status", ["closed", "invalid"])
        if hsd_open_cond:
            base_filter = f"({base_filter}) AND (({ips_sub_open_cond}) OR ({hsd_open_cond}))"
        else:
            base_filter = f"({base_filter}) AND ({ips_sub_open_cond})"
        ips_sub_status_norm = "LOWER(REPLACE(TRIM(COALESCE(NULLIF(ips_sub_status::text, 'NA'), '')), ' ', '-'))"
        base_filter = (
            f"({base_filter}) AND "
            f"({ips_sub_status_norm} NOT IN ('close-pending', 'pending-closed', 'pending-close'))"
        )
    if _has(columns, "jira_status"):
        jira_open_cond = _status_open_condition("jira_status", ["closed", "implemented", "verify"])
        if hsd_open_cond:
            base_filter = f"({base_filter}) AND (({jira_open_cond}) OR ({hsd_open_cond}))"
        else:
            base_filter = f"({base_filter}) AND ({jira_open_cond})"

    # Always exclude the DB health sentinel row
    # In this function's CTE, ips_title is aliased as "title" — use that alias.
    _title_col = "ips_title" if _has(columns, "ips_title") else ("jira_title" if _has(columns, "jira_title") else None)
    if _title_col:
        base_filter = f"({base_filter}) AND (COALESCE(TRIM(title::text), '') NOT LIKE '[DB HEALTH]%')"

    if _has(columns, "bug_created_date"):
        created_col = "bug_created_date"
    elif _has(columns, "ips_created_date"):
        created_col = "ips_created_date"
    else:
        created_col = "NULL"
    created_expr = _created_date_expr(columns)
    ips_order_expr = _ips_order_expr(columns)
    ips_status_rank = _ips_status_rank_expr(columns)
    jira_status_rank = _jira_status_rank_expr(columns)
    ips_status_expr = "ips_status" if _has(columns, "ips_status") else "NULL"
    ips_sub_status_expr = "ips_sub_status" if _has(columns, "ips_sub_status") else "NULL"
    jira_status_expr = "jira_status" if _has(columns, "jira_status") else "NULL"
    hsd_status_reason_expr = "hsd_status_reason" if _has(columns, "hsd_status_reason") else "NULL"
    hsd_id_expr = "NULL"
    if _has(columns, "hsd_id"):
        hsd_id_expr = "NULLIF(NULLIF(TRIM(hsd_id::text), ''), 'NA')"
    source_issue_expr = f"COALESCE(NULLIF({_issue_case_expr()}, '0'), {hsd_id_expr})"
    source_issue_filter = f"LOWER(reporter) = ANY({reporters_array})"
    # Derive technology for WiFi/BT routing of recommended issue
    if _has(columns, "technology"):
        technology_select = "LOWER(TRIM(COALESCE(technology::text, ''))) AS technology"
    elif _has(columns, "bug_project"):
        technology_select = (
            "CASE "
            "WHEN LOWER(TRIM(COALESCE(bug_project::text, ''))) = 'wifi' THEN 'wifi' "
            "WHEN LOWER(TRIM(COALESCE(bug_project::text, ''))) = 'bt' THEN 'bt' "
            "ELSE '' END AS technology"
        )
    else:
        technology_select = "'' AS technology"
    query = f"""
        WITH base AS (
            SELECT
                {_reporter_expr(columns)} AS reporter,
                {source_issue_expr} AS issue_id,
                {_title_expr(columns)} AS title,
                {created_col} AS created_date,
                {created_expr} AS created_date_filter,
                {ips_status_expr} AS ips_status,
                {ips_sub_status_expr} AS ips_sub_status,
                {jira_status_expr} AS jira_status,
                {hsd_status_reason_expr} AS hsd_status_reason,
                {technology_select},
                CASE
                    WHEN {source_issue_expr} IS NULL THEN 1
                    ELSE ROW_NUMBER() OVER (
                        PARTITION BY {source_issue_expr}
                        ORDER BY {ips_status_rank} DESC, {ips_order_expr} DESC NULLS LAST
                    )
                END AS ips_rn
            FROM {table}
        ),
        normalized AS (
            SELECT *
            FROM base
            WHERE (issue_id IS NULL OR ips_rn = 1)
              AND {base_filter}
        )
        SELECT reporter, issue_id AS ips_case_number, title, created_date, technology
        FROM normalized
        WHERE {source_issue_filter}
        {"AND LOWER(reporter) = ANY(" + allowed_reporters_array + ")" if allowed_reporters_array != "NULL" else ""}
        {excluded_cases_filter}
        ORDER BY created_date DESC NULLS LAST, ips_case_number DESC NULLS LAST
        LIMIT 1;
    """
    rows = db.query_rows(query, None)
    return rows[0] if rows else None


def _format_counts(counts: List[Dict[str, Any]], limit: int = 10) -> str:
    lines = []
    for row in counts[:limit]:
        effective = row.get("effective_current_issue_count")
        if effective is None:
            effective = row.get("total_current_issue_count")
        lines.append(f"- {row.get('reporter')}: {float(effective or 0.0):.2f} current issues")
    if len(counts) > limit:
        lines.append(f"... and {len(counts) - limit} more")
    return "\n".join(lines)


def _attach_effective_counts(
    counts: Sequence[Dict[str, Any]],
    weighted_summary: Dict[str, Dict[str, float]],
    *,
    use_weighted_for_decision: bool = False,
) -> List[Dict[str, Any]]:
    output: List[Dict[str, Any]] = []
    for row in counts:
        copied = dict(row)
        reporter_norm = _normalize_reporter(str(copied.get("reporter") or ""))
        raw_current = float(copied.get("total_current_issue_count") or 0.0)
        weighted_current = None
        if reporter_norm in weighted_summary:
            weighted_current = float(weighted_summary[reporter_norm].get("weighted_current_issue_count") or 0.0)

        # Trial mode: always expose weighted loading for visibility, but keep decision on raw current count.
        copied["weighted_trial_current_issue_count"] = weighted_current
        if use_weighted_for_decision and weighted_current is not None:
            copied["effective_current_issue_count"] = weighted_current
        else:
            copied["effective_current_issue_count"] = raw_current
        output.append(copied)
    return output


def _format_breakdown_table(rows: List[Dict[str, str]], *, include_weighted: bool = False) -> List[str]:
    normalized_rows: List[Dict[str, str]] = []
    for row in rows:
        copied = dict(row)
        try:
            active_ips = max(
                0,
                int(row.get("unpromoted_ips") or 0)
                - int(row.get("stale") or 0)
                - int(row.get("close_pending") or 0),
            )
        except (TypeError, ValueError):
            active_ips = 0
        copied["active_ips"] = str(active_ips)
        normalized_rows.append(copied)

    headers = [
        ("Reporter", "reporter", "left"),
        ("uHSD", "unpromoted_hsd", "right"),
        ("sHSD", "stale_hsd", "right"),
        ("uIPS", "unpromoted_ips", "right"),
        ("Active IPS", "active_ips", "right"),
        ("pIPS", "promoted_ips", "right"),
        ("Jira", "jira", "right"),
        ("Stale", "stale", "right"),
        ("Curr", "current", "right"),
    ]
    if include_weighted:
        headers.extend([
            ("wCurr", "weighted_current", "right"),
        ])

    widths = [
        max(len(title), max((len(str(r.get(key, ""))) for r in normalized_rows), default=0))
        for title, key, _ in headers
    ]

    def _cell(text: str, width: int, align: str) -> str:
        s = str(text)
        if align == "right":
            return s.rjust(width)
        return s.ljust(width)

    border = "+-" + "-+-".join("-" * w for w in widths) + "-+"
    header_line = "| " + " | ".join(
        _cell(title, width, "left") for (title, _, _), width in zip(headers, widths)
    ) + " |"

    lines = [border, header_line, border]
    for row in normalized_rows:
        line = "| " + " | ".join(
            _cell(row.get(key, ""), width, align)
            for (_, key, align), width in zip(headers, widths)
        ) + " |"
        lines.append(line)
    lines.append(border)
    return lines


def _load_weight_category_display_order(weight_map_path: str) -> List[str]:
    try:
        if not weight_map_path or not os.path.exists(weight_map_path):
            return []
        with open(weight_map_path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        if not isinstance(payload, dict):
            return []

        category_order: List[str] = []
        category_weights = payload.get("category_weights") if isinstance(payload.get("category_weights"), dict) else {}
        for key in category_weights.keys():
            label = str(key or "").strip()
            if label and label not in category_order:
                category_order.append(label)

        category_tech = payload.get("category_technology_weights") if isinstance(payload.get("category_technology_weights"), dict) else {}
        for raw_key in category_tech.keys():
            key_text = str(raw_key or "").strip()
            if not key_text:
                continue
            if "|" in key_text:
                cat_text, _ = key_text.split("|", 1)
            elif "@" in key_text:
                cat_text, _ = key_text.split("@", 1)
            else:
                cat_text = key_text
            label = str(cat_text or "").strip()
            if label and label not in category_order:
                category_order.append(label)

        return category_order
    except Exception as exc:
        LOG.warning("Failed to parse category order from weight map %s: %s", weight_map_path, exc)
        return []


def _build_combined_trial_table_data(
    breakdown_rows: List[Dict[str, Any]],
    weighted_summary: Dict[str, Dict[str, float]],
    weighted_category_rows: List[Dict[str, Any]],
    preferred_categories: Optional[List[str]] = None,
) -> Tuple[List[str], List[Dict[str, Any]]]:
    category_by_reporter: Dict[str, Dict[str, int]] = {}
    technology_by_reporter: Dict[str, Dict[str, int]] = {}
    all_categories: set[str] = set()
    preferred = [str(c or "").strip() for c in (preferred_categories or []) if str(c or "").strip()]
    preferred_norm_to_label = {str(c).strip().lower(): str(c).strip() for c in preferred}
    unmapped_label = "Unmapped(Default)"
    has_unmapped = False

    for row in weighted_category_rows:
        reporter_norm = _normalize_reporter(str(row.get("reporter") or ""))
        if not reporter_norm:
            continue
        raw_category = str(row.get("category") or "Uncategorized").strip() or "Uncategorized"
        category = raw_category
        if preferred_norm_to_label:
            category = preferred_norm_to_label.get(raw_category.lower(), unmapped_label)
            if category == unmapped_label:
                has_unmapped = True
        issue_count = int(row.get("issue_count") or 0)
        all_categories.add(category)
        reporter_map = category_by_reporter.setdefault(reporter_norm, {})
        reporter_map[category] = int(reporter_map.get(category, 0)) + issue_count
        technology_key = f"{category}|{str(row.get('technology') or 'Unknown').strip() or 'Unknown'}"
        technology_map = technology_by_reporter.setdefault(reporter_norm, {})
        technology_map[technology_key] = int(technology_map.get(technology_key, 0)) + issue_count

    if preferred:
        categories = [c for c in preferred if c in all_categories or c]
        if has_unmapped:
            categories.append(unmapped_label)
    else:
        categories = sorted(all_categories)
    combined_rows: List[Dict[str, Any]] = []
    for row in breakdown_rows:
        reporter_name = str(row.get("reporter") or "")
        reporter_norm = _normalize_reporter(reporter_name)
        category_counts = category_by_reporter.get(reporter_norm, {})
        technology_counts = technology_by_reporter.get(reporter_norm, {})
        raw_uips = int(row.get("num_unpromoted_ips") or 0)
        stale_ips = int(row.get("num_stale") or 0)
        close_pending = int(row.get("num_close_pending") or 0)
        raw_uhsd = int(row.get("num_unpromoted_hsd") or 0)
        stale_hsd = int(row.get("num_stale_hsd") or 0)
        combined_rows.append(
            {
                "name": reporter_name,
                "uips": raw_uips,
                "active_uips": max(0, raw_uips - stale_ips - close_pending),
            "uhsd": raw_uhsd,
            "active_hsd": max(0, raw_uhsd - stale_hsd),
                "jira": int(row.get("num_jira") or 0),
                "stale_ips": stale_ips,
            "stale_hsd": stale_hsd,
                "total": int(row.get("total_current_issue_count") or 0),
                "weighted_loading": (
                    f"{float(weighted_summary.get(reporter_norm, {}).get('weighted_current_issue_count', 0.0)):.2f}"
                    if reporter_norm in weighted_summary
                    else ""
                ),
                "weighted_loading_value": float(
                    weighted_summary.get(reporter_norm, {}).get("weighted_current_issue_count", 0.0)
                ),
                "category_counts": category_counts,
                "technology_counts": technology_counts,
            }
        )

    combined_rows.sort(
        key=lambda row: (
            -float(row.get("weighted_loading_value") or 0.0),
            str(row.get("name") or "").lower(),
        )
    )
    return categories, combined_rows


def _format_combined_trial_table(category_names: List[str], rows: List[Dict[str, Any]]) -> List[str]:
    headers: List[Tuple[str, str, str]] = [
        ("Name", "name", "left"),
        ("uIPS", "uips", "right"),
        ("Active IPS", "active_uips", "right"),
        ("uHSD", "uhsd", "right"),
        ("Active HSD", "active_hsd", "right"),
        ("Jira", "jira", "right"),
        ("Stale IPS", "stale_ips", "right"),
        ("Stale HSD", "stale_hsd", "right"),
        ("Total", "total", "right"),
        ("Weighted Loading", "weighted_loading", "right"),
    ]
    headers.extend([(category, f"cat::{category}", "right") for category in category_names])

    normalized_rows: List[Dict[str, Any]] = []
    for row in rows:
        copied = dict(row)
        category_counts = row.get("category_counts") or {}
        for category in category_names:
            copied[f"cat::{category}"] = str(int(category_counts.get(category, 0)))
        normalized_rows.append(copied)

    widths = [
        max(len(title), max((len(str(r.get(key, ""))) for r in normalized_rows), default=0))
        for title, key, _ in headers
    ]

    def _cell(text: str, width: int, align: str) -> str:
        s = str(text)
        return s.rjust(width) if align == "right" else s.ljust(width)

    border = "+-" + "-+-".join("-" * w for w in widths) + "-+"
    header_line = "| " + " | ".join(
        _cell(title, width, "left") for (title, _, _), width in zip(headers, widths)
    ) + " |"

    lines = [border, header_line, border]
    for row in normalized_rows:
        line = "| " + " | ".join(
            _cell(row.get(key, ""), width, align)
            for (_, key, align), width in zip(headers, widths)
        ) + " |"
        lines.append(line)
    lines.append(border)
    return lines


def _get_unpromoted_ips_rows_for_weighting(
    db: DbConnector,
    table: str,
    *,
    filter_allowed: bool = True,
    include_jira: bool = False,
    count_profile: str = "offload",
) -> List[Dict[str, Any]]:
    columns = _get_table_columns(db, table)
    allowed_reporters_array = _allowed_reporters_array_sql() if filter_allowed else "NULL"

    if _has(columns, "ips_case_number"):
        ips_case_num_expr = (
            "CASE WHEN ips_case_number::text ~ '^[0-9]+$' "
            "AND ips_case_number::int > 0 "
            "THEN ips_case_number::int ELSE NULL END"
        )
        ips_case_valid_cond = f"{ips_case_num_expr} > 0"
    else:
        return []

    ips_sub_status_expr = None
    if _has(columns, "ips_sub_status"):
        ips_sub_status_expr = "LOWER(TRIM(COALESCE(NULLIF(ips_sub_status::text, 'NA'), '')))"

    not_closed_cond = "TRUE"
    if ips_sub_status_expr:
        not_closed_cond = f"{ips_sub_status_expr} <> 'closed'"

    if _has(columns, "ips_jira_promo_status"):
        promo_status_expr = "UPPER(TRIM(ips_jira_promo_status))"
        unpromoted_ips_cond = (
            f"{ips_case_valid_cond} AND {promo_status_expr} IN ('NOT YET PROMOTED', 'FAILED') AND {not_closed_cond}"
        )
    elif _has(columns, "is_ips_promoted_to_jira"):
        unpromoted_ips_cond = f"{ips_case_valid_cond} AND NOT is_ips_promoted_to_jira AND {not_closed_cond}"
    elif _has(columns, "ips_jira_id"):
        unpromoted_ips_cond = f"{ips_case_valid_cond} AND ips_jira_id IS NULL AND {not_closed_cond}"
    else:
        unpromoted_ips_cond = f"{ips_case_valid_cond} AND {not_closed_cond}"

    if ips_sub_status_expr:
        close_pending_cond = f"{ips_sub_status_expr} = 'close-pending'"
        if _has(columns, "ips_jira_promo_status"):
            close_pending_cond = (
                f"{ips_case_valid_cond} AND {close_pending_cond} "
                "AND UPPER(TRIM(ips_jira_promo_status)) IN ('NOT YET PROMOTED', 'FAILED')"
            )
        else:
            close_pending_cond = f"{ips_case_valid_cond} AND {close_pending_cond}"
    else:
        close_pending_cond = "FALSE"

    jira_stale_gate = "TRUE"
    if _has(columns, "jira_status"):
        jira_status_norm = "LOWER(TRIM(COALESCE(jira_status::text, '')))"
        jira_stale_gate = f"({jira_status_norm} IN ('', 'na', 'closed', 'verify', 'implemented'))"

    stale_conds = []
    if _has(columns, "ips_last_modified_days"):
        last_mod_days = (
            "CASE WHEN ips_last_modified_days::text ~ '^[0-9]+(\\.[0-9]+)?$' "
            "THEN ips_last_modified_days::numeric ELSE NULL END"
        )
    elif _has(columns, "ips_last_modified_date"):
        last_mod_days = "(CURRENT_DATE - ips_last_modified_date::date)"
    else:
        last_mod_days = None

    if _has(columns, "ips_open_days"):
        open_days = (
            "CASE WHEN ips_open_days::text ~ '^[0-9]+(\\.[0-9]+)?$' "
            "THEN ips_open_days::numeric ELSE NULL END"
        )
    elif _has(columns, "ips_created_date"):
        open_days = "(CURRENT_DATE - ips_created_date::date)"
    elif _has(columns, "bug_created_date"):
        open_days = "(CURRENT_DATE - bug_created_date::date)"
    else:
        open_days = None

    if last_mod_days:
        stale_conds.append(f"{last_mod_days} > 21")
    if open_days:
        stale_conds.append(f"{open_days} > 30")
    if stale_conds:
        not_close_pending = "TRUE"
        if ips_sub_status_expr:
            not_close_pending = f"{ips_sub_status_expr} <> 'close-pending'"
        stale_cond = f"({unpromoted_ips_cond}) AND ({' OR '.join(stale_conds)}) AND {not_close_pending} AND {jira_stale_gate}"
    else:
        stale_cond = "FALSE"

    jira_id_expr = _jira_key_expr(columns)

    created_expr = _created_date_expr(columns)
    ips_order_expr = _ips_order_expr(columns)
    ips_status_rank = _ips_status_rank_expr(columns)
    jira_status_rank = _jira_status_rank_expr(columns)
    ips_status_expr = "ips_status" if _has(columns, "ips_status") else "NULL"
    ips_sub_status_raw_expr = "ips_sub_status" if _has(columns, "ips_sub_status") else "NULL"
    jira_status_expr = "jira_status" if _has(columns, "jira_status") else "NULL"
    hsd_status_reason_expr = "hsd_status_reason" if _has(columns, "hsd_status_reason") else "NULL"
    hsd_id_expr = "hsd_id" if _has(columns, "hsd_id") else "NULL"
    if _has(columns, "technology"):
        technology_expr = "technology"
    elif _has(columns, "bug_project"):
        technology_expr = (
            "CASE "
            "WHEN LOWER(TRIM(COALESCE(bug_project::text, ''))) = 'wifi' THEN 'WiFi' "
            "WHEN LOWER(TRIM(COALESCE(bug_project::text, ''))) = 'bt' THEN 'BT' "
            "WHEN LOWER(TRIM(COALESCE(bug_project::text, ''))) = 'cie' THEN 'Software' "
            "WHEN LOWER(TRIM(COALESCE(bug_project::text, ''))) = 'wot' THEN 'Tools' "
            "ELSE NULL END"
        )
    else:
        technology_expr = "NULL"
    title_expr = _title_expr(columns)
    # Same match as the Field Issues page (loading_dashboard._load_field_issues).
    field_issue_parts = [
        f"COALESCE({name}::text, '') ILIKE '%field issue%'"
        for name in ("ips_title", "jira_summary")
        if _has(columns, name)
    ]
    field_issue_expr = f"({' OR '.join(field_issue_parts)})" if field_issue_parts else "FALSE"

    valid_ips_select = f"""
        SELECT
            reporter,
            ips_case_number::text AS issue_key,
            MAX(COALESCE(title, '')) AS title,
            MAX(COALESCE(technology::text, '')) AS technology,
            BOOL_OR(COALESCE(is_field_issue, FALSE)) AS is_field_issue,
                        BOOL_OR(COALESCE(is_stale, FALSE)) AS is_stale,
                        BOOL_OR(COALESCE(is_close_pending, FALSE)) AS is_close_pending,
            'valid_ips' AS source_type
        FROM normalized
        WHERE reporter IS NOT NULL
          AND is_unpromoted_ips
                    AND COALESCE(is_stale, FALSE) = FALSE
                    AND COALESCE(is_close_pending, FALSE) = FALSE
          AND ips_case_number IS NOT NULL
          {"AND LOWER(reporter) = ANY(" + allowed_reporters_array + ")" if allowed_reporters_array != "NULL" else ""}
        GROUP BY reporter, ips_case_number
    """

    jira_select = f"""
        SELECT
            reporter,
            jira_id::text AS issue_key,
            MAX(COALESCE(title, '')) AS title,
            MAX(COALESCE(technology::text, '')) AS technology,
            BOOL_OR(COALESCE(is_field_issue, FALSE)) AS is_field_issue,
            FALSE AS is_stale,
            FALSE AS is_close_pending,
            'jira' AS source_type
        FROM normalized
        WHERE reporter IS NOT NULL
          AND jira_id IS NOT NULL
          {"AND LOWER(reporter) = ANY(" + allowed_reporters_array + ")" if allowed_reporters_array != "NULL" else ""}
        GROUP BY reporter, jira_id
    """

    final_select = valid_ips_select
    if include_jira:
        final_select = valid_ips_select + "\nUNION ALL\n" + jira_select

    query = f"""
        WITH base AS (
            SELECT
                {_reporter_expr(columns)} AS reporter,
                {ips_case_num_expr} AS ips_case_number,
                {jira_id_expr} AS jira_id,
                {title_expr} AS title,
                {field_issue_expr} AS is_field_issue,
                {created_expr} AS created_date,
                {ips_status_expr} AS ips_status,
                {ips_sub_status_raw_expr} AS ips_sub_status,
                {jira_status_expr} AS jira_status,
                {hsd_status_reason_expr} AS hsd_status_reason,
                {hsd_id_expr} AS hsd_id,
                {technology_expr} AS technology,
                {unpromoted_ips_cond} AS is_unpromoted_ips,
                {stale_cond} AS is_stale,
                {close_pending_cond} AS is_close_pending,
                CASE
                    WHEN {ips_case_num_expr} IS NULL THEN 1
                    ELSE ROW_NUMBER() OVER (
                        PARTITION BY {ips_case_num_expr}
                        ORDER BY {ips_status_rank} DESC, {ips_order_expr} DESC NULLS LAST
                    )
                END AS ips_rn,
                CASE
                    WHEN {jira_id_expr} IS NULL THEN 1
                    ELSE ROW_NUMBER() OVER (
                        PARTITION BY {jira_id_expr}
                        ORDER BY {jira_status_rank} DESC, {ips_order_expr} DESC NULLS LAST
                    )
                END AS jira_rn
            FROM {table}
        ),
        normalized AS (
            SELECT *
            FROM base
            WHERE {_normalized_filter_expr(ips_case_num_expr, jira_id_expr)}
              AND {_resolve_count_filter_expr(columns, count_profile)}
        )
                {final_select}
                ORDER BY reporter, issue_key;
    """
    return db.query_rows(query, None)


def _build_weighted_summary(
    db: DbConnector,
    table: str,
    breakdown_rows: List[Dict[str, Any]],
    *,
    model_path: str,
    weight_map_path: str,
    count_profile: str = "offload",
) -> Tuple[Dict[str, Dict[str, float]], List[Dict[str, Any]]]:
    try:
        from issue_category_model import (
            classify_and_weight_rows,
            load_category_model,
            load_weight_map,
        )
    except Exception as exc:
        LOG.warning("Category weighting disabled (dependency/import issue): %s", exc)
        return {}, []

    if not model_path or not os.path.exists(model_path):
        LOG.warning("Category weighting disabled: model file not found (%s)", model_path)
        return {}, []

    try:
        model_bundle = load_category_model(model_path)
        category_weights, category_technology_weights, default_weight = load_weight_map(weight_map_path)
    except Exception as exc:
        LOG.warning("Category weighting disabled (model/weight load failed): %s", exc)
        return {}, []

    weight_rows = _get_unpromoted_ips_rows_for_weighting(
        db,
        table,
        filter_allowed=True,
        include_jira=True,
        count_profile=count_profile,
    )
    if not weight_rows:
        return {}, []

    active_limits: Dict[str, Tuple[int, int]] = {}
    for breakdown_row in breakdown_rows:
        reporter = _normalize_reporter(str(breakdown_row.get("reporter") or ""))
        if not reporter:
            continue
        raw_uips = int(breakdown_row.get("num_unpromoted_ips") or 0)
        stale_ips = int(breakdown_row.get("num_stale") or 0)
        close_pending = int(breakdown_row.get("num_close_pending") or 0)
        active_limits[reporter] = (
            max(0, raw_uips - stale_ips - close_pending),
            int(breakdown_row.get("num_jira") or 0),
        )

    active_candidates = [
        row
        for row in weight_rows
        if not bool(row.get("is_stale")) and not bool(row.get("is_close_pending"))
    ]
    active_weight_rows: List[Dict[str, Any]] = []
    grouped_candidates: Dict[str, List[Dict[str, Any]]] = {}
    for row in active_candidates:
        reporter = _normalize_reporter(str(row.get("reporter") or ""))
        if reporter:
            grouped_candidates.setdefault(reporter, []).append(row)
    for reporter, candidates in grouped_candidates.items():
        active_ips_limit, jira_limit = active_limits.get(reporter, (0, 0))
        jira_rows = [row for row in candidates if str(row.get("source_type") or "").lower() == "jira"]
        ips_rows = [row for row in candidates if str(row.get("source_type") or "").lower() != "jira"]
        active_weight_rows.extend(jira_rows[:jira_limit])
        active_weight_rows.extend(ips_rows[:active_ips_limit])
    classified = classify_and_weight_rows(
        active_weight_rows,
        model_bundle=model_bundle,
        category_weights=category_weights,
        category_technology_weights=category_technology_weights,
        default_weight=default_weight,
    )

    weighted_uips_by_reporter: Dict[str, float] = {}
    weighted_jira_by_reporter: Dict[str, float] = {}
    for row in classified:
        reporter = _normalize_reporter(str(row.get("reporter") or ""))
        if not reporter:
            continue
        source_type = str(row.get("source_type") or "valid_ips").strip().lower()
        issue_weight = float(row.get("issue_weight") or 0.0)
        if source_type == "jira":
            weighted_jira_by_reporter[reporter] = weighted_jira_by_reporter.get(reporter, 0.0) + issue_weight
        else:
            weighted_uips_by_reporter[reporter] = weighted_uips_by_reporter.get(reporter, 0.0) + issue_weight

    # Category columns represent active classified issues only: valid IPS rows
    # already exclude stale/close-pending items, while Jira rows are active queue items.
    category_counts: Dict[Tuple[str, str, str], int] = {}
    for row in classified:
        reporter = _normalize_reporter(str(row.get("reporter") or ""))
        if not reporter:
            continue
        source_type = str(row.get("source_type") or "").strip().lower()
        if source_type not in {"valid_ips", "jira"}:
            continue
        category = str(row.get("predicted_human_category") or "Uncategorized").strip() or "Uncategorized"
        technology = str(row.get("technology") or "Unknown").strip() or "Unknown"
        key = (reporter, category, technology)
        category_counts[key] = int(category_counts.get(key, 0)) + 1

    category_rows: List[Dict[str, Any]] = []
    for (reporter_norm, category, technology), issue_count in category_counts.items():
        display_reporter = next(
            (str(r.get("reporter") or "") for r in breakdown_rows if _normalize_reporter(str(r.get("reporter") or "")) == reporter_norm),
            reporter_norm,
        )
        category_rows.append(
            {
                "reporter": display_reporter,
                "category": category,
                "technology": technology,
                "issue_count": issue_count,
            }
        )

    category_rows.sort(
        key=lambda r: (
            str(r.get("reporter") or ""),
            -int(r.get("issue_count") or 0),
            str(r.get("category") or ""),
            str(r.get("technology") or ""),
        )
    )

    result: Dict[str, Dict[str, float]] = {}
    for row in breakdown_rows:
        reporter = _normalize_reporter(str(row.get("reporter") or ""))
        if not reporter:
            continue
        raw_uips = float(row.get("num_unpromoted_ips") or 0.0)
        raw_stale = float(row.get("num_stale") or 0.0)
        raw_close_pending = float(row.get("num_close_pending") or 0.0)
        valid_uips = max(0.0, raw_uips - raw_stale - raw_close_pending)
        raw_jira = float(row.get("num_jira") or 0.0)
        raw_curr = float(row.get("total_current_issue_count") or 0.0)
        weighted_uips = weighted_uips_by_reporter.get(reporter, valid_uips)
        weighted_jira = weighted_jira_by_reporter.get(reporter, raw_jira)
        raw_hsd = float(row.get("num_unpromoted_hsd") or 0.0)
        raw_stale_hsd = float(row.get("num_stale_hsd") or 0.0)
        active_hsd = max(0.0, raw_hsd - raw_stale_hsd)
        weighted_current = active_hsd + weighted_uips + weighted_jira
        result[reporter] = {
            "weighted_unpromoted_ips": round(weighted_uips, 2),
            "weighted_jira": round(weighted_jira, 2),
            "weighted_current_issue_count": round(weighted_current, 2),
        }
    return result, category_rows


def _history_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _load_history(path: str) -> Dict[str, Any]:
    try:
        if not os.path.exists(path):
            return {"entries": []}
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, dict):
            return {"entries": []}
        entries = data.get("entries")
        if not isinstance(entries, list):
            data["entries"] = []
        return data
    except Exception as exc:
        LOG.warning("Failed to load history file %s: %s", path, exc)
        return {"entries": []}


def _save_history(path: str, history: Dict[str, Any]) -> None:
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(history, handle, indent=2, ensure_ascii=False)


def _case_key(value: Any) -> str:
    return str(value or "").strip()


def _get_case_current_reporter(db: DbConnector, table: str, case_number: str) -> Optional[str]:
    case_number = _case_key(case_number)
    if not case_number:
        return None
    columns = _get_table_columns(db, table)
    ips_order_expr = _ips_order_expr(columns)
    ips_status_rank = _ips_status_rank_expr(columns)
    issue_expr = _issue_case_expr()
    reporter_expr = _reporter_expr(columns)
    case_literal = _sql_literal(case_number)
    query = f"""
        WITH base AS (
            SELECT
                {reporter_expr} AS reporter,
                {issue_expr} AS ips_case_number,
                CASE
                    WHEN {issue_expr} IS NULL THEN 1
                    ELSE ROW_NUMBER() OVER (
                        PARTITION BY {issue_expr}
                        ORDER BY {ips_status_rank} DESC, {ips_order_expr} DESC NULLS LAST
                    )
                END AS ips_rn
            FROM {table}
            WHERE {issue_expr} = {case_literal}
        )
        SELECT reporter
        FROM base
        WHERE ips_rn = 1
        LIMIT 1;
    """
    rows = db.query_rows(query, None)
    if not rows:
        return None
    return str(rows[0].get("reporter") or "").strip() or None


def _refresh_history_statuses(
    db: DbConnector,
    table: str,
    history: Dict[str, Any],
) -> Tuple[List[Dict[str, Any]], bool]:
    changed = False
    active_entries: List[Dict[str, Any]] = []
    allowed_receivers = set(_allowed_reporters())
    for entry in history.get("entries", []):
        if not isinstance(entry, dict):
            continue
        if str(entry.get("table") or table) != table:
            continue
        if str(entry.get("status") or "pending") != "pending":
            continue
        case_number = _case_key(entry.get("ips_case_number"))
        overloaded_reporter = str(entry.get("overloaded_reporter") or "").strip()
        receiving_reporter = str(entry.get("receiving_reporter") or "").strip()
        overloaded_norm = _normalize_reporter(overloaded_reporter)
        receiving_norm = _normalize_reporter(receiving_reporter)

        if receiving_norm not in allowed_receivers or _is_always_excluded_reporter(receiving_reporter):
            entry["status"] = "cancelled"
            entry["cancelled_at"] = _history_now_iso()
            entry["cancel_reason"] = "Receiving reporter is no longer eligible for offload rotation."
            changed = True
            continue

        current_reporter = _get_case_current_reporter(db, table, case_number)
        current_norm = _normalize_reporter(current_reporter or "")

        # Mark realized only when ownership actually lands on the intended receiver.
        if current_norm and current_norm == receiving_norm and current_norm != overloaded_norm:
            entry["status"] = "realized"
            entry["realized_at"] = _history_now_iso()
            entry["current_reporter"] = current_reporter
            changed = True
            continue
        # If ownership moved away from source but not to intended receiver, mark as diverted.
        if current_norm and current_norm != overloaded_norm and current_norm != receiving_norm:
            entry["status"] = "diverted"
            entry["diverted_at"] = _history_now_iso()
            entry["current_reporter"] = current_reporter
            entry["divert_reason"] = "Owner changed to a different engineer than recommended receiver."
            changed = True
            continue
        active_entries.append(entry)
    return active_entries, changed


def _apply_pending_adjustments(
    counts: List[Dict[str, Any]],
    active_entries: Sequence[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    count_map: Dict[str, Dict[str, Any]] = {}
    for row in counts:
        reporter = str(row.get("reporter") or "").strip()
        if not reporter:
            continue
        if _is_always_excluded_reporter(reporter):
            continue
        norm = _normalize_reporter(reporter)
        count_map[norm] = {
            "reporter": reporter,
            "total_current_issue_count": int(row.get("total_current_issue_count") or 0),
        }

    # Keep all allowed reporters eligible as receivers, even if they currently have no issue rows.
    seeded_allowed_zero = 0
    for norm, reporter in _allowed_reporters_display_map().items():
        if _is_always_excluded_reporter(reporter):
            continue
        if norm not in count_map:
            count_map[norm] = {
                "reporter": reporter,
                "total_current_issue_count": 0,
            }
            seeded_allowed_zero += 1

    if seeded_allowed_zero > 0:
        LOG.info(
            "Included %d allowed reporter(s) with zero current issues for receiver rotation.",
            seeded_allowed_zero,
        )

    for entry in active_entries:
        source = str(entry.get("overloaded_reporter") or "").strip()
        target = str(entry.get("receiving_reporter") or "").strip()
        if not source or not target:
            continue

        source_norm = _normalize_reporter(source)
        target_norm = _normalize_reporter(target)

        if source_norm not in count_map:
            count_map[source_norm] = {"reporter": source, "total_current_issue_count": 0}
        if target_norm not in count_map:
            count_map[target_norm] = {"reporter": target, "total_current_issue_count": 0}

        count_map[source_norm]["total_current_issue_count"] = max(
            0, int(count_map[source_norm].get("total_current_issue_count") or 0) - 1
        )
        count_map[target_norm]["total_current_issue_count"] = int(
            count_map[target_norm].get("total_current_issue_count") or 0
        ) + 1

    return sorted(
        list(count_map.values()),
        key=lambda r: (int(r.get("total_current_issue_count") or 0), str(r.get("reporter") or "")),
    )


def _known_history_case_numbers(
    history: Dict[str, Any],
    table: str,
    *,
    statuses: Optional[Sequence[str]] = None,
) -> List[str]:
    status_set = {str(s or "").strip().lower() for s in (statuses or ["pending", "realized"]) if str(s or "").strip()}
    cases: List[str] = []
    for entry in history.get("entries", []):
        if not isinstance(entry, dict):
            continue
        if str(entry.get("table") or table) != table:
            continue
        entry_status = str(entry.get("status") or "").strip().lower()
        if status_set and entry_status not in status_set:
            continue
        case_number = _case_key(entry.get("ips_case_number"))
        if case_number:
            cases.append(case_number)
    return cases


def _history_entries_for_table(history: Dict[str, Any], table: str) -> List[Dict[str, Any]]:
    entries: List[Dict[str, Any]] = []
    for entry in history.get("entries", []):
        if not isinstance(entry, dict):
            continue
        if str(entry.get("table") or table) != table:
            continue
        entries.append(entry)
    entries.sort(key=lambda e: str(e.get("created_at") or ""), reverse=True)
    return entries


def _recent_receiving_reporters(
    history_entries: Sequence[Dict[str, Any]],
    *,
    statuses: Sequence[str] = ("pending", "realized"),
    limit: int = 20,
    groups: Optional[set[str]] = None,
    max_age_days: Optional[int] = None,
) -> set[str]:
    status_set = {str(s or "").strip().lower() for s in statuses if str(s or "").strip()}
    group_set = {str(g or "").strip().lower() for g in (groups or set()) if str(g or "").strip()}
    recent = list(history_entries)[: max(0, int(limit))]
    reporters: set[str] = set()
    for entry in recent:
        if not isinstance(entry, dict):
            continue
        status = str(entry.get("status") or "").strip().lower()
        if status_set and status not in status_set:
            continue
        if max_age_days is not None and int(max_age_days) >= 0:
            created_ts = _parse_history_datetime(entry.get("created_at"))
            if created_ts is None:
                continue
            if created_ts.tzinfo is None:
                created_ts = created_ts.replace(tzinfo=timezone.utc)
            age_days = int((datetime.now(timezone.utc) - created_ts.astimezone(timezone.utc)).days)
            if age_days > int(max_age_days):
                continue
        receiver = _normalize_reporter(str(entry.get("receiving_reporter") or ""))
        if group_set:
            receiver_groups = _groups_for_reporter(receiver)
            if not receiver_groups or not (receiver_groups & group_set):
                continue
        if receiver:
            reporters.add(receiver)
    return reporters


def _parse_history_datetime(value: Any) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _is_receiver_rotation_reset_due(
    history_entries: Sequence[Dict[str, Any]],
    reset_days: int,
    *,
    statuses: Sequence[str] = ("pending", "realized"),
) -> bool:
    if reset_days <= 0:
        return False

    status_set = {str(s or "").strip().lower() for s in statuses if str(s or "").strip()}
    latest_ts: Optional[datetime] = None
    for entry in history_entries:
        if not isinstance(entry, dict):
            continue
        status = str(entry.get("status") or "").strip().lower()
        if status_set and status not in status_set:
            continue
        created_ts = _parse_history_datetime(entry.get("created_at"))
        if created_ts is None:
            continue
        if created_ts.tzinfo is None:
            created_ts = created_ts.replace(tzinfo=timezone.utc)
        if latest_ts is None or created_ts > latest_ts:
            latest_ts = created_ts

    if latest_ts is None:
        return False

    age = datetime.now(timezone.utc) - latest_ts.astimezone(timezone.utc)
    return age.days >= int(reset_days)


def _days_since_last_offload_activity(history_entries: Sequence[Dict[str, Any]]) -> Optional[int]:
    return _days_since_last_offload_activity_by_status(history_entries, statuses=("pending", "realized"))


def _days_since_last_offload_activity_by_status(
    history_entries: Sequence[Dict[str, Any]],
    *,
    statuses: Sequence[str],
) -> Optional[int]:
    status_set = {str(s or "").strip().lower() for s in statuses if str(s or "").strip()}
    latest_ts: Optional[datetime] = None
    for entry in history_entries:
        if not isinstance(entry, dict):
            continue
        status = str(entry.get("status") or "").strip().lower()
        if status_set and status not in status_set:
            continue
        created_ts = _parse_history_datetime(entry.get("created_at"))
        if created_ts is None:
            continue
        if created_ts.tzinfo is None:
            created_ts = created_ts.replace(tzinfo=timezone.utc)
        if latest_ts is None or created_ts > latest_ts:
            latest_ts = created_ts

    if latest_ts is None:
        return None

    age = datetime.now(timezone.utc) - latest_ts.astimezone(timezone.utc)
    return int(age.days)


def _env_int(name: str, default: int) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return int(default)
    try:
        value = int(raw)
    except ValueError:
        return int(default)
    return value if value >= 0 else int(default)


def _load_reporter_email_map(recipients_path: str) -> Dict[str, str]:
    try:
        with open(recipients_path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, dict):
            return {}
        raw_map = data.get("reporter_email_map")
        if not isinstance(raw_map, dict):
            return {}
        normalized: Dict[str, str] = {}
        for reporter, email in raw_map.items():
            reporter_norm = _normalize_reporter(str(reporter or ""))
            email_text = str(email or "").strip()
            if reporter_norm and email_text:
                normalized[reporter_norm] = email_text
        return normalized
    except Exception as exc:
        LOG.warning("Failed to load reporter_email_map from %s: %s", recipients_path, exc)
        return {}


def _minutes_since(value: Any) -> Optional[int]:
    parsed = _parse_history_datetime(value)
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int((datetime.now(timezone.utc) - parsed.astimezone(timezone.utc)).total_seconds() // 60)


def _is_recent_trigger_entry(entry: Dict[str, Any], cooldown_minutes: int) -> bool:
    if cooldown_minutes <= 0:
        return False
    mins = _minutes_since(entry.get("created_at"))
    return mins is not None and mins < cooldown_minutes


def _build_trigger_notification_body(
    reporter: str,
    current_count: float,
    threshold: int,
) -> str:
    return (
        "IPS Offload Trigger Notification\n\n"
        f"- Engineer: {reporter}\n"
        f"- Current issue count: {current_count:.2f}\n"
        f"- Threshold: {int(threshold)}\n\n"
        "Action required:\n"
        "Please move one eligible IPS issue to Jonathan Tsao queue first.\n"
        "After this transfer, the next scheduler run will recommend which IPS in Jonathan queue to offload to the least-loaded engineer.\n"
    )


def _pending_entries_for_table(history: Dict[str, Any], table: str) -> List[Dict[str, Any]]:
    return [
        entry
        for entry in _history_entries_for_table(history, table)
        if str(entry.get("status") or "").strip().lower() == "pending"
    ]


def _auto_cancel_closed_case_pending_entries(
    db: "DbConnector",
    table: str,
    history: Dict[str, Any],
) -> int:
    """Query the DB for IPS status of each pending entry's ips_case_number.

    If the case is found to be closed in the DB, mark the history entry as
    'cancelled' with a reason. Returns the number of entries auto-cancelled.
    """
    pending = _pending_entries_for_table(history, table)
    if not pending:
        return 0

    # Collect case numbers that have a numeric IPS case number
    case_numbers = [
        str(e.get("ips_case_number") or "").strip()
        for e in pending
        if str(e.get("ips_case_number") or "").strip().isdigit()
    ]
    if not case_numbers:
        return 0

    # Check which columns exist so we query only available status columns
    columns = _get_table_columns(db, table)
    has_ips_status = _has(columns, "ips_status")
    has_ips_sub_status = _has(columns, "ips_sub_status")
    has_jira_status = _has(columns, "jira_status")
    has_ips_case_number = _has(columns, "ips_case_number")

    if not has_ips_case_number:
        return 0

    select_parts = ["ips_case_number::text AS ips_case_number"]
    if has_ips_status:
        select_parts.append("LOWER(TRIM(COALESCE(ips_status::text, ''))) AS ips_status")
    if has_ips_sub_status:
        select_parts.append("LOWER(TRIM(COALESCE(ips_sub_status::text, ''))) AS ips_sub_status")
    if has_jira_status:
        select_parts.append("LOWER(TRIM(COALESCE(jira_status::text, ''))) AS jira_status")

    placeholders = ", ".join(["%s"] * len(case_numbers))
    query = (
        f"SELECT {', '.join(select_parts)} FROM {table} "
        f"WHERE ips_case_number::text IN ({placeholders})"
    )
    try:
        rows = db.query_rows(query, case_numbers)
    except Exception as exc:
        LOG.warning("Auto-cancel closed case check failed: %s", exc)
        return 0

    # Build a map: case_number -> is_closed_or_not_offloadable
    closed_cases: set = set()
    for row in rows:
        case_num = str(row.get("ips_case_number") or "").strip()
        ips_st = str(row.get("ips_status") or "")
        ips_sub = str(row.get("ips_sub_status") or "")
        jira_st = str(row.get("jira_status") or "")
        ips_sub_norm = ips_sub.replace(" ", "-")
        is_closed = (
            ips_st == "closed"
            or ips_sub == "closed"
            or ips_sub_norm in ("close-pending", "pending-closed", "pending-close")
            or jira_st in ("closed", "implemented", "verify")
        )
        if is_closed:
            closed_cases.add(case_num)

    cancelled = 0
    now_utc = datetime.now(timezone.utc).isoformat()
    for entry in history.get("entries", []):
        if str(entry.get("status") or "").strip().lower() != "pending":
            continue
        case_num = str(entry.get("ips_case_number") or "").strip()
        if case_num in closed_cases:
            entry["status"] = "cancelled"
            entry["cancelled_at"] = now_utc
            entry["cancelled_reason"] = "IPS case is closed/close-pending in DB; offload no longer applicable."
            LOG.info(
                "Auto-cancelled pending offload for case %s (IPS case is closed/close-pending).", case_num
            )
            cancelled += 1

    return cancelled


    count_map: Dict[str, float] = {}
    for row in counts:
        reporter_norm = _normalize_reporter(str(row.get("reporter") or ""))
        if not reporter_norm:
            continue
        effective = row.get("effective_current_issue_count")
        if effective is None:
            effective = row.get("total_current_issue_count")
        count_map[reporter_norm] = float(effective or 0.0)
    return count_map


def _source_threshold_for_reporter(reporter: str, wifi_threshold: int, bt_threshold: int) -> int:
    groups = _groups_for_reporter(reporter)
    if "wifi" in groups and "bt" in groups:
        return int(min(wifi_threshold, bt_threshold))
    if "bt" in groups:
        return int(bt_threshold)
    return int(wifi_threshold)


def _build_current_count_map(
    decision_counts: Sequence[Dict[str, Any]],
) -> Dict[str, float]:
    """Return {reporter_norm: effective_current_issue_count} from decision_counts_all rows."""
    result: Dict[str, float] = {}
    for row in decision_counts:
        reporter_norm = _normalize_reporter(str(row.get("reporter") or ""))
        if reporter_norm:
            result[reporter_norm] = float(row.get("effective_current_issue_count") or 0.0)
    return result


def _build_over_threshold_reporters(
    counts: Sequence[Dict[str, Any]],
    *,
    wifi_threshold: int,
    bt_threshold: int,
) -> List[Dict[str, Any]]:
    overloaded: List[Dict[str, Any]] = []
    for row in counts:
        reporter = str(row.get("reporter") or "").strip()
        if not reporter:
            continue
        if _is_always_excluded_reporter(reporter):
            continue
        groups = _groups_for_reporter(reporter)
        if not groups:
            continue
        current = float(row.get("effective_current_issue_count") or 0.0)
        threshold = _source_threshold_for_reporter(reporter, wifi_threshold, bt_threshold)
        if current > float(threshold):
            overloaded.append(
                {
                    "reporter": reporter,
                    "reporter_norm": _normalize_reporter(reporter),
                    "groups": groups,
                    "current": current,
                    "threshold": int(threshold),
                    "over_by": current - float(threshold),
                }
            )

    overloaded.sort(key=lambda r: (-float(r.get("over_by") or 0.0), str(r.get("reporter") or "")))
    return overloaded


def _open_trigger_entries_for_table(history: Dict[str, Any], table: str) -> List[Dict[str, Any]]:
    entries: List[Dict[str, Any]] = []
    for entry in history.get("entries", []):
        if not isinstance(entry, dict):
            continue
        if str(entry.get("table") or table) != table:
            continue
        if str(entry.get("status") or "").strip().lower() != "awaiting_source_transfer":
            continue
        entries.append(entry)
    entries.sort(key=lambda e: str(e.get("created_at") or ""), reverse=True)
    return entries


def _close_trigger_entries_when_recovered(
    trigger_entries: Sequence[Dict[str, Any]],
    *,
    current_count_map: Dict[str, float],
    wifi_threshold: int,
    bt_threshold: int,
) -> bool:
    changed = False
    for entry in trigger_entries:
        reporter = str(entry.get("trigger_reporter") or "").strip()
        if not reporter:
            continue
        reporter_norm = _normalize_reporter(reporter)
        current = float(current_count_map.get(reporter_norm, 0.0))
        threshold = _source_threshold_for_reporter(reporter, wifi_threshold, bt_threshold)
        if current <= float(threshold):
            entry["status"] = "trigger_cleared"
            entry["cleared_at"] = _history_now_iso()
            entry["cleared_reason"] = "Current count is now at/below threshold."
            changed = True
    return changed


def _filter_pending_entries_still_need_offload(
    pending_entries: Sequence[Dict[str, Any]],
    current_count_map: Dict[str, float],
    threshold: int,
    bt_threshold: int,
    receiver_max_issues: int,
) -> List[Dict[str, Any]]:
    filtered: List[Dict[str, Any]] = []
    skipped = 0
    allowed_receivers = set(_allowed_reporters())
    for entry in pending_entries:
        trigger_reporter = str(entry.get("trigger_reporter") or entry.get("overloaded_reporter") or "")
        overloaded_reporter = str(entry.get("overloaded_reporter") or trigger_reporter or "")
        source_norm = _normalize_reporter(trigger_reporter)
        overloaded_norm = _normalize_reporter(overloaded_reporter)
        receiving_reporter = str(entry.get("receiving_reporter") or "")
        target_norm = _normalize_reporter(receiving_reporter)
        if not source_norm or not target_norm:
            skipped += 1
            continue
        if target_norm not in allowed_receivers or _is_always_excluded_reporter(receiving_reporter):
            LOG.info(
                "Pending reminder check: case=%s target=%s is no longer an eligible receiver => skip reminder",
                str(entry.get("ips_case_number") or ""),
                receiving_reporter,
            )
            skipped += 1
            continue

        source_current = float(current_count_map.get(source_norm, 0.0))
        target_current = float(current_count_map.get(target_norm, 0.0))
        source_threshold = _source_threshold_for_reporter(trigger_reporter, threshold, bt_threshold)
        receiver_within_cap = target_current <= float(receiver_max_issues)
        still_needed = (
            source_current > float(source_threshold)
            and source_current > target_current
            and receiver_within_cap
        )
        LOG.info(
            "Pending reminder check: case=%s source=%s(%.2f) target=%s(%.2f) threshold=%d receiver_max=%d => %s",
            str(entry.get("ips_case_number") or ""),
            trigger_reporter,
            source_current,
            receiving_reporter,
            target_current,
            int(source_threshold),
            int(receiver_max_issues),
            "send reminder" if still_needed else "skip reminder",
        )
        if still_needed:
            enriched = dict(entry)
            # Keep email display consistent with "Giving Engineer" (= overloaded_reporter).
            overloaded_current = float(current_count_map.get(overloaded_norm, source_current))
            enriched["overloaded_count_current"] = overloaded_current
            enriched["receiving_count_current"] = target_current
            filtered.append(enriched)
        else:
            skipped += 1

    if skipped:
        LOG.info(
            "Pending reminder filter skipped %d entrie(s): offload no longer needed based on current load.",
            skipped,
        )
    return filtered


def _collect_pending_reminder_recipients(
    pending_entries: Sequence[Dict[str, Any]],
    reporter_email_map: Dict[str, str],
    fallback_to_list: Sequence[str],
) -> List[str]:
    recipients: set[str] = set()
    unresolved_reporters: set[str] = set()

    for entry in pending_entries:
        for key in ("overloaded_reporter", "receiving_reporter"):
            reporter_name = str(entry.get(key) or "").strip()
            reporter_norm = _normalize_reporter(reporter_name)
            if not reporter_norm:
                continue
            email = reporter_email_map.get(reporter_norm, "").strip()
            if email:
                recipients.add(email)
            else:
                unresolved_reporters.add(reporter_name)

    if unresolved_reporters:
        LOG.warning(
            "No email mapping found for pending reminder reporters: %s",
            ", ".join(sorted(unresolved_reporters)),
        )

    if recipients:
        return sorted(recipients)

    fallback = sorted({str(v or "").strip() for v in fallback_to_list if str(v or "").strip()})
    if fallback:
        LOG.warning(
            "Pending reminders fallback to default recipient list because no reporter_email_map recipients were resolved."
        )
    return fallback


def _build_pending_reminder_html(pending_entries: Sequence[Dict[str, Any]]) -> str:
    rows = []
    for entry in pending_entries:
        rows.append(
            "<tr>"
            f"<td>{escape(str(entry.get('created_at') or ''))}</td>"
            f"<td>{escape(str(entry.get('ips_case_number') or ''))}</td>"
            f"<td>{escape(str(entry.get('overloaded_reporter') or ''))}</td>"
            f"<td>{escape(str(entry.get('receiving_reporter') or ''))}</td>"
            f"<td>{int(entry.get('overloaded_count_current') or entry.get('overloaded_count_at_offload') or 0)}</td>"
            f"<td>{int(entry.get('receiving_count_current') or entry.get('receiving_count_at_offload') or 0)}</td>"
            f"<td>{int(entry.get('overloaded_count_at_offload') or 0)}</td>"
            f"<td>{int(entry.get('receiving_count_at_offload') or 0)}</td>"
            "</tr>"
        )

    return (
        "<html><body>"
        "<p><b>IPS Offload Pending Re-Assignment Reminder</b></p>"
        "<p>The following offload transactions are still <b>pending</b>."
        " Please complete the re-assignment in IPS.</p>"
        "<table border='1' cellpadding='4' cellspacing='0' style='border-collapse:collapse;'>"
        "<tr><th>Offload Time</th><th>Case</th><th>Giving Engineer</th><th>Receiving Engineer</th><th>Giving Current Cnt</th><th>Receiving Current Cnt</th><th>Giving Cnt at Offload</th><th>Receiving Cnt at Offload</th></tr>"
        + "".join(rows)
        + "</table>"
        "</body></html>"
    )


def _render_history_html(history_entries: Sequence[Dict[str, Any]], *, limit: int = 20) -> str:
    if not history_entries:
        return "<p>Offload history record: none yet.</p>"

    def _format_timestamp_local(value: Any) -> str:
        parsed = _parse_history_datetime(value)
        if parsed is None:
            return escape(str(value or ""))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        local_ts = parsed.astimezone()
        return escape(local_ts.strftime("%Y-%m-%d %H:%M:%S %Z"))

    def _status_style(status_value: str) -> str:
        status_norm = str(status_value or "").strip().lower()
        if status_norm == "pending":
            return "background-color:#fff3cd;color:#7a5d00;font-weight:600;"
        if status_norm == "realized":
            return "background-color:#d1e7dd;color:#0f5132;font-weight:600;"
        if status_norm == "cancelled":
            return "background-color:#e2e3e5;color:#41464b;font-weight:600;"
        if status_norm == "diverted":
            return "background-color:#ffe5d0;color:#7f3f00;font-weight:600;"
        return ""

    rows = []
    for entry in list(history_entries)[:limit]:
        status_text = str(entry.get("status") or "")
        status_cell_style = _status_style(status_text)
        rows.append(
            "<tr>"
            f"<td>{_format_timestamp_local(entry.get('created_at'))}</td>"
            f"<td style='{status_cell_style}'>{escape(status_text)}</td>"
            f"<td>{escape(str(entry.get('ips_case_number') or ''))}</td>"
            f"<td>{escape(str(entry.get('overloaded_reporter') or ''))}</td>"
            f"<td>{escape(str(entry.get('receiving_reporter') or ''))}</td>"
            f"<td>{float(entry.get('overloaded_count_at_offload') or 0.0):.2f}</td>"
            f"<td>{float(entry.get('receiving_count_at_offload') or 0.0):.2f}</td>"
            "</tr>"
        )
    return (
        "<p><b>Offload history record</b></p>"
        "<table border='1' cellpadding='4' cellspacing='0' style='border-collapse:collapse;'>"
        "<tr><th>Time</th><th>Status</th><th>Case</th><th>From</th><th>To</th><th>From Cnt</th><th>To Cnt</th></tr>"
        + "".join(rows)
        + "</table>"
    )


def _build_email_body_html(
    *,
    overloaded: List[Dict[str, Any]],
    receiving_engineer: Dict[str, Any],
    issue: Dict[str, Any],
    combined_trial_categories: Optional[List[str]] = None,
    combined_trial_rows: Optional[List[Dict[str, Any]]] = None,
    history_html: str,
) -> str:
    issue_case = escape(str(issue.get("ips_case_number") or "(unknown)"))
    issue_title = escape(str(issue.get("title") or "(no title)"))
    issue_reporter = escape(str(issue.get("reporter") or "(unassigned)"))
    created_date = escape(str(issue.get("created_date") or "(unknown)"))

    recv_name = escape(str(receiving_engineer.get("reporter") or "(unassigned)"))
    recv_curr = float(receiving_engineer.get("total_current_issue_count") or 0.0)
    recv_wcurr = receiving_engineer.get("weighted_trial_current_issue_count")
    recv_wcurr_text = "N/A" if recv_wcurr is None else f"{float(recv_wcurr):.2f}"

    combined_table_html = ""
    if combined_trial_rows:
        category_headers = combined_trial_categories or []
        header_html = (
            "<tr>"
            "<th>Name</th><th>uIPS</th><th>Active IPS</th><th>uHSD</th><th>Active HSD</th><th>Jira</th><th>Stale IPS</th><th>Stale HSD</th>"
            "<th>Total</th><th>Weighted Loading</th>"
            + "".join(f"<th>{escape(cat)}</th>" for cat in category_headers)
            + "</tr>"
        )

        body_rows = []
        for row in combined_trial_rows:
            category_counts = row.get("category_counts") or {}
            body_rows.append(
                "<tr>"
                f"<td>{escape(str(row.get('name') or ''))}</td>"
                f"<td style='text-align:right'>{int(row.get('uips') or 0)}</td>"
                f"<td style='text-align:right'>{int(row.get('active_uips') or 0)}</td>"
                f"<td style='text-align:right'>{int(row.get('uhsd') or 0)}</td>"
                f"<td style='text-align:right'>{int(row.get('active_hsd') or 0)}</td>"
                f"<td style='text-align:right'>{int(row.get('jira') or 0)}</td>"
                f"<td style='text-align:right'>{int(row.get('stale_ips') or 0)}</td>"
                f"<td style='text-align:right'>{int(row.get('stale_hsd') or 0)}</td>"
                f"<td style='text-align:right'>{int(row.get('total') or 0)}</td>"
                f"<td style='text-align:right'>{escape(str(row.get('weighted_loading') or ''))}</td>"
                + "".join(
                    f"<td style='text-align:right'>{int(category_counts.get(cat, 0))}</td>" for cat in category_headers
                )
                + "</tr>"
            )

        combined_table_html = (
            "<p>Reporter load summary (single combined table):</p>"
            "<table border='1' cellpadding='4' cellspacing='0' style='border-collapse:collapse;'>"
            f"{header_html}"
            + "".join(body_rows)
            + "</table>"
        )
    else:
        overloaded_rows = "".join(
            "<tr>"
            f"<td>{escape(str(row.get('reporter') or ''))}</td>"
            f"<td>{float(row.get('total_current_issue_count') or 0.0):.2f}</td>"
            f"<td>{('N/A' if row.get('weighted_trial_current_issue_count') is None else f'{float(row.get('weighted_trial_current_issue_count') or 0.0):.2f}')}</td>"
            "</tr>"
            for row in overloaded
        )
        combined_table_html = (
            "<p>Source queue reporter load (weighted is trial-only):</p>"
            "<table border='1' cellpadding='4' cellspacing='0' style='border-collapse:collapse;'>"
            "<tr><th>Reporter</th><th>Curr</th><th>wCurr (Trial)</th></tr>"
            f"{overloaded_rows}"
            "</table>"
        )

    return (
        "<html><body>"
        "<p><b>IPS Offload Notification</b></p>"
        f"{combined_table_html}"
        "<p>Most recently created source issue in queue (IPS/HSD):</p>"
        "<ul>"
        f"<li>Case: {issue_case}</li>"
        f"<li>Title: {issue_title}</li>"
        f"<li>Created: {created_date}</li>"
        f"<li>Current reporter: {issue_reporter}</li>"
        "</ul>"
        f"<p><b>{issue_reporter}</b>, please reassign the IPS <b>{issue_case}</b> to <b>{recv_name}</b>.</p>"
        "<p>Re-assignment rule: always pick the receiving engineer with the lowest <b>Curr</b> issue count.</p>"
        f"<p>Receiving engineer: <b>{recv_name}</b> (Curr: {recv_curr:.2f}, wCurr Trial: {recv_wcurr_text})</p>"
        "<p><i>Note: wCurr is shown for trial visibility only and is not used in current offload threshold/selection.</i></p>"
        f"<p>Most updated team loading data: <a href='{DASHBOARD_URL}'>{DASHBOARD_URL}</a></p>"
        f"{history_html}"
        "</body></html>"
    )


def _build_loading_summary_email_html(
    *,
    combined_trial_categories: Optional[List[str]],
    combined_trial_rows: Optional[List[Dict[str, Any]]],
    decision_counts: Sequence[Dict[str, Any]],
    history_html: str,
    weight_map_path: str,
) -> str:
    def _load_weighting_definition(path: str) -> Tuple[float, List[Tuple[str, float]], List[Tuple[str, str, float]]]:
        default_weight = 1.0
        category_rows: List[Tuple[str, float]] = []
        category_tech_rows: List[Tuple[str, str, float]] = []
        try:
            if not path or not os.path.exists(path):
                return default_weight, category_rows, category_tech_rows
            with open(path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
            if not isinstance(payload, dict):
                return default_weight, category_rows, category_tech_rows

            try:
                default_weight = float(payload.get("default_weight", 1.0) or 1.0)
            except Exception:
                default_weight = 1.0

            raw_cat = payload.get("category_weights")
            if isinstance(raw_cat, dict):
                for category, weight in raw_cat.items():
                    cat_text = str(category or "").strip()
                    if not cat_text:
                        continue
                    try:
                        w = float(weight)
                    except Exception:
                        continue
                    category_rows.append((cat_text, w))

            raw_cat_tech = payload.get("category_technology_weights")
            if isinstance(raw_cat_tech, dict):
                for raw_key, weight in raw_cat_tech.items():
                    key_text = str(raw_key or "").strip()
                    if not key_text:
                        continue
                    if "|" in key_text:
                        category, technology = key_text.split("|", 1)
                    elif "@" in key_text:
                        category, technology = key_text.split("@", 1)
                    else:
                        category, technology = key_text, "Any"
                    cat_text = str(category or "").strip()
                    tech_text = str(technology or "").strip() or "Any"
                    if not cat_text:
                        continue
                    try:
                        w = float(weight)
                    except Exception:
                        continue
                    category_tech_rows.append((cat_text, tech_text, w))

            category_rows.sort(key=lambda x: x[0].lower())
            category_tech_rows.sort(key=lambda x: (x[0].lower(), x[1].lower()))
            return default_weight, category_rows, category_tech_rows
        except Exception as exc:
            LOG.warning("Failed to load weighting definition from %s: %s", path, exc)
            return default_weight, category_rows, category_tech_rows

    combined_trial_categories = combined_trial_categories or []
    combined_trial_rows = combined_trial_rows or []
    default_weight, category_weight_defs, category_tech_weight_defs = _load_weighting_definition(weight_map_path)

    if combined_trial_rows:
        header_html = (
            "<tr>"
            "<th>Name</th><th>uIPS</th><th>Active IPS</th><th>uHSD</th><th>Active HSD</th><th>Jira</th><th>Stale IPS</th><th>Stale HSD</th>"
            "<th>Total</th><th>Weighted Loading</th>"
            + "".join(f"<th>{escape(cat)}</th>" for cat in combined_trial_categories)
            + "</tr>"
        )

        body_rows: List[str] = []
        for row in combined_trial_rows:
            category_counts = row.get("category_counts") or {}
            body_rows.append(
                "<tr>"
                f"<td>{escape(str(row.get('name') or ''))}</td>"
                f"<td class='num'>{int(row.get('uips') or 0)}</td>"
                f"<td class='num'>{int(row.get('active_uips') or 0)}</td>"
                f"<td class='num'>{int(row.get('uhsd') or 0)}</td>"
                f"<td class='num'>{int(row.get('active_hsd') or 0)}</td>"
                f"<td class='num'>{int(row.get('jira') or 0)}</td>"
                f"<td class='num'>{int(row.get('stale_ips') or 0)}</td>"
                f"<td class='num'>{int(row.get('stale_hsd') or 0)}</td>"
                f"<td class='num'>{int(row.get('total') or 0)}</td>"
                f"<td class='num weighted'>{escape(str(row.get('weighted_loading') or ''))}</td>"
                + "".join(
                    f"<td class='num'>{int(category_counts.get(cat, 0))}</td>"
                    for cat in combined_trial_categories
                )
                + "</tr>"
            )

        table_html = (
            "<div class='card'>"
            "<h3>Current Reporter Loading Table</h3>"
            "<div class='subtle'>Daily snapshot including weighted loading and active issue category distribution.</div>"
            "<table class='tbl'>"
            f"{header_html}"
            + "".join(body_rows)
            + "</table>"
            "</div>"
        )
    else:
        table_html = (
            "<div class='card'>"
            "<h3>Current Reporter Loading Summary</h3>"
            f"<pre>{escape(_format_counts(list(decision_counts), limit=len(decision_counts)))}</pre>"
            "</div>"
        )

    def _render_text_table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
        if not headers:
            return ""
        col_widths = [len(str(h)) for h in headers]
        for row in rows:
            for idx, cell in enumerate(row):
                if idx < len(col_widths):
                    col_widths[idx] = max(col_widths[idx], len(str(cell)))

        def _fmt_row(values: Sequence[str]) -> str:
            return "  ".join(str(v).ljust(col_widths[i]) for i, v in enumerate(values))

        header_line = _fmt_row(headers)
        sep_line = "  ".join("-" * w for w in col_widths)
        body_lines = [_fmt_row(row) for row in rows]
        return "\n".join([header_line, sep_line] + body_lines)

    definition_html = ""
    if category_weight_defs or category_tech_weight_defs:
        cat_rows_text: List[Tuple[str, str]] = [
            (str(cat), f"{weight:.2f}")
            for cat, weight in category_weight_defs
        ]
        if not cat_rows_text:
            cat_rows_text = [("(none)", "")]

        tech_rows_text: List[Tuple[str, str, str]] = [
            (str(cat), str(tech), f"{weight:.2f}")
            for cat, tech, weight in category_tech_weight_defs
        ]
        if not tech_rows_text:
            tech_rows_text = [("(none)", "", "")]

        cat_table_text = _render_text_table(
            headers=["Category", "Weight"],
            rows=cat_rows_text,
        )
        tech_table_text = _render_text_table(
            headers=["Category", "Technology", "Weight"],
            rows=tech_rows_text,
        )

        definition_html = (
            "<div class='card'>"
            "<h3>Weighting Table Definition</h3>"
            f"<p class='subtle'>Source: {escape(weight_map_path or '(not set)')} | default_weight = <b>{default_weight:.2f}</b></p>"
            "<div class='grid'>"
            "<div>"
            "<h4>Category Weights</h4>"
            f"<pre class='mono'>{escape(cat_table_text)}</pre>"
            "</div>"
            "<div>"
            "<h4>Category + Technology Weights</h4>"
            f"<pre class='mono'>{escape(tech_table_text)}</pre>"
            "</div>"
            "</div>"
            "</div>"
        )

    weighting_flow_html = (
        "<details class='flow-panel'>"
        "<summary>Show weighting decision flow</summary>"
        "<ol class='flow-list'>"
        "<li><b>Collect current load:</b> calculate each reporter's raw <b>Curr</b> issue count.</li>"
        "<li><b>Classify issues:</b> use the category model on unpromoted IPS issues.</li>"
        "<li><b>Resolve weight:</b> apply category + technology weight first, then category weight, then the default weight.</li>"
        "<li><b>Calculate weighted loading:</b> use weighted active IPS + weighted Jira + active HSD, where active HSD = uHSD - stale HSD.</li>"
        "<li><b>Make the offload decision:</b> use raw <b>Curr</b> for the threshold and receiver selection; weighted loading does not change the current decision.</li>"
        "</ol>"
        "</details>"
    )

    return (
        "<html><head>"
        "<style>"
        "body{font-family:Segoe UI,Arial,sans-serif;background:#f5f7fb;color:#1f2937;margin:0;padding:0;}"
        ".wrap{max-width:1400px;margin:0 auto;padding:20px;}"
        ".hero{background:linear-gradient(120deg,#1f4e79,#2b7a78);color:#fff;border-radius:12px;padding:18px 20px;margin-bottom:14px;}"
        ".hero h2{margin:0 0 6px 0;font-size:22px;}"
        ".hero p{margin:0;opacity:.95;}"
        ".flow-panel{margin-top:14px;background:rgba(255,255,255,.12);border:1px solid rgba(255,255,255,.35);border-radius:8px;padding:10px 12px;}"
        ".flow-panel summary{cursor:pointer;font-weight:700;color:#fff;}"
        ".flow-list{margin:10px 0 2px 20px;padding:0;color:#f5fbff;line-height:1.55;}"
        ".card{background:#fff;border:1px solid #d8e1ea;border-radius:12px;padding:14px 16px;margin:12px 0;}"
        ".card h3{margin:0 0 6px 0;font-size:17px;color:#0f2940;}"
        ".card h4{margin:8px 0 6px 0;font-size:14px;color:#274c77;}"
        ".subtle{color:#5f6b7a;font-size:12px;}"
        ".tbl{width:100%;border-collapse:collapse;margin-top:8px;font-size:12px;}"
        ".tbl th{background:#0f2940;color:#ffffff;padding:8px;border:1px solid #d8e1ea;text-align:left;position:sticky;top:0;}"
        ".tbl td{padding:7px;border:1px solid #d8e1ea;}"
        ".tbl tr:nth-child(even) td{background:#f8fbff;}"
        ".tbl .num{text-align:right;font-variant-numeric:tabular-nums;}"
        ".tbl .weighted{font-weight:700;color:#1f4e79;}"
        ".compact td,.compact th{padding:6px;}"
        ".mono{margin:8px 0 0 0;padding:10px;background:#f7fafc;border:1px solid #d8e1ea;border-radius:8px;font-family:Consolas,Menlo,monospace;font-size:12px;line-height:1.35;white-space:pre;overflow-x:auto;}"
        ".grid{display:grid;grid-template-columns:1fr 1.3fr;gap:14px;}"
        "@media(max-width:960px){.grid{grid-template-columns:1fr;}}"
        "</style>"
        "</head><body><div class='wrap'>"
        "<div class='hero'><h2>IPS Daily Loading Summary</h2>"
        "<p>Daily load snapshot for team balancing. This email is summary-only and does not include offload recommendation actions.</p>"
        f"<p>Most updated team loading data: <a href='{DASHBOARD_URL}'>{DASHBOARD_URL}</a></p>"
        f"{weighting_flow_html}</div>"
        f"{table_html}"
        f"{definition_html}"
        "<div class='card'><p class='subtle'><i>Weighted Loading is for trial visibility and does not change offload decision logic.</i></p></div>"
        f"{history_html}"
        "</div></body></html>"
    )


def _save_loading_snapshot(
    path: str,
    *,
    categories: Sequence[str],
    rows: Sequence[Dict[str, Any]],
) -> None:
    snapshot_path = str(path or "team_loading_latest.json").strip()
    if not snapshot_path:
        return
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "categories": [str(category) for category in categories],
        "rows": [dict(row) for row in rows],
    }

    def _write_json(target_path: str) -> None:
        temporary_path = f"{target_path}.tmp"
        with open(temporary_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=True, indent=2)
        os.replace(temporary_path, target_path)

    try:
        _write_json(snapshot_path)
        history_dir = _env_str(
            "OFFLOAD_LOADING_HISTORY_DIR",
            os.path.join(os.path.dirname(snapshot_path), "loading_history"),
        ).strip()
        if history_dir:
            os.makedirs(history_dir, exist_ok=True)
            history_path = os.path.join(
                history_dir,
                datetime.now().strftime("%Y-%m-%d") + ".json",
            )
            _write_json(history_path)
            cutoff = datetime.now().date().toordinal() - 29
            for filename in os.listdir(history_dir):
                if not filename.endswith(".json"):
                    continue
                try:
                    snapshot_date = datetime.strptime(filename[:-5], "%Y-%m-%d").date()
                except ValueError:
                    continue
                if snapshot_date.toordinal() < cutoff:
                    try:
                        os.remove(os.path.join(history_dir, filename))
                    except OSError:
                        pass
    except Exception as exc:
        LOG.warning("Unable to write loading snapshot %s: %s", snapshot_path, exc)
        try:
            temporary_path = f"{snapshot_path}.tmp"
            if os.path.exists(temporary_path):
                os.remove(temporary_path)
        except OSError:
            pass


def _append_history_entry(
    history: Dict[str, Any],
    *,
    table: str,
    issue: Dict[str, Any],
    overloaded_reporter: str,
    receiving_reporter: str,
    overloaded_count: float,
    receiving_count: float,
    trigger_reporter: str = "",
) -> None:
    entries = history.setdefault("entries", [])
    entries.append(
        {
            "created_at": _history_now_iso(),
            "status": "pending",
            "table": table,
            "ips_case_number": _case_key(issue.get("ips_case_number")),
            "issue_title": str(issue.get("title") or ""),
            "issue_created_date": str(issue.get("created_date") or ""),
            "overloaded_reporter": overloaded_reporter,
            "trigger_reporter": trigger_reporter,
            "receiving_reporter": receiving_reporter,
            "overloaded_count_at_offload": round(float(overloaded_count), 2),
            "receiving_count_at_offload": round(float(receiving_count), 2),
        }
    )


def _build_email_body(
    *,
    threshold: int,
    overloaded: List[Dict[str, Any]],
    least_loaded: Dict[str, Any],
    issue: Dict[str, Any],
) -> str:
    issue_case = issue.get("ips_case_number") or "(unknown)"
    issue_title = issue.get("title") or "(no title)"
    issue_reporter = issue.get("reporter") or "(unassigned)"
    created_date = issue.get("created_date") or "(unknown)"

    least_reporter = least_loaded.get("reporter") or "(unassigned)"
    least_total = float(
        least_loaded.get("effective_current_issue_count")
        if least_loaded.get("effective_current_issue_count") is not None
        else least_loaded.get("total_current_issue_count")
        or 0.0
    )

    overloaded_lines = "\n".join(
        f"- {row.get('reporter')}: "
        f"{float((row.get('effective_current_issue_count') if row.get('effective_current_issue_count') is not None else row.get('total_current_issue_count')) or 0.0):.2f} current issues"
        for row in overloaded
    )

    return (
        "IPS Offload Notification\n\n"
        "Source queue reporter load:\n"
        f"{overloaded_lines}\n\n"
        "Most recently created source issue in queue (IPS/HSD):\n"
        f"- Case: {issue_case}\n"
        f"- Title: {issue_title}\n"
        f"- Created: {created_date}\n"
        f"- Current reporter: {issue_reporter}\n\n"
        "Recommended reassignment:\n"
        f"- Assign to lowest-count reporter: {least_reporter} ({least_total:.2f} current issues)\n"
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Offload IPS cases based on reporter load.")
    parser.add_argument("--table", default=_env_str("DB_TABLE", "ips_jira_bugs"))
    parser.add_argument("--history-file", default=_env_str("OFFLOAD_HISTORY_FILE", "offload_reassignment_history.json"))
    parser.add_argument("--threshold", type=int, default=15)
    parser.add_argument(
        "--bt-threshold",
        type=int,
        default=_env_int("OFFLOAD_BT_THRESHOLD", 10),
        help="Source threshold for BT engineers (WiFi threshold uses --threshold).",
    )
    parser.add_argument(
        "--receiver-max-issues",
        type=int,
        default=_env_int("OFFLOAD_RECEIVER_MAX_ISSUES", 9),
        help="Skip receiving engineer candidates whose current issue count is greater than this value.",
    )
    parser.add_argument("--recipients", default=_env_str("OFFLOAD_RECIPIENTS", DEFAULT_RECIPIENTS_PATH))
    parser.add_argument("--subject", default="IPS Offload Recommendation")
    parser.add_argument(
        "--shared-csv-path",
        default=_env_str("OFFLOAD_SHARED_CSV_PATH", ""),
        help="Optional CSV path (local synced SharePoint/OneDrive path) to append run notifications.",
    )
    parser.add_argument(
        "--shared-file-url",
        default=_env_str("OFFLOAD_SHARED_FILE_URL", ""),
        help="Optional shared file URL metadata recorded in shared CSV logs.",
    )
    parser.add_argument(
        "--teams-webhook-url",
        default=_env_str("OFFLOAD_TEAMS_WEBHOOK_URL", ""),
        help="Optional Microsoft Teams Incoming Webhook URL for additional notifications.",
    )
    parser.add_argument(
        "--blocked-subject",
        default=_env_str("OFFLOAD_BLOCKED_SUBJECT", "IPS Offload Blocked: No Eligible Receiver"),
        help="Subject used when no eligible receiving engineer is available.",
    )
    parser.add_argument(
        "--trigger-subject",
        default=_env_str("OFFLOAD_TRIGGER_SUBJECT", "IPS Offload Action Required (Move to Jonathan Queue)"),
        help="Subject used for stage-1 trigger notifications to over-threshold engineers.",
    )
    parser.add_argument(
        "--summary-subject",
        default=_env_str("OFFLOAD_SUMMARY_SUBJECT", "IPS Daily Loading Summary"),
        help="Subject used when --summary-only-email is enabled.",
    )
    parser.add_argument("--reminder-subject", default=_env_str("OFFLOAD_REMINDER_SUBJECT", "IPS Offload Pending Reminder"))
    parser.add_argument("--stale-days", type=int, default=int(_env_str("STALE_DAYS", "90")))
    parser.add_argument("--debug-reporter", default="")
    parser.add_argument("--debug-case", default="")
    parser.add_argument("--debug-jira", default="")
    parser.add_argument("--debug-jira-raw", action="store_true")
    parser.add_argument("--debug-case-raw", action="store_true")
    parser.add_argument("--no-email", action="store_true")
    parser.add_argument("--send-email", action="store_true")
    parser.add_argument("--send-even-if-none", action="store_true")
    parser.add_argument(
        "--trigger-cooldown-minutes",
        type=int,
        default=_env_int("OFFLOAD_TRIGGER_COOLDOWN_MINUTES", 240),
        help="Minimum minutes before re-sending the same stage-1 trigger to the same engineer.",
    )
    parser.add_argument(
        "--summary-only-email",
        action="store_true",
        help="Send only current loading summary table email (no recommendation/reminder).",
    )
    parser.add_argument("--enable-category-weighting", action="store_true")
    parser.add_argument(
        "--disable-weighted-trial-display",
        action="store_true",
        help="Disable weighted loading trial column in logs/email table.",
    )
    parser.add_argument(
        "--category-model",
        default=_env_str("OFFLOAD_CATEGORY_MODEL", os.path.join("models", "issue_category_model.joblib")),
        help="Path to trained issue category model artifact (joblib)",
    )
    parser.add_argument(
        "--category-weight-map",
        default=_env_str("OFFLOAD_CATEGORY_WEIGHT_MAP", "issue_category_weights.json"),
        help="Path to category->weight mapping JSON",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--log-level", default="INFO")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    _setup_logging(args.log_level)
    teams_webhook_url = str(args.teams_webhook_url or "").strip()
    shared_csv_path = str(args.shared_csv_path or "").strip()
    shared_file_url = str(args.shared_file_url or "").strip()

    table = (args.table or "").strip()
    _validate_table_name(table)

    db = DbConnector()
    counts = _get_reporter_current_counts(db, table, args.stale_days)
    if not counts:
        LOG.info("No reporter data found in %s", table)
        _append_shared_csv_log(
            shared_csv_path,
            table=table,
            event_type="run_no_data",
            status="skipped",
            reason="No reporter data found.",
            shared_file_url=shared_file_url,
        )
        return 0

    history_file = str(args.history_file or "offload_reassignment_history.json").strip()
    history = _load_history(history_file)

    active_history, history_changed = _refresh_history_statuses(db, table, history)
    if history_changed:
        _save_history(history_file, history)

    adjusted_counts_all = _apply_pending_adjustments(counts, active_history)
    history_entries = _history_entries_for_table(history, table)
    pending_entries = _pending_entries_for_table(history, table)
    history_html = _render_history_html(history_entries)
    if active_history:
        LOG.info(
            "Applying %d pending offload(s) from history to avoid duplicate recommendations.",
            len(active_history),
        )

    breakdown = _get_reporter_current_breakdown(
        db,
        table,
        args.stale_days,
        filter_allowed=True,
    )
    weighted_summary: Dict[str, Dict[str, float]] = {}
    weighted_category_rows: List[Dict[str, Any]] = []
    combined_trial_categories: List[str] = []
    combined_trial_rows: List[Dict[str, Any]] = []
    weighted_category_order: List[str] = []
    if breakdown:
        LOG.info("Current issue calculation breakdown per reporter:")
        if args.enable_category_weighting or not args.disable_weighted_trial_display:
            weighted_summary, weighted_category_rows = _build_weighted_summary(
                db,
                table,
                breakdown,
                model_path=str(args.category_model or "").strip(),
                weight_map_path=str(args.category_weight_map or "").strip(),
            )
            if weighted_summary and args.enable_category_weighting:
                LOG.info(
                    "Category weighting enabled with model=%s and weight_map=%s",
                    args.category_model,
                    args.category_weight_map,
                )
            elif weighted_summary and not args.enable_category_weighting:
                LOG.info("Weighted loading trial enabled for visibility; offload decision remains based on Curr.")
            weighted_category_order = _load_weight_category_display_order(str(args.category_weight_map or "").strip())
        combined_trial_categories, combined_trial_rows = _build_combined_trial_table_data(
            breakdown,
            weighted_summary,
            weighted_category_rows,
            preferred_categories=weighted_category_order,
        )
        LOG.info("Combined trial table (single table with category columns, decision still uses Curr):")
        for line in _format_combined_trial_table(combined_trial_categories, combined_trial_rows):
            LOG.info(line)

    _save_loading_snapshot(
        _env_str("OFFLOAD_LOADING_SNAPSHOT_FILE", "team_loading_latest.json"),
        categories=combined_trial_categories,
        rows=combined_trial_rows,
    )

    decision_counts_all = _attach_effective_counts(
        adjusted_counts_all,
        weighted_summary,
        use_weighted_for_decision=False,
    )
    decision_counts = [
        row
        for row in decision_counts_all
        if not _is_always_excluded_reporter(str(row.get("reporter") or ""))
    ]
    decision_mode = "Curr"
    LOG.info("Offload decision mode: %s", decision_mode)

    # Summary-only mode must be read-only: do not mutate offload trigger/recommendation state.
    if args.summary_only_email:
        summary_body = _build_loading_summary_email_html(
            combined_trial_categories=combined_trial_categories,
            combined_trial_rows=combined_trial_rows,
            decision_counts=decision_counts,
            history_html=history_html,
            weight_map_path=str(args.category_weight_map or "").strip(),
        )
        if args.dry_run:
            LOG.info(
                "Dry run enabled; summary email not sent. Subject=%s\n\n%s",
                args.summary_subject,
                summary_body,
            )
            _append_shared_csv_log(
                shared_csv_path,
                table=table,
                event_type="summary_dry_run",
                subject=str(args.summary_subject),
                status="dry_run",
                reason="Summary-only dry run; email not sent.",
                recommendation_recipient_count=len(to_list) if 'to_list' in locals() else 0,
                shared_file_url=shared_file_url,
            )
            return 0

        if not args.send_email or args.no_email:
            LOG.info("Summary-only mode: email sending disabled by flags.")
            _append_shared_csv_log(
                shared_csv_path,
                table=table,
                event_type="summary_skipped",
                subject=str(args.summary_subject),
                status="skipped",
                reason="Summary-only mode with email disabled by flags.",
                shared_file_url=shared_file_url,
            )
            return 0

        to_list, cc_list = load_recipients(args.recipients)
        if not to_list:
            raise SystemExit("Recipient list is empty; update recipients.json or DEFAULT_TO in .env.")

        secret = resolve_graph_client_secret()
        graph_auth_mode = _env_str("GRAPH_AUTH_MODE", "app").lower()
        graph_sender_upn = _env_str("GRAPH_SENDER_UPN", "")

        if graph_auth_mode == "delegated":
            LOG.info("Graph auth mode: delegated")
            token = get_graph_token_delegated_with_secret(secret)
            send_mail_via_graph(token, args.summary_subject, summary_body, to_list, cc_list, content_type="HTML")
        else:
            LOG.info("Graph auth mode: app")
            if not graph_sender_upn:
                raise SystemExit("GRAPH_SENDER_UPN is required when GRAPH_AUTH_MODE=app.")
            token = get_graph_token_app_only(secret)
            send_mail_via_graph(
                token,
                args.summary_subject,
                summary_body,
                to_list,
                cc_list,
                content_type="HTML",
                sender_upn=graph_sender_upn,
            )

        LOG.info("Summary-only loading email sent to %d recipient(s).", len(to_list))
        _append_shared_csv_log(
            shared_csv_path,
            table=table,
            event_type="summary_sent",
            subject=str(args.summary_subject),
            status="sent",
            reason="Summary-only email sent.",
            recommendation_recipient_count=len(to_list),
            shared_file_url=shared_file_url,
        )
        if teams_webhook_url:
            _send_teams_webhook(
                teams_webhook_url,
                "IPS Offload Summary Sent",
                f"Subject: {args.summary_subject}\nRecipients: {len(to_list)}\nDashboard: {DASHBOARD_URL}",
            )
        return 0

    current_count_map = _build_current_count_map(decision_counts_all)
    over_threshold_reporters = _build_over_threshold_reporters(
        decision_counts,
        wifi_threshold=int(args.threshold),
        bt_threshold=int(args.bt_threshold),
    )

    trigger_entries = _open_trigger_entries_for_table(history, table)
    trigger_entries_changed = _close_trigger_entries_when_recovered(
        trigger_entries,
        current_count_map=current_count_map,
        wifi_threshold=int(args.threshold),
        bt_threshold=int(args.bt_threshold),
    )
    if trigger_entries_changed:
        history_changed = True

    latest_trigger_by_reporter: Dict[str, Dict[str, Any]] = {}
    for entry in trigger_entries:
        reporter_norm = _normalize_reporter(str(entry.get("trigger_reporter") or ""))
        if not reporter_norm:
            continue
        existing = latest_trigger_by_reporter.get(reporter_norm)
        if existing is None or str(entry.get("created_at") or "") > str(existing.get("created_at") or ""):
            latest_trigger_by_reporter[reporter_norm] = entry

    trigger_notifications: List[Dict[str, Any]] = []
    new_trigger_reporters: set[str] = set()
    first_time_trigger_reporters: set[str] = set()  # only reporters with NO prior trigger entry
    new_trigger_sent = False
    for row in over_threshold_reporters:
        reporter = str(row.get("reporter") or "").strip()
        reporter_norm = str(row.get("reporter_norm") or "").strip()
        if not reporter or not reporter_norm:
            continue
        existing = latest_trigger_by_reporter.get(reporter_norm)
        if existing and _is_recent_trigger_entry(existing, int(args.trigger_cooldown_minutes)):
            continue

        history.setdefault("entries", []).append(
            {
                "created_at": _history_now_iso(),
                "status": "awaiting_source_transfer",
                "table": table,
                "trigger_reporter": reporter,
                "trigger_threshold": int(row.get("threshold") or 0),
                "trigger_count": round(float(row.get("current") or 0.0), 2),
                "trigger_groups": sorted(list(row.get("groups") or [])),
            }
        )
        history_changed = True
        new_trigger_sent = True
        new_trigger_reporters.add(reporter_norm)
        # Only a truly first-time trigger (no prior entry) defers stage-2.
        # Repeat reminders (cooldown expired on existing entry) should not block stage-2.
        if existing is None:
            first_time_trigger_reporters.add(reporter_norm)
        trigger_notifications.append(
            {
                "reporter": reporter,
                "reporter_norm": reporter_norm,
                "current": float(row.get("current") or 0.0),
                "threshold": int(row.get("threshold") or 0),
            }
        )

    if history_changed:
        _save_history(history_file, history)

    # Auto-cancel any pending entries whose IPS case is already closed in the DB.
    auto_cancelled = _auto_cancel_closed_case_pending_entries(db, table, history)
    if auto_cancelled:
        LOG.info("Auto-cancelled %d pending offload entry/entries with closed IPS cases.", auto_cancelled)
        _save_history(history_file, history)

    history_entries = _history_entries_for_table(history, table)
    pending_entries = _pending_entries_for_table(history, table)
    history_html = _render_history_html(history_entries)

    if active_history:
        LOG.info(
            "Applying %d pending offload(s) from history to avoid duplicate recommendations.",
            len(active_history),
        )

    # Keep reminder gating consistent with recommendation detection by using
    # post-pending adjusted effective counts (weighted when enabled).
    pending_entries_for_reminder = _filter_pending_entries_still_need_offload(
        pending_entries,
        current_count_map,
        int(args.threshold),
        int(args.bt_threshold),
        int(args.receiver_max_issues),
    )

    if args.debug_reporter:
        _debug_reporter_issue_lists(db, table, args.debug_reporter)
        _debug_unpromoted_hsd_ids(db, table, args.debug_reporter)

    if args.debug_case:
        _debug_case_details(db, table, args.debug_case, include_raw=args.debug_case_raw)
    if args.debug_jira:
        _debug_jira_details(db, table, args.debug_jira, include_raw=args.debug_jira_raw)

    queue_reporter_rows = [
        row
        for row in decision_counts_all
        if _normalize_reporter(str(row.get("reporter") or "")) in OFFLOAD_QUEUE_SOURCE_REPORTERS
    ]

    body = None
    reminder_body = None
    trigger_bodies: List[Dict[str, Any]] = []
    reminder_to_list: List[str] = []
    recommendation_subject = str(args.subject)
    issue = None
    least_loaded = None
    trigger_reporter_for_case = ""
    selected_source_reporter = ""
    selected_source_count = 0.0

    for row in trigger_notifications:
        trigger_bodies.append(
            {
                "reporter": str(row.get("reporter") or ""),
                "reporter_norm": str(row.get("reporter_norm") or ""),
                "body": _build_trigger_notification_body(
                    str(row.get("reporter") or ""),
                    float(row.get("current") or 0.0),
                    int(row.get("threshold") or 0),
                ),
            }
        )

    if trigger_bodies:
        LOG.info("Created %d trigger notification(s) for over-threshold reporters.", len(trigger_bodies))

    # Stage-2 recommendation selects the first overloaded engineer who did not
    # get a fresh stage-1 trigger in this run (prevents global starvation).
    stage2_overloaded: Optional[Dict[str, Any]] = None
    for row in over_threshold_reporters:
        reporter_norm = str(row.get("reporter_norm") or "").strip()
        if reporter_norm in first_time_trigger_reporters:
            continue
        stage2_overloaded = row
        break

    can_run_stage2 = stage2_overloaded is not None

    if decision_counts and can_run_stage2:
        primary_overloaded = stage2_overloaded or {}
        trigger_reporter_for_case = str(primary_overloaded.get("reporter") or "").strip()
        source_groups = set(primary_overloaded.get("groups") or set())

        known_cases = _known_history_case_numbers(history, table, statuses=["pending", "realized"])
        issue = _get_most_recent_overloaded_issue(
            db,
            table,
            list(OFFLOAD_QUEUE_SOURCE_REPORTERS),
            exclude_case_numbers=known_cases,
        )
        # Guard: double-check the selected case is actually open in the DB.
        # The selection query uses an OR with hsd_status_reason that can let
        # closed IPS cases through when no HSD is linked (hsd_status_reason IS NULL).
        if issue:
            case_num = str(issue.get("ips_case_number") or "").strip()
            cancelled_count = _auto_cancel_closed_case_pending_entries(db, table, {"entries": []})
            # Run a targeted check for this specific case number
            closed_check: set = set()
            if case_num.isdigit():
                columns_chk = _get_table_columns(db, table)
                chk_parts = ["ips_case_number::text AS ips_case_number"]
                if _has(columns_chk, "ips_status"):
                    chk_parts.append("LOWER(TRIM(COALESCE(ips_status::text,''))) AS ips_status")
                if _has(columns_chk, "ips_sub_status"):
                    chk_parts.append("LOWER(TRIM(COALESCE(ips_sub_status::text,''))) AS ips_sub_status")
                if _has(columns_chk, "jira_status"):
                    chk_parts.append("LOWER(TRIM(COALESCE(jira_status::text,''))) AS jira_status")
                try:
                    chk_rows = db.query_rows(
                        f"SELECT {', '.join(chk_parts)} FROM {table} WHERE ips_case_number::text = %s LIMIT 1",
                        [case_num],
                    )
                    for chk_row in chk_rows:
                        if (
                            chk_row.get("ips_status") == "closed"
                            or chk_row.get("ips_sub_status") == "closed"
                            or chk_row.get("jira_status") in ("closed", "implemented", "verify")
                        ):
                            closed_check.add(case_num)
                except Exception as _exc:
                    LOG.warning("Pre-recommendation status check failed for case %s: %s", case_num, _exc)
            if case_num in closed_check:
                LOG.warning(
                    "Skipping recommendation for case %s: IPS case is already closed.", case_num
                )
                issue = None
        if not issue:
            LOG.warning("No new IPS case found in Jonathan queue after history filtering.")
            if args.send_even_if_none:
                body = (
                    "IPS Offload Notification\n\n"
                    "No new IPS case available for recommendation from Jonathan queue after history filtering.\n\n"
                    "Current load:\n"
                    f"{_format_counts(decision_counts, limit=len(decision_counts))}\n"
                )

        if issue:
            selected_source_reporter = str(issue.get("reporter") or "").strip()
            # Determine target group from the issue's technology, not from the
            # overloaded reporter's group — prevents WiFi bugs going to BT and v.v.
            issue_tech = str(issue.get("technology") or "").strip().lower()
            if issue_tech in ("wifi",):
                issue_groups: set = {"wifi"}
            elif issue_tech in ("bt", "bluetooth"):
                issue_groups = {"bt"}
            else:
                # Unknown technology: fall back to overloaded reporter's group
                issue_groups = source_groups
                LOG.info(
                    "Issue technology unknown ('%s') for case %s; using overloaded reporter group %s as fallback.",
                    issue_tech,
                    issue.get("ips_case_number"),
                    source_groups,
                )
            LOG.info(
                "Issue technology='%s' -> routing to group(s): %s",
                issue_tech, issue_groups,
            )
            receiver_candidates = [
                row
                for row in decision_counts
                if _normalize_reporter(str(row.get("reporter") or ""))
                not in {
                    _normalize_reporter(selected_source_reporter),
                    _normalize_reporter(trigger_reporter_for_case),
                }
                and bool(_groups_for_reporter(str(row.get("reporter") or "")) & issue_groups)
                and float(row.get("effective_current_issue_count") or 0.0) <= float(args.receiver_max_issues)
            ]
            if not receiver_candidates:
                LOG.warning(
                    "No receiving engineer candidate found in the same group after exclusions and receiver max issues cap (%d).",
                    args.receiver_max_issues,
                )
                recommendation_subject = str(args.blocked_subject)
                body = (
                    "IPS Offload Notification\n\n"
                    "No eligible receiving engineer is currently available for offload.\n\n"
                    f"- Triggered engineer: {trigger_reporter_for_case or '(unknown)'}\n"
                    f"- Receiver max issues cap: {int(args.receiver_max_issues)}\n"
                    "- Reason: all same-group candidates are excluded or above cap.\n"
                )
                issue = None
                least_loaded = None
                trigger_reporter_for_case = ""
                selected_source_reporter = ""
                selected_source_count = 0.0
            else:
                sorted_candidates = sorted(
                    receiver_candidates,
                    key=lambda r: (
                        float(r.get("effective_current_issue_count") or 0.0),
                        str(r.get("reporter") or ""),
                    ),
                )
                least_loaded = sorted_candidates[0]
                LOG.info(
                    "Receiver rule: selecting lowest-count candidate without rotation. mode=%s source=%s receiver=%s receiver_effective_count=%.2f",
                    decision_mode,
                    selected_source_reporter,
                    least_loaded.get("reporter"),
                    float(least_loaded.get("effective_current_issue_count") or 0.0),
                )

                selected_source_count = float(
                    next(
                        (
                            float(row.get("effective_current_issue_count") or 0.0)
                            for row in decision_counts_all
                            if _normalize_reporter(str(row.get("reporter") or ""))
                            == _normalize_reporter(selected_source_reporter)
                        ),
                        0.0,
                    )
                )

                current_recommendation = {
                    "created_at": _history_now_iso(),
                    "status": "pending",
                    "table": table,
                    "ips_case_number": _case_key(issue.get("ips_case_number")),
                    "issue_title": str(issue.get("title") or ""),
                    "issue_created_date": str(issue.get("created_date") or ""),
                    "overloaded_reporter": selected_source_reporter,
                    "trigger_reporter": trigger_reporter_for_case,
                    "receiving_reporter": str(least_loaded.get("reporter") or ""),
                    "overloaded_count_at_offload": round(selected_source_count, 2),
                    "receiving_count_at_offload": round(float(least_loaded.get("effective_current_issue_count") or 0.0), 2),
                }
                history_html = _render_history_html([current_recommendation] + history_entries)

                body = _build_email_body(
                    threshold=int(primary_overloaded.get("threshold") or 0),
                    overloaded=queue_reporter_rows,
                    least_loaded=least_loaded,
                    issue=issue,
                )
    else:
        if not over_threshold_reporters:
            LOG.info("No over-threshold reporters found; stage-2 offload recommendation skipped.")
        elif new_trigger_sent and stage2_overloaded is None and first_time_trigger_reporters:
            LOG.info("All current overloaded reporters received their first-ever trigger this run; stage-2 deferred to next run.")
        else:
            LOG.info("No eligible receiver counts available for offload recommendation.")
        if args.send_even_if_none:
            body = (
                "IPS Offload Notification\n\n"
                "No eligible receiver counts available for offload recommendation.\n\n"
                "Current load:\n"
                f"{_format_counts(decision_counts, limit=len(decision_counts))}\n"
            )

    if not args.send_email:
        LOG.info("Email notification disabled by default; pass --send-email to send.")
        _append_shared_csv_log(
            shared_csv_path,
            table=table,
            event_type="run_no_send",
            status="skipped",
            reason="Email disabled by default (--send-email not set).",
            trigger_count=len(trigger_bodies),
            reminder_recipient_count=len(reminder_to_list),
            shared_file_url=shared_file_url,
        )
        return 0

    if args.no_email:
        LOG.info("Email notification explicitly disabled (--no-email).")
        _append_shared_csv_log(
            shared_csv_path,
            table=table,
            event_type="run_no_send",
            status="skipped",
            reason="Email explicitly disabled (--no-email).",
            trigger_count=len(trigger_bodies),
            reminder_recipient_count=len(reminder_to_list),
            shared_file_url=shared_file_url,
        )
        return 0

    to_list, cc_list = load_recipients(args.recipients)
    if not to_list:
        raise SystemExit("Recipient list is empty; update recipients.json or DEFAULT_TO in .env.")

    reporter_email_map = _load_reporter_email_map(args.recipients)
    trigger_email_jobs: List[Dict[str, Any]] = []
    for item in trigger_bodies:
        reporter_norm = str(item.get("reporter_norm") or "").strip()
        direct_email = reporter_email_map.get(reporter_norm, "").strip()
        resolved_to = [direct_email] if direct_email else list(to_list)
        trigger_email_jobs.append(
            {
                "reporter": str(item.get("reporter") or ""),
                "to": resolved_to,
                "body": str(item.get("body") or ""),
            }
        )

    if trigger_email_jobs:
        unresolved = [j for j in trigger_email_jobs if len(j.get("to") or []) != 1]
        if unresolved:
            LOG.warning(
                "Some trigger notifications have no direct reporter_email_map match; fallback to default TO list. count=%d",
                len(unresolved),
            )

    if pending_entries_for_reminder:
        reminder_to_list = _collect_pending_reminder_recipients(
            pending_entries_for_reminder,
            reporter_email_map,
            to_list,
        )
        if reminder_to_list:
            reminder_body = _build_pending_reminder_html(pending_entries_for_reminder)
            LOG.info(
                "Pending offload reminders: %d pending entrie(s) still need offload, %d recipient(s).",
                len(pending_entries_for_reminder),
                len(reminder_to_list),
            )
        else:
            LOG.warning("Pending offload reminders skipped: no recipients resolved.")
    elif pending_entries:
        LOG.info(
            "Pending offload reminders skipped: %d pending entrie(s) no longer require offload based on current load.",
            len(pending_entries),
        )

    dashboard_text = f"Most updated team loading data: {DASHBOARD_URL}"
    for job in trigger_email_jobs:
        job["body"] = f"{str(job.get('body') or '').rstrip()}\n\n{dashboard_text}\n"
    if reminder_body:
        reminder_body = reminder_body.replace(
            "</body>",
            f"<p>{dashboard_text}: <a href='{DASHBOARD_URL}'>{DASHBOARD_URL}</a></p></body>",
        )
    if body:
        body = f"{body.rstrip()}\n\n{dashboard_text}\n"

    if issue and least_loaded:
        LOG.info("Source queue reporter summary:\n%s", _format_counts(queue_reporter_rows))
        LOG.info(
            "Receiving engineer with least issues: %s (effective %.2f)",
            least_loaded.get("reporter"),
            float(least_loaded.get("effective_current_issue_count") or 0.0),
        )
        LOG.info("Most recent source issue (IPS/HSD): %s", issue.get("ips_case_number"))
    else:
        LOG.info("Sending informational notification: no queue case available for recommendation.")
        LOG.info("Current load:\n%s", _format_counts(decision_counts, limit=len(decision_counts)))

    if args.dry_run:
        for job in trigger_email_jobs:
            LOG.info(
                "Dry run enabled; trigger email not sent. Subject=%s To=%s\n\n%s",
                args.trigger_subject,
                ", ".join(job.get("to") or []),
                str(job.get("body") or ""),
            )
        if reminder_body:
            LOG.info(
                "Dry run enabled; reminder email not sent. Subject=%s To=%s\n\n%s",
                args.reminder_subject,
                ", ".join(reminder_to_list),
                reminder_body,
            )
        if body:
            LOG.info("Dry run enabled; recommendation email not sent.\n\n%s", body)
        if not reminder_body and not body:
            LOG.info("Dry run enabled; no email content generated.")
        _append_shared_csv_log(
            shared_csv_path,
            table=table,
            event_type="run_dry_run",
            subject=str(recommendation_subject),
            case_number=_case_key(issue.get("ips_case_number")) if issue else "",
            from_owner=selected_source_reporter,
            to_owner=str(least_loaded.get("reporter") or "") if least_loaded else "",
            status="dry_run",
            reason="Dry run; no emails sent.",
            source_count=selected_source_count if selected_source_count else None,
            target_count=float(least_loaded.get("effective_current_issue_count") or 0.0) if least_loaded else None,
            trigger_count=len(trigger_email_jobs),
            reminder_recipient_count=len(reminder_to_list),
            recommendation_recipient_count=len(to_list) if body else 0,
            shared_file_url=shared_file_url,
        )
        return 0

    secret = resolve_graph_client_secret()
    graph_auth_mode = _env_str("GRAPH_AUTH_MODE", "app").lower()
    graph_sender_upn = _env_str("GRAPH_SENDER_UPN", "")

    # Add the original overloaded reporter (e.g. Steven1) to the recommendation email
    # so they are aware of the Jonathan <-> target engineer notification
    recommendation_to_list = list(to_list)
    if selected_source_reporter and body:
        source_norm = _normalize_reporter(selected_source_reporter)
        source_email = reporter_email_map.get(source_norm, "").strip()
        if source_email and source_email not in recommendation_to_list:
            recommendation_to_list.append(source_email)
            LOG.info(
                "Added source reporter '%s' (%s) to recommendation email recipients.",
                selected_source_reporter, source_email,
            )

    if graph_auth_mode == "delegated":
        LOG.info("Graph auth mode: delegated")
        token = get_graph_token_delegated_with_secret(secret)
        for job in trigger_email_jobs:
            send_mail_via_graph(
                token,
                args.trigger_subject,
                str(job.get("body") or ""),
                list(job.get("to") or []),
                cc_list,
                content_type="Text",
            )
        if reminder_body and reminder_to_list:
            send_mail_via_graph(
                token,
                args.reminder_subject,
                reminder_body,
                reminder_to_list,
                cc_list,
                content_type="HTML",
            )
        if body:
            send_mail_via_graph(token, recommendation_subject, body, recommendation_to_list, cc_list, content_type="Text")
    else:
        LOG.info("Graph auth mode: app")
        if not graph_sender_upn:
            raise SystemExit("GRAPH_SENDER_UPN is required when GRAPH_AUTH_MODE=app.")
        token = get_graph_token_app_only(secret)
        for job in trigger_email_jobs:
            send_mail_via_graph(
                token,
                args.trigger_subject,
                str(job.get("body") or ""),
                list(job.get("to") or []),
                cc_list,
                content_type="Text",
                sender_upn=graph_sender_upn,
            )
        if reminder_body and reminder_to_list:
            send_mail_via_graph(
                token,
                args.reminder_subject,
                reminder_body,
                reminder_to_list,
                cc_list,
                content_type="HTML",
                sender_upn=graph_sender_upn,
            )
        if body:
            send_mail_via_graph(
                token,
                recommendation_subject,
                body,
                recommendation_to_list,
                cc_list,
                content_type="Text",
                sender_upn=graph_sender_upn,
            )

    if issue and least_loaded:
        _append_history_entry(
            history,
            table=table,
            issue=issue,
            overloaded_reporter=selected_source_reporter,
            trigger_reporter=trigger_reporter_for_case,
            receiving_reporter=str(least_loaded.get("reporter") or ""),
            overloaded_count=selected_source_count,
            receiving_count=float(least_loaded.get("effective_current_issue_count") or 0.0),
        )
        _save_history(history_file, history)

    if trigger_email_jobs:
        LOG.info("Trigger email sent to %d target(s).", len(trigger_email_jobs))
    if reminder_body and reminder_to_list:
        LOG.info("Pending reminder email sent to %d recipient(s).", len(reminder_to_list))
    if body:
        LOG.info("Recommendation/info email sent to %d recipient(s).", len(to_list))
    if not reminder_body and not body:
        LOG.info("No email sent: no pending reminder and no recommendation/info content.")

    _append_shared_csv_log(
        shared_csv_path,
        table=table,
        event_type="run_sent",
        subject=str(recommendation_subject),
        case_number=_case_key(issue.get("ips_case_number")) if issue else "",
        from_owner=selected_source_reporter,
        to_owner=str(least_loaded.get("reporter") or "") if least_loaded else "",
        status="sent" if (trigger_email_jobs or reminder_body or body) else "no_content",
        reason=(
            "Recommendation/reminder/trigger emails sent."
            if (trigger_email_jobs or reminder_body or body)
            else "No recommendation/reminder content generated this run."
        ),
        source_count=selected_source_count if selected_source_count else None,
        target_count=float(least_loaded.get("effective_current_issue_count") or 0.0) if least_loaded else None,
        trigger_count=len(trigger_email_jobs),
        reminder_recipient_count=len(reminder_to_list),
        recommendation_recipient_count=len(recommendation_to_list) if body else 0,
        shared_file_url=shared_file_url,
    )

    if teams_webhook_url:
        teams_lines: List[str] = []
        if trigger_email_jobs:
            teams_lines.append(f"- Trigger notifications sent: {len(trigger_email_jobs)}")
        if reminder_body and reminder_to_list:
            teams_lines.append(f"- Pending reminders sent: {len(reminder_to_list)}")
        if body:
            teams_lines.append(f"- Recommendation/info subject: {recommendation_subject}")
            if issue and least_loaded:
                teams_lines.append(
                    f"- Case {_case_key(issue.get('ips_case_number'))} suggested to {least_loaded.get('reporter')}"
                )
        if not teams_lines:
            teams_lines.append("- No recommendation/reminder content generated this run.")

        _send_teams_webhook(
            teams_webhook_url,
            "IPS Offload Run",
            "\n".join(teams_lines),
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
