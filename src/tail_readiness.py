"""Tail readiness audit for V2.0 formal afternoon runs."""
from __future__ import annotations

import json
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

from src.quality_gate import audit_snapshot, format_quality_summary, quality_config, resolve_snapshot
from src.x1_preheat import select_tail_snapshot


def audit(cfg: Dict[str, Any], probe: bool = False) -> Dict[str, Any]:
    now = datetime.now()
    checks: List[Dict[str, Any]] = []
    blocking = 0
    warnings = 0

    def add(name: str, ok: bool, detail: str, *, warning: bool = False) -> None:
        nonlocal blocking, warnings
        checks.append({"name": name, "ok": ok, "detail": detail, "warning": warning})
        if not ok and warning:
            warnings += 1
        elif not ok:
            blocking += 1

    paths = cfg.get("paths", {})
    active_snapshot_dir, active_snapshot_source = resolve_snapshot(cfg)
    snapshot_dir, snapshot_source, preheat_status = select_tail_snapshot(cfg, active_snapshot_dir)
    quality = audit_snapshot(snapshot_dir, cfg, official=True)
    add(
        "快照质量闸门",
        bool(quality.get("ok")),
        f"{format_quality_summary(quality)} | 来源={snapshot_source} | {snapshot_dir}",
        warning=not bool(quality.get("ok")),
    )
    if active_snapshot_dir.resolve() != snapshot_dir.resolve():
        add(
            "尾盘快照选择",
            True,
            f"活动={active_snapshot_dir} ({active_snapshot_source}) | 尾盘使用={snapshot_dir} ({snapshot_source})",
        )
    for blocker in quality.get("blockers", [])[:5]:
        add("快照阻断项", False, blocker)

    vip = Path(str(paths.get("vip_screener_dir", "")))
    legacy = Path(str(paths.get("legacy_screener_dir", "")))
    x1 = Path(str(paths.get("x1_xin_dir", "")))
    xgb_dir = Path(str(paths.get("xgb_dir", "")))

    _check_file_group(add, "V10 VIP 策略", [
        vip / "screener_vip.py",
        vip / "screener_data.json",
        vip / "binning_stats_v3.json",
        vip / "neg_rules_3ind_core22.json",
    ])
    _check_file_group(add, "V1 策略", [
        legacy / "screener_app.py",
        legacy / "screener_data.json",
    ])
    _check_file_group(add, "V4 策略", [
        legacy / "screener_v4.py",
        legacy / "screener_v4.json",
    ])
    _check_file_group(add, "X1Beam 策略", [
        x1 / "screener_beam.py",
        x1 / "beam_core.py",
        x1 / "cache" / "beam_merged.json",
    ])
    if preheat_status.get("usable"):
        _check_x1beam_cache(add, cfg, snapshot_dir, preheat_status)
        add(
            "X1Beam manifest",
            True,
            f"完整预热可用 | {preheat_status.get('trade_date', '')} | Top={preheat_status.get('top_count', 0)} | {preheat_status.get('cache_path', '')}",
        )
    else:
        _check_x1beam_cache(add, cfg, snapshot_dir, preheat_status)
        add(
            "X1Beam manifest",
            False,
            preheat_status.get("error") or "未找到与当前快照匹配的完整预热 manifest；尾盘跳过 X1Beam，不阻断 V10/V1/V4",
            warning=True,
        )

    _check_file_group(add, "XGB 诊断验证层", [
        xgb_dir / "xgb_realtime_bridge" / "realtime_xgb.py",
        xgb_dir / "indicator_TA-Lib_native.py",
        xgb_dir / "model_v2" / "xgb_5d_v2.json",
        xgb_dir / "model_v2" / "names_v2.pkl",
    ], warning=True)

    has_wecom, channel_count = _wecom_channels(cfg)
    add("企业微信推送", has_wecom, f"{channel_count} 个通道" if has_wecom else "未配置 webhook")

    out_dir = Path(str(paths.get("output_root", "outputs")))
    out_dir.mkdir(parents=True, exist_ok=True)
    add("V2.0 输出目录", out_dir.exists(), str(out_dir))

    tail_cfg = cfg.get("automation", {}).get("tail", {})
    start = str(tail_cfg.get("start_time", "14:50:00"))
    end = str(tail_cfg.get("end_time", "14:57:00"))
    interval = int(tail_cfg.get("interval_seconds", 60))
    max_pushes = int(tail_cfg.get("max_pushes", 3))
    min_ok = int(tail_cfg.get("min_strategy_success", 2))
    capacity = _capacity_minutes(start, end) * 60 // max(interval, 1)
    add("尾盘窗口容量", capacity >= 2, f"{start}-{end}, 间隔 {interval}s, 理论 {capacity} 轮, 目标推送 {max_pushes} 轮")
    add("策略成功门槛", min_ok <= 2, f"成功策略 >= {min_ok} 才推送", warning=min_ok > 2)

    if probe:
        tasks_ok, tasks_total = _scheduled_tasks_ok()
        add("Windows 计划任务", tasks_ok >= 1, f"{tasks_ok}/{tasks_total} 已注册", warning=True)

    status = "ready" if blocking == 0 else "blocked"
    return {
        "generated_at": now.isoformat(timespec="seconds"),
        "status": status,
        "blocking": blocking,
        "warnings": warnings,
        "checks": checks,
        "snapshot_quality": quality,
        "tail_window": {"start": start, "end": end, "interval_seconds": interval, "max_pushes": max_pushes},
        "push_config": {"ok": has_wecom, "channels": channel_count},
        "quality_gate": quality_config(cfg),
        "x1_preheat": preheat_status,
    }


