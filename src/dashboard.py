"""Small Web dashboard server for V2.0."""
from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "static"
PYTHON = sys.executable

_job_lock = threading.Lock()
_job_status: Dict[str, Any] = {
    "running": False,
    "kind": "",
    "stage": "idle",
    "message": "空闲",
    "started_at": None,
    "returncode": None,
    "log_path": "",
}
_quality_cache: Dict[str, Any] = {"expires": 0.0, "snapshot_dir": "", "source": "", "quality": None}
_name_map_cache: Dict[str, Any] = {"key": "", "expires": 0.0, "map": {}}


def _json_bytes(data: Any) -> bytes:
    return json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")


def _json_dir() -> Path:
    path = ROOT / "outputs" / "json"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _logs_dir() -> Path:
    path = ROOT / "outputs" / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _load_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _latest_file(pattern: str) -> Optional[Path]:
    files = sorted(_json_dir().glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0] if files else None


def _load_latest_pipeline() -> Optional[Dict[str, Any]]:
    path = _latest_file("pipeline_v2_*.json")
    if not path:
        return None
    data = _load_json(path)
    if data is not None:
        data["_file"] = path.name
        data["_mtime"] = datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds")
    return data


def _iter_candidate_codes(pipeline: Dict[str, Any]) -> set[str]:
    from src.common import norm_code

    codes: set[str] = set()

    def add(value: Any) -> None:
        code = norm_code(value)
        if code:
            codes.add(code)

    for result in pipeline.get("strategies", []) or []:
        for row in result.get("top", []) or []:
            add(row.get("code"))
    for item in (pipeline.get("overlap") or {}).get("overlaps", []) or []:
        add(item.get("code"))
    for item in pipeline.get("diagnosis_results", []) or []:
        add(item.get("code"))
    diag = pipeline.get("diagnosis") or {}
    for key in ("results", "top_picks", "watch_list"):
        for item in diag.get(key, []) or []:
            add(item.get("code"))
    boundary = pipeline.get("boundary") or {}
    for key in ("risks", "candidates", "critical"):
        for item in boundary.get(key, []) or []:
            add(item.get("code"))
    return codes


def _snapshot_file_candidates(snapshot_dir: Path, code: str) -> list[Path]:
    from src.common import norm_code

    normalized = norm_code(code)
    digits = normalized[-6:] if len(normalized) >= 6 else str(code)[-6:]
    market = normalized[:2] if normalized[:2] in {"SH", "SZ", "BJ"} else ""
    markets = [market] if market else []
    markets += [item for item in ("SH", "SZ", "BJ") if item not in markets]
    paths: list[Path] = []
    for prefix in markets:
        paths.extend([
            snapshot_dir / f"{prefix}#{digits}.txt",
            snapshot_dir / f"{prefix}{digits}.txt",
            snapshot_dir / f"{digits}.{prefix}.txt",
        ])
    return paths


def _read_snapshot_stock_name(path: Path) -> str:
    from src.common import repair_mojibake

    for encoding in ("utf-8-sig", "utf-8", "gb18030", "gbk"):
        try:
            first_line = path.read_text(encoding=encoding, errors="strict").splitlines()[0]
        except Exception:
            continue
        parts = first_line.strip().split()
        if len(parts) >= 2:
            return repair_mojibake(parts[1]).strip()
    return ""


def _build_name_map(snapshot_dir: Path, pipeline: Dict[str, Any]) -> Dict[str, str]:
    from src.common import norm_code, repair_mojibake

    codes = _iter_candidate_codes(pipeline)
    cache_key = f"{Path(snapshot_dir).resolve()}|{','.join(sorted(codes))}"
    now_ts = time.time()
    if _name_map_cache.get("key") == cache_key and float(_name_map_cache.get("expires", 0) or 0) > now_ts:
        return dict(_name_map_cache.get("map") or {})

    name_map: Dict[str, str] = {}
    for result in pipeline.get("strategies", []) or []:
        for row in result.get("top", []) or []:
            code = norm_code(row.get("code"))
            name = repair_mojibake(row.get("name", "")).strip()
            if code and name:
                name_map.setdefault(code, name)
    for item in (pipeline.get("overlap") or {}).get("overlaps", []) or []:
        code = norm_code(item.get("code"))
        name = repair_mojibake(item.get("name", "")).strip()
        if code and name:
            name_map.setdefault(code, name)

    for code in codes:
        for path in _snapshot_file_candidates(snapshot_dir, code):
            if not path.exists():
                continue
            name = _read_snapshot_stock_name(path)
            if name:
                name_map[code] = name
                break

    _name_map_cache.update({"key": cache_key, "expires": now_ts + 60, "map": dict(name_map)})
    return name_map


