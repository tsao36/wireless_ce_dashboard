from __future__ import annotations

import argparse
import csv
import glob
import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, List

from issue_category_model import _compose_feature_text

# ---------------------------------------------------------------------------
# Central label normalisation map.  All human_category values are mapped
# through this before being used for training or ingestion.  Update this
# whenever the team agrees on a new remapping decision.
# ---------------------------------------------------------------------------
LABEL_MAP: Dict[str, str] = {
    # case / typo normalisations
    "sensing": "Sensing",
    "P2p": "P2P",
    "p2p": "P2P",
    "Miracast": "P2P",
    "miracast": "P2P",
    "roaming": "Roaming",
    "WowLan": "WowLAN",
    "Wowlan": "WowLAN",
    "Power consumption": "Power Consumption",
    "Lost": "YB/Lost",
    "YB": "YB/Lost",
    "\nOEM Tools": "OEM Tools",
    # deliberate remappings
    "TAS": "UEFI",
    "Assert": "YB/Lost",
    "FW Assert": "YB/Lost",
    "D3": "Power Consumption",
    "Performance/P2P": "P2P",
    "Connectivity/P2P": "P2P",
    # Coex (BT/WiFi coexistence interference) is treated as Performance for now
    "Coex": "Performance",
    "Performance/Coex": "Performance",
    "BT/WiFi Coex": "Performance",
    "Coexistence": "Performance",
    # out-of-scope triage aliases
    "Needs-Triage": "Need-Triage",
    "needs-triage": "Need-Triage",
    "need-triage": "Need-Triage",
    "Unknown": "Need-Triage",
    "unknown": "Need-Triage",
    "Not-Wireless": "Need-Triage",
    "not-wireless": "Need-Triage",
    "Not Wireless": "Need-Triage",
    "not wireless": "Need-Triage",
    "Not WiFi Issue": "Need-Triage",
    "Not BT Issue": "Need-Triage",
    # ICPS and Killer are treated as one category
    "ICPS": "ICPS/Killer",
    "icps": "ICPS/Killer",
    "Killer": "ICPS/Killer",
    "killer": "ICPS/Killer",
    "icps/killer": "ICPS/Killer",
}

# Labels that should be dropped from training entirely
LABEL_DELETE: frozenset = frozenset({
    "fuck", "Cant judge", "Can't judge", "Can\u2019t judge", "Can\x92t judge",
    "dummy", "3rd-party", "System/platform", "FW/Driver", "PHY/Regulatory",
    "Tools", "Driver", "FW/Protocol", "SoftAP", "BT", "WRT", "OS", "Extension",
    "ANT Tool", "Not WiFi Issue", "Platform", "Connectivity Enterprise",
})


def _normalise_label(raw: str) -> str:
    """Strip surrounding quotes, apply LABEL_MAP, return empty string if label should be deleted."""
    v = raw.strip()
    # strip wrapping single quotes e.g. 'YB/Lost' -> YB/Lost
    if len(v) >= 2 and v[0] == "'" and v[-1] == "'":
        v = v[1:-1].strip()
    # strip leading newline
    v = v.lstrip("\n").strip()
    # apply map
    v = LABEL_MAP.get(v, v)
    # mark deletable labels as empty
    if v in LABEL_DELETE:
        return ""
    return v


def _try_import_ml() -> Dict[str, Any]:
    try:
        import joblib  # type: ignore
        from sklearn.feature_extraction.text import TfidfVectorizer  # type: ignore
        from sklearn.linear_model import LogisticRegression  # type: ignore
        from sklearn.metrics import accuracy_score, classification_report  # type: ignore
        from sklearn.model_selection import train_test_split  # type: ignore
        from sklearn.pipeline import Pipeline  # type: ignore
    except Exception as exc:  # pragma: no cover
        raise RuntimeError(
            "Missing dependencies. Install with: pip install scikit-learn joblib"
        ) from exc

    return {
        "joblib": joblib,
        "TfidfVectorizer": TfidfVectorizer,
        "LogisticRegression": LogisticRegression,
        "accuracy_score": accuracy_score,
        "classification_report": classification_report,
        "train_test_split": train_test_split,
        "Pipeline": Pipeline,
    }


def _clean_text(value: Any) -> str:
    text = str(value or "").strip()
    if text.upper() == "NA":
        return ""
    return text


def _normalize_technology(value: Any) -> str:
    raw = _clean_text(value).lower()
    aliases = {
        "software": "software",
        "icps/killer": "software",
        "sw": "software",
        "wifi": "wifi",
        "wi-fi": "wifi",
        "wlan": "wifi",
        "bt": "bt",
        "bluetooth": "bt",
    }
    return aliases.get(raw, raw)


