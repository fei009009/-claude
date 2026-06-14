"""Candidate tracking store for V2.0 pipelines.

This module only reads V2.0 pipeline json files and writes V2.0 outputs.
It does not touch any upstream screener project or rule file.
"""
from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from src.common import norm_code, repair_mojibake, safe_float, safe_int

STRATEGY_KEYS = ("v10", "v1", "v4", "x1beam")


def output_root(cfg: Dict[str, Any]) -> Path:
    return Path(str((cfg.get("paths") or {}).get("output_root") or "outputs"))


def json_dir(cfg: Dict[str, Any]) -> Path:
    path = output_root(cfg) / "json"
    path.mkdir(parents=True, exist_ok=True)
    return path


def tracking_dir(cfg: Dict[str, Any]) -> Path:
    path = output_root(cfg) / "tracking"
    path.mkdir(parents=True, exist_ok=True)
    return path


def tracking_file(cfg: Dict[str, Any]) -> Path:
    return tracking_dir(cfg) / "candidates.jsonl"


def pipeline_paths(cfg: Dict[str, Any], *, latest: bool = True) -> List[Path]:
    paths = sorted(json_dir(cfg).glob("pipeline_v2_*.json"), key=lambda p: p.stat().st_mtime)
    if latest and paths:
        return [paths[-1]]
    return paths


