"""Windows scheduled task helpers for V2.0 automation."""
from __future__ import annotations

import csv
import subprocess
from dataclasses import dataclass
from io import StringIO
from pathlib import Path
from typing import Any, Dict, List


PROJECT_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class TaskSpec:
    name: str
    time: str
    script: str
    description: str

    @property
    def script_path(self) -> Path:
        return PROJECT_ROOT / "tasks" / self.script


TASKS = [
    TaskSpec(
        name="ZTFHQ-V2-SnapshotCheck",
        time="14:20",
        script="v2_pre_tail_prep.cmd",
        description="尾盘前准备：实时快照 + 正式质检 + X1Beam预热",
    ),
    TaskSpec(
        name="ZTFHQ-V2-TailWatch",
        time="14:49",
        script="v2_tail_watch.cmd",
        description="尾盘监控：14:50-14:57 自动推送2-3轮",
    ),
    TaskSpec(
        name="ZTFHQ-V2-DailyReport",
        time="15:05",
        script="v2_post_market_refresh.cmd",
        description="盘后刷新：追踪入库 + 收益回填 + 因子与模式标签",
    ),
]


def _run(args: List[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)


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
                    def pick(*names: str, default: str = "") -> str:
                        for name in names:
                            value = row.get(name)
                            if value not in (None, ""):
                                return str(value)
                        return default
                    data.update(
                        {
                            "task_name": pick("TaskName", "任务名", default=spec.name),
                            "next_run_time": pick("Next Run Time", "下次运行时间", default=spec.time),
                            "status": pick("Status", "状态", default="-"),
                            "last_run_time": pick("Last Run Time", "上次运行时间", default="-"),
                            "last_result": pick("Last Result", "上次结果", default="-"),
                            "task_to_run": pick("Task To Run", "要运行的任务", default=str(spec.script_path)),
                            "start_in": pick("Start In", "起始于", default="N/A"),
                            "schedule_type": pick("Schedule Type", "计划类型", default="Weekly"),
                            "start_time": pick("Start Time", "开始时间", default=spec.time),
                            "days": pick("Days", "天", default="MON-FRI"),
                        }
                    )
            except Exception as exc:
                data["parse_error"] = str(exc)
        else:
            data["stderr"] = (cp.stderr or cp.stdout or "").strip()
        rows.append(data)
    return {
        "ok": all(row.get("exists") for row in rows),
        "count": sum(1 for row in rows if row.get("exists")),
        "total": len(rows),
        "tasks": rows,
    }
