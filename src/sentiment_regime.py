"""Read-only bridge for the external 情绪推演 project.

The external project remains the owner of data refresh and timing inference.
V2.0 only consumes its generated JSON artifacts and converts them into a
market-regime layer used by the dashboard, health audit and later ranking work.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.common import output_root, safe_float, safe_int
from src.quality_gate import load_snapshot_meta, resolve_snapshot


def sentiment_project_dir(cfg: Dict[str, Any]) -> Path:
    paths = cfg.get("paths") or {}
    configured = paths.get("reference_sentiment_dir") or paths.get("sentiment_project_dir")
    return Path(str(configured)) if configured else Path(r"C:\Users\Administrator\Desktop\情绪推演\offline_sentiment_vip")


def build_sentiment_regime(cfg: Dict[str, Any], *, trade_date: str = "", persist: bool = False) -> Dict[str, Any]:
    root = sentiment_project_dir(cfg)
    data_dir = root / "data"
    feedback_dir = root / "feedback"
    analysis_dir = root / "analysis"
    active_snapshot, _ = resolve_snapshot(cfg)
    snapshot_meta = load_snapshot_meta(active_snapshot)
    target_date = _date_text(trade_date or snapshot_meta.get("trade_date") or "")

    timing_combined = _load_json(data_dir / "timing_combined.json")
    timing_forward = _load_json(data_dir / "timing_forward.json")
    sentiment = _load_json(data_dir / "sentiment.json")
    manifest = _load_json(data_dir / "manifest.json")
    accuracy = _load_json(feedback_dir / "accuracy_report.json")
    aux_model = _load_json(analysis_dir / "aux_model_report.json")

    timing_rows = [_normalize_timing(row) for row in timing_combined.get("data", [])]
    timing_rows = [row for row in timing_rows if row.get("date")]
    forward_rows = [_normalize_timing(row) for row in timing_forward.get("data", [])]
    forward_rows = [row for row in forward_rows if row.get("date")]
    sentiment_rows = [_normalize_sentiment(row) for row in sentiment.get("data", [])]
    sentiment_rows = [row for row in sentiment_rows if row.get("date")]

    current = _latest_on_or_before(timing_rows, target_date) if target_date else (timing_rows[-1] if timing_rows else {})
    latest_sentiment = _latest_on_or_before(sentiment_rows, target_date) if target_date else (sentiment_rows[-1] if sentiment_rows else {})
    next_forward = _first_after(forward_rows, target_date or str(current.get("date") or ""))

    timing_value = safe_int(current.get("market_timing"), 0)
    previous = _previous_row(timing_rows, str(current.get("date") or ""))
    previous_timing = safe_int(previous.get("market_timing"), timing_value)
    profile = _classify_timing(timing_value)
    metrics = _sentiment_metrics(latest_sentiment)
    freshness = _freshness(target_date, current, latest_sentiment, manifest, timing_combined)

    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "source_project": str(root),
        "ok": root.exists() and bool(current) and bool(latest_sentiment),
        "target_trade_date": target_date,
        "timing": {
            "date": current.get("date", ""),
            "value": timing_value,
            "previous_value": previous_timing,
            "delta": timing_value - previous_timing,
            "state": profile["state"],
            "state_group": profile["state_group"],
            "risk_appetite": profile["risk_appetite"],
            "position_multiplier": profile["position_multiplier"],
            "tail_guidance": profile["tail_guidance"],
        },
        "next_forward": {
            "date": next_forward.get("date", ""),
            "value": safe_int(next_forward.get("market_timing"), 0) if next_forward else None,
            "confidence": safe_float(next_forward.get("confidence"), 0.0) if next_forward else None,
            "turn_prob": safe_float(next_forward.get("turn_prob"), 0.0) if next_forward else None,
            "max_lb_trend": safe_float(next_forward.get("max_lb_trend"), 0.0) if next_forward else None,
        },
        "sentiment": {
            "date": latest_sentiment.get("date", ""),
            **metrics,
        },
        "freshness": freshness,
        "model_quality": {
            "latest_accuracy": (accuracy.get("latest_stats") or {}),
            "aux_summary": aux_model.get("summary", ""),
            "aux_status": aux_model.get("status", ""),
        },
        "files": {
            "timing_combined": str(data_dir / "timing_combined.json"),
            "timing_forward": str(data_dir / "timing_forward.json"),
            "sentiment": str(data_dir / "sentiment.json"),
        },
        "missing": _missing_files(data_dir, feedback_dir, analysis_dir),
    }
    if persist:
        path = output_root(cfg) / "reports" / f"sentiment_regime_{datetime.now():%Y%m%d_%H%M%S}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        current_path = output_root(cfg) / "reports" / "sentiment_regime_current.json"
        current_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        report["report_path"] = str(path)
    return report


def _load_json(path: Path) -> Dict[str, Any]:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return {}


def _date_text(value: Any) -> str:
    text = str(value or "").strip().replace("/", "-")
    if len(text) >= 10:
        return text[:10]
    if len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:8]}"
    return text


def _normalize_timing(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        **row,
        "date": _date_text(row.get("date1") or row.get("date")),
        "market_timing": safe_int(row.get("market_timing"), 0),
    }


def _normalize_sentiment(row: Dict[str, Any]) -> Dict[str, Any]:
    return {**row, "date": _date_text(row.get("date1") or row.get("date"))}


def _latest_on_or_before(rows: List[Dict[str, Any]], date_text: str) -> Dict[str, Any]:
    candidates = [row for row in rows if str(row.get("date") or "") <= date_text]
    return candidates[-1] if candidates else {}


def _first_after(rows: List[Dict[str, Any]], date_text: str) -> Dict[str, Any]:
    for row in rows:
        if str(row.get("date") or "") > date_text:
            return row
    return {}


def _previous_row(rows: List[Dict[str, Any]], date_text: str) -> Dict[str, Any]:
    previous: Dict[str, Any] = {}
    for row in rows:
        if str(row.get("date") or "") >= date_text:
            return previous
        previous = row
    return previous


def _classify_timing(value: int) -> Dict[str, Any]:
    if value <= -3:
        return {
            "state": "冰点/深退潮",
            "state_group": "risk_off",
            "risk_appetite": "低",
            "position_multiplier": 0.45,
            "tail_guidance": "只保留强共识、强XGB确认和低回撤候选，弱共识票降权。",
        }
    if value == -2:
        return {
            "state": "退潮",
            "state_group": "risk_off",
            "risk_appetite": "偏低",
            "position_multiplier": 0.55,
            "tail_guidance": "控制仓位，优先多策略交集，避免追高和边界风险票。",
        }
    if value == -1:
        return {
            "state": "弱修复观察",
            "state_group": "neutral_low",
            "risk_appetite": "谨慎",
            "position_multiplier": 0.70,
            "tail_guidance": "允许小仓试错，但需要历史模式或XGB确认加分。",
        }
    if value == 0:
        return {
            "state": "中性震荡",
            "state_group": "neutral",
            "risk_appetite": "中性",
            "position_multiplier": 0.80,
            "tail_guidance": "按四策略共识正常筛选，风险标签决定排序。",
        }
    if value == 1:
        return {
            "state": "修复启动",
            "state_group": "risk_on",
            "risk_appetite": "回升",
            "position_multiplier": 0.90,
            "tail_guidance": "可提高共识候选权重，重点看次日冲高历史标签。",
        }
    if value in (2, 3):
        return {
            "state": "升温/主升",
            "state_group": "risk_on",
            "risk_appetite": "较高",
            "position_multiplier": 1.00,
            "tail_guidance": "环境支持进攻，但仍需过滤炸板、零价、历史高回撤模式。",
        }
    if value == 4:
        return {
            "state": "高潮",
            "state_group": "overheat",
            "risk_appetite": "高但分歧风险上升",
            "position_multiplier": 0.85,
            "tail_guidance": "减少追高权重，关注高潮次日分歧和边界候选风险。",
        }
    return {
        "state": "过热/一致高潮",
        "state_group": "overheat",
        "risk_appetite": "过热",
        "position_multiplier": 0.65,
        "tail_guidance": "防次日兑现，除非多策略+XGB+历史标签都强，否则降权。",
    }


def _sentiment_metrics(row: Dict[str, Any]) -> Dict[str, Any]:
    uplimit = safe_float(row.get("uplimit_num"), 0.0)
    downlimit = safe_float(row.get("downlimit_num"), 0.0)
    zb = safe_float(row.get("zb_num"), 0.0)
    up_num = safe_float(row.get("up_num"), 0.0)
    down_num = safe_float(row.get("down_num"), 0.0)
    gt5 = safe_float(row.get("gt5_num"), 0.0)
    lt5 = safe_float(row.get("lt5_num"), 0.0)
    seal_base = uplimit + zb
    breadth_base = up_num + down_num
    profit_base = gt5 + lt5
    return {
        "uplimit_num": safe_int(row.get("uplimit_num"), 0),
        "downlimit_num": safe_int(row.get("downlimit_num"), 0),
        "fried_board_num": safe_int(row.get("zb_num"), 0),
        "max_lb_num": safe_int(row.get("max_lb_num"), 0),
        "up_num": safe_int(row.get("up_num"), 0),
        "down_num": safe_int(row.get("down_num"), 0),
        "seal_rate": round(uplimit / seal_base, 4) if seal_base > 0 else 0.0,
        "fried_board_rate": round(zb / seal_base, 4) if seal_base > 0 else 0.0,
        "up_down_ratio": round(up_num / down_num, 4) if down_num > 0 else 0.0,
        "breadth_score": round(up_num / breadth_base, 4) if breadth_base > 0 else 0.0,
        "profit_effect_score": round(gt5 / profit_base, 4) if profit_base > 0 else 0.0,
        "limit_balance": safe_int(uplimit - downlimit, 0),
        "max_lb_stocks": str(row.get("max_lb_stocks") or ""),
    }


def _freshness(
    target_date: str,
    timing_row: Dict[str, Any],
    sentiment_row: Dict[str, Any],
    manifest: Dict[str, Any],
    combined: Dict[str, Any],
) -> Dict[str, Any]:
    timing_date = str(timing_row.get("date") or "")
    sentiment_date = str(sentiment_row.get("date") or "")
    latest_official = str((combined.get("meta") or {}).get("latest_official_date") or timing_date)
    ok_for_snapshot = bool(target_date and timing_date == target_date and sentiment_date == target_date)
    return {
        "ok_for_snapshot": ok_for_snapshot,
        "timing_date": timing_date,
        "sentiment_date": sentiment_date,
        "latest_official_date": latest_official,
        "manifest_updated_at": manifest.get("updated_at", ""),
        "auth_failed": bool(manifest.get("auth_failed")),
        "target_date": target_date,
    }


def _missing_files(data_dir: Path, feedback_dir: Path, analysis_dir: Path) -> List[str]:
    required = [
        data_dir / "timing_combined.json",
        data_dir / "timing_forward.json",
        data_dir / "sentiment.json",
        feedback_dir / "accuracy_report.json",
        analysis_dir / "aux_model_report.json",
    ]
    return [str(path) for path in required if not path.exists()]
