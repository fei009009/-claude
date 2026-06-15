"""Factor effectiveness evaluation for V2.0 tracked candidates.

This module is deliberately post-market only. It reads the tracking store,
factor fields and outcome labels, then writes reports under V2.0 outputs.
"""
from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from src.candidate_factor_panel import enrich_factor_row
from src.common import safe_float, safe_int
from src.tracking_outcomes import load_current_outcomes
from src.tracking_store import load_tracking_records, output_root


FACTOR_SPECS: List[Dict[str, Any]] = [
    {"key": "preliminary_score", "label": "综合初评分", "higher_is_better": True},
    {"key": "strategy_count", "label": "策略共识数", "higher_is_better": True},
    {"key": "strategy_consensus_score", "label": "策略共识分", "higher_is_better": True},
    {"key": "rank_strength_score", "label": "策略排名强度", "higher_is_better": True},
    {"key": "xgb_blended_score", "label": "XGB综合分", "higher_is_better": True},
    {"key": "xgb_model_score", "label": "XGB模型分", "higher_is_better": True},
    {"key": "xgb_rule_score", "label": "XGB规则分", "higher_is_better": True},
    {"key": "sentiment_tradeability_score", "label": "情绪可交易分", "higher_is_better": True},
    {"key": "p80_count", "label": "P80命中数", "higher_is_better": True},
    {"key": "p80_score", "label": "P80归一分", "higher_is_better": True},
    {"key": "lift_score", "label": "LiftScore", "higher_is_better": True},
    {"key": "lift_score_norm", "label": "Lift归一分", "higher_is_better": True},
    {"key": "wr", "label": "WR", "higher_is_better": True},
    {"key": "positive_count", "label": "正向规则数", "higher_is_better": True},
    {"key": "negative_count", "label": "负向规则数", "higher_is_better": False},
    {"key": "risk_penalty", "label": "风险惩罚", "higher_is_better": False},
]

TARGET_SPECS: List[Dict[str, Any]] = [
    {"key": "next_high_return", "label": "次日冲高收益", "kind": "return"},
    {"key": "next_close_return", "label": "次日收盘收益", "kind": "return"},
    {"key": "max_5d_high_return", "label": "5日最高收益", "kind": "return"},
    {"key": "max_5d_drawdown", "label": "5日最大回撤", "kind": "return"},
    {"key": "hit_next_high_profit", "label": "次日冲高盈利", "kind": "hit"},
    {"key": "hit_5d_5pct", "label": "5日5%达标", "kind": "hit"},
]


def reports_dir(cfg: Dict[str, Any]) -> Path:
    path = output_root(cfg) / "reports"
    path.mkdir(parents=True, exist_ok=True)
    return path


def current_factor_eval_path(cfg: Dict[str, Any]) -> Path:
    return reports_dir(cfg) / "factor_eval_current.json"


