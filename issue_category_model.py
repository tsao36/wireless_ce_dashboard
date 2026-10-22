from __future__ import annotations

import json
import os
import re
import logging
import urllib.request
import urllib.error
import urllib.parse
from collections import Counter
from typing import Any, Dict, Iterable, List, Tuple

import numpy as np
from sklearn.base import BaseEstimator, ClassifierMixin, clone
from sklearn.feature_extraction import DictVectorizer
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import GroupKFold
from sklearn.pipeline import FeatureUnion, Pipeline
from sklearn.preprocessing import FunctionTransformer

try:
    import joblib  # type: ignore
except Exception:  # pragma: no cover
    joblib = None

try:
    import openai  # type: ignore
except Exception:  # pragma: no cover
    openai = None

try:
    from dotenv import load_dotenv  # type: ignore
except Exception:  # pragma: no cover
    load_dotenv = None


if load_dotenv is not None:
    _BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    load_dotenv(dotenv_path=os.path.join(_BASE_DIR, ".env"), override=False)


_LOG = logging.getLogger(__name__)
# Prefer GNAI; ExpertGPT settings are used only when no GNAI token is configured.
if (os.getenv("GNAI_TOKEN") or "").strip():
    _DEFAULT_BASE_URL = (os.getenv("GNAI_URL") or "https://gnai.intel.com/api/providers/openai/v1").strip()
    _DEFAULT_MODEL = (os.getenv("GNAI_MODEL") or "gpt-4.1").strip()
    _DEFAULT_API_KEY = (os.getenv("GNAI_TOKEN") or "").strip()
else:
    _DEFAULT_BASE_URL = (os.getenv("EXPERTGPT_URL") or "https://expertgpt.intel.com").strip()
    _DEFAULT_MODEL = (os.getenv("EXPERTGPT_MODEL") or os.getenv("MODEL") or "").strip()
    _DEFAULT_API_KEY = (os.getenv("EXPERTGPT_TOKEN") or "").strip()
_PREDICT_BACKEND = (os.getenv("ISSUE_CATEGORY_PREDICT_BACKEND") or "ml").strip().lower()
_LLM_TIMEOUT_SECONDS = float((os.getenv("ISSUE_CATEGORY_LLM_TIMEOUT_SEC") or "30").strip() or 30)
PROMPT_VERSION = "v2.1.1_definitions_hierarchy"
_LLM_CLIENT: Any = None
# GNAI/ExpertGPT are Intel-internal; the corporate proxy answers them with HTTP 403.
_HTTP_OPENER = (
    urllib.request.build_opener(urllib.request.ProxyHandler({}))
    if (urllib.parse.urlparse(_DEFAULT_BASE_URL).hostname or "").endswith("intel.com")
    else urllib.request.build_opener()
)

CATEGORY_DEFINITIONS: Dict[str, str] = {
    "Audio": "Use for Bluetooth audio-path failures such as missing prompt/notification sound, microphone, speaker, mute, or codec behavior; an OEM app that triggers the audio symptom does not make it OEM Tools.",
    "BIOS": "Use for an in-BIOS setup item, firmware table, or runtime BIOS interface defect such as bSmbios or DSM; BIOS POST and pre-boot failures belong to UEFI.",
    "BSOD": "Use only when Windows crashes with a bugcheck, stop code, or crash dump; Code 10/43 and a missing adapter are YB/Lost, not BSOD.",
    "Connectivity": "Use for general association, authentication, DHCP, or disconnect failures without a specific cause; generic disconnects with 802.11r merely enabled stay Connectivity.",
    "Field Issue": "Use when the issue is explicitly classified as a field/customer deployment issue; do not use as a substitute for its technical root-cause category.",
    "HLK": "Use for failures in Windows Hardware Lab Kit certification or test runs; do not use for a failure seen only in ordinary customer use.",
    "ICPS/Killer": "Use for the Killer/ICPS software suite, service, installation, or UI; do not use for OEM utilities, which are OEM Tools.",
    "IOP": "Use for a confirmed interoperability defect between the wireless device and a specific peer, access point, or platform; use Connectivity when only a generic connection failure is known.",
    "MSFT": "Use when evidence identifies a Microsoft-owned component or confirmed Microsoft root cause; a Windows version or build alone is not sufficient.",
    "Need-Triage": "Use when evidence is insufficient to assign a wireless category or the issue is confirmed unrelated to Wi-Fi/BT; do not use merely because symptoms are complex.",
    "OEM Tools": "Use when a named OEM utility is the issue or test surface, including Vantage or ANT Tool, even if it configures antennas/bands; distinguish it from Intel Killer/ICPS software, which is ICPS/Killer.",
    "P2P": "Use when Wi-Fi Direct, Miracast, SoftAP, or another peer-to-peer mode is central; band/channel details alone do not turn a P2P-mode question into RF.",
    "Performance": "Use for measured throughput/TPT degradation or latency/ping spikes while the link remains connected; radio, antenna, band, or regulatory defects are RF.",
    "Power Consumption": "Use for excessive wireless power draw, battery drain, or energy use; classify an S3/S4 wake or adapter failure by its more specific root cause.",
    "RF": "Use for a demonstrated radio/antenna fault, measured RF behavior, radio-regulatory limits, or certification requests; do not use as a catch-all for SoftAP/P2P, sensing, roaming, or generic slow/disconnected Wi-Fi.",
    "Roaming": "Use for a failure during an observed AP handoff or an explicit 802.11r/FT EAP authentication or reassociation failure; mere 802.11r environment context with generic disconnects is Connectivity.",
    "Sensing": "Use for explicit presence/proximity sensor behavior, TAS, or a sensing measurement; fluctuating Wi-Fi signal strength alone is not Sensing.",
    "System Hang": "Use when the operating system or system becomes unresponsive without a bugcheck; the word 'code' or 'stuck' alone is not YB/Lost.",
    "UEFI": "Use for BIOS POST, boot-time, or pre-boot UEFI services/configuration; reserve BIOS for in-BIOS setup, tables, or runtime interfaces.",
    "WowLAN": "Use for Wake-on-WLAN behavior, wake patterns, or wireless wake from sleep; use Connectivity for failures after the system is awake.",
    "YB/Lost": "Use only for Yellow Bang, a specific Device Manager Code 10/43, confirmed adapter disappearance, or PLDR failure; generic 'code', UI icon flicker, or 'stuck' wording is not enough, and a bugcheck is BSOD.",
}


