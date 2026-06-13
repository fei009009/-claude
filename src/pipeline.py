"""分仓之神 V2.0 主流水线 — 四策略并行 + XGB 诊断"""
from __future__ import annotations
import time
import warnings
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


def _resolve_top_n(adapter_path: str, cfg: dict) -> int:
    """从配置中解析策略的 top_n 设置"""
    strategy_key = adapter_path.split(".")[-1].replace("Adapter", "").lower()
    strategies_cfg = cfg.get("strategies", {})
    for key in strategies_cfg:
        if key.lower() == strategy_key or strategy_key.startswith(key.lower()):
            return int(strategies_cfg[key].get("top_n", 10))
    return 10


def _run_strategy_process(
    adapter_class_path: str,
    snapshot_dir: str,
    screener_dir: str,
    top_n: int,
    cfg: dict,
) -> dict:
    """进程隔离运行单个策略"""
    import importlib

    module_path, class_name = adapter_class_path.rsplit(".", 1)
    mod = importlib.import_module(module_path)
    adapter_cls = getattr(mod, class_name)
    adapter = adapter_cls(Path(screener_dir), top_n=top_n)
    result = adapter.run(Path(snapshot_dir), cfg)
    return {
        "strategy_name": result.strategy_name,
        "display_name": result.display_name,
        "ok": result.ok,
        "error": result.error,
        "top": result.top,
        "top_n": result.top_n,
        "elapsed_seconds": result.elapsed_seconds,
        "quality_fields": result.quality_fields,
        "metadata": result.metadata,
    }


def _make_strategy_result_error(strategy_name: str, error: str) -> dict:
    """构建统一格式的错误结果"""
    return {
        "strategy_name": strategy_name,
        "display_name": strategy_name,
        "ok": False,
        "error": error,
        "top": [],
        "top_n": 0,
        "elapsed_seconds": 0,
        "quality_fields": {},
        "metadata": {},
    }


def run_all_strategies(
    snapshot_dir: Path,
    cfg: dict,
    parallel: bool = True,
) -> Tuple[List[dict], dict]:
    """运行全部四策略，返回 (结果列表, 汇总)

    先做环境预检（检查筛选脚本是否存在），然后并行/串行执行。
    每完成一个策略即打印进度。
    """
    started = time.perf_counter()

    strategy_configs = [
        ("src.strategies.v10_adapter.V10Adapter",
         str(cfg.get("paths", {}).get("vip_screener_dir", "vendor/VIP")), "V10"),
        ("src.strategies.v1_adapter.V1Adapter",
         str(cfg.get("paths", {}).get("legacy_screener_dir", "vendor/legacy_screeners")), "V1"),
        ("src.strategies.v4_adapter.V4Adapter",
         str(cfg.get("paths", {}).get("legacy_screener_dir", "vendor/legacy_screeners")), "V4"),
        ("src.strategies.x1beam_adapter.X1BeamAdapter",
         str(cfg.get("paths", {}).get("x1_xin_dir", r"D:\ZTFHQ\X1-XIN")), "X1Beam"),
    ]

    # 预检：确认筛选脚本存在
    print("预检策略环境...", flush=True)
    for adapter_path, screener_dir, label in strategy_configs:
        try:
            module_path, class_name = adapter_path.rsplit(".", 1)
            mod = __import__(module_path, fromlist=[class_name])
            adapter_cls = getattr(mod, class_name)
            adapter = adapter_cls(Path(screener_dir), top_n=10)
            env_ok = adapter.validate_environment()
            print(f"  {label:<8} {'OK' if env_ok else 'MISSING'}  ({screener_dir})", flush=True)
        except Exception as exc:
            print(f"  {label:<8} ERROR ({exc})", flush=True)

    # 每个策略独立的 top_n
    tasks = [
        (adapter_path, str(snapshot_dir), screener_dir, _resolve_top_n(adapter_path, cfg), cfg)
        for adapter_path, screener_dir, _label in strategy_configs
    ]

    results: List[dict] = []
    if parallel:
        # 优先尝试 ProcessPoolExecutor，失败降级为 ThreadPoolExecutor
        executor_class = ProcessPoolExecutor
        executor_kwargs: dict = {"max_workers": min(4, len(tasks))}
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                with executor_class(**executor_kwargs) as executor:
                    futures = {
                        executor.submit(_run_strategy_process, *task): task[0]
                        for task in tasks
                    }
                    for future in as_completed(futures):
                        try:
                            results.append(future.result(timeout=360))
                        except Exception as exc:
                            results.append(
                                _make_strategy_result_error(futures[future], str(exc))
                            )
        except RuntimeError:
            # Windows spawn 失败，降级为线程池
            import warnings as _w
            _w.warn("ProcessPoolExecutor unavailable, falling back to ThreadPoolExecutor")
            executor_class = ThreadPoolExecutor
            with executor_class(max_workers=min(4, len(tasks))) as executor:
                futures = {
                    executor.submit(_run_strategy_process, *task): task[0]
                    for task in tasks
                }
                for future in as_completed(futures):
                    try:
                        results.append(future.result(timeout=360))
                    except Exception as exc:
                        results.append(
                            _make_strategy_result_error(futures[future], str(exc))
                        )
    else:
        for adapter_path, snapshot, screener_dir, top_n, _cfg in tasks:
            try:
                r = _run_strategy_process(adapter_path, snapshot, screener_dir, top_n, _cfg)
                results.append(r)
            except Exception as exc:
                results.append(_make_strategy_result_error(adapter_path, str(exc)))

    # Build overlaps
    codes_by_strategy: Dict[str, set] = {}
    for r in results:
        if r["ok"]:
            codes_by_strategy[r["strategy_name"]] = {row["code"] for row in r["top"]}

    all_codes: set = set()
    for codes in codes_by_strategy.values():
        all_codes |= codes

    overlap_codes: Dict[str, List[str]] = {}
    for code in all_codes:
        strategies = [s for s, cs in codes_by_strategy.items() if code in cs]
        if len(strategies) >= 2:
            overlap_codes[code] = strategies

    summary = {
        "total_elapsed_seconds": round(time.perf_counter() - started, 3),
        "strategies_run": len(results),
        "strategies_ok": sum(1 for r in results if r["ok"]),
        "total_candidates": len(all_codes),
        "overlap_candidates": len(overlap_codes),
        "timestamp": datetime.now().isoformat(timespec="seconds"),
    }

    return results, summary


def build_overlap_analysis(results: List[dict], cfg: Optional[dict] = None) -> dict:
    """构建多策略交集分析"""
    codes = {}
    for r in results:
        if r["ok"]:
            for row in r["top"]:
                code = row["code"]
                if code not in codes:
                    codes[code] = {"code": code, "name": row.get("name", ""), "strategies": [], "ranks": {}}
                codes[code]["strategies"].append(r["strategy_name"])
                codes[code]["ranks"][r["strategy_name"]] = row.get("rank", 99)

    overlap_list = []
    for c in codes.values():
        if len(c["strategies"]) >= 2:
            c["strategy_count"] = len(c["strategies"])
            c["intersection_label"] = "+".join(sorted(c["strategies"]))
            overlap_list.append(c)

    overlap_list.sort(key=lambda x: (-x["strategy_count"], min(x["ranks"].values())))

    return {
        "total_overlaps": len(overlap_list),
        "overlaps": overlap_list[:30],
        "by_count": {
            k: len([o for o in overlap_list if o["strategy_count"] == k])
            for k in [2, 3, 4]
        },
    }
