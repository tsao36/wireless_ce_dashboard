"""Score ML vs two LLMs against the human-labeled golden set; the dashboard serves the result on category_golden.html."""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from sklearn.base import clone
from sklearn.model_selection import GroupKFold, cross_val_predict

import issue_category_model as category_model
import loading_dashboard as dashboard

ROOT = Path(__file__).resolve().parent
DEFAULT_GOLDEN = (
    ROOT.parents[1]
    / "jira_ips_tat_HP" / "jira_tat" / "jira_customer_tat"
    / "golden_training_set_package_20260603" / "golden_training_set_20260603.csv"
)
OUTPUT_PATH = ROOT / "cache" / "category_golden_benchmark.json"
IPS_LIST_PATH = ROOT / "golden_training_set_20260603_all_ips.csv"
REVIEW_PATH = ROOT / "category_golden_reviews.csv"
REVIEW_LOCK = threading.Lock()
REVIEW_COLUMNS = (
    "row_id", "ips_case_number", "ips_case_numbers", "ips_title", "technology", "source_human_category", "human_category",
    "ml", "ml_cv", "llm1_model", "llm1_category", "llm2_model", "llm2_category",
    "has_details", "selected_category", "reviewer", "reviewed_at",
)
# Golden labels predate the current category list.
LABEL_ALIASES = {"killer": "ICPS/Killer", "icps": "ICPS/Killer", "not wifi issue": "Need-Triage", "miracast": "P2P"}
TECH_NAMES = {"wifi": "WiFi", "bt": "BT", "tools": "Tools", "software": "Software"}


def _load_golden(path: Path, categories: set[str]) -> tuple[list[dict], Counter]:
    rows, skipped = [], Counter()
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        for raw in csv.DictReader(handle):
            title = (raw.get("ips_title") or "").strip()
            technology = TECH_NAMES.get((raw.get("technology") or "").strip().lower(), (raw.get("technology") or "").strip())
            label = (raw.get("human_category") or "").strip()
            label = LABEL_ALIASES.get(label.lower(), label)
            if technology == "Software":
                skipped["Software (always ICPS/Killer by rule)"] += 1
            elif label not in categories:
                skipped[f"Label not in current list: {label}"] += 1
            else:
                rows.append({"ips_title": title, "technology": technology, "human_category": label,
                             "ips_case_number": (raw.get("ips_case_number") or "").strip(),
                             "ips_case_numbers": (raw.get("ips_case_numbers") or raw.get("ips_candidate_numbers") or raw.get("ips_case_number") or "").strip()})
    return rows, skipped


def _title_key(title: str) -> str:
    return re.sub(r"[^0-9a-z]+", "", title.lower())


def _lookup_ips_cases(titles: list[str]) -> dict[str, list[dict]]:
    wanted: dict[str, list[str]] = {}
    for title in titles:
        wanted.setdefault(_title_key(title), []).append(title)
    rows = dashboard._ips_case_query("CASE_CREATED_DTM >= '2023-01-01' AND ASSIGNED_QUEUE_SS_ONE_DSC IN ({in})", ["WCS"])
    rows.extend(dashboard._ips_case_query("SUBJECT_TXT IN ({in})", titles, 200))
    found: dict[str, dict[str, dict]] = {}
    for case_number, subject, created, description, env_details in rows:
        for title in wanted.get(_title_key(str(subject or "")), []):
            case_number = str(int(case_number))
            found.setdefault(title, {})[case_number] = {
                "ips_case_number": case_number,
                "created": str(created)[:10],
                "details": dashboard._ips_detail_text(description, env_details),
            }
    return {title: sorted(cases.values(), key=lambda case: int(case["ips_case_number"])) for title, cases in found.items()}


def _lookup_details(titles: list[str]) -> dict[str, dict]:
    return {title: cases[0] for title, cases in _lookup_ips_cases(titles).items() if len({case["ips_case_number"] for case in cases}) == 1}