def _display_stock_name(code: Any, name: Any, name_map: Dict[str, str]) -> str:
    from src.common import norm_code, repair_mojibake

    normalized = norm_code(code)
    mapped = name_map.get(normalized, "")
    if mapped:
        return mapped
    return repair_mojibake(name).strip()


def _clean_diagnosis_item(item: Dict[str, Any], name_map: Dict[str, str]) -> Dict[str, Any]:
    cleaned = dict(item)
    cleaned["name"] = _display_stock_name(cleaned.get("code"), cleaned.get("name", ""), name_map)
    extra = cleaned.get("extra") if isinstance(cleaned.get("extra"), dict) else {}
    rich_report = cleaned.get("rich_report") or extra.get("rich_report")
    if isinstance(rich_report, dict) and rich_report:
        cleaned["rich_report"] = rich_report
        cleaned.setdefault("headline", rich_report.get("headline", ""))
        cleaned.setdefault("probability_text", rich_report.get("probability_text", ""))
        cleaned.setdefault("pattern_text", rich_report.get("pattern_text", ""))
        cleaned.setdefault("veto_text", rich_report.get("veto_text", ""))
        cleaned.setdefault("final_view", rich_report.get("final_view", ""))
        cleaned.setdefault("markdown_report", rich_report.get("markdown", ""))
        cleaned.setdefault("report_path", rich_report.get("markdown") or rich_report.get("json") or "")
        cleaned.setdefault("diagnosis_compact", rich_report.get("headline") or rich_report.get("final_view") or "")
    events = extra.get("event_probabilities")
    if isinstance(events, dict) and events:
        cleaned.setdefault("event_probabilities", events)
    pattern = extra.get("pattern")
    if isinstance(pattern, dict) and pattern:
        cleaned.setdefault("pattern", pattern.get("label", ""))
    if not cleaned.get("diagnosis_badge") and cleaned.get("signal"):
        labels = {
            "STRONG_BUY": "强确认",
            "BUY": "确认",
            "WATCH": "观察",
            "NEUTRAL": "中性",
            "SKIP": "跳过",
        }
        label = labels.get(str(cleaned.get("signal")), str(cleaned.get("signal")))
        try:
            score = f"{float(cleaned.get('blended_score')):.0%}"
        except Exception:
            score = ""
        cleaned["diagnosis_badge"] = f"XGB{label}{score}"
    return cleaned


def _clean_diagnosis_summary_text(summary: str, items: list[Dict[str, Any]], name_map: Dict[str, str]) -> str:
    text = str(summary or "")
    if not text:
        return ""
    for item in items:
        code = str(item.get("code") or "")
        old_name = str(item.get("name") or "")
        new_name = _display_stock_name(code, old_name, name_map)
        if code and old_name and new_name and old_name != new_name:
            text = text.replace(f"{code} {old_name}", f"{code} {new_name}")
            text = text.replace(old_name, new_name)
    return text


def _clean_boundary(boundary: Dict[str, Any], name_map: Dict[str, str]) -> Dict[str, Any]:
    cleaned = {"stats": boundary.get("stats", {})}
    for key in ("risks", "candidates"):
        rows = []
        for item in boundary.get(key, []) or []:
            row = dict(item)
            row["name"] = _display_stock_name(row.get("code"), row.get("name", ""), name_map)
            rows.append(row)
        cleaned[key] = rows
    return cleaned