def _build_llm_system_prompt(categories: List[str]) -> str:
    definitions = "\n".join(
        f"- {category}: {CATEGORY_DEFINITIONS.get(category, 'Use only when this exact taxonomy label is supported by direct evidence.') }"
        for category in categories
    )
    return (
        "You classify IPS wireless issues. Choose exact labels only from the supplied allowed-category list, "
        "using the title and cleaned IPS details as evidence. Do not use or repeat any ML prediction or human label.\n\n"
        "Category boundaries:\n"
        f"{definitions}\n\n"
        "When symptoms cascade, assign primary_category by this strict priority (root cause/critical failure over general symptom):\n"
        "1. BSOD: system crash, bugcheck, stop code, or crash dump.\n"
        "2. YB/Lost: Yellow Bang, Code 10/43, device missing/disappeared, or PLDR failure.\n"
        "3. UEFI / BIOS: pre-boot UEFI or BIOS table/interface root cause.\n"
        "4. Specific feature/protocol: Roaming, P2P, WowLAN, Sensing, RF, Audio, OEM Tools, or another explicitly supported specialized taxonomy category.\n"
        "5. Performance: measured throughput/TPT drop or latency/ping spikes while connected.\n"
        "6. Connectivity: general association, authentication, DHCP, or disconnect when no higher-priority category applies.\n"
        "7. Need-Triage: insufficient evidence or confirmed non-Wi-Fi/BT issue.\n"
        "4 also covers explicit Power Consumption, Field Issue, HLK, IOP, MSFT, System Hang, and ICPS/Killer cases when their definitions directly fit.\n"
        "When a higher-priority primary wins, include a separately evidenced lower-priority symptom as secondary_category "
        "(for example, Roaming primary with a distinct general Connectivity symptom). Do not repeat the primary or list a speculative alternative.\n"
        "A named SoftAP/P2P mode outranks incidental band/channel words; a named OEM utility or Bluetooth audio failure outranks incidental RF/tool context.\n\n"
        "Return only one JSON object with this exact schema and no markdown or extra keys:\n"
        '{"primary_category":"<exact category name>","secondary_category":"<exact category name or null>",'
        '"confidence":0.0,"evidence_quote":"<short exact quote from title or details>",'
        '"reasoning":"<one sentence>"}'
    )


def _clean_text(value: Any) -> str:
    text = str(value or "").strip()
    if text.upper() == "NA":
        return ""
    return text


def _compose_feature_text(
    title: str,
    predicted_category: str = "",
    technology: str = "",
    description: str = "",
) -> str:
    title_part = _clean_text(title)
    desc_part = _clean_text(description)
    llm_part = _clean_text(predicted_category)
    tech_part = _clean_text(technology)
    if desc_part:
        return f"[TECH={tech_part}] [LLM={llm_part}] [DESC={desc_part}] {title_part}".strip()
    return f"[TECH={tech_part}] [LLM={llm_part}] {title_part}".strip()


_IPS_SIGNATURE_PATTERNS: List[re.Pattern[str]] = [
    re.compile(r"\bBSOD\b|\bbugcheck\b|\bnetwtw\d*\.sys\b|\bbtmhsf\.sys\b", re.IGNORECASE),
    re.compile(r"\bCode\s*(?:10|43)\b|yellow\s*(?:bang|bang!|mark)|\bYB\b|device disappeared|\bPLDR\b", re.IGNORECASE),
    re.compile(r"\b5002-FW_ASSERT\b|\b5055-FW_HW_ERROR\b|\bEvent\s*ID\s*\d+\b|\bAssert\b", re.IGNORECASE),
    re.compile(r"\b0x[0-9A-Fa-f]{4,8}\b"),
    re.compile(r"\bbSmbios\b|\bDSM\b|\bTAS\b|\bSAR\b|\b6\s*GHz\b|\bUEFI\b", re.IGNORECASE),
]
_IPS_HEADER_PATTERNS: List[re.Pattern[str]] = [
    re.compile(r"^\s*(?:from|sent|to|cc|subject|date)\s*:\s*", re.IGNORECASE),
    re.compile(r"^\s*(?:please\s+(?:provide|describe|include|attach)|describe\s+the\s+issue|steps\s+to\s+reproduce)\b", re.IGNORECASE),
    re.compile(r"^\s*(?:customer\s+information|problem\s+description|issue\s+description)\s*:?\s*$", re.IGNORECASE),
]
_IPS_SIGNATURE_ENDINGS = re.compile(
    r"^\s*(?:best\s+regards|kind\s+regards|regards|thanks|thank\s+you|confidentiality\s+notice|this\s+email\s+and\s+any\s+attachments)\b",
    re.IGNORECASE,
)
_IPS_ENV_KEYS = re.compile(
    r"^\s*(?:os(?: build| version)?|operating system|windows build|build number|driver(?: version)?|firmware(?: version)?|fw version|bios version|ap model|access point|router model)\s*[:=]",
    re.IGNORECASE,
)


def _strip_ips_template_boilerplate(raw_text: str) -> str:
    kept: List[str] = []
    in_signature = False
    for raw_line in str(raw_text or "").splitlines():
        line = re.sub(r"[ \t\xa0]+", " ", raw_line).strip()
        if not line:
            continue
        if _IPS_SIGNATURE_ENDINGS.search(line):
            in_signature = True
        if in_signature:
            continue
        if any(pattern.search(line) for pattern in _IPS_HEADER_PATTERNS):
            continue
        if re.match(r"^>\s*(?:from|sent|to|subject):", line, re.IGNORECASE):
            continue
        if len(line) > 300 or re.match(r"^(?:\d{4}[-/]\d\d[-/]\d\d[ T]|\[\d{2}:\d{2}:\d{2})", line):
            continue
        if re.search(r"(?:0x[0-9a-f]{6,}|[0-9a-f]{24,}|\\Device\\Harddisk|Call Stack|Stack trace)", line, re.IGNORECASE):
            continue
        kept.append(line)
    return "\n".join(kept)


def _extract_title_tags(title: str) -> Tuple[str, List[str]]:
    remaining = str(title or "").strip()
    tags: List[str] = []
    while True:
        match = re.match(r"^\s*\[([^\]]{1,80})\]\s*", remaining)
        if not match:
            break
        tags.append(match.group(1).strip())
        remaining = remaining[match.end():]
    return remaining.strip(), tags


