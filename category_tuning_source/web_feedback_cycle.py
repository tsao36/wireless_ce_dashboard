from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shutil
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
POOL_NAME = "web_2026_issue_pool.csv"
MIN_NEW_LABELS = 10
MIN_MACRO_F1_GAIN = 0.005
KEY_RECALL_DROP_TOLERANCE = 0.02
KEY_CLASSES = ("Audio", "Connectivity", "BSOD", "System Hang", "Performance")


def _dashboard_root(value: str) -> Path:
    if value:
        root = Path(value).resolve()
    else:
        root = (SCRIPT_DIR.parents[4] / "AI Project" / "wireless_ce_dashboard").resolve()
    if not (root / "loading_dashboard.py").is_file():
        raise FileNotFoundError(f"Cannot locate wireless_ce_dashboard at {root}; set WIRELESS_CE_DASHBOARD_ROOT.")
    return root


def _load_runtime(dashboard_root: Path) -> tuple[Any, Any]:
    for path in (str(SCRIPT_DIR), str(dashboard_root)):
        if path not in sys.path:
            sys.path.insert(0, path)
    import issue_category_model as category_model
    import loading_dashboard as dashboard

    return category_model, dashboard


def _atomic_csv(path: Path, rows: list[dict[str, Any]], fieldnames: tuple[str, ...]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    os.replace(temporary, path)


def refresh_web_pool(dashboard: Any, tuning_dir: Path) -> int:
    queue = dashboard._load_category_queue()
    reviews = dashboard._read_category_reviews()
    rows = []
    for case_number, issue in queue.items():
        context = dashboard.CATEGORY_CACHE["details"].get(case_number) or {}
        review = reviews.get(case_number) or {}
        rows.append({
            "ips_case_number": case_number,
            "ips_title": issue["ips_title"],
            "technology": issue["technology"],
            "ips_description": context.get("llm_context", ""),
            "predicted_category": issue["predicted_category"],
            "ml_confidence": issue["confidence"],
            "ml_secondary_category": issue.get("ml_secondary_category", ""),
            "human_category": review.get("human_category", ""),
            "reviewer": review.get("reviewer", ""),
            "reviewed_at": review.get("reviewed_at", ""),
            "has_details": bool(context.get("llm_context")),
        })
    destination = tuning_dir / "CFE_input" / POOL_NAME
    _atomic_csv(
        destination,
        rows,
        ("ips_case_number", "ips_title", "technology", "ips_description", "predicted_category",
         "ml_confidence", "ml_secondary_category", "human_category", "reviewer", "reviewed_at", "has_details"),
    )
    print(f"Web issue pool refreshed: {len(rows)} current-year issues; {sum(row['has_details'] for row in rows)} with IPS details.")
    print(f"Pool snapshot: {destination}")
    return len(rows)


def _title_key(title: str) -> str:
    return re.sub(r"[^0-9a-z]+", "", str(title or "").casefold())


def _read_training_rows(tuning_dir: Path, category_model: Any) -> list[dict[str, Any]]:
    from train_issue_category_model import LABEL_DELETE, _normalise_label, _read_csv_dict_rows

    source_dirs = (tuning_dir / "CFE_reviewed_issue", tuning_dir / "CFE_input")
    collected: list[dict[str, Any]] = []
    for source_dir in source_dirs:
        for path in sorted(source_dir.glob("*.csv")):
            if path.name.casefold() in {POOL_NAME.casefold(), "weekly_labeling_template.csv"}:
                continue
            try:
                raw_rows = _read_csv_dict_rows(str(path))
            except Exception as exc:
                print(f"[WARN] Skipping unreadable feedback file {path.name}: {type(exc).__name__}")
                continue
            for raw in raw_rows:
                title = str(raw.get("ips_title") or "").strip()
                technology = str(raw.get("technology") or "").strip()
                label = _normalise_label(str(raw.get("human_category") or ""))
                description = str(raw.get("ips_description") or raw.get("description") or raw.get("jira_description") or "")
                if technology.strip().casefold() in {"software", "cie"}:
                    label = "ICPS/Killer"
                if not title or not label or label in LABEL_DELETE:
                    continue
                context = category_model.extract_structured_ips_context(title, description, "") if description else None
                collected.append({
                    "ips_case_number": str(raw.get("ips_case_number") or "").strip(),
                    "ips_title": title,
                    "technology": technology,
                    "human_category": label,
                    "description": description,
                    "structured_context": context,
                    "group": _title_key(title),
                    "source_file": path.name,
                    "reviewed_at": str(raw.get("reviewed_at") or "").strip(),
                })

    latest_by_case: dict[str, dict[str, Any]] = {}
    no_case_rows: list[dict[str, Any]] = []
    for row in collected:
        case_number = row["ips_case_number"]
        if not case_number:
            no_case_rows.append(row)
            continue
        previous = latest_by_case.get(case_number)
        if previous is None or row["reviewed_at"] >= previous["reviewed_at"]:
            latest_by_case[case_number] = row
    return [*latest_by_case.values(), *no_case_rows]


def _active_web_review_cases(dashboard: Any, active_model_path: Path) -> set[str]:
    reviews = dashboard._read_category_reviews()
    if not reviews:
        return set()
    modified_after = datetime.fromtimestamp(active_model_path.stat().st_mtime, tz=timezone.utc)
    new_cases = set()
    for case_number, review in reviews.items():
        try:
            reviewed_at = datetime.fromisoformat(str(review.get("reviewed_at") or "").replace("Z", "+00:00"))
            if reviewed_at.tzinfo is None:
                reviewed_at = reviewed_at.replace(tzinfo=timezone.utc)
            if reviewed_at > modified_after:
                new_cases.add(str(case_number).strip())
        except ValueError:
            continue
    return new_cases


def _candidate_oof(rows: list[dict[str, Any]], category_model: Any) -> list[str]:
    from sklearn.model_selection import GroupKFold

    if len({_title_key(row["ips_title"]) for row in rows}) < 5:
        raise ValueError("At least five normalized-title groups are required for GroupKFold.")
    titles = [row["ips_title"] for row in rows]
    labels = [row["human_category"] for row in rows]
    technologies = [row["technology"] for row in rows]
    contexts = [row["structured_context"] for row in rows]
    groups = [row["group"] for row in rows]
    predictions = [""] * len(rows)
    for train_ids, test_ids in GroupKFold(n_splits=5).split(titles, labels, groups):
        candidate = category_model.train_dual_feature_models(
            [titles[index] for index in train_ids],
            [labels[index] for index in train_ids],
            technologies=[technologies[index] for index in train_ids],
            contexts=[contexts[index] for index in train_ids],
            groups=[groups[index] for index in train_ids],
            detail_blend_weights={"title_only": 0.65, "with_details": 0.35},
        )
        for index in test_ids:
            rule = category_model._tier1_rule_category(titles[index], technologies[index])
            if rule:
                predictions[index] = rule
            else:
                predictions[index] = category_model._predict_dual_feature_model(
                    candidate, titles[index], technologies[index], contexts[index] or {},
                )[0]
    return predictions


def retrain_from_web_feedback(dashboard: Any, category_model: Any, tuning_dir: Path, min_new_labels: int = MIN_NEW_LABELS) -> dict[str, Any]:
    from sklearn.metrics import accuracy_score, classification_report, f1_score

    active_model_path = tuning_dir / "models" / "issue_category_model.joblib"
    metrics_path = tuning_dir / "models" / "issue_category_model_metrics.json"
    report: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "input_mode": "web_feedback_plus_existing_labeled_csvs",
        "splitter": "GroupKFold(5), normalized title",
        "min_new_labels": min_new_labels,
    }
    if not dashboard.CATEGORY_REVIEW_PATH.is_file():
        report.update(action="skip", reason="No web feedback CSV exists yet.", new_web_labels=0)
        _write_cycle_report(tuning_dir, report)
        print("No webpage feedback file found; active model unchanged.")
        return report

    new_review_cases = _active_web_review_cases(dashboard, active_model_path)
    report["new_web_labels"] = len(new_review_cases)
    if len(new_review_cases) < min_new_labels:
        report.update(action="skip", reason=f"Need at least {min_new_labels} new webpage labels before training.")
        _write_cycle_report(tuning_dir, report)
        print(f"Only {len(new_review_cases)} new webpage labels; need {min_new_labels}. Active model unchanged.")
        return report

    rows = _read_training_rows(tuning_dir, category_model)
    if not rows:
        raise RuntimeError("No usable human-labeled training rows found under CFE_reviewed_issue/CFE_input.")
    candidate_oof = _candidate_oof(rows, category_model)
    active_bundle = category_model.load_category_model(str(active_model_path))
    active_predictions = [
        category_model.classify_issue_title(
            active_bundle,
            row["ips_title"],
            technology=row["technology"],
            description=row.get("structured_context") or row["description"],
            use_llm=False,
        )[0]
        for row in rows
    ]
    review_indices = [
        index for index, row in enumerate(rows)
        if row["ips_case_number"] in new_review_cases
    ]
    if len(review_indices) < min_new_labels:
        report.update(action="skip", reason="New webpage reviews could not be matched to training rows.", matched_new_web_labels=len(review_indices))
        _write_cycle_report(tuning_dir, report)
        print(f"Only {len(review_indices)} new webpage reviews matched training rows; active model unchanged.")
        return report

    labels = [rows[index]["human_category"] for index in review_indices]
    candidate_new = [candidate_oof[index] for index in review_indices]
    active_new = [active_predictions[index] for index in review_indices]
    active_f1 = float(f1_score(labels, active_new, average="macro", zero_division=0))
    candidate_f1 = float(f1_score(labels, candidate_new, average="macro", zero_division=0))
    gain = candidate_f1 - active_f1
    recalls: dict[str, dict[str, Any]] = {}
    recall_ok = True
    for label in KEY_CLASSES:
        class_rows = [index for index, value in enumerate(labels) if value == label]
        if not class_rows:
            recalls[label] = {"status": "not_present"}
            continue
        class_gold = [labels[index] for index in class_rows]
        old_recall = sum(active_new[index] == label for index in class_rows) / len(class_rows)
        new_recall = sum(candidate_new[index] == label for index in class_rows) / len(class_rows)
        passed = new_recall >= old_recall - KEY_RECALL_DROP_TOLERANCE
        recalls[label] = {"support": len(class_rows), "active_recall": old_recall, "candidate_recall": new_recall, "status": "pass" if passed else "fail"}
        recall_ok = recall_ok and passed

    report.update({
        "training_rows": len(rows),
        "matched_new_web_labels": len(review_indices),
        "active_accuracy_on_new_web_labels": float(accuracy_score(labels, active_new)),
        "candidate_oof_accuracy_on_new_web_labels": float(accuracy_score(labels, candidate_new)),
        "active_macro_f1_on_new_web_labels": active_f1,
        "candidate_oof_macro_f1_on_new_web_labels": candidate_f1,
        "macro_f1_gain": gain,
        "minimum_macro_f1_gain": MIN_MACRO_F1_GAIN,
        "key_class_recall_tolerance": KEY_RECALL_DROP_TOLERANCE,
        "key_class_recalls": recalls,
        "promotion_passed": gain >= MIN_MACRO_F1_GAIN and recall_ok,
    })
    candidate_bundle = category_model.train_dual_feature_models(
        [row["ips_title"] for row in rows],
        [row["human_category"] for row in rows],
        technologies=[row["technology"] for row in rows],
        contexts=[row["structured_context"] for row in rows],
        groups=[row["group"] for row in rows],
        detail_blend_weights={"title_only": 0.65, "with_details": 0.35},
    )
    candidate_bundle.update({
        "trained_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "training_source": "web_feedback_cycle",
        "training_rows": len(rows),
        "new_web_labels": len(review_indices),
        "oof_grouping": "normalized_title",
    })
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    archive_dir = tuning_dir / "models" / "archive"
    candidate_dir = tuning_dir / "models" / "candidates"
    archive_dir.mkdir(parents=True, exist_ok=True)
    candidate_dir.mkdir(parents=True, exist_ok=True)
    candidate_path = candidate_dir / f"issue_category_model_web_candidate_{timestamp}.joblib"
    category_model.joblib.dump(candidate_bundle, candidate_path)
    report["candidate_model"] = str(candidate_path)

    if report["promotion_passed"]:
        targets = (
            (active_model_path, "tuning"),
            (dashboard.ROOT / "models" / "issue_category_model.joblib", "dashboard"),
        )
        for target, target_name in targets:
            target = target.resolve()
            if target.is_file():
                shutil.copy2(
                    target,
                    archive_dir / f"{target.stem}_{target_name}_pre_web_promotion_{timestamp}{target.suffix}",
                )
            temporary = target.with_name(target.name + ".web-feedback.tmp")
            category_model.joblib.dump(candidate_bundle, temporary)
            os.replace(temporary, target)
        key_report = classification_report(labels, candidate_new, output_dict=True, zero_division=0)
        metrics = {
            "accuracy": float(accuracy_score(labels, candidate_new)),
            "macro_f1": candidate_f1,
            "classification_report": key_report,
            "rows_used": len(rows),
            "test_rows_human": len(review_indices),
            "trained_at": datetime.now().strftime("%Y-%m-%d"),
            "dropped_rare_labels": [],
        }
        metrics_temp = metrics_path.with_suffix(".tmp")
        metrics_temp.write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(metrics_temp, metrics_path)
        report["action"] = "promote"
        print("Web-feedback dual model passed GroupKFold promotion gates and was promoted.")
    else:
        report["action"] = "keep_active"
        print("Web-feedback candidate did not pass OOF promotion gates; active model unchanged.")

    _write_cycle_report(tuning_dir, report)
    print(f"Web feedback report: {report['report_path']}")
    return report


