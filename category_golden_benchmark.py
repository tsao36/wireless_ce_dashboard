"""Score ML vs two LLMs against the human-labeled golden set; the dashboard serves the result on category_golden.html."""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shutil
import threading
import warnings
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import joblib
import sklearn
from sklearn.base import clone
from sklearn.metrics import accuracy_score, f1_score
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
IPS_DETAILS_CACHE_PATH = ROOT / "cache" / "ips_details_cache.json"
ML_EVALUATION_PATH = ROOT / "cache" / "category_ml_groupkfold.json"
DETAIL_BENCHMARK_PATH = ROOT / "cache" / "category_detail_benchmark.json"
PROMPT_SAMPLE_PATH = ROOT / "cache" / "category_prompt_v2_sample.json"
PROMPT_SECONDARY_AUDIT_PATH = ROOT / "cache" / "category_prompt_v2_secondary_audit.json"
PROMPT_V21_RETEST_PATH = ROOT / "cache" / "category_prompt_v21_miss_retest.json"
PROMPT_V211_RETEST_PATH = ROOT / "cache" / "category_prompt_v211_miss_retest.json"
IPS_LIST_PATH = ROOT / "golden_training_set_20260603_all_ips.csv"
REVIEW_PATH = ROOT / "category_golden_reviews.csv"
EXPECTED_DETAIL_ROWS = 313
GATED_DETAIL_WEIGHTS = (0.30, 0.35, 0.40, 0.45)
GATED_TARGETS = {"overall_accuracy": 0.7404, "detail_accuracy": 0.7316, "detail_macro_f1": 0.52}
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
                rows.append({"row_id": len(rows), "ips_title": title, "technology": technology, "human_category": label,
                             "dual_review_acceptable": (raw.get("dual_review_acceptable") or "").strip(),
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


def _case_numbers_for_row(row: dict) -> list[str]:
    raw_numbers = row.get("ips_case_numbers") or row.get("ips_case_number") or ""
    numbers = []
    for raw_number in str(raw_numbers).split(";"):
        raw_number = raw_number.strip()
        if raw_number.isdecimal():
            normalized = str(int(raw_number))
            if normalized not in numbers:
                numbers.append(normalized)
    return numbers


def _read_ips_details_cache(path: Path = IPS_DETAILS_CACHE_PATH) -> dict[str, dict]:
    if not path.is_file():
        return {}
    with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    cached = payload.get("cases", {}) if isinstance(payload, dict) else {}
    if not isinstance(cached, dict):
        raise ValueError(f"Invalid IPS detail cache format: {path}")
    return {str(key): value for key, value in cached.items() if isinstance(value, dict)}


def _write_ips_details_cache(cases: dict[str, dict], path: Path = IPS_DETAILS_CACHE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    with temp_path.open("w", encoding="utf-8") as handle:
        json.dump({"schema_version": 1, "cases": cases}, handle, ensure_ascii=False)
    os.replace(temp_path, path)


def _materialize_cached_case(number: str, case: dict) -> dict:
    title = str(case.get("title") or "")
    raw_description = str(case.get("raw_description") or "")
    raw_environment = str(case.get("raw_environment") or "")
    context = dashboard.clean_and_zone_ips_details(title, raw_description, raw_environment)
    return {
        "title": title,
        "created": str(case.get("created") or ""),
        "raw_description": raw_description,
        "raw_environment": raw_environment,
        "details": context["llm_context"],
        "context": context,
        "has_details": bool(raw_description.strip() or raw_environment.strip()),
    }


def _lookup_numbered_details(
    rows: list[dict], *, cache_path: Path = IPS_DETAILS_CACHE_PATH, offline: bool = False, refresh: bool = False,
) -> dict[str, dict]:
    expected_titles: dict[str, set[str]] = {}
    for row in rows:
        title_key = _title_key(row["ips_title"])
        for number in _case_numbers_for_row(row):
            expected_titles.setdefault(number, set()).add(title_key)
    if not expected_titles:
        return {}

    cached = _read_ips_details_cache(cache_path)
    eligible_numbers = {
        number for number, titles in expected_titles.items()
        if len(titles) == 1
    }
    valid_cached = {
        number: case for number, case in cached.items()
        if number in eligible_numbers
        and _title_key(str(case.get("title") or "")) in expected_titles[number]
    }
    missing = sorted(eligible_numbers - set(valid_cached), key=int)
    requested = sorted(eligible_numbers if refresh else missing, key=int)
    fetched_count = 0
    if requested and not offline:
        fetched_rows = dashboard._ips_case_query("CASE_NBR IN ({in})", [int(number) for number in requested])
        for case_number, subject, created, description, environment in fetched_rows:
            number = str(int(case_number))
            title = str(subject or "")
            if number not in eligible_numbers or _title_key(title) not in expected_titles[number]:
                continue
            cached[number] = {
                "title": title,
                "created": str(created)[:10],
                "raw_description": str(description or ""),
                "raw_environment": str(environment or ""),
            }
            fetched_count += 1
        if fetched_count:
            _write_ips_details_cache(cached, cache_path)

    valid_cached = {
        number: case for number, case in cached.items()
        if number in eligible_numbers
        and _title_key(str(case.get("title") or "")) in expected_titles[number]
    }
    print(
        f"IPS detail cache: {len(valid_cached)} verified cases available; "
        f"{fetched_count} fetched, {len(eligible_numbers - set(valid_cached))} unresolved."
    )
    return {number: _materialize_cached_case(number, case) for number, case in valid_cached.items()}


def _details_for_row(row: dict, cases: dict[str, dict]) -> tuple[str, str, dict | None]:
    numbers = _case_numbers_for_row(row)
    matches = [cases.get(number) for number in numbers]
    if not numbers or not all(
        match and _title_key(match["title"]) == _title_key(row["ips_title"]) and match["has_details"]
        for match in matches
    ):
        return "", "", None
    raw_description = "\n\n".join(
        f"IPS {number}:\n{match['raw_description']}" for number, match in zip(numbers, matches)
    )
    raw_environment = "\n\n".join(
        f"IPS {number}:\n{match['raw_environment']}" for number, match in zip(numbers, matches)
    )
    context = dashboard.clean_and_zone_ips_details(row["ips_title"], raw_description, raw_environment)
    created = matches[0]["created"] if len(matches) == 1 else ""
    return context["llm_context"], created, context


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
    def predict(row: dict) -> tuple[str, dict | None]:
        key = _prompt_cache_key(model, row)
        if key in cache:
            cached = _read_cached_response(cache[key])
            if cached:
                return key, cached
        return key, category_model.classify_issue_title_llm_record(
            bundle, row["ips_title"], technology=row["technology"], description=row["details"], model=model
        )

    done = 0
    with ThreadPoolExecutor(max_workers=4) as pool:
        for row, (key, result) in zip(rows, pool.map(predict, rows)):
            if result is not None:
                cache[key] = result
            row[model] = result["primary_category"] if result else None
            done += 1
            if done % 100 == 0:
                print(f"  {model}: {done}/{len(rows)}", flush=True)


def _prompt_cache_key(model: str, row: dict) -> str:
    return "|".join((
        model,
        row["technology"],
        _title_key(row["ips_title"]),
        dashboard._detail_hash(row["details"]),
        category_model.PROMPT_VERSION,
    ))


def _read_cached_result(value: object) -> tuple[str, float] | None:
    if isinstance(value, (list, tuple)) and value:
        category = str(value[0] or "").strip()
        try:
            confidence = float(value[1]) if len(value) > 1 else 0.0
        except (TypeError, ValueError):
            confidence = 0.0
        return (category, confidence) if category else None
    if isinstance(value, dict):
        category = str(value.get("primary_category") or value.get("category") or "").strip()
        try:
            confidence = float(value.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        return (category, confidence) if category else None
    return None


def _read_cached_response(value: object) -> dict | None:
    if isinstance(value, dict) and value.get("primary_category"):
        return {
            "primary_category": str(value["primary_category"]),
            "secondary_category": value.get("secondary_category"),
            "confidence": float(value.get("confidence") or 0.0),
            "evidence_quote": str(value.get("evidence_quote") or ""),
            "reasoning": str(value.get("reasoning") or ""),
        }
    cached = _read_cached_result(value)
    if cached:
        return {
            "primary_category": cached[0],
            "secondary_category": None,
            "confidence": cached[1],
            "evidence_quote": "",
            "reasoning": "",
        }
    return None


def _save_prompt_cache(cache: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(".tmp")
    temp_path.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    os.replace(temp_path, path)


def _select_prompt_sample(rows: list[dict], limit: int) -> list[dict]:
    old_report = dashboard._read_json(OUTPUT_PATH)
    old_issues: dict[tuple[str, str, str], list[dict]] = {}
    for issue in old_report.get("issues") or []:
        key = (_title_key(issue.get("ips_title", "")), str(issue.get("technology", "")).casefold(),
               str(issue.get("human_category", "")).casefold())
        old_issues.setdefault(key, []).append(issue)

    models = ["gpt-5.4-mini", "gpt-5.6-luna"]
    buckets: dict[str, list[dict]] = {}
    pair_confusions = {
        ("roaming", "connectivity"), ("connectivity", "roaming"),
        ("yb/lost", "bsod"), ("bsod", "yb/lost"),
    }
    seen_prompt_inputs: set[tuple[str, str, str]] = set()
    for row in rows:
        if not row.get("details"):
            continue
        key = (_title_key(row["ips_title"]), row["technology"].casefold(), row["human_category"].casefold())
        prior = old_issues.get(key, [])
        if not prior:
            continue
        old = prior.pop(0)
        predictions = {
            "ml_cv": old.get("ml_cv") or old.get("ml"),
            models[0]: old.get(models[0]),
            models[1]: old.get(models[1]),
        }
        if not all(predictions[model] for model in models):
            continue
        wrong = [prediction for prediction in predictions.values() if prediction and prediction != row["human_category"]]
        if not wrong:
            continue
        prompt_input = (row["technology"].casefold(), _title_key(row["ips_title"]), dashboard._detail_hash(row["details"]))
        if prompt_input in seen_prompt_inputs:
            continue
        seen_prompt_inputs.add(prompt_input)
        row_copy = {
            **row,
            "row_id": old.get("row_id"),
            "old_predictions": predictions,
            "old_error_count": len(wrong),
        }
        row_copy["old_pair_confusion"] = any(
            (row["human_category"].casefold(), prediction.casefold()) in pair_confusions
            for prediction in wrong
        )
        buckets.setdefault(row["human_category"], []).append(row_copy)

    for category_rows in buckets.values():
        category_rows.sort(key=lambda row: (
            not row["old_pair_confusion"], -row["old_error_count"], _title_key(row["ips_title"]),
        ))
    preferred = ["Roaming", "BSOD", "YB/Lost", "Connectivity"]
    category_order = [category for category in preferred if category in buckets]
    category_order.extend(sorted((category for category in buckets if category not in category_order), key=str.casefold))
    selected: list[dict] = []
    while len(selected) < limit:
        advanced = False
        for category in category_order:
            if buckets[category]:
                selected.append(buckets[category].pop(0))
                advanced = True
                if len(selected) == limit:
                    break
        if not advanced:
            break
    if len(selected) < limit:
        raise ValueError(f"Only {len(selected)} detail-verified rows with prior ML/LLM errors are available; requested {limit}.")
    return selected


def run_prompt_v2_sample(rows: list[dict], limit: int) -> dict:
    if limit < 1 or limit > 25:
        raise ValueError("Prompt comparison sample limit must be between 1 and 25.")
    selected = _select_prompt_sample(rows, limit)
    models = [dashboard._slot_model("llm"), dashboard._slot_model("llm2")]
    if models != ["gpt-5.4-mini", "gpt-5.6-luna"]:
        raise RuntimeError(f"Expected both requested LLMs to be configured; got {[bool(model) for model in models]}.")
    categories = dashboard._category_labels()
    cache_path = ROOT / "cache" / "category_golden_llm_cache.json"
    cache = dashboard._read_json(cache_path)
    tasks: list[tuple[dict, str, str]] = []
    results: dict[tuple[str, str], tuple[str, float] | None] = {}
    for row in selected:
        for model in models:
            key = _prompt_cache_key(model, row)
            cached = _read_cached_result(cache.get(key))
            if cached is not None:
                results[(key, model)] = cached
            else:
                tasks.append((row, model, key))

    def predict(task: tuple[dict, str, str]) -> tuple[dict, str, str, tuple[str, float] | None]:
        row, model, key = task
        result = category_model._llm_predict_category(
            title=row["ips_title"],
            description=row.get("structured_context") or row["details"],
            predicted_category="",
            technology=row["technology"],
            categories=categories,
            model=model,
        )
        return row, model, key, result

    if tasks:
        with ThreadPoolExecutor(max_workers=4) as pool:
            for row, model, key, result in pool.map(predict, tasks):
                results[(key, model)] = result
                if result is not None:
                    cache[key] = [result[0], result[1]]
        _save_prompt_cache(cache, cache_path)

    comparison_rows = []
    model_scores = {model: {"old_right": 0, "new_right": 0, "new_available": 0, "changed": 0} for model in models}
    for index, row in enumerate(selected, 1):
        comparisons = {}
        for model in models:
            old_category = row["old_predictions"][model]
            new_result = results.get((_prompt_cache_key(model, row), model))
            new_category = new_result[0] if new_result else ""
            scores = model_scores[model]
            scores["old_right"] += old_category == row["human_category"]
            scores["new_available"] += bool(new_category)
            scores["new_right"] += new_category == row["human_category"]
            scores["changed"] += bool(new_category and new_category != old_category)
            comparisons[model] = {
                "old_cached_category": old_category,
                "v2_category": new_category or None,
                "v2_confidence": new_result[1] if new_result else None,
            }
        comparison_rows.append({
            "sample_index": index,
            "row_id": row.get("row_id"),
            "ips_case_number": row.get("ips_case_number"),
            "ips_title": row["ips_title"],
            "human_category": row["human_category"],
            "old_ml_error": row["old_predictions"]["ml_cv"] != row["human_category"],
            "models": comparisons,
        })

    summary = {
        "prompt_version": category_model.PROMPT_VERSION,
        "compared_to_prompt_version": "v2_definitions_hierarchy",
        "sample_rows": len(comparison_rows),
        "selection": "Detail-verified rows with a prior ML or old-LLM error, stratified by human category and prioritizing the requested confusion pairs.",
        "models": {
            model: {
                **score,
                "old_accuracy": score["old_right"] / len(comparison_rows),
                "v2_accuracy": score["new_right"] / score["new_available"] if score["new_available"] else None,
            }
            for model, score in model_scores.items()
        },
        "rows": comparison_rows,
    }
    PROMPT_SAMPLE_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp_path = PROMPT_SAMPLE_PATH.with_suffix(".tmp")
    temp_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp_path, PROMPT_SAMPLE_PATH)
    print(f"Prompt v2 comparison: {len(comparison_rows)} detail-verified error rows")
    for model in models:
        score = summary["models"][model]
        print(f"{model}: old cached {score['old_right']}/{limit}; v2 {score['new_right']}/{score['new_available']}; changed {score['changed']}")
    for row in comparison_rows:
        predictions = " | ".join(
            f"{model}: {row['models'][model]['old_cached_category']} -> {row['models'][model]['v2_category'] or 'NO_RESULT'}"
            for model in models
        )
        print(f"{row['sample_index']:02d}. [{row['human_category']}] {row['ips_title']} | {predictions}")
    print(f"Sample report: {PROMPT_SAMPLE_PATH}")
    return summary


def run_prompt_secondary_audit(rows: list[dict]) -> dict:
    prior_report = dashboard._read_json(PROMPT_SAMPLE_PATH)
    prior_rows = prior_report.get("rows") or []
    missed_rows = [
        row for row in prior_rows
        if any(row.get("models", {}).get(model, {}).get("v2_category") != row.get("human_category")
               for model in ("gpt-5.4-mini", "gpt-5.6-luna"))
    ]
    if not missed_rows:
        raise ValueError("No missed rows were found in the saved 25-row Prompt v2 sample.")

    rows_by_id = {int(row["row_id"]): row for row in rows if row.get("details")}
    selected = []
    for previous in missed_rows:
        row_id = previous.get("row_id")
        row = rows_by_id.get(int(row_id)) if str(row_id).isdecimal() else None
        if not row or _title_key(row["ips_title"]) != _title_key(previous["ips_title"]):
            raise ValueError(f"Could not rematch missed sample row {previous.get('row_id')} to a verified detail input.")
        selected.append({**row, "previous_v2": previous["models"]})

    models = ["gpt-5.4-mini", "gpt-5.6-luna"]
    categories = dashboard._category_labels()
    cache_path = ROOT / "cache" / "category_golden_llm_cache.json"
    cache = dashboard._read_json(cache_path)
    tasks = [(row, model, _prompt_cache_key(model, row)) for row in selected for model in models]

    def predict(task: tuple[dict, str, str]) -> tuple[dict, str, str, dict | None]:
        row, model, key = task
        response = category_model._llm_predict_category_response(
            title=row["ips_title"],
            description=row.get("structured_context") or row["details"],
            predicted_category="",
            technology=row["technology"],
            categories=categories,
            model=model,
        )
        return row, model, key, response

    responses: dict[tuple[str, str], dict | None] = {}
    with ThreadPoolExecutor(max_workers=4) as pool:
        for row, model, key, response in pool.map(predict, tasks):
            responses[(row["ips_case_number"], model)] = response
            if response:
                cache[key] = response
    _save_prompt_cache(cache, cache_path)

    scores = {model: {"prior_v2_primary_right": 0, "audited_primary_right": 0, "secondary_matches_gold": 0, "top2_acceptable": 0} for model in models}
    output_rows = []
    for row in selected:
        comparisons = {}
        for model in models:
            old = row["previous_v2"][model]
            response = responses.get((row["ips_case_number"], model))
            primary = response.get("primary_category") if response else None
            secondary = response.get("secondary_category") if response else None
            score = scores[model]
            score["prior_v2_primary_right"] += old.get("v2_category") == row["human_category"]
            score["audited_primary_right"] += primary == row["human_category"]
            score["secondary_matches_gold"] += secondary == row["human_category"]
            score["top2_acceptable"] += row["human_category"] in {primary, secondary}
            comparisons[model] = {
                "prior_v2_primary": old.get("v2_category"),
                "audited_primary": primary,
                "secondary_category": secondary,
                "secondary_matches_gold": secondary == row["human_category"],
                "confidence": response.get("confidence") if response else None,
                "evidence_quote": response.get("evidence_quote") if response else None,
                "reasoning": response.get("reasoning") if response else None,
            }
        output_rows.append({
            "row_id": row.get("row_id"),
            "ips_case_number": row.get("ips_case_number"),
            "ips_title": row["ips_title"],
            "technology": row["technology"],
            "human_category": row["human_category"],
            "models": comparisons,
        })
    summary = {
        "prompt_version": category_model.PROMPT_VERSION,
        "audited_rows": len(output_rows),
        "selection": "Only rows where at least one model missed the prior 25-row Prompt v2 sample.",
        "models": {
            model: {**score, **{key: value / len(output_rows) for key, value in score.items()}}
            for model, score in scores.items()
        },
        "rows": output_rows,
    }
    if category_model.PROMPT_VERSION == "v2_definitions_hierarchy":
        audit_path = PROMPT_SECONDARY_AUDIT_PATH
    elif category_model.PROMPT_VERSION == "v2.1.1_definitions_hierarchy":
        audit_path = PROMPT_V211_RETEST_PATH
    else:
        audit_path = PROMPT_V21_RETEST_PATH
    temp_path = audit_path.with_suffix(".tmp")
    temp_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp_path, audit_path)
    print(f"Prompt {category_model.PROMPT_VERSION} miss-only audit: {len(output_rows)} rows")
    for model in models:
        print(model, summary["models"][model])
    print(f"Audit report: {audit_path}")
    return summary


def _score(rows: list[dict], key: str) -> dict:
    scored = [row for row in rows if row.get(key)]
    right = sum(row[key] == row["human_category"] for row in scored)
    return {"right": right, "scored": len(scored), "accuracy": right / len(scored) if scored else None}


def _classification_metrics(rows: list[dict], predictions: list[str]) -> dict:
    labels = [row["human_category"] for row in rows]
    return {
        "rows": len(rows),
        "accuracy": float(accuracy_score(labels, predictions)) if rows else None,
        "macro_f1": float(f1_score(labels, predictions, average="macro", zero_division=0)) if rows else None,
    }


def evaluate_ml_pipelines(rows: list[dict], legacy_bundle: dict) -> dict:
    """Compare the legacy and both structured pipelines using shared normalized-title folds."""
    if len({_title_key(row["ips_title"]) for row in rows}) < 5:
        raise ValueError("At least five distinct normalized titles are required for GroupKFold.")
    groups = [_title_key(row["ips_title"]) for row in rows]
    labels = [row["human_category"] for row in rows]
    detail_indexes = [index for index, row in enumerate(rows) if row.get("details")]
    gated_keys = [f"gated_dual_{int(weight * 100):02d}" for weight in GATED_DETAIL_WEIGHTS]
    predictions = {
        key: [""] * len(rows)
        for key in ("legacy", "title_only", "title_plus_details", *gated_keys)
    }
    gated_oof_predictions: list[dict | None] = [None] * len(rows)
    legacy_pipeline = legacy_bundle["pipeline"]
    splitter = GroupKFold(n_splits=5)

    for train_indexes, test_indexes in splitter.split(rows, labels, groups):
        training_rows = [rows[index] for index in train_indexes]
        train_labels = [row["human_category"] for row in training_rows]
        legacy_fold = clone(legacy_pipeline)
        legacy_features = [
            category_model._compose_feature_text(row["ips_title"], technology=row["technology"])
            for row in training_rows
        ]
        legacy_fold.fit(legacy_features, train_labels)
        legacy_fold_bundle = {**legacy_bundle, "pipeline": legacy_fold}

        group_rows = [_title_key(row["ips_title"]) for row in training_rows]
        dual_bundle = category_model.train_dual_feature_models(
            [row["ips_title"] for row in training_rows],
            train_labels,
            technologies=[row["technology"] for row in training_rows],
            contexts=[row.get("structured_context") for row in training_rows],
            groups=group_rows,
        )
        title_only_bundle = {
            "pipeline": dual_bundle["model_title_only"],
            "labels": dual_bundle["labels"],
            "feature_mode": "structured_v1",
        }
        title_detail_bundle = {
            "pipeline": dual_bundle["model_with_details"],
            "labels": dual_bundle["labels"],
            "feature_mode": "structured_v1",
        }

        for index in test_indexes:
            row = rows[index]
            context = row.get("structured_context")
            predictions["legacy"][index] = category_model.classify_issue_title(
                legacy_fold_bundle, row["ips_title"], technology=row["technology"], use_llm=False
            )[0]
            predictions["title_only"][index] = category_model.classify_issue_title(
                title_only_bundle, row["ips_title"], technology=row["technology"], use_llm=False
            )[0]
            predictions["title_plus_details"][index] = category_model.classify_issue_title(
                title_detail_bundle,
                row["ips_title"],
                technology=row["technology"],
                description=context or "",
                use_llm=False,
            )[0]
            for weight, key in zip(GATED_DETAIL_WEIGHTS, gated_keys):
                dual_bundle["detail_blend_weights"] = {
                    "title_only": 1.0 - weight,
                    "with_details": weight,
                }
                predictions[key][index] = category_model.classify_issue_title(
                    dual_bundle,
                    row["ips_title"],
                    technology=row["technology"],
                    description=context or "",
                    use_llm=False,
                )[0]
                if key == "gated_dual_35":
                    rule_category = category_model._tier1_rule_category(row["ips_title"], row["technology"])
                    if rule_category:
                        gated_prediction = (rule_category, 1.0, None)
                    else:
                        gated_prediction = category_model._predict_dual_feature_model(
                            dual_bundle, row["ips_title"], row["technology"], context or {},
                        )
                    hybrid = category_model.predict_issue_category_hybrid(
                        {
                            "row_id": row.get("row_id", index),
                            "ips_title": row["ips_title"],
                            "technology": row["technology"],
                            "structured_context": context,
                            "ml_pred": gated_prediction[0],
                            "ml_confidence": gated_prediction[1],
                            "ml_secondary_category": gated_prediction[2],
                        },
                        model_bundle=dual_bundle,
                    )
                    gated_oof_predictions[index] = {
                        "row_id": row.get("row_id", index),
                        "primary_category": gated_prediction[0],
                        "confidence": gated_prediction[1],
                        "secondary_category": gated_prediction[2],
                        "routing_tier": hybrid["routing_tier"],
                        "needs_human_review": hybrid["needs_human_review"],
                    }

    model_labels = {
        "legacy": "Legacy ML",
        "title_only": "New ML: title + signature hints",
        "title_plus_details": "New ML: title + cleaned details + signature hints",
        **{
            key: f"Dual gated: {1.0 - weight:.2f} title + {weight:.2f} detail"
            for key, weight in zip(gated_keys, GATED_DETAIL_WEIGHTS)
        },
    }
    models = []
    detail_rows = [rows[index] for index in detail_indexes]
    for key, label in model_labels.items():
        detail_predictions = [predictions[key][index] for index in detail_indexes]
        models.append({
            "key": key,
            "label": label,
            "overall": _classification_metrics(rows, predictions[key]),
            "detail_verified": _classification_metrics(detail_rows, detail_predictions),
        })
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "splitter": "GroupKFold(n_splits=5), grouped by normalized title",
        "rows": len(rows),
        "normalized_title_groups": len(set(groups)),
        "detail_verified_rows": len(detail_indexes),
        "models": models,
        "gated_oof_predictions": gated_oof_predictions,
    }


def _print_ml_comparison(result: dict) -> None:
    print("\n5-fold GroupKFold comparison (same normalized-title splits)")
    print(f"{'Model':48} {'Overall Acc':>12} {'Overall F1':>12} {'Details Acc':>12} {'Details F1':>12}")
    for model in result["models"]:
        overall = model["overall"]
        details = model["detail_verified"]
        print(
            f"{model['label']:48} {overall['accuracy']:.2%} {overall['macro_f1']:.4f} "
            f"{details['accuracy']:.2%} {details['macro_f1']:.4f}"
        )
    print(
        f"Rows: {result['rows']} overall; {result['detail_verified_rows']} detail-verified; "
        f"{result['normalized_title_groups']} normalized-title groups."
    )


def _backup_and_promote_model(artifact_path: Path, model_bundle: dict) -> Path:
    if not artifact_path.is_file():
        raise FileNotFoundError(f"Production model artifact does not exist: {artifact_path}")
    backup_path = artifact_path.with_name(artifact_path.name + ".bak_v18")
    if backup_path.exists():
        backup_path = artifact_path.with_name(
            artifact_path.name + ".bak_v18_" + datetime.now().strftime("%Y%m%d_%H%M%S")
        )
    shutil.copy2(artifact_path, backup_path)
    temp_path = artifact_path.with_name(artifact_path.name + ".tmp")
    try:
        joblib.dump(model_bundle, temp_path)
        os.replace(temp_path, artifact_path)
        with warnings.catch_warnings(record=True) as captured:
            warnings.simplefilter("always")
            loaded = joblib.load(artifact_path)
        version_warnings = [warning for warning in captured if type(warning.message).__name__ == "InconsistentVersionWarning"]
        if version_warnings:
            raise RuntimeError("The newly written model still emits a scikit-learn version warning.")
        if not isinstance(loaded, dict) or loaded.get("feature_mode") != "dual_structured_v2":
            raise RuntimeError("The newly written artifact failed its dual-model reload check.")
    except Exception:
        if temp_path.exists():
            temp_path.unlink()
        shutil.copy2(backup_path, artifact_path)
        raise
    print(f"Model backup: {backup_path}")
    print(f"Promoted artifact: {artifact_path} (reload check: no version warning)")
    return backup_path


def _legacy_baseline_bundle(active_bundle: dict, artifact_path: Path) -> dict:
    if active_bundle.get("feature_mode") not in {"structured_v1", "dual_structured_v2"}:
        return active_bundle
    candidates = [artifact_path.with_name(artifact_path.name + ".bak_v18")]
    candidates.extend(sorted(artifact_path.parent.glob(artifact_path.name + ".bak_v18_*"), reverse=True))
    candidates.append(ROOT / "models" / "issue_category_model.joblib")
    for candidate in candidates:
        if not candidate.is_file():
            continue
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", message="Trying to unpickle estimator", category=Warning)
            legacy = category_model.load_category_model(str(candidate))
        if legacy.get("feature_mode") not in {"structured_v1", "dual_structured_v2"}:
            print(f"Legacy CV baseline: {candidate}")
            return legacy
    raise RuntimeError("A legacy title-text model is required for the three-model GroupKFold comparison.")


def evaluate_and_promote_ml_model(rows: list[dict], legacy_bundle: dict, artifact_path: Path) -> dict:
    detail_rows = sum(bool(row.get("details")) for row in rows)
    if detail_rows < EXPECTED_DETAIL_ROWS:
        raise RuntimeError(
            f"Only {detail_rows} detail-verified rows are available; refusing promotion "
            f"because at least {EXPECTED_DETAIL_ROWS} are required."
        )
    legacy_bundle = _legacy_baseline_bundle(legacy_bundle, artifact_path)
    result = evaluate_ml_pipelines(rows, legacy_bundle)
    _print_ml_comparison(result)
    gated = [model for model in result["models"] if model["key"].startswith("gated_dual_")]
    eligible = [
        model for model in gated
        if model["overall"]["accuracy"] >= GATED_TARGETS["overall_accuracy"]
        and model["detail_verified"]["accuracy"] >= GATED_TARGETS["detail_accuracy"]
        and model["detail_verified"]["macro_f1"] >= GATED_TARGETS["detail_macro_f1"]
    ]
    result["gated_targets"] = GATED_TARGETS
    result["promotion_thresholds_met"] = bool(eligible)
    if not eligible:
        ML_EVALUATION_PATH.parent.mkdir(parents=True, exist_ok=True)
        temp_path = ML_EVALUATION_PATH.with_suffix(".tmp")
        temp_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp_path, ML_EVALUATION_PATH)
        print("No gated blend met all three targets; production artifact was left unchanged.")
        print(f"Evaluation report: {ML_EVALUATION_PATH}")
        return result

    selected_model = max(
        eligible,
        key=lambda model: (
            model["detail_verified"]["macro_f1"],
            model["detail_verified"]["accuracy"],
            model["overall"]["accuracy"],
        ),
    )
    detail_weight = int(selected_model["key"].rsplit("_", 1)[1]) / 100.0
    selected = selected_model["key"]
    promoted_bundle = category_model.train_dual_feature_models(
        [row["ips_title"] for row in rows],
        [row["human_category"] for row in rows],
        technologies=[row["technology"] for row in rows],
        contexts=[row.get("structured_context") for row in rows],
        groups=[_title_key(row["ips_title"]) for row in rows],
        detail_blend_weights={"title_only": 1.0 - detail_weight, "with_details": detail_weight},
    )
    backup_path = _backup_and_promote_model(artifact_path, promoted_bundle)
    result.update({
        "sklearn_version": sklearn.__version__,
        "selected_variant": selected,
        "selected_blend_weights": promoted_bundle["detail_blend_weights"],
        "selection_reason": "Selected gated blend met overall accuracy, detail accuracy, and detail macro-F1 targets.",
        "artifact_path": str(artifact_path),
        "backup_path": str(backup_path),
        "calibration_method": promoted_bundle["calibration_method"],
        "temperatures": promoted_bundle["temperatures"],
    })
    ML_EVALUATION_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp_path = ML_EVALUATION_PATH.with_suffix(".tmp")
    temp_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp_path, ML_EVALUATION_PATH)
    print(f"Selected model: {selected}; calibration: {promoted_bundle['calibration_method']}")
    print(f"Evaluation report: {ML_EVALUATION_PATH}")
    return result