def extract_structured_ips_context(title: str, raw_description: str, raw_env: str) -> Dict[str, Any]:
    """Extract title tags and compact symptom, signature, and environment zones from an IPS case."""
    clean_title, metadata_tags = _extract_title_tags(title)
    description = _strip_ips_template_boilerplate(str(raw_description or ""))
    env_text = _strip_ips_template_boilerplate(str(raw_env or ""))

    signature_source = "\n".join((str(raw_description or ""), str(raw_env or "")))
    signatures: List[str] = []
    seen_signatures: set[str] = set()
    for pattern in _IPS_SIGNATURE_PATTERNS:
        for match in pattern.finditer(signature_source):
            value = re.sub(r"\s+", " ", match.group(0)).strip()
            key = value.casefold()
            if key not in seen_signatures:
                seen_signatures.add(key)
                signatures.append(value)

    symptom_lines: List[str] = []
    env_lines: List[str] = []
    for line in description.splitlines():
        if _IPS_ENV_KEYS.search(line) or re.search(r"\b(?:Windows\s+\d|OS\s+build)\b", line, re.IGNORECASE):
            env_lines.append(line)
        else:
            symptom_lines.append(line)
    symptom_and_repro = "\n".join(symptom_lines).strip()[:5000]

    all_env_lines = [*env_lines, *env_text.splitlines()]
    selected_env: List[str] = []
    seen_env: set[str] = set()
    for line in all_env_lines:
        line = re.sub(r"\s+", " ", line).strip(" :;\t")
        if not line:
            continue
        if _IPS_ENV_KEYS.search(line) or re.search(
            r"\b(?:Windows\s+(?:10|11)|10\.0\.\d{4,6}|\d{1,2}\.\d{1,2}\.\d{3,6}\.\d{1,5}|\bAP\b.{0,20}\b[A-Z0-9-]{3,})",
            line,
            re.IGNORECASE,
        ):
            key = line.casefold()
            if key not in seen_env:
                seen_env.add(key)
                selected_env.append(line)
    env_summary = "\n".join(selected_env)[:1000]
    error_signatures = " | ".join(signatures)
    llm_context = "\n".join(
        part for part in (
            f"Symptom and reproduction:\n{symptom_and_repro}" if symptom_and_repro else "",
            f"Error signatures:\n{error_signatures}" if error_signatures else "",
            f"Environment:\n{env_summary}" if env_summary else "",
        ) if part
    )[:3000]
    ml_context = "\n".join(part for part in (symptom_and_repro, error_signatures) if part)[:1000]
    return {
        "clean_title": clean_title,
        "metadata_tags": metadata_tags,
        "symptom_and_repro": symptom_and_repro,
        "error_signatures": signatures,
        "env_summary": env_summary,
        "llm_context": llm_context,
        "ml_context": ml_context,
    }


_RULE_HINT_PATTERNS: List[Tuple[str, List[re.Pattern[str]]]] = [
    ("BSOD", [re.compile(r"\bBSOD\b|\bbugcheck\b|\bstop code\b|\bnetwtw\d*\.sys\b|\bbtmhsf\.sys\b", re.IGNORECASE)]),
    ("YB/Lost", [re.compile(
        r"\bCode\s*(?:10|43)\b|yellow\s*(?:bang|mark)|\bYB\b|device disappeared|\bPLDR\b|"
        r"device cannot start|driver\s+(?:error|fail(?:ed|ure)?|issue)|unknown USB device|驚嘆號|黃驚嘆號|黃色驚嘆號",
        re.IGNORECASE,
    )]),
    ("UEFI", [re.compile(r"\bUEFI\b|pre-boot", re.IGNORECASE)]),
    ("BIOS", [re.compile(r"\bBIOS\b|\bbSmbios\b|\bDSM\b", re.IGNORECASE)]),
    ("Roaming", [re.compile(r"\broam(?:ing|ed)?\b|\b802\.11r\b", re.IGNORECASE)]),
    ("P2P", [re.compile(r"\bMiracast\b|\bWi-?Fi\s+Direct\b|\bP2P\b", re.IGNORECASE)]),
    ("WowLAN", [re.compile(r"\bWoWLAN\b|\bWake\s+on\s+Wireless\b", re.IGNORECASE)]),
    ("Sensing", [re.compile(r"\b(?:presence|proximity|human)\s+sensing\b|\bsensing\b|\bTAS\b", re.IGNORECASE)]),
    ("RF", [re.compile(r"\b(?:RF|antenna|SAR|6\s*GHz|regulatory)\b", re.IGNORECASE)]),
    ("Audio", [re.compile(r"\b(?:audio|microphone|speaker|mute|no sound)\b", re.IGNORECASE)]),
    ("Performance", [re.compile(r"\b(?:throughput|TPT|latency|ping|performance|slow speed)\b", re.IGNORECASE)]),
    ("Connectivity", [
        re.compile(r"\b(?:can(?:not|'t)?\s+connect|unable\s+to\s+connect|fails?\s+to\s+connect)\b", re.IGNORECASE),
        re.compile(r"\b(?:disconnect(?:s|ed|ion)?|connection\s+lost|link\s+down)\b", re.IGNORECASE),
        re.compile(r"\b(?:no\s+internet|internet\s+not\s+available|limited\s+connectivity|unidentified\s+network|dhcp|ip\s+address\s+conflict|ip\s+assignment\s+fail(?:ed|ure)?|ssid\s+not\s+found|cannot\s+find\s+ssid|auth(?:entication)?\s+fail(?:ed|ure)?)\b", re.IGNORECASE),
        re.compile(r"連不上|無法連線|斷線|掉線|網路中斷|網路不穩|找不到\s*ssid|驗證失敗", re.IGNORECASE),
    ]),
]


def extract_rule_hints(clean_title: str, structured_context: Dict[str, Any], technology: str) -> Dict[str, Any]:
    """Return a categorical hint only when the detected category signature is unambiguous."""
    signatures = structured_context.get("error_signatures") or []
    if isinstance(signatures, str):
        signatures = [signatures]
    source = "\n".join([_clean_text(clean_title), *(_clean_text(item) for item in signatures)])
    detected: Dict[str, List[str]] = {}
    for category, patterns in _RULE_HINT_PATTERNS:
        matches = [match.group(0) for pattern in patterns for match in pattern.finditer(source)]
        if matches:
            detected[category] = list(dict.fromkeys(matches))
    hint = next(iter(detected)) if len(detected) == 1 else ""
    return {
        "predicted_category_hint": hint,
        "matched_signatures": list(dict.fromkeys(match for values in detected.values() for match in values)),
        "hint_candidates": list(detected),
        "technology": _norm_technology(technology),
    }


def _select_title_text(rows: List[Dict[str, Any]]) -> List[str]:
    return [str(row.get("title_text") or "") for row in rows]


def _select_detail_text(rows: List[Dict[str, Any]]) -> List[str]:
    return [str(row.get("detail_text") or "detail_missing") for row in rows]


def _select_signature_features(rows: List[Dict[str, Any]]) -> List[Dict[str, float]]:
    selected: List[Dict[str, float]] = []
    for row in rows:
        features: Dict[str, float] = {}
        if row.get("hint"):
            features[f"hint={row['hint']}"] = 1.0
        technology = str(row.get("technology") or "unknown")
        features[f"technology={technology}"] = 1.0
        for signature in row.get("signatures") or []:
            features[f"signature={str(signature).casefold()}"] = 1.0
        selected.append(features)
    return selected