def _lookup_numbered_details(rows: list[dict]) -> dict[str, dict]:
    numbers = sorted({int(number) for row in rows for number in row["ips_case_numbers"].split(";") if number})
    if not numbers:
        return {}
    cases = dashboard._ips_case_query("CASE_NBR IN ({in})", numbers)
    return {
        str(int(number)): {"title": str(subject or ""), "created": str(created)[:10],
                           "details": dashboard._ips_detail_text(description, env_details)}
        for number, subject, created, description, env_details in cases
    }


def _details_for_row(row: dict, cases: dict[str, dict]) -> tuple[str, str]:
    numbers = row["ips_case_numbers"].split(";") if row["ips_case_numbers"] else []
    matches = [cases.get(number) for number in numbers]
    if not numbers or not all(match and _title_key(match["title"]) == _title_key(row["ips_title"]) for match in matches):
        return "", ""
    if len(matches) == 1:
        return matches[0]["details"], matches[0]["created"]
    if not all(match["details"] for match in matches):
        return "", ""
    limit = dashboard.IPS_DETAIL_MAX_CHARS // len(matches)
    description = "\n\n".join(
        f"IPS {number}:\n{match['details'][:limit]}"
        for number, match in zip(numbers, matches)
    )
    return description[:dashboard.IPS_DETAIL_MAX_CHARS], ""


def export_ips_list(source: Path, destination: Path) -> Counter:
    with source.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        columns = reader.fieldnames
        if not columns or "ips_title" not in columns:
            raise ValueError("Golden CSV must contain an ips_title column.")
        rows = list(reader)
    matches = _lookup_ips_cases(sorted({(row["ips_title"] or "").strip() for row in rows if row["ips_title"]}))
    counts: Counter = Counter()
    for row in rows:
        title = (row["ips_title"] or "").strip()
        candidates = sorted({case["ips_case_number"] for case in matches.get(title, [])})
        status = "unique" if len(candidates) == 1 else "ambiguous" if candidates else "unmatched"
        row["ips_case_number"] = candidates[0] if status == "unique" else ""
        row["ips_case_numbers"] = ";".join(candidates)
        row["ips_match_status"] = status
        row["ips_candidate_numbers"] = ";".join(candidates) if status == "ambiguous" else ""
        counts[status] += 1
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp_path = destination.with_suffix(".tmp")
    with temp_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=[*columns, "ips_case_number", "ips_case_numbers", "ips_match_status", "ips_candidate_numbers"])
        writer.writeheader()
        writer.writerows(rows)
    os.replace(temp_path, destination)
    return counts


def _predict_llm(rows: list[dict], model: str, bundle: dict, cache: dict) -> None:
    def predict(row: dict) -> tuple[str, tuple[str, float] | None]:
        key = f"{model}|{row['technology']}|{row['ips_title']}|{dashboard._detail_hash(row['details'])}"
        if key in cache:
            return key, cache[key]
        return key, category_model.classify_issue_title_llm(
            bundle, row["ips_title"], technology=row["technology"], description=row["details"], model=model
        )

    done = 0
    with ThreadPoolExecutor(max_workers=4) as pool:
        for row, (key, result) in zip(rows, pool.map(predict, rows)):
            if result is not None:
                cache[key] = list(result)
            row[model] = result[0] if result else None
            done += 1
            if done % 100 == 0:
                print(f"  {model}: {done}/{len(rows)}", flush=True)


def _score(rows: list[dict], key: str) -> dict:
    scored = [row for row in rows if row.get(key)]
    right = sum(row[key] == row["human_category"] for row in scored)
    return {"right": right, "scored": len(scored), "accuracy": right / len(scored) if scored else None}


