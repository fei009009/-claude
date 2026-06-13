"""X1Beam Forest Beam Search adapter with preheated-cache tail semantics."""
from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.common import norm_code, output_root
from src.strategies.base import StrategyAdapter, StrategyResult


class X1BeamAdapter(StrategyAdapter):
    def __init__(self, x1_dir: Path, top_n: int = 10):
        super().__init__(name="x1beam", display_name="X1Beam", top_n=top_n)
        self._dir = Path(x1_dir)
        self._script = self._dir / "screener_beam.py"
        self._runner = Path(__file__).resolve().with_name("x1beam_fast_runner.py")

    def validate_environment(self) -> bool:
        return self._script.exists() and self._runner.exists()

    def run(self, snap: Path, cfg: Dict[str, Any]) -> StrategyResult:
        started = time.perf_counter()
        result = StrategyResult(strategy_name=self.name, display_name=self.display_name, top_n=self.top_n)
        x1_cfg = cfg.get("strategies", {}).get("x1beam", {})
        timeout_seconds = int(x1_cfg.get("timeout_seconds", 45))
        workers = int(x1_cfg.get("workers", 1))
        allow_stale_on_timeout = bool(x1_cfg.get("allow_stale_cache_on_timeout", False))
        require_preheated = bool(x1_cfg.get("require_preheated_cache", True))

        if not self.validate_environment():
            result.error = f"X1Beam environment missing: {self._dir}"
            result.elapsed_seconds = round(time.perf_counter() - started, 3)
            return result

        cache_meta = self._snapshot_cache_meta(snap)
        cache_path = self._preheated_cache(cfg, cache_meta)
        mode = "preheated_cache"

        if cache_path is None:
            if require_preheated:
                result.error = "X1Beam: no complete preheated cache for current snapshot"
                result.quality_fields = {"mode": "missing_preheated_cache", "snapshot": cache_meta}
                result.metadata = {"cache_mode": "missing_preheated_cache", "snapshot": cache_meta}
                result.elapsed_seconds = round(time.perf_counter() - started, 3)
                return result
            mode = "generated"
            try:
                cache_path = self._run_fast_runner(snap, cfg, cache_meta, workers, timeout_seconds)
            except subprocess.TimeoutExpired:
                mode = "timeout_stale_cache"
                if allow_stale_on_timeout:
                    cache_path = self._latest_cache(cfg, cache_meta, fresh_only=False)
                if cache_path is None:
                    result.error = f"X1Beam timeout after {timeout_seconds}s; no usable cache"
            except Exception as exc:
                mode = "error_stale_cache"
                if allow_stale_on_timeout:
                    cache_path = self._latest_cache(cfg, cache_meta, fresh_only=False)
                if cache_path is None:
                    result.error = f"X1Beam fast runner error: {exc}"

        if cache_path and not result.error:
            payload = self._read_payload(cache_path)
            rows = self._rows_from_payload(payload)
            for idx, row in enumerate(rows[: self.top_n], 1):
                row["rank"] = idx
            result.top = rows[: self.top_n]
            result.quality_fields = {
                "mode": mode,
                "cache_path": str(cache_path),
                "cache_generated_at": payload.get("generated_at", ""),
                "file_count": payload.get("file_count", 0),
                "processed_hit_stocks": payload.get("processed_hit_stocks", 0),
                "error_count": payload.get("error_count", 0),
                "runner_elapsed_seconds": payload.get("elapsed_seconds", 0),
                "formula_counts": payload.get("formula_counts", {}),
            }
            if not result.top:
                result.error = "X1Beam: no results"
        result.metadata = {"cache_mode": mode, "snapshot": cache_meta}
        result.elapsed_seconds = round(time.perf_counter() - started, 3)
        return result

    def _run_fast_runner(
        self,
        snap: Path,
        cfg: Dict[str, Any],
        cache_meta: Dict[str, Any],
        workers: int,
        timeout_seconds: int,
    ) -> Path:
        self._ensure_summary_files(overwrite=False)
        out_dir = self._cache_dir(cfg)
        trade_date = cache_meta.get("trade_date") or datetime.now().strftime("%Y-%m-%d")
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_path = out_dir / f"x1beam_fast_{trade_date.replace('-', '')}_{stamp}.json"
        cp = subprocess.run(
            [
                sys.executable,
                str(self._runner),
                "--x1-dir",
                str(self._dir),
                "--snapshot",
                str(snap),
                "--output",
                str(out_path),
                "--workers",
                str(max(workers, 1)),
                "--top-n",
                str(max(self.top_n, 10)),
                "--keep-per-tier",
                "50",
                "--time-budget",
                str(max(timeout_seconds - 5, 1)),
            ],
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            cwd=str(Path(__file__).resolve().parents[2]),
        )
        if cp.returncode not in (0, 2) and not out_path.exists():
            raise RuntimeError((cp.stderr or cp.stdout or "X1Beam runner failed")[-800:])
        if not out_path.exists():
            raise RuntimeError((cp.stderr or cp.stdout or "X1Beam runner produced no cache")[-800:])
        payload = self._read_payload(out_path)
        payload["adapter_snapshot_meta"] = cache_meta
        payload["runner_returncode"] = cp.returncode
        payload["runner_stdout_tail"] = (cp.stdout or "")[-1000:]
        payload["runner_stderr_tail"] = (cp.stderr or "")[-1000:]
        out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        if payload.get("completed") is not True:
            scanned = payload.get("scanned_files", 0)
            total = payload.get("file_count", 0)
            raise RuntimeError(f"X1Beam fast runner incomplete: scanned {scanned}/{total}; cache kept for audit only")
        return out_path

    def _rows_from_payload(self, payload: Dict[str, Any]) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        for item in payload.get("top", []) or []:
            code = norm_code(item.get("code", ""))
            if not code:
                continue
            rows.append(
                {
                    "rank": len(rows) + 1,
                    "code": code,
                    "name": str(item.get("name", "") or ""),
                    "price": float(item.get("close", 0) or 0),
                    "pct_chg": float(item.get("daily_gain_pct", 0) or 0),
                    "wr": float(item.get("top_wr", 0) or 0),
                    "lift": float(item.get("top_lift", 0) or 0),
                    "matched_combos": int(float(item.get("matched_count", 0) or 0)),
                    "avg_wr": float(item.get("avg_wr", 0) or 0),
                    "tier": str(item.get("tier", "")),
                    "top_path": str(item.get("top_path", ""))[:160],
                }
            )
        return rows

    def _cache_dir(self, cfg: Dict[str, Any]) -> Path:
        path = output_root(cfg) / "cache" / "x1beam_fast"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _preheated_cache(self, cfg: Dict[str, Any], cache_meta: Dict[str, Any]) -> Optional[Path]:
        manifest_path = self._cache_dir(cfg) / "x1beam_preheat_manifest.json"
        if not manifest_path.exists():
            return self._latest_cache(cfg, cache_meta, fresh_only=True)
        manifest = self._read_payload(manifest_path)
        if manifest.get("completed") is not True:
            return None
        cache_path = Path(str(manifest.get("cache_path") or ""))
        if not cache_path.exists():
            return None
        sig = manifest.get("snapshot_signature") or {}
        meta = {
            "snapshot_dir": sig.get("snapshot_dir"),
            "trade_date": sig.get("trade_date"),
            "file_count": sig.get("file_count"),
            "max_mtime": sig.get("max_mtime"),
        }
        if self._cache_matches(meta, cache_meta):
            return cache_path
        return None

    def _latest_cache(self, cfg: Dict[str, Any], cache_meta: Dict[str, Any], *, fresh_only: bool) -> Optional[Path]:
        files = sorted(self._cache_dir(cfg).glob("x1beam_fast_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        for path in files:
            payload = self._read_payload(path)
            if payload.get("completed") is not True:
                continue
            meta = payload.get("adapter_snapshot_meta") or {}
            same_trade_date = meta.get("trade_date") == cache_meta.get("trade_date")
            if not same_trade_date:
                continue
            if fresh_only and not self._cache_matches(meta, cache_meta):
                continue
            return path
        return None

    def _cache_matches(self, cached: Dict[str, Any], current: Dict[str, Any]) -> bool:
        return (
            cached.get("snapshot_dir") == current.get("snapshot_dir")
            and cached.get("file_count") == current.get("file_count")
            and abs(float(cached.get("max_mtime", 0) or 0) - float(current.get("max_mtime", 0) or 0)) < 0.001
        )

    def _snapshot_cache_meta(self, snap: Path) -> Dict[str, Any]:
        files = [p for p in Path(snap).glob("*.txt") if p.is_file()]
        max_mtime = max((p.stat().st_mtime for p in files), default=0.0)
        trade_date = ""
        meta_path = Path(snap) / "snapshot_meta.json"
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                trade_date = str(meta.get("trade_date") or "")
            except Exception:
                trade_date = ""
        if not trade_date:
            trade_date = datetime.fromtimestamp(max_mtime).strftime("%Y-%m-%d") if max_mtime else ""
        return {
            "snapshot_dir": str(Path(snap).resolve()),
            "trade_date": trade_date,
            "file_count": len(files),
            "max_mtime": max_mtime,
        }

    def _read_payload(self, path: Path) -> Dict[str, Any]:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _ensure_summary_files(self, overwrite: bool = False) -> None:
        summary_dir = self._dir / "cache" / "_summary"
        summary_dir.mkdir(parents=True, exist_ok=True)
        source = self._dir / "cache" / "beam_merged.json"
        if not source.exists():
            source = self._dir / "cache" / "beam_all_deduped.json"
        if not source.exists():
            source = self._dir / "beam_all_deduped.json"
        if not source.exists():
            return
        try:
            data = json.loads(source.read_text(encoding="utf-8"))
        except Exception:
            return
        combos = data.get("combos", []) if isinstance(data, dict) else []
        if not combos:
            return
        normalized = []
        for combo in combos:
            item = dict(combo)
            path = item.get("path")
            if isinstance(path, dict):
                item["path"] = [[k, v] for k, v in path.items()]
            normalized.append(item)
        targets = {
            "y_close_5d_5pct": "5d close >=5% compatibility summary",
            "y_close_5d_0pct": "5d close >=0% compatibility summary",
            "y_high_5d_5pct": "5d high >=5% compatibility summary",
            "y_next_5pct": "next day >=5% compatibility summary",
        }
        for target, desc in targets.items():
            out = summary_dir / f"{target}_merged.json"
            if out.exists() and not overwrite:
                continue
            payload = {
                "target": target,
                "desc": desc,
                "source": str(source),
                "total_raw": len(normalized),
                "total_deduped": len(normalized),
                "combos": normalized,
            }
            out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