def build_issue_feature_record(title: str, technology: str = "", structured_context: Dict[str, Any] | None = None) -> Dict[str, Any]:
    context = structured_context or extract_structured_ips_context(title, "", "")
    clean_title = _clean_text(context.get("clean_title")) or _extract_title_tags(title)[0]
    hints = extract_rule_hints(clean_title, context, technology)
    return {
        "title_text": f"[TECH={_norm_technology(technology)}] {clean_title}".strip(),
        "detail_text": str(context.get("ml_context") or "")[:1000],
        "hint": hints["predicted_category_hint"],
        "technology": hints["technology"],
        "signatures": [str(item).casefold() for item in context.get("error_signatures") or []],
    }


class OOFTemperatureScaledClassifier(ClassifierMixin, BaseEstimator):
    """Calibrate multiclass probabilities with OOF temperature scaling, including rare labels."""

    def __init__(self, estimator: Any, n_splits: int = 3) -> None:
        self.estimator = estimator
        self.n_splits = n_splits

    def fit(self, features: List[Dict[str, Any]], labels: List[str], groups: List[str] | None = None) -> "OOFTemperatureScaledClassifier":
        label_rows = np.asarray(labels, dtype=object)
        group_rows = np.asarray(groups if groups is not None else list(range(len(label_rows))), dtype=object)
        self.classes_ = np.unique(label_rows)
        class_indexes = {label: index for index, label in enumerate(self.classes_)}
        oof_probabilities = np.full((len(label_rows), len(self.classes_)), 1.0 / len(self.classes_))
        calibratable = np.zeros(len(label_rows), dtype=bool)
        group_count = len(np.unique(group_rows))
        split_count = min(int(self.n_splits), group_count)

        if split_count >= 2:
            splitter = GroupKFold(n_splits=split_count)
            for train_indexes, validation_indexes in splitter.split(features, label_rows, group_rows):
                fold_estimator = clone(self.estimator)
                fold_features = [features[index] for index in train_indexes]
                fold_labels = label_rows[train_indexes].tolist()
                fold_estimator.fit(fold_features, fold_labels)
                fold_probabilities = fold_estimator.predict_proba(
                    [features[index] for index in validation_indexes]
                )
                fold_indexes = {label: index for index, label in enumerate(fold_estimator.classes_)}
                for row_position, probabilities in zip(validation_indexes, fold_probabilities):
                    for label, class_index in class_indexes.items():
                        if label in fold_indexes:
                            oof_probabilities[row_position, class_index] = probabilities[fold_indexes[label]]
                    calibratable[row_position] = label_rows[row_position] in fold_indexes

        self.temperature_ = 1.0
        if calibratable.any() and len(self.classes_) > 1:
            valid_rows = np.flatnonzero(calibratable)
            log_probabilities = np.log(np.clip(oof_probabilities[valid_rows], 1e-12, 1.0))
            target_indexes = np.asarray([class_indexes[label_rows[index]] for index in valid_rows])
            best_loss = float("inf")
            for candidate in np.exp(np.linspace(-3.0, 3.0, 121)):
                scaled = log_probabilities / candidate
                scaled -= scaled.max(axis=1, keepdims=True)
                log_normalizer = np.log(np.exp(scaled).sum(axis=1))
                loss = float(np.mean(log_normalizer - scaled[np.arange(len(valid_rows)), target_indexes]))
                if loss < best_loss:
                    best_loss = loss
                    self.temperature_ = float(candidate)

        self.estimator_ = clone(self.estimator)
        self.estimator_.fit(features, label_rows.tolist())
        self.classes_ = self.estimator_.classes_
        return self

    def predict_proba(self, features: List[Dict[str, Any]]) -> np.ndarray:
        probabilities = np.clip(self.estimator_.predict_proba(features), 1e-12, 1.0)
        logits = np.log(probabilities) / self.temperature_
        logits -= logits.max(axis=1, keepdims=True)
        scaled = np.exp(logits)
        return scaled / scaled.sum(axis=1, keepdims=True)

    def predict(self, features: List[Dict[str, Any]]) -> np.ndarray:
        probabilities = self.predict_proba(features)
        return np.asarray(self.classes_)[probabilities.argmax(axis=1)]


def build_feature_fused_pipeline(
    *, include_detail_tfidf: bool = False, detail_min_df: int = 3,
    detail_max_features: int = 800, detail_weight: float = 0.35,
) -> Pipeline:
    """Create a title/signature classifier with an optional restrained detail branch."""
    title_tfidf = FeatureUnion([
        ("word", Pipeline([("select", FunctionTransformer(_select_title_text, validate=False)),
                           ("tfidf", TfidfVectorizer(ngram_range=(1, 2), sublinear_tf=True))])),
        ("char_wb", Pipeline([("select", FunctionTransformer(_select_title_text, validate=False)),
                               ("tfidf", TfidfVectorizer(analyzer="char_wb", ngram_range=(3, 5), sublinear_tf=True))])),
    ])
    transformers: List[Tuple[str, Any]] = [("title_tfidf", title_tfidf)]
    weights = {"title_tfidf": 2.0}
    if include_detail_tfidf:
        transformers.append(("detail_tfidf", Pipeline([
                ("select", FunctionTransformer(_select_detail_text, validate=False)),
                ("tfidf", TfidfVectorizer(
                    ngram_range=(1, 2), min_df=detail_min_df,
                    max_features=detail_max_features, sublinear_tf=True,
                )),
            ])))
        weights["detail_tfidf"] = detail_weight
    transformers.append(("signature_features", Pipeline([
                ("select", FunctionTransformer(_select_signature_features, validate=False)),
                ("encode", DictVectorizer()),
            ])))
    features = FeatureUnion(transformers, transformer_weights=weights)
    return Pipeline([("features", features), ("classifier", LogisticRegression(class_weight="balanced", max_iter=2000))])


def train_feature_fused_model(
    titles: Iterable[str], labels: Iterable[str], *, technologies: Iterable[str] | None = None,
    contexts: Iterable[Dict[str, Any] | None] | None = None,
    groups: Iterable[str] | None = None, include_detail_tfidf: bool = False,
) -> Dict[str, Any]:
    """Fit a structured model and temperature-calibrate it on grouped out-of-fold scores."""
    title_rows, label_rows = list(titles), [str(label) for label in labels]
    tech_rows = list(technologies) if technologies is not None else [""] * len(title_rows)
    context_rows = list(contexts) if contexts is not None else [None] * len(title_rows)
    group_rows = list(groups) if groups is not None else [re.sub(r"[^0-9a-z]+", "", title.lower()) for title in title_rows]
    if not title_rows or any(len(values) != len(title_rows) for values in (label_rows, tech_rows, context_rows, group_rows)):
        raise ValueError("Titles, labels, technologies, contexts, and groups must have the same non-zero row count.")
    records = [build_issue_feature_record(title, tech, context) for title, tech, context in zip(title_rows, tech_rows, context_rows)]
    base_pipeline = build_feature_fused_pipeline(include_detail_tfidf=include_detail_tfidf)
    pipeline = OOFTemperatureScaledClassifier(estimator=base_pipeline, n_splits=3)
    pipeline.fit(records, label_rows, groups=group_rows)
    return {
        "pipeline": pipeline,
        "labels": sorted(set(label_rows), key=str.casefold),
        "feature_mode": "structured_v1",
        "calibration_method": "oof_temperature_groupkfold3",
        "temperature": pipeline.temperature_,
        "detail_tfidf": {
            "enabled": include_detail_tfidf,
            "min_df": 3,
            "max_features": 800,
            "weight": 0.35,
        },
    }