def _read_csv_dict_rows(path: str) -> List[Dict[str, Any]]:
    encodings = ["utf-8-sig", "cp1252", "latin-1"]
    last_error: Exception | None = None
    for enc in encodings:
        try:
            with open(path, "r", encoding=enc, newline="") as handle:
                return list(csv.DictReader(handle))
        except UnicodeDecodeError as exc:
            last_error = exc
            continue
    if last_error is not None:
        raise last_error
    return []


def _load_training_rows(input_dirs: List[str]) -> List[Dict[str, str]]:
    csv_files: List[str] = []
    for input_dir in input_dirs:
        pattern = os.path.join(input_dir, "*.csv")
        csv_files.extend([p for p in glob.glob(pattern) if not os.path.basename(p).startswith("~$")])
    if not csv_files:
        raise FileNotFoundError(f"No CSV files found under: {', '.join(input_dirs)}")

    rows: List[Dict[str, str]] = []
    for path in sorted(csv_files):
        data_rows = _read_csv_dict_rows(path)
        if not data_rows:
            continue
        fieldnames = set(data_rows[0].keys())
        if "ips_title" not in fieldnames or "human_category" not in fieldnames:
            continue
        for row in data_rows:
            title = _clean_text(row.get("ips_title"))
            description = _clean_text(row.get("ips_description") or row.get("description") or row.get("jira_description"))
            human_category = _normalise_label(_clean_text(row.get("human_category")))
            technology = _clean_text(row.get("technology"))

            # Business rule: any software technology row is deterministically ICPS/Killer.
            if _normalize_technology(technology) == "software":
                human_category = "ICPS/Killer"

            if not title or not human_category:
                continue
            rows.append(
                {
                    "ips_title": title,
                    "predicted_category": _clean_text(row.get("predicted_category")),
                    "technology": technology,
                    "human_category": human_category,
                    "source_file": os.path.basename(path),
                    "feature_text": _compose_feature_text(
                        title,
                        predicted_category=_clean_text(row.get("predicted_category")),
                        technology=technology,
                        description=description,
                    ),
                }
            )

    if not rows:
        raise RuntimeError("No usable rows found. Required columns: ips_title, human_category")
    return rows


def _load_pseudo_rows(pseudo_csv: str, pseudo_weight: float) -> List[Dict[str, Any]]:
    if not pseudo_csv or not os.path.exists(pseudo_csv):
        return []

    data_rows = _read_csv_dict_rows(pseudo_csv)
    if not data_rows:
        return []

    fieldnames = set(data_rows[0].keys())
    if "ips_title" not in fieldnames or "human_category" not in fieldnames:
        return []

    rows: List[Dict[str, Any]] = []
    for row in data_rows:
        title = _clean_text(row.get("ips_title"))
        description = _clean_text(row.get("ips_description") or row.get("description") or row.get("jira_description"))
        human_category = _normalise_label(_clean_text(row.get("human_category")))
        technology = _clean_text(row.get("technology"))

        if _normalize_technology(technology) == "software":
            human_category = "ICPS/Killer"

        if not title or not human_category:
            continue

        feature_text = _clean_text(row.get("feature_text"))
        if not feature_text:
            feature_text = _compose_feature_text(
                title,
                predicted_category=_clean_text(row.get("predicted_category")),
                technology=technology,
                description=description,
            )

        row_weight_text = _clean_text(row.get("sample_weight"))
        row_weight = float(row_weight_text) if row_weight_text else float(pseudo_weight)

        rows.append(
            {
                "ips_title": title,
                "human_category": human_category,
                "feature_text": feature_text,
                "sample_weight": row_weight,
                "source_file": os.path.basename(pseudo_csv),
                "is_pseudo": True,
            }
        )

    return rows


