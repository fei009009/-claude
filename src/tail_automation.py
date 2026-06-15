"""V2.0 尾盘自动化：14:50-14:57 多轮推送 + 熔断 + 控制台刷新。"""
from __future__ import annotations

import json
import time
import traceback
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Optional

from src.boundary_audit import scan_boundary_candidates
from src.diagnosis.annotate import annotate_all
from src.diagnosis.service import run_xgb_validation_layer
from src.pipeline import build_overlap_analysis, run_all_strategies
from src.quality_gate import audit_snapshot, format_quality_summary, quality_config, resolve_snapshot
from src.sentiment_regime import build_sentiment_regime
from src.sentiment_overlay import annotate_all_with_sentiment
from src.tracking_store import ingest_pipeline_file
from src.wecom_push import build_run_markdown, push_wecom_report
from src.x1_preheat import latest_status as x1_preheat_status, run_preheat as run_x1_preheat, select_tail_snapshot

SEP = "-" * 72
_FAILS = 0


def _now() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _emit(message: str = "") -> None:
    print(f"[{_now()}] {message}", flush=True)


def _banner(title: str) -> None:
    _emit(SEP)
    _emit(title)
    _emit(SEP)


def _json_dir(cfg: Dict[str, Any]) -> Path:
    out = Path(str(cfg.get("paths", {}).get("output_root", "outputs"))) / "json"
    out.mkdir(parents=True, exist_ok=True)
    return out


def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _tail_status_payload(cycle: Dict[str, Any]) -> Dict[str, Any]:
    drop = {"results", "overlap", "diagnosis_results"}
    clean = {key: value for key, value in cycle.items() if key not in drop}
    clean["updated_at"] = datetime.now().isoformat(timespec="seconds")
    return clean


def _persist_tail_current(cfg: Dict[str, Any], cycle: Dict[str, Any]) -> Path:
    path = _json_dir(cfg) / "tail_v2_current.json"
    _write_json(path, _tail_status_payload(cycle))
    return path


def _persist_pipeline(
    cfg: Dict[str, Any],
    snapshot_dir: Path,
    quality: Dict[str, Any],
    cycle: Dict[str, Any],
) -> Path:
    summary = cycle.get("summary") or {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "strategies_run": cycle.get("strategies_run", 0),
        "strategies_ok": cycle.get("strategies_ok", 0),
        "total_candidates": cycle.get("total_candidates", 0),
        "overlap_candidates": cycle.get("overlap_candidates", 0),
        "total_elapsed_seconds": cycle.get("elapsed_seconds", 0),
    }
    payload = {
        "timestamp": summary.get("timestamp", datetime.now().isoformat(timespec="seconds")),
        "tail_label": cycle.get("label", ""),
        "snapshot_dir": str(snapshot_dir),
        "snapshot_quality": quality,
        "strategies": cycle.get("results", []),
        "overlap": cycle.get("overlap", {}),
        "summary": summary,
        "boundary": cycle.get("boundary"),
        "diagnosis": cycle.get("diagnosis"),
        "diagnosis_results": cycle.get("diagnosis_results", []),
        "sentiment": cycle.get("sentiment"),
        "x1_preheat": cycle.get("x1_preheat"),
    }
    path = _json_dir(cfg) / f"pipeline_v2_{datetime.now():%Y%m%d_%H%M%S}.json"
    _write_json(path, payload)
    return path


def _persist_tail_status(cfg: Dict[str, Any], cycle: Dict[str, Any]) -> Path:
    path = _json_dir(cfg) / f"tail_v2_{datetime.now():%Y%m%d_%H%M%S_%f}.json"
    clean = _tail_status_payload(cycle)
    _write_json(path, clean)
    _write_json(_json_dir(cfg) / "tail_v2_current.json", clean)
    return path


def tail_window_state(cfg: Dict[str, Any], now: Optional[datetime] = None) -> str:
    tail = cfg.get("automation", {}).get("tail", {})
    now = now or datetime.now()
    start = _at(str(tail.get("start_time", "14:50:00")))
    end = _at(str(tail.get("end_time", "14:57:00")))
    if now < start:
        return "before"
    if now > end:
        return "after"
    return "during"