def train_dual_feature_models(
    titles: Iterable[str], labels: Iterable[str], *, technologies: Iterable[str],
    contexts: Iterable[Dict[str, Any] | None], groups: Iterable[str],
    detail_blend_weights: Dict[str, float] | None = None,
) -> Dict[str, Any]:
    title_rows, label_rows = list(titles), list(labels)
    tech_rows, context_rows, group_rows = list(technologies), list(contexts), list(groups)
    title_only = train_feature_fused_model(
        title_rows, label_rows, technologies=tech_rows, groups=group_rows,
    )
    detail_indexes = [
        index for index, context in enumerate(context_rows)
        if context and context.get("ml_context")
    ]
    if not detail_indexes:
        raise ValueError("The detail estimator requires at least one verified IPS ML context.")
    with_details = train_feature_fused_model(
        title_rows, label_rows, technologies=tech_rows, contexts=context_rows,
        groups=group_rows, include_detail_tfidf=True,
    )
    blend_weights = detail_blend_weights or {"title_only": 0.6, "with_details": 0.4}
    return {
        "pipeline": title_only["pipeline"],
        "model_title_only": title_only["pipeline"],
        "model_with_details": with_details["pipeline"],
        "labels": title_only["labels"],
        "feature_mode": "dual_structured_v2",
        "detail_blend_weights": blend_weights,
        "detail_training_rows": len(detail_indexes),
        "model_with_details_training_rows": len(title_rows),
        "calibration_method": "oof_temperature_groupkfold3",
        "temperatures": {
            "title_only": title_only["temperature"],
            "with_details": with_details["temperature"],
        },
        "detail_tfidf": with_details["detail_tfidf"],
    }


def load_category_model(model_path: str) -> Dict[str, Any]:
    if joblib is None:
        raise RuntimeError("Missing dependency: joblib. Install with: pip install scikit-learn joblib")
    if not os.path.exists(model_path):
        raise FileNotFoundError(f"Model file not found: {model_path}")

    model = joblib.load(model_path)
    if isinstance(model, dict) and "pipeline" in model:
        return model
    return {"pipeline": model, "labels": []}


def _norm_key(value: str) -> str:
    return _clean_text(value).lower()


def _norm_technology(value: str) -> str:
    raw = _norm_key(value)
    aliases = {
        "wifi": "wifi",
        "wi-fi": "wifi",
        "wlan": "wifi",
        "bt": "bt",
        "bluetooth": "bt",
        "software": "software",
        "cie": "software",
        "icps/killer": "software",
        "tools": "tools",
        "product": "product",
    }
    return aliases.get(raw, raw)


def load_weight_map(weight_map_path: str) -> Tuple[Dict[str, float], Dict[str, float], float]:
    if not weight_map_path or not os.path.exists(weight_map_path):
        return {}, {}, 1.0

    with open(weight_map_path, "r", encoding="utf-8") as handle:
        payload = json.load(handle)

    if isinstance(payload, dict) and "category_weights" in payload:
        raw_map = payload.get("category_weights") or {}
        raw_category_tech_map = payload.get("category_technology_weights") or {}
        default_weight = float(payload.get("default_weight", 1.0) or 1.0)
    elif isinstance(payload, dict):
        raw_map = payload
        raw_category_tech_map = {}
        default_weight = 1.0
    else:
        raw_map = {}
        raw_category_tech_map = {}
        default_weight = 1.0

    normalized: Dict[str, float] = {}
    for key, value in raw_map.items():
        cat = _clean_text(key)
        if not cat:
            continue
        try:
            normalized[_norm_key(cat)] = float(value)
        except Exception:
            continue

    normalized_category_tech: Dict[str, float] = {}
    for key, value in raw_category_tech_map.items():
        key_text = _clean_text(key)
        if not key_text:
            continue
        if "|" in key_text:
            cat_text, tech_text = key_text.split("|", 1)
        else:
            # Backward-compatible: allow keys like "Connectivity@WiFi"
            cat_text, _, tech_text = key_text.partition("@")
        cat_key = _norm_key(cat_text)
        tech_key = _norm_technology(tech_text)
        if not cat_key or not tech_key:
            continue
        try:
            normalized_category_tech[f"{cat_key}|{tech_key}"] = float(value)
        except Exception:
            continue

    return normalized, normalized_category_tech, default_weight


def _predict_with_confidence(pipeline: Any, feature_input: Any) -> Tuple[str, float]:
    predicted = str(pipeline.predict([feature_input])[0])

    if hasattr(pipeline, "predict_proba"):
        probs = pipeline.predict_proba([feature_input])[0]
        try:
            confidence = float(max(probs))
        except Exception:
            confidence = 0.0
        return predicted, confidence

    if hasattr(pipeline, "decision_function"):
        scores = pipeline.decision_function([feature_input])
        try:
            values = scores[0] if hasattr(scores, "__len__") else scores
            if hasattr(values, "__len__") and len(values) > 1:
                top = float(max(values))
                confidence = 1.0 / (1.0 + pow(2.71828, -top))
            else:
                confidence = 0.5
        except Exception:
            confidence = 0.0
        return predicted, confidence

    return predicted, 0.0


def _predict_dual_feature_model(
    model_bundle: Dict[str, Any], title: str, technology: str, context: Dict[str, Any],
) -> Tuple[str, float, str | None]:
    title_model = model_bundle["model_title_only"]
    detail_model = model_bundle["model_with_details"]
    title_record = build_issue_feature_record(title, technology)
    title_probabilities = title_model.predict_proba([title_record])[0]
    categories = [str(label) for label in title_model.classes_]
    title_map = {str(label): float(value) for label, value in zip(title_model.classes_, title_probabilities)}
    has_ml_details = bool(context.get("ml_context"))
    if not has_ml_details:
        probabilities = np.asarray([title_map.get(category, 0.0) for category in categories])
    else:
        detail_record = build_issue_feature_record(title, technology, context)
        detail_probabilities = detail_model.predict_proba([detail_record])[0]
        detail_map = {str(label): float(value) for label, value in zip(detail_model.classes_, detail_probabilities)}
        weights = model_bundle.get("detail_blend_weights") or {"title_only": 0.6, "with_details": 0.4}
        probabilities = np.asarray([
            float(weights["title_only"]) * title_map.get(category, 0.0)
            + float(weights["with_details"]) * detail_map.get(category, 0.0)
            for category in categories
        ])
        total = float(probabilities.sum())
        if total:
            probabilities /= total
    winner = int(probabilities.argmax())
    if len(categories) < 2:
        return categories[winner], float(probabilities[winner]), None
    runner_up = int(np.argsort(probabilities)[-2])
    return categories[winner], float(probabilities[winner]), categories[runner_up]


