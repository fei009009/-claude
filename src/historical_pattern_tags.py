"""Historical pattern tags for V2.0 candidates.

This module turns the existing tracking/outcome store into lightweight labels
that can be shown beside current candidates. It is intentionally post-market:
tail runs should read the latest generated report, not recompute history.
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

from src.common import safe_float, safe_int
from src.tracking_outcomes import load_current_outcomes
from src.tracking_store import build_candidate_records, load_pipeline, load_tracking_records, output_root, pipeline_paths


PatternKey = Tuple[str, str]


def pattern_dir(cfg: Dict[str, Any]) -> Path:
    path = output_root(cfg) / "patterns"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _eligible(outcome: Dict[str, Any]) -> bool:
    return outcome.get("outcome_status") in {"partial", "complete"}


def _strategy_combo(record: Dict[str, Any]) -> str:
    sources = record.get("strategy_sources") or []
    if not isinstance(sources, list):
        sources = [str(sources)] if sources else []
    return "+".join(sorted(str(item) for item in sources if item)) or "none"


def _sentiment_score_bucket(record: Dict[str, Any]) -> str:
    if not str(record.get("sentiment_state") or "").strip():
        return "UNKNOWN"
    score = safe_float(record.get("sentiment_tradeability_score"), 0.0)
    if score >= 0.65:
        return "env>=0.65"
    if score >= 0.45:
        return "0.45<=env<0.65"
    if score >= 0.25:
        return "0.25<=env<0.45"
    return "env<0.25"


def pattern_keys(record: Dict[str, Any]) -> List[PatternKey]:
    keys: List[PatternKey] = [
        ("selection_layer", str(record.get("selection_layer") or "unknown")),
        ("strategy_count", str(safe_int(record.get("strategy_count"), 0))),
        ("strategy_combo", _strategy_combo(record)),
        ("diagnosis_signal", str(record.get("diagnosis_signal") or "NO_DIAG")),
    ]
    if record.get("sentiment_state"):
        keys.extend(
            [
                ("sentiment_state", str(record.get("sentiment_state"))),
                ("sentiment_state_group", str(record.get("sentiment_state_group") or "unknown")),
                ("sentiment_action", str(record.get("sentiment_action") or "unknown")),
                ("sentiment_score_bucket", _sentiment_score_bucket(record)),
            ]
        )
    xgb_score = safe_float(record.get("xgb_blended_score"), 0.0)
    if xgb_score >= 0.60:
        keys.append(("xgb_score_bucket", "xgb>=0.60"))
    elif xgb_score >= 0.45:
        keys.append(("xgb_score_bucket", "0.45<=xgb<0.60"))
    else:
        keys.append(("xgb_score_bucket", "xgb<0.45"))
    if safe_int(record.get("p80_count"), 0) >= 2:
        keys.append(("factor", "P80>=2"))
    if safe_float(record.get("lift_score"), 0.0) >= 1.5:
        keys.append(("factor", "Lift>=1.5"))
    if safe_float(record.get("wr"), 0.0) >= 0.60 or safe_float(record.get("wr28"), 0.0) >= 0.60:
        keys.append(("factor", "WR>=0.60"))
    if record.get("boundary_risk"):
        keys.append(("risk", "boundary_risk"))
    for reason in record.get("diagnosis_risks") or []:
        keys.append(("diagnosis_risk", str(reason)))
    return keys


def _event_maps(cfg: Dict[str, Any]) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Dict[str, Any]]]:
    records = {str(row.get("event_id")): row for row in load_tracking_records(cfg) if row.get("event_id")}
    outcomes_payload = load_current_outcomes(cfg)
    outcomes = {
        str(row.get("event_id")): row
        for row in outcomes_payload.get("outcomes", []) or []
        if row.get("event_id")
    }
    return records, outcomes


def _rate(values: Iterable[Any]) -> float:
    vals = [1.0 if bool(v) else 0.0 for v in values if v is not None]
    return round(sum(vals) / len(vals), 6) if vals else 0.0


def _mean(values: Iterable[Any]) -> float:
    vals = [safe_float(v, 0.0) for v in values if v is not None]
    return round(sum(vals) / len(vals), 6) if vals else 0.0


def _group_stats(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "sample_count": len(rows),
        "next_high_profit_rate": _rate(row.get("hit_next_high_profit") for row in rows),
        "five_day_5pct_hit_rate": _rate(row.get("hit_5d_5pct") for row in rows),
        "avg_next_high_return": _mean(row.get("next_high_return") for row in rows),
        "avg_max_5d_high_return": _mean(row.get("max_5d_high_return") for row in rows),
        "avg_max_5d_drawdown": _mean(row.get("max_5d_drawdown") for row in rows),
        "complete_count": sum(1 for row in rows if row.get("outcome_status") == "complete"),
        "partial_count": sum(1 for row in rows if row.get("outcome_status") == "partial"),
    }


def _label_for_stats(stats: Dict[str, Any], min_samples: int) -> Dict[str, Any]:
    count = safe_int(stats.get("sample_count"), 0)
    next_rate = safe_float(stats.get("next_high_profit_rate"), 0.0)
    five_rate = safe_float(stats.get("five_day_5pct_hit_rate"), 0.0)
    drawdown = safe_float(stats.get("avg_max_5d_drawdown"), 0.0)
    if count < min_samples:
        return {"label": "样本不足", "score": 0.0, "risk": False}
    if drawdown <= -0.05 and next_rate < 0.55:
        return {"label": "高回撤警戒", "score": -0.35, "risk": True}
    if next_rate >= 0.65 and five_rate >= 0.35:
        return {"label": "历史高胜率", "score": 0.80, "risk": False}
    if next_rate >= 0.65:
        return {"label": "历史高冲高", "score": 0.62, "risk": False}
    if five_rate >= 0.35:
        return {"label": "5日达标较强", "score": 0.55, "risk": False}
    if next_rate <= 0.35 and five_rate <= 0.15:
        return {"label": "历史偏弱", "score": -0.25, "risk": True}
    return {"label": "历史中性", "score": 0.15, "risk": False}


def build_group_stats(cfg: Dict[str, Any], *, min_samples: int = 3) -> Dict[str, Any]:
    records, outcomes = _event_maps(cfg)
    grouped: Dict[PatternKey, List[Dict[str, Any]]] = defaultdict(list)
    joined_count = 0
    for event_id, record in records.items():
        outcome = outcomes.get(event_id)
        if not outcome or not _eligible(outcome):
            continue
        joined = dict(record)
        joined.update(outcome)
        joined_count += 1
        for key in pattern_keys(record):
            grouped[key].append(joined)

    groups: List[Dict[str, Any]] = []
    for (dimension, key), rows in grouped.items():
        stats = _group_stats(rows)
        label = _label_for_stats(stats, min_samples)
        groups.append({
            "dimension": dimension,
            "key": key,
            **stats,
            **label,
        })
    groups.sort(
        key=lambda row: (
            safe_float(row.get("score"), 0.0),
            safe_float(row.get("next_high_profit_rate"), 0.0),
            safe_float(row.get("five_day_5pct_hit_rate"), 0.0),
            safe_int(row.get("sample_count"), 0),
        ),
        reverse=True,
    )
    return {
        "joined_outcome_count": joined_count,
        "group_count": len(groups),
        "groups": groups,
    }


def _latest_candidate_records(cfg: Dict[str, Any], *, latest: bool) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for path in pipeline_paths(cfg, latest=latest):
        pipeline = load_pipeline(path)
        rows.extend(build_candidate_records(pipeline, path))
    return rows


def tag_candidates(
    cfg: Dict[str, Any],
    groups: List[Dict[str, Any]],
    *,
    latest: bool = True,
    min_samples: int = 3,
) -> List[Dict[str, Any]]:
    lookup = {(row.get("dimension"), row.get("key")): row for row in groups}
    tagged: List[Dict[str, Any]] = []
    for record in _latest_candidate_records(cfg, latest=latest):
        matched = []
        for dimension, key in pattern_keys(record):
            stats = lookup.get((dimension, key))
            if not stats:
                continue
            label = _label_for_stats(stats, min_samples)
            matched.append({**stats, **label})
        matched.sort(key=lambda row: abs(safe_float(row.get("score"), 0.0)), reverse=True)
        positive = [row for row in matched if safe_float(row.get("score"), 0.0) > 0][:3]
        risks = [row for row in matched if row.get("risk")][:3]
        tag_score = round(sum(safe_float(row.get("score"), 0.0) for row in matched[:5]), 6)
        tagged.append({
            "event_id": record.get("event_id", ""),
            "code": record.get("code", ""),
            "name": record.get("name", ""),
            "selection_layer": record.get("selection_layer", ""),
            "strategy_sources": record.get("strategy_sources", []),
            "strategy_count": record.get("strategy_count", 0),
            "diagnosis_signal": record.get("diagnosis_signal", ""),
            "sentiment_state": record.get("sentiment_state", ""),
            "sentiment_action": record.get("sentiment_action", ""),
            "sentiment_tradeability_score": record.get("sentiment_tradeability_score", 0),
            "pattern_tag_score": tag_score,
            "positive_tags": positive,
            "risk_tags": risks,
            "matched_group_count": len(matched),
        })
    tagged.sort(key=lambda row: (safe_float(row.get("pattern_tag_score"), 0.0), safe_int(row.get("strategy_count"), 0)), reverse=True)
    return tagged


def build_historical_pattern_tags(
    cfg: Dict[str, Any],
    *,
    latest: bool = True,
    min_samples: int = 3,
    persist: bool = True,
) -> Dict[str, Any]:
    stats = build_group_stats(cfg, min_samples=min_samples)
    candidate_tags = tag_candidates(cfg, stats["groups"], latest=latest, min_samples=min_samples)
    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "latest": latest,
        "min_samples": min_samples,
        "joined_outcome_count": stats["joined_outcome_count"],
        "group_count": stats["group_count"],
        "top_positive_groups": [row for row in stats["groups"] if safe_float(row.get("score"), 0.0) > 0][:20],
        "top_risk_groups": [row for row in stats["groups"] if row.get("risk")][:20],
        "candidate_tags": candidate_tags,
    }
    if persist:
        out_dir = pattern_dir(cfg)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = out_dir / f"historical_pattern_tags_{ts}.json"
        current = out_dir / "historical_pattern_tags_current.json"
        text = json.dumps(report, ensure_ascii=False, indent=2)
        path.write_text(text, encoding="utf-8")
        current.write_text(text, encoding="utf-8")
        report["report_path"] = str(path)
        report["current_path"] = str(current)
    return report
