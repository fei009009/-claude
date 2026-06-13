from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

from src.common import norm_code, write_report
from src.diagnosis.engine import DiagnosisEngine, DiagnosisResult
from src.diagnosis.report import build_diagnosis_report


def collect_candidates_from_strategy_results(results: Iterable[Dict[str, Any]]) -> List[Dict[str, str]]:
    candidates: List[Dict[str, str]] = []
    seen = set()
    for result in results:
        if not result.get("ok"):
            continue
        for row in result.get("top") or []:
            code = norm_code(row.get("code"))
            if not code or code in seen:
                continue
            seen.add(code)
            candidates.append({"code": code, "name": str(row.get("name") or "")})
    return candidates


def run_xgb_validation_layer(
    cfg: Dict[str, Any],
    results: Iterable[Dict[str, Any]],
    snapshot_dir: Path | None = None,
    candidates_source: str = "V10/V1/V4/X1Beam candidates",
    persist: bool = True,
) -> Tuple[List[DiagnosisResult], Dict[str, Any]]:
    candidates = collect_candidates_from_strategy_results(results)
    if not cfg.get("diagnosis", {}).get("enabled", True):
        return [], {"enabled": False, "role": "validation_layer", "reason": "disabled"}
    if not candidates:
        return [], {"enabled": True, "role": "validation_layer", "total_diagnosed": 0, "reason": "no_candidates"}

    engine = DiagnosisEngine(
        xgb_dir=Path(str((cfg.get("paths") or {}).get("xgb_dir") or "")),
        xgbzx_dir=Path(str((cfg.get("paths") or {}).get("xgbzx_dir") or "")),
        data_dir=snapshot_dir if snapshot_dir else None,
        x1_dir=Path(str((cfg.get("paths") or {}).get("x1_xin_dir") or "")),
    )
    if not engine.validate_environment() or not engine.load_models():
        return [], {
            "enabled": True,
            "role": "validation_layer",
            "total_diagnosed": 0,
            "error": "xgb_environment_not_ready",
        }

    diagnosis_results = engine.diagnose_candidates(candidates, snapshot_dir=snapshot_dir)
    report = build_diagnosis_report(
        diagnosis_results,
        candidates_source=candidates_source,
        snapshot_dir=str(snapshot_dir) if snapshot_dir else "",
    )
    if persist:
        report_path = write_report("xgb_diagnosis", report, cfg)
        report["report_path"] = str(report_path)
    summary = {
        "enabled": True,
        "role": "validation_layer",
        "independent_strategy": False,
        "total": report.get("total_diagnosed", 0),
        "signal_distribution": report.get("signal_distribution") or {},
        "score_distribution": report.get("score_distribution") or {},
        "top_picks": report.get("top_picks") or [],
        "watch_list": report.get("watch_list") or [],
        "report_path": report.get("report_path", ""),
        "summary": report.get("summary", ""),
    }
    return diagnosis_results, summary