# Maps stale/legacy model output labels to the current canonical category names
# in bug_category_config.json. Update this when the model produces labels that
# no longer match the config (e.g. after a category rename or model retrain
# with outdated training data).
_LEGACY_CATEGORY_MAP: Dict[str, str] = {
    "yb": "YB/Lost",
    "icps": "ICPS/Killer",
    "killer": "ICPS/Killer",
    "icps/killer": "ICPS/Killer",
    "miracast": "P2P",
    "not wifi issue": "Need-Triage",
    "not bt issue": "Need-Triage",
    "not-wireless": "Need-Triage",
    "not wireless": "Need-Triage",
    "unknown": "Need-Triage",
    "needs-triage": "Need-Triage",
    "layout": "Need-Triage",
    "schematic": "Need-Triage",
}


# High-precision symptom overrides to protect obvious failure signatures from
# being misrouted by contextual keywords (for example, "roaming").
_YB_LOST_PATTERNS: List[re.Pattern[str]] = [
    re.compile(r"\byellow\s*(?:mark|bang|!|exclamation)\b", re.IGNORECASE),
    re.compile(r"\bcode\s*(?:10|43)\b", re.IGNORECASE),
    re.compile(r"\bdevice\s+cannot\s+start\b", re.IGNORECASE),
    re.compile(r"\b(this\s+device\s+cannot\s+start)\b", re.IGNORECASE),
    re.compile(r"\bdriver\s+(?:error|fail(?:ed|ure)?|issue)\b", re.IGNORECASE),
    re.compile(r"\bunknown\s+usb\s+device\b", re.IGNORECASE),
    re.compile(r"\b(?:驚嘆號|黃驚嘆號|黃色驚嘆號)\b", re.IGNORECASE),
]

_CONNECTIVITY_PATTERNS: List[re.Pattern[str]] = [
    re.compile(r"\b(?:can(?:not|'t)?\s+connect|unable\s+to\s+connect|fails?\s+to\s+connect)\b", re.IGNORECASE),
    re.compile(r"\b(?:disconnect(?:ed|ion)?|connection\s+lost|link\s+down)\b", re.IGNORECASE),
    re.compile(r"\b(?:no\s+internet|internet\s+not\s+available|limited\s+connectivity|unidentified\s+network)\b", re.IGNORECASE),
    re.compile(r"\b(?:dhcp|ip\s+address\s+conflict|ip\s+assignment\s+fail(?:ed|ure)?)\b", re.IGNORECASE),
    re.compile(r"\b(?:ssid\s+not\s+found|cannot\s+find\s+ssid|auth(?:entication)?\s+fail(?:ed|ure)?)\b", re.IGNORECASE),
    re.compile(r"\b(?:連不上|無法連線|斷線|掉線|網路中斷|網路不穩|找不到\s*ssid|驗證失敗)\b", re.IGNORECASE),
]


def _override_category_from_text(title: str, description: str = "") -> str:
    context = extract_structured_ips_context(title, str(description or ""), "")
    hint = extract_rule_hints(context["clean_title"], context, "")["predicted_category_hint"]
    return hint if hint in {"YB/Lost", "Connectivity"} else ""


def _safe_parse_json(payload: str) -> Dict[str, Any]:
    text = str(payload or "").strip()
    if not text:
        return {}
    try:
        return json.loads(text)
    except Exception:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except Exception:
                return {}
        return {}


def _get_llm_client() -> Any:
    global _LLM_CLIENT
    if _LLM_CLIENT is not None:
        return _LLM_CLIENT
    if openai is None:
        return None
    if not _DEFAULT_MODEL:
        return None
    if not _DEFAULT_API_KEY:
        return None
    if (urllib.parse.urlparse(_DEFAULT_BASE_URL).hostname or "").endswith("intel.com"):
        return None
    try:
        _LLM_CLIENT = openai.OpenAI(api_key=_DEFAULT_API_KEY, base_url=_DEFAULT_BASE_URL, timeout=_LLM_TIMEOUT_SECONDS)
        return _LLM_CLIENT
    except Exception as exc:
        _LOG.warning("LLM client init failed, fallback to ML: %s", exc)
        return None


def _llm_predict_category_response(
    *,
    title: str,
    description: str | Dict[str, Any],
    predicted_category: str,
    technology: str,
    categories: List[str],
    model: str = "",
) -> Dict[str, Any] | None:
    model = model or _DEFAULT_MODEL
    allowed = [c for c in (categories or []) if _clean_text(c)]
    if not allowed:
        return None

    system_prompt = _build_llm_system_prompt(allowed)
    description_text = (
        str(description.get("llm_context") or "")
        if isinstance(description, dict)
        else _clean_text(description)
    )
    user_prompt = (
        "Classify this IPS issue.\n"
        f"Technology: {_clean_text(technology) or 'Unknown'}\n"
        f"Title: {_clean_text(title) or '(empty)'}\n"
        f"Description: {description_text or '(empty)'}\n\n"
        "Allowed categories:\n"
        + "\n".join(f"- {c}" for c in allowed)
    )

    client = _get_llm_client()
    content = ""
    if client is not None:
        try:
            resp = client.chat.completions.create(
                model=model,
                temperature=0.0,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
            )
            content = str(resp.choices[0].message.content or "")
        except Exception as exc:
            _LOG.warning("LLM predict via openai SDK failed, trying HTTP fallback: %s", exc)
            content = ""
    if not content and model and _DEFAULT_API_KEY:
        payload = {
            "model": model,
            "temperature": 0.0,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
        url = _DEFAULT_BASE_URL.rstrip("/") + "/chat/completions"
        for attempt in range(2):
            req = urllib.request.Request(
                url,
                data=json.dumps(payload).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {_DEFAULT_API_KEY}",
                },
                method="POST",
            )
            try:
                with _HTTP_OPENER.open(req, timeout=max(5.0, _LLM_TIMEOUT_SECONDS)) as resp:
                    payload_resp = json.loads(resp.read().decode("utf-8", errors="replace"))
                choices = payload_resp.get("choices") or []
                if choices and isinstance(choices[0], dict):
                    message = choices[0].get("message") or {}
                    content = str(message.get("content") or "")
                break
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace")
                # Some models (e.g. gpt-5.6-*) only accept the default temperature.
                if attempt == 0 and exc.code == 400 and "temperature" in body:
                    payload.pop("temperature", None)
                    continue
                _LOG.warning("LLM predict (%s) failed: HTTP %s %s", model, exc.code, body[:200])
                break
            except Exception as exc:
                _LOG.warning("LLM predict (%s) failed: %s", model, exc)
                break

    if not content:
        return None
    parsed = _safe_parse_json(content)
    category = _clean_text(parsed.get("primary_category") or parsed.get("category"))
    if category not in allowed:
        return None
    try:
        confidence = float(parsed.get("confidence", 0.0))
    except Exception:
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))
    secondary = _clean_text(parsed.get("secondary_category"))
    if secondary not in allowed or secondary == category:
        secondary = ""
    return {
        "primary_category": category,
        "secondary_category": secondary or None,
        "confidence": confidence,
        "evidence_quote": _clean_text(parsed.get("evidence_quote"))[:300],
        "reasoning": _clean_text(parsed.get("reasoning"))[:500],
    }


