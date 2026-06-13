"""V2.0 adapter for the XGB 25-bin diagnostic sidecar."""
from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

from src.common import norm_code, repair_mojibake, safe_float
from src.diagnosis.engine import DiagnosisResult


def _bin_code(code: Any) -> str:
    normalized = norm_code(code)
    if len(normalized) >= 8 and normalized[:2] in {"SH", "SZ", "BJ"}:
        return f"{normalized[:2]}#{normalized[-6:]}"
    digits = "".join(ch for ch in str(code or "") if ch.isdigit())[-6:]
    if not digits:
        return str(code or "")
    market = "SH" if digits.startswith(("5", "6", "9")) else "BJ" if digits.startswith(("4", "8")) else "SZ"
    return f"{market}#{digits}"


def _load_sidecar(xgb_dir: Path, snapshot_dir: Path):
    xgb_dir = Path(xgb_dir).resolve()
    os.environ["XGB_BIN_DATA_DIR"] = str(Path(snapshot_dir).resolve())
    if str(xgb_dir) not in sys.path:
        sys.path.insert(0, str(xgb_dir))
    features = importlib.import_module("xgb_bin_features")
    features.DATA_DIR = Path(snapshot_dir).resolve()
    diagnose = importlib.import_module("xgb_bin_diagnose")
    diagnose.DATA_DIR = Path(snapshot_dir).resolve()
    return diagnose


def _load_reporter(xgb_dir: Path, snapshot_dir: Path):
    _load_sidecar(xgb_dir, snapshot_dir)
    reporter = importlib.import_module("xgb_bin_report")
    reporter.DATA_DIR = Path(snapshot_dir).resolve()
    reporter.OUT_DIR = Path(xgb_dir).resolve() / "screener_output" / "bin_reports"
    reporter.OUT_DIR.mkdir(parents=True, exist_ok=True)
    for name in ("_stock_name", "_latest_local_data_date"):
        cached = getattr(reporter, name, None)
        if hasattr(cached, "cache_clear"):
            cached.cache_clear()
    return reporter


def _signal(row: Dict[str, Any]) -> str:
    score = safe_float(row.get("bin_score"), 0.0)
    rule_score = safe_float(row.get("bin_rule_score"), 0.0)
    bin_signal = str(row.get("bin_signal") or "")
    high5 = safe_float((row.get("bin_xgb_per_target") or {}).get("y_high_5d_5pct"), 0.0)
    next5 = safe_float((row.get("bin_xgb_per_target") or {}).get("y_next_5pct"), 0.0)
    if bin_signal == "BIN_STRONG_BUY" and score >= 0.72 and rule_score > 0:
        return "STRONG_BUY"
    if bin_signal in {"BIN_STRONG_BUY", "BIN_BUY"} and score >= 0.62 and rule_score > 0:
        return "BUY"
    if score >= 0.50 or high5 >= 0.58 or next5 >= 0.65:
        return "WATCH"
    if score >= 0.35:
        return "NEUTRAL"
    return "SKIP"


def _risk_flags(row: Dict[str, Any]) -> List[str]:
    flags: List[str] = []
    code = norm_code(row.get("code"))
    if code[-6:].startswith(("300", "688")):
        flags.append("创业板/科创板波动风险")
    if safe_float(row.get("bin_rule_score"), 0.0) <= 0:
        flags.append("X1高胜率规则未确认")
    high5 = safe_float((row.get("bin_xgb_per_target") or {}).get("y_high_5d_5pct"), 0.0)
    close5 = safe_float((row.get("bin_xgb_per_target") or {}).get("y_close_5d_5pct"), 0.0)
    no_loss = safe_float((row.get("bin_xgb_per_target") or {}).get("y_close_5d_0pct"), 0.0)
    if high5 >= 0.58 and close5 < 0.52:
        flags.append("冲高概率强于收盘达标")
    if no_loss < 0.45:
        flags.append("5日不亏概率偏低")
    if safe_float(row.get("gain_5d_pct"), 0.0) > 18:
        flags.append("短线涨幅偏高")
    return flags