def train_model(
    input_dirs: List[str],
    model_out: str,
    metrics_out: str,
    test_size: float,
    random_state: int,
    pseudo_csv: str = "",
    pseudo_weight: float = 1.0,
    enable_pseudo: bool = True,
    pseudo_cap_k: int = 1,
    tfidf_ngram_max: int = 1,
    tfidf_min_df: int = 1,
    lr_c: float = 2.0,
) -> Dict[str, Any]:
    ml = _try_import_ml()
    joblib = ml["joblib"]
    Pipeline = ml["Pipeline"]
    TfidfVectorizer = ml["TfidfVectorizer"]
    LogisticRegression = ml["LogisticRegression"]
    train_test_split = ml["train_test_split"]
    accuracy_score = ml["accuracy_score"]
    classification_report = ml["classification_report"]

    data = _load_training_rows(input_dirs)
    pseudo_rows = _load_pseudo_rows(pseudo_csv, pseudo_weight) if enable_pseudo else []

    # Keep labels with enough support in both train and test folds.
    # Require >= 5 samples so stratified split produces at least 1 test sample
    # and the model can learn a meaningful pattern per class.
    _MIN_CLASS_SUPPORT = 5
    label_counts: Dict[str, int] = {}
    for row in data:
        label = row["human_category"]
        label_counts[label] = label_counts.get(label, 0) + 1

    dropped_rare = sorted({k for k, v in label_counts.items() if v < _MIN_CLASS_SUPPORT})
    filtered = [row for row in data if label_counts.get(row["human_category"], 0) >= _MIN_CLASS_SUPPORT]

    unique_labels = sorted({row["human_category"] for row in filtered})
    if len(unique_labels) < 2:
        raise RuntimeError("Need at least 2 categories with >=2 samples to train/evaluate.")

    X = [row["feature_text"] for row in filtered]
    y = [row["human_category"] for row in filtered]

    X_train, X_test, y_train, y_test = train_test_split(
        X,
        y,
        test_size=test_size,
        random_state=random_state,
        stratify=y,
    )

    X_train_aug = list(X_train)
    y_train_aug = list(y_train)
    w_train_aug: List[float] = [1.0] * len(X_train)

    pseudo_added = 0
    if pseudo_rows:
        pseudo_label_counts: Dict[str, int] = {}
        for row in pseudo_rows:
            label = row["human_category"]
            pseudo_label_counts[label] = pseudo_label_counts.get(label, 0) + 1
        pseudo_filtered = [r for r in pseudo_rows if pseudo_label_counts.get(r["human_category"], 0) >= _MIN_CLASS_SUPPORT]

        train_human_counts: Dict[str, int] = {}
        for label in y_train:
            train_human_counts[label] = train_human_counts.get(label, 0) + 1

        if pseudo_cap_k > 0:
            per_label_seen: Dict[str, int] = {}
            capped_rows: List[Dict[str, Any]] = []
            for row in sorted(pseudo_filtered, key=lambda r: (str(r.get("ips_title", "")), str(r.get("source_file", "")))):
                label = row["human_category"]
                human_count = train_human_counts.get(label, 0)
                if human_count <= 0:
                    continue
                cap = human_count * pseudo_cap_k
                seen = per_label_seen.get(label, 0)
                if seen >= cap:
                    continue
                per_label_seen[label] = seen + 1
                capped_rows.append(row)
            pseudo_filtered = capped_rows

        test_titles = {_clean_text(t) for t in X_test}
        for row in pseudo_filtered:
            # Defensive leakage guard: if exact feature text appears in test set, skip.
            if row["feature_text"] in test_titles:
                continue
            X_train_aug.append(row["feature_text"])
            y_train_aug.append(row["human_category"])
            w_train_aug.append(float(row.get("sample_weight", pseudo_weight)))
            pseudo_added += 1

    pipeline = Pipeline(
        steps=[
            (
                "tfidf",
                TfidfVectorizer(
                    ngram_range=(1, tfidf_ngram_max),
                    min_df=tfidf_min_df,
                    max_df=0.98,
                    sublinear_tf=True,
                    lowercase=True,
                ),
            ),
            (
                "clf",
                LogisticRegression(
                    max_iter=2000,
                    class_weight="balanced",
                    C=lr_c,
                ),
            ),
        ]
    )

    pipeline.fit(X_train_aug, y_train_aug, clf__sample_weight=w_train_aug)
    y_pred = pipeline.predict(X_test)

    acc = float(accuracy_score(y_test, y_pred))
    report_dict = classification_report(y_test, y_pred, output_dict=True, zero_division=0)

    artifact = {
        "pipeline": pipeline,
        "labels": unique_labels,
        "trained_at_utc": datetime.now(timezone.utc).isoformat(),
        "input_dirs": [os.path.abspath(p) for p in input_dirs],
        "pseudo_csv": os.path.abspath(pseudo_csv) if pseudo_csv else "",
        "enable_pseudo": bool(enable_pseudo),
        "pseudo_weight": float(pseudo_weight),
        "pseudo_cap_k": int(pseudo_cap_k),
        "pseudo_rows_loaded": int(len(pseudo_rows)),
        "pseudo_rows_used": int(pseudo_added),
        "tfidf_ngram_max": int(tfidf_ngram_max),
        "tfidf_min_df": int(tfidf_min_df),
        "lr_c": float(lr_c),
        "rows_total": int(len(data)),
        "rows_used": int(len(filtered)),
        "train_rows_human": int(len(X_train)),
        "train_rows_total": int(len(X_train_aug)),
        "test_rows_human": int(len(X_test)),
        "dropped_rare_labels": dropped_rare,
        "test_size": float(test_size),
        "random_state": int(random_state),
    }

    os.makedirs(os.path.dirname(model_out) or ".", exist_ok=True)
    joblib.dump(artifact, model_out)

    metrics = {
        "accuracy": acc,
        "labels": artifact["labels"],
        "rows_total": artifact["rows_total"],
        "rows_used": artifact["rows_used"],
        "pseudo_csv": artifact["pseudo_csv"],
        "enable_pseudo": artifact["enable_pseudo"],
        "pseudo_weight": artifact["pseudo_weight"],
        "pseudo_cap_k": artifact["pseudo_cap_k"],
        "pseudo_rows_loaded": artifact["pseudo_rows_loaded"],
        "pseudo_rows_used": artifact["pseudo_rows_used"],
        "tfidf_ngram_max": artifact["tfidf_ngram_max"],
        "tfidf_min_df": artifact["tfidf_min_df"],
        "lr_c": artifact["lr_c"],
        "train_rows_human": artifact["train_rows_human"],
        "train_rows_total": artifact["train_rows_total"],
        "test_rows_human": artifact["test_rows_human"],
        "dropped_rare_labels": artifact["dropped_rare_labels"],
        "test_size": artifact["test_size"],
        "random_state": artifact["random_state"],
        "classification_report": report_dict,
    }

    os.makedirs(os.path.dirname(metrics_out) or ".", exist_ok=True)
    with open(metrics_out, "w", encoding="utf-8") as handle:
        json.dump(metrics, handle, indent=2, ensure_ascii=False)

    return metrics


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Train IPS issue-category model from CFE_input CSVs")
    parser.add_argument("--input-dir", default="", help="Deprecated single input dir; use --input-dirs")
    parser.add_argument("--input-dirs", default="CFE_reviewed_issue,CFE_input", help="Comma-separated training folders")
    parser.add_argument("--pseudo-csv", default="high_confidence_training_from_component_map.csv")
    parser.add_argument("--pseudo-weight", type=float, default=1.0)
    parser.add_argument("--pseudo-cap-k", type=int, default=1, help="Cap pseudo rows per label to k * train_human_count; 0 disables cap")
    parser.add_argument("--disable-pseudo", action="store_true")
    parser.add_argument("--tfidf-ngram-max", type=int, default=1)
    parser.add_argument("--tfidf-min-df", type=int, default=1)
    parser.add_argument("--lr-c", type=float, default=2.0)
    parser.add_argument("--model-out", default=os.path.join("models", "issue_category_model.joblib"))
    parser.add_argument("--metrics-out", default=os.path.join("models", "issue_category_model_metrics.json"))
    parser.add_argument("--test-size", type=float, default=0.2)
    parser.add_argument("--random-state", type=int, default=42)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    input_dirs = [p.strip() for p in str(args.input_dirs).split(",") if p.strip()]
    if args.input_dir:
        input_dirs = [args.input_dir]
    metrics = train_model(
        input_dirs=input_dirs,
        model_out=args.model_out,
        metrics_out=args.metrics_out,
        test_size=args.test_size,
        random_state=args.random_state,
        pseudo_csv=args.pseudo_csv,
        pseudo_weight=args.pseudo_weight,
        enable_pseudo=not args.disable_pseudo,
        pseudo_cap_k=args.pseudo_cap_k,
        tfidf_ngram_max=args.tfidf_ngram_max,
        tfidf_min_df=args.tfidf_min_df,
        lr_c=args.lr_c,
    )
    print("Training complete")
    print(f"Accuracy: {metrics['accuracy']:.4f}")
    print(f"Rows used: {metrics['rows_used']} (from {metrics['rows_total']})")
    print(
        "Pseudo rows: "
        f"loaded={metrics.get('pseudo_rows_loaded', 0)} "
        f"used={metrics.get('pseudo_rows_used', 0)} "
        f"weight={metrics.get('pseudo_weight', 0.0)} "
        f"cap_k={metrics.get('pseudo_cap_k', 0)}"
    )
    print(
        "Model params: "
        f"ngram_max={metrics.get('tfidf_ngram_max')} "
        f"min_df={metrics.get('tfidf_min_df')} "
        f"lr_c={metrics.get('lr_c')}"
    )
    print(f"Model: {args.model_out}")
    print(f"Metrics: {args.metrics_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