def load_pipeline(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _strategy_key(value: Any) -> str:
    text = str(value or "").strip().lower().replace("-", "").replace("_", "")
    if "x1" in text or "beam" in text:
        return "x1beam"
    if "v10" in text or "vip" in text:
        return "v10"
    if "v4" in text:
        return "v4"
    if "v1" in text:
        return "v1"
    return text or "unknown"


def _timestamp_date(value: str) -> str:
    if not value:
        return ""
    return value.split("T", 1)[0].split(" ", 1)[0]


def trade_date_from_pipeline(pipeline: Dict[str, Any]) -> str:
    quality = pipeline.get("snapshot_quality") or {}
    metrics = quality.get("metrics") or {}
    meta = quality.get("meta") or {}
    return (
        metrics.get("expected_trade_date")
        or metrics.get("observed_trade_date")
        or metrics.get("meta_trade_date")
        or meta.get("trade_date")
        or _timestamp_date(str(pipeline.get("timestamp", "")))
    )


def _quality_summary(pipeline: Dict[str, Any]) -> Dict[str, Any]:
    quality = pipeline.get("snapshot_quality") or {}
    metrics = quality.get("metrics") or {}
    meta = quality.get("meta") or {}
    validation = meta.get("validation") or {}
    continuity = (
        validation.get("summary", {}).get("history_continuity")
        or meta.get("history_continuity")
        or {}
    )
    return {
        "snapshot_quality_ok": bool(quality.get("ok")),
        "snapshot_status": quality.get("status", ""),
        "snapshot_file_count": metrics.get("file_count", 0),
        "snapshot_empty_files": metrics.get("empty_files", 0),
        "snapshot_zero_close_count": metrics.get("zero_close_count", 0),
        "snapshot_discontinuous_count": metrics.get("discontinuous_count", 0),
        "snapshot_stale_count": metrics.get("stale_count", 0),
        "primary_source": meta.get("primary_source")
        or meta.get("validation", {}).get("primary_source", ""),
        "validation_grade": validation.get("grade") or meta.get("grade", ""),
        "history_continuity_ok": continuity.get("ok"),
        "history_checked": continuity.get("checked", 0),
        "history_failed": continuity.get("failed", 0),
        "history_expected_previous_date": continuity.get("expected_previous_date", ""),
    }


def _boundary_map(pipeline: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    boundary = pipeline.get("boundary") or {}
    mapping: Dict[str, Dict[str, Any]] = {}
    for key in ("risks", "critical", "candidates"):
        rows = boundary.get(key) or []
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            code = norm_code(row.get("code", ""))
            if code:
                mapping[code] = row
    return mapping


def _diag_from_row(row: Dict[str, Any]) -> Dict[str, Any]:
    diag = row.get("diagnosis") or {}
    if not isinstance(diag, dict):
        diag = {}
    return {
        "diagnosis_signal": diag.get("signal", row.get("diagnosis_signal", "")),
        "diagnosis_badge": row.get("diagnosis_badge") or diag.get("badge", ""),
        "diagnosis_compact": row.get("diagnosis_compact") or diag.get("compact", ""),
        "xgb_model_score": safe_float(diag.get("model_score"), 0.0),
        "xgb_rule_score": safe_float(diag.get("rule_score"), 0.0),
        "xgb_blended_score": safe_float(diag.get("blended_score"), 0.0),
        "xgb_matched_rule_count": safe_int(diag.get("matched_rule_count"), 0),
        "diagnosis_risks": list(diag.get("risk_flags") or []),
        "diagnosis_recommendation": diag.get("recommendation", ""),
    }


def _row_features(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "price": safe_float(row.get("price"), 0.0),
        "pct_chg": safe_float(row.get("pct_chg"), 0.0),
        "lift_score": safe_float(row.get("lift_score"), 0.0),
        "wr": safe_float(row.get("wr", row.get("wr28")), 0.0),
        "wr28": safe_float(row.get("wr28", row.get("wr")), 0.0),
        "p80_count": safe_int(row.get("p80_count"), 0),
        "positive_count": safe_int(row.get("positive_count"), 0),
        "negative_count": safe_int(row.get("negative_count"), 0),
        "top_lu1_rate": safe_float(row.get("top_lu1_rate"), 0.0),
        "top_rule": row.get("top_rule", ""),
        "raw_rank": safe_int(row.get("rank"), 0),
    }


def _make_record(
    *,
    pipeline: Dict[str, Any],
    pipeline_path: Optional[Path],
    code: str,
    name: str,
    selection_layer: str,
    strategies: Iterable[str],
    ranks: Dict[str, Any],
    base_row: Dict[str, Any],
    boundary: Optional[Dict[str, Any]],
    selection_rank: int,
) -> Dict[str, Any]:
    pipeline_file = pipeline_path.name if pipeline_path else str(pipeline.get("_file", "memory"))
    strategy_list = sorted({_strategy_key(s) for s in strategies if s})
    rank_map = {_strategy_key(k): safe_int(v, 0) for k, v in (ranks or {}).items()}
    diag = _diag_from_row(base_row)
    quality = _quality_summary(pipeline)
    sentiment = base_row.get("sentiment_context") or {}
    if not sentiment:
        sentiment = (pipeline.get("sentiment") or {}).get("timing") or {}
    boundary = boundary or {}
    trade_date = trade_date_from_pipeline(pipeline)
    event_id = f"{pipeline_file}|{selection_layer}|{code}"

    record = {
        "event_id": event_id,
        "pipeline_file": pipeline_file,
        "pipeline_path": str(pipeline_path) if pipeline_path else "",
        "run_timestamp": pipeline.get("timestamp", ""),
        "trade_date": trade_date,
        "ingested_at": datetime.now().isoformat(timespec="seconds"),
        "snapshot_dir": pipeline.get("snapshot_dir", ""),
        "selection_layer": selection_layer,
        "selection_rank": selection_rank,
        "code": code,
        "name": repair_mojibake(name),
        "strategy_sources": strategy_list,
        "strategy_count": len(strategy_list),
        "v10_rank": rank_map.get("v10", 0),
        "v1_rank": rank_map.get("v1", 0),
        "v4_rank": rank_map.get("v4", 0),
        "x1beam_rank": rank_map.get("x1beam", 0),
        "intersection_label": base_row.get("intersection_label", ""),
        "boundary_risk": bool(boundary),
        "boundary_reasons": list(boundary.get("risk_reasons") or boundary.get("reasons") or []),
        "boundary_pct": safe_float(boundary.get("pct"), 0.0),
        "x1_preheat_usable": bool((pipeline.get("x1_preheat") or {}).get("usable")),
        "x1_preheat_completed": bool((pipeline.get("x1_preheat") or {}).get("completed")),
        "sentiment_date": sentiment.get("date", ""),
        "sentiment_state": sentiment.get("state", ""),
        "sentiment_state_group": sentiment.get("state_group", ""),
        "sentiment_value": safe_int(sentiment.get("value"), 0),
        "sentiment_risk_appetite": sentiment.get("risk_appetite", ""),
        "sentiment_position_multiplier": safe_float(sentiment.get("position_multiplier"), 1.0),
        "sentiment_tradeability_score": safe_float(sentiment.get("tradeability_score"), 0.0),
        "sentiment_action": sentiment.get("action", base_row.get("sentiment_action", "")),
        "sentiment_reason": sentiment.get("reason", ""),
        "sentiment_fresh_for_snapshot": bool(sentiment.get("fresh_for_snapshot", True)),
    }
    record.update(_row_features(base_row))
    record.update(diag)
    record.update(quality)
    return record


def build_candidate_records(
    pipeline: Dict[str, Any],
    pipeline_path: Optional[Path] = None,
    *,
    include_overlap: bool = True,
) -> List[Dict[str, Any]]:
    by_code: Dict[str, Dict[str, Any]] = {}
    boundary = _boundary_map(pipeline)

    for result in pipeline.get("strategies", []) or []:
        strategy = _strategy_key(result.get("strategy_name") or result.get("display_name"))
        if not strategy:
            continue
        for index, row in enumerate(result.get("top", []) or [], start=1):
            if not isinstance(row, dict):
                continue
            code = norm_code(row.get("code", ""))
            if not code:
                continue
            rank = safe_int(row.get("rank"), index)
            item = by_code.setdefault(
                code,
                {
                    "name": row.get("name", ""),
                    "strategies": set(),
                    "ranks": {},
                    "base_row": row,
                    "best_rank": rank,
                },
            )
            item["strategies"].add(strategy)
            item["ranks"][strategy] = rank
            if rank and (not item.get("best_rank") or rank < item["best_rank"]):
                item["base_row"] = row
                item["best_rank"] = rank
            if row.get("name") and not item.get("name"):
                item["name"] = row.get("name")

    records: List[Dict[str, Any]] = []
    for code, item in sorted(by_code.items(), key=lambda kv: (safe_int(kv[1].get("best_rank"), 999), kv[0])):
        records.append(
            _make_record(
                pipeline=pipeline,
                pipeline_path=pipeline_path,
                code=code,
                name=str(item.get("name", "")),
                selection_layer="strategy_top",
                strategies=item.get("strategies", set()),
                ranks=item.get("ranks", {}),
                base_row=item.get("base_row") or {},
                boundary=boundary.get(code),
                selection_rank=safe_int(item.get("best_rank"), 0),
            )
        )

    if include_overlap:
        for index, row in enumerate((pipeline.get("overlap") or {}).get("overlaps", []) or [], start=1):
            if not isinstance(row, dict):
                continue
            code = norm_code(row.get("code", ""))
            if not code:
                continue
            records.append(
                _make_record(
                    pipeline=pipeline,
                    pipeline_path=pipeline_path,
                    code=code,
                    name=str(row.get("name", "")),
                    selection_layer="overlap",
                    strategies=row.get("strategies", []),
                    ranks=row.get("ranks", {}),
                    base_row=row,
                    boundary=boundary.get(code),
                    selection_rank=index,
                )
            )
    return records


def _existing_event_ids(path: Path) -> set[str]:
    if not path.exists():
        return set()
    ids: set[str] = set()
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except Exception:
                continue
            event_id = item.get("event_id")
            if event_id:
                ids.add(str(event_id))
    return ids


def append_records(cfg: Dict[str, Any], records: List[Dict[str, Any]]) -> Dict[str, Any]:
    path = tracking_file(cfg)
    existing = _existing_event_ids(path)
    fresh = [record for record in records if record.get("event_id") not in existing]
    if fresh:
        with path.open("a", encoding="utf-8") as f:
            for record in fresh:
                f.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    return {
        "tracking_file": str(path),
        "input_records": len(records),
        "new_records": len(fresh),
        "duplicate_records": len(records) - len(fresh),
    }


def ingest_pipeline_file(cfg: Dict[str, Any], pipeline_path: Path) -> Dict[str, Any]:
    pipeline_path = Path(pipeline_path)
    pipeline = load_pipeline(pipeline_path)
    records = build_candidate_records(pipeline, pipeline_path)
    summary = append_records(cfg, records)
    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "mode": "single",
        "pipeline_count": 1,
        "pipelines": [str(pipeline_path)],
        "records_built": len(records),
        **summary,
    }
    report_path = tracking_dir(cfg) / f"tracking_ingest_{datetime.now():%Y%m%d_%H%M%S}.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    report["report_path"] = str(report_path)
    return report


def ingest_pipelines(
    cfg: Dict[str, Any],
    *,
    latest: bool = True,
    paths: Optional[List[Path]] = None,
) -> Dict[str, Any]:
    selected = paths or pipeline_paths(cfg, latest=latest)
    records: List[Dict[str, Any]] = []
    loaded: List[str] = []
    errors: List[Dict[str, str]] = []
    for path in selected:
        try:
            pipeline = load_pipeline(path)
            records.extend(build_candidate_records(pipeline, path))
            loaded.append(str(path))
        except Exception as exc:
            errors.append({"path": str(path), "error": str(exc)})
    summary = append_records(cfg, records)
    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "mode": "latest" if latest else "all",
        "pipeline_count": len(loaded),
        "pipelines": loaded,
        "records_built": len(records),
        "errors": errors,
        **summary,
    }
    report_path = tracking_dir(cfg) / f"tracking_ingest_{datetime.now():%Y%m%d_%H%M%S}.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    report["report_path"] = str(report_path)
    return report