def _acceptable_categories(row: dict) -> set[str]:
    accepted = {row["human_category"]}
    raw = row.get("dual_review_acceptable") or ""
    values = raw if isinstance(raw, list) else re.split(r"[;,|]", str(raw))
    accepted.update(str(value).strip() for value in values if str(value).strip())
    return accepted


def _prediction_summary(rows: list[dict], predictions: list[dict]) -> dict:
    gold = [row["human_category"] for row in rows]
    primary = [prediction.get("primary_category") or "" for prediction in predictions]
    primary_right = sum(value == label for value, label in zip(primary, gold))
    acceptable = sum(
        prediction.get("primary_category") in _acceptable_categories(row)
        or prediction.get("secondary_category") in _acceptable_categories(row)
        for row, prediction in zip(rows, predictions)
    )
    secondary_right = sum(
        bool(prediction.get("secondary_category"))
        and prediction.get("secondary_category") in _acceptable_categories(row)
        for row, prediction in zip(rows, predictions)
    )
    return {
        "rows": len(rows),
        "primary_accuracy": primary_right / len(rows) if rows else None,
        "top2_acceptable_accuracy": acceptable / len(rows) if rows else None,
        "secondary_gold_rate": secondary_right / len(rows) if rows else None,
        "macro_f1": float(f1_score(gold, primary, average="macro", zero_division=0)) if rows else None,
    }


