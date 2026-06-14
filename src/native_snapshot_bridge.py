"""Isolated bridge into the vendored V1 multi-source snapshot builder.

This file is executed in a subprocess with PYTHONPATH pointed at
vendor/fczs_v1_core, so imports named ``src.*`` resolve to the vendored copy,
not to V2.0's runtime package.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any, Dict


def _json_safe(value: Any) -> Any:
    if dataclasses.is_dataclass(value):
        return _json_safe(dataclasses.asdict(value))
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v) for v in value]
    return value


def _source_summary(result: Any) -> Dict[str, Any]:
    return {
        "source": str(getattr(result, "source", "")),
        "ok": bool(getattr(result, "ok", False)),
        "rows": int(getattr(result, "row_count", 0) or 0),
        "error": str(getattr(result, "error", "") or ""),
        "fetched_at": _json_safe(getattr(result, "fetched_at", "")),
        "meta": _json_safe(getattr(result, "meta", {}) or {}),
    }


def _write_report(path: Path, payload: Dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="V2 native snapshot bridge")
    parser.add_argument("--config", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--v2-root", required=True)
    parser.add_argument("--vendor-root", required=True)
    args = parser.parse_args()

    vendor_root = Path(args.vendor_root).resolve()
    sys.path.insert(0, str(vendor_root))
    started = time.perf_counter()
    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)

    payload: Dict[str, Any] = {
        "ok": False,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "mode": "v2_native_snapshot_bridge",
        "vendor_root": str(vendor_root),
        "config_path": str(Path(args.config).resolve()),
        "snapshot_dir": "",
        "error": "",
        "sources": [],
    }
    try:
        from src.data_sources.manager import fetch_enabled_sources
        from src.settings import load_settings
        from src.snapshot_builder import build_snapshot

        cfg = load_settings(Path(args.config))
        cfg.setdefault("runtime", {})
        cfg["runtime"]["project_root"] = str(Path(args.v2_root).resolve())
        cfg["runtime"]["tushare_token"] = os.getenv("TUSHARE_TOKEN", "")
        cfg["runtime"]["zzshare_token"] = os.getenv("ZZSHARE_TOKEN", "")
        payload["stage"] = "fetch_sources"
        _write_report(report_path, payload)

        results = fetch_enabled_sources(cfg)
        payload["sources"] = [_source_summary(item) for item in results]
        payload["stage"] = "build_snapshot"
        payload["elapsed_seconds"] = round(time.perf_counter() - started, 3)
        _write_report(report_path, payload)

        snapshot = build_snapshot(results, cfg)
        validation = getattr(snapshot, "validation", None)
        payload.update(
            {
                "ok": bool(getattr(validation, "passed", False)),
                "snapshot_dir": str(getattr(snapshot, "snapshot_dir", "")),
                "trade_date": str(getattr(snapshot, "trade_date", "")),
                "created_files": int(getattr(snapshot, "created_files", 0) or 0),
                "reused_files": int(getattr(snapshot, "reused_files", 0) or 0),
                "skipped_files": int(getattr(snapshot, "skipped_files", 0) or 0),
                "metadata_path": str(getattr(snapshot, "metadata_path", "")),
                "validation": _json_safe(validation),
                "sources": [_source_summary(item) for item in results],
                "stage": "done",
                "elapsed_seconds": round(time.perf_counter() - started, 3),
            }
        )
    except Exception as exc:
        payload.update(
            {
                "ok": False,
                "error": str(exc),
                "traceback": traceback.format_exc()[-4000:],
                "stage": "error",
                "elapsed_seconds": round(time.perf_counter() - started, 3),
            }
        )

    _write_report(report_path, payload)
    return 0 if payload.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