def _review_rows(payload: dict, existing: dict[str, dict]) -> list[dict]:
    models = [p["key"] for p in payload["predictors"] if p["key"] not in ("ml", "ml_cv")]
    rows = []
    for issue in payload["issues"]:
        row_id = str(issue["row_id"])
        previous = existing.get(row_id, {})
        same_issue = (
            previous.get("ips_title") == issue["ips_title"]
            and previous.get("technology") == issue["technology"]
            and previous.get("source_human_category") == issue["human_category"]
        )
        selected = previous.get("selected_category", "") if same_issue else ""
        rows.append({
            "row_id": row_id,
            "ips_case_number": issue.get("ips_case_number") or "",
            "ips_case_numbers": issue.get("ips_case_numbers") or "",
            "ips_title": issue["ips_title"],
            "technology": issue["technology"],
            "source_human_category": issue["human_category"],
            "human_category": selected or issue["human_category"],
            "ml": issue.get("ml") or "",
            "ml_cv": issue.get("ml_cv") or "",
            "llm1_model": models[0] if models else "",
            "llm1_category": issue.get(models[0]) or "" if models else "",
            "llm2_model": models[1] if len(models) > 1 else "",
            "llm2_category": issue.get(models[1]) or "" if len(models) > 1 else "",
            "has_details": str(bool(issue.get("has_details"))).lower(),
            "selected_category": selected,
            "reviewer": previous.get("reviewer", "") if same_issue else "",
            "reviewed_at": previous.get("reviewed_at", "") if same_issue else "",
        })
    return rows


