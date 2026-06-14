"""Attach market sentiment regime context to strategy candidates.

This layer is deliberately not a fifth strategy.  It keeps V10/V1/V4/X1Beam
ranking and intersection counts unchanged, then adds market-regime guidance
for dashboard, push and post-market factor analysis.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Tuple

from src.common import norm_code, safe_float, safe_int


XGB_CONFIRM_SIGNALS = {"STRONG_BUY", "BUY"}


def annotate_all_with_sentiment(
    results: List[Dict[str, Any]],
    overlap: Dict[str, Any],
    sentiment_report: Dict[str, Any] | None,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Mutate strategy and overlap rows with sentiment context."""
    report = sentiment_report or {}
    code_context = _code_context(results, overlap)

    for result in results or []:
        strategy = _strategy_key(result.get("strategy_name") or result.get("display_name"))
        for row in result.get("top") or []:
            code = norm_code(row.get("code"))
            context = code_context.get(code, {"strategies": {strategy}, "ranks": {strategy: row.get("rank")}})
            _attach(row, report, context)

    for row in (overlap or {}).get("overlaps") or []:
        code = norm_code(row.get("code"))
        context = code_context.get(
            code,
            {"strategies": set(row.get("strategies") or []), "ranks": row.get("ranks") or {}},
        )
        _attach(row, report, context)

    return results, overlap


def sentiment_summary(report: Dict[str, Any] | None) -> Dict[str, Any]:
    report = report or {}
    timing = report.get("timing") or {}
    sentiment = report.get("sentiment") or {}
    freshness = report.get("freshness") or {}
    return {
        "ok": bool(report.get("ok")),
        "date": timing.get("date", ""),
        "state": timing.get("state", ""),
        "state_group": timing.get("state_group", ""),
        "value": safe_int(timing.get("value"), 0),
        "risk_appetite": timing.get("risk_appetite", ""),
        "position_multiplier": safe_float(timing.get("position_multiplier"), 1.0),
        "tail_guidance": timing.get("tail_guidance", ""),
        "fresh_for_snapshot": bool(freshness.get("ok_for_snapshot")),
        "uplimit_num": safe_int(sentiment.get("uplimit_num"), 0),
        "downlimit_num": safe_int(sentiment.get("downlimit_num"), 0),
        "fried_board_num": safe_int(sentiment.get("fried_board_num"), 0),
        "seal_rate": safe_float(sentiment.get("seal_rate"), 0.0),
        "breadth_score": safe_float(sentiment.get("breadth_score"), 0.0),
    }


def _attach(row: Dict[str, Any], report: Dict[str, Any], context: Dict[str, Any]) -> None:
    summary = sentiment_summary(report)
    strategy_count = len({str(item).lower() for item in context.get("strategies") or [] if item})
    strategy_count = max(strategy_count, safe_int(row.get("strategy_count"), 0), 1)
    xgb_signal = _xgb_signal(row)
    risk_flags = _risk_flags(row)
    tradeability = _tradeability_score(row, summary, strategy_count, xgb_signal, risk_flags)
    action, reason = _action(summary, strategy_count, xgb_signal, risk_flags, row)

    row["sentiment_context"] = {
        **summary,
        "strategy_count": strategy_count,
        "xgb_signal": xgb_signal,
        "risk_flags": risk_flags,
        "tradeability_score": tradeability,
        "action": action,
        "reason": reason,
    }
    row["sentiment_badge"] = _badge(summary, action)
    row["sentiment_compact"] = _compact(summary, action, reason, tradeability)
    row["sentiment_position_multiplier"] = summary.get("position_multiplier", 1.0)
    row["sentiment_tradeability_score"] = tradeability
    row["sentiment_action"] = action


