"""Build a flat candidate factor panel from V2.0 pipeline files."""
from __future__ import annotations

import csv
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from src.common import safe_float, safe_int
from src.tracking_store import build_candidate_records, load_pipeline, output_root, pipeline_paths


BASE_COLUMNS = [
    "trade_date",
    "run_timestamp",
    "pipeline_file",
    "selection_layer",
    "selection_rank",
    "code",
    "name",
    "strategy_sources",
    "strategy_count",
    "v10_rank",
    "v1_rank",
    "v4_rank",
    "x1beam_rank",
    "strategy_consensus_score",
    "rank_strength_score",
    "xgb_model_score",
    "xgb_rule_score",
    "xgb_blended_score",
    "diagnosis_signal",
    "diagnosis_badge",
    "diagnosis_risks",
    "risk_penalty",
    "sentiment_date",
    "sentiment_state",
    "sentiment_state_group",
    "sentiment_value",
    "sentiment_position_multiplier",
    "sentiment_tradeability_score",
    "sentiment_score",
    "sentiment_action",
    "sentiment_reason",
    "p80_count",
    "p80_score",
    "lift_score",
    "lift_score_norm",
    "wr",
    "wr28",
    "pct_chg",
    "price",
    "positive_count",
    "negative_count",
    "top_lu1_rate",
    "top_rule",
    "boundary_risk",
    "boundary_reasons",
    "snapshot_quality_ok",
    "primary_source",
    "history_continuity_ok",
    "snapshot_zero_close_count",
    "snapshot_discontinuous_count",
    "preliminary_score",
    "factor_notes",
]


def factors_dir(cfg: Dict[str, Any]) -> Path:
    path = output_root(cfg) / "factors"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _rank_strength(row: Dict[str, Any]) -> float:
    scores: List[float] = []
    for key in ("v10_rank", "v1_rank", "v4_rank", "x1beam_rank"):
        rank = safe_int(row.get(key), 0)
        if rank > 0:
            scores.append(max(0.0, (11.0 - min(rank, 10)) / 10.0))
    if not scores:
        return 0.0
    return sum(scores) / len(scores)


def _risk_penalty(row: Dict[str, Any]) -> float:
    risks = row.get("diagnosis_risks") or []
    if not isinstance(risks, list):
        risks = [str(risks)] if risks else []
    penalty = 0.08 * len(risks)
    if row.get("boundary_risk"):
        penalty += 0.18
    if not row.get("snapshot_quality_ok"):
        penalty += 0.35
    if row.get("history_continuity_ok") is False:
        penalty += 0.25
    if safe_int(row.get("snapshot_zero_close_count"), 0) > 0:
        penalty += 0.20
    if safe_int(row.get("snapshot_discontinuous_count"), 0) > 0:
        penalty += 0.20
    action = str(row.get("sentiment_action") or "")
    if "降权" in action or "防兑现" in action:
        penalty += 0.10
    if row.get("sentiment_fresh_for_snapshot") is False:
        penalty += 0.15
    return min(1.0, penalty)


def _notes(row: Dict[str, Any]) -> List[str]:
    notes: List[str] = []
    if safe_int(row.get("strategy_count"), 0) >= 3:
        notes.append("3+策略共识")
    elif safe_int(row.get("strategy_count"), 0) >= 2:
        notes.append("2策略共识")
    if safe_float(row.get("xgb_blended_score"), 0.0) >= 0.6:
        notes.append("XGB支持")
    if safe_int(row.get("p80_count"), 0) >= 2:
        notes.append("P80>=2")
    if safe_float(row.get("lift_score"), 0.0) >= 1.5:
        notes.append("Lift较强")
    if row.get("boundary_risk"):
        notes.append("边界风险")
    action = str(row.get("sentiment_action") or "")
    if action:
        notes.append("情绪:" + action)
    risks = row.get("diagnosis_risks") or []
    if risks:
        notes.append("诊断风险:" + ",".join(str(item) for item in risks[:3]))
    return notes


