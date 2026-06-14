"""End-to-end health audit for V2.0 data, strategies, diagnosis and tracking."""
from __future__ import annotations

import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from src.common import norm_code, safe_int
from src.quality_gate import audit_snapshot, resolve_snapshot
from src.tracking_outcomes import load_current_outcomes
from src.tracking_store import build_candidate_records, json_dir, load_tracking_records, output_root
from src.x1_preheat import latest_status as x1_preheat_status


EXPECTED_STRATEGIES = ("v10", "v1", "v4", "x1beam")


def build_health_audit(
    cfg: Dict[str, Any],
    *,
    official: bool = False,
    persist: bool = False,
) -> Dict[str, Any]:
    active_snapshot, snapshot_source = resolve_snapshot(cfg)
    pipeline_path, pipeline = _latest_pipeline(cfg)
    pipeline_snapshot = Path(str(pipeline.get("snapshot_dir") or active_snapshot)) if pipeline else active_snapshot
    audit_snapshot_dir = pipeline_snapshot if pipeline_snapshot.exists() else active_snapshot
    audit_snapshot_source = "latest_pipeline.snapshot_dir" if pipeline and pipeline_snapshot.exists() else snapshot_source
    quality = audit_snapshot(audit_snapshot_dir, cfg, official=official)
    trade_date = _pipeline_trade_date(pipeline) or _quality_trade_date(quality)

    checks: List[Dict[str, Any]] = []

    def add(area: str, name: str, ok: bool, detail: str, *, warning: bool = False, data: Optional[Dict[str, Any]] = None) -> None:
        checks.append({
            "area": area,
            "name": name,
            "ok": bool(ok),
            "warning": bool(warning),
            "detail": detail,
            "data": data or {},
        })

    add(
        "snapshot",
        "快照质量",
        bool(quality.get("ok")),
        _quality_detail(quality, audit_snapshot_source, audit_snapshot_dir),
        data=quality.get("metrics") or {},
    )
    if pipeline and active_snapshot.resolve() != pipeline_snapshot.resolve():
        add(
            "snapshot",
            "活动快照与最新运行快照",
            False,
            f"活动={active_snapshot} ({snapshot_source}) | 最新运行={pipeline_snapshot}",
            warning=True,
            data={
                "active_snapshot_dir": str(active_snapshot),
                "active_snapshot_source": snapshot_source,
                "pipeline_snapshot_dir": str(pipeline_snapshot),
            },
        )
    for blocker in (quality.get("blockers") or [])[:5]:
        add("snapshot", "快照阻断项", False, str(blocker))
    if not pipeline:
        add("pipeline", "最新运行结果", False, "未找到 pipeline_v2_*.json")
        return _finish(checks, quality, {}, None, None, persist, cfg)

    add(
        "pipeline",
        "最新运行结果",
        True,
        f"{pipeline_path.name} | {pipeline.get('timestamp', '')} | snapshot={pipeline_snapshot}",
        data={"pipeline": str(pipeline_path), "snapshot_dir": str(pipeline_snapshot)},
    )

    strategy_report = _audit_strategies(cfg, pipeline, audit_snapshot_dir, trade_date, add)
    xgb_report = _audit_xgb(pipeline, trade_date, strategy_report.get("union_codes", set()), add)
    x1_report = _audit_x1(cfg, audit_snapshot_dir, pipeline, add)
    tracking_report = _audit_tracking(cfg, pipeline, pipeline_path, add)
    step_report = _audit_required_steps(pipeline, add)

    return _finish(
        checks,
        quality,
        {
            "strategies": strategy_report,
            "xgb": xgb_report,
            "x1beam": x1_report,
            "tracking": tracking_report,
            "steps": step_report,
        },
        pipeline_path,
        pipeline,
        persist,
        cfg,
    )


