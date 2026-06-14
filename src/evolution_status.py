"""Evolution roadmap status derived from V2.0 runtime artifacts."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.common import output_root
from src.snapshot_manager import live_current_dir
from src.x1_preheat import latest_status as x1_preheat_status


def build_evolution_status(cfg: Dict[str, Any]) -> Dict[str, Any]:
    out = output_root(cfg)
    pipeline_path, pipeline = _latest_json(out / "json", "pipeline_v2_*.json")
    factor_path, factor = _latest_json(out / "factors", "candidate_factor_panel_latest_*.json")
    pattern_path, pattern = _latest_json(out / "patterns", "historical_pattern_tags_current.json")
    outcomes_path = out / "tracking" / "outcomes_current.json"
    outcomes = _load_json(outcomes_path)
    tracking_file = out / "tracking" / "candidates.jsonl"
    snapshot_dir = live_current_dir(cfg)
    snapshot_meta = _load_json(snapshot_dir / "snapshot_meta.json")
    x1_status = x1_preheat_status(cfg, snapshot_dir)

    summary = pipeline.get("summary") or {}
    diagnosis = pipeline.get("diagnosis") or {}
    outcome_summary = outcomes.get("summary") or {}
    factor_rows = int((factor or {}).get("row_count") or 0)
    pattern_candidates = len((pattern or {}).get("candidate_tags") or [])
    joined_outcomes = int((pattern or {}).get("joined_outcome_count") or 0)
    tracked_count = int(outcome_summary.get("tracked_count") or 0)
    pending_count = int(outcome_summary.get("pending_count") or 0)
    tracking_records = _count_lines(tracking_file)
    snapshot_file_count = len(list(snapshot_dir.glob("*.txt"))) if snapshot_dir.exists() else 0

    snapshot_ok = str(snapshot_meta.get("primary_source") or "") == "tushare_rt_k" and snapshot_file_count >= 2000
    strategies_ok = int(summary.get("strategies_ok") or 0)
    strategies_run = int(summary.get("strategies_run") or 0)
    xgb_total = int(diagnosis.get("total") or diagnosis.get("candidate_count") or 0)
    xgb_diagnosed = int(diagnosis.get("diagnosed_count") or xgb_total or 0)
    xgb_covered = xgb_total > 0 and xgb_diagnosed >= xgb_total

    phases: List[Dict[str, Any]] = [
        {
            "key": "phase0",
            "name": "Phase 0 稳定基线与控制台清理",
            "status": "done" if snapshot_ok and strategies_ok >= 4 and bool(x1_status.get("usable")) and xgb_covered else "in_progress",
            "progress": _progress([snapshot_ok, strategies_ok >= 4, bool(x1_status.get("usable")), xgb_covered]),
            "evidence": [
                f"快照主源={snapshot_meta.get('primary_source') or '-'}，文件={snapshot_file_count}",
                f"四策略={strategies_ok}/{strategies_run or 4}",
                f"X1预热={'可用' if x1_status.get('usable') else '待预热'}",
                f"XGB覆盖={xgb_diagnosed}/{xgb_total}",
            ],
        },
        {
            "key": "phase1",
            "name": "Phase 1 验证体系与盘后追踪",
            "status": "done" if tracking_records > 0 and tracked_count > 0 else "in_progress" if tracking_records > 0 else "pending",
            "progress": _progress([tracking_records > 0, bool(outcomes.get("generated_at")), tracked_count > 0, pending_count == 0 and tracked_count > 0]),
            "evidence": [
                f"追踪记录={tracking_records}",
                f"收益样本=已回填{tracked_count}/等待{pending_count}",
                f"收益文件={outcomes_path.name if outcomes_path.exists() else '未生成'}",
            ],
        },
        {
            "key": "phase2",
            "name": "Phase 2 因子宽表与历史模式标签",
            "status": "done" if factor_rows > 0 and joined_outcomes > 0 else "in_progress" if factor_rows > 0 or pattern_candidates > 0 else "pending",
            "progress": _progress([factor_rows > 0, pattern_candidates > 0, joined_outcomes > 0, int((pattern or {}).get("group_count") or 0) > 0]),
            "evidence": [
                f"宽表行数={factor_rows}",
                f"候选标签={pattern_candidates}",
                f"已连接真实收益样本={joined_outcomes}",
            ],
        },
        {
            "key": "phase3",
            "name": "Phase 3 情绪周期与可交易性过滤",
            "status": "pending",
            "progress": 0,
            "evidence": ["尚未进入正式落地；等待追踪样本稳定后接入情绪/流动性过滤。"],
        },
        {
            "key": "phase4",
            "name": "Phase 4 XGB Meta-labeling 过滤层",
            "status": "in_progress" if xgb_covered else "pending",
            "progress": 50 if xgb_covered else 0,
            "evidence": [
                "XGB 已作为候选诊断验证层运行。",
                "Meta-labeling 仍需真实追踪标签累积后校准。",
            ],
        },
    ]

    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "current_stage": _current_stage(tracked_count, joined_outcomes),
        "pipeline_file": pipeline_path.name if pipeline_path else "",
        "factor_file": factor_path.name if factor_path else "",
        "pattern_file": pattern_path.name if pattern_path else "",
        "phases": phases,
        "next_actions": _next_actions(tracked_count, joined_outcomes, factor_rows, bool(x1_status.get("usable"))),
    }


def _load_json(path: Path) -> Dict[str, Any]:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return {}


def _latest_json(root: Path, pattern: str) -> tuple[Optional[Path], Dict[str, Any]]:
    if not root.exists():
        return None, {}
    files = sorted(root.glob(pattern), key=lambda path: path.stat().st_mtime, reverse=True)
    if not files:
        return None, {}
    path = files[0]
    return path, _load_json(path)


def _count_lines(path: Path) -> int:
    if not path.exists():
        return 0
    try:
        with path.open("r", encoding="utf-8", errors="ignore") as handle:
            return sum(1 for line in handle if line.strip())
    except Exception:
        return 0


def _progress(items: List[bool]) -> int:
    if not items:
        return 0
    return int(round(sum(1 for item in items if item) / len(items) * 100))


def _current_stage(tracked_count: int, joined_outcomes: int) -> str:
    if tracked_count <= 0:
        return "V2.1 稳定验证期：稳定出票链路已跑通，正在等待真实收益样本回填。"
    if joined_outcomes <= 0:
        return "V2.1 盘后追踪期：已有收益标签，正在积累历史模式样本。"
    return "V2.2 因子宽表期：开始用历史胜率和诊断标签辅助横截面排序。"


def _next_actions(tracked_count: int, joined_outcomes: int, factor_rows: int, x1_usable: bool) -> List[str]:
    actions: List[str] = []
    if not x1_usable:
        actions.append("尾盘前先完成 X1Beam 预热，只有完整缓存才作为第四策略。")
    if tracked_count <= 0:
        actions.append("下个交易日盘后运行 post-market-refresh，让最近出票开始产生次日冲高和5日追踪标签。")
    if factor_rows <= 0:
        actions.append("生成 candidate_factor_panel，先把策略/XGB/风险字段摊平成可统计宽表。")
    if joined_outcomes <= 0:
        actions.append("等待真实收益样本后刷新 historical_pattern_tags，把高胜率/高回撤模式贴回 Top10。")
    actions.append("继续做手动 V10/V1/V4 结果对比，差异样本进入 parity 审计。")
    return actions[:5]