def enrich_factor_row(record: Dict[str, Any]) -> Dict[str, Any]:
    row = dict(record)
    consensus = min(1.0, safe_int(row.get("strategy_count"), 0) / 4.0)
    rank_strength = _rank_strength(row)
    diagnosis = max(0.0, min(1.0, safe_float(row.get("xgb_blended_score"), 0.0)))
    p80_score = min(1.0, safe_int(row.get("p80_count"), 0) / 5.0)
    lift_norm = min(1.0, max(0.0, safe_float(row.get("lift_score"), 0.0) / 3.0))
    sentiment_score = safe_float(row.get("sentiment_tradeability_score"), 0.0)
    if not row.get("sentiment_state"):
        sentiment_score = 0.50
    risk = _risk_penalty(row)
    preliminary = (
        0.27 * consensus
        + 0.22 * diagnosis
        + 0.18 * rank_strength
        + 0.13 * p80_score
        + 0.08 * lift_norm
        + 0.12 * sentiment_score
        - 0.20 * risk
    )
    row.update(
        {
            "strategy_consensus_score": round(consensus, 4),
            "rank_strength_score": round(rank_strength, 4),
            "risk_penalty": round(risk, 4),
            "p80_score": round(p80_score, 4),
            "lift_score_norm": round(lift_norm, 4),
            "sentiment_score": round(sentiment_score, 4),
            "preliminary_score": round(max(0.0, min(1.0, preliminary)), 4),
            "factor_notes": _notes(row),
        }
    )
    return row


def _selected_paths(
    cfg: Dict[str, Any],
    *,
    latest: bool,
    paths: Optional[List[Path]] = None,
) -> List[Path]:
    return paths or pipeline_paths(cfg, latest=latest)


def build_rows(
    cfg: Dict[str, Any],
    *,
    latest: bool = True,
    paths: Optional[List[Path]] = None,
    selection_layer: str = "all",
) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for path in _selected_paths(cfg, latest=latest, paths=paths):
        pipeline = load_pipeline(path)
        for record in build_candidate_records(pipeline, path):
            if selection_layer != "all" and record.get("selection_layer") != selection_layer:
                continue
            rows.append(enrich_factor_row(record))
    rows.sort(
        key=lambda row: (
            str(row.get("trade_date", "")),
            str(row.get("run_timestamp", "")),
            str(row.get("selection_layer", "")),
            -safe_float(row.get("preliminary_score"), 0.0),
            safe_int(row.get("selection_rank"), 999),
        )
    )
    return rows


def _csv_value(value: Any) -> Any:
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return value


def write_rows(cfg: Dict[str, Any], rows: List[Dict[str, Any]], *, label: str) -> Dict[str, Any]:
    out_dir = factors_dir(cfg)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    json_path = out_dir / f"candidate_factor_panel_{label}_{ts}.json"
    csv_path = out_dir / f"candidate_factor_panel_{label}_{ts}.csv"

    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "label": label,
        "row_count": len(rows),
        "rows": rows,
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    columns = list(BASE_COLUMNS)
    extra = sorted({key for row in rows for key in row.keys()} - set(columns))
    columns.extend(extra)
    with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: _csv_value(row.get(key, "")) for key in columns})

    return {
        "generated_at": payload["generated_at"],
        "label": label,
        "row_count": len(rows),
        "json_path": str(json_path),
        "csv_path": str(csv_path),
    }


def build_candidate_factor_panel(
    cfg: Dict[str, Any],
    *,
    latest: bool = True,
    paths: Optional[List[Path]] = None,
    selection_layer: str = "all",
) -> Dict[str, Any]:
    rows = build_rows(cfg, latest=latest, paths=paths, selection_layer=selection_layer)
    label = "latest" if latest else "all"
    if selection_layer != "all":
        label = f"{label}_{selection_layer}"
    return write_rows(cfg, rows, label=label)


def top_rows(rows: Iterable[Dict[str, Any]], n: int = 10) -> List[Dict[str, Any]]:
    return sorted(rows, key=lambda row: -safe_float(row.get("preliminary_score"), 0.0))[:n]
