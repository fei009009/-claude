"""Soft tradeability labels for V2.0 candidates.

The filter is a post-pipeline risk annotation layer. It does not remove or
rerank strategy picks; it marks whether a candidate is easy to act on and why.
"""
from __future__ import annotations

import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from src.candidate_factor_panel import build_rows
from src.common import repair_mojibake, safe_float, safe_int
from src.tracking_store import output_root


def reports_dir(cfg: Dict[str, Any]) -> Path:
    path = output_root(cfg) / "reports"
    path.mkdir(parents=True, exist_ok=True)
    return path


def current_tradeability_path(cfg: Dict[str, Any]) -> Path:
    return reports_dir(cfg) / "tradeability_current.json"


def _safe_list(value: Any) -> List[str]:
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    if value in (None, ""):
        return []
    return [str(value)]


def evaluate_candidate(row: Dict[str, Any]) -> Dict[str, Any]:
    warnings: List[str] = []
    blockers: List[str] = []
    score = 1.0

    price = safe_float(row.get("price"), 0.0)
    pct_chg = safe_float(row.get("pct_chg"), 0.0)
    risk_penalty = safe_float(row.get("risk_penalty"), 0.0)
    sentiment_score = safe_float(row.get("sentiment_tradeability_score"), 0.5)
    if not row.get("sentiment_state"):
        sentiment_score = 0.5

    if row.get("snapshot_quality_ok") is False:
        blockers.append("快照质量未通过")
        score -= 0.45
    if row.get("history_continuity_ok") is False:
        blockers.append("历史K线连续性异常")
        score -= 0.35
    if safe_int(row.get("snapshot_zero_close_count"), 0) > 0:
        blockers.append("快照存在零价记录")
        score -= 0.30
    if safe_int(row.get("snapshot_discontinuous_count"), 0) > 0:
        warnings.append("快照存在断档样本")
        score -= 0.18
    if price <= 0:
        warnings.append("候选输出缺少实时价，需盘中行情再核")
        score -= 0.16
    if row.get("boundary_risk"):
        warnings.extend(_safe_list(row.get("boundary_reasons")) or ["边界涨幅风险"])
        score -= 0.22
    if pct_chg >= 9.5:
        warnings.append("涨幅接近涨停，需确认是否可买")
        score -= 0.16
    if risk_penalty >= 0.35:
        warnings.append("综合风险惩罚较高")
        score -= 0.20
    elif risk_penalty >= 0.20:
        warnings.append("综合风险惩罚偏高")
        score -= 0.10
    if sentiment_score < 0.25:
        warnings.append("情绪/环境可交易分偏低")
        score -= 0.18
    elif sentiment_score < 0.45:
        warnings.append("情绪/环境可交易分一般")
        score -= 0.08
    diagnosis_risks = _safe_list(row.get("diagnosis_risks"))
    if diagnosis_risks:
        warnings.extend([f"诊断风险:{item}" for item in diagnosis_risks[:3]])
        score -= min(0.20, 0.06 * len(diagnosis_risks))

    score = max(0.0, min(1.0, score))
    if blockers or score < 0.38:
        label = "不宜买"
        level = "avoid"
    elif warnings or score < 0.68:
        label = "谨慎"
        level = "caution"
    else:
        label = "可买"
        level = "ok"

    return {
        "code": row.get("code", ""),
        "name": repair_mojibake(row.get("name", "")),
        "selection_layer": row.get("selection_layer", ""),
        "strategy_count": safe_int(row.get("strategy_count"), 0),
        "preliminary_score": safe_float(row.get("preliminary_score"), 0.0),
        "tradeability_level": level,
        "tradeability_label": label,
        "tradeability_score": round(score, 4),
        "blockers": blockers,
        "warnings": warnings,
        "price": price,
        "pct_chg": pct_chg,
        "risk_penalty": risk_penalty,
        "sentiment_tradeability_score": sentiment_score,
    }


def _summary(items: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    rows = list(items)
    counts = Counter(str(row.get("tradeability_level") or "unknown") for row in rows)
    labels = Counter(str(row.get("tradeability_label") or "未知") for row in rows)
    return {
        "count": len(rows),
        "level_counts": dict(counts),
        "label_counts": dict(labels),
        "ok_count": counts.get("ok", 0),
        "caution_count": counts.get("caution", 0),
        "avoid_count": counts.get("avoid", 0),
    }


def build_tradeability_report(
    cfg: Dict[str, Any],
    *,
    latest: bool = True,
    selection_layer: str = "all",
    persist: bool = True,
) -> Dict[str, Any]:
    rows = build_rows(cfg, latest=latest, selection_layer=selection_layer)
    items = [evaluate_candidate(row) for row in rows]
    items.sort(
        key=lambda row: (
            {"ok": 0, "caution": 1, "avoid": 2}.get(str(row.get("tradeability_level")), 9),
            -safe_float(row.get("tradeability_score"), 0.0),
            -safe_float(row.get("preliminary_score"), 0.0),
            str(row.get("code") or ""),
        )
    )
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "latest": latest,
        "selection_layer": selection_layer,
        "summary": _summary(items),
        "items": items,
        "top": items[:20],
        "message": "可交易性为软标记，不改变 V10/V1/V4/X1Beam 原始出票结果。",
    }
    if persist:
        out_dir = reports_dir(cfg)
        report_path = out_dir / f"tradeability_{datetime.now():%Y%m%d_%H%M%S}.json"
        current_path = current_tradeability_path(cfg)
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        report_path.write_text(text, encoding="utf-8")
        current_path.write_text(text, encoding="utf-8")
        payload["report_path"] = str(report_path)
        payload["current_path"] = str(current_path)
    return payload


def load_current_tradeability(cfg: Dict[str, Any]) -> Dict[str, Any]:
    path = current_tradeability_path(cfg)
    if not path.exists():
        return {"exists": False, "summary": {}, "top": []}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["exists"] = True
        payload["path"] = str(path)
        return payload
    except Exception as exc:
        return {"exists": True, "error": str(exc), "summary": {}, "top": []}