def _trade_date_from_quality(quality: Dict[str, Any] | None) -> str:
    quality = quality or {}
    metrics = quality.get("metrics") or {}
    meta = quality.get("meta") or {}
    return str(
        metrics.get("expected_trade_date")
        or metrics.get("observed_trade_date")
        or metrics.get("meta_trade_date")
        or meta.get("trade_date")
        or ""
    )


def _run_cycle(snapshot_dir: Path, cfg: Dict[str, Any], label: str, quality: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    global _FAILS
    max_fails = quality_config(cfg)["max_consecutive_failures"]
    _banner(f"{label} 开始")
    cycle: Dict[str, Any] = {
        "label": label,
        "ok": False,
        "pushed": False,
        "error": "",
        "elapsed_seconds": 0,
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "strategies_run": 0,
        "strategies_ok": 0,
        "total_candidates": 0,
        "overlap_candidates": 0,
    }

    if _FAILS >= max_fails:
        cycle["error"] = f"熔断: 连续失败 {_FAILS}/{max_fails}"
        return cycle

    started = time.perf_counter()
    try:
        _emit("运行四策略...")
        results, summary = run_all_strategies(snapshot_dir, cfg, parallel=True)
        overlap = build_overlap_analysis(results, cfg)
        min_ok = int(cfg.get("automation", {}).get("tail", {}).get("min_strategy_success", 2))
        cycle.update(
            results=results,
            summary=summary,
            overlap=overlap,
            strategies_run=summary.get("strategies_run", 0),
            strategies_ok=summary.get("strategies_ok", 0),
            total_candidates=summary.get("total_candidates", 0),
            overlap_candidates=summary.get("overlap_candidates", 0),
            ok=summary.get("strategies_ok", 0) >= min_ok,
        )

        for result in results:
            tag = "OK" if result.get("ok") else "FAIL"
            top3 = [row.get("code", "") for row in result.get("top", [])[:3]]
            _emit(f"  [{tag}] {result.get('display_name', result.get('strategy_name', '?'))}: {len(result.get('top', []))} {top3}")

        if overlap.get("overlaps"):
            for item in overlap["overlaps"][:5]:
                _emit(f"  交集: {item['code']} {item.get('name', '')} [{'+'.join(item.get('strategies', []))}]")

        try:
            boundary = scan_boundary_candidates(snapshot_dir, results)
            cycle["boundary"] = boundary
            stats = boundary.get("stats", {})
            if stats.get("risk_count", 0):
                _emit(f"  边界风险: 临界 {stats.get('critical', 0)}，风险 {stats.get('risk_count', 0)}")
        except Exception as exc:
            _emit(f"  边界审计跳过: {exc}")

        if cfg.get("diagnosis", {}).get("enabled", True):
            _emit("运行 XGB 诊断验证层...")
            try:
                diag_objects, diag_summary = run_xgb_validation_layer(
                    cfg,
                    results,
                    snapshot_dir=snapshot_dir,
                    candidates_source="tail V10/V1/V4/X1Beam Top candidates",
                    persist=True,
                )
                cycle["diagnosis"] = diag_summary
                cycle["diagnosis_results"] = [item.to_dict() if hasattr(item, "to_dict") else item for item in diag_objects]
                cycle["results"], cycle["overlap"] = annotate_all(
                    cycle.get("results", []),
                    cycle.get("overlap", {}),
                    cycle["diagnosis_results"],
                    diag_summary,
                )
                _emit(f"  诊断: {diag_summary.get('signal_distribution', {})}")
                if diag_summary.get("report_path"):
                    _emit(f"  报告: {diag_summary['report_path']}")
            except Exception as exc:
                cycle["diagnosis"] = {"enabled": True, "role": "validation_layer", "error": str(exc)}
                cycle["diagnosis_results"] = []
                _emit(f"  XGB 诊断跳过: {exc}")

        try:
            sentiment_report = build_sentiment_regime(
                cfg,
                trade_date=_trade_date_from_quality(quality),
                persist=True,
            )
            cycle["sentiment"] = sentiment_report
            cycle["results"], cycle["overlap"] = annotate_all_with_sentiment(
                cycle.get("results", []),
                cycle.get("overlap", {}),
                sentiment_report,
            )
            timing = sentiment_report.get("timing") or {}
            freshness = sentiment_report.get("freshness") or {}
            _emit(
                "  情绪周期: "
                f"{timing.get('state', '-')}({timing.get('value', 0)}) "
                f"仓位系数={timing.get('position_multiplier', 1)} "
                f"对齐={'OK' if freshness.get('ok_for_snapshot') else 'WARN'}"
            )
        except Exception as exc:
            cycle["sentiment"] = {"ok": False, "error": str(exc)}
            _emit(f"  情绪周期融合跳过: {exc}")

        _FAILS = 0 if cycle["ok"] else _FAILS + 1
        if not cycle["ok"]:
            _emit(f"  策略成功数不足，连续失败 {_FAILS}/{max_fails}")

    except Exception as exc:
        cycle["error"] = str(exc)
        _FAILS += 1
        traceback.print_exc()

    cycle["elapsed_seconds"] = round(time.perf_counter() - started, 3)
    _emit(
        f"完成: {'通过' if cycle['ok'] else '失败'} | "
        f"策略 {cycle['strategies_ok']}/{cycle['strategies_run']} | "
        f"交集 {cycle['overlap_candidates']} | {cycle['elapsed_seconds']:.1f}s"
    )
    return cycle


def run_tail_once(cfg: Dict[str, Any], push: bool = True, label: str = "once", persist_status: bool = True) -> Dict[str, Any]:
    active_snapshot_dir, active_source = resolve_snapshot(cfg)
    snapshot_dir, source, preheat_status = select_tail_snapshot(cfg, active_snapshot_dir)
    _emit(f"快照: {snapshot_dir} ({source})")
    if preheat_status.get("usable"):
        _emit("X1Beam 预热: 可用，作为第四策略参与交集")
    else:
        _emit("X1Beam 预热: 未就绪，本轮只用 V10/V1/V4 正式出票")

    quality = audit_snapshot(snapshot_dir, cfg, official=True)
    _emit(f"质量: {format_quality_summary(quality)}")
    if not quality.get("ok"):
        error = "; ".join(quality.get("blockers", [])) or "快照质量不合格"
        cycle = {
            "label": label,
            "ok": False,
            "pushed": False,
            "error": error,
            "snapshot_source": source,
            "snapshot_dir": str(snapshot_dir),
            "snapshot_quality": quality,
            "x1_preheat": preheat_status,
            "elapsed_seconds": 0,
            "started_at": datetime.now().isoformat(timespec="seconds"),
            "stage": "blocked",
            "running": False,
        }
        if persist_status:
            status_path = _persist_tail_status(cfg, cycle)
            cycle["status_path"] = str(status_path)
        else:
            _persist_tail_current(cfg, cycle)
        _emit(f"阻断正式出票: {error}")
        return cycle

    cycle = _run_cycle(snapshot_dir, cfg, label, quality)
    cycle["snapshot_source"] = source
    cycle["snapshot_dir"] = str(snapshot_dir)
    cycle["snapshot_quality"] = quality
    cycle["active_snapshot_dir"] = str(active_snapshot_dir)
    cycle["active_snapshot_source"] = active_source
    cycle["x1_preheat"] = preheat_status
    cycle["stage"] = "analysis_done"
    cycle["running"] = False

    pipeline_path = _persist_pipeline(cfg, snapshot_dir, quality, cycle)
    cycle["pipeline_path"] = str(pipeline_path)
    _emit(f"控制台结果已刷新: {pipeline_path.name}")
    try:
        tracking = ingest_pipeline_file(cfg, pipeline_path)
        _emit(f"追踪入库: 新增 {tracking.get('new_records', 0)} 条")
    except Exception as exc:
        _emit(f"追踪入库跳过: {exc}")

    if push and cycle.get("ok"):
        try:
            markdown = build_run_markdown(
                cycle.get("results", []),
                cycle.get("overlap", {}),
                diagnosis_results=cycle.get("diagnosis_results", []),
                sentiment_report=cycle.get("sentiment"),
                cfg=cfg,
                label=label,
            )
            push_report = push_wecom_report(markdown, cfg)
            cycle["push_report"] = push_report
            cycle["pushed"] = bool(push_report.get("ok"))
            _emit(
                f"推送: {'OK' if cycle['pushed'] else 'FAIL'} | "
                f"通道 {push_report.get('ok_count', 0)}/{push_report.get('channel_count', 0)} | "
                f"{push_report.get('elapsed_seconds', 0)}s"
            )
        except Exception as exc:
            cycle["pushed"] = False
            _emit(f"推送异常: {exc}")

    if persist_status:
        status_path = _persist_tail_status(cfg, cycle)
        cycle["status_path"] = str(status_path)
    else:
        _persist_tail_current(cfg, cycle)
    return cycle


def run_tail_watch(
    cfg: Dict[str, Any],
    push: bool = True,
    no_wait: bool = False,
    max_cycles: Optional[int] = None,
) -> Dict[str, Any]:
    tail = cfg.get("automation", {}).get("tail", {})
    start = _at(str(tail.get("start_time", "14:50:00")))
    end = _at(str(tail.get("end_time", "14:57:00")))
    interval = int(tail.get("interval_seconds", 60))
    max_pushes = int(tail.get("max_pushes", 3))
    min_pushes = int(tail.get("min_pushes", 2))

    _banner("尾盘监控 V2.0")
    _emit(f"窗口: {start:%H:%M:%S} - {end:%H:%M:%S} | 间隔 {interval}s | 目标推送 {min_pushes}-{max_pushes} 轮")
    watch_started_at = datetime.now().isoformat(timespec="seconds")
    watch_mode = "test" if no_wait else "formal"
    _persist_tail_current(
        cfg,
        {
            "label": "尾盘监控",
            "mode": watch_mode,
            "stage": "preheating" if not no_wait else "running",
            "running": True,
            "ok": False,
            "pushed": False,
            "cycle_count": 0,
            "accepted_cycle_count": 0,
            "push_count": 0,
            "min_pushes": min_pushes,
            "max_pushes": max_pushes,
            "started_at": watch_started_at,
        },
    )

    if not no_wait:
        _maybe_preheat_x1_before_tail(cfg, start)
        _persist_tail_current(
            cfg,
            {
                "label": "尾盘监控",
                "mode": watch_mode,
                "stage": "waiting_window",
                "running": True,
                "ok": False,
                "pushed": False,
                "cycle_count": 0,
                "accepted_cycle_count": 0,
                "push_count": 0,
                "min_pushes": min_pushes,
                "max_pushes": max_pushes,
                "started_at": watch_started_at,
            },
        )
        _emit("等待尾盘窗口...")
        while datetime.now() < start:
            time.sleep(max(1, min(30, (start - datetime.now()).total_seconds())))
        _emit("尾盘窗口开始")

    cycles = 0
    accepted = 0
    pushes = 0
    best_cycle: Optional[Dict[str, Any]] = None
    next_due = datetime.now() if no_wait else start

    while True:
        now = datetime.now()
        if not no_wait and now > end:
            _emit("尾盘窗口结束")
            break
        if max_cycles is not None and cycles >= max_cycles:
            _emit(f"达到指定轮次上限 {max_cycles}")
            break
        if pushes >= max_pushes:
            _emit(f"已达到推送上限 {max_pushes}")
            break
        if not no_wait and now < next_due:
            wait_seconds = max(0.0, (next_due - now).total_seconds())
            if wait_seconds > 0:
                _emit(f"等待下一轮目标时点 {next_due:%H:%M:%S}，约 {wait_seconds:.0f}s")
                time.sleep(min(wait_seconds, max(1, interval)))

        label = f"第{cycles + 1}轮"
        due_at = next_due
        _persist_tail_current(
            cfg,
            {
                "label": label,
                "mode": watch_mode,
                "stage": "cycle_running",
                "running": True,
                "ok": False,
                "pushed": False,
                "cycle_count": cycles + 1,
                "accepted_cycle_count": accepted,
                "push_count": pushes,
                "min_pushes": min_pushes,
                "max_pushes": max_pushes,
                "target_start_at": due_at.isoformat(timespec="seconds"),
                "started_at": datetime.now().isoformat(timespec="seconds"),
            },
        )
        cycle = run_tail_once(cfg, push=False, label=label, persist_status=False)
        cycles += 1
        cycle["target_start_at"] = due_at.isoformat(timespec="seconds")
        cycle["cycle_count"] = cycles
        cycle["accepted_cycle_count"] = accepted
        cycle["push_count"] = pushes
        cycle["min_pushes"] = min_pushes
        cycle["max_pushes"] = max_pushes
        cycle["mode"] = watch_mode
        cycle["stage"] = "analysis_done"
        cycle["running"] = True

        if cycle.get("ok"):
            accepted += 1
            cycle["accepted_cycle_count"] = accepted
            if push and pushes < max_pushes:
                try:
                    cycle["stage"] = "pushing"
                    _persist_tail_current(cfg, cycle)
                    markdown = build_run_markdown(
                        cycle.get("results", []),
                        cycle.get("overlap", {}),
                        diagnosis_results=cycle.get("diagnosis_results", []),
                        sentiment_report=cycle.get("sentiment"),
                        cfg=cfg,
                        label=f"{label} {now:%H:%M:%S}",
                    )
                    push_report = push_wecom_report(markdown, cfg)
                    cycle["push_report"] = push_report
                    if push_report.get("ok"):
                        pushes += 1
                        cycle["pushed"] = True
                        cycle["pushed_at"] = datetime.now().isoformat(timespec="seconds")
                        cycle["push_count"] = pushes
                        cycle["stage"] = "pushed"
                        cycle["running"] = False
                        _emit(
                            f"{label} 推送成功 {pushes}/{max_pushes} | "
                            f"通道 {push_report.get('ok_count', 0)}/{push_report.get('channel_count', 0)} | "
                            f"{push_report.get('elapsed_seconds', 0)}s"
                        )
                    else:
                        cycle["pushed"] = False
                        cycle["push_error"] = "push_wecom_report returned False"
                        cycle["stage"] = "push_failed"
                        cycle["running"] = False
                        _emit(
                            f"{label} 推送失败 | "
                            f"通道 {push_report.get('ok_count', 0)}/{push_report.get('channel_count', 0)} | "
                            f"{push_report.get('elapsed_seconds', 0)}s"
                        )
                except Exception as exc:
                    cycle["pushed"] = False
                    cycle["push_error"] = str(exc)
                    cycle["stage"] = "push_failed"
                    cycle["running"] = False
                    _emit(f"{label} 推送异常: {exc}")
                try:
                    status_path = _persist_tail_status(cfg, cycle)
                    cycle["status_path"] = str(status_path)
                    _emit(f"{label} 推送状态已刷新: {status_path.name}")
                except Exception as exc:
                    _emit(f"{label} 推送状态刷新失败: {exc}")
            else:
                cycle["stage"] = "accepted_no_push"
                cycle["running"] = False
                status_path = _persist_tail_status(cfg, cycle)
                cycle["status_path"] = str(status_path)
            if best_cycle is None or cycle.get("overlap_candidates", 0) > best_cycle.get("overlap_candidates", 0):
                best_cycle = cycle
        else:
            cycle["stage"] = "cycle_failed"
            cycle["running"] = False
            status_path = _persist_tail_status(cfg, cycle)
            cycle["status_path"] = str(status_path)
            _emit(f"{label} 未通过: {cycle.get('error', '')}")

        next_due = due_at + timedelta(seconds=max(1, interval))
        if not no_wait and datetime.now() > end:
            break
        if max_cycles is not None and cycles >= max_cycles:
            break
        if no_wait:
            time.sleep(interval)

    if push and best_cycle and pushes == 0 and accepted > 0:
        try:
            markdown = build_run_markdown(
                best_cycle.get("results", []),
                best_cycle.get("overlap", {}),
                diagnosis_results=best_cycle.get("diagnosis_results", []),
                sentiment_report=best_cycle.get("sentiment"),
                cfg=cfg,
                label=f"汇总 {accepted}/{cycles} 轮通过",
            )
            push_report = push_wecom_report(markdown, cfg)
            best_cycle["push_report"] = push_report
            if push_report.get("ok"):
                pushes += 1
                best_cycle["pushed"] = True
                best_cycle["pushed_at"] = datetime.now().isoformat(timespec="seconds")
                best_cycle["push_count"] = pushes
                best_cycle["stage"] = "summary_pushed"
                best_cycle["running"] = False
                status_path = _persist_tail_status(cfg, best_cycle)
                best_cycle["status_path"] = str(status_path)
                _emit(
                    f"汇总推送成功 | 通道 {push_report.get('ok_count', 0)}/"
                    f"{push_report.get('channel_count', 0)} | {push_report.get('elapsed_seconds', 0)}s"
                )
        except Exception as exc:
            _emit(f"汇总推送异常: {exc}")

    _banner("尾盘监控完成")
    _emit(f"轮次 {cycles} | 通过 {accepted} | 推送 {pushes}/{max_pushes}")
    ok = accepted > 0 and (not push or pushes >= min(min_pushes, max_pushes))
    final = dict(best_cycle or {})
    final.update({
        "label": "尾盘监控完成",
        "mode": watch_mode,
        "stage": "completed" if ok else "completed_below_min_pushes",
        "running": False,
        "ok": ok,
        "pushed": pushes > 0,
        "cycle_count": cycles,
        "accepted_cycle_count": accepted,
        "push_count": pushes,
        "min_pushes": min_pushes,
        "max_pushes": max_pushes,
        "started_at": watch_started_at,
        "error": "" if ok else f"推送轮次不足: {pushes}/{min_pushes}",
    })
    _persist_tail_current(cfg, final)
    return {"ok": ok, "cycle_count": cycles, "accepted_cycle_count": accepted, "push_count": pushes}


def _maybe_preheat_x1_before_tail(cfg: Dict[str, Any], tail_start: datetime) -> None:
    x1_cfg = (cfg.get("strategies") or {}).get("x1beam", {})
    preheat_cfg = x1_cfg.get("preheat") or {}
    if not bool(preheat_cfg.get("enabled", True)):
        return
    active_snapshot, _source = resolve_snapshot(cfg)
    status = x1_preheat_status(cfg, active_snapshot)
    if status.get("usable"):
        _emit("X1Beam 预热缓存已就绪，尾盘将作为第四策略参与")
        return

    start = _at(str(preheat_cfg.get("start_time", "14:20:00")))
    deadline = _at(str(preheat_cfg.get("deadline_time", "14:47:00")))
    now = datetime.now()
    if now >= tail_start or now > deadline:
        _emit("X1Beam 预热窗口已过，本次尾盘不临时全量计算 X1")
        return
    if now < start:
        wait = max(0, (start - now).total_seconds())
        _emit(f"等待 X1Beam 预热窗口 {start:%H:%M:%S}...")
        while wait > 0 and datetime.now() < tail_start:
            time.sleep(max(1, min(60, wait)))
            wait = max(0, (start - datetime.now()).total_seconds())

    remaining = min((deadline - datetime.now()).total_seconds(), (tail_start - datetime.now()).total_seconds() - 10)
    if remaining < 30:
        _emit("X1Beam 预热剩余时间不足，跳过预热，避免挤占尾盘推送")
        return
    timeout = int(min(float(preheat_cfg.get("timeout_seconds", 7200)), remaining))
    _emit(f"开始 X1Beam 尾盘前预热，最多 {timeout}s")
    manifest = run_x1_preheat(
        cfg,
        snapshot=active_snapshot,
        workers=int(preheat_cfg.get("workers", x1_cfg.get("workers", 1))),
        top_n=int(x1_cfg.get("top_n", 10)),
        keep_per_tier=int(preheat_cfg.get("keep_per_tier", 80)),
        timeout=timeout,
        time_budget=0,
        force=False,
        freeze=bool(preheat_cfg.get("freeze_snapshot", True)),
    )
    if manifest.get("completed"):
        _emit(f"X1Beam 预热完成: Top={manifest.get('top_count', 0)}")
    else:
        _emit(f"X1Beam 预热未完成: {manifest.get('error', '')}")


def _at(time_str: str) -> datetime:
    parts = [int(value) for value in time_str.split(":")]
    now = datetime.now()
    return now.replace(hour=parts[0], minute=parts[1], second=parts[2] if len(parts) > 2 else 0, microsecond=0)
