"""X1Beam preheat manifest management for V2.0.

The tail window must not spend minutes doing a full Forest Beam Search. This
module records completed X1Beam caches with a snapshot signature so the adapter,
dashboard and tail runner all make the same decision about whether X1Beam can be
used as the fourth peer strategy.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from src.common import output_root
from src.quality_gate import audit_snapshot, format_quality_summary, resolve_snapshot
from src.strategies.x1beam_adapter import X1BeamAdapter


MANIFEST_NAME = "x1beam_preheat_manifest.json"


def cache_dir(cfg: Dict[str, Any]) -> Path:
    path = output_root(cfg) / "cache" / "x1beam_fast"
    path.mkdir(parents=True, exist_ok=True)
    return path


def manifest_path(cfg: Dict[str, Any]) -> Path:
    return cache_dir(cfg) / MANIFEST_NAME


def snapshot_signature(snapshot_dir: Path) -> Dict[str, Any]:
    snapshot_dir = Path(snapshot_dir).resolve()
    files = sorted(path for path in snapshot_dir.glob("*.txt") if path.is_file())
    max_mtime = max((path.stat().st_mtime for path in files), default=0.0)
    total_size = sum((path.stat().st_size for path in files), 0)
    trade_date = ""
    meta_path = snapshot_dir / "snapshot_meta.json"
    meta: Dict[str, Any] = {}
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            trade_date = str(meta.get("trade_date") or "")
        except Exception:
            meta = {}
    if not trade_date and max_mtime:
        trade_date = datetime.fromtimestamp(max_mtime).strftime("%Y-%m-%d")
    raw = "|".join([
        str(snapshot_dir),
        trade_date,
        str(len(files)),
        f"{max_mtime:.6f}",
        str(total_size),
    ])
    return {
        "snapshot_dir": str(snapshot_dir),
        "trade_date": trade_date,
        "file_count": len(files),
        "max_mtime": max_mtime,
        "total_size": total_size,
        "signature": hashlib.sha1(raw.encode("utf-8", errors="ignore")).hexdigest(),
        "meta": meta,
    }


def signatures_match(left: Dict[str, Any], right: Dict[str, Any]) -> bool:
    return bool(left and right and left.get("signature") == right.get("signature"))


def load_manifest(cfg: Dict[str, Any]) -> Dict[str, Any]:
    path = manifest_path(cfg)
    if not path.exists():
        return {"exists": False, "path": str(path)}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["exists"] = True
        payload["path"] = str(path)
        return payload
    except Exception as exc:
        return {"exists": True, "path": str(path), "error": str(exc)}


def write_manifest(cfg: Dict[str, Any], payload: Dict[str, Any]) -> Path:
    path = manifest_path(cfg)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def latest_status(cfg: Dict[str, Any], snapshot_dir: Optional[Path] = None) -> Dict[str, Any]:
    current_sig = snapshot_signature(snapshot_dir) if snapshot_dir else None
    manifest = load_manifest(cfg)
    completed = bool(manifest.get("completed"))
    cache_text = str(manifest.get("cache_path") or "").strip()
    cache_path = Path(cache_text) if cache_text else None
    cache_exists = bool(cache_path and cache_path.exists())
    snapshot_sig = manifest.get("snapshot_signature") or {}
    source_sig = manifest.get("source_signature") or {}
    matches_current = bool(
        current_sig
        and (
            signatures_match(snapshot_sig, current_sig)
            or signatures_match(source_sig, current_sig)
        )
    )
    usable = completed and cache_exists and (matches_current if current_sig else True)

    age_minutes = None
    generated_at = str(manifest.get("generated_at") or "")
    if generated_at:
        try:
            age_minutes = round((datetime.now() - datetime.fromisoformat(generated_at)).total_seconds() / 60, 1)
        except Exception:
            age_minutes = None
    summary_quality: Dict[str, Any] = {}
    try:
        x1_dir = Path(str((cfg.get("paths") or {}).get("x1_xin_dir", "")))
        summary_quality = X1BeamAdapter(x1_dir)._summary_quality(x1_dir / "cache" / "_summary")
    except Exception:
        summary_quality = {}
    reason = str(manifest.get("error") or "")
    if not reason:
        if not completed:
            reason = "尚未完成 X1Beam 预热"
        elif not cache_exists:
            reason = "X1Beam 预热缓存文件不存在"
        elif current_sig and not matches_current:
            reason = "X1Beam 预热缓存与当前活动快照不匹配"

    return {
        "exists": bool(manifest.get("exists")),
        "usable": usable,
        "completed": completed,
        "cache_exists": cache_exists,
        "matches_current_snapshot": matches_current,
        "snapshot_match": matches_current,
        "manifest_path": manifest.get("path", str(manifest_path(cfg))),
        "cache_path": str(cache_path) if cache_path else "",
        "snapshot_dir": (manifest.get("snapshot_signature") or {}).get("snapshot_dir", ""),
        "trade_date": (manifest.get("snapshot_signature") or {}).get("trade_date", ""),
        "file_count": (manifest.get("snapshot_signature") or {}).get("file_count", 0),
        "scanned_files": manifest.get("scanned_files", 0),
        "top_count": manifest.get("top_count", 0),
        "elapsed_seconds": manifest.get("elapsed_seconds", 0),
        "generated_at": generated_at,
        "age_minutes": age_minutes,
        "mode": manifest.get("mode", ""),
        "error": str(manifest.get("error") or ""),
        "reason": reason,
        "summary_quality": summary_quality,
        "source_signature": manifest.get("source_signature") or {},
        "current_snapshot_signature": current_sig,
    }


def select_tail_snapshot(cfg: Dict[str, Any], default_snapshot: Path) -> Tuple[Path, str, Dict[str, Any]]:
    preheat_cfg = (cfg.get("strategies") or {}).get("x1beam", {}).get("preheat", {})
    if not bool(preheat_cfg.get("use_frozen_snapshot_for_tail", True)):
        return default_snapshot, "active_snapshot", latest_status(cfg, default_snapshot)

    status = latest_status(cfg)
    if not status.get("usable"):
        return default_snapshot, "active_snapshot", latest_status(cfg, default_snapshot)

    current_sig = snapshot_signature(default_snapshot)
    source_sig = status.get("source_signature") or {}
    if source_sig and not signatures_match(source_sig, current_sig):
        return default_snapshot, "active_snapshot", latest_status(cfg, default_snapshot)
    if not source_sig and status.get("trade_date") and status.get("trade_date") != current_sig.get("trade_date"):
        return default_snapshot, "active_snapshot", latest_status(cfg, default_snapshot)

    frozen_dir = Path(str(status.get("snapshot_dir") or ""))
    if not frozen_dir.exists():
        return default_snapshot, "active_snapshot", latest_status(cfg, default_snapshot)

    max_age = float(preheat_cfg.get("max_age_minutes", 60))
    age = status.get("age_minutes")
    if age is not None and age > max_age:
        return default_snapshot, "active_snapshot", latest_status(cfg, default_snapshot)

    return frozen_dir, "x1_preheated_frozen_snapshot", latest_status(cfg, frozen_dir)


def run_preheat(
    cfg: Dict[str, Any],
    *,
    snapshot: Optional[Path] = None,
    workers: int = 1,
    top_n: int = 10,
    keep_per_tier: int = 50,
    timeout: int = 7200,
    time_budget: float = 0,
    force: bool = False,
    freeze: Optional[bool] = None,
) -> Dict[str, Any]:
    started = time.perf_counter()
    source_snapshot, source_label = (Path(snapshot), "arg") if snapshot else resolve_snapshot(cfg)
    source_sig = snapshot_signature(source_snapshot)
    quality = audit_snapshot(source_snapshot, cfg, official=not force)
    preheat_cfg = (cfg.get("strategies") or {}).get("x1beam", {}).get("preheat", {})
    freeze_snapshot = bool(preheat_cfg.get("freeze_snapshot", True) if freeze is None else freeze)

    base_manifest: Dict[str, Any] = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "mode": "x1beam_preheat",
        "source_snapshot_dir": str(source_snapshot),
        "source_snapshot_label": source_label,
        "source_signature": source_sig,
        "quality": quality,
        "quality_summary": format_quality_summary(quality),
        "completed": False,
        "cache_path": "",
        "error": "",
    }

    if not quality.get("ok") and not force:
        base_manifest["error"] = "; ".join(quality.get("blockers", [])) or "snapshot quality failed"
        existing = load_manifest(cfg)
        existing_cache = Path(str(existing.get("cache_path") or ""))
        if existing.get("completed") is True and existing_cache.exists():
            existing["last_failed_at"] = base_manifest["generated_at"]
            existing["last_failure_error"] = base_manifest["error"]
            existing["last_failure_quality_summary"] = base_manifest["quality_summary"]
            write_manifest(cfg, existing)
            return existing
        write_manifest(cfg, base_manifest)
        return base_manifest

    work_snapshot = _freeze_snapshot(cfg, source_snapshot) if freeze_snapshot else source_snapshot
    sig = snapshot_signature(work_snapshot)
    base_manifest["snapshot_signature"] = sig
    base_manifest["frozen_snapshot"] = bool(freeze_snapshot)

    x1_dir = Path(str((cfg.get("paths") or {}).get("x1_xin_dir", "")))
    adapter = X1BeamAdapter(x1_dir, top_n=top_n)
    if not adapter.validate_environment():
        base_manifest["error"] = f"X1Beam environment missing: {x1_dir}"
        write_manifest(cfg, base_manifest)
        return base_manifest
    adapter._ensure_summary_files(
        reference_dir=Path(str((cfg.get("paths") or {}).get("reference_x1_dir") or "")),
        overwrite=False,
    )

    trade_date = str(sig.get("trade_date") or datetime.now().strftime("%Y-%m-%d")).replace("-", "")
    out_path = cache_dir(cfg) / f"x1beam_fast_{trade_date}_{datetime.now():%Y%m%d_%H%M%S}_preheat.json"
    runner = Path(__file__).resolve().parent / "strategies" / "x1beam_fast_runner.py"
    cmd = [
        sys.executable,
        str(runner),
        "--x1-dir",
        str(x1_dir),
        "--snapshot",
        str(work_snapshot),
        "--output",
        str(out_path),
        "--workers",
        str(max(int(workers), 1)),
        "--top-n",
        str(max(int(top_n), 1)),
        "--keep-per-tier",
        str(max(int(keep_per_tier), int(top_n), 1)),
    ]
    if time_budget and time_budget > 0:
        cmd.extend(["--time-budget", str(time_budget)])

    try:
        cp = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(Path(__file__).resolve().parents[1]),
        )
    except subprocess.TimeoutExpired:
        base_manifest["error"] = f"X1Beam preheat timeout after {timeout}s"
        write_manifest(cfg, base_manifest)
        return base_manifest

    if not out_path.exists():
        base_manifest.update({
            "error": "X1Beam runner produced no cache",
            "runner_returncode": cp.returncode,
            "runner_stdout_tail": (cp.stdout or "")[-1200:],
            "runner_stderr_tail": (cp.stderr or "")[-1200:],
        })
        write_manifest(cfg, base_manifest)
        return base_manifest

    payload = json.loads(out_path.read_text(encoding="utf-8"))
    payload["adapter_snapshot_meta"] = {
        "snapshot_dir": sig["snapshot_dir"],
        "trade_date": sig["trade_date"],
        "file_count": sig["file_count"],
        "max_mtime": sig["max_mtime"],
    }
    payload["snapshot_signature"] = sig
    payload["runner_returncode"] = cp.returncode
    payload["runner_stdout_tail"] = (cp.stdout or "")[-1200:]
    payload["runner_stderr_tail"] = (cp.stderr or "")[-1200:]
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    completed = payload.get("completed") is True and bool(payload.get("top"))
    base_manifest.update({
        "completed": completed,
        "cache_path": str(out_path),
        "snapshot_signature": sig,
        "runner_returncode": cp.returncode,
        "scanned_files": payload.get("scanned_files", 0),
        "file_count": payload.get("file_count", 0),
        "top_count": len(payload.get("top") or []),
        "elapsed_seconds": payload.get("elapsed_seconds", round(time.perf_counter() - started, 3)),
        "runner_stdout_tail": (cp.stdout or "")[-1200:],
        "runner_stderr_tail": (cp.stderr or "")[-1200:],
    })
    if not completed:
        base_manifest["error"] = (
            f"incomplete cache: scanned {payload.get('scanned_files', 0)}/"
            f"{payload.get('file_count', 0)}, top={len(payload.get('top') or [])}"
        )
    write_manifest(cfg, base_manifest)
    return base_manifest


def _freeze_snapshot(cfg: Dict[str, Any], source_snapshot: Path) -> Path:
    source_snapshot = Path(source_snapshot)
    sig = snapshot_signature(source_snapshot)
    trade_date = str(sig.get("trade_date") or datetime.now().strftime("%Y-%m-%d")).replace("-", "")
    root = output_root(cfg) / "cache" / "tail_preheat_snapshots"
    root.mkdir(parents=True, exist_ok=True)
    target = root / f"{trade_date}_{datetime.now():%H%M%S}"
    target.mkdir(parents=True, exist_ok=True)
    for path in source_snapshot.glob("*.txt"):
        if path.is_file():
            shutil.copy2(path, target / path.name)
    meta_path = source_snapshot / "snapshot_meta.json"
    if meta_path.exists():
        shutil.copy2(meta_path, target / meta_path.name)
    freeze_meta = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "source_snapshot_dir": str(source_snapshot),
        "source_signature": sig,
        "purpose": "X1Beam preheat frozen snapshot for V2.0 tail consistency",
    }
    (target / "v2_freeze_meta.json").write_text(json.dumps(freeze_meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return target
