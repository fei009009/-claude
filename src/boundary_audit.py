"""分仓之神 V2.0 — 边界审计模块（Phase 3.3: 临界涨幅/硬过滤风险检测）"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


def scan_boundary_candidates(
    snapshot_dir: Path,
    results: List[Dict[str, Any]],
    pct_range: Tuple[float, float] = (8.5, 10.5),
) -> Dict[str, Any]:
    """扫描快照中的临界涨幅候选股

    检测 8.5%-10.5% 涨幅区间内的股票，这些可能因 9% 硬过滤阈值
    在不同数据源下产生分歧。

    Args:
        snapshot_dir: 快照目录
        results: 策略运行结果（用于标记已入选/未入选）
        pct_range: 涨幅检测范围 (min_pct, max_pct)

    Returns:
        {candidates, risks, stats}
    """
    if not snapshot_dir.exists():
        return {"candidates": [], "risks": [], "stats": {"scanned": 0, "critical": 0, "risk": 0}}

    # 收集已入选的股票代码
    selected_codes: set = set()
    for r in results:
        if r.get("ok"):
            for row in r.get("top", []):
                code = row.get("code", "")
                if code:
                    selected_codes.add(code)

    # 收集各策略排名
    rank_map: Dict[str, Dict[str, int]] = {}
    for r in results:
        if r.get("ok"):
            name = r.get("strategy_name", "?")
            for row in r.get("top", []):
                code = row.get("code", "")
                if code:
                    rank_map.setdefault(code, {})[name] = row.get("rank", 99)

    candidates = []
    risks = []
    scanned = 0
    critical = 0

    for txt_file in snapshot_dir.glob("SH#*.txt"):
        scanned += 1
        try:
            lines = txt_file.read_text(encoding="gbk", errors="replace").strip().split("\n")
            if len(lines) < 3:
                continue

            # 解析股票名
            header = lines[0].strip().split()
            name = header[1] if len(header) >= 2 else ""
            full_code = txt_file.stem  # e.g., SH#600000

            # 解析最后两根K线计算涨幅
            last_line = lines[-1].strip()
            if not last_line or "\t" not in last_line:
                continue
            parts = last_line.split("\t")
            if len(parts) < 5:
                continue

            close = float(parts[4])
            pre_close = None

            # 找前一交易日收盘价
            for line in reversed(lines[:-1]):
                lp = line.strip().split("\t")
                if len(lp) >= 5 and "/" in lp[0]:
                    pre_close = float(lp[4])
                    break

            if pre_close is None or pre_close <= 0:
                continue

            pct = (close - pre_close) / pre_close * 100

            if pct_range[0] <= pct <= pct_range[1]:
                critical += 1
                code = full_code.replace("#", "")
                in_top = full_code in selected_codes or code in selected_codes
                ranks = rank_map.get(full_code, rank_map.get(code, {}))

                candidate = {
                    "code": code, "name": name,
                    "close": round(close, 2), "pre_close": round(pre_close, 2),
                    "pct": round(pct, 4),
                    "in_any_top": in_top,
                    "strategy_ranks": ranks,
                }

                # 风险评估
                is_risk = False
                risk_reasons = []
                if pct >= 8.8 and pct <= 9.2:
                    risk_reasons.append("near_9pct_filter")
                    is_risk = True
                if pct >= 9.7 and pct <= 10.2:
                    risk_reasons.append("near_limit_up_filter")
                    is_risk = True
                if not in_top and pct >= 8.7:
                    risk_reasons.append("critical_not_in_top")
                    is_risk = True

                candidate["risk_reasons"] = risk_reasons
                candidate["is_risk"] = is_risk

                if is_risk:
                    risks.append(candidate)
                candidates.append(candidate)

        except Exception:
            continue

    # 排序：风险优先，接近9%的优先
    risks.sort(key=lambda x: (
        not x.get("in_any_top", False),
        -len(x.get("risk_reasons", [])),
        abs(x.get("pct", 0) - 9.0),
    ))
    candidates.sort(key=lambda x: (
        not x.get("in_any_top", False),
        abs(x.get("pct", 0) - 9.0),
    ))

    return {
        "candidates": candidates[:50],
        "risks": risks[:20],
        "stats": {
            "scanned": scanned,
            "critical": critical,
            "risk_count": len(risks),
            "pct_range": list(pct_range),
        },
    }
