"""XGB 25-bin calibration/backtest status for the V2.0 validation layer."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

from src.common import latest_file, write_report


TARGETS = (
    "y_high_5d_5pct",
    "y_close_5d_5pct",
    "y_close_5d_0pct",
    "y_next_5pct",
)

SCRIPT_ASSET_PATTERNS = (
    "xgb_bin_features.py",
    "xgb_bin_diagnose.py",
    "xgb_bin_report.py",
    "xgb_bin_indicator.py",
    "xgb_bin_build_samples.py",
    "xgb_bin_train.py",
    "xgb_bin_rule_audit.py",
    "xgb_bin_backtest.py",
    "xgb_bin_model/*.json",
    "xgb_bin_model/*.pkl",
    "xgb_bin_model/rules/*.json",
)

SAMPLE_ASSET_PATTERNS = (
    "xgb_bin_model/bin_samples.npz",
    "xgb_bin_model/bin_samples_v2.npz",
)


def ensure_xgb_vendor_assets(cfg: Dict[str, Any], *, include_samples: bool = False) -> Dict[str, Any]:
    """Mirror required XGB files into V2.0 vendor without mutating the source project."""
    paths = cfg.get("paths") or {}
    source_root = Path(str(paths.get("reference_xgb_dir") or "")).resolve()
    target_root = Path(str(paths.get("xgb_dir") or "vendor/xgb_diagnosis")).resolve()
    if not source_root.exists():
        return {"ok": False, "source": str(source_root), "target": str(target_root), "error": "reference_xgb_dir missing"}
    copied: List[str] = []
    skipped: List[str] = []
    patterns = list(SCRIPT_ASSET_PATTERNS)
    if include_samples:
        patterns.extend(SAMPLE_ASSET_PATTERNS)
    for pattern in patterns:
        matches = [path for path in source_root.glob(pattern) if path.is_file()]
        for src in matches:
            rel = src.relative_to(source_root)
            dst = target_root / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            if dst.exists() and dst.stat().st_mtime >= src.stat().st_mtime and dst.stat().st_size == src.stat().st_size:
                skipped.append(str(rel))
                continue
            shutil.copy2(src, dst)
            copied.append(str(rel))
    return {
        "ok": True,
        "source": str(source_root),
        "target": str(target_root),
        "include_samples": include_samples,
        "copied": copied,
        "skipped_count": len(skipped),
    }


def _file_state(path: Path) -> Dict[str, Any]:
    return {
        "path": str(path),
        "exists": path.exists(),
        "size": path.stat().st_size if path.exists() else 0,
        "mtime": datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds") if path.exists() else "",
    }


def _latest_backtest(xgb_dir: Path, top_n: int) -> Dict[str, Any]:
    path = latest_file(xgb_dir / "screener_output", [f"bin_backtest_top{top_n}_*.json"])
    if not path:
        return {"exists": False}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"exists": True, "path": str(path), "error": str(exc)}
    payload.pop("daily_rows", None)
    payload["exists"] = True
    payload["path"] = str(path)
    return payload


def _run_backtest(
    xgb_dir: Path,
    *,
    top_n: int,
    start_date: int,
    min_stocks_per_day: int,
    timeout: int,
    snapshot_dir: Path,
) -> Dict[str, Any]:
    script = xgb_dir / "xgb_bin_backtest.py"
    cmd = [
        sys.executable,
        str(script),
        "--top-n",
        str(top_n),
        "--min-stocks-per-day",
        str(min_stocks_per_day),
    ]
    if start_date:
        cmd.extend(["--start-date", str(start_date)])
    env = os.environ.copy()
    env["XGB_BIN_DATA_DIR"] = str(snapshot_dir)
    try:
        cp = subprocess.run(
            cmd,
            cwd=str(xgb_dir),
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"backtest timeout after {timeout}s", "cmd": cmd}
    return {
        "ok": cp.returncode == 0,
        "returncode": cp.returncode,
        "cmd": cmd,
        "stdout_tail": (cp.stdout or "")[-3000:],
        "stderr_tail": (cp.stderr or "")[-3000:],
    }


def build_xgb_calibration_status(
    cfg: Dict[str, Any],
    *,
    top_n: int = 50,
    run_backtest: bool = False,
    start_date: int = 0,
    min_stocks_per_day: int = 500,
    timeout: int = 900,
    persist: bool = True,
) -> Dict[str, Any]:
    paths = cfg.get("paths") or {}
    sync = ensure_xgb_vendor_assets(cfg, include_samples=True)
    xgb_dir = Path(str(paths.get("xgb_dir") or "vendor/xgb_diagnosis")).resolve()
    model_dir = xgb_dir / "xgb_bin_model"
    rules_dir = model_dir / "rules"
    snapshot_dir = Path(str(paths.get("snapshot_root") or "")).resolve()

    scripts = {
        name: _file_state(xgb_dir / name)
        for name in (
            "xgb_bin_build_samples.py",
            "xgb_bin_train.py",
            "xgb_bin_rule_audit.py",
            "xgb_bin_backtest.py",
            "xgb_bin_diagnose.py",
            "xgb_bin_report.py",
        )
    }
    datasets = {
        "bin_samples_v2": _file_state(model_dir / "bin_samples_v2.npz"),
        "bin_samples_legacy": _file_state(model_dir / "bin_samples.npz"),
    }
    models = {
        target: _file_state(model_dir / f"xgb_bin_model_{target}.json")
        for target in TARGETS
    }
    configs = {
        target: _file_state(model_dir / f"config_bin_{target}.json")
        for target in TARGETS
    }
    rule_audits = {
        target: _file_state(rules_dir / f"{target}_test_audit.json")
        for target in TARGETS
    }

    missing: List[str] = []
    for group_name, group in (
        ("script", scripts),
        ("model", models),
        ("config", configs),
        ("rule_audit", rule_audits),
    ):
        for key, state in group.items():
            if not state.get("exists"):
                missing.append(f"{group_name}:{key}")
    if not datasets["bin_samples_v2"].get("exists"):
        missing.append("dataset:bin_samples_v2")

    backtest_run = None
    if run_backtest:
        backtest_run = _run_backtest(
            xgb_dir,
            top_n=top_n,
            start_date=start_date,
            min_stocks_per_day=min_stocks_per_day,
            timeout=timeout,
            snapshot_dir=snapshot_dir,
        )

    latest = _latest_backtest(xgb_dir, top_n)
    work_needed: List[str] = []
    if missing:
        work_needed.append("补齐缺失的 XGB 样本、模型、规则审计或脚本资产")
    if not latest.get("exists"):
        work_needed.append(f"运行 walk-forward Top{top_n} 回测，生成最新 bin_backtest_top{top_n}_*.json")
    if latest.get("exists") and not latest.get("targets"):
        work_needed.append("检查回测输出是否包含四目标命中率、Lift 和逐日表现")
    if not work_needed:
        work_needed.append("进入观察校准阶段：用每日出票结果继续累计真实次日/五日收益表现")

    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "role": "xgb_validation_layer_calibration",
        "xgb_dir": str(xgb_dir),
        "top_n": top_n,
        "vendor_sync": sync,
        "scripts": scripts,
        "datasets": datasets,
        "models": models,
        "configs": configs,
        "rule_audits": rule_audits,
        "latest_backtest": latest,
        "backtest_run": backtest_run,
        "missing": missing,
        "ready_for_diagnosis": not bool(missing),
        "ready_for_hard_signal": not bool(missing) and bool(latest.get("exists")),
        "work_needed": work_needed,
    }
    if persist:
        path = write_report("xgb_calibration_status", report, cfg)
        report["report_path"] = str(path)
    return report