def _finish(
    checks: List[Dict[str, Any]],
    quality: Dict[str, Any],
    sections: Dict[str, Any],
    pipeline_path: Optional[Path],
    pipeline: Optional[Dict[str, Any]],
    persist: bool,
    cfg: Dict[str, Any],
) -> Dict[str, Any]:
    blocking = sum(1 for item in checks if not item["ok"] and not item["warning"])
    warnings = sum(1 for item in checks if not item["ok"] and item["warning"])
    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "status": "pass" if blocking == 0 else "blocked",
        "blocking": blocking,
        "warnings": warnings,
        "checks": checks,
        "snapshot_quality": quality,
        "pipeline_file": pipeline_path.name if pipeline_path else "",
        "pipeline_path": str(pipeline_path) if pipeline_path else "",
        "pipeline_timestamp": (pipeline or {}).get("timestamp", ""),
        "sections": sections,
    }
    if persist:
        out_dir = output_root(cfg) / "reports"
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"health_audit_{datetime.now():%Y%m%d_%H%M%S}.json"
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        report["report_path"] = str(path)
    else:
        report["report_path"] = ""
    return report


def _latest_pipeline(cfg: Dict[str, Any]) -> Tuple[Optional[Path], Dict[str, Any]]:
    paths = sorted(json_dir(cfg).glob("pipeline_v2_*.json"), key=lambda p: p.stat().st_mtime)
    if not paths:
        return None, {}
    path = paths[-1]
    try:
        return path, json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return path, {}


def _quality_trade_date(quality: Dict[str, Any]) -> str:
    metrics = quality.get("metrics") or {}
    meta = quality.get("meta") or {}
    return str(
        metrics.get("expected_trade_date")
        or metrics.get("observed_trade_date")
        or metrics.get("meta_trade_date")
        or meta.get("trade_date")
        or ""
    )


def _pipeline_trade_date(pipeline: Dict[str, Any]) -> str:
    quality = pipeline.get("snapshot_quality") or {}
    return _quality_trade_date(quality)


def _quality_detail(quality: Dict[str, Any], source: str, snapshot_dir: Path) -> str:
    metrics = quality.get("metrics") or {}
    return (
        f"{'通过' if quality.get('ok') else '阻断'} | source={source} | "
        f"files={metrics.get('file_count', 0)} empty={metrics.get('empty_files', 0)} "
        f"date={metrics.get('expected_trade_date', '')} discontinuous={metrics.get('discontinuous_count', 0)} "
        f"zero={metrics.get('zero_close_count', 0)} | {snapshot_dir}"
    )


def _strategy_key(value: Any) -> str:
    text = str(value or "").lower()
    if "x1" in text or "beam" in text:
        return "x1beam"
    if "v10" in text or "vip" in text:
        return "v10"
    if "v4" in text:
        return "v4"
    if "v1" in text:
        return "v1"
    return text.strip()


def _audit_strategies(
    cfg: Dict[str, Any],
    pipeline: Dict[str, Any],
    snapshot_dir: Path,
    trade_date: str,
    add,
) -> Dict[str, Any]:
    results = pipeline.get("strategies") or []
    by_key = {_strategy_key(item.get("strategy_name") or item.get("display_name")): item for item in results}
    union_codes: set[str] = set()
    per_strategy: Dict[str, Any] = {}
    strategy_cfg = cfg.get("strategies") or {}

    for key in EXPECTED_STRATEGIES:
        result = by_key.get(key)
        expected_top = int((strategy_cfg.get(key) or {}).get("top_n", 10) or 10)
        if not result:
            add("strategy", f"{key} 执行", False, "最新 pipeline 中缺少该策略结果")
            per_strategy[key] = {"present": False, "ok": False, "top_count": 0}
            continue
        top = result.get("top") or []
        for row in top:
            code = norm_code(row.get("code"))
            if code:
                union_codes.add(code)
        ok = bool(result.get("ok")) and len(top) >= min(expected_top, 10)
        add(
            "strategy",
            f"{key} 执行",
            ok,
            f"ok={result.get('ok')} top={len(top)}/{expected_top} elapsed={result.get('elapsed_seconds', 0)}s {result.get('error', '')}",
            warning=(key == "x1beam" and not ok),
            data={"top_count": len(top), "expected_top": expected_top, "error": result.get("error", "")},
        )
        history = _audit_top_history(snapshot_dir, top, trade_date)
        history_ok = history["missing"] == 0 and history["bars_lt_60"] == 0 and history["date_mismatch"] == 0 and history["zero_close"] == 0
        add(
            "strategy_data",
            f"{key} Top历史完整性",
            history_ok,
            (
                f"checked={history['checked']} missing={history['missing']} "
                f"bars<60={history['bars_lt_60']} date_mismatch={history['date_mismatch']} zero={history['zero_close']}"
            ),
            data=history,
        )
        per_strategy[key] = {
            "present": True,
            "ok": bool(result.get("ok")),
            "top_count": len(top),
            "expected_top": expected_top,
            "history": history,
        }

    summary = pipeline.get("summary") or {}
    min_ok = int(((cfg.get("automation") or {}).get("tail") or {}).get("min_strategy_success", 2) or 2)
    add(
        "strategy",
        "策略成功门槛",
        safe_int(summary.get("strategies_ok"), 0) >= min_ok,
        f"{summary.get('strategies_ok', 0)}/{summary.get('strategies_run', 0)} | min={min_ok}",
    )
    return {"per_strategy": per_strategy, "union_count": len(union_codes), "union_codes": sorted(union_codes)}