def _code_context(results: List[Dict[str, Any]], overlap: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    mapping: Dict[str, Dict[str, Any]] = {}
    for result in results or []:
        strategy = _strategy_key(result.get("strategy_name") or result.get("display_name"))
        if not strategy:
            continue
        for index, row in enumerate(result.get("top") or [], start=1):
            code = norm_code(row.get("code"))
            if not code:
                continue
            item = mapping.setdefault(code, {"strategies": set(), "ranks": {}})
            item["strategies"].add(strategy)
            item["ranks"][strategy] = safe_int(row.get("rank"), index)
    for row in (overlap or {}).get("overlaps") or []:
        code = norm_code(row.get("code"))
        if not code:
            continue
        item = mapping.setdefault(code, {"strategies": set(), "ranks": {}})
        item["strategies"].update(_strategy_key(s) for s in row.get("strategies") or [] if s)
        item["ranks"].update(row.get("ranks") or {})
    return mapping


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
    return text


def _xgb_signal(row: Dict[str, Any]) -> str:
    diag = row.get("diagnosis") or {}
    if not isinstance(diag, dict):
        diag = {}
    return str(diag.get("signal") or row.get("diagnosis_signal") or "").upper()


def _risk_flags(row: Dict[str, Any]) -> List[str]:
    diag = row.get("diagnosis") or {}
    if not isinstance(diag, dict):
        diag = {}
    risks = diag.get("risk_flags") or row.get("diagnosis_risks") or []
    if isinstance(risks, str):
        return [risks] if risks else []
    if isinstance(risks, Iterable):
        return [str(item) for item in risks if str(item)]
    return []


def _tradeability_score(
    row: Dict[str, Any],
    summary: Dict[str, Any],
    strategy_count: int,
    xgb_signal: str,
    risk_flags: List[str],
) -> float:
    if not summary.get("ok") or not summary.get("fresh_for_snapshot"):
        return 0.0
    multiplier = safe_float(summary.get("position_multiplier"), 1.0)
    base = 0.30 + min(0.40, 0.10 * strategy_count)
    if xgb_signal == "STRONG_BUY":
        base += 0.18
    elif xgb_signal == "BUY":
        base += 0.13
    elif xgb_signal == "WATCH":
        base += 0.04

    penalty = min(0.24, 0.08 * len(risk_flags))
    state_group = str(summary.get("state_group") or "")
    pct = safe_float(row.get("pct_chg"), 0.0)
    if state_group == "risk_off" and strategy_count < 3 and xgb_signal not in XGB_CONFIRM_SIGNALS:
        penalty += 0.16
    if state_group == "overheat" and pct >= 8.5:
        penalty += 0.12
    if safe_float(summary.get("fried_board_num"), 0.0) > safe_float(summary.get("uplimit_num"), 0.0):
        penalty += 0.04

    score = (base - penalty) * multiplier
    return round(max(0.0, min(1.0, score)), 4)


def _action(
    summary: Dict[str, Any],
    strategy_count: int,
    xgb_signal: str,
    risk_flags: List[str],
    row: Dict[str, Any],
) -> Tuple[str, str]:
    if not summary.get("ok"):
        return "情绪缺失", "外部情绪推演数据不可用"
    if not summary.get("fresh_for_snapshot"):
        return "待核对", "情绪日期与当前快照未完全对齐"

    state_group = str(summary.get("state_group") or "")
    has_xgb = xgb_signal in XGB_CONFIRM_SIGNALS
    pct = safe_float(row.get("pct_chg"), 0.0)
    if state_group == "risk_off":
        if strategy_count >= 3 and has_xgb and not risk_flags:
            return "低仓强观察", "退潮期只保留多策略+XGB共振候选"
        if strategy_count >= 2 and has_xgb:
            return "小仓观察", "弱环境下需要次日确认"
        return "情绪降权", "退潮期弱共识候选不宜放大仓位"
    if state_group == "neutral_low":
        return ("谨慎观察", "修复未确认，优先看共识与历史标签") if strategy_count < 3 else ("可观察", "共识较好但仍需控制仓位")
    if state_group == "risk_on":
        if risk_flags:
            return "环境支持但有风险", "情绪配合，仍需处理诊断风险"
        return "环境支持", "市场情绪对进攻候选较友好"
    if state_group == "overheat":
        if pct >= 8.5:
            return "防兑现", "高潮期临近涨停候选需防次日分歧"
        return "降追高", "过热环境下优先看低回撤确认"
    return "正常观察", "按四策略/XGB/历史标签综合判断"


def _badge(summary: Dict[str, Any], action: str) -> str:
    state = summary.get("state") or "情绪未知"
    multiplier = safe_float(summary.get("position_multiplier"), 0.0)
    return f"{state} x{multiplier:.2f} {action}"


def _compact(summary: Dict[str, Any], action: str, reason: str, score: float) -> str:
    state = summary.get("state") or "情绪未知"
    return f"{state} | {action} | 环境分{score:.2f} | {reason}"