def _latest_factor_panel() -> Dict[str, Any]:
    from src.common import repair_mojibake

    factor_dir = ROOT / "outputs" / "factors"
    files = sorted(factor_dir.glob("candidate_factor_panel_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        return {"file": "", "mtime": "", "row_count": 0, "top": []}
    path = files[0]
    data = _load_json(path) or {}
    rows = data.get("rows") or []
    try:
        rows = sorted(rows, key=lambda row: float(row.get("preliminary_score") or 0), reverse=True)
    except Exception:
        pass
    top = []
    for row in rows[:10]:
        top.append({
            "code": row.get("code", ""),
            "name": repair_mojibake(row.get("name", "")),
            "selection_layer": row.get("selection_layer", ""),
            "strategy_count": row.get("strategy_count", 0),
            "preliminary_score": row.get("preliminary_score", 0),
            "diagnosis_badge": row.get("diagnosis_badge", ""),
            "factor_notes": row.get("factor_notes", []),
        })
    return {
        "file": path.name,
        "mtime": datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds"),
        "row_count": data.get("row_count", len(rows)),
        "top": top,
    }


def _strategy_display_state(result: Dict[str, Any]) -> Dict[str, str]:
    key = str(result.get("strategy_name", "")).lower()
    error = str(result.get("error") or "")
    metadata = result.get("metadata") or {}
    cache_mode = str(metadata.get("cache_mode") or "")
    if result.get("ok"):
        return {"kind": "ok", "label": "OK"}
    if key == "x1beam" and (
        "no complete preheated cache" in error
        or "missing_preheated_cache" in cache_mode
        or "incomplete cache" in error
    ):
        return {"kind": "warn", "label": "待预热"}
    return {"kind": "fail", "label": "FAIL"}


def _strategy_display_error(result: Dict[str, Any], state: Dict[str, str]) -> str:
    key = str(result.get("strategy_name", "")).lower()
    if key == "x1beam" and state.get("kind") == "warn":
        return "当前快照没有完整 X1Beam 预热缓存；尾盘前预热完成后，X1Beam 会作为第四个对等策略参与交集。"
    return str(result.get("error") or "")


def _tracking_status(cfg: Dict[str, Any]) -> Dict[str, Any]:
    try:
        from src.tracking_store import summarize_tracking
        from src.tracking_outcomes import load_current_outcomes

        report = summarize_tracking(cfg, persist=False)
        outcomes = load_current_outcomes(cfg)
        return {
            "record_count": report.get("record_count", 0),
            "unique_pipeline_count": report.get("unique_pipeline_count", 0),
            "unique_code_count": report.get("unique_code_count", 0),
            "by_selection_layer": report.get("by_selection_layer", {}),
            "by_strategy_count": report.get("by_strategy_count", {}),
            "by_diagnosis_signal": report.get("by_diagnosis_signal", {}),
            "strategy_source_counts": report.get("strategy_source_counts", {}),
            "latest_factor_panel": _latest_factor_panel(),
            "outcomes": {
                "generated_at": outcomes.get("generated_at", ""),
                "summary": outcomes.get("summary", {}),
            },
        }
    except Exception as exc:
        return {"error": str(exc), "latest_factor_panel": _latest_factor_panel()}


def _load_latest_tails(n: int = 10) -> list[Dict[str, Any]]:
    rows = []
    for path in sorted(_json_dir().glob("tail_v2_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:n]:
        data = _load_json(path)
        if data is not None:
            data["_file"] = path.name
            data["_mtime"] = datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds")
            rows.append(data)
    return rows


def _load_recent_pipelines(n: int = 10) -> list[Dict[str, Any]]:
    rows = []
    for path in sorted(_json_dir().glob("pipeline_v2_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:n]:
        data = _load_json(path)
        if not data:
            continue
        summary = data.get("summary", {})
        quality = data.get("snapshot_quality", {})
        rows.append({
            "_file": path.name,
            "timestamp": data.get("timestamp", ""),
            "tail_label": data.get("tail_label", ""),
            "strategies_ok": summary.get("strategies_ok", 0),
            "strategies_run": summary.get("strategies_run", 0),
            "overlap_candidates": summary.get("overlap_candidates", 0),
            "total_candidates": summary.get("total_candidates", 0),
            "elapsed": summary.get("total_elapsed_seconds", 0),
            "quality_ok": quality.get("ok"),
        })
    return rows


def _diagnosis_summary(pipeline: Dict[str, Any], name_map: Dict[str, str]) -> Optional[Dict[str, Any]]:
    diag = pipeline.get("diagnosis") or {}
    raw_source_items = pipeline.get("diagnosis_results") or diag.get("results") or []
    raw_results = [
        _clean_diagnosis_item(item, name_map)
        for item in raw_source_items
    ]
    if not diag and not raw_results:
        return None
    signals = diag.get("signal_distribution") or {}
    if raw_results and not signals:
        for item in raw_results:
            signal = item.get("signal", "")
            if signal:
                signals[signal] = signals.get(signal, 0) + 1
    top_picks = [
        _clean_diagnosis_item(item, name_map)
        for item in (diag.get("top_picks") or [
            item for item in raw_results if item.get("signal") in ("STRONG_BUY", "BUY")
        ])
    ][:10]
    watch_list = [
        _clean_diagnosis_item(item, name_map)
        for item in (diag.get("watch_list") or [
            item for item in raw_results if item.get("signal") == "WATCH"
        ])
    ][:10]
    summary_text = _clean_diagnosis_summary_text(
        str(diag.get("summary", "")),
        list(raw_source_items) + raw_results + top_picks + watch_list,
        name_map,
    )
    return {
        "enabled": diag.get("enabled", True),
        "role": diag.get("role", "validation_layer"),
        "engine": diag.get("engine", ""),
        "independent_strategy": bool(diag.get("independent_strategy", False)),
        "total": diag.get("total", len(raw_results)),
        "signal_distribution": signals,
        "results": raw_results[:30],
        "top_picks": top_picks,
        "watch_list": watch_list,
        "report_path": diag.get("report_path", ""),
        "summary": summary_text,
        "error": diag.get("error", ""),
        "sidecar": diag.get("sidecar", {}),
    }


def _v2_status() -> Dict[str, Any]:
    from src.quality_gate import audit_snapshot, resolve_snapshot
    from src.settings import load_settings
    from src.x1_preheat import latest_status as x1_preheat_status

    cfg = load_settings()
    snapshot_dir, source = resolve_snapshot(cfg)
    now_ts = time.time()
    cache_hit = (
        _quality_cache.get("quality") is not None
        and _quality_cache.get("snapshot_dir") == str(snapshot_dir)
        and float(_quality_cache.get("expires", 0) or 0) > now_ts
    )
    if cache_hit:
        quality = _quality_cache["quality"]
    else:
        quality = audit_snapshot(snapshot_dir, cfg, official=False)
        _quality_cache.update({
            "expires": now_ts + 60,
            "snapshot_dir": str(snapshot_dir),
            "source": source,
            "quality": quality,
        })
    metrics = quality.get("metrics", {})
    meta = quality.get("meta", {})
    x1_status = x1_preheat_status(cfg, snapshot_dir)
    pipeline = _load_latest_pipeline()

    status: Dict[str, Any] = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "snapshot": {
            "source": source,
            "dir": str(snapshot_dir),
            "quality_ok": quality.get("ok", False),
            "trade_date": metrics.get("expected_trade_date") or meta.get("trade_date") or "",
            "file_count": metrics.get("file_count", 0),
            "empty_files": metrics.get("empty_files", 0),
            "discontinuous_count": metrics.get("discontinuous_count", 0),
            "zero_close_count": metrics.get("zero_close_count", 0),
            "primary_source": meta.get("validation", {}).get("primary_source", ""),
            "grade": meta.get("grade", ""),
            "blockers": quality.get("blockers", []),
            "warnings": quality.get("warnings", []),
            "quality_cache": "hit" if cache_hit else "refresh",
        },
        "x1_preheat": x1_status,
        "tracking": _tracking_status(cfg),
        "latest_run": None,
        "boundary": None,
    }

    if not pipeline:
        return status

    name_map = _build_name_map(snapshot_dir, pipeline)
    summary = pipeline.get("summary", {})
    strategies = []
    for result in pipeline.get("strategies", []):
        top = result.get("top", [])
        display_state = _strategy_display_state(result)
        strategies.append({
            "name": result.get("display_name", result.get("strategy_name", "?")),
            "key": result.get("strategy_name", ""),
            "ok": result.get("ok", False),
            "status_kind": display_state["kind"],
            "status_label": display_state["label"],
            "count": len(top),
            "elapsed": result.get("elapsed_seconds", 0),
            "error": result.get("error", ""),
            "display_error": _strategy_display_error(result, display_state),
            "top": [
                {
                    "rank": row.get("rank", idx + 1),
                    "code": row.get("code", ""),
                    "name": _display_stock_name(row.get("code", ""), row.get("name", ""), name_map),
                    "pct_chg": row.get("pct_chg"),
                    "score": row.get("lift_score", row.get("wr", row.get("score"))),
                    "tag": row.get("tag", ""),
                    "diagnosis_badge": row.get("diagnosis_badge", ""),
                    "diagnosis_compact": row.get("diagnosis_compact", ""),
                    "diagnosis": row.get("diagnosis"),
                }
                for idx, row in enumerate(top[:10])
            ],
        })

    overlap = pipeline.get("overlap", {})
    overlaps = []
    for item in overlap.get("overlaps", [])[:30]:
        overlaps.append({
            "code": item.get("code", ""),
            "name": _display_stock_name(item.get("code", ""), item.get("name", ""), name_map),
            "strategies": item.get("strategies", []),
            "strategy_count": item.get("strategy_count", 0),
            "ranks": item.get("ranks", {}),
            "diagnosis_badge": item.get("diagnosis_badge", ""),
            "diagnosis_compact": item.get("diagnosis_compact", ""),
            "diagnosis": item.get("diagnosis"),
            "xgb_confirmed": item.get("xgb_confirmed", False),
        })

    boundary = pipeline.get("boundary")
    if boundary:
        clean_boundary = _clean_boundary(boundary, name_map)
        status["boundary"] = {
            "stats": clean_boundary.get("stats", {}),
            "risks": clean_boundary.get("risks", [])[:30],
            "candidates": clean_boundary.get("candidates", [])[:50],
        }

    status["latest_run"] = {
        "file": pipeline.get("_file", ""),
        "mtime": pipeline.get("_mtime", ""),
        "timestamp": pipeline.get("timestamp", ""),
        "tail_label": pipeline.get("tail_label", ""),
        "snapshot_dir": pipeline.get("snapshot_dir", ""),
        "strategies_run": summary.get("strategies_run", 0),
        "strategies_ok": summary.get("strategies_ok", 0),
        "total_candidates": summary.get("total_candidates", 0),
        "overlap_candidates": summary.get("overlap_candidates", 0),
        "elapsed": summary.get("total_elapsed_seconds", 0),
        "strategies": strategies,
        "overlaps": overlaps,
        "by_count": overlap.get("by_count", {}),
        "xgb_diagnosis": _diagnosis_summary(pipeline, name_map),
        "x1_preheat": pipeline.get("x1_preheat") or x1_status,
        "snapshot_quality": pipeline.get("snapshot_quality"),
    }
    return status


def _self_check() -> Dict[str, Any]:
    from src.settings import load_settings
    from src.tail_readiness import audit

    report = audit(load_settings())
    return {
        "timestamp": report.get("generated_at"),
        "checks": report.get("checks", []),
        "all_ok": report.get("blocking", 0) == 0,
        "blocking": report.get("blocking", 0),
        "warnings": report.get("warnings", 0),
    }


def _tail_readiness() -> Dict[str, Any]:
    from src.settings import load_settings
    from src.tail_automation import tail_window_state
    from src.tail_readiness import audit

    cfg = load_settings()
    report = audit(cfg)
    state = tail_window_state(cfg)
    tw = report.get("tail_window", {})
    return {
        "window_state": state,
        "window_start": tw.get("start", "14:50:00"),
        "window_end": tw.get("end", "14:57:00"),
        "interval_seconds": tw.get("interval_seconds", 60),
        "max_pushes": tw.get("max_pushes", 3),
        "blocking": report.get("blocking", 0),
        "warnings": report.get("warnings", 0),
        "checks": report.get("checks", []),
        "can_push": state == "during" and report.get("blocking", 0) == 0,
        "status": report.get("status", "blocked"),
    }


def _run_job(cmd: list[str]) -> None:
    global _job_status
    display = " ".join(cmd)
    with _job_lock:
        if _job_status.get("running"):
            return
        _job_status = {
            "running": True,
            "kind": display,
            "stage": "running",
            "message": "执行中...",
            "started_at": datetime.now().isoformat(timespec="seconds"),
            "returncode": None,
            "log_path": "",
        }
    log_path = _logs_dir() / f"dashboard_job_{datetime.now():%Y%m%d_%H%M%S}.log"
    try:
        result = subprocess.run(
            [PYTHON, str(ROOT / "main.py")] + cmd,
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=1800,
        )
        log_path.write_text((result.stdout or "") + "\n--- STDERR ---\n" + (result.stderr or ""), encoding="utf-8")
        with _job_lock:
            _job_status = {
                "running": False,
                "kind": display,
                "stage": "done" if result.returncode == 0 else "failed",
                "message": f"完成，返回码 {result.returncode}",
                "started_at": None,
                "returncode": result.returncode,
                "log_path": str(log_path),
            }
    except Exception as exc:
        log_path.write_text(str(exc), encoding="utf-8")
        with _job_lock:
            _job_status = {
                "running": False,
                "kind": display,
                "stage": "error",
                "message": str(exc),
                "started_at": None,
                "returncode": None,
                "log_path": str(log_path),
            }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: Any) -> None:
        return

    def _json(self, data: Any, code: int = 200) -> None:
        body = _json_bytes(data)
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _html(self, path: Path, code: int = 200) -> None:
        if not path.exists():
            self.send_response(404)
            self.end_headers()
            return
        body = path.read_bytes()
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        if path in ("/", "/dashboard"):
            self._html(STATIC / "dashboard.html")
        elif path == "/api/status":
            self._json(_v2_status())
        elif path == "/api/self-check":
            self._json(_self_check())
        elif path == "/api/tail-readiness":
            self._json(_tail_readiness())
        elif path == "/api/history":
            n = int(query.get("n", [10])[0])
            self._json({"pipelines": _load_recent_pipelines(n), "tails": _load_latest_tails(n)})
        elif path == "/api/job":
            self._json(_job_status)
        elif path == "/api/ping":
            self._json({"ok": True, "time": datetime.now().isoformat(timespec="seconds")})
        elif path == "/api/run":
            self._handle_run(query)
        else:
            self.send_response(404)
            self.end_headers()

    def _handle_run(self, query: Dict[str, list[str]]) -> None:
        cmd = query.get("cmd", [""])[0]
        allowed = {
            "tail-once",
            "tail-watch",
            "quality",
            "test-push",
            "run",
            "x1-preheat",
            "tracking-ingest",
            "factor-panel",
            "outcome-update",
        }
        if cmd not in allowed:
            self._json({"ok": False, "error": f"unknown command: {cmd}"}, code=400)
            return
        args = [cmd]
        if query.get("push", ["0"])[0] == "1" and cmd in {"tail-once", "tail-watch", "run"}:
            args.append("--push")
        if query.get("no_wait", ["0"])[0] == "1" and cmd == "tail-watch":
            args.append("--no-wait")
        max_cycles = query.get("max_cycles", [""])[0]
        if max_cycles and cmd == "tail-watch":
            args.extend(["--max-cycles", max_cycles])
        if query.get("serial", ["0"])[0] == "1" and cmd == "run":
            args.append("--serial")
        if query.get("skip_diag", ["0"])[0] == "1" and cmd == "run":
            args.append("--skip-diag")

        with _job_lock:
            if _job_status.get("running"):
                self._json({"ok": False, "error": "已有任务运行中", "job": _job_status}, code=409)
                return
        threading.Thread(target=_run_job, args=(args,), daemon=True).start()
        self._json({"ok": True, "action": "started", "cmd": args})


def run(host: str = "127.0.0.1", port: int = 8766, open_browser: bool = True) -> None:
    server = ThreadingHTTPServer((host, port), Handler)
    url = f"http://{host}:{port}"
    print(f"分仓之神 V2.0 控制台: {url}")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n控制台已关闭")
        server.shutdown()