def _audit_xgb(pipeline: Dict[str, Any], trade_date: str, union_codes: set[str], add) -> Dict[str, Any]:
    diag = pipeline.get("diagnosis") or {}
    results = pipeline.get("diagnosis_results") or []
    diagnosed_codes = {norm_code(row.get("code")) for row in results if norm_code(row.get("code"))}
    candidate_count = safe_int(diag.get("candidate_count"), len(union_codes))
    diagnosed_count = safe_int(diag.get("diagnosed_count"), len(diagnosed_codes))
    skipped_count = safe_int(diag.get("skipped_count"), max(0, candidate_count - diagnosed_count))
    coverage = float(diag.get("coverage_rate") or (diagnosed_count / candidate_count if candidate_count else 0.0))

    source_ok = not union_codes or candidate_count == len(union_codes)
    add(
        "xgb",
        "候选来源一致性",
        source_ok,
        f"四策略并集={len(union_codes)} XGB候选={candidate_count} 来源={diag.get('candidates_source', '')}",
        data={"union_count": len(union_codes), "candidate_count": candidate_count},
    )
    add(
        "xgb",
        "诊断覆盖率",
        diagnosed_count == candidate_count and skipped_count == 0 and coverage >= 0.999,
        f"diagnosed={diagnosed_count}/{candidate_count} skipped={skipped_count} coverage={coverage:.1%}",
        data={"skipped": diag.get("skipped") or []},
    )
    expected_digits = "".join(ch for ch in str(trade_date) if ch.isdigit())[:8]
    date_mismatch = []
    for row in results:
        quality = row.get("diagnosis_quality") or {}
        date_value = str(quality.get("date") or "")
        if expected_digits and date_value and date_value[:8] != expected_digits:
            date_mismatch.append({"code": row.get("code"), "date": date_value})
    add(
        "xgb",
        "诊断时效性",
        not date_mismatch,
        f"trade_date={expected_digits or '-'} mismatch={len(date_mismatch)} engine={diag.get('engine', '')}",
        data={"mismatch": date_mismatch[:20], "signal_distribution": diag.get("signal_distribution") or {}},
    )
    calibration = _latest_xgb_calibration(pipeline)
    calibration_ok = bool(calibration.get("ready_for_diagnosis"))
    hard_signal = bool(calibration.get("ready_for_hard_signal"))
    add(
        "xgb",
        "校准/回测状态",
        calibration_ok,
        (
            f"ready_diag={calibration_ok} hard_signal={hard_signal} "
            f"report={calibration.get('file', '') or '-'} "
            f"backtest={((calibration.get('latest_backtest') or {}).get('path') or '')}"
        ),
        warning=not calibration_ok or not hard_signal,
        data=calibration,
    )
    return {
        "candidate_count": candidate_count,
        "diagnosed_count": diagnosed_count,
        "skipped_count": skipped_count,
        "coverage_rate": coverage,
        "date_mismatch": date_mismatch,
        "signal_distribution": diag.get("signal_distribution") or {},
        "calibration": calibration,
    }


def _latest_xgb_calibration(pipeline: Dict[str, Any]) -> Dict[str, Any]:
    snapshot_dir = Path(str(pipeline.get("snapshot_dir") or ""))
    root = _project_root_from_snapshot(snapshot_dir)
    reports_dir = root / "outputs" / "reports"
    paths = sorted(reports_dir.glob("xgb_calibration_status_*.json"), key=lambda p: p.stat().st_mtime, reverse=True) if reports_dir.exists() else []
    if not paths:
        return {"exists": False, "ready_for_diagnosis": False, "ready_for_hard_signal": False}
    path = paths[0]
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"exists": True, "file": path.name, "path": str(path), "error": str(exc), "ready_for_diagnosis": False}
    payload["exists"] = True
    payload["file"] = path.name
    payload["path"] = str(path)
    return payload


