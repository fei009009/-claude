"""V2.0 snapshot preparation.

The first production step is deliberately conservative: V2.0 imports a
verified upstream multi-source snapshot into its own cache directory, audits it,
and makes that local copy the official runtime snapshot. The upstream project
remains read-only.
"""
from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from src.common import output_root
from src.quality_gate import audit_snapshot, format_quality_summary


SNAPSHOT_META = "snapshot_meta.json"


def live_snapshot_root(cfg: Dict[str, Any]) -> Path:
    paths = cfg.get("paths") or {}
    configured = paths.get("v2_live_snapshot_root") or (output_root(cfg) / "cache" / "live_snapshots")
    return Path(str(configured))


def live_current_dir(cfg: Dict[str, Any]) -> Path:
    paths = cfg.get("paths") or {}
    configured = paths.get("snapshot_root") or paths.get("v2_live_snapshot_current")
    if configured:
        return Path(str(configured))
    return live_snapshot_root(cfg) / "current"


def upstream_candidates(cfg: Dict[str, Any]) -> List[Tuple[str, Path]]:
    paths = cfg.get("paths") or {}
    raw = [
        ("v2_import_source", paths.get("v2_import_source")),
        ("reference_tail_work_snapshot", paths.get("reference_tail_work_snapshot")),
        ("v1_work", paths.get("v1_work")),
        ("reference_fczs_v1_work", Path(str(paths.get("reference_fczs_v1_dir") or "")) / "outputs" / "cache" / "tail_work_snapshots" / "work"),
        ("v1_snapshot", paths.get("v1_snapshot")),
    ]
    seen: set[str] = set()
    out: List[Tuple[str, Path]] = []
    for label, value in raw:
        if not value:
            continue
        path = Path(str(value))
        key = str(path).lower()
        if key in seen:
            continue
        seen.add(key)
        out.append((label, path))
    return out


def choose_upstream_snapshot(cfg: Dict[str, Any], explicit: Optional[Path] = None) -> Tuple[str, Path, Dict[str, Any]]:
    candidates = [("arg", explicit)] if explicit else upstream_candidates(cfg)
    best: Tuple[str, Path, Dict[str, Any]] | None = None
    for label, path in candidates:
        if path is None or not path.exists():
            continue
        if path.is_dir() and (path / SNAPSHOT_META).exists():
            audit = audit_snapshot(path, cfg, official=False)
            if audit.get("ok"):
                return label, path, audit
            if best is None:
                best = (label, path, audit)
        elif path.is_dir():
            nested = sorted(path.glob("**/snapshot_meta.json"), key=lambda p: p.stat().st_mtime, reverse=True)
            for meta in nested[:6]:
                snap = meta.parent
                audit = audit_snapshot(snap, cfg, official=False)
                if audit.get("ok"):
                    return f"{label}:latest_nested", snap, audit
                if best is None:
                    best = (f"{label}:latest_nested", snap, audit)
    if best:
        return best
    raise FileNotFoundError("未找到可导入的上游快照")


def prepare_live_snapshot(
    cfg: Dict[str, Any],
    *,
    source: Optional[Path] = None,
    force: bool = False,
) -> Dict[str, Any]:
    source_label, source_dir, source_audit = choose_upstream_snapshot(cfg, source)
    current = live_current_dir(cfg)
    root = live_snapshot_root(cfg)
    staging = root / f"staging_{datetime.now():%Y%m%d_%H%M%S}"
    previous = root / "previous"
    root.mkdir(parents=True, exist_ok=True)
    _ensure_under_project(cfg, root)
    _ensure_under_project(cfg, current)
    _ensure_under_project(cfg, staging)
    _ensure_under_project(cfg, previous)

    _copy_snapshot(source_dir, staging)
    _rewrite_meta(cfg, staging, source_label, source_dir, source_audit)
    audit = audit_snapshot(staging, cfg, official=False)
    if not audit.get("ok") and not force:
        shutil.rmtree(staging, ignore_errors=True)
        return {
            "ok": False,
            "error": "; ".join(audit.get("blockers", [])) or "导入后快照质检失败",
            "source_label": source_label,
            "source_dir": str(source_dir),
            "target_dir": str(current),
            "audit": audit,
            "source_audit": source_audit,
        }

    if previous.exists():
        shutil.rmtree(previous)
    if current.exists():
        current.rename(previous)
    staging.rename(current)
    final_audit = audit_snapshot(current, cfg, official=False)
    report = {
        "ok": bool(final_audit.get("ok")),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "source_label": source_label,
        "source_dir": str(source_dir),
        "target_dir": str(current),
        "previous_dir": str(previous) if previous.exists() else "",
        "file_count": (final_audit.get("metrics") or {}).get("file_count", 0),
        "trade_date": (final_audit.get("metrics") or {}).get("expected_trade_date", ""),
        "quality_summary": format_quality_summary(final_audit),
        "audit": final_audit,
        "source_audit": source_audit,
    }
    report_path = output_root(cfg) / "reports" / f"snapshot_prepare_{datetime.now():%Y%m%d_%H%M%S}.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    report["report_path"] = str(report_path)
    return report


def _copy_snapshot(source_dir: Path, target_dir: Path) -> None:
    target_dir.mkdir(parents=True, exist_ok=False)
    for path in source_dir.iterdir():
        if not path.is_file():
            continue
        if path.suffix.lower() == ".txt" or path.name in {SNAPSHOT_META, ".tdx_latest_bar_index.json"}:
            shutil.copy2(path, target_dir / path.name)


def _rewrite_meta(
    cfg: Dict[str, Any],
    snapshot_dir: Path,
    source_label: str,
    source_dir: Path,
    source_audit: Dict[str, Any],
) -> None:
    meta_path = snapshot_dir / SNAPSHOT_META
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
    except Exception:
        meta = {}
    original = dict(meta)
    metrics = source_audit.get("metrics") or {}
    meta.update(
        {
            "project": "分仓之神V2.0",
            "snapshot_dir": str(snapshot_dir),
            "prepared_at": datetime.now().isoformat(timespec="seconds"),
            "prepared_by": "v2_snapshot_prepare",
            "imported_from_label": source_label,
            "imported_from_dir": str(source_dir),
            "trade_date": meta.get("trade_date") or metrics.get("expected_trade_date") or metrics.get("observed_trade_date"),
            "v2_runtime_snapshot": True,
            "v2_source_role": "read_only_upstream_verified_multi_source_snapshot",
            "v2_next_step": "replace importer with native tushare_rt_k/zzshare/eltdx realtime fetch once parity is stable",
            "upstream_meta": original,
        }
    )
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


def _ensure_under_project(cfg: Dict[str, Any], path: Path) -> None:
    project_root = Path(str((cfg.get("paths") or {}).get("project_root") or Path(__file__).resolve().parents[1])).resolve()
    resolved = path.resolve()
    if project_root not in [resolved, *resolved.parents]:
        raise ValueError(f"拒绝写入 V2.0 项目外路径: {resolved}")
