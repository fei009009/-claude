"""XGB 个股诊断引擎 — 多目标概率 + Beam 规则匹配 + 风险标记"""
from __future__ import annotations
import importlib.util
import json
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

try:
    import xgboost as xgb

    _HAS_XGB = True
except ImportError:
    _HAS_XGB = False


class DiagnosisResult:
    """个股诊断结果"""

    def __init__(self, code: str, name: str = ""):
        self.code = code
        self.name = name
        self.model_score: float = 0.0
        self.rule_score: float = 0.0
        self.blended_score: float = 0.0
        self.target_scores: Dict[str, float] = {}
        self.matched_rules: Dict[str, List[Dict]] = {}
        self.risk_flags: List[str] = []
        self.signal: str = "SKIP"
        self.recommendation: str = ""
        self.diagnosis_quality: Dict[str, Any] = {}
        self.best_rule: Dict[str, Any] = {}
        self.extra: Dict[str, Any] = {}

    def to_dict(self) -> Dict[str, Any]:
        return {
            "code": self.code,
            "name": self.name,
            "model_score": round(self.model_score, 4),
            "rule_score": round(self.rule_score, 4),
            "blended_score": round(self.blended_score, 4),
            "target_scores": self.target_scores,
            "matched_rule_count": self._matched_rule_count(),
            "risk_flags": self.risk_flags,
            "signal": self.signal,
            "recommendation": self.recommendation,
            "diagnosis_quality": self.diagnosis_quality,
            "best_rule": self.best_rule,
            "extra": self.extra,
        }

    def _matched_rule_count(self) -> int:
        total = 0
        for value in self.matched_rules.values():
            if isinstance(value, dict):
                total += int(value.get("matched", 0) or 0)
            elif isinstance(value, list):
                total += len(value)
        return total


class DiagnosisEngine:
    """XGB + Beam 规则混合诊断引擎

    融合 XGB 实时模型预测和 XGBZX Beam 规则匹配，对候选股深度验证。

    使用 XGB realtime bridge (realtime_xgb.py) 的 load_model_and_indicators
    进行模型评分，结合 xgb_bin_model/rules/ 的 Beam 规则进行规则匹配。
    """

    TARGET_WEIGHTS = {
        "y_high_5d_5pct": 0.42,
        "y_close_5d_5pct": 0.26,
        "y_next_5pct": 0.18,
        "y_close_5d_0pct": 0.14,
    }

    SIGNAL_THRESHOLDS = [
        ("STRONG_BUY", 0.80),
        ("BUY", 0.60),
        ("WATCH", 0.40),
    ]

    def __init__(
        self,
        xgb_dir: Path,
        xgbzx_dir: Path,
        data_dir: Optional[Path] = None,
        x1_dir: Optional[Path] = None,
    ):
        self.xgb_dir = Path(xgb_dir)
        self.xgbzx_dir = Path(xgbzx_dir)
        self.data_dir = Path(data_dir) if data_dir else self.xgbzx_dir / "data"
        self.x1_dir = Path(x1_dir) if x1_dir else None
        self._bridge_module = None
        self._xgb_model = None
        self._feature_names = None
        self._calc_indicators = None
        self._model_cfg = None
        self._loaded = False
        self._rules_cache: Optional[Dict[str, Dict[str, Any]]] = None
        self._x1_indicator_module = None
        self._x1_config: Optional[Dict[str, Any]] = None

    def validate_environment(self) -> bool:
        rt_bridge = self.xgb_dir / "xgb_realtime_bridge" / "realtime_xgb.py"
        model = self.xgb_dir / "model_v2" / "xgb_5d_v2.json"
        names = self.xgb_dir / "model_v2" / "names_v2.pkl"
        return rt_bridge.exists() and model.exists() and names.exists()

    def load_models(self) -> bool:
        """加载 XGB 模型和指标计算模块

        Returns:
            bool: 是否成功加载
        """
        if self._loaded:
            return True

        try:
            bridge_path = self.xgb_dir / "xgb_realtime_bridge" / "realtime_xgb.py"
            if not bridge_path.exists():
                print(f"[DiagnosisEngine] Bridge not found: {bridge_path}")
                return False

            bridge_dir = str(bridge_path.parent)
            if bridge_dir not in sys.path:
                sys.path.insert(0, bridge_dir)

            module_name = f"fczs_diag_v2_{abs(hash(bridge_dir)) % 100000}"
            spec = importlib.util.spec_from_file_location(module_name, bridge_path)
            if spec is None or spec.loader is None:
                print("[DiagnosisEngine] Cannot load bridge spec")
                return False

            self._bridge_module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = self._bridge_module
            spec.loader.exec_module(self._bridge_module)

            # 构建 config dict 供 load_model_and_indicators 使用
            config_path = self.xgb_dir / "xgb_realtime_bridge" / "config.json"
            if config_path.exists():
                cfg = json.loads(config_path.read_text(encoding="utf-8"))
            else:
                cfg = {
                    "paths": {
                        "xgb_dir": str(self.xgb_dir),
                        "data_dir": str(self.data_dir),
                        "output_dir": str(self.xgb_dir / "xgb_realtime_bridge" / "reports"),
                    }
                }

            # The copied XGB config can still contain source-project paths.
            # V2.0 should always load models/reports from its own vendor mirror
            # and score against the active snapshot passed by the pipeline.
            cfg.setdefault("paths", {})
            cfg["paths"]["xgb_dir"] = str(self.xgb_dir)
            cfg["paths"]["data_dir"] = str(self.data_dir)
            cfg["paths"]["output_dir"] = str(self.xgb_dir / "xgb_realtime_bridge" / "reports")

            model, names, calc_fn, model_cfg = self._bridge_module.load_model_and_indicators(cfg)
            self._xgb_model = model
            self._feature_names = names
            self._calc_indicators = calc_fn
            self._model_cfg = model_cfg
            self._loaded = True

            print(f"[DiagnosisEngine] XGB model loaded: {len(names)} features, "
                  f"model_type={model_cfg.get('model_type', 'xgb')}")
            return True
        except Exception as e:
            print(f"[DiagnosisEngine] Failed to load XGB bridge: {e}")
            import traceback
            traceback.print_exc()
            return False

    def diagnose_candidates(
        self,
        candidates: List[Dict[str, str]],
        snapshot_dir: Optional[Path] = None,
    ) -> List[DiagnosisResult]:
        """对候选股池逐只诊断

        Args:
            candidates: [{"code": "SH600000", "name": "浦发银行"}, ...]
            snapshot_dir: 快照目录（用于读取日线数据）

        Returns:
            按 blended_score 降序排列
        """
        started = time.perf_counter()
        if not self._loaded and not self.load_models():
            print("[DiagnosisEngine] Models not loaded, returning empty results")
            return []

        # 批量预缓存历史数据
        history_cache: Dict[str, pd.DataFrame] = {}
        search_dirs = []
        if snapshot_dir:
            search_dirs.append(snapshot_dir)
        if self.data_dir:
            search_dirs.append(self.data_dir)

        for c in candidates:
            market, raw_code = self._parse_code(c["code"])
            full_code = f"{market.upper()}#{raw_code}"
            for d in search_dirs:
                path = d / f"{full_code}.txt"
                if path.exists():
                    try:
                        df = self._read_tdx_history(path)
                        if df is not None and len(df) >= 60:
                            history_cache[full_code] = df
                            break
                    except Exception:
                        continue

        if not history_cache:
            print("[DiagnosisEngine] No valid history data for any candidate")
            return []

        print(f"[DiagnosisEngine] Cached history for {len(history_cache)}/{len(candidates)} candidates")

        results = []
        for c in candidates:
            diag = DiagnosisResult(c["code"], c.get("name", ""))
            market, raw_code = self._parse_code(c["code"])
            full_code = f"{market.upper()}#{raw_code}"
            df = history_cache.get(full_code)

            # 1. XGB 模型评分
            if df is not None:
                try:
                    diag.model_score = self._xgb_model_score_from_df(df, full_code)
                except Exception as exc:
                    print(f"[DiagnosisEngine] Model score error for {c['code']}: {exc}")

            # 2. Beam 规则匹配
            try:
                rule_result = self._rule_match(c["code"], df, full_code)
                diag.rule_score = rule_result.get("blended", 0.0)
                diag.matched_rules = rule_result.get("details", {})
                diag.target_scores = rule_result.get("by_target", {})
                diag.best_rule = rule_result.get("best_rule", {})
                diag.diagnosis_quality = rule_result.get("quality", {})
            except Exception as exc:
                print(f"[DiagnosisEngine] Rule match error for {c['code']}: {exc}")

            # 3. 混合评分 (72/28)
            if diag.model_score > 0:
                diag.blended_score = diag.model_score * 0.72 + diag.rule_score * 0.28
            else:
                diag.blended_score = diag.rule_score

            # 4. 风险标记
            diag.risk_flags = self._assess_risks(c["code"], diag)

            # 5. 信号判定：阈值从高到低
            for signal, threshold in self.SIGNAL_THRESHOLDS:
                if diag.blended_score >= threshold:
                    diag.signal = signal
                    break
            else:
                diag.signal = "NEUTRAL"

            diag.recommendation = self._build_recommendation(diag)
            results.append(diag)

        elapsed = round(time.perf_counter() - started, 3)
        print(f"[DiagnosisEngine] Diagnosed {len(results)} candidates in {elapsed}s")
        return sorted(results, key=lambda x: -x.blended_score)

    def _xgb_model_score_from_df(self, df: pd.DataFrame, full_code: str) -> float:
        """从已缓存的 DataFrame 计算 XGB 模型评分"""
        if not self._loaded:
            return 0.0
        try:
            feature_vector = self._build_feature_vector(df, full_code)
            if not feature_vector or len(feature_vector) != len(self._feature_names):
                return 0.0

            if not _HAS_XGB:
                return 0.0

            dmatrix = xgb.DMatrix(
                np.array(feature_vector, dtype=np.float32).reshape(1, -1),
                feature_names=self._feature_names,
            )
            raw_pred = float(self._xgb_model.predict(dmatrix)[0])
            prob = 1.0 / (1.0 + np.exp(-raw_pred))
            return round(max(0.0, min(1.0, prob)), 4)
        except Exception as exc:
            print(f"[DiagnosisEngine] Score error for {full_code}: {exc}")
            return 0.0

    def _build_feature_vector(self, df: pd.DataFrame, full_code: str) -> List[float]:
        """构建特征向量

        优先使用 realtime_xgb 的 calc_indicators；fallback 用内置简化计算
        """
        try:
            if self._calc_indicators and self._model_cfg:
                close = df["close"].values
                high = df["high"].values
                low = df["low"].values
                volume = df["volume"].values
                amount = df["amount"].values

                indicators = self._calc_indicators(
                    close=close, high=high, low=low,
                    volume=volume, amount=amount,
                )
                # indicators 通常返回 DataFrame 或 Dict
                if isinstance(indicators, dict):
                    features = [float(indicators.get(name, 0.0) or 0.0)
                                for name in self._feature_names]
                    return features
                elif hasattr(indicators, "iloc"):
                    last_row = indicators.iloc[-1]
                    features = [float(last_row.get(name, 0.0) or 0.0)
                                for name in self._feature_names
                                if name in last_row]
                    return features
        except Exception:
            pass

        # Fallback: 简化的特征提取
        return self._fallback_features(df)

    def _fallback_features(self, df: pd.DataFrame) -> List[float]:
        """简化的 fallback 特征计算（当 calc_indicators 不可用时）"""
        features = []
        close = df["close"].values
        volume = df["volume"].values

        try:
            pct_chg_1d = (close[-1] / close[-2] - 1) if len(close) >= 2 else 0
            pct_chg_5d = (close[-1] / close[-6] - 1) if len(close) >= 6 else 0
            pct_chg_20d = (close[-1] / close[-21] - 1) if len(close) >= 21 else 0

            vol_ratio_5 = volume[-5:].mean() / max(volume[-10:].mean(), 1) if len(volume) >= 10 else 1.0

            features.extend([
                float(pct_chg_1d), float(pct_chg_5d), float(pct_chg_20d),
                float(vol_ratio_5),
            ])

            # 填充到模型需要的特征数
            required = len(self._feature_names) if self._feature_names else 40
            while len(features) < required:
                features.append(0.0)
            return features[:required]
        except Exception:
            return [0.0] * len(self._feature_names) if self._feature_names else [0.0]

    def _read_tdx_history(self, path: Path) -> Optional[pd.DataFrame]:
        """读取通达信 TXT 历史数据"""
        try:
            df = pd.read_csv(
                path,
                sep="\t",
                encoding="gbk",
                skiprows=2,
                comment="#",
                names=["date", "open", "high", "low", "close", "volume", "amount"],
                dtype={
                    "open": "float64", "high": "float64", "low": "float64",
                    "close": "float64", "volume": "float64", "amount": "float64",
                },
                na_filter=False,
                engine="c",
            )
            df["date"] = pd.to_datetime(df["date"], errors="coerce")
            df = df.dropna(subset=["date"])
            df = df.sort_values("date").reset_index(drop=True)
            return df
        except Exception:
            return None

    def _load_rules(self) -> Dict[str, Any]:
        """一次性加载所有 Beam 规则，缓存结果"""
        if self._rules_cache is not None:
            return self._rules_cache

        rules_dir = self.xgb_dir / "xgb_bin_model" / "rules"
        target_map = {
            "y_close_5d_5pct": "y_close_5d_5pct",
            "y_close_5d_0pct": "y_close_5d_0pct",
            "y_high_5d_5pct": "y_high_5d_5pct",
            "y_next_5pct": "y_next_5pct",
        }

        loaded = {}
        for target_file, target_key in target_map.items():
            rule_file = rules_dir / f"{target_file}_merged.json"
            if not rule_file.exists():
                loaded[target_key] = {"combos": [], "count": 0}
                continue
            try:
                rules_data = json.loads(rule_file.read_text(encoding="utf-8"))
                combos = rules_data.get("combos", [])
                loaded[target_key] = {
                    "combos": combos,
                    "count": len(combos),
                }
            except Exception:
                loaded[target_key] = {"combos": [], "count": 0}

        self._rules_cache = loaded
        return loaded

    def _rule_match(self, code: str, df: Optional[pd.DataFrame], full_code: str) -> Dict[str, Any]:
        """Match current X1-style binned features against local Beam rules."""
        rules = self._load_rules()
        scores: Dict[str, float] = {}
        details: Dict[str, Any] = {}
        best_rule: Dict[str, Any] = {}
        binned = self._x1_binned_features(df, full_code) if df is not None else {}
        if not binned:
            return {
                "by_target": {target: 0.0 for target in rules},
                "details": {},
                "blended": 0.0,
                "quality": {"rule_match_ready": False, "reason": "x1_binning_unavailable"},
            }

        for target_key, data in rules.items():
            combos = data.get("combos", []) or []
            matched: List[Dict[str, Any]] = []
            for combo in combos:
                path_items = self._combo_path_items(combo)
                if not path_items:
                    continue
                if all(int(binned.get(name, -999)) == int(value) for name, value in path_items):
                    matched.append(combo)

            if matched:
                matched.sort(key=lambda item: (
                    -float(item.get("wr", 0) or 0),
                    -float(item.get("lift", 0) or 0),
                    -int(float(item.get("n", 0) or 0)),
                ))
                best = matched[0]
                score = float(best.get("wr", 0) or 0)
                if score > 1:
                    score /= 100.0
                scores[target_key] = max(0.0, min(score, 1.0))
                detail = {
                    "total_combos": len(combos),
                    "matched": len(matched),
                    "best_wr": round(scores[target_key], 4),
                    "best_lift": round(float(best.get("lift", 0) or 0), 4),
                    "best_n": int(float(best.get("n", 0) or 0)),
                    "best_path": self._path_text(self._combo_path_items(best)),
                }
                details[target_key] = detail
                if not best_rule or scores[target_key] > float(best_rule.get("wr", 0) or 0):
                    best_rule = {"target": target_key, "wr": scores[target_key], **detail}
            else:
                scores[target_key] = 0.0
                details[target_key] = {"total_combos": len(combos), "matched": 0, "best_wr": 0.0}

        blended = sum(scores.get(k, 0.0) * self.TARGET_WEIGHTS.get(k, 0.0) for k in scores)
        return {
            "by_target": {k: round(v, 4) for k, v in scores.items()},
            "details": details,
            "best_rule": best_rule,
            "blended": round(blended, 4),
            "quality": {"rule_match_ready": True, "binned_feature_count": len(binned)},
        }

    def _x1_binned_features(self, df: pd.DataFrame, full_code: str) -> Dict[str, int]:
        cfg = self._load_x1_config()
        mod = self._x1_indicator_module
        if not cfg or mod is None or df is None or len(df) < int(cfg.get("min_history", 60)):
            return {}
        try:
            o = df["open"].to_numpy(dtype="float64")
            h = df["high"].to_numpy(dtype="float64")
            l = df["low"].to_numpy(dtype="float64")
            c = df["close"].to_numpy(dtype="float64")
            v = df["volume"].to_numpy(dtype="float64")
            indicators = mod.calc_indicators(o, h, l, c, v, full_code)
            binned: Dict[str, int] = {}
            for name in cfg["continuous_indicators"]:
                val = indicators.get(name)
                if val is None:
                    return {}
                binned[name] = int(np.digitize(float(val[-1]), bins=cfg["bin_edges"]) + 1)
            for name in cfg["binary_indicators"]:
                val = indicators.get(name)
                if val is None:
                    return {}
                binned[name] = int(np.clip(np.round(float(val[-1])), 0, 1))
            return binned
        except Exception as exc:
            print(f"[DiagnosisEngine] X1 binning error for {full_code}: {exc}")
            return {}

    def _load_x1_config(self) -> Dict[str, Any]:
        if self._x1_config is not None:
            return self._x1_config
        x1_dir = self.x1_dir
        if not x1_dir or not x1_dir.exists():
            self._x1_config = {}
            return self._x1_config
        config_path = x1_dir / "config.py"
        if not config_path.exists():
            self._x1_config = {}
            return self._x1_config
        try:
            if str(x1_dir) not in sys.path:
                sys.path.insert(0, str(x1_dir))
            spec = importlib.util.spec_from_file_location("fczs_x1_config_for_diag", config_path)
            if spec is None or spec.loader is None:
                self._x1_config = {}
                return self._x1_config
            cfg_mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(cfg_mod)
            ind_path = Path(str(getattr(cfg_mod, "INDICATOR_MODULE_PATH")))
            ind_spec = importlib.util.spec_from_file_location("fczs_x1_indicator_for_diag", ind_path)
            if ind_spec is None or ind_spec.loader is None:
                self._x1_config = {}
                return self._x1_config
            ind_mod = importlib.util.module_from_spec(ind_spec)
            ind_spec.loader.exec_module(ind_mod)
            self._x1_indicator_module = ind_mod
            self._x1_config = {
                "continuous_indicators": list(getattr(cfg_mod, "CONTINUOUS_INDICATORS", [])),
                "binary_indicators": list(getattr(cfg_mod, "BINARY_INDICATORS", [])),
                "bin_edges": list(getattr(cfg_mod, "BIN_EDGES", [20, 40, 60, 80])),
                "min_history": int(getattr(cfg_mod, "MIN_HISTORY", 60)),
            }
            return self._x1_config
        except Exception as exc:
            print(f"[DiagnosisEngine] X1 config load error: {exc}")
            self._x1_config = {}
            return self._x1_config

    @staticmethod
    def _combo_path_items(combo: Dict[str, Any]) -> List[Tuple[str, int]]:
        raw = combo.get("path") or combo.get("conditions") or []
        if isinstance(raw, dict):
            items = list(raw.items())
        else:
            items = list(raw)
        out: List[Tuple[str, int]] = []
        for item in items:
            if not isinstance(item, (list, tuple)) or len(item) < 2:
                continue
            try:
                out.append((str(item[0]), int(item[1])))
            except Exception:
                continue
        return out

    @staticmethod
    def _path_text(items: List[Tuple[str, int]]) -> str:
        return " -> ".join(f"{name}={value}" for name, value in items)[:240]

    def _assess_risks(self, code: str, diag: DiagnosisResult) -> List[str]:
        """评估风险标记"""
        flags = []
        market, raw_code = self._parse_code(code)

        if raw_code.startswith(("300", "688")):
            flags.append("创业板/科创板波动风险")

        if diag.model_score < 0.35:
            flags.append("XGB模型评分偏低")

        if diag.rule_score < 0.10:
            flags.append("Beam规则覆盖不足")

        if diag.blended_score > 0 and diag.model_score == 0:
            flags.append("仅规则评分无模型验证")

        return flags

    def _build_recommendation(self, diag: DiagnosisResult) -> str:
        if diag.signal == "STRONG_BUY":
            return (f"{diag.code}{' ' + diag.name if diag.name else ''} "
                    f"XGB双重确认STRONG_BUY，评分{diag.blended_score:.1%}")
        elif diag.signal == "BUY":
            return (f"{diag.code}{' ' + diag.name if diag.name else ''} "
                    f"XGB确认BUY，评分{diag.blended_score:.1%}")
        elif diag.signal == "WATCH":
            return f"{diag.code} 关注观察，评分{diag.blended_score:.1%}"
        return f"{diag.code} 暂不建议"

    @staticmethod
    def _parse_code(code: str) -> Tuple[str, str]:
        """解析代码为 (market, raw_code)，如 'SH600000' -> ('sh', '600000')"""
        code = code.strip().upper().replace("#", "").replace(".", "")
        if code.startswith(("SH", "SZ", "BJ")):
            return code[:2].lower(), code[2:]
        digits = re.sub(r"\D", "", code)[-6:]
        if digits.startswith(("6", "9")):
            return "sh", digits
        elif digits.startswith(("8", "4")):
            return "bj", digits
        return "sz", digits