def _project_root_from_snapshot(snapshot_dir: Path) -> Path:
    try:
        resolved = snapshot_dir.resolve()
        parts = resolved.parts
        marker = "分仓之神V2.0"
        if marker in parts:
            return Path(*parts[: parts.index(marker) + 1])
    except Exception:
        pass
    return Path(__file__).resolve().parents[1]


def _audit_x1(cfg: Dict[str, Any], snapshot_dir: Path, pipeline: Dict[str, Any], add) -> Dict[str, Any]:
    status = x1_preheat_status(cfg, snapshot_dir)
    preheat_cfg = ((cfg.get("strategies") or {}).get("x1beam") or {}).get("preheat") or {}
    max_age = float(preheat_cfg.get("max_age_minutes", 60) or 60)
    age = status.get("age_minutes")
    fresh = age is None or float(age) <= max_age
    effective_usable = bool(status.get("usable")) and fresh
    status["max_age_minutes"] = max_age
    status["fresh_for_tail"] = fresh
    status["effective_usable_for_tail"] = effective_usable
    summary_quality = status.get("summary_quality") or {}
    add(
        "x1beam",
        "预热缓存匹配",
        effective_usable,
        (
            f"usable={status.get('usable')} completed={status.get('completed')} "
            f"cache={status.get('cache_exists')} match={status.get('matches_current_snapshot')} "
            f"top={status.get('top_count')} age={status.get('age_minutes')}m/max{max_age:g}m "
            f"fresh={fresh} {status.get('error', '')}"
        ),
        warning=not effective_usable,
        data=status,
    )
    counts = summary_quality.get("counts") or {}
    pools_ok = bool(summary_quality.get("complete")) and all(int(v or 0) > 0 for v in counts.values())
    add(
        "x1beam",
        "规则池完整性",
        pools_ok,
        f"complete={summary_quality.get('complete')} counts={counts}",
        data=summary_quality,
    )
    x1_result = next((item for item in pipeline.get("strategies", []) or [] if _strategy_key(item.get("strategy_name")) == "x1beam"), {})
    add(
        "x1beam",
        "最新X1Beam出票",
        bool(x1_result.get("ok")) and len(x1_result.get("top") or []) > 0,
        f"ok={x1_result.get('ok')} top={len(x1_result.get('top') or [])} {x1_result.get('error', '')}",
        warning=not bool(x1_result.get("ok")),
    )
    return status


def _audit_tracking(cfg: Dict[str, Any], pipeline: Dict[str, Any], pipeline_path: Optional[Path], add) -> Dict[str, Any]:
    records = load_tracking_records(cfg)
    current_records = build_candidate_records(pipeline, pipeline_path) if pipeline_path else []
    existing_ids = {str(row.get("event_id")) for row in records if row.get("event_id")}
    missing = [row.get("event_id") for row in current_records if row.get("event_id") not in existing_ids]
    add(
        "tracking",
        "候选追踪入库",
        not missing,
        f"tracking_records={len(records)} latest_records={len(current_records)} missing_latest={len(missing)}",
        warning=bool(missing),
        data={"missing_event_ids": missing[:20]},
    )
    outcomes = load_current_outcomes(cfg)
    outcome_summary = outcomes.get("summary") or {}
    outcome_count = safe_int(outcome_summary.get("outcome_count"), 0)
    tracked_count = safe_int(outcome_summary.get("tracked_count"), 0)
    pending_count = safe_int(outcome_summary.get("pending_count"), 0)
    outcome_generated = bool(outcomes.get("generated_at"))
    outcome_ok = outcome_generated and (outcome_count == 0 or tracked_count > 0 or pending_count < outcome_count)
    add(
        "tracking",
        "收益回填状态",
        outcome_ok,
        (
            f"generated={outcomes.get('generated_at', '') or '-'} "
            f"outcomes={outcome_count} tracked={tracked_count} pending={pending_count}"
        ),
        warning=True,
        data=outcome_summary,
    )
    factor_dir = output_root(cfg) / "factors"
    factor_files = sorted(factor_dir.glob("candidate_factor_panel_*.json"), key=lambda p: p.stat().st_mtime) if factor_dir.exists() else []
    add(
        "tracking",
        "因子宽表",
        bool(factor_files),
        factor_files[-1].name if factor_files else "尚未生成 candidate_factor_panel",
        warning=not bool(factor_files),
    )
    pattern_dir = output_root(cfg) / "patterns"
    pattern_files = sorted(pattern_dir.glob("historical_pattern_tags*.json"), key=lambda p: p.stat().st_mtime) if pattern_dir.exists() else []
    add(
        "tracking",
        "历史模式标签",
        bool(pattern_files),
        pattern_files[-1].name if pattern_files else "尚未生成 historical_pattern_tags",
        warning=not bool(pattern_files),
    )
    return {
        "tracking_records": len(records),
        "latest_records": len(current_records),
        "missing_latest": len(missing),
        "outcome_summary": outcome_summary,
        "latest_factor_panel": factor_files[-1].name if factor_files else "",
        "latest_pattern_tags": pattern_files[-1].name if pattern_files else "",
    }