def _sync_hybrid_to_golden_report(
    rows: list[dict], oof: list[dict], llm_records: dict[tuple[int, str], dict | None],
    hybrid: list[dict], summary: dict,
) -> None:
    payload = dashboard._read_json(OUTPUT_PATH)
    issues = payload.get("issues") or []
    if len(issues) != len(rows):
        raise ValueError("Existing golden report does not match the 809-row benchmark input.")
    for index, (row, ml_prediction, hybrid_prediction) in enumerate(zip(rows, oof, hybrid)):
        issue = issues[index]
        issue.update({
            "gated_ml_oof": ml_prediction["primary_category"],
            "gated_ml_confidence": ml_prediction["confidence"],
            "gated_ml_secondary_category": ml_prediction["secondary_category"],
            "hybrid_category": hybrid_prediction["primary_category"],
            "routing_tier": hybrid_prediction["routing_tier"],
            "secondary_category": hybrid_prediction["secondary_category"],
            "needs_human_review": hybrid_prediction["needs_human_review"],
        })
        for model in ("gpt-5.4-mini", "gpt-5.6-luna"):
            response = llm_records.get((index, model))
            if row.get("details") and response:
                issue[model] = response["primary_category"]
                issue[f"{model}_secondary_category"] = response.get("secondary_category")
            elif not row.get("details"):
                issue[model] = None
                issue[f"{model}_secondary_category"] = None
    payload["prompt_version"] = category_model.PROMPT_VERSION
    payload["detail_ablation"] = summary["models"]
    payload["hybrid_evaluation"] = summary["hybrid"]
    payload["hybrid_updated_at"] = summary["generated_at"]
    temp_path = OUTPUT_PATH.with_suffix(".tmp")
    temp_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(temp_path, OUTPUT_PATH)


