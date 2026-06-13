"""Lightweight metrics used by post-market tracking and review."""
from __future__ import annotations

import math
from typing import Any, Dict, Iterable, List


def _clean(values: Iterable[Any]) -> List[float]:
    cleaned: List[float] = []
    for value in values:
        try:
            number = float(value)
        except Exception:
            continue
        if math.isfinite(number):
            cleaned.append(number)
    return cleaned


def mean(values: Iterable[Any], default: float = 0.0) -> float:
    data = _clean(values)
    if not data:
        return default
    return sum(data) / len(data)


def stdev(values: Iterable[Any], sample: bool = True, default: float = 0.0) -> float:
    data = _clean(values)
    if len(data) < 2:
        return default
    avg = mean(data)
    denom = len(data) - 1 if sample else len(data)
    if denom <= 0:
        return default
    return math.sqrt(sum((value - avg) ** 2 for value in data) / denom)


def win_rate(values: Iterable[Any], threshold: float = 0.0) -> float:
    data = _clean(values)
    if not data:
        return 0.0
    return sum(1 for value in data if value > threshold) / len(data)


def hit_rate(values: Iterable[Any], threshold: float) -> float:
    data = _clean(values)
    if not data:
        return 0.0
    return sum(1 for value in data if value >= threshold) / len(data)


def downside_deviation(values: Iterable[Any], target: float = 0.0) -> float:
    data = _clean(values)
    downside = [min(0.0, value - target) for value in data]
    if not downside:
        return 0.0
    return math.sqrt(sum(value * value for value in downside) / len(downside))


def max_drawdown(equity_curve: Iterable[Any]) -> float:
    data = _clean(equity_curve)
    if not data:
        return 0.0
    peak = data[0]
    worst = 0.0
    for value in data:
        if value > peak:
            peak = value
        if peak == 0:
            continue
        drawdown = value / peak - 1.0
        if drawdown < worst:
            worst = drawdown
    return worst


def returns_to_equity(returns: Iterable[Any], start: float = 1.0) -> List[float]:
    equity: List[float] = []
    current = start
    for value in _clean(returns):
        current *= 1.0 + value
        equity.append(current)
    return equity


def sharpe_ratio(returns: Iterable[Any], risk_free: float = 0.0) -> float:
    data = [value - risk_free for value in _clean(returns)]
    vol = stdev(data)
    if not data or vol == 0:
        return 0.0
    return mean(data) / vol


def sortino_ratio(returns: Iterable[Any], target: float = 0.0) -> float:
    data = _clean(returns)
    dd = downside_deviation(data, target=target)
    if not data or dd == 0:
        return 0.0
    return (mean(data) - target) / dd


def calmar_ratio(returns: Iterable[Any]) -> float:
    data = _clean(returns)
    if not data:
        return 0.0
    equity = returns_to_equity(data)
    mdd = abs(max_drawdown(equity))
    if mdd == 0:
        return 0.0
    return mean(data) / mdd


def summarize_returns(
    returns: Iterable[Any],
    *,
    profit_threshold: float = 0.0,
    target_threshold: float = 0.05,
) -> Dict[str, float]:
    data = _clean(returns)
    if not data:
        return {
            "count": 0,
            "avg_return": 0.0,
            "median_return": 0.0,
            "win_rate": 0.0,
            "target_hit_rate": 0.0,
            "max_drawdown": 0.0,
            "sharpe": 0.0,
            "sortino": 0.0,
            "calmar": 0.0,
        }

    sorted_data = sorted(data)
    mid = len(sorted_data) // 2
    if len(sorted_data) % 2:
        median = sorted_data[mid]
    else:
        median = (sorted_data[mid - 1] + sorted_data[mid]) / 2

    equity = returns_to_equity(data)
    return {
        "count": float(len(data)),
        "avg_return": mean(data),
        "median_return": median,
        "win_rate": win_rate(data, profit_threshold),
        "target_hit_rate": hit_rate(data, target_threshold),
        "max_drawdown": max_drawdown(equity),
        "sharpe": sharpe_ratio(data),
        "sortino": sortino_ratio(data),
        "calmar": calmar_ratio(data),
    }
