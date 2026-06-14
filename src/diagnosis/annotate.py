"""Attach XGB diagnosis results to strategy rows and overlap rows."""
from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict, Iterable, List, Tuple

from src.common import norm_code


SIGNAL_TEXT = {
    "STRONG_BUY": "强确认",
    "BUY": "确认",
    "WATCH": "观察",
    "NEUTRAL": "中性",
    "SKIP": "跳过",
}


def build_diagnosis_map(diagnosis_results: Iterable[Any]) -> Dict[str, Dict[str, Any]]:
    mapping: Dict[str, Dict[str, Any]] = {}
    for item in diagnosis_results or []:
        data = item.to_dict() if hasattr(item, "to_dict") else dict(item or {})
        code = norm_code(data.get("code"))
        if not code:
            continue
        data["code"] = code
        data["badge"] = _badge(data)
        data["compact"] = _compact(data)
        mapping[code] = data
    return mapping


def build_skip_map(diagnosis_summary: Dict[str, Any] | None) -> Dict[str, Dict[str, Any]]:
    mapping: Dict[str, Dict[str, Any]] = {}
    for item in (diagnosis_summary or {}).get("skipped") or []:
        code = norm_code(item.get("code"))
        if not code:
            continue
        reason = str(item.get("reason") or "not_diagnosed")
        label = _skip_label(reason)
        mapping[code] = {
            "code": code,
            "name": item.get("name", ""),
            "reason": reason,
            "badge": "XGB未覆盖",
            "compact": f"XGB未覆盖: {label}",
        }
    return mapping


def annotate_strategy_results(
    results: List[Dict[str, Any]],
    diagnosis_results: Iterable[Any],
    diagnosis_summary: Dict[str, Any] | None = None,
) -> List[Dict[str, Any]]:
    diag_map = build_diagnosis_map(diagnosis_results)
    skip_map = build_skip_map(diagnosis_summary)
    for result in results or []:
        for row in result.get("top") or []:
            code = norm_code(row.get("code"))
            diag = diag_map.get(code)
            if diag:
                row["diagnosis"] = _public_diag(diag)
                row["diagnosis_badge"] = diag.get("badge", "")
                row["diagnosis_compact"] = diag.get("compact", "")
            elif code in skip_map:
                skipped = skip_map[code]
                row["diagnosis"] = {
                    "signal": "NO_DIAG",
                    "badge": skipped["badge"],
                    "compact": skipped["compact"],
                    "skip_reason": skipped["reason"],
                }
                row["diagnosis_badge"] = skipped["badge"]
                row["diagnosis_compact"] = skipped["compact"]
    return results


def annotate_overlap(
    overlap: Dict[str, Any],
    diagnosis_results: Iterable[Any],
    diagnosis_summary: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    diag_map = build_diagnosis_map(diagnosis_results)
    skip_map = build_skip_map(diagnosis_summary)
    for row in (overlap or {}).get("overlaps") or []:
        code = norm_code(row.get("code"))
        diag = diag_map.get(code)
        if diag:
            row["diagnosis"] = _public_diag(diag)
            row["diagnosis_badge"] = diag.get("badge", "")
            row["diagnosis_compact"] = diag.get("compact", "")
            signal = str(diag.get("signal") or "")
            if signal in {"STRONG_BUY", "BUY"}:
                row["xgb_confirmed"] = True
        elif code in skip_map:
            skipped = skip_map[code]
            row["diagnosis"] = {
                "signal": "NO_DIAG",
                "badge": skipped["badge"],
                "compact": skipped["compact"],
                "skip_reason": skipped["reason"],
            }
            row["diagnosis_badge"] = skipped["badge"]
            row["diagnosis_compact"] = skipped["compact"]
    return overlap


def annotate_all(
    results: List[Dict[str, Any]],
    overlap: Dict[str, Any],
    diagnosis_results: Iterable[Any],
    diagnosis_summary: Dict[str, Any] | None = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    annotate_strategy_results(results, diagnosis_results, diagnosis_summary)
    annotate_overlap(overlap, diagnosis_results, diagnosis_summary)
    return results, overlap


def _public_diag(data: Dict[str, Any]) -> Dict[str, Any]:
    keep = [
        "signal",
        "badge",
        "compact",
        "model_score",
        "rule_score",
        "blended_score",
        "target_scores",
        "matched_rule_count",
        "risk_flags",
        "recommendation",
        "diagnosis_quality",
        "best_rule",
    ]
    public = {key: deepcopy(data.get(key)) for key in keep if key in data}
    rich_report = (data.get("extra") or {}).get("rich_report")
    if rich_report:
        public["rich_report"] = deepcopy(rich_report)
    return public


def _badge(data: Dict[str, Any]) -> str:
    signal = str(data.get("signal") or "NEUTRAL")
    text = SIGNAL_TEXT.get(signal, signal)
    score = _pct(data.get("blended_score"))
    return f"XGB{text}{score}"


def _compact(data: Dict[str, Any]) -> str:
    signal = SIGNAL_TEXT.get(str(data.get("signal") or ""), str(data.get("signal") or "-"))
    extra = data.get("extra") or {}
    events = extra.get("event_probabilities") or {}
    pattern = (extra.get("pattern") or {}).get("label", "")
    parts = [f"{signal} 综合{_pct(data.get('blended_score'))}"]
    if pattern:
        parts.append(str(pattern))
    if events:
        parts.append(f"冲高{_pct(events.get('high5'))}")
        parts.append(f"次日{_pct(events.get('next5'))}")
    model = data.get("model_score")
    rule = data.get("rule_score")
    if model not in (None, ""):
        parts.append(f"模型{_pct(model)}")
    if rule not in (None, ""):
        parts.append(f"规则{_pct(rule)}")
    risks = data.get("risk_flags") or []
    if risks:
        parts.append("风险:" + ",".join(str(x) for x in risks[:2]))
    return " | ".join(parts)


def _pct(value: Any) -> str:
    try:
        return f"{float(value):.0%}"
    except Exception:
        return "-"


def _skip_label(reason: str) -> str:
    if "Insufficient data" in reason and "<60" in reason:
        return "历史K线不足60根，暂不能做XGB诊断"
    return reason
