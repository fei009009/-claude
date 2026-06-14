from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

from src.common import norm_code, repair_mojibake, write_report
from src.diagnosis.bin_sidecar import run_bin_sidecar
from src.diagnosis.engine import DiagnosisEngine, DiagnosisResult
from src.diagnosis.report import build_diagnosis_report
from src.diagnosis.xgb_calibration import ensure_xgb_vendor_assets


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
            candidates.append({"code": code, "name": repair_mojibake(row.get("name") or "")})
    return candidates


def _coverage_fields(
    candidates: List[Dict[str, str]],
    diagnosis_results: List[DiagnosisResult],
    sidecar_meta: Dict[str, Any] | None = None,
    *,
    candidates_source: str = "",
) -> Dict[str, Any]:
    sidecar_meta = sidecar_meta or {}
    candidate_names = {norm_code(item.get("code")): repair_mojibake(item.get("name") or "") for item in candidates}
    diagnosed_codes = {
        norm_code(item.code if hasattr(item, "code") else getattr(item, "get", lambda _k, _d=None: _d)("code"))
        for item in diagnosis_results or []
    }
    skipped = []
    for err in sidecar_meta.get("errors") or []:
        code = norm_code(err.get("code"))
        if not code:
            continue
        skipped.append({
            "code": code,
            "name": candidate_names.get(code, ""),
            "reason": str(err.get("error") or "diagnosis_failed"),
        })
    missing_without_error = sorted(set(candidate_names) - diagnosed_codes - {item["code"] for item in skipped})
    for code in missing_without_error:
        skipped.append({
            "code": code,
            "name": candidate_names.get(code, ""),
            "reason": "not_diagnosed",
        })
    candidate_count = len(candidates)
    diagnosed_count = len(diagnosed_codes)
    skipped_count = len(skipped)
    return {
        "candidates_source": candidates_source,
        "candidate_count": candidate_count,
        "diagnosed_count": diagnosed_count,
        "skipped_count": skipped_count,
        "coverage_rate": round(diagnosed_count / candidate_count, 4) if candidate_count else 0.0,
        "skipped": skipped[:50],
    }


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

    diagnosis_cfg = cfg.get("diagnosis", {}) or {}
    xgb_dir = Path(str((cfg.get("paths") or {}).get("xgb_dir") or ""))
    if diagnosis_cfg.get("engine", "bin_sidecar") != "legacy":
        try:
            ensure_xgb_vendor_assets(cfg, include_samples=False)
            diagnosis_results, sidecar_meta = run_bin_sidecar(
                xgb_dir=xgb_dir,
                snapshot_dir=snapshot_dir or Path(""),
                candidates=candidates,
                limit_rules=int(diagnosis_cfg.get("bin_limit_rules", 0) or 0),
                top_matches=int(diagnosis_cfg.get("bin_top_matches", 8) or 8),
                rich_reports=bool(diagnosis_cfg.get("rich_reports", True)),
                rich_report_top_n=int(diagnosis_cfg.get("rich_report_top_n", 10) or 10),
            )
            if diagnosis_results:
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
                    "engine": sidecar_meta.get("engine", "xgb_25bin_sidecar"),
                    "independent_strategy": False,
                    "total": report.get("total_diagnosed", 0),
                    "signal_distribution": report.get("signal_distribution") or {},
                    "score_distribution": report.get("score_distribution") or {},
                    "top_picks": report.get("top_picks") or [],
                    "watch_list": report.get("watch_list") or [],
                    "report_path": report.get("report_path", ""),
                    "summary": report.get("summary", ""),
                    "sidecar": sidecar_meta,
                }
                summary.update(_coverage_fields(
                    candidates,
                    diagnosis_results,
                    sidecar_meta,
                    candidates_source=candidates_source,
                ))
                return diagnosis_results, summary
        except Exception as exc:
            if not diagnosis_cfg.get("allow_legacy_fallback", True):
                raise
            print(f"[DiagnosisService] bin sidecar fallback: {exc}")

    engine = DiagnosisEngine(
        xgb_dir=xgb_dir,
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
    summary.update(_coverage_fields(
        candidates,
        diagnosis_results,
        {},
        candidates_source=candidates_source,
    ))
    return diagnosis_results, summary