def _llm_predict_category(
    *,
    title: str,
    description: str | Dict[str, Any],
    predicted_category: str,
    technology: str,
    categories: List[str],
    model: str = "",
) -> Tuple[str, float] | None:
    response = _llm_predict_category_response(
        title=title,
        description=description,
        predicted_category=predicted_category,
        technology=technology,
        categories=categories,
        model=model,
    )
    if response is None:
        return None
    return response["primary_category"], response["confidence"]


def _normalize_predicted_category(category: str) -> str:
    """Map a raw model prediction to the current canonical category name."""
    label = _clean_text(category)
    return _LEGACY_CATEGORY_MAP.get(label.lower(), label)


def llm_model_name() -> str:
    """Configured LLM model name, or empty when the LLM backend is not configured."""
    return _DEFAULT_MODEL if _DEFAULT_MODEL and _DEFAULT_API_KEY else ""


def _tier1_rule_category(title: str, technology: str) -> str | None:
    if _norm_technology(technology) == "software" or re.search(
        r"\[(?:ICS6\.0|ICPS5\.0)\]|\b(?:Link\s+Sense|Killer/ICPS)\b", title, re.IGNORECASE
    ):
        return "ICPS/Killer"
    return None


def _llm_input_record(row: Dict[str, Any], slot: str) -> Dict[str, Any]:
    is_first = slot == "llm1"
    raw = row.get(slot) or row.get(f"{slot}_prediction")
    if raw is None:
        raw = row.get("gpt-5.4-mini" if is_first else "gpt-5.6-luna")
    if raw is None:
        raw = row.get("llm_category" if is_first else "llm2_category")
    if isinstance(raw, dict):
        primary = raw.get("primary_category") or raw.get("category")
        secondary = raw.get("secondary_category")
        confidence = raw.get("confidence")
    else:
        primary = raw
        secondary = row.get(f"{slot}_secondary_category") or row.get(
            "llm_secondary_category" if is_first else "llm2_secondary_category"
        )
        confidence = row.get(f"{slot}_confidence") or row.get("llm_confidence" if is_first else "llm2_confidence")
    try:
        confidence_value = float(confidence) if confidence is not None else 0.0
    except (TypeError, ValueError):
        confidence_value = 0.0
    return {
        "primary_category": _normalize_predicted_category(_clean_text(primary)) if primary else "",
        "secondary_category": _normalize_predicted_category(_clean_text(secondary)) if secondary else "",
        "confidence": max(0.0, min(1.0, confidence_value)),
    }


