"""分仓之神 V2.0 — 选股器运行管理（适配器层编排）"""
from __future__ import annotations

import csv
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.strategies.base import StrategyAdapter, StrategyResult


def prewarm_snapshot_mirror(
    snapshot_dir: Path,
    target_dir: Path,
    file_pattern: str = "*.txt",
) -> Dict[str, Any]:
    """预热快照镜像：将 snapshot 目录下的 TXT 文件复制到目标目录，供策略脚本使用

    V2.0 策略适配器通过 subprocess 调用原始筛选脚本时需要本地数据路径，
    此函数负责将统一快照分发给各策略的本地数据目录。
    """
    started = time.perf_counter()
    result: Dict[str, Any] = {
        "enabled": True,
        "ok": False,
        "source_dir": str(snapshot_dir),
        "mirror_dir": str(target_dir),
        "file_count": 0,
        "elapsed_seconds": 0.0,
        "errors": [],
        "reason": "",
    }

    if not snapshot_dir.exists():
        result["error"] = f"Snapshot dir not found: {snapshot_dir}"
        result["reason"] = "snapshot_not_found"
        return result

    target_dir.mkdir(parents=True, exist_ok=True)
    source_files = sorted(snapshot_dir.glob(file_pattern))
    if not source_files:
        result["error"] = f"No {file_pattern} files in {snapshot_dir}"
        result["reason"] = "no_source_files"
        return result

    copied = 0
    skipped = 0
    errors = []
    for src in source_files:
        dst = target_dir / src.name
        try:
            if dst.exists() and dst.stat().st_mtime >= src.stat().st_mtime:
                skipped += 1
                continue
            content = src.read_bytes()
            dst.write_bytes(content)
            copied += 1
        except Exception as exc:
            errors.append(f"{src.name}: {exc}")

    result["ok"] = len(errors) == 0
    result["file_count"] = copied
    result["skipped"] = skipped
    if errors:
        result["errors"] = errors[:10]
    result["elapsed_seconds"] = round(time.perf_counter() - started, 3)
    return result


def validate_screener_environment(
    adapters: List[StrategyAdapter],
) -> Dict[str, bool]:
    """验证所有策略适配器的运行环境"""
    status: Dict[str, bool] = {}
    for adapter in adapters:
        status[adapter.name] = adapter.validate_environment()
    return status


def collect_strategy_csv_results(
    result: StrategyResult,
    glob_pattern: str,
    search_dir: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    """从策略输出 CSV 收集结果行（作为 fallback）

    Args:
        result: 策略运行结果
        glob_pattern: CSV 文件匹配模式 (e.g. "vip_result_*.csv")
        search_dir: 搜索目录，默认搜索 adapter 的 screener_dir 父目录
    """
    rows: List[Dict[str, Any]] = []
    if not result.ok:
        return rows
    if search_dir and search_dir.exists():
        csv_files = sorted(
            search_dir.glob(glob_pattern),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if csv_files:
            try:
                with csv_files[0].open("r", encoding="gbk", errors="replace") as f:
                    for row in csv.DictReader(f):
                        code = row.get("code", "").strip().upper().replace("#", "")
                        if code.startswith(("SH", "SZ", "BJ")):
                            code = f"{code[:2]}{code[-6:]}"
                        rows.append({
                            "rank": len(rows) + 1,
                            "code": code,
                            "name": row.get("name", ""),
                        })
            except Exception:
                pass
    return rows