def _write_cycle_report(tuning_dir: Path, report: dict[str, Any]) -> None:
    output_dir = tuning_dir / "tuning_outputs" / f"weekly_{datetime.now().strftime('%Y%m%d')}"
    output_dir.mkdir(parents=True, exist_ok=True)
    report["timestamp"] = report.get("generated_at", datetime.now(timezone.utc).isoformat(timespec="seconds"))
    report["human_responses_received"] = report.get("matched_new_web_labels", report.get("new_web_labels", 0))
    report["promoted"] = report.get("action") == "promote"
    path = output_dir / "model_promotion_decision.json"
    report["report_path"] = str(path)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Refresh the live 2026 web review pool or retrain from webpage feedback.")
    parser.add_argument("--mode", choices=("refresh-pool", "retrain"), required=True)
    parser.add_argument("--dashboard-root", default=os.environ.get("WIRELESS_CE_DASHBOARD_ROOT", ""))
    parser.add_argument("--min-new-labels", type=int, default=MIN_NEW_LABELS)
    args = parser.parse_args()

    dashboard_root = _dashboard_root(args.dashboard_root)
    category_model, dashboard = _load_runtime(dashboard_root)
    tuning_dir = Path(dashboard.CATEGORY_TUNING_DIR)
    if args.mode == "refresh-pool":
        refresh_web_pool(dashboard, tuning_dir)
        return 0

    refresh_web_pool(dashboard, tuning_dir)
    retrain_from_web_feedback(dashboard, category_model, tuning_dir, min_new_labels=args.min_new_labels)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())