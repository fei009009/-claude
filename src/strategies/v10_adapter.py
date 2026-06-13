"""V10 VIP strategy adapter: P80 + Lift rules."""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

from src.common import latest_file, norm_code, pick, read_csv_rows, repair_mojibake, safe_float, safe_int
from src.strategies.base import StrategyAdapter, StrategyResult


class V10Adapter(StrategyAdapter):
    def __init__(self, screener_dir: Path, top_n: int = 10):
        super().__init__(name="v10", display_name="V10 VIP", top_n=top_n)
        self.screener_dir = Path(screener_dir)
        self._entry_script = self.screener_dir / "screener_vip.py"

    def validate_environment(self) -> bool:
        required = [
            self._entry_script,
            self.screener_dir / "screener_data.json",
            self.screener_dir / "binning_stats_v3.json",
            self.screener_dir / "neg_rules_3ind_core22.json",
            self.screener_dir / "calc_indicators_nb.py",
        ]
        return all(path.exists() for path in required)

    def run(self, snapshot_dir: Path, cfg: Dict[str, Any]) -> StrategyResult:
        started = time.perf_counter()
        result = StrategyResult(strategy_name=self.name, display_name=self.display_name, top_n=self.top_n)
        if not self.validate_environment():
            result.error = f"V10 environment missing: {self.screener_dir}"
            return result
        try:
            before_mtime = self._latest_output_mtime()
            cp = subprocess.run(
                [sys.executable, str(self._entry_script), "--data-dir", str(snapshot_dir)],
                capture_output=True,
                text=True,
                timeout=int(cfg.get("strategies", {}).get("v10", {}).get("timeout_seconds", 120)),
                cwd=str(self.screener_dir),
            )
            csv_path = latest_file(self.screener_dir, ["vip_result_*.csv", "vip_v10_result_*.csv"])
            if not csv_path:
                result.error = (cp.stderr or cp.stdout or "V10 produced no CSV")[:500]
                return result
            if before_mtime and csv_path.stat().st_mtime <= before_mtime and cp.returncode != 0:
                result.error = (cp.stderr or "V10 failed before producing fresh CSV")[:500]
                return result
            rows, encoding = read_csv_rows(csv_path)
            top_rows: List[Dict[str, Any]] = []
            total_final = 0
            for row in rows:
                if str(row.get("IsFinal", "1")).strip() not in ("", "1", "True", "true"):
                    continue
                total_final += 1
                code = norm_code(pick(row, ["Code", "code", "代码"]))
                if not code:
                    continue
                top_rows.append(
                    {
                        "rank": len(top_rows) + 1,
                        "code": code,
                        "name": repair_mojibake(pick(row, ["Name", "name", "名称"])).strip(),
                        "price": round(safe_float(pick(row, ["Price", "price", "现价", "价格"])), 2),
                        "pct_chg": round(safe_float(pick(row, ["Return%", "pct_chg", "涨幅%", "涨幅"])), 2),
                        "positive_count": safe_int(pick(row, ["PosCount", "positive_count", "正匹配数"])),
                        "negative_count": safe_int(pick(row, ["NegCount", "negative_count", "负匹配数"])),
                        "top_lu1_rate": round(safe_float(pick(row, ["TopLU1%", "top_lu1_rate", "最高lu1%"])), 2),
                        "top_rule": str(pick(row, ["TopRule", "top_rule", "TOP1规则"])).strip(),
                        "lift_score": round(safe_float(pick(row, ["LiftScore", "lift_score"])), 4),
                        "p80_count": safe_int(pick(row, ["P80Count", "p80_count"])),
                        "in_group_b": safe_int(pick(row, ["InGroupB"])),
                        "in_group_c": safe_int(pick(row, ["InGroupC"])),
                        "wr28": round(safe_float(pick(row, ["wr28"])), 2),
                        "yz016": round(safe_float(pick(row, ["yz016"])), 2),
                        "j": round(safe_float(pick(row, ["j"])), 2),
                        "rsi24": round(safe_float(pick(row, ["rsi24"])), 2),
                        "dc_p20": round(safe_float(pick(row, ["dc_p20"])), 2),
                    }
                )
                if len(top_rows) >= self.top_n:
                    break
            result.top = top_rows
            result.quality_fields = {
                "csv_path": str(csv_path),
                "csv_encoding": encoding,
                "csv_rows": len(rows),
                "final_rows": total_final,
                "returncode": cp.returncode,
            }
            result.metadata = {"stdout_tail": (cp.stdout or "")[-1000:], "stderr_tail": (cp.stderr or "")[-1000:]}
            if not result.top:
                result.error = (cp.stderr or cp.stdout or "V10 produced no final rows")[:500]
        except subprocess.TimeoutExpired:
            result.error = "V10 timeout"
        except Exception as exc:
            result.error = f"V10 exception: {exc}"
        finally:
            result.elapsed_seconds = round(time.perf_counter() - started, 3)
        return result

    def _latest_output_mtime(self) -> float | None:
        path = latest_file(self.screener_dir, ["vip_result_*.csv", "vip_v10_result_*.csv"])
        return path.stat().st_mtime if path else None