def _pattern(row: Dict[str, Any]) -> Dict[str, Any]:
    bins = row.get("binned_features") or {}
    if bins.get("adx6") == 1 and bins.get("vol_ratio_20") == 1 and bins.get("boll_pos") == 1:
        label = "底部磨底压缩"
    elif bins.get("atr_ratio", 0) >= 4 and bins.get("boll_pos", 5) <= 2:
        label = "高波动超跌反弹"
    elif bins.get("adx6", 0) >= 4 and bins.get("pdi14", 0) >= 4:
        label = "趋势强势延续"
    elif bins.get("j", 0) >= 5 and bins.get("roc2", 0) >= 5:
        label = "短线动量冲高"
    else:
        label = "混合结构"
    return {
        "label": label,
        "bins": {key: bins.get(key) for key in ("adx6", "atr_ratio", "boll_pos", "j", "roc2", "vol_ratio_20", "pdi14", "mfi")},
    }


def _recommendation(item: DiagnosisResult) -> str:
    event = item.extra.get("event_probabilities") or {}
    high5 = safe_float(event.get("high5"), 0.0)
    close5 = safe_float(event.get("close5"), 0.0)
    next5 = safe_float(event.get("next5"), 0.0)
    pattern = (item.extra.get("pattern") or {}).get("label", "")
    if item.signal in {"STRONG_BUY", "BUY"}:
        return f"{item.code} {item.name}：XGB与规则共振，模式={pattern}，5日冲高={high5:.1%}，次日强度={next5:.1%}"
    if item.signal == "WATCH":
        return f"{item.code} {item.name}：模型偏强但规则未完全确认，模式={pattern}，5日冲高={high5:.1%}，收盘达标={close5:.1%}"
    if item.signal == "NEUTRAL":
        return f"{item.code} {item.name}：模型有一定弹性但确认不足，模式={pattern}，5日冲高={high5:.1%}"
    return f"{item.code} {item.name}：诊断偏弱，暂不作为XGB确认"


def _name_needs_repair(name: Any) -> bool:
    text = str(name or "").strip()
    return not text or "?" in text or "\ufffd" in text


