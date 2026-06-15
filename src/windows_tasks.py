"""Windows scheduled task helpers for V2.0 automation."""
from __future__ import annotations

import csv
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime
from io import StringIO
from pathlib import Path
from typing import Any, Dict, List


PROJECT_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class TaskSpec:
    name: str
    time: str
    script: str
    log_name: str
    description: str

    @property
    def script_path(self) -> Path:
        return PROJECT_ROOT / "tasks" / self.script

    @property
    def log_path(self) -> Path:
        return PROJECT_ROOT / "outputs" / "logs" / self.log_name


TASKS = [
    TaskSpec(
        name="ZTFHQ-V2-SnapshotCheck",
        time="14:20",
        script="v2_pre_tail_prep.cmd",
        log_name="task_pre_tail_prep.log",
        description="尾盘前准备：实时快照 + 正式质检 + X1Beam预热",
    ),
    TaskSpec(
        name="ZTFHQ-V2-TailWatch",
        time="14:49",
        script="v2_tail_watch.cmd",
        log_name="task_tail_watch.log",
        description="尾盘监控：14:50-14:57 自动推送2-3轮",
    ),
    TaskSpec(
        name="ZTFHQ-V2-DailyReport",
        time="15:05",
        script="v2_post_market_refresh.cmd",
        log_name="task_post_market_refresh.log",
        description="盘后刷新：追踪入库 + 收益回填 + 因子与模式标签",
    ),
]