def predict_issue_category_hybrid(
    row: Dict[str, Any], *, model_bundle: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """Route a ticket through Tier 1 rules, calibrated ML, and available LLM opinions."""
    title = _clean_text(row.get("ips_title") or row.get("title"))
    technology = _clean_text(row.get("technology"))
    rule_category = _tier1_rule_category(title, technology)
    llm1 = _llm_input_record(row, "llm1")
    llm2 = _llm_input_record(row, "llm2")
    if rule_category:
        return {
            "primary_category": rule_category,
            "provisional_category": None,
            "secondary_category": None,
            "confidence": 1.0,
            "routing_tier": "Tier1_Rule",
            "needs_human_review": False,
            "ml_category": rule_category,
            "ml_confidence": 1.0,
            "llm1_category": llm1["primary_category"] or None,
            "llm2_category": llm2["primary_category"] or None,
        }

    bundle = model_bundle or row.get("model_bundle")
    context = row.get("structured_context") or row.get("description") or ""
    if not isinstance(context, dict):
        context = extract_structured_ips_context(title, _clean_text(context), "")
    ml_category = _clean_text(row.get("ml_pred") or row.get("ml_category"))
    ml_confidence_value = row.get("ml_confidence")
    ml_secondary = _clean_text(row.get("ml_secondary_category"))
    if not ml_category or ml_confidence_value is None:
        if bundle is None:
            raise ValueError("model_bundle is required when the row has no OOF ML prediction.")
        if bundle.get("feature_mode") == "dual_structured_v2":
            ml_category, ml_confidence, ml_secondary_value = _predict_dual_feature_model(
                bundle, title, technology, context,
            )
            ml_secondary = ml_secondary or _clean_text(ml_secondary_value)
        else:
            ml_category, ml_confidence = classify_issue_title(
                bundle, title, technology=technology, description=context, use_llm=False,
            )
        ml_confidence_value = ml_confidence
    try:
        ml_confidence = max(0.0, min(1.0, float(ml_confidence_value)))
    except (TypeError, ValueError):
        ml_confidence = 0.0

    llm1_category = llm1["primary_category"]
    llm2_category = llm2["primary_category"]
    secondary_categories = [category for category in (llm1["secondary_category"], llm2["secondary_category"]) if category]
    secondary = next((category for category in secondary_categories if category != ml_category), None)
    secondary = secondary or (ml_secondary if ml_secondary and ml_secondary != ml_category else None)

    if ml_confidence >= 0.70 or (ml_category and llm1_category == ml_category):
        return {
            "primary_category": ml_category,
            "provisional_category": None,
            "secondary_category": secondary,
            "confidence": ml_confidence,
            "routing_tier": "Tier2_ML_HighConf",
            "needs_human_review": False,
            "ml_category": ml_category,
            "ml_confidence": ml_confidence,
            "llm1_category": llm1_category or None,
            "llm2_category": llm2_category or None,
        }

    llm_primary_consensus = bool(llm1_category and llm1_category == llm2_category)
    llm_secondary_consensus = bool(
        ml_category
        and llm1["secondary_category"] == ml_category
        and llm2["secondary_category"] == ml_category
    )
    if ml_confidence < 0.70 and (llm_primary_consensus or llm_secondary_consensus):
        consensus = llm1_category if llm_primary_consensus else ml_category
        llm_confidences = [item["confidence"] for item in (llm1, llm2) if item["primary_category"]]
        confidence = sum(llm_confidences) / len(llm_confidences) if llm_confidences else ml_confidence
        return {
            "primary_category": consensus,
            "provisional_category": consensus,
            "secondary_category": next((item for item in secondary_categories if item != consensus), None),
            "confidence": confidence,
            "routing_tier": "Tier3_LLM_Consensus",
            "needs_human_review": True,
            "ml_category": ml_category,
            "ml_confidence": ml_confidence,
            "llm1_category": llm1_category or None,
            "llm2_category": llm2_category or None,
        }

    provisional = ml_category or llm1_category or llm2_category or "Need-Triage"
    return {
        "primary_category": provisional,
        "provisional_category": provisional,
        "secondary_category": secondary,
        "confidence": ml_confidence,
        "routing_tier": "Tier3_Disagreement_Queue",
        "needs_human_review": True,
        "ml_category": ml_category or None,
        "ml_confidence": ml_confidence,
        "llm1_category": llm1_category or None,
        "llm2_category": llm2_category or None,
    }


def classify_issue_title(
    model_bundle: Dict[str, Any],
    title: str,
    *,
    predicted_category: str = "",
    technology: str = "",
    description: str | Dict[str, Any] = "",
    structured_context: Dict[str, Any] | None = None,
    use_llm: bool = True,
) -> Tuple[str, float]:
    pipeline = model_bundle.get("pipeline")
    title_text = _clean_text(title)

    rule_category = _tier1_rule_category(title_text, technology)
    if rule_category:
        return rule_category, 1.0

    context = structured_context or (description if isinstance(description, dict) else None)
    if context is None:
        context = extract_structured_ips_context(title_text, _clean_text(description), "")
    feature_mode = model_bundle.get("feature_mode")
    structured_model = feature_mode in {"structured_v1", "dual_structured_v2"}
    if not structured_model:
        overridden = _override_category_from_text(title_text)
        if overridden:
            return overridden, 0.99

    labels = [str(x).strip() for x in (model_bundle.get("labels") or []) if str(x).strip()]
    if use_llm and _PREDICT_BACKEND != "ml":
        llm_result = _llm_predict_category(
            title=title,
            description=context,
            predicted_category=predicted_category,
            technology=technology,
            categories=labels,
        )
        if llm_result is not None:
            raw_category, confidence = llm_result
            return _normalize_predicted_category(raw_category), confidence

    if pipeline is None:
        # Last-resort fallback when running pure-LLM mode without a model artifact.
        return "Need-Triage", 0.0

    if feature_mode == "dual_structured_v2":
        raw_category, confidence, _secondary = _predict_dual_feature_model(model_bundle, title_text, technology, context)
        return _normalize_predicted_category(raw_category), confidence

    if structured_model:
        features = build_issue_feature_record(title_text, technology, context)
    else:
        # The deployed artifact was trained on title-only text; never feed it raw details.
        features = _compose_feature_text(
            title_text,
            predicted_category=predicted_category,
            technology=technology,
            description="",
        )
    raw_category, confidence = _predict_with_confidence(pipeline, features)
    return _normalize_predicted_category(raw_category), confidence


def classify_issue_title_llm_record(
    model_bundle: Dict[str, Any],
    title: str,
    *,
    technology: str = "",
    description: str | Dict[str, Any] = "",
    model: str = "",
) -> Dict[str, Any] | None:
    """Return the full LLM response while applying deterministic Tier 1 and title overrides."""
    title_text = _clean_text(title)
    rule_category = _tier1_rule_category(title_text, technology)
    if rule_category:
        return {
            "primary_category": rule_category,
            "secondary_category": None,
            "confidence": 1.0,
            "evidence_quote": title_text[:160],
            "reasoning": "A deterministic software or ICPS/Killer rule matched.",
        }
    overridden = _override_category_from_text(title_text)
    if overridden:
        return {
            "primary_category": overridden,
            "secondary_category": None,
            "confidence": 0.99,
            "evidence_quote": title_text[:160],
            "reasoning": "A high-precision title signature matched.",
        }
    labels = [str(x).strip() for x in (model_bundle.get("labels") or []) if str(x).strip()]
    return _llm_predict_category_response(
        title=title,
        description=description,
        predicted_category="",
        technology=technology,
        categories=labels,
        model=model,
    )


def classify_issue_title_llm(
    model_bundle: Dict[str, Any],
    title: str,
    *,
    technology: str = "",
    description: str | Dict[str, Any] = "",
    model: str = "",
) -> Tuple[str, float] | None:
    """Tuple-compatible LLM wrapper; see classify_issue_title_llm_record for full fields."""
    result = classify_issue_title_llm_record(
        model_bundle, title, technology=technology, description=description, model=model,
    )
    if result is None:
        return None
    return _normalize_predicted_category(result["primary_category"]), float(result["confidence"])


def calculate_weight_for_category(
    category: str,
    category_weights: Dict[str, float],
    category_technology_weights: Dict[str, float],
    technology: str = "",
    default_weight: float = 1.0,
) -> float:
    cat_key = _norm_key(category)
    if not cat_key:
        return float(default_weight)
    tech_key = _norm_technology(technology)
    if tech_key:
        combo = f"{cat_key}|{tech_key}"
        if combo in category_technology_weights:
            return float(category_technology_weights[combo])
    return float(category_weights.get(cat_key, default_weight))


def classify_and_weight_rows(
    rows: Iterable[Dict[str, Any]],
    *,
    model_bundle: Dict[str, Any],
    category_weights: Dict[str, float],
    category_technology_weights: Dict[str, float],
    default_weight: float,
) -> List[Dict[str, Any]]:
    output: List[Dict[str, Any]] = []
    for row in rows:
        title = _clean_text(row.get("title"))
        description = _clean_text(row.get("description") or row.get("ips_description") or row.get("jira_description"))
        llm_pred = _clean_text(row.get("predicted_category"))
        tech = _clean_text(row.get("technology"))
        category, confidence = classify_issue_title(
            model_bundle,
            title,
            predicted_category=llm_pred,
            technology=tech,
            description=description,
        )

        # Rule override: BT issues containing "mute" should be treated as Audio.
        if _norm_technology(tech) == "bt" and "mute" in title.lower():
            category = "Audio"

        if row.get("is_field_issue") and _norm_technology(tech) in {"wifi", "bt"}:
            category = "Field Issue"

        weight = calculate_weight_for_category(
            category,
            category_weights,
            category_technology_weights,
            technology=tech,
            default_weight=default_weight,
        )
        enriched = dict(row)
        enriched["predicted_human_category"] = category
        enriched["category_confidence"] = round(float(confidence), 4)
        enriched["issue_weight"] = float(weight)
        output.append(enriched)
    return output