def _audit_required_steps(pipeline: Dict[str, Any], add) -> Dict[str, Any]:
    required = {
        "boundary": bool(pipeline.get("boundary")),
        "diagnosis": bool(pipeline.get("diagnosis")),
        "diagnosis_results": bool(pipeline.get("diagnosis_results")),
        "x1_preheat": bool(pipeline.get("x1_preheat")),
        "summary": bool(pipeline.get("summary")),
        "overlap": bool(pipeline.get("overlap")),
    }
    for name, ok in required.items():
        add("steps", f"{name} 步骤产物", ok, "存在" if ok else "缺失", warning=name == "x1_preheat")
    return required


def _audit_top_history(snapshot_dir: Path, rows: Iterable[Dict[str, Any]], trade_date: str) -> Dict[str, Any]:
    expected = "".join(ch for ch in str(trade_date) if ch.isdigit())[:8]
    report = {
        "checked": 0,
        "missing": 0,
        "bars_lt_60": 0,
        "date_mismatch": 0,
        "zero_close": 0,
        "missing_previous": 0,
        "examples": [],
    }
    for row in rows:
        code = norm_code(row.get("code"))
        if not code:
            continue
        path = _resolve_code_file(snapshot_dir, code)
        if not path:
            report["missing"] += 1
            _example(report, code, "missing_file")
            continue
        bars = _read_bars(path)
        report["checked"] += 1
        if len(bars) < 60:
            report["bars_lt_60"] += 1
            _example(report, code, f"bars={len(bars)}")
        if len(bars) < 2:
            report["missing_previous"] += 1
            _example(report, code, "missing_previous")
        if bars:
            last = bars[-1]
            if expected and last.get("date_digits") != expected:
                report["date_mismatch"] += 1
                _example(report, code, f"last={last.get('date_digits')}")
            if float(last.get("close") or 0) <= 0:
                report["zero_close"] += 1
                _example(report, code, "zero_close")
    return report


def _resolve_code_file(snapshot_dir: Path, code: str) -> Optional[Path]:
    code = norm_code(code)
    if len(code) < 8:
        return None
    market, digits = code[:2], code[-6:]
    for path in (
        snapshot_dir / f"{market}#{digits}.txt",
        snapshot_dir / f"{market}{digits}.txt",
        snapshot_dir / f"{digits}.{market}.txt",
        snapshot_dir / f"{market.lower()}#{digits}.txt",
        snapshot_dir / f"{market.lower()}{digits}.txt",
    ):
        if path.exists():
            return path
    return None


def _read_bars(path: Path) -> List[Dict[str, Any]]:
    text = ""
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "gbk"):
        try:
            text = path.read_text(encoding=encoding, errors="strict")
            break
        except Exception:
            continue
    if not text:
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            return []
    bars: List[Dict[str, Any]] = []
    for line in text.splitlines():
        parts = line.replace(",", "\t").split()
        if len(parts) < 7:
            continue
        raw_date = str(parts[0]).replace("/", "-")
        digits = "".join(ch for ch in raw_date if ch.isdigit())[:8]
        if len(digits) != 8:
            continue
        try:
            bars.append({"date_digits": digits, "close": float(parts[4])})
        except Exception:
            continue
    return bars


def _example(report: Dict[str, Any], code: str, reason: str) -> None:
    if len(report["examples"]) < 12:
        report["examples"].append({"code": code, "reason": reason})