def _run(args: List[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)


def _clean_task_target(value: str) -> str:
    return str(value or "").strip().strip('"').rstrip()


def _target_matches(actual: str, expected: Path) -> bool:
    actual_clean = _clean_task_target(actual)
    if not actual_clean:
        return False
    expected_clean = str(expected)
    lowered = actual_clean.replace("/", "\\").casefold()
    expected_lowered = expected_clean.replace("/", "\\").casefold()
    expected_suffix = f"\\tasks\\{expected.name}".casefold()
    return lowered == expected_lowered or lowered.endswith(expected_suffix)


def _looks_like_datetime(value: str) -> bool:
    return bool(re.search(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}\s+\d{1,2}:\d{2}", str(value or "")))


def _looks_like_result(value: str) -> bool:
    return bool(re.fullmatch(r"-?\d+", str(value or "").strip()))


def _fallback_from_values(data: Dict[str, Any], values: List[str], spec: TaskSpec) -> None:
    clean_values = [str(value or "").strip() for value in values if str(value or "").strip()]
    datetimes = [value for value in clean_values if _looks_like_datetime(value)]
    if not _looks_like_datetime(str(data.get("next_run_time") or "")) and datetimes:
        data["next_run_time"] = datetimes[0]
    if not _looks_like_datetime(str(data.get("last_run_time") or "")) and len(datetimes) > 1:
        data["last_run_time"] = datetimes[1]

    script_hits = [value for value in clean_values if spec.script.casefold() in value.casefold()]
    if script_hits:
        data["task_to_run"] = script_hits[0]

    if not _looks_like_result(str(data.get("last_result") or "")):
        results = [value for value in clean_values if _looks_like_result(value)]
        if results:
            data["last_result"] = results[0]


def _log_status(path: Path) -> Dict[str, Any]:
    info: Dict[str, Any] = {"log_path": str(path), "log_exists": path.exists()}
    if not path.exists():
        return info
    try:
        stat = path.stat()
        info["log_mtime"] = datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds")
        info["log_size"] = stat.st_size
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        info["log_tail"] = "\n".join(lines[-6:])
    except Exception as exc:
        info["log_error"] = str(exc)
    return info


def _enrich_runtime_status(data: Dict[str, Any], spec: TaskSpec) -> None:
    task_to_run = _clean_task_target(str(data.get("task_to_run") or ""))
    script_exists = spec.script_path.exists()
    target_matches = _target_matches(task_to_run, spec.script_path)
    display_target = str(spec.script_path) if target_matches else task_to_run
    data.update(
        {
            "task_to_run": display_target or str(spec.script_path),
            "script_exists": script_exists,
            "target_matches": target_matches,
            "ready": bool(data.get("exists") and script_exists and target_matches),
        }
    )
    data.update(_log_status(spec.log_path))


def install_tasks() -> Dict[str, Any]:
    results = []
    for spec in TASKS:
        command = str(spec.script_path)
        args = [
            "schtasks",
            "/Create",
            "/TN",
            spec.name,
            "/SC",
            "WEEKLY",
            "/D",
            "MON,TUE,WED,THU,FRI",
            "/ST",
            spec.time,
            "/TR",
            command,
            "/F",
        ]
        cp = _run(args)
        results.append(
            {
                "name": spec.name,
                "ok": cp.returncode == 0,
                "time": spec.time,
                "script": str(spec.script_path),
                "description": spec.description,
                "stdout": (cp.stdout or "").strip(),
                "stderr": (cp.stderr or "").strip(),
                "returncode": cp.returncode,
            }
        )
    return {"ok": all(row["ok"] for row in results), "results": results}


def delete_tasks() -> Dict[str, Any]:
    results = []
    for spec in TASKS:
        cp = _run(["schtasks", "/Delete", "/TN", spec.name, "/F"])
        ok = cp.returncode == 0 or "cannot find" in ((cp.stdout or "") + (cp.stderr or "")).lower()
        results.append(
            {
                "name": spec.name,
                "ok": ok,
                "stdout": (cp.stdout or "").strip(),
                "stderr": (cp.stderr or "").strip(),
                "returncode": cp.returncode,
            }
        )
    return {"ok": all(row["ok"] for row in results), "results": results}


def query_tasks() -> Dict[str, Any]:
    rows = []
    for spec in TASKS:
        cp = _run(["schtasks", "/Query", "/TN", spec.name, "/FO", "CSV", "/V"])
        data: Dict[str, Any] = {
            "name": spec.name,
            "exists": cp.returncode == 0,
            "expected_time": spec.time,
            "expected_script": str(spec.script_path),
            "description": spec.description,
            "returncode": cp.returncode,
        }
        if cp.returncode == 0 and cp.stdout.strip():
            try:
                parsed = list(csv.DictReader(StringIO(cp.stdout)))
                if parsed:
                    row = {str(k or "").strip().lstrip("\ufeff"): v for k, v in parsed[0].items()}
                    values = list(row.values())

                    def pick(*names: str, index: int = -1, default: str = "") -> str:
                        for name in names:
                            value = row.get(name)
                            if value not in (None, ""):
                                return str(value)
                        if 0 <= index < len(values):
                            value = values[index]
                            if value not in (None, ""):
                                return str(value)
                        return default
                    data.update(
                        {
                            "task_name": pick("TaskName", "任务名", index=1, default=spec.name),
                            "next_run_time": pick("Next Run Time", "下次运行时间", index=2, default=spec.time),
                            "status": pick("Status", "状态", index=3, default="-"),
                            "last_run_time": pick("Last Run Time", "上次运行时间", index=5, default="-"),
                            "last_result": pick("Last Result", "上次结果", index=6, default="-"),
                            "task_to_run": pick("Task To Run", "要运行的任务", index=8, default=str(spec.script_path)),
                            "start_in": pick("Start In", "起始于", index=9, default="N/A"),
                            "schedule_type": pick("Schedule Type", "计划类型", index=18, default="Weekly"),
                            "start_time": pick("Start Time", "开始时间", index=19, default=spec.time),
                            "days": pick("Days", "天", index=22, default="MON-FRI"),
                        }
                    )
                    _fallback_from_values(data, values, spec)
            except Exception as exc:
                data["parse_error"] = str(exc)
        else:
            data["stderr"] = (cp.stderr or cp.stdout or "").strip()
        _enrich_runtime_status(data, spec)
        rows.append(data)
    return {
        "ok": all(row.get("ready") for row in rows),
        "count": sum(1 for row in rows if row.get("exists")),
        "ready_count": sum(1 for row in rows if row.get("ready")),
        "total": len(rows),
        "tasks": rows,
    }
