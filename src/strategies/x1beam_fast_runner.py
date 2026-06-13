"""Fast X1Beam runner for the V2.0 adapter.

It keeps the X1Beam rule semantics but scans each stock only once, then matches
all five WR tiers in memory. The adapter executes this module in a subprocess so
formal tail runs can enforce a hard timeout.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import numpy as np

_IND_MOD = None
_FORMULAS: Dict[str, List[Dict[str, Any]]] = {}
_CFG: Dict[str, Any] = {}


def _read_text_head(path: Path) -> str:
    try:
        with path.open("r", encoding="gbk", errors="ignore") as f:
            return f.readline().strip()
    except Exception:
        return ""


def _stock_name(path: Path) -> str:
    first = _read_text_head(path)
    parts = first.split()
    return parts[1] if len(parts) >= 2 else ""


def _is_st_stock(path: Path) -> bool:
    if "ST" in path.name.upper():
        return True
    first = _read_text_head(path)
    return "ST" in first or "*ST" in first


def _read_kline_raw(path: Path) -> Optional[Dict[str, np.ndarray]]:
    try:
        raw = path.read_text(encoding="gbk", errors="ignore").splitlines()
    except Exception:
        try:
            raw = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        except Exception:
            return None
    if len(raw) < 62:
        return None

    o: List[float] = []
    h: List[float] = []
    l: List[float] = []
    c: List[float] = []
    v: List[float] = []
    for line in raw[2:]:
        parts = line.strip().split("\t")
        if len(parts) < 6:
            continue
        try:
            o.append(float(parts[1]))
            h.append(float(parts[2]))
            l.append(float(parts[3]))
            c.append(float(parts[4]))
            v.append(float(parts[6]) if len(parts) > 6 else float(parts[5]))
        except (ValueError, IndexError):
            continue
    if len(c) < int(_CFG.get("min_history", 60)):
        return None
    return {
        "o": np.asarray(o, dtype=np.float64),
        "h": np.asarray(h, dtype=np.float64),
        "l": np.asarray(l, dtype=np.float64),
        "c": np.asarray(c, dtype=np.float64),
        "v": np.asarray(v, dtype=np.float64),
    }


def _init_worker(indicator_path: str, formulas: Dict[str, List[Dict[str, Any]]], cfg: Dict[str, Any]) -> None:
    global _IND_MOD, _FORMULAS, _CFG
    _FORMULAS = formulas
    _CFG = cfg
    spec = importlib.util.spec_from_file_location("x1_indicator", indicator_path)
    if not spec or not spec.loader:
        raise RuntimeError(f"cannot load indicator module: {indicator_path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    _IND_MOD = mod


def _process_stock(path_text: str) -> Optional[Dict[str, Any]]:
    path = Path(path_text)
    code = path.name[:-4] if path.name.lower().endswith(".txt") else path.name
    try:
        if _CFG.get("filter_bj", True) and ("#BJ" in code or code.startswith("8")):
            return None
        if _CFG.get("filter_st", True) and _is_st_stock(path):
            return None

        kl = _read_kline_raw(path)
        if kl is None:
            return None

        close_t = float(kl["c"][-1])
        close_y = float(kl["c"][-2]) if len(kl["c"]) >= 2 else close_t
        if close_t <= 0 or close_y <= 0:
            return None
        daily_gain = (close_t / close_y - 1) * 100
        if _CFG.get("filter_limit_up", True) and daily_gain >= float(_CFG.get("limit_up_threshold", 9.0)):
            return None

        indicators = _IND_MOD.calc_indicators(kl["o"], kl["h"], kl["l"], kl["c"], kl["v"], code)
        binned: Dict[str, int] = {}
        for name in _CFG["continuous_indicators"]:
            val = indicators.get(name)
            if val is None:
                return None
            binned[name] = int(np.digitize(float(val[-1]), bins=_CFG["bin_edges"]) + 1)
        for name in _CFG["binary_indicators"]:
            val = indicators.get(name)
            if val is None:
                return None
            binned[name] = int(np.clip(np.round(float(val[-1])), 0, 1))

        tier_hits: List[Dict[str, Any]] = []
        for tier, formulas in _FORMULAS.items():
            hits = []
            for idx, formula in enumerate(formulas):
                path_dict = formula.get("path") or {}
                if all(binned.get(k) == v for k, v in path_dict.items()):
                    hits.append((idx, formula))
            if not hits:
                continue
            wrs = [float(f.get("wr", 0) or 0) for _, f in hits]
            lifts = [float(f.get("lift", 0) or 0) for _, f in hits]
            best_idx = max(range(len(hits)), key=lambda i: (wrs[i], lifts[i], int(hits[i][1].get("n", 0) or 0)))
            best = hits[best_idx][1]
            tier_hits.append({
                "code": code,
                "name": _stock_name(path),
                "close": round(close_t, 2),
                "daily_gain_pct": round(daily_gain, 2),
                "matched_count": len(hits),
                "top_wr": round(float(best.get("wr", 0) or 0), 4),
                "avg_wr": round(sum(wrs) / max(len(wrs), 1), 4),
                "top_lift": round(float(best.get("lift", 0) or 0), 4),
                "top_path": best.get("path_str", ""),
                "tier": tier,
            })
        if not tier_hits:
            return None
        return {"code": code, "tiers": tier_hits}
    except Exception as exc:
        return {"code": code, "error": str(exc)[:200]}


def _normalize_formula(formula: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    try:
        wr = float(formula.get("wr", 0) or 0)
    except (TypeError, ValueError):
        return None
    if wr > 1:
        wr /= 100.0

    raw_path = formula.get("path") or []
    if isinstance(raw_path, dict):
        items = list(raw_path.items())
    else:
        items = list(raw_path)
    clean_path: Dict[str, int] = {}
    clean_items = []
    for item in items:
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            continue
        try:
            value = int(item[1])
        except (TypeError, ValueError):
            continue
        name = str(item[0])
        clean_path[name] = value
        clean_items.append((name, value))
    if not clean_path:
        return None

    try:
        lift = float(formula.get("lift", 0) or 0)
    except (TypeError, ValueError):
        lift = 0.0
    return {
        "wr": round(wr, 4),
        "n": int(float(formula.get("n", 0) or 0)),
        "lift": round(lift, 4),
        "path": clean_path,
        "path_str": " -> ".join(f"{k}={v}" for k, v in clean_items),
    }


def _load_formulas(x1_dir: Path) -> tuple[Dict[str, List[Dict[str, Any]]], Dict[str, Any]]:
    os.environ.setdefault("X1_DATA_DIR", "")
    sys.path.insert(0, str(x1_dir))
    import screener_beam as sb  # type: ignore

    tier_formulas: Dict[str, List[Dict[str, Any]]] = {}
    for tier_name, tier in sb.SCREENER_TIERS.items():
        merged = Path(sb.SUMMARY_DIR) / sb.TARGETS[tier["target"]]["merged_json"]
        data = json.loads(merged.read_text(encoding="utf-8"))
        formulas: List[Dict[str, Any]] = []
        for combo in data.get("combos", []):
            normalized = _normalize_formula(combo)
            if not normalized:
                continue
            wr = normalized["wr"]
            if tier["wr_lo"] <= wr < tier["wr_hi"]:
                formulas.append(normalized)
        formulas.sort(key=lambda item: (-item["wr"], -item["n"], -item["lift"]))
        tier_formulas[tier_name] = formulas

    cfg = {
        "continuous_indicators": list(sb.CONTINUOUS_INDICATORS),
        "binary_indicators": list(sb.BINARY_INDICATORS),
        "bin_edges": list(sb.BIN_EDGES),
        "min_history": int(getattr(sb, "MIN_HISTORY", 60)),
        "filter_bj": bool(getattr(sb, "FILTER_BJ_BOARD", True)),
        "filter_st": bool(getattr(sb, "FILTER_ST_STOCK", True)),
        "filter_limit_up": bool(getattr(sb, "FILTER_LIMIT_UP", True)),
        "limit_up_threshold": float(getattr(sb, "LIMIT_UP_THRESHOLD", 9.0)),
        "indicator_path": str(sb.INDICATOR_MODULE_PATH),
    }
    return tier_formulas, cfg


def _load_snapshot_meta(snapshot_dir: Path) -> Dict[str, Any]:
    meta = snapshot_dir / "snapshot_meta.json"
    if not meta.exists():
        return {}
    try:
        return json.loads(meta.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _sort_rows(rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return sorted(
        rows,
        key=lambda row: (
            -float(row.get("top_wr", 0) or 0),
            -int(row.get("matched_count", 0) or 0),
            -float(row.get("top_lift", 0) or 0),
            -float(row.get("avg_wr", 0) or 0),
            str(row.get("code", "")),
        ),
    )


def run(args: argparse.Namespace) -> int:
    started = time.perf_counter()
    x1_dir = Path(args.x1_dir).resolve()
    snapshot_dir = Path(args.snapshot).resolve()
    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)

    os.environ["X1_DATA_DIR"] = str(snapshot_dir)
    tier_formulas, runner_cfg = _load_formulas(x1_dir)
    files = sorted(path for path in snapshot_dir.glob("*.txt") if path.is_file())
    if args.max_files and args.max_files > 0:
        files = files[: args.max_files]

    rows_by_tier: Dict[str, List[Dict[str, Any]]] = {tier: [] for tier in tier_formulas}
    processed = 0
    visited = 0
    errors: List[Dict[str, Any]] = []
    workers = max(1, int(args.workers or 1))
    deadline = started + float(args.time_budget) if args.time_budget and args.time_budget > 0 else None
    completed = True

    # Bounded tail runs stay single-process so Windows child processes cannot
    # survive a timeout and block the caller. Full preheats can still use workers.
    if deadline:
        workers = 1

    if workers == 1:
        _init_worker(runner_cfg["indicator_path"], tier_formulas, runner_cfg)
        for path in files:
            if deadline and time.perf_counter() >= deadline:
                completed = False
                break
            visited += 1
            item = _process_stock(str(path))
            if not item:
                continue
            if item.get("error"):
                errors.append(item)
                continue
            processed += 1
            for row in item.get("tiers", []):
                rows_by_tier.setdefault(row["tier"], []).append(row)
    else:
        with ProcessPoolExecutor(
            max_workers=workers,
            initializer=_init_worker,
            initargs=(runner_cfg["indicator_path"], tier_formulas, runner_cfg),
        ) as executor:
            futures = {executor.submit(_process_stock, str(path)): path.name for path in files}
            for future in as_completed(futures):
                if deadline and time.perf_counter() >= deadline:
                    completed = False
                    for pending in futures:
                        pending.cancel()
                    break
                visited += 1
                item = future.result()
                if not item:
                    continue
                if item.get("error"):
                    errors.append(item)
                    continue
                processed += 1
                for row in item.get("tiers", []):
                    rows_by_tier.setdefault(row["tier"], []).append(row)

    if visited < len(files):
        completed = False

    for tier, rows in list(rows_by_tier.items()):
        rows_by_tier[tier] = _sort_rows(rows)[: max(args.keep_per_tier, args.top_n)]

    all_rows: List[Dict[str, Any]] = []
    for rows in rows_by_tier.values():
        all_rows.extend(rows)
    deduped: List[Dict[str, Any]] = []
    seen = set()
    for row in _sort_rows(all_rows):
        code = row.get("code")
        if code in seen:
            continue
        seen.add(code)
        deduped.append(row)

    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "snapshot_dir": str(snapshot_dir),
        "snapshot_meta": _load_snapshot_meta(snapshot_dir),
        "completed": completed,
        "file_count": len(files),
        "scanned_files": visited,
        "processed_hit_stocks": processed,
        "error_count": len(errors),
        "errors": errors[:20],
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "workers": workers,
        "formula_counts": {tier: len(formulas) for tier, formulas in tier_formulas.items()},
        "tiers": rows_by_tier,
        "top": deduped[: args.top_n],
    }
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "output": str(output),
        "top": len(payload["top"]),
        "completed": completed,
        "scanned_files": visited,
        "processed_hit_stocks": processed,
        "elapsed_seconds": payload["elapsed_seconds"],
    }, ensure_ascii=False))
    return 0 if payload["top"] and completed else 2


def main() -> int:
    parser = argparse.ArgumentParser(description="Fast X1Beam runner")
    parser.add_argument("--x1-dir", required=True)
    parser.add_argument("--snapshot", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--top-n", type=int, default=10)
    parser.add_argument("--keep-per-tier", type=int, default=50)
    parser.add_argument("--max-files", type=int, default=0)
    parser.add_argument("--time-budget", type=float, default=0)
    return run(parser.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
