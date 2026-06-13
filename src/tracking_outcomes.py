"""Post-market outcome labeling for tracked V2.0 candidates."""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from src.backtest_metrics import hit_rate, mean, win_rate
from src.common import norm_code, repair_mojibake, safe_float, safe_int
from src.tracking_store import load_tracking_records, tracking_dir


def _parse_date(value: Any) -> Optional[datetime]:
    text = str(value or "").strip().replace("/", "-")
    if not text:
        return None
    for width, fmt in ((10, "%Y-%m-%d"), (19, "%Y-%m-%dT%H:%M:%S"), (19, "%Y-%m-%d %H:%M:%S")):
        try:
            return datetime.strptime(text[:width], fmt)
        except Exception:
            continue
    return None


def _date_text(value: Any) -> str:
    dt = _parse_date(value)
    return dt.strftime("%Y-%m-%d") if dt else str(value or "")


def _code_file_candidates(snapshot_dir: Path, code: str) -> List[Path]:
    code = norm_code(code)
    if len(code) < 8:
        return []
    market, digits = code[:2], code[2:]
    return [
        snapshot_dir / f"{market}#{digits}.txt",
        snapshot_dir / f"{market}{digits}.txt",
        snapshot_dir / f"{market.lower()}#{digits}.txt",
        snapshot_dir / f"{market.lower()}{digits}.txt",
        snapshot_dir / f"{digits}.{market}.txt",
        snapshot_dir / f"{digits}{market}.txt",
    ]


def resolve_kline_file(snapshot_dir: Path, code: str) -> Optional[Path]:
    for path in _code_file_candidates(snapshot_dir, code):
        if path.exists() and path.is_file():
            return path
    return None


def read_kline_txt(path: Path) -> List[Dict[str, Any]]:
    bars: List[Dict[str, Any]] = []
    if not path.exists():
        return bars
    for encoding in ("utf-8", "utf-8-sig", "gb18030", "gbk"):
        try:
            lines = path.read_text(encoding=encoding, errors="ignore").splitlines()
            break
        except Exception:
            lines = []
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("date") or line.startswith("日期"):
            continue
        parts = [item for item in line.replace(",", "\t").split("\t") if item != ""]
        if len(parts) < 6:
            continue
        dt = _parse_date(parts[0])
        if not dt:
            continue
        bars.append(
            {
                "date": dt.strftime("%Y-%m-%d"),
                "open": safe_float(parts[1]),
                "high": safe_float(parts[2]),
                "low": safe_float(parts[3]),
                "close": safe_float(parts[4]),
                "volume": safe_float(parts[5]),
                "amount": safe_float(parts[6]) if len(parts) > 6 else 0.0,
            }
        )
    bars.sort(key=lambda row: row["date"])
    return bars


def _bars_around_trade_date(
    bars: List[Dict[str, Any]],
    trade_date: str,
    max_days: int,
) -> Tuple[Optional[Dict[str, Any]], List[Dict[str, Any]]]:
    trade_dt = _parse_date(trade_date)
    if not trade_dt:
        return None, []
    trade_text = trade_dt.strftime("%Y-%m-%d")
    base = None
    future: List[Dict[str, Any]] = []
    for bar in bars:
        dt = _parse_date(bar.get("date"))
        if not dt:
            continue
        if bar.get("date") == trade_text:
            base = bar
        elif dt > trade_dt:
            future.append(bar)
    return base, future[:max_days]


def _return(value: float, entry: float) -> float:
    if entry <= 0:
        return 0.0
    return value / entry - 1.0


def label_record(
    record: Dict[str, Any],
    *,
    max_days: int = 5,
    one_day_profit_threshold: float = 0.0,
    five_day_target: float = 0.05,
) -> Dict[str, Any]:
    code = norm_code(record.get("code", ""))
    snapshot_dir = Path(str(record.get("snapshot_dir") or ""))
    trade_date = _date_text(record.get("trade_date"))
    kline_path = resolve_kline_file(snapshot_dir, code)
    entry_price = safe_float(record.get("price"), 0.0)

    outcome: Dict[str, Any] = {
        "event_id": record.get("event_id", ""),
        "pipeline_file": record.get("pipeline_file", ""),
        "trade_date": trade_date,
        "code": code,
        "name": repair_mojibake(record.get("name", "")),
        "selection_layer": record.get("selection_layer", ""),
        "strategy_sources": record.get("strategy_sources", []),
        "strategy_count": safe_int(record.get("strategy_count"), 0),
        "diagnosis_signal": record.get("diagnosis_signal") or "NO_DIAG",
        "diagnosis_badge": record.get("diagnosis_badge", ""),
        "entry_price": entry_price,
        "snapshot_dir": str(snapshot_dir),
        "kline_path": str(kline_path or ""),
        "outcome_status": "pending",
        "future_bar_count": 0,
        "future_dates": [],
        "last_future_date": "",
        "next_high_return": None,
        "next_close_return": None,
        "next_low_return": None,
        "max_5d_high_return": None,
        "close_5d_return": None,
        "max_5d_drawdown": None,
        "hit_next_high_profit": None,
        "hit_5d_5pct": None,
        "failure_reason": "",
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }

    if not kline_path:
        outcome["outcome_status"] = "missing_kline"
        outcome["failure_reason"] = "missing_kline_file"
        return outcome

    bars = read_kline_txt(kline_path)
    base, future = _bars_around_trade_date(bars, trade_date, max_days=max_days)
    if entry_price <= 0 and base:
        entry_price = safe_float(base.get("close"), 0.0)
        outcome["entry_price"] = entry_price
    if entry_price <= 0:
        outcome["outcome_status"] = "blocked"
        outcome["failure_reason"] = "missing_entry_price"
        return outcome
    if not future:
        outcome["outcome_status"] = "pending"
        outcome["failure_reason"] = "no_future_bar_yet"
        return outcome

    next_bar = future[0]
    highs = [safe_float(bar.get("high"), 0.0) for bar in future]
    lows = [safe_float(bar.get("low"), 0.0) for bar in future]
    closes = [safe_float(bar.get("close"), 0.0) for bar in future]
    outcome.update(
        {
            "outcome_status": "complete" if len(future) >= max_days else "partial",
            "future_bar_count": len(future),
            "future_dates": [bar.get("date", "") for bar in future],
            "last_future_date": future[-1].get("date", ""),
            "next_high_return": round(_return(safe_float(next_bar.get("high"), 0.0), entry_price), 6),
            "next_close_return": round(_return(safe_float(next_bar.get("close"), 0.0), entry_price), 6),
            "next_low_return": round(_return(safe_float(next_bar.get("low"), 0.0), entry_price), 6),
            "max_5d_high_return": round(_return(max(highs), entry_price), 6),
            "close_5d_return": round(_return(closes[-1], entry_price), 6),
            "max_5d_drawdown": round(_return(min(lows), entry_price), 6),
        }
    )
    outcome["hit_next_high_profit"] = bool(outcome["next_high_return"] is not None and outcome["next_high_return"] > one_day_profit_threshold)
    outcome["hit_5d_5pct"] = bool(outcome["max_5d_high_return"] is not None and outcome["max_5d_high_return"] >= five_day_target)
    if outcome["hit_5d_5pct"]:
        outcome["failure_reason"] = ""
    elif outcome["max_5d_drawdown"] is not None and outcome["max_5d_drawdown"] <= -0.05:
        outcome["failure_reason"] = "drawdown_over_5pct"
    elif not outcome["hit_next_high_profit"]:
        outcome["failure_reason"] = "next_day_no_profit"
    elif len(future) < max_days:
        outcome["failure_reason"] = "waiting_5d_confirmation"
    else:
        outcome["failure_reason"] = "no_5d_target"
    return outcome


def current_outcome_path(cfg: Dict[str, Any]) -> Path:
    return tracking_dir(cfg) / "outcomes_current.json"


def update_outcomes(
    cfg: Dict[str, Any],
    *,
    max_days: int = 5,
    one_day_profit_threshold: float = 0.0,
    five_day_target: float = 0.05,
) -> Dict[str, Any]:
    records = load_tracking_records(cfg)
    outcomes = [
        label_record(
            record,
            max_days=max_days,
            one_day_profit_threshold=one_day_profit_threshold,
            five_day_target=five_day_target,
        )
        for record in records
    ]
    summary = summarize_outcomes(outcomes)
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "max_days": max_days,
        "one_day_profit_threshold": one_day_profit_threshold,
        "five_day_target": five_day_target,
        "summary": summary,
        "outcomes": outcomes,
    }
    current = current_outcome_path(cfg)
    current.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    report_path = tracking_dir(cfg) / f"tracking_outcomes_{datetime.now():%Y%m%d_%H%M%S}.json"
    report_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "generated_at": payload["generated_at"],
        "record_count": len(records),
        "outcome_count": len(outcomes),
        "summary": summary,
        "current_path": str(current),
        "report_path": str(report_path),
    }


def load_current_outcomes(cfg: Dict[str, Any]) -> Dict[str, Any]:
    path = current_outcome_path(cfg)
    if not path.exists():
        return {
            "generated_at": "",
            "summary": {"status_counts": {}, "completed_count": 0},
            "outcomes": [],
        }
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"generated_at": "", "summary": {"error": str(exc)}, "outcomes": []}


def _eligible(outcomes: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [row for row in outcomes if row.get("outcome_status") in ("partial", "complete")]


def _bool_values(outcomes: Iterable[Dict[str, Any]], key: str) -> List[float]:
    values: List[float] = []
    for row in outcomes:
        value = row.get(key)
        if value is True:
            values.append(1.0)
        elif value is False:
            values.append(0.0)
    return values


def _return_values(outcomes: Iterable[Dict[str, Any]], key: str) -> List[float]:
    values: List[float] = []
    for row in outcomes:
        value = row.get(key)
        if value is None:
            continue
        values.append(safe_float(value))
    return values


def _group_summary(outcomes: List[Dict[str, Any]]) -> Dict[str, Any]:
    tracked = _eligible(outcomes)
    next_high = _return_values(tracked, "next_high_return")
    max_5d = _return_values(tracked, "max_5d_high_return")
    drawdown = _return_values(tracked, "max_5d_drawdown")
    return {
        "count": len(outcomes),
        "tracked_count": len(tracked),
        "next_high_profit_rate": round(mean(_bool_values(tracked, "hit_next_high_profit")), 4),
        "five_day_5pct_hit_rate": round(mean(_bool_values(tracked, "hit_5d_5pct")), 4),
        "avg_next_high_return": round(mean(next_high), 6),
        "avg_max_5d_high_return": round(mean(max_5d), 6),
        "avg_max_5d_drawdown": round(mean(drawdown), 6),
    }


def _group_by(outcomes: List[Dict[str, Any]], key: str) -> Dict[str, Any]:
    buckets: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in outcomes:
        value = row.get(key)
        if isinstance(value, list):
            value = "+".join(str(item) for item in value)
        text = str(value or "UNKNOWN")
        buckets[text].append(row)
    return {name: _group_summary(rows) for name, rows in sorted(buckets.items())}


def _group_by_strategy_source(outcomes: List[Dict[str, Any]]) -> Dict[str, Any]:
    buckets: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in outcomes:
        sources = row.get("strategy_sources") or []
        for source in sources:
            buckets[str(source)].append(row)
    return {name: _group_summary(rows) for name, rows in sorted(buckets.items())}


def summarize_outcomes(outcomes: List[Dict[str, Any]]) -> Dict[str, Any]:
    status_counts = Counter(str(row.get("outcome_status", "UNKNOWN")) for row in outcomes)
    tracked = _eligible(outcomes)
    complete = [row for row in outcomes if row.get("outcome_status") == "complete"]
    pending = [row for row in outcomes if row.get("outcome_status") == "pending"]
    failure_counts = Counter(str(row.get("failure_reason") or "NONE") for row in outcomes)
    summary = {
        "outcome_count": len(outcomes),
        "tracked_count": len(tracked),
        "completed_count": len(complete),
        "pending_count": len(pending),
        "status_counts": dict(status_counts.most_common()),
        "failure_counts": dict(failure_counts.most_common()),
        "overall": _group_summary(outcomes),
        "by_selection_layer": _group_by(outcomes, "selection_layer"),
        "by_strategy_count": _group_by(outcomes, "strategy_count"),
        "by_diagnosis_signal": _group_by(outcomes, "diagnosis_signal"),
        "by_strategy_source": _group_by_strategy_source(outcomes),
    }
    return summary


def outcome_report(cfg: Dict[str, Any]) -> Dict[str, Any]:
    payload = load_current_outcomes(cfg)
    outcomes = payload.get("outcomes") or []
    summary = summarize_outcomes(outcomes)
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "source_generated_at": payload.get("generated_at", ""),
        "summary": summary,
        "top_success": sorted(
            _eligible(outcomes),
            key=lambda row: safe_float(row.get("max_5d_high_return"), -999),
            reverse=True,
        )[:20],
        "top_drawdown": sorted(
            _eligible(outcomes),
            key=lambda row: safe_float(row.get("max_5d_drawdown"), 999),
        )[:20],
    }
