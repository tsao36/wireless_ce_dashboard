from __future__ import annotations

import json
import os
import re
import logging
import urllib.request
import urllib.error
from typing import Any, Dict, Iterable, List, Tuple

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
_LLM_CLIENT: Any = None


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


def _predict_with_confidence(pipeline: Any, feature_text: str) -> Tuple[str, float]:
    predicted = str(pipeline.predict([feature_text])[0])

    if hasattr(pipeline, "predict_proba"):
        probs = pipeline.predict_proba([feature_text])[0]
        try:
            confidence = float(max(probs))
        except Exception:
            confidence = 0.0
        return predicted, confidence

    if hasattr(pipeline, "decision_function"):
        scores = pipeline.decision_function([feature_text])
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


# Maps stale/legacy model output labels to the current canonical category names
# in bug_category_config.json. Update this when the model produces labels that
# no longer match the config (e.g. after a category rename or model retrain
# with outdated training data).
_LEGACY_CATEGORY_MAP: Dict[str, str] = {
    "yb": "YB/Lost",
    "icps": "ICPS/Killer",
    "killer": "ICPS/Killer",
    "icps/killer": "ICPS/Killer",
    "not wifi issue": "Need-Triage",
    "not bt issue": "Need-Triage",
    "not-wireless": "Need-Triage",
    "not wireless": "Need-Triage",
    "unknown": "Need-Triage",
    "needs-triage": "Need-Triage",
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
    text = "\n".join(part for part in [_clean_text(title), _clean_text(description)] if part)
    if not text:
        return ""

    for pattern in _YB_LOST_PATTERNS:
        if pattern.search(text):
            return "YB/Lost"

    for pattern in _CONNECTIVITY_PATTERNS:
        if pattern.search(text):
            return "Connectivity"
    return ""


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
    try:
        _LLM_CLIENT = openai.OpenAI(api_key=_DEFAULT_API_KEY, base_url=_DEFAULT_BASE_URL, timeout=_LLM_TIMEOUT_SECONDS)
        return _LLM_CLIENT
    except Exception as exc:
        _LOG.warning("LLM client init failed, fallback to ML: %s", exc)
        return None


def _llm_predict_category(
    *,
    title: str,
    description: str,
    predicted_category: str,
    technology: str,
    categories: List[str],
) -> Tuple[str, float] | None:
    allowed = [c for c in (categories or []) if _clean_text(c)]
    if not allowed:
        return None

    system_prompt = (
        "You classify IPS issues into one category from an allowed list. "
        "Respond only JSON: {\"category\": <string>, \"confidence\": <0-1>}."
    )
    user_prompt = (
        "Classify this IPS issue.\n"
        f"Technology: {_clean_text(technology) or 'Unknown'}\n"
        f"Existing hint category: {_clean_text(predicted_category) or 'None'}\n"
        f"Title: {_clean_text(title) or '(empty)'}\n"
        f"Description: {_clean_text(description) or '(empty)'}\n\n"
        "Allowed categories:\n"
        + "\n".join(f"- {c}" for c in allowed)
    )

    client = _get_llm_client()
    content = ""
    if client is not None:
        try:
            resp = client.chat.completions.create(
                model=_DEFAULT_MODEL,
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
    elif _DEFAULT_MODEL and _DEFAULT_API_KEY:
        payload = {
            "model": _DEFAULT_MODEL,
            "temperature": 0.0,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
        url = _DEFAULT_BASE_URL.rstrip("/") + "/chat/completions"
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
            with urllib.request.urlopen(req, timeout=max(5.0, _LLM_TIMEOUT_SECONDS)) as resp:
                payload_resp = json.loads(resp.read().decode("utf-8", errors="replace"))
            choices = payload_resp.get("choices") or []
            if choices and isinstance(choices[0], dict):
                message = choices[0].get("message") or {}
                content = str(message.get("content") or "")
        except Exception as exc:
            _LOG.warning("LLM predict via HTTP fallback failed, fallback to ML: %s", exc)
            content = ""

    if not content:
        return None
    parsed = _safe_parse_json(content)
    category = _clean_text(parsed.get("category"))
    if category not in allowed:
        return None
    try:
        confidence = float(parsed.get("confidence", 0.0))
    except Exception:
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))
    return category, confidence


def _normalize_predicted_category(category: str) -> str:
    """Map a raw model prediction to the current canonical category name."""
    return _LEGACY_CATEGORY_MAP.get(category.strip().lower(), category)


def llm_model_name() -> str:
    """Configured LLM model name, or empty when the LLM backend is not configured."""
    return _DEFAULT_MODEL if _DEFAULT_MODEL and _DEFAULT_API_KEY else ""


def classify_issue_title(
    model_bundle: Dict[str, Any],
    title: str,
    *,
    predicted_category: str = "",
    technology: str = "",
    description: str = "",
    use_llm: bool = True,
) -> Tuple[str, float]:
    pipeline = model_bundle.get("pipeline")

    # Business hard rule: software issues are always routed to ICPS/Killer.
    if _norm_technology(technology) == "software":
        return "ICPS/Killer", 1.0

    overridden = _override_category_from_text(title, description=description)
    if overridden:
        return overridden, 0.99

    labels = [str(x).strip() for x in (model_bundle.get("labels") or []) if str(x).strip()]
    if use_llm and _PREDICT_BACKEND != "ml":
        llm_result = _llm_predict_category(
            title=title,
            description=description,
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

    feature_text = _compose_feature_text(
        title,
        predicted_category=predicted_category,
        technology=technology,
        description=description,
    )
    raw_category, confidence = _predict_with_confidence(pipeline, feature_text)
    return _normalize_predicted_category(raw_category), confidence


def classify_issue_title_llm(
    model_bundle: Dict[str, Any],
    title: str,
    *,
    technology: str = "",
    description: str = "",
) -> Tuple[str, float] | None:
    """LLM-only prediction with the same business rules; None when the LLM is unavailable or fails."""
    if _norm_technology(technology) == "software":
        return "ICPS/Killer", 1.0
    overridden = _override_category_from_text(title, description=description)
    if overridden:
        return overridden, 0.99
    labels = [str(x).strip() for x in (model_bundle.get("labels") or []) if str(x).strip()]
    result = _llm_predict_category(
        title=title,
        description=description,
        predicted_category="",
        technology=technology,
        categories=labels,
    )
    if result is None:
        return None
    return _normalize_predicted_category(result[0]), result[1]


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
