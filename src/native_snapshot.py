"""Native V2.0 multi-source snapshot workflow.

V2.0 keeps the old project as a read-only reference. This module runs the
vendored multi-source builder in an isolated subprocess, writes all caches under
the V2.0 project, audits the result, and promotes it to the live runtime
snapshot only when quality gates pass.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

from src.common import output_root
from src.quality_gate import audit_snapshot, format_quality_summary
from src.snapshot_manager import SNAPSHOT_META, live_current_dir, live_snapshot_root


NATIVE_SOURCE_ORDER = ["tushare_rt_k", "zzshare", "eltdx", "sina_direct", "tencent_direct"]


def native_snapshot_root(cfg: Dict[str, Any]) -> Path:
    configured = ((cfg.get("snapshot") or {}).get("native") or {}).get("snapshot_root")
    return Path(str(configured)) if configured else output_root(cfg) / "cache" / "native_snapshots"


def native_work_dir(cfg: Dict[str, Any]) -> Path:
    configured = ((cfg.get("snapshot") or {}).get("native") or {}).get("work_dir")
    return Path(str(configured)) if configured else output_root(cfg) / "cache" / "native_work_snapshot" / "work"


def build_native_snapshot(
    cfg: Dict[str, Any],
    *,
    promote: bool = False,
    allow_single_source: bool = False,
    official: bool = False,
    timeout: int = 900,
    force_promote: bool = False,
) -> Dict[str, Any]:
    project_root = _project_root(cfg)
    vendor_root = project_root / "vendor" / "fczs_v1_core"
    bridge = project_root / "src" / "native_snapshot_bridge.py"
    reports_dir = output_root(cfg) / "reports"
    runtime_dir = output_root(cfg) / "cache" / "native_runtime"
    reports_dir.mkdir(parents=True, exist_ok=True)
    runtime_dir.mkdir(parents=True, exist_ok=True)
    _ensure_under_project(cfg, runtime_dir)
    _ensure_under_project(cfg, native_snapshot_root(cfg))
    cleanup_report = cleanup_incomplete_native_builds(cfg)

    seed_report = seed_native_history_base(cfg)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    vendor_config_path = runtime_dir / f"native_config_{stamp}.yaml"
    bridge_report_path = reports_dir / f"native_snapshot_bridge_{stamp}.json"
    final_report_path = reports_dir / f"native_snapshot_{stamp}.json"

    vendor_cfg = build_vendor_runtime_config(
        cfg,
        allow_single_source=allow_single_source,
    )
    vendor_config_path.write_text(yaml.safe_dump(vendor_cfg, allow_unicode=True, sort_keys=False), encoding="utf-8")

    env = os.environ.copy()
    env["PYTHONPATH"] = str(vendor_root)
    cmd = [
        sys.executable,
        str(bridge),
        "--config",
        str(vendor_config_path),
        "--report",
        str(bridge_report_path),
        "--v2-root",
        str(project_root),
        "--vendor-root",
        str(vendor_root),
    ]
    started = datetime.now()
    timed_out = False
    timeout_error = ""
    try:
        cp = subprocess.run(
            cmd,
            cwd=str(project_root),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=max(int(timeout), 60),
        )
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        timeout_error = f"native snapshot bridge timeout after {timeout}s"
        cleanup_report = cleanup_incomplete_native_builds(cfg)
        cp = subprocess.CompletedProcess(
            cmd,
            returncode=124,
            stdout=(exc.stdout or "") if isinstance(exc.stdout, str) else "",
            stderr=(exc.stderr or "") if isinstance(exc.stderr, str) else timeout_error,
        )
    bridge_report = _load_json(bridge_report_path)
    snapshot_dir_text = str(bridge_report.get("snapshot_dir") or "").strip()
    snapshot_dir = Path(snapshot_dir_text) if snapshot_dir_text else None
    sanitize_report: Dict[str, Any] = {}
    if snapshot_dir and snapshot_dir.exists():
        sanitize_report = sanitize_native_snapshot(cfg, snapshot_dir)
    quality: Dict[str, Any] = {}
    if snapshot_dir and snapshot_dir.exists():
        quality = audit_snapshot(snapshot_dir, cfg, official=official)

    promoted: Dict[str, Any] = {"ok": False, "skipped": True}
    source_health = evaluate_source_health(cfg, bridge_report)
    snapshot_built = bool(snapshot_dir and snapshot_dir.exists() and (snapshot_dir / SNAPSHOT_META).exists())
    native_ok = snapshot_built and bool(quality.get("ok")) and bool(source_health.get("ok"))
    can_promote = native_ok
    if promote and (can_promote or force_promote) and snapshot_dir and snapshot_dir.exists():
        promoted = promote_native_snapshot(
            cfg,
            snapshot_dir,
            bridge_report=bridge_report,
            quality=quality,
            force=force_promote,
        )

    report = {
        "ok": native_ok,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "started_at": started.isoformat(timespec="seconds"),
        "mode": "v2_native_snapshot",
        "command": cmd,
        "returncode": cp.returncode,
        "timed_out": timed_out,
        "timeout_error": timeout_error,
        "stdout_tail": (cp.stdout or "")[-3000:],
        "stderr_tail": (cp.stderr or "")[-3000:],
        "vendor_config_path": str(vendor_config_path),
        "bridge_report_path": str(bridge_report_path),
        "snapshot_dir": str(snapshot_dir) if snapshot_dir else "",
        "seed_report": seed_report,
        "cleanup_report": cleanup_report,
        "sanitize_report": sanitize_report,
        "bridge_report": bridge_report,
        "source_health": source_health,
        "quality": quality,
        "quality_summary": format_quality_summary(quality) if quality else "",
        "promote_requested": promote,
        "promoted": promoted,
        "sources": bridge_report.get("sources", []),
    }
    final_report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    report["report_path"] = str(final_report_path)
    return report


def promote_latest_native_snapshot(cfg: Dict[str, Any], *, force: bool = False) -> Dict[str, Any]:
    report_dir = output_root(cfg) / "reports"
    reports = sorted(report_dir.glob("native_snapshot_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    selected: Optional[Dict[str, Any]] = None
    selected_path: Optional[Path] = None
    for path in reports:
        data = _load_json(path)
        if not data.get("ok") and not force:
            continue
        snapshot_dir = Path(str(data.get("snapshot_dir") or ""))
        if snapshot_dir.exists() and (snapshot_dir / SNAPSHOT_META).exists():
            selected = data
            selected_path = path
            break
    if not selected or not selected_path:
        return {"ok": False, "reason": "未找到可提升的原生快照报告"}
    snapshot_dir = Path(str(selected.get("snapshot_dir") or ""))
    quality = audit_snapshot(snapshot_dir, cfg, official=False)
    if not quality.get("ok") and not force:
        return {
            "ok": False,
            "reason": "最近原生快照质检未通过",
            "report_path": str(selected_path),
            "snapshot_dir": str(snapshot_dir),
            "quality": quality,
        }
    promoted = promote_native_snapshot(
        cfg,
        snapshot_dir,
        bridge_report=selected.get("bridge_report") or {},
        quality=quality,
        force=force,
    )
    return {
        "ok": bool(promoted.get("ok")),
        "report_path": str(selected_path),
        "snapshot_dir": str(snapshot_dir),
        "quality": quality,
        "promoted": promoted,
    }


def build_vendor_runtime_config(cfg: Dict[str, Any], *, allow_single_source: bool = False) -> Dict[str, Any]:
    project_root = _project_root(cfg)
    vendor_root = project_root / "vendor" / "fczs_v1_core"
    base_path = vendor_root / "config" / "config.yaml"
    vendor_cfg = yaml.safe_load(base_path.read_text(encoding="utf-8")) or {}
    vendor_cfg = deepcopy(vendor_cfg)

    disabled_manual_dir = output_root(cfg) / "cache" / "manual_data_disabled"
    disabled_manual_dir.mkdir(parents=True, exist_ok=True)
    root = native_snapshot_root(cfg)
    root.mkdir(parents=True, exist_ok=True)

    paths = vendor_cfg.setdefault("paths", {})
    paths.update(
        {
            "legacy_data_dir": str(disabled_manual_dir),
            "legacy_screener_dir": str(project_root / "vendor" / "legacy_screeners"),
            "legacy_screener_source_dir": str(project_root / "vendor" / "legacy_screeners"),
            "legacy_screener_work_dir": str(project_root / "vendor" / "legacy_screeners"),
            "vip_screener_source_dir": str(project_root / "vendor" / "VIP"),
            "vip_screener_dir": str(project_root / "vendor" / "VIP"),
            "snapshot_root": str(root),
            "output_root": str(output_root(cfg)),
            "dzh_root": str(paths.get("dzh_root") or r"D:\dzh2\dzh2"),
        }
    )

    market = vendor_cfg.setdefault("market", {})
    market.update(
        {
            "include_bj": False,
            "snapshot_history_merge_recent_dirs": 8,
            "snapshot_reuse_work_dir": str(native_work_dir(cfg)),
            "snapshot_prefer_previous_over_legacy": True,
            "snapshot_allow_local_history_anchor": False,
            "snapshot_allow_stale_previous_for_speed": False,
            "snapshot_trust_previous_history_length": False,
            "snapshot_trust_reuse_work_quality": True,
            "snapshot_require_history_continuity": False,
            "snapshot_repair_zero_halt_quote": True,
            "snapshot_force_refresh_seed_on_continuity_gap": True,
            "snapshot_online_history_seed_refresh_limit": int(
                (((cfg.get("snapshot") or {}).get("native") or {}).get("online_history_seed_refresh_limit") or 0)
            ),
            "snapshot_fast_previous_update": True,
            "snapshot_fast_skip_full_clean": True,
            "snapshot_build_deadline_seconds": int(
                (((cfg.get("snapshot") or {}).get("native") or {}).get("build_deadline_seconds") or 360)
            ),
        }
    )

    sources = vendor_cfg.setdefault("sources", {})
    enabled = {str(name): False for name in (sources.get("enabled") or {})}
    for name in NATIVE_SOURCE_ORDER:
        enabled[name] = True
    sources.update(
        {
            "priority": list(NATIVE_SOURCE_ORDER),
            "enabled": enabled,
            "fetch_parallel": True,
            "fetch_max_workers": 5,
            "fetch_tiered": False,
            "fetch_soft_deadline_seconds": 0,
            "fetch_hard_deadline_seconds": int(
                (((cfg.get("snapshot") or {}).get("native") or {}).get("fetch_hard_deadline_seconds") or 180)
            ),
            "fetch_min_completed_sources": 2,
            "fetch_soft_required_sources": ["tushare_rt_k"],
            "cache_ttl_seconds": 20,
            "fallback_cache_seconds": 900,
        }
    )
    sources.setdefault("zzshare", {})["timeout_seconds"] = int((sources.get("zzshare") or {}).get("timeout_seconds") or 10)
    sources.setdefault("eltdx", {})["timeout_seconds"] = int((sources.get("eltdx") or {}).get("timeout_seconds") or 3)
    sources.setdefault("tushare_rt_k", {})["request_timeout_seconds"] = int(
        (sources.get("tushare_rt_k") or {}).get("request_timeout_seconds") or 20
    )

    source_roles = vendor_cfg.setdefault("source_roles", {})
    source_roles["dayline_primary_order"] = list(NATIVE_SOURCE_ORDER)
    source_roles["realtime_snapshot_primary_order"] = list(NATIVE_SOURCE_ORDER)
    source_roles["enhancement_cross_check"] = ["zzshare", "eltdx", "sina_direct", "tencent_direct"]
    source_roles["verification_only"] = []

    native_cfg = ((cfg.get("snapshot") or {}).get("native") or {})
    require_tushare_primary = bool(native_cfg.get("require_tushare_primary", False))

    validation = vendor_cfg.setdefault("validation", {})
    validation.update(
        {
            "allow_single_source_push": bool(allow_single_source),
            "require_current_trade_date": True,
            "min_primary_rows": 4500,
            "min_coverage_ratio": 0.92,
            "reject_cache_fallback_primary": require_tushare_primary,
        }
    )
    vendor_cfg.setdefault("runtime", {})
    vendor_cfg["runtime"]["project_root"] = str(project_root)
    vendor_cfg["runtime"]["tushare_token"] = ""
    vendor_cfg["runtime"]["zzshare_token"] = ""
    vendor_cfg["project"] = {"name": "fenCangZhiShenV2-native", "timezone": "Asia/Shanghai"}
    return vendor_cfg


def evaluate_source_health(cfg: Dict[str, Any], bridge_report: Dict[str, Any]) -> Dict[str, Any]:
    native_cfg = ((cfg.get("snapshot") or {}).get("native") or {})
    min_rows = int(native_cfg.get("min_source_rows") or 4500)
    min_healthy = int(native_cfg.get("min_healthy_sources") or 2)
    max_price_mismatch = int(native_cfg.get("max_price_mismatch") or 20)
    require_tushare = bool(native_cfg.get("require_tushare_primary", False))
    sources = bridge_report.get("sources") or []
    healthy = [
        item for item in sources
        if item.get("ok") and int(item.get("rows") or 0) >= min_rows
    ]
    by_name = {str(item.get("source")): item for item in sources}
    validation = bridge_report.get("validation") or {}
    summary = validation.get("summary") or {}
    price_mismatch = int(summary.get("price_mismatch") or 0)
    primary_source = str(validation.get("primary_source") or "")
    blockers = []
    warnings = []
    if len(healthy) < min_healthy:
        blockers.append(f"健康数据源不足: {len(healthy)} < {min_healthy}")
    if price_mismatch > max_price_mismatch:
        blockers.append(f"跨源价格差异过多: {price_mismatch} > {max_price_mismatch}")
    tushare = by_name.get("tushare_rt_k") or {}
    if require_tushare and not tushare.get("ok"):
        blockers.append("tushare_rt_k 不可用，但配置要求必须可用")
    if require_tushare and tushare.get("ok") and primary_source != "tushare_rt_k":
        blockers.append(f"正式主源异常: 期望 tushare_rt_k，实际 {primary_source or '空'}")
    elif not tushare.get("ok"):
        warnings.append("tushare_rt_k 当前不可用，已按健康源自动降级")
    return {
        "ok": not blockers,
        "blockers": blockers,
        "warnings": warnings,
        "healthy_source_count": len(healthy),
        "healthy_sources": [item.get("source") for item in healthy],
        "primary_source": primary_source,
        "tushare_ok": bool(tushare.get("ok")),
        "price_mismatch": price_mismatch,
        "min_source_rows": min_rows,
        "min_healthy_sources": min_healthy,
        "max_price_mismatch": max_price_mismatch,
        "vendor_validation_passed": bool(validation.get("passed")),
        "vendor_grade": validation.get("grade", ""),
    }


def seed_native_history_base(cfg: Dict[str, Any]) -> Dict[str, Any]:
    source = live_current_dir(cfg)
    root = native_snapshot_root(cfg)
    seed_target = root / "000_seed" / "current"
    work_target = native_work_dir(cfg)
    report = {
        "ok": False,
        "source": str(source),
        "seed_target": str(seed_target),
        "work_target": str(work_target),
        "seed_copied_files": 0,
        "work_copied_files": 0,
        "reason": "",
    }
    if not source.exists() or not (source / SNAPSHOT_META).exists():
        report["reason"] = "live current snapshot missing"
        return report
    for target, key in ((seed_target, "seed_copied_files"), (work_target, "work_copied_files")):
        _ensure_under_project(cfg, target)
        if target.exists():
            shutil.rmtree(target)
        target.mkdir(parents=True, exist_ok=True)
        report[key] = _copy_snapshot_files(source, target)
    report["ok"] = int(report["seed_copied_files"]) > 0 and int(report["work_copied_files"]) > 0
    return report


def sanitize_native_snapshot(cfg: Dict[str, Any], snapshot_dir: Path) -> Dict[str, Any]:
    native_cfg = ((cfg.get("snapshot") or {}).get("native") or {})
    max_gap_days = int(native_cfg.get("max_halt_gap_days") or 4)
    quarantine = output_root(cfg) / "cache" / "native_excluded" / datetime.now().strftime("%Y%m%d_%H%M%S")
    report = {
        "snapshot_dir": str(snapshot_dir),
        "quarantine_dir": str(quarantine),
        "removed": 0,
        "examples": [],
    }
    _ensure_under_project(cfg, snapshot_dir)
    _ensure_under_project(cfg, quarantine)
    for path in sorted(snapshot_dir.glob("*.txt")):
        rows = _last_two_rows(path)
        if not rows:
            continue
        prev = rows[-2] if len(rows) >= 2 else None
        last = rows[-1]
        reason = ""
        if float(last.get("close") or 0) <= 0:
            reason = "zero_close"
        elif prev:
            gap = (last["date"] - prev["date"]).days
            if gap > max_gap_days and float(last.get("volume") or 0) <= 0 and float(last.get("amount") or 0) <= 0:
                reason = f"halt_gap_zero_volume:{gap}d"
        if not reason:
            continue
        quarantine.mkdir(parents=True, exist_ok=True)
        target = quarantine / path.name
        shutil.move(str(path), str(target))
        report["removed"] += 1
        if len(report["examples"]) < 20:
            report["examples"].append({"file": path.name, "reason": reason, "to": str(target)})
    return report


def cleanup_incomplete_native_builds(cfg: Dict[str, Any]) -> Dict[str, Any]:
    root = native_snapshot_root(cfg)
    report = {"root": str(root), "removed": 0, "examples": []}
    if not root.exists():
        return report
    _ensure_under_project(cfg, root)
    for parent in root.iterdir():
        if not parent.is_dir() or parent.name.startswith("000_"):
            continue
        for child in parent.iterdir():
            if not child.is_dir():
                continue
            if (child / SNAPSHOT_META).exists():
                continue
            _ensure_under_project(cfg, child)
            shutil.rmtree(child, ignore_errors=True)
            report["removed"] += 1
            if len(report["examples"]) < 10:
                report["examples"].append(str(child))
        try:
            if not any(parent.iterdir()):
                parent.rmdir()
        except Exception:
            pass
    return report


def promote_native_snapshot(
    cfg: Dict[str, Any],
    source_dir: Path,
    *,
    bridge_report: Dict[str, Any],
    quality: Dict[str, Any],
    force: bool = False,
) -> Dict[str, Any]:
    current = live_current_dir(cfg)
    root = live_snapshot_root(cfg)
    staging = root / f"native_staging_{datetime.now():%Y%m%d_%H%M%S}"
    previous = root / "previous"
    for path in (root, current, staging, previous):
        _ensure_under_project(cfg, path)
    if not quality.get("ok") and not force:
        return {"ok": False, "skipped": False, "reason": "quality failed"}
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True, exist_ok=True)
    copied = _copy_snapshot_files(source_dir, staging)
    _rewrite_native_live_meta(staging, source_dir, bridge_report, quality)
    final_quality = audit_snapshot(staging, cfg, official=False)
    if not final_quality.get("ok") and not force:
        shutil.rmtree(staging, ignore_errors=True)
        return {"ok": False, "skipped": False, "reason": "promoted staging quality failed", "quality": final_quality}
    if previous.exists():
        shutil.rmtree(previous)
    if current.exists():
        current.rename(previous)
    staging.rename(current)
    live_quality = audit_snapshot(current, cfg, official=False)
    return {
        "ok": bool(live_quality.get("ok")),
        "skipped": False,
        "source_dir": str(source_dir),
        "target_dir": str(current),
        "previous_dir": str(previous),
        "copied_files": copied,
        "quality": live_quality,
    }


def _rewrite_native_live_meta(
    staging: Path,
    source_dir: Path,
    bridge_report: Dict[str, Any],
    quality: Dict[str, Any],
) -> None:
    meta_path = staging / SNAPSHOT_META
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
    except Exception:
        meta = {}
    original = dict(meta)
    metrics = quality.get("metrics") or {}
    meta.update(
        {
            "project": "分仓之神V2.0",
            "snapshot_dir": str(staging),
            "prepared_at": datetime.now().isoformat(timespec="seconds"),
            "prepared_by": "v2_native_multi_source_snapshot",
            "imported_from_label": "v2_native_multi_source",
            "imported_from_dir": str(source_dir),
            "trade_date": meta.get("trade_date") or metrics.get("expected_trade_date") or bridge_report.get("trade_date"),
            "v2_runtime_snapshot": True,
            "v2_source_role": "native_tushare_rt_k_primary_with_zzshare_eltdx_sina_tencent_validation",
            "native_bridge_summary": {
                "sources": bridge_report.get("sources", []),
                "validation": bridge_report.get("validation", {}),
                "created_files": bridge_report.get("created_files", 0),
                "reused_files": bridge_report.get("reused_files", 0),
                "skipped_files": bridge_report.get("skipped_files", 0),
            },
            "upstream_meta": original,
        }
    )
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


def _copy_snapshot_files(source: Path, target: Path) -> int:
    count = 0
    for path in source.iterdir():
        if not path.is_file():
            continue
        if path.suffix.lower() == ".txt" or path.name in {SNAPSHOT_META, ".tdx_latest_bar_index.json"}:
            shutil.copy2(path, target / path.name)
            count += 1
    return count


def _last_two_rows(path: Path) -> list[Dict[str, Any]]:
    rows: list[Dict[str, Any]] = []
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return rows
    for line in text.splitlines():
        parts = line.strip().split()
        if len(parts) < 7 or "/" not in parts[0]:
            continue
        try:
            rows.append(
                {
                    "date": datetime.strptime(parts[0].replace("-", "/"), "%Y/%m/%d"),
                    "open": float(parts[1]),
                    "high": float(parts[2]),
                    "low": float(parts[3]),
                    "close": float(parts[4]),
                    "volume": float(parts[5]),
                    "amount": float(parts[6]),
                }
            )
        except Exception:
            continue
    return rows[-2:]


def _load_json(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _project_root(cfg: Dict[str, Any]) -> Path:
    return Path(str((cfg.get("paths") or {}).get("project_root") or Path(__file__).resolve().parents[1])).resolve()


def _ensure_under_project(cfg: Dict[str, Any], path: Path) -> None:
    project_root = _project_root(cfg)
    resolved = Path(path).resolve()
    if project_root not in [resolved, *resolved.parents]:
        raise ValueError(f"拒绝写入 V2.0 项目外路径: {resolved}")