def build_push_markdown(report: Dict[str, Any]) -> str:
    lines = [
        "## 分仓之神 V2.0 尾盘就绪审计",
        f"> {report['generated_at'][:19]} | 状态: **{report['status'].upper()}**",
        f"> 窗口: {report['tail_window']['start']} - {report['tail_window']['end']} | 阻断 {report['blocking']} | 提醒 {report.get('warnings', 0)}",
        "",
        "| 检查项 | 状态 | 详情 |",
        "|--------|------|------|",
    ]
    for check in report["checks"]:
        if check.get("ok"):
            tag = "OK"
        elif check.get("warning"):
            tag = "WARN"
        else:
            tag = "FAIL"
        lines.append(f"| {check['name']} | {tag} | {check['detail']} |")

    lines.append("")
    if report["blocking"]:
        lines.append(f"> **{report['blocking']} 项硬阻断**，正式尾盘出票/推送应停止。")
    else:
        lines.append("> 硬门槛已通过，尾盘可按 V10/V1/V4 执行；X1Beam 有完整预热缓存时作为第四个对等策略；XGB 只做诊断验证层。")
    return "\n".join(lines)


def _check_x1beam_cache(add, cfg: Dict[str, Any], snapshot_dir: Path, preheat_status: Dict[str, Any]) -> None:
    if not preheat_status.get("usable"):
        detail = (
            f"completed={preheat_status.get('completed')} cache={preheat_status.get('cache_exists')} "
            f"match={preheat_status.get('matches_current_snapshot')} top={preheat_status.get('top_count', 0)} "
            f"{preheat_status.get('reason') or preheat_status.get('error', '')}"
        )
        add("X1Beam 预热缓存", False, detail, warning=True)
        return
    cache_dir = Path(str(cfg.get("paths", {}).get("output_root", "outputs"))) / "cache" / "x1beam_fast"
    files = sorted(cache_dir.glob("x1beam_fast_*.json"), key=lambda p: p.stat().st_mtime, reverse=True) if cache_dir.exists() else []
    if not files:
        add("X1Beam 预热缓存", False, "尚无完整预热缓存；尾盘会跳过 X1Beam，不阻断 V10/V1/V4", warning=True)
        return
    current_files = len(list(Path(snapshot_dir).glob("*.txt")))
    latest_detail = ""
    for path in files:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            meta = payload.get("adapter_snapshot_meta") or {}
            detail = f"{path.name} | 完整={payload.get('completed')} | 文件={meta.get('file_count', 0)}/{current_files}"
            if not latest_detail:
                latest_detail = detail
            ok = payload.get("completed") is True and int(meta.get("file_count", 0) or 0) == current_files
            if ok:
                add("X1Beam 预热缓存", True, detail)
                return
        except Exception as exc:
            if not latest_detail:
                latest_detail = f"{path.name} | 读取失败: {exc}"
    add("X1Beam 预热缓存", False, latest_detail or "未找到可读取缓存", warning=True)


def _check_file_group(add, name: str, files: List[Path], *, warning: bool = False) -> None:
    missing = [str(path) for path in files if not path.exists()]
    if missing:
        add(name, False, "缺失: " + "; ".join(missing[:3]), warning=warning)
    else:
        add(name, True, f"{len(files)} 个文件就绪")


def _wecom_channels(cfg: Dict[str, Any]) -> tuple[bool, int]:
    runtime = cfg.get("runtime", {})
    urls = []
    if runtime.get("wecom_webhook_urls"):
        urls.extend([item.strip() for item in str(runtime["wecom_webhook_urls"]).split(";") if item.strip()])
    if runtime.get("wecom_webhook_url"):
        urls.append(str(runtime["wecom_webhook_url"]).strip())
    return bool(urls), len(urls)


def _capacity_minutes(start: str, end: str) -> int:
    try:
        sh, sm, *_ = [int(x) for x in start.split(":")]
        eh, em, *_ = [int(x) for x in end.split(":")]
        return max(0, (eh * 60 + em) - (sh * 60 + sm))
    except Exception:
        return 0


def _scheduled_tasks_ok() -> tuple[int, int]:
    tasks = ["ZTFHQ-V2-TailWatch", "ZTFHQ-V2-SnapshotCheck", "ZTFHQ-V2-DailyReport"]
    ok = 0
    for task in tasks:
        try:
            result = subprocess.run(["schtasks", "/Query", "/TN", task], capture_output=True, text=True, timeout=5)
            ok += 1 if result.returncode == 0 else 0
        except Exception:
            pass
    return ok, len(tasks)
