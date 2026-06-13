"""V1 — importlib + scan_market, exact V1.0 row format"""
from __future__ import annotations
import importlib.util, io, sys, time
from pathlib import Path
from typing import Any, Dict, List
from src.common import norm_code, output_root
from src.strategies.base import StrategyAdapter, StrategyResult

class _NoInput(io.StringIO):
    def readline(self, *a, **kw): return "\n"

class _ReconfigurableStringIO(io.StringIO):
    def reconfigure(self, *a, **kw): return None

class V1Adapter(StrategyAdapter):
    def __init__(self, screener_dir: Path, top_n: int = 10):
        super().__init__(name="v1", display_name="V1", top_n=top_n)
        self._dir = Path(screener_dir)
        self._script = self._dir / "screener_app.py"

    def validate_environment(self) -> bool:
        return self._script.exists()

    def run(self, snap: Path, cfg: Dict[str, Any]) -> StrategyResult:
        t0 = time.perf_counter()
        r = StrategyResult(strategy_name=self.name, display_name=self.display_name, top_n=self.top_n)
        try:
            spec = importlib.util.spec_from_file_location("v1scr", self._script)
            if not spec or not spec.loader: r.error="V1: spec failed"; return r
            mod = importlib.util.module_from_spec(spec)
            sys.modules["v1scr"] = mod
            old_in, old_out = sys.stdin, sys.stdout
            sys.stdin = _NoInput(); sys.stdout = _ReconfigurableStringIO()
            try: spec.loader.exec_module(mod)
            finally: sys.stdin = old_in; sys.stdout = old_out

            # Patch data path + rebuild (exact V1.0 match)
            mod.LOCAL_DATA_PATH = str(snap)
            if hasattr(mod, "StockIndex"): mod._stock_index = mod.StockIndex(str(snap))
            if hasattr(mod, "KlineCache"): mod._kline_cache = mod.KlineCache(max_size=2000)
            if hasattr(mod, "OUTPUT_PATH"): mod.OUTPUT_PATH = str(output_root(cfg) / "csv" / "v1_raw")

            results = mod.scan_market(limit=max(self.top_n, 50))
            top = []
            for i, item in enumerate(results[:self.top_n], 1):
                meta = item.get("meta", {}) or {}
                tp = item.get("top_positive") or {}
                tn = item.get("top_negative") or {}
                code = str(item.get("code", "")).replace(".txt","").strip()
                top.append({
                    "rank": i, "code": norm_code(code), "name": meta.get("name",""),
                    "price": round(float(meta.get("price",0) or 0), 2),
                    "pct_chg": round(float(meta.get("pct_chg",0) or 0), 2),
                    "positive_count": item.get("positive_count",0),
                    "negative_count": item.get("negative_count",0),
                    "top_lu1_rate": tp.get("lu1_rate",""),
                    "top_lu5_rate": tp.get("lu5_rate",""),
                    "top_rule": tp.get("label",""),
                    "top_negative_lu1_rate": tn.get("lu1_rate",""),
                    "tag": "",
                })
            r.top = self._filter(top)
            if not r.top: r.error = "V1: no results"
        except Exception as e: r.error = f"V1: {e}"
        r.elapsed_seconds = round(time.perf_counter() - t0, 3)
        return r

    def _filter(self, rows: List[Dict]) -> List[Dict]:
        out = []
        for row in rows:
            if not row.get("code"): continue
            if float(row.get("price",0) or 0) <= 0: continue
            if float(row.get("pct_chg",0) or 0) <= -99: continue
            out.append(row)
        return out