def load_tracking_records(cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    path = tracking_file(cfg)
    if not path.exists():
        return []
    rows: List[Dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
    return rows


def _parse_dt(value: Any) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        pass
    for width, fmt in ((19, "%Y-%m-%dT%H:%M:%S"), (19, "%Y-%m-%d %H:%M:%S"), (10, "%Y-%m-%d")):
        try:
            return datetime.strptime(text[:width], fmt)
        except Exception:
            continue
    return None


def summarize_tracking(cfg: Dict[str, Any], *, days: int = 0, persist: bool = True) -> Dict[str, Any]:
    rows = load_tracking_records(cfg)
    cutoff = datetime.now() - timedelta(days=days) if days and days > 0 else None
    if cutoff:
        rows = [row for row in rows if (_parse_dt(row.get("run_timestamp")) or datetime.min) >= cutoff]

    def bucket(value: Any, default: str) -> str:
        text = str(value or "").strip()
        return text if text else default

    by_trade_date = Counter(bucket(row.get("trade_date"), "UNKNOWN_DATE") for row in rows)
    by_layer = Counter(bucket(row.get("selection_layer"), "UNKNOWN_LAYER") for row in rows)
    by_signal = Counter(bucket(row.get("diagnosis_signal"), "NO_DIAG") for row in rows)
    by_strategy_count = Counter(str(row.get("strategy_count", 0)) for row in rows)
    source_counter: Counter[str] = Counter()
    for row in rows:
        for source in row.get("strategy_sources") or []:
            source_counter[str(source)] += 1

    latest = sorted(rows, key=lambda r: str(r.get("run_timestamp", "")), reverse=True)[:20]
    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "days": days,
        "tracking_file": str(tracking_file(cfg)),
        "record_count": len(rows),
        "unique_pipeline_count": len({row.get("pipeline_file") for row in rows}),
        "unique_code_count": len({row.get("code") for row in rows}),
        "by_trade_date": dict(by_trade_date.most_common()),
        "by_selection_layer": dict(by_layer.most_common()),
        "by_diagnosis_signal": dict(by_signal.most_common()),
        "by_strategy_count": dict(by_strategy_count.most_common()),
        "strategy_source_counts": dict(source_counter.most_common()),
        "latest_records": latest,
    }
    if persist:
        report_path = tracking_dir(cfg) / f"tracking_report_{datetime.now():%Y%m%d_%H%M%S}.json"
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        report["report_path"] = str(report_path)
    else:
        report["report_path"] = ""
    return report