def run_bin_sidecar(
    *,
    xgb_dir: Path,
    snapshot_dir: Path,
    candidates: Iterable[Dict[str, Any]],
    limit_rules: int = 0,
    top_matches: int = 8,
    rich_reports: bool = True,
    rich_report_top_n: int = 10,
) -> Tuple[List[DiagnosisResult], Dict[str, Any]]:
    diagnose = _load_sidecar(Path(xgb_dir), Path(snapshot_dir))
    indicator = diagnose.load_indicator_module()
    booster = diagnose.load_bin_booster()
    if not booster.get("available"):
        return [], {"available": False, "reason": booster.get("reason", "bin booster unavailable")}

    min_wr: Dict[str, float] = {}
    results: List[DiagnosisResult] = []
    errors: List[Dict[str, Any]] = []
    for candidate in candidates:
        code = norm_code(candidate.get("code"))
        if not code:
            continue
        try:
            raw = diagnose.diagnose_code(
                _bin_code(code),
                indicator_module=indicator,
                booster=booster,
                min_wr=min_wr,
                limit_rules=limit_rules,
                top_matches=top_matches,
            )
        except Exception as exc:
            errors.append({"code": code, "error": str(exc)[:240]})
            continue
        if raw.get("error"):
            errors.append({"code": code, "error": raw.get("error")})
            continue
        item = DiagnosisResult(code, repair_mojibake(candidate.get("name", "")))
        item.model_score = safe_float(raw.get("bin_xgb_blended_score") or raw.get("bin_xgb_score"), 0.0)
        item.rule_score = safe_float(raw.get("bin_rule_score"), 0.0)
        item.blended_score = safe_float(raw.get("bin_score"), 0.0)
        item.target_scores = {
            "xgb_high5": safe_float((raw.get("bin_xgb_per_target") or {}).get("y_high_5d_5pct"), 0.0),
            "xgb_close5": safe_float((raw.get("bin_xgb_per_target") or {}).get("y_close_5d_5pct"), 0.0),
            "xgb_noloss": safe_float((raw.get("bin_xgb_per_target") or {}).get("y_close_5d_0pct"), 0.0),
            "xgb_next5": safe_float((raw.get("bin_xgb_per_target") or {}).get("y_next_5pct"), 0.0),
        }
        item.matched_rules = raw.get("targets") or {}
        high_target = ((raw.get("targets") or {}).get("y_high_5d_5pct") or {})
        high_matches = high_target.get("top_matches") or []
        item.best_rule = high_matches[0] if high_matches else {}
        item.signal = _signal(raw)
        item.risk_flags = _risk_flags(raw)
        item.diagnosis_quality = {
            "engine": "xgb_25bin_sidecar",
            "snapshot_dir": str(Path(snapshot_dir).resolve()),
            "date": raw.get("date"),
            "bin_signal": raw.get("bin_signal"),
            "bin_feature_count": len(raw.get("feature_vector") or []),
            "rule_targets": {
                key: {
                    "matched_count": value.get("matched_count", 0),
                    "best_wr": value.get("best_wr", 0),
                    "weighted_wr": value.get("weighted_wr", 0),
                }
                for key, value in (raw.get("targets") or {}).items()
            },
        }
        item.extra = {
            "event_probabilities": {
                "high5": item.target_scores["xgb_high5"],
                "close5": item.target_scores["xgb_close5"],
                "noloss": item.target_scores["xgb_noloss"],
                "next5": item.target_scores["xgb_next5"],
            },
            "pattern": _pattern(raw),
            "bin_score": item.blended_score,
            "bin_xgb_score": safe_float(raw.get("bin_xgb_score"), 0.0),
            "bin_xgb_blended_score": safe_float(raw.get("bin_xgb_blended_score"), 0.0),
            "bin_rule_score": item.rule_score,
            "gain_1d_pct": safe_float(raw.get("gain_1d_pct"), 0.0),
            "gain_5d_pct": safe_float(raw.get("gain_5d_pct"), 0.0),
            "close": safe_float(raw.get("close"), 0.0),
        }
        item.recommendation = _recommendation(item)
        results.append(item)

    results.sort(key=lambda x: (x.signal not in {"STRONG_BUY", "BUY", "WATCH"}, -x.blended_score, -x.model_score))
    rich_errors: List[Dict[str, Any]] = []
    if rich_reports and results:
        try:
            reporter = _load_reporter(Path(xgb_dir), Path(snapshot_dir))
            for item in results[: max(int(rich_report_top_n or 0), 0)]:
                try:
                    payload = reporter.build_report(
                        _bin_code(item.code),
                        limit_rules=limit_rules,
                        top_matches=top_matches,
                    )
                    if payload.get("error"):
                        rich_errors.append({"code": item.code, "error": payload.get("error")})
                        continue
                    stock_name = str(payload.get("stock_name") or "").strip()
                    if stock_name and _name_needs_repair(item.name):
                        item.name = stock_name
                    paths = reporter.write_report(payload)
                    narrative = payload.get("narrative") or {}
                    rich_report = {
                        "rating": payload.get("rating"),
                        "risk_level": payload.get("risk_level"),
                        "json": paths.get("latest_json") or paths.get("json"),
                        "markdown": paths.get("latest_md") or paths.get("md"),
                        "headline": narrative.get("headline"),
                        "probability_text": narrative.get("probability_text"),
                        "pattern_text": narrative.get("pattern_text"),
                        "veto_text": narrative.get("veto_text"),
                        "final_view": narrative.get("final_view"),
                    }
                    item.extra["rich_report"] = rich_report
                    item.diagnosis_quality["rich_report"] = {
                        "markdown": rich_report.get("markdown"),
                        "json": rich_report.get("json"),
                    }
                    if rich_report.get("final_view"):
                        item.recommendation = str(rich_report["final_view"])
                except Exception as exc:
                    rich_errors.append({"code": item.code, "error": str(exc)[:240]})
        except Exception as exc:
            rich_errors.append({"code": "*", "error": f"rich reporter unavailable: {str(exc)[:240]}"})
    return results, {
        "available": True,
        "engine": "xgb_25bin_sidecar",
        "booster_targets": len(booster.get("models") or {}),
        "errors": errors[:20],
        "rich_report_top_n": rich_report_top_n if rich_reports else 0,
        "rich_report_errors": rich_errors[:20],
    }