def _clean_number(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except Exception:
        return None
    return number if math.isfinite(number) else None


def _target_value(row: Dict[str, Any], key: str) -> Optional[float]:
    value = row.get(key)
    if value is True:
        return 1.0
    if value is False:
        return 0.0
    return _clean_number(value)


def _mean(values: Iterable[float]) -> float:
    data = [float(v) for v in values if math.isfinite(float(v))]
    return sum(data) / len(data) if data else 0.0


def _pearson(pairs: List[Tuple[float, float]]) -> float:
    if len(pairs) < 3:
        return 0.0
    xs = [x for x, _ in pairs]
    ys = [y for _, y in pairs]
    mx = _mean(xs)
    my = _mean(ys)
    vx = sum((x - mx) ** 2 for x in xs)
    vy = sum((y - my) ** 2 for y in ys)
    if vx <= 0 or vy <= 0:
        return 0.0
    cov = sum((x - mx) * (y - my) for x, y in pairs)
    return cov / math.sqrt(vx * vy)


def _rankdata(values: List[float]) -> List[float]:
    indexed = sorted(enumerate(values), key=lambda item: item[1])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(indexed):
        j = i + 1
        while j < len(indexed) and indexed[j][1] == indexed[i][1]:
            j += 1
        avg_rank = (i + 1 + j) / 2.0
        for k in range(i, j):
            ranks[indexed[k][0]] = avg_rank
        i = j
    return ranks


def _spearman(pairs: List[Tuple[float, float]]) -> float:
    if len(pairs) < 3:
        return 0.0
    xr = _rankdata([x for x, _ in pairs])
    yr = _rankdata([y for _, y in pairs])
    return _pearson(list(zip(xr, yr)))


def _split_groups(
    pairs: List[Tuple[float, float]],
    *,
    higher_is_better: bool,
    group_count: int = 3,
) -> List[Dict[str, Any]]:
    if not pairs:
        return []
    ordered = sorted(pairs, key=lambda item: item[0], reverse=higher_is_better)
    n = len(ordered)
    labels = ["best", "middle", "worst"] if group_count == 3 else [f"group_{i+1}" for i in range(group_count)]
    groups: List[Dict[str, Any]] = []
    for idx in range(group_count):
        start = round(idx * n / group_count)
        end = round((idx + 1) * n / group_count)
        chunk = ordered[start:end]
        if not chunk:
            continue
        ys = [y for _, y in chunk]
        groups.append(
            {
                "label": labels[idx] if idx < len(labels) else f"group_{idx+1}",
                "count": len(chunk),
                "factor_min": round(min(x for x, _ in chunk), 6),
                "factor_max": round(max(x for x, _ in chunk), 6),
                "target_mean": round(_mean(ys), 6),
                "hit_rate": round(_mean(ys), 4),
            }
        )
    return groups


def _topk_metrics(
    pairs: List[Tuple[float, float]],
    *,
    higher_is_better: bool,
    target_kind: str,
) -> List[Dict[str, Any]]:
    if not pairs:
        return []
    ordered = sorted(pairs, key=lambda item: item[0], reverse=higher_is_better)
    baseline = _mean(y for _, y in ordered)
    rows: List[Dict[str, Any]] = []
    for k in (5, 10, 20):
        if len(ordered) < k:
            continue
        ys = [y for _, y in ordered[:k]]
        avg = _mean(ys)
        item = {
            "top_n": k,
            "target_mean": round(avg, 6),
            "baseline_mean": round(baseline, 6),
            "excess": round(avg - baseline, 6),
        }
        if target_kind == "hit":
            item["lift"] = round(avg / baseline, 4) if baseline > 0 else 0.0
        rows.append(item)
    return rows


def _pairs(rows: List[Dict[str, Any]], factor_key: str, target_key: str) -> List[Tuple[float, float]]:
    out: List[Tuple[float, float]] = []
    for row in rows:
        x = _clean_number(row.get(factor_key))
        y = _target_value(row, target_key)
        if x is None or y is None:
            continue
        out.append((x, y))
    return out


def _joined_rows(cfg: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    records = load_tracking_records(cfg)
    outcomes_payload = load_current_outcomes(cfg)
    outcome_map = {
        str(row.get("event_id")): row
        for row in outcomes_payload.get("outcomes", []) or []
        if row.get("event_id")
    }
    rows: List[Dict[str, Any]] = []
    for record in records:
        event_id = str(record.get("event_id") or "")
        outcome = outcome_map.get(event_id)
        if not outcome or outcome.get("outcome_status") not in {"partial", "complete"}:
            continue
        joined = enrich_factor_row(record)
        for key in (
            "outcome_status",
            "future_bar_count",
            "next_high_return",
            "next_close_return",
            "next_low_return",
            "max_5d_high_return",
            "close_5d_return",
            "max_5d_drawdown",
            "hit_next_high_profit",
            "hit_5d_5pct",
            "failure_reason",
        ):
            joined[key] = outcome.get(key)
        rows.append(joined)
    return rows, outcomes_payload


def _factor_target_report(
    rows: List[Dict[str, Any]],
    factor: Dict[str, Any],
    target: Dict[str, Any],
    min_samples: int,
) -> Dict[str, Any]:
    pairs = _pairs(rows, factor["key"], target["key"])
    expected_sign = 1.0 if factor.get("higher_is_better", True) else -1.0
    ic = _pearson(pairs)
    rank_ic = _spearman(pairs)
    enough = len(pairs) >= min_samples
    return {
        "factor": factor["key"],
        "factor_label": factor["label"],
        "target": target["key"],
        "target_label": target["label"],
        "target_kind": target["kind"],
        "sample_count": len(pairs),
        "enough_samples": enough,
        "ic": round(ic, 6),
        "rank_ic": round(rank_ic, 6),
        "aligned_ic": round(ic * expected_sign, 6),
        "aligned_rank_ic": round(rank_ic * expected_sign, 6),
        "groups": _split_groups(pairs, higher_is_better=bool(factor.get("higher_is_better", True))) if enough else [],
        "topk": _topk_metrics(
            pairs,
            higher_is_better=bool(factor.get("higher_is_better", True)),
            target_kind=str(target["kind"]),
        ) if enough else [],
    }


def _summarize_factors(metrics: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    important_targets = {"hit_5d_5pct", "max_5d_high_return", "hit_next_high_profit", "next_high_return"}
    buckets: Dict[str, List[Dict[str, Any]]] = {}
    for item in metrics:
        if not item.get("enough_samples") or item.get("target") not in important_targets:
            continue
        buckets.setdefault(str(item.get("factor")), []).append(item)
    summary: List[Dict[str, Any]] = []
    for factor, rows in buckets.items():
        if not rows:
            continue
        label = str(rows[0].get("factor_label") or factor)
        summary.append(
            {
                "factor": factor,
                "factor_label": label,
                "target_count": len(rows),
                "avg_aligned_ic": round(_mean(safe_float(row.get("aligned_ic")) for row in rows), 6),
                "avg_aligned_rank_ic": round(_mean(safe_float(row.get("aligned_rank_ic")) for row in rows), 6),
                "min_sample_count": min(safe_int(row.get("sample_count"), 0) for row in rows),
                "best_target": max(rows, key=lambda row: safe_float(row.get("aligned_rank_ic"), -999)).get("target_label"),
            }
        )
    return sorted(summary, key=lambda row: safe_float(row.get("avg_aligned_rank_ic"), -999), reverse=True)


def build_factor_eval(
    cfg: Dict[str, Any],
    *,
    min_samples: int = 20,
    persist: bool = True,
) -> Dict[str, Any]:
    rows, outcomes_payload = _joined_rows(cfg)
    outcome_summary = outcomes_payload.get("summary") or {}
    metrics: List[Dict[str, Any]] = []
    for factor in FACTOR_SPECS:
        for target in TARGET_SPECS:
            metrics.append(_factor_target_report(rows, factor, target, min_samples))
    factor_summary = _summarize_factors(metrics)
    status = "ready" if len(rows) >= min_samples else "waiting_outcomes"
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "status": status,
        "min_samples": min_samples,
        "joined_sample_count": len(rows),
        "outcome_generated_at": outcomes_payload.get("generated_at", ""),
        "outcome_summary": {
            "outcome_count": outcome_summary.get("outcome_count", 0),
            "tracked_count": outcome_summary.get("tracked_count", 0),
            "completed_count": outcome_summary.get("completed_count", 0),
            "pending_count": outcome_summary.get("pending_count", 0),
            "status_counts": outcome_summary.get("status_counts", {}),
            "failure_counts": outcome_summary.get("failure_counts", {}),
        },
        "factor_summary": factor_summary,
        "metrics": metrics,
        "message": (
            "真实收益样本不足，等待盘后 outcome-update 回填后再评估因子有效性。"
            if status == "waiting_outcomes"
            else "因子有效性报告已生成，可观察 aligned_rank_ic、TopK 与分组收益。"
        ),
    }
    if persist:
        out_dir = reports_dir(cfg)
        report_path = out_dir / f"factor_eval_{datetime.now():%Y%m%d_%H%M%S}.json"
        current_path = current_factor_eval_path(cfg)
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        report_path.write_text(text, encoding="utf-8")
        current_path.write_text(text, encoding="utf-8")
        payload["report_path"] = str(report_path)
        payload["current_path"] = str(current_path)
    return payload


def load_current_factor_eval(cfg: Dict[str, Any]) -> Dict[str, Any]:
    path = current_factor_eval_path(cfg)
    if not path.exists():
        return {"exists": False, "status": "missing", "factor_summary": []}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["exists"] = True
        payload["path"] = str(path)
        return payload
    except Exception as exc:
        return {"exists": True, "status": "error", "error": str(exc), "factor_summary": []}
