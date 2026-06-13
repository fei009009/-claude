"""XGB diagnosis reporting for V2.0 validation layer."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from src.diagnosis.engine import DiagnosisResult


def build_diagnosis_report(
    results: List[DiagnosisResult],
    candidates_source: str = "",
    snapshot_dir: Optional[str] = None,
) -> Dict[str, Any]:
    total = len(results)
    signals = {"STRONG_BUY": 0, "BUY": 0, "WATCH": 0, "NEUTRAL": 0, "SKIP": 0}
    for item in results:
        signals[item.signal] = signals.get(item.signal, 0) + 1

    score_bands = {"0.80-1.00": 0, "0.60-0.80": 0, "0.40-0.60": 0, "0.00-0.40": 0}
    for item in results:
        score = item.blended_score
        if score >= 0.80:
            score_bands["0.80-1.00"] += 1
        elif score >= 0.60:
            score_bands["0.60-0.80"] += 1
        elif score >= 0.40:
            score_bands["0.40-0.60"] += 1
        else:
            score_bands["0.00-0.40"] += 1

    risk_counts: Dict[str, int] = {}
    for item in results:
        for flag in item.risk_flags:
            risk_counts[flag] = risk_counts.get(flag, 0) + 1

    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "role": "validation_layer",
        "independent_strategy": False,
        "candidates_source": candidates_source,
        "snapshot_dir": snapshot_dir,
        "total_diagnosed": total,
        "signal_distribution": signals,
        "score_distribution": score_bands,
        "top_risk_flags": [
            {"flag": flag, "count": count}
            for flag, count in sorted(risk_counts.items(), key=lambda pair: -pair[1])[:10]
        ],
        "results": [item.to_dict() for item in results],
        "top_picks": [item.to_dict() for item in results if item.signal in ("STRONG_BUY", "BUY")][:10],
        "watch_list": [item.to_dict() for item in results if item.signal == "WATCH"][:10],
        "summary": _build_summary_text(results, signals),
    }


def _build_summary_text(results: List[DiagnosisResult], signals: Dict[str, int]) -> str:
    strong = signals.get("STRONG_BUY", 0)
    buy = signals.get("BUY", 0)
    watch = signals.get("WATCH", 0)
    total = len(results)
    parts = [f"XGB 诊断完成：共 {total} 只，定位为策略验证层，不参与四策略交集计数。"]
    if strong:
        parts.append(f"STRONG_BUY {strong} 只")
    if buy:
        parts.append(f"BUY {buy} 只")
    if watch:
        parts.append(f"WATCH {watch} 只")
    top3 = results[:3]
    if top3:
        parts.append("Top 诊断：")
        for index, item in enumerate(top3, 1):
            parts.append(f"  #{index} {item.code} {item.name} 评分={item.blended_score:.1%} 信号={item.signal}")
    return "\n".join(parts)


def build_markdown_diagnosis(results: List[DiagnosisResult], max_chars: int = 3000) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        f"## XGB 诊断验证层 {now}",
        "",
        "| 代码 | 名称 | 评分 | 模型 | 规则 | 信号 |",
        "|------|------|------|------|------|------|",
    ]
    for item in results[:15]:
        lines.append(
            f"| {item.code} | {item.name} | {item.blended_score:.1%} | "
            f"{item.model_score:.1%} | {item.rule_score:.1%} | {item.signal} |"
        )
    if not results:
        lines.append("| - | 无候选 | - | - | - | - |")
    lines.append("")
    lines.append("> XGB 仅作为验证层，不作为独立选股策略。")
    markdown = "\n".join(lines)
    if len(markdown) > max_chars:
        markdown = markdown[: max_chars - 50] + "\n\n> 报告过长已截断"
    return markdown