def _write_review_rows(rows: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(".tmp")
    with temp_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=REVIEW_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    os.replace(temp_path, path)


def sync_golden_reviews(payload: dict, path: Path = REVIEW_PATH) -> list[dict]:
    with REVIEW_LOCK:
        existing = {}
        if path.is_file():
            with path.open("r", encoding="utf-8-sig", newline="") as handle:
                existing = {row["row_id"]: row for row in csv.DictReader(handle)}
        rows = _review_rows(payload, existing)
        _write_review_rows(rows, path)
    return rows


def read_golden_reviews(path: Path = REVIEW_PATH) -> dict[str, dict]:
    if not path.is_file():
        return {}
    with REVIEW_LOCK, path.open("r", encoding="utf-8-sig", newline="") as handle:
        return {row["row_id"]: row for row in csv.DictReader(handle)}


def save_golden_review(
    payload: dict, row_id: object, category: object, reviewer: object,
    allowed_categories: set[str], path: Path = REVIEW_PATH,
) -> dict:
    row_id = str(row_id)
    category = str(category or "").strip()
    reviewer = str(reviewer or "").strip()
    if not row_id.isdecimal() or int(row_id) >= len(payload["issues"]) or str(payload["issues"][int(row_id)]["row_id"]) != row_id:
        raise ValueError("This issue is no longer in the golden set. Reload the page.")
    if category and category not in allowed_categories:
        raise ValueError("Choose a category from the list.")
    if category and (not reviewer or len(reviewer) > 100):
        raise ValueError("Enter your name before saving a category.")
    issue = payload["issues"][int(row_id)]
    with REVIEW_LOCK:
        existing = {}
        if path.is_file():
            with path.open("r", encoding="utf-8-sig", newline="") as handle:
                existing = {row["row_id"]: row for row in csv.DictReader(handle)}
        rows = _review_rows(payload, existing)
        row = rows[int(row_id)]
        if row["ips_title"] != issue["ips_title"] or row["technology"] != issue["technology"]:
            raise ValueError("Golden issue changed. Reload the page.")
        row.update(
            selected_category=category,
            human_category=category or row["source_human_category"],
            reviewer=reviewer if category else "",
            reviewed_at=datetime.now().isoformat(timespec="seconds") if category else "",
        )
        _write_review_rows(rows, path)
    return {key: row[key] for key in ("selected_category", "reviewer", "reviewed_at")}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--golden", type=Path)
    parser.add_argument("--export-ips", type=Path, help="Export all golden rows with verified IPS case numbers and match status.")
    args = parser.parse_args()
    golden_path = args.golden or (DEFAULT_GOLDEN if args.export_ips else IPS_LIST_PATH if IPS_LIST_PATH.is_file() else DEFAULT_GOLDEN)
    if args.export_ips:
        counts = export_ips_list(golden_path, args.export_ips)
        print(f"Wrote {sum(counts.values())} rows to {args.export_ips}: {dict(counts)}")
        return 0

    bundle = category_model.load_category_model(str(dashboard.CATEGORY_MODEL_PATH))
    rows, skipped = _load_golden(golden_path, set(dashboard._category_labels()) or set(bundle.get("labels") or []))
    print(f"{len(rows)} golden rows to score; skipped {sum(skipped.values())}.")

    with golden_path.open("r", encoding="utf-8-sig", newline="") as handle:
        has_numbers = "ips_case_number" in (csv.DictReader(handle).fieldnames or [])
    try:
        details = _lookup_numbered_details(rows) if has_numbers else _lookup_details(sorted({row["ips_title"] for row in rows}))
    except Exception as exc:
        print(f"IPS lookup failed ({type(exc).__name__}: {exc}); LLMs will see titles only.")
        details = {}
    for row in rows:
        if has_numbers:
            description, created = _details_for_row(row, details)
        else:
            match = details.get(row["ips_title"], {})
            description, created = match.get("details", ""), match.get("created", "")
        row.update(ips_case_number=row["ips_case_number"] if has_numbers else match.get("ips_case_number", ""),
                   created=created, details=description)
    print(f"{sum(bool(row['details']) for row in rows)} rows matched to an IPS case with description/environment details.")

    for row in rows:
        description = row["details"] if dashboard.CATEGORY_ML_USE_DETAILS else ""
        row["ml"] = category_model.classify_issue_title(
            bundle, row["ips_title"], technology=row["technology"], description=description, use_llm=False
        )[0]

    # The deployed model was trained on these labels, so also score a copy retrained without each held-out fold.
    features = [category_model._compose_feature_text(row["ips_title"], technology=row["technology"]) for row in rows]
    folds = cross_val_predict(
        clone(bundle["pipeline"]), features, [row["human_category"] for row in rows],
        cv=GroupKFold(n_splits=5), groups=[_title_key(row["ips_title"]) for row in rows],
    )
    for row, predicted in zip(rows, folds):
        rule = category_model._override_category_from_text(row["ips_title"])
        row["ml_cv"] = rule or category_model._normalize_predicted_category(str(predicted))

    models = [model for model in (dashboard._slot_model("llm"), dashboard._slot_model("llm2")) if model]
    cache_path = ROOT / "cache" / "category_golden_llm_cache.json"
    cache = dashboard._read_json(cache_path)
    for model in models:
        print(f"Scoring with {model}...", flush=True)
        _predict_llm(rows, model, bundle, cache)
        cache_path.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")

    predictors = [
        {"key": "ml", "label": "ML model as deployed (title only)", "note": "Trained on these labels, so this is optimistic."},
        {"key": "ml_cv", "label": "ML retrained, 5-fold held out (title only)", "note": "Repeated titles stay in the same fold."},
    ] + [{"key": model, "label": model, "note": "Title + IPS description/environment when matched."} for model in models]
    with_details = [row for row in rows if row["details"]]
    distinct_titles = list({(_title_key(row["ips_title"]), row["technology"]): row for row in rows}.values())
    categories = sorted({row["human_category"] for row in rows}, key=str.casefold)
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "golden_file": golden_path.name,
        "model_file": dashboard.CATEGORY_MODEL_PATH.name,
        "rows": len(rows),
        "distinct_titles": len(distinct_titles),
        "rows_with_details": len(with_details),
        "skipped": dict(skipped),
        "predictors": [
            {**p, "overall": _score(rows, p["key"]), "distinct_titles": _score(distinct_titles, p["key"]),
             "with_details": _score(with_details, p["key"]),
             "by_technology": {tech: _score([r for r in rows if r["technology"] == tech], p["key"]) for tech in sorted({r["technology"] for r in rows})}}
            for p in predictors
        ],
        "by_category": [
            {"category": category, "support": sum(r["human_category"] == category for r in rows),
             **{p["key"]: _score([r for r in rows if r["human_category"] == category], p["key"])["accuracy"] for p in predictors}}
            for category in categories
        ],
        "issues": [
            {key: row.get(key) for key in ("ips_case_number", "ips_case_numbers", "created", "ips_title", "technology", "human_category", "ml", "ml_cv", *models)}
            | {"row_id": index}
            | {"has_details": bool(row["details"])}
            for index, row in enumerate(rows)
        ],
    }
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp_path = OUTPUT_PATH.with_suffix(".tmp")
    temp_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(temp_path, OUTPUT_PATH)
    sync_golden_reviews(payload)
    for p in payload["predictors"]:
        print(f"{p['label']}: {p['overall']['right']}/{p['overall']['scored']} = {p['overall']['accuracy']:.1%}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