def run_detail_benchmark(rows: list[dict], active_bundle: dict, artifact_path: Path) -> dict:
    detail_indexes = [index for index, row in enumerate(rows) if row.get("details")]
    if len(rows) != 809 or len(detail_indexes) < EXPECTED_DETAIL_ROWS:
        raise RuntimeError(
            f"Matched-input guard failed: got {len(rows)} total rows and {len(detail_indexes)} details; "
            f"expected 809 and at least {EXPECTED_DETAIL_ROWS}."
        )
    legacy_bundle = _legacy_baseline_bundle(active_bundle, artifact_path)
    cv_result = evaluate_ml_pipelines(rows, legacy_bundle)
    ml_oof = cv_result["gated_oof_predictions"]
    if len(ml_oof) != len(rows) or any(prediction is None for prediction in ml_oof):
        raise RuntimeError("The gated ML GroupKFold predictions are incomplete.")

    ml_eval_result = {
        **cv_result,
        "selected_variant": "gated_dual_35",
        "selected_blend_weights": {"title_only": 0.65, "with_details": 0.35},
        "gated_targets": GATED_TARGETS,
    }
    ML_EVALUATION_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp_ml_path = ML_EVALUATION_PATH.with_suffix(".tmp")
    temp_ml_path.write_text(json.dumps(ml_eval_result, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp_ml_path, ML_EVALUATION_PATH)
    _print_ml_comparison(cv_result)

    models = [dashboard._slot_model("llm"), dashboard._slot_model("llm2")]
    if models != ["gpt-5.4-mini", "gpt-5.6-luna"]:
        raise RuntimeError(f"Expected both requested LLMs to be configured; got {[bool(model) for model in models]}.")
    labels = dashboard._category_labels()
    cache_path = ROOT / "cache" / "category_golden_llm_cache.json"
    cache = dashboard._read_json(cache_path)
    records: dict[tuple[int, str], dict | None] = {}
    pending: dict[str, tuple[dict, str, list[int]]] = {}
    for index in detail_indexes:
        row = rows[index]
        for model in models:
            key = _prompt_cache_key(model, row)
            cached = cache.get(key)
            if isinstance(cached, dict) and cached.get("primary_category"):
                records[(index, model)] = _read_cached_response(cached)
            else:
                if key in pending:
                    pending[key][2].append(index)
                else:
                    pending[key] = (row, model, [index])

    def predict(task: tuple[str, tuple[dict, str, list[int]]]) -> tuple[str, list[int], str, dict | None]:
        key, (row, model, indexes) = task
        response = category_model._llm_predict_category_response(
            title=row["ips_title"],
            description=row.get("structured_context") or row["details"],
            predicted_category="",
            technology=row["technology"],
            categories=labels,
            model=model,
        )
        return key, indexes, model, response

    completed = 0
    if pending:
        print(f"Prompt {category_model.PROMPT_VERSION}: querying {len(pending)} unique model/detail inputs across {len(detail_indexes)} verified rows.")
        with ThreadPoolExecutor(max_workers=4) as pool:
            for key, indexes, model, response in pool.map(predict, pending.items()):
                for index in indexes:
                    records[(index, model)] = response
                if response:
                    cache[key] = response
                completed += 1
                if completed % 20 == 0:
                    _save_prompt_cache(cache, cache_path)
                    print(f"  prompt v2.1.1: {completed}/{len(pending)} inputs complete", flush=True)
        _save_prompt_cache(cache, cache_path)

    detail_rows = [rows[index] for index in detail_indexes]
    llm_predictions = {
        model: [records.get((index, model)) or {} for index in detail_indexes]
        for model in models
    }
    gated_predictions = [ml_oof[index] for index in detail_indexes]
    model_results: dict[str, dict] = {
        "gated_ml_oof": {
            "overall": _prediction_summary(rows, ml_oof),
            "detail_verified": _prediction_summary(detail_rows, gated_predictions),
        }
    }
    for model in models:
        model_results[model] = {"detail_verified": _prediction_summary(detail_rows, llm_predictions[model])}

    hybrids: list[dict] = []
    detail_index_set = set(detail_indexes)
    for index, row in enumerate(rows):
        llm1 = records.get((index, models[0])) if index in detail_index_set else None
        llm2 = records.get((index, models[1])) if index in detail_index_set else None
        hybrid = category_model.predict_issue_category_hybrid(
            {
                "row_id": row["row_id"],
                "ips_title": row["ips_title"],
                "technology": row["technology"],
                "structured_context": row.get("structured_context"),
                "ml_pred": ml_oof[index]["primary_category"],
                "ml_confidence": ml_oof[index]["confidence"],
                "ml_secondary_category": ml_oof[index]["secondary_category"],
                "llm1": llm1,
                "llm2": llm2,
            },
            model_bundle=active_bundle,
        )
        hybrids.append(hybrid)

    hybrid_detail = [hybrids[index] for index in detail_indexes]
    review_overall = sum(bool(item["needs_human_review"]) for item in hybrids)
    review_detail = sum(bool(item["needs_human_review"]) for item in hybrid_detail)
    summary = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "prompt_version": category_model.PROMPT_VERSION,
        "rows": len(rows),
        "detail_verified_rows": len(detail_indexes),
        "ml_splitter": cv_result["splitter"],
        "llm_detail_coverage": {model: sum(bool(records.get((index, model))) for index in detail_indexes) for model in models},
        "dual_review_acceptable_column_present": any(bool(row.get("dual_review_acceptable")) for row in rows),
        "models": model_results,
        "hybrid": {
            "overall": _prediction_summary(rows, hybrids),
            "detail_verified": _prediction_summary(detail_rows, hybrid_detail),
            "overall_human_review_rows": review_overall,
            "overall_human_review_rate": review_overall / len(rows),
            "detail_human_review_rows": review_detail,
            "detail_human_review_rate": review_detail / len(detail_indexes),
            "overall_routing_tiers": dict(Counter(item["routing_tier"] for item in hybrids)),
            "detail_routing_tiers": dict(Counter(item["routing_tier"] for item in hybrid_detail)),
        },
        "rows_detail": [
            {
                "row_id": rows[index]["row_id"],
                "ips_case_number": rows[index].get("ips_case_number"),
                "ips_title": rows[index]["ips_title"],
                "technology": rows[index]["technology"],
                "human_category": rows[index]["human_category"],
                "dual_review_acceptable": rows[index].get("dual_review_acceptable") or None,
                "gated_ml_oof": ml_oof[index],
                "gpt-5.4-mini": records.get((index, models[0])),
                "gpt-5.6-luna": records.get((index, models[1])),
                "hybrid": hybrids[index],
            }
            for index in detail_indexes
        ],
    }
    DETAIL_BENCHMARK_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp_path = DETAIL_BENCHMARK_PATH.with_suffix(".tmp")
    temp_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp_path, DETAIL_BENCHMARK_PATH)
    _sync_hybrid_to_golden_report(rows, ml_oof, records, hybrids, summary)
    print("\nMatched-input ablation on verified IPS details")
    for model, result in model_results.items():
        metrics = result["detail_verified"]
        print(f"{model}: primary {metrics['primary_accuracy']:.2%}; top-2 acceptable {metrics['top2_acceptable_accuracy']:.2%}; macro-F1 {metrics['macro_f1']:.4f}")
    print(
        f"Hybrid: all rows primary {summary['hybrid']['overall']['primary_accuracy']:.2%}, "
        f"review {summary['hybrid']['overall_human_review_rate']:.1%}; detail primary "
        f"{summary['hybrid']['detail_verified']['primary_accuracy']:.2%}, "
        f"review {summary['hybrid']['detail_human_review_rate']:.1%}."
    )
    print(f"Detail report: {DETAIL_BENCHMARK_PATH}")
    return summary


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
    parser.add_argument("--ml-eval-and-promote", action="store_true", help="Run leakage-safe ML comparison, then back up and promote the winning structured model.")
    parser.add_argument("--offline-details", action="store_true", help="Use only the local IPS detail cache; do not query Snowflake.")
    parser.add_argument("--refresh-ips-details", action="store_true", help="Refresh cached IPS cases from Snowflake before evaluation.")
    parser.add_argument("--sample-limit", type=int, help="Run only a prompt comparison on this many error-prioritized detail rows (maximum 25).")
    parser.add_argument("--sample-secondary-audit", action="store_true", help="Re-query only sample rows missed by either LLM and retain the full v2 JSON response.")
    parser.add_argument("--detail-benchmark", action="store_true", help="Run the matched detail-row LLM ablation and 809/313-row hybrid evaluation.")
    args = parser.parse_args()
    if args.offline_details and args.refresh_ips_details:
        parser.error("--offline-details and --refresh-ips-details cannot be used together.")
    if args.sample_limit is not None and not 1 <= args.sample_limit <= 25:
        parser.error("--sample-limit must be between 1 and 25; full-set LLM scoring is not enabled here.")
    if args.sample_limit is not None and args.ml_eval_and_promote:
        parser.error("--sample-limit and --ml-eval-and-promote are separate benchmark modes.")
    if args.sample_secondary_audit and (args.sample_limit is not None or args.ml_eval_and_promote):
        parser.error("--sample-secondary-audit is a standalone miss-only mode.")
    if args.detail_benchmark and (args.sample_limit is not None or args.sample_secondary_audit or args.ml_eval_and_promote):
        parser.error("--detail-benchmark is a standalone benchmark mode.")
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
    if (args.ml_eval_and_promote or args.detail_benchmark) and not has_numbers:
        print("ERROR: this evaluation requires IPS case number columns for verified detail matching.")
        return 2
    try:
        details = (
            _lookup_numbered_details(
                rows,
                offline=args.offline_details,
                refresh=args.refresh_ips_details,
            )
            if has_numbers
            else _lookup_details(sorted({row["ips_title"] for row in rows}))
        )
    except Exception as exc:
        print(f"WARNING: IPS detail lookup failed ({type(exc).__name__}: {exc}); detail coverage may drop.")
        if args.ml_eval_and_promote or args.detail_benchmark:
            return 2
        details = {}
    for row in rows:
        if has_numbers:
            description, created, structured_context = _details_for_row(row, details)
        else:
            match = details.get(row["ips_title"], {})
            description, created = match.get("details", ""), match.get("created", "")
            structured_context = (
                dashboard.clean_and_zone_ips_details(row["ips_title"], description, "")
                if description else None
            )
        row.update(ips_case_number=row["ips_case_number"] if has_numbers else match.get("ips_case_number", ""),
                   created=created, details=description, structured_context=structured_context)
    detail_count = sum(bool(row["details"]) for row in rows)
    print(f"{detail_count} rows matched to an IPS case with description/environment details.")

    if args.detail_benchmark:
        try:
            run_detail_benchmark(rows, bundle, dashboard.CATEGORY_MODEL_PATH)
        except Exception as exc:
            print(f"ERROR: detail benchmark stopped ({type(exc).__name__}: {exc}).")
            return 2
        return 0

    if args.sample_secondary_audit:
        try:
            run_prompt_secondary_audit(rows)
        except Exception as exc:
            print(f"ERROR: secondary audit stopped ({type(exc).__name__}: {exc}).")
            return 2
        return 0

    if args.sample_limit is not None:
        try:
            run_prompt_v2_sample(rows, args.sample_limit)
        except Exception as exc:
            print(f"ERROR: prompt sample stopped ({type(exc).__name__}: {exc}).")
            return 2
        return 0

    if args.ml_eval_and_promote:
        if len(rows) != 809:
            print(f"WARNING: expected 809 eligible golden rows; found {len(rows)}.")
        try:
            evaluate_and_promote_ml_model(rows, bundle, dashboard.CATEGORY_MODEL_PATH)
        except Exception as exc:
            print(f"ERROR: ML evaluation/promotion stopped ({type(exc).__name__}: {exc}).")
            return 2
        return 0

    for row in rows:
        description = row["structured_context"] if dashboard.CATEGORY_ML_USE_DETAILS else ""
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
