"""分仓之神 V2.0 — 选股后分析模块（交集追踪、胜率统计、日报）"""
from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional


def _output_root(cfg: Dict[str, Any]) -> Path:
    return Path(cfg.get("paths", {}).get("output_root", "outputs"))


def _json_dir(cfg: Dict[str, Any]) -> Path:
    d = _output_root(cfg) / "json"
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_latest_pipeline(output_root: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """加载最近一次 pipeline_v2_*.json 结果"""
    search_dir = output_root / "json" if output_root else output_root or Path("outputs") / "json"
    if not search_dir.exists():
        return None
    paths = sorted(search_dir.glob("pipeline_v2_*.json"), key=lambda p: p.stat().st_mtime)
    if not paths:
        return None
    try:
        return json.loads(paths[-1].read_text(encoding="utf-8"))
    except Exception:
        return None


def build_run_report(
    results: List[Dict[str, Any]],
    overlap: Dict[str, Any],
    cfg: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """从策略运行结果构建分析报告

    Args:
        results: 策略运行结果列表
        overlap: 交集分析结果
        cfg: 全局配置（可选）
    """
    # 策略成功率统计
    strategy_stats = {}
    for r in results:
        name = r.get("display_name", r.get("strategy_name", "unknown"))
        strategy_stats[name] = {
            "ok": r.get("ok", False),
            "top_count": len(r.get("top", [])),
            "elapsed_seconds": r.get("elapsed_seconds", 0),
            "error": r.get("error", ""),
        }

    # 代码出现频率
    code_freq: Dict[str, int] = Counter()
    for r in results:
        if r.get("ok"):
            for row in r.get("top", []):
                code_freq[row.get("code", "")] += 1

    # 多策略共识股
    consensus = [
        {"code": code, "strategy_count": count}
        for code, count in code_freq.most_common(20)
        if count >= 2
    ]

    # 策略独有候选
    exclusive: Dict[str, List[str]] = {}
    for r in results:
        if not r.get("ok"):
            continue
        name = r.get("display_name", r.get("strategy_name", "unknown"))
        my_codes = {row.get("code") for row in r.get("top", [])}
        all_other_codes = set()
        for r2 in results:
            if r2 is r or not r2.get("ok"):
                continue
            all_other_codes |= {row.get("code") for row in r2.get("top", [])}
        exclusive[name] = sorted(my_codes - all_other_codes)[:5]

    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "strategy_stats": strategy_stats,
        "overlap_summary": overlap.get("by_count", {}),
        "total_overlaps": overlap.get("total_overlaps", 0),
        "consensus_top": consensus,
        "exclusive_picks": exclusive,
        "code_frequency": {k: v for k, v in code_freq.most_common(30)},
    }

    return report


def build_daily_tracking_report(
    cfg: Dict[str, Any],
    days: int = 7,
) -> Dict[str, Any]:
    """生成最近 N 天的每日跟踪报告

    聚合所有 pipeline_v2_*.json，统计：
    - 交易日覆盖
    - 策略成功率
    - 交集稳定度
    - 推荐关注列表
    """
    json_dir = _json_dir(cfg)
    if not json_dir.exists():
        return {"status": "no_data", "days": days, "runs": []}

    cutoff = datetime.now() - timedelta(days=days)
    runs: List[Dict[str, Any]] = []
    paths = sorted(json_dir.glob("pipeline_v2_*.json"), key=lambda p: p.stat().st_mtime)

    for path in paths:
        try:
            mtime = datetime.fromtimestamp(path.stat().st_mtime)
            if mtime < cutoff:
                continue
            data = json.loads(path.read_text(encoding="utf-8"))
            runs.append({
                "timestamp": data.get("timestamp", ""),
                "snapshot_dir": data.get("snapshot_dir", ""),
                "strategies_run": data.get("summary", {}).get("strategies_run", 0),
                "strategies_ok": data.get("summary", {}).get("strategies_ok", 0),
                "total_candidates": data.get("summary", {}).get("total_candidates", 0),
                "overlap_candidates": data.get("summary", {}).get("overlap_candidates", 0),
                "overlap_by_count": data.get("overlap", {}).get("by_count", {}),
            })
        except Exception:
            continue

    if not runs:
        return {"status": "no_recent_data", "days": days, "runs": []}

    avg_total = sum(r.get("total_candidates", 0) for r in runs) / max(len(runs), 1)
    avg_overlap = sum(r.get("overlap_candidates", 0) for r in runs) / max(len(runs), 1)
    success_rate = (
        sum(1 for r in runs if r.get("strategies_ok", 0) >= 3) / max(len(runs), 1)
    )

    report = {
        "status": "ok",
        "days": days,
        "run_count": len(runs),
        "avg_total_candidates": round(avg_total, 1),
        "avg_overlap_candidates": round(avg_overlap, 1),
        "pipeline_success_rate": round(success_rate, 3),
        "recommendation": "",
        "runs": runs,
    }

    if success_rate < 0.5:
        report["recommendation"] = "流水线成功率低，建议检查数据源连通性和策略环境配置"
    elif avg_overlap < 2:
        report["recommendation"] = "多策略交集偏少，四策略共识度较低，关注单策略稳定性"
    else:
        report["recommendation"] = "流水线运行稳定，多策略交集正常"

    return report


def build_latency_breakdown(
    results: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """分析各策略耗时分布"""
    elapsed = {}
    for r in results:
        name = r.get("display_name", r.get("strategy_name", "unknown"))
        elapsed[name] = r.get("elapsed_seconds", 0)

    sorted_elapsed = sorted(elapsed.items(), key=lambda x: -x[1])
    bottleneck = sorted_elapsed[0] if sorted_elapsed else ("unknown", 0)

    return {
        "per_strategy": elapsed,
        "bottleneck": {"strategy": bottleneck[0], "elapsed_seconds": bottleneck[1]},
        "total_elapsed": sum(elapsed.values()),
    }


def persist_report(
    report: Dict[str, Any],
    report_type: str,
    cfg: Optional[Dict[str, Any]] = None,
) -> Path:
    """将分析报告持久化到 outputs/json/"""
    output_dir = _output_root(cfg or {})
    report_dir = output_dir / "json"
    report_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = report_dir / f"{report_type}_{ts}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return path
