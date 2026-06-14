"""分仓之神 V2.0：四策略并行 + XGB 诊断验证层 + 交集分析。"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from src.boundary_audit import scan_boundary_candidates
from src.dashboard import run as run_dashboard
from src.diagnosis.annotate import annotate_all
from src.diagnosis.service import run_xgb_validation_layer
from src.diagnosis.xgb_calibration import build_xgb_calibration_status
from src.candidate_factor_panel import build_candidate_factor_panel
from src.health_audit import build_health_audit
from src.historical_pattern_tags import build_historical_pattern_tags
from src.native_snapshot import build_native_snapshot, promote_latest_native_snapshot
from src.pipeline import build_overlap_analysis, run_all_strategies
from src.quality_gate import audit_snapshot, format_quality_summary, resolve_snapshot
from src.settings import ensure_output_dirs, load_settings
from src.snapshot_manager import live_current_dir, prepare_live_snapshot
from src.tail_automation import run_tail_once, run_tail_watch
from src.tail_readiness import audit as tail_readiness_audit
from src.tail_readiness import build_push_markdown as build_readiness_md
from src.tracking_outcomes import outcome_report, update_outcomes
from src.tracking_store import ingest_pipeline_file, ingest_pipelines, summarize_tracking
from src.wecom_push import build_run_markdown, push_test_markdown, push_wecom
from src.x1_preheat import latest_status as x1_preheat_status, run_preheat as run_x1_preheat, select_tail_snapshot


STRATEGY_PRECHECKS = [
    ("src.strategies.v10_adapter.V10Adapter", "vip_screener_dir", "V10"),
    ("src.strategies.v1_adapter.V1Adapter", "legacy_screener_dir", "V1"),
    ("src.strategies.v4_adapter.V4Adapter", "legacy_screener_dir", "V4"),
    ("src.strategies.x1beam_adapter.X1BeamAdapter", "x1_xin_dir", "X1Beam"),
]


def _banner(title: str) -> None:
    print(f"\n{'=' * 62}\n  {title}\n{'=' * 62}", flush=True)


def _json_dir(cfg: dict) -> Path:
    out = Path(str(cfg["paths"].get("output_root", ROOT / "outputs"))) / "json"
    out.mkdir(parents=True, exist_ok=True)
    return out


def _write_pipeline(
    cfg: dict,
    snapshot_dir: Path,
    quality: dict,
    results: list[dict],
    summary: dict,
    overlap: dict,
    boundary: dict | None,
    diagnosis_summary: dict | None,
    diagnosis_results: list[dict] | None,
    x1_preheat: dict | None = None,
    *,
    prefix: str = "pipeline_v2",
) -> Path:
    path = _json_dir(cfg) / f"{prefix}_{datetime.now():%Y%m%d_%H%M%S}.json"
    payload = {
        "timestamp": summary.get("timestamp", datetime.now().isoformat(timespec="seconds")),
        "snapshot_dir": str(snapshot_dir),
        "snapshot_quality": quality,
        "strategies": results,
        "overlap": overlap,
        "summary": summary,
        "boundary": boundary,
        "diagnosis": diagnosis_summary,
        "diagnosis_results": diagnosis_results or [],
        "x1_preheat": x1_preheat,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _precheck_strategies(cfg: dict) -> int:
    ok_count = 0
    for cls_path, key, label in STRATEGY_PRECHECKS:
        try:
            mod_name, cls_name = cls_path.rsplit(".", 1)
            mod = __import__(mod_name, fromlist=[cls_name])
            adapter = getattr(mod, cls_name)(Path(str(cfg["paths"][key])), top_n=10)
            ok = adapter.validate_environment()
            print(f"  {label:<8} {'OK' if ok else 'MISSING'}  {cfg['paths'][key]}")
            ok_count += 1 if ok else 0
        except Exception as exc:
            print(f"  {label:<8} ERROR  {exc}")
    return ok_count


def _print_strategy_summary(results: list[dict], summary: dict, overlap: dict) -> None:
    print(
        f"策略成功: {summary['strategies_ok']}/{summary['strategies_run']} | "
        f"候选: {summary['total_candidates']} | "
        f"交集: {summary['overlap_candidates']} | "
        f"耗时: {summary['total_elapsed_seconds']:.1f}s\n"
    )
    for result in results:
        tag = "OK" if result.get("ok") else "FAIL"
        tops = [f"{row.get('code', '')} {row.get('name', '')}" for row in result.get("top", [])[:3]]
        print(
            f"  [{tag}] {result.get('display_name', result.get('strategy_name', '?')):<8} "
            f"{len(result.get('top', [])):>2}  {' | '.join(tops)}"
        )
        if result.get("error"):
            print(f"       error: {str(result['error'])[:180]}")

    if overlap.get("overlaps"):
        _banner("多策略交集 Top10")
        for item in overlap["overlaps"][:10]:
            strategies = "+".join(item.get("strategies", []))
            print(f"  {item['code']} {item.get('name', ''):<8} 覆盖 {item.get('strategy_count', 0)} [{strategies}]")


def cmd_run(args: argparse.Namespace) -> int:
    cfg = load_settings()
    ensure_output_dirs(cfg)
    if args.snapshot:
        snapshot_dir = Path(args.snapshot)
        snapshot_source = "arg"
    else:
        active_snapshot, _active_source = resolve_snapshot(cfg)
        snapshot_dir, snapshot_source, _preheat_status = select_tail_snapshot(cfg, active_snapshot)
    quality = audit_snapshot(snapshot_dir, cfg, official=not args.force)
    x1_status = x1_preheat_status(cfg, snapshot_dir)

    print(f"快照: {snapshot_dir} ({snapshot_source})")
    print(f"质量: {format_quality_summary(quality)}")
    for blocker in quality.get("blockers", []):
        print(f"  阻断: {blocker}")
    for warning in quality.get("warnings", []):
        print(f"  提醒: {warning}")

    if not quality.get("ok") and not args.force:
        print("\n数据质量未通过，正式流程已停止。需要临时排查时可加 --force，但尾盘出票不建议强制。")
        return 2
    if args.dry_run:
        return 0

    _banner("策略环境预检")
    _precheck_strategies(cfg)

    _banner("运行四策略")
    results, summary = run_all_strategies(snapshot_dir, cfg, parallel=not args.serial)
    overlap = build_overlap_analysis(results, cfg)
    _print_strategy_summary(results, summary, overlap)

    boundary = None
    try:
        boundary = scan_boundary_candidates(snapshot_dir, results)
        stats = boundary.get("stats", {})
        if stats.get("risk_count", 0):
            print(f"\n边界风险: 临界 {stats.get('critical', 0)}，风险 {stats.get('risk_count', 0)}")
            for row in boundary.get("risks", [])[:5]:
                print(f"  {row['code']} {row.get('name', '')} {row.get('pct', 0):.2f}% {','.join(row.get('risk_reasons', []))}")
    except Exception as exc:
        print(f"边界审计跳过: {exc}")

    diagnosis_results = []
    diagnosis_summary = None
    if not args.skip_diag:
        _banner("XGB 诊断验证层")
        try:
            diagnosis_objects, diagnosis_summary = run_xgb_validation_layer(
                cfg,
                results,
                snapshot_dir=snapshot_dir,
                candidates_source="V10/V1/V4/X1Beam Top candidates",
                persist=True,
            )
            diagnosis_results = [item.to_dict() if hasattr(item, "to_dict") else item for item in diagnosis_objects]
            results, overlap = annotate_all(results, overlap, diagnosis_results, diagnosis_summary)
            print(diagnosis_summary.get("summary") or f"诊断完成: {diagnosis_summary.get('total', 0)} 只")
            if diagnosis_summary.get("report_path"):
                print(f"诊断报告: {diagnosis_summary['report_path']}")
        except Exception as exc:
            diagnosis_summary = {"enabled": True, "role": "validation_layer", "error": str(exc)}
            print(f"XGB 诊断异常: {exc}")

    pipeline_path = _write_pipeline(
        cfg,
        snapshot_dir,
        quality,
        results,
        summary,
        overlap,
        boundary,
        diagnosis_summary,
        diagnosis_results[:50],
        x1_status,
    )
    print(f"\n结果文件: {pipeline_path}")
    try:
        tracking = ingest_pipeline_file(cfg, pipeline_path)
        print(f"追踪入库: 新增 {tracking.get('new_records', 0)} 条 | {tracking.get('tracking_file', '')}")
    except Exception as exc:
        print(f"追踪入库跳过: {exc}")

    if args.push:
        markdown = build_run_markdown(
            results,
            overlap,
            diagnosis_results=diagnosis_results[:50],
            cfg=cfg,
            label="手动触发",
        )
        ok = push_wecom(markdown, cfg)
        print(f"推送: {'OK' if ok else 'FAIL'}")

    min_ok = int(cfg.get("automation", {}).get("tail", {}).get("min_strategy_success", 2))
    return 0 if summary.get("strategies_ok", 0) >= min_ok else 2


def cmd_quality(args: argparse.Namespace) -> int:
    cfg = load_settings()
    snapshot_dir = Path(args.path) if args.path else resolve_snapshot(cfg)[0]
    report = audit_snapshot(snapshot_dir, cfg, official=not args.allow_stale)
    print(f"快照: {snapshot_dir}")
    print(f"质量: {format_quality_summary(report)}")
    metrics = report.get("metrics", {})
    print(json.dumps(metrics, ensure_ascii=False, indent=2))
    if report.get("blockers"):
        print("\n阻断项:")
        for item in report["blockers"]:
            print(f"  - {item}")
    if args.samples:
        print("\n样例:")
        print(json.dumps(report.get("samples", {}), ensure_ascii=False, indent=2))
    return 0 if report.get("ok") else 2


def cmd_diagnose(args: argparse.Namespace) -> int:
    cfg = load_settings()
    snapshot_dir = Path(args.snapshot) if args.snapshot else resolve_snapshot(cfg)[0]
    codes = [code.strip() for code in (args.codes or "").split(",") if code.strip()]
    if not codes:
        print("示例: python main.py diagnose --codes SH600000,SZ000001")
        return 1
    fake_results = [{"ok": True, "strategy_name": "manual", "top": [{"code": code, "name": ""} for code in codes]}]
    items, summary = run_xgb_validation_layer(
        cfg,
        fake_results,
        snapshot_dir=snapshot_dir,
        candidates_source="manual diagnose",
        persist=True,
    )
    print(summary.get("summary") or f"诊断完成: {len(items)} 只")
    if summary.get("report_path"):
        print(f"批量报告: {summary['report_path']}")
    for item in items:
        extra = item.extra or {}
        rich = extra.get("rich_report") or {}
        print(
            f"\n{item.code} {item.name}\n"
            f"  信号: {item.signal}  综合: {item.blended_score:.1%}\n"
            f"  模型: {item.model_score:.1%}  规则: {item.rule_score:.1%}"
        )
        events = extra.get("event_probabilities") or {}
        if events:
            print(
                f"  概率: 冲高5%={events.get('high5', 0):.1%} "
                f"收盘5%={events.get('close5', 0):.1%} "
                f"次日强度={events.get('next5', 0):.1%}"
            )
        if item.risk_flags:
            print(f"  风险: {', '.join(item.risk_flags)}")
        if rich.get("final_view"):
            print(f"  研判: {rich['final_view']}")
        if rich.get("markdown"):
            print(f"  明细: {rich['markdown']}")
    return 0


def cmd_xgb_calibration(args: argparse.Namespace) -> int:
    cfg = load_settings()
    report = build_xgb_calibration_status(
        cfg,
        top_n=args.top_n,
        run_backtest=args.run_backtest,
        start_date=args.start_date,
        min_stocks_per_day=args.min_stocks_per_day,
        timeout=args.timeout,
        persist=True,
    )
    print(
        f"XGB校准状态: 诊断资产={'OK' if report.get('ready_for_diagnosis') else '缺失'} | "
        f"硬信号依据={'OK' if report.get('ready_for_hard_signal') else '不足'}"
    )
    latest = report.get("latest_backtest") or {}
    if latest.get("exists"):
        print(f"最新回测: {latest.get('path', '')}")
        for target, stat in (latest.get("targets") or {}).items():
            print(
                f"  {target}: TopN={stat.get('daily_topN_mean')} "
                f"Base={stat.get('daily_baseline_mean')} Lift={stat.get('lift')} "
                f"胜出日={stat.get('days_beating_baseline')}"
            )
    else:
        print("最新回测: 未发现")
    if report.get("backtest_run"):
        run = report["backtest_run"]
        print(f"本次回测: {'OK' if run.get('ok') else 'FAIL'}")
        if run.get("stderr_tail"):
            print(run["stderr_tail"][-800:])
    if report.get("missing"):
        print("缺失项:")
        for item in report["missing"]:
            print(f"  - {item}")
    print("下一步:")
    for item in report.get("work_needed") or []:
        print(f"  - {item}")
    print(f"报告: {report.get('report_path', '')}")
    return 0 if report.get("ready_for_diagnosis") else 2


def cmd_tail_once(args: argparse.Namespace) -> int:
    cfg = load_settings()
    result = run_tail_once(cfg, push=args.push, label="cli")
    print(
        f"结果: {'OK' if result.get('ok') else 'FAIL'} | "
        f"耗时 {result.get('elapsed_seconds', 0):.1f}s | "
        f"推送 {'OK' if result.get('pushed') else 'SKIP'}"
    )
    if result.get("pipeline_path"):
        print(f"控制台结果已刷新: {result['pipeline_path']}")
    if result.get("error"):
        print(f"错误: {result['error']}")
    return 0 if result.get("ok") else 2


def cmd_tail_watch(args: argparse.Namespace) -> int:
    cfg = load_settings()
    result = run_tail_watch(cfg, push=args.push, no_wait=args.no_wait, max_cycles=args.max_cycles)
    print(
        f"\n尾盘监控: {'OK' if result.get('ok') else 'FAIL'} | "
        f"轮次 {result.get('cycle_count', 0)} | "
        f"通过 {result.get('accepted_cycle_count', 0)} | "
        f"推送 {result.get('push_count', 0)}"
    )
    return 0 if result.get("ok") else 2


def cmd_tail_ready(args: argparse.Namespace) -> int:
    cfg = load_settings()
    report = tail_readiness_audit(cfg)
    print(f"就绪状态: {report['status']} | 阻断: {report['blocking']}")
    for check in report["checks"]:
        level = "OK" if check.get("ok") else ("WARN" if check.get("warning") else "FAIL")
        print(f"  [{level}] {check['name']}: {check['detail']}")
    if args.push:
        ok = push_wecom(build_readiness_md(report), cfg)
        print(f"\n推送: {'OK' if ok else 'FAIL'}")
        return 0 if ok else 2
    return 0 if report.get("blocking", 0) == 0 else 2


def cmd_snapshot(args: argparse.Namespace) -> int:
    cfg = load_settings()
    snapshot_dir = Path(args.path) if args.path else resolve_snapshot(cfg)[0]
    if not snapshot_dir.exists():
        print(f"不存在: {snapshot_dir}")
        return 2
    files = sorted(snapshot_dir.glob("*.txt"))
    print(f"目录: {snapshot_dir}\n文件: {len(files)}")
    report = audit_snapshot(snapshot_dir, cfg, official=False)
    print(f"质量: {format_quality_summary(report)}")
    if args.stats:
        for size, name in sorted((path.stat().st_size, path.name) for path in files)[:30]:
            print(f"  {name}: {size}B")
    return 0


def cmd_snapshot_prepare(args: argparse.Namespace) -> int:
    cfg = load_settings()
    ensure_output_dirs(cfg)
    report = prepare_live_snapshot(
        cfg,
        source=Path(args.source) if args.source else None,
        force=args.force,
    )
    print(f"V2.0 快照准备: {'OK' if report.get('ok') else 'FAIL'}")
    print(f"来源: {report.get('source_label')} | {report.get('source_dir')}")
    print(f"目标: {report.get('target_dir')}")
    print(f"质量: {report.get('quality_summary') or report.get('error', '')}")
    if report.get("report_path"):
        print(f"报告: {report['report_path']}")
    return 0 if report.get("ok") else 2


def cmd_native_snapshot(args: argparse.Namespace) -> int:
    cfg = load_settings()
    ensure_output_dirs(cfg)
    report = build_native_snapshot(
        cfg,
        promote=args.promote,
        allow_single_source=args.allow_single_source,
        official=args.official,
        timeout=args.timeout,
        force_promote=args.force_promote,
    )
    print(f"V2.0 原生多源快照: {'OK' if report.get('ok') else 'FAIL'}")
    print(f"快照: {report.get('snapshot_dir') or '-'}")
    print(f"质量: {report.get('quality_summary') or '-'}")
    bridge = report.get("bridge_report") or {}
    print(
        f"生成={bridge.get('created_files', 0)} "
        f"复用={bridge.get('reused_files', 0)} "
        f"跳过={bridge.get('skipped_files', 0)} "
        f"交易日={bridge.get('trade_date') or '-'}"
    )
    sources = report.get("sources") or []
    if sources:
        print("数据源:")
        for item in sources:
            status = "OK" if item.get("ok") else "FAIL"
            print(f"  - {item.get('source')}: {status}, rows={item.get('rows', 0)}, error={item.get('error', '')}")
    promoted = report.get("promoted") or {}
    if args.promote:
        print(f"提升live: {'OK' if promoted.get('ok') else 'FAIL'} | {promoted.get('target_dir') or promoted.get('reason') or ''}")
    if report.get("report_path"):
        print(f"报告: {report['report_path']}")
    return 0 if report.get("ok") else 2


def cmd_native_promote(args: argparse.Namespace) -> int:
    cfg = load_settings()
    ensure_output_dirs(cfg)
    report = promote_latest_native_snapshot(cfg, force=args.force)
    print(f"提升最近原生快照: {'OK' if report.get('ok') else 'FAIL'}")
    print(f"来源报告: {report.get('report_path') or '-'}")
    print(f"来源快照: {report.get('snapshot_dir') or '-'}")
    quality = report.get("quality") or {}
    if quality:
        print(f"质量: {format_quality_summary(quality)}")
    promoted = report.get("promoted") or {}
    if promoted:
        print(f"目标: {promoted.get('target_dir') or '-'}")
    if report.get("reason"):
        print(f"原因: {report.get('reason')}")
    return 0 if report.get("ok") else 2


def cmd_pre_tail_prep(args: argparse.Namespace) -> int:
    cfg = load_settings()
    ensure_output_dirs(cfg)
    _banner("盘前/盘中快照准备")
    prep = prepare_live_snapshot(
        cfg,
        source=Path(args.source) if args.source else None,
        force=args.force,
    )
    print(f"快照准备: {'OK' if prep.get('ok') else 'FAIL'} | {prep.get('quality_summary') or prep.get('error', '')}")
    if not prep.get("ok"):
        return 2

    snapshot_dir = live_current_dir(cfg)
    quality = audit_snapshot(snapshot_dir, cfg, official=not args.allow_stale)
    print(f"正式质量闸门: {format_quality_summary(quality)}")
    for blocker in quality.get("blockers", []):
        print(f"  阻断: {blocker}")
    if not quality.get("ok") and not args.force:
        print("快照未达到正式尾盘条件，停止 X1Beam 预热。")
        return 2

    if not args.skip_x1:
        _banner("X1Beam 预热")
        x1_cfg = (cfg.get("strategies") or {}).get("x1beam", {})
        preheat_cfg = x1_cfg.get("preheat") or {}
        manifest = run_x1_preheat(
            cfg,
            snapshot=snapshot_dir,
            workers=args.workers or int(preheat_cfg.get("workers", x1_cfg.get("workers", 1))),
            top_n=int(x1_cfg.get("top_n", 10)),
            keep_per_tier=int(preheat_cfg.get("keep_per_tier", 80)),
            timeout=args.timeout or int(preheat_cfg.get("timeout_seconds", 7200)),
            time_budget=args.time_budget,
            force=bool(args.force or args.allow_stale),
            freeze=True,
        )
        print(f"X1Beam: completed={manifest.get('completed')} top={manifest.get('top_count', 0)} error={manifest.get('error', '')}")
        if not manifest.get("completed") and not args.allow_x1_fail:
            return 2

    _banner("健康审计")
    health = build_health_audit(cfg, official=not args.allow_stale, persist=True)
    print(f"健康审计: {health.get('status')} | 阻断 {health.get('blocking', 0)} | 提醒 {health.get('warnings', 0)}")
    print(f"报告: {health.get('report_path', '')}")
    return 0 if health.get("blocking", 0) == 0 else 2


def cmd_post_market_refresh(args: argparse.Namespace) -> int:
    cfg = load_settings()
    ensure_output_dirs(cfg)
    latest = not args.all
    _banner("盘后追踪入库")
    tracking = ingest_pipelines(cfg, latest=latest)
    print(f"追踪入库: pipeline={tracking.get('pipeline_count', 0)} records={tracking.get('records_built', 0)} new={tracking.get('new_records', 0)}")

    _banner("收益回填")
    outcomes = update_outcomes(
        cfg,
        max_days=args.max_days,
        one_day_profit_threshold=args.next_profit_threshold,
        five_day_target=args.five_day_target,
    )
    summary = outcomes.get("summary", {})
    print(f"收益: total={outcomes.get('outcome_count', 0)} tracked={summary.get('tracked_count', 0)} pending={summary.get('pending_count', 0)}")

    _banner("因子宽表")
    factors = build_candidate_factor_panel(cfg, latest=latest, selection_layer=args.selection_layer)
    print(f"因子宽表: rows={factors.get('row_count', 0)} | {factors.get('json_path', '')}")

    _banner("历史模式标签")
    tags = build_historical_pattern_tags(cfg, latest=latest, min_samples=args.min_samples, persist=True)
    print(f"模式标签: joined={tags.get('joined_outcome_count', 0)} groups={tags.get('group_count', 0)} candidates={len(tags.get('candidate_tags', []))}")

    _banner("健康审计")
    health = build_health_audit(cfg, official=False, persist=True)
    print(f"健康审计: {health.get('status')} | 阻断 {health.get('blocking', 0)} | 提醒 {health.get('warnings', 0)}")
    return 0 if health.get("blocking", 0) == 0 else 2


def cmd_x1_preheat(args: argparse.Namespace) -> int:
    cfg = load_settings()
    ensure_output_dirs(cfg)

    print("开始 X1Beam 预热...")
    x1_cfg = (cfg.get("strategies") or {}).get("x1beam", {})
    preheat_cfg = x1_cfg.get("preheat") or {}
    manifest = run_x1_preheat(
        cfg,
        snapshot=Path(args.snapshot) if args.snapshot else None,
        workers=args.workers or int(preheat_cfg.get("workers", x1_cfg.get("workers", 1))),
        top_n=args.top_n or int(x1_cfg.get("top_n", 10)),
        keep_per_tier=args.keep_per_tier or int(preheat_cfg.get("keep_per_tier", 80)),
        timeout=args.timeout or int(preheat_cfg.get("timeout_seconds", 7200)),
        time_budget=args.time_budget,
        force=args.force,
        freeze=not args.no_freeze,
    )
    sig = manifest.get("snapshot_signature") or {}
    print(f"快照: {sig.get('snapshot_dir') or manifest.get('source_snapshot_dir', '')}")
    print(f"质量: {manifest.get('quality_summary', '')}")
    print(
        f"预热缓存: {manifest.get('cache_path', '')}\n"
        f"完整={manifest.get('completed')} | 扫描={manifest.get('scanned_files', 0)}/{manifest.get('file_count', 0)} | "
        f"Top={manifest.get('top_count', 0)} | 耗时={manifest.get('elapsed_seconds', 0)}s"
    )
    if manifest.get("error"):
        print(f"提示: {manifest['error']}")
    return 0 if manifest.get("completed") is True and manifest.get("top_count", 0) > 0 else 2

def cmd_test_push(args: argparse.Namespace) -> int:
    cfg = load_settings()
    if args.dry_run:
        url = cfg.get("runtime", {}).get("wecom_webhook_url") or cfg.get("runtime", {}).get("wecom_webhook_urls") or ""
        print(f"Webhook: {url[:80] if url else '未配置'}")
        return 0
    ok = push_test_markdown(cfg)
    print(f"测试推送: {'OK' if ok else 'FAIL'}")
    return 0 if ok else 2


def cmd_dashboard(args: argparse.Namespace) -> int:
    run_dashboard(host=args.host, port=args.port, open_browser=not args.no_open)
    return 0


def cmd_tracking_ingest(args: argparse.Namespace) -> int:
    cfg = load_settings()
    ensure_output_dirs(cfg)
    paths = [Path(item) for item in args.pipeline] if args.pipeline else None
    report = ingest_pipelines(cfg, latest=not args.all and not paths, paths=paths)
    print(
        f"追踪入库完成: pipeline {report.get('pipeline_count', 0)} 个 | "
        f"新增 {report.get('new_records', 0)} 条 | 重复 {report.get('duplicate_records', 0)} 条"
    )
    print(f"追踪库: {report.get('tracking_file', '')}")
    print(f"报告: {report.get('report_path', '')}")
    return 0 if not report.get("errors") else 2


def cmd_tracking_report(args: argparse.Namespace) -> int:
    cfg = load_settings()
    report = summarize_tracking(cfg, days=args.days)
    print(
        f"追踪记录: {report.get('record_count', 0)} 条 | "
        f"pipeline {report.get('unique_pipeline_count', 0)} 个 | "
        f"股票 {report.get('unique_code_count', 0)} 只"
    )
    print(f"按层级: {json.dumps(report.get('by_selection_layer', {}), ensure_ascii=False)}")
    print(f"按策略数: {json.dumps(report.get('by_strategy_count', {}), ensure_ascii=False)}")
    print(f"按诊断: {json.dumps(report.get('by_diagnosis_signal', {}), ensure_ascii=False)}")
    print(f"报告: {report.get('report_path', '')}")
    return 0


def cmd_factor_panel(args: argparse.Namespace) -> int:
    cfg = load_settings()
    ensure_output_dirs(cfg)
    paths = [Path(item) for item in args.pipeline] if args.pipeline else None
    report = build_candidate_factor_panel(
        cfg,
        latest=not args.all and not paths,
        paths=paths,
        selection_layer=args.selection_layer,
    )
    print(f"候选因子宽表: {report.get('row_count', 0)} 行")
    print(f"JSON: {report.get('json_path', '')}")
    print(f"CSV: {report.get('csv_path', '')}")
    return 0


def cmd_outcome_update(args: argparse.Namespace) -> int:
    cfg = load_settings()
    ensure_output_dirs(cfg)
    report = update_outcomes(
        cfg,
        max_days=args.max_days,
        one_day_profit_threshold=args.next_profit_threshold,
        five_day_target=args.five_day_target,
    )
    summary = report.get("summary", {})
    overall = summary.get("overall", {})
    print(
        f"收益回填完成: {report.get('outcome_count', 0)} 条 | "
        f"可追踪 {summary.get('tracked_count', 0)} | "
        f"完整5日 {summary.get('completed_count', 0)} | "
        f"等待 {summary.get('pending_count', 0)}"
    )
    print(
        f"次日冲高盈利率: {overall.get('next_high_profit_rate', 0):.2%} | "
        f"5日5%达标率: {overall.get('five_day_5pct_hit_rate', 0):.2%}"
    )
    print(f"当前结果: {report.get('current_path', '')}")
    print(f"报告: {report.get('report_path', '')}")
    return 0


def cmd_outcome_report(args: argparse.Namespace) -> int:
    cfg = load_settings()
    report = outcome_report(cfg)
    summary = report.get("summary", {})
    overall = summary.get("overall", {})
    print(
        f"追踪收益: 总 {summary.get('outcome_count', 0)} | "
        f"可追踪 {summary.get('tracked_count', 0)} | "
        f"完整5日 {summary.get('completed_count', 0)} | "
        f"等待 {summary.get('pending_count', 0)}"
    )
    print(
        f"次日冲高盈利率: {overall.get('next_high_profit_rate', 0):.2%} | "
        f"5日5%达标率: {overall.get('five_day_5pct_hit_rate', 0):.2%} | "
        f"平均最大回撤: {overall.get('avg_max_5d_drawdown', 0):.2%}"
    )
    if args.detail:
        print("\n按层级:")
        print(json.dumps(summary.get("by_selection_layer", {}), ensure_ascii=False, indent=2))
        print("\n按策略数:")
        print(json.dumps(summary.get("by_strategy_count", {}), ensure_ascii=False, indent=2))
        print("\n按策略来源:")
        print(json.dumps(summary.get("by_strategy_source", {}), ensure_ascii=False, indent=2))
    return 0


def cmd_health(args: argparse.Namespace) -> int:
    cfg = load_settings()
    report = build_health_audit(cfg, official=args.official, persist=True)
    print(
        f"健康审计: {report.get('status')} | "
        f"阻断 {report.get('blocking', 0)} | 提醒 {report.get('warnings', 0)}"
    )
    for item in report.get("checks", []):
        if item.get("ok"):
            tag = "OK"
        elif item.get("warning"):
            tag = "WARN"
        else:
            tag = "FAIL"
        print(f"  [{tag}] {item.get('area')} / {item.get('name')}: {item.get('detail')}")
    print(f"报告: {report.get('report_path', '')}")
    return 0 if report.get("blocking", 0) == 0 else 2


def cmd_pattern_tags(args: argparse.Namespace) -> int:
    cfg = load_settings()
    report = build_historical_pattern_tags(
        cfg,
        latest=not args.all,
        min_samples=args.min_samples,
        persist=True,
    )
    print(
        f"历史模式标签: joined={report.get('joined_outcome_count', 0)} | "
        f"groups={report.get('group_count', 0)} | candidates={len(report.get('candidate_tags', []))}"
    )
    for row in report.get("candidate_tags", [])[:10]:
        positives = ",".join(str(item.get("label", "")) for item in row.get("positive_tags", [])[:2]) or "-"
        risks = ",".join(str(item.get("label", "")) for item in row.get("risk_tags", [])[:2]) or "-"
        print(
            f"  {row.get('code')} {row.get('name')} | "
            f"score={row.get('pattern_tag_score', 0)} | 正向={positives} | 风险={risks}"
        )
    print(f"报告: {report.get('report_path', '')}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="分仓之神 V2.0：V10/V1/V4/X1Beam 四策略 + XGB 诊断验证层")
    sub = parser.add_subparsers(dest="cmd")

    run = sub.add_parser("run", help="运行一次完整分析")
    run.add_argument("--snapshot")
    run.add_argument("--serial", action="store_true", help="串行运行四策略")
    run.add_argument("--skip-diag", action="store_true", help="跳过 XGB 诊断验证层")
    run.add_argument("--force", action="store_true", help="质量闸门不通过时仍强制运行，仅用于排查")
    run.add_argument("--push", action="store_true")
    run.add_argument("--dry-run", action="store_true")
    run.set_defaults(func=cmd_run)

    quality = sub.add_parser("quality", help="检查快照质量")
    quality.add_argument("--path")
    quality.add_argument("--allow-stale", action="store_true", help="不要求快照日期等于今天")
    quality.add_argument("--samples", action="store_true", help="显示异常样例")
    quality.set_defaults(func=cmd_quality)

    diagnose = sub.add_parser("diagnose", help="对指定股票做 XGB 诊断")
    diagnose.add_argument("--codes", required=True)
    diagnose.add_argument("--snapshot")
    diagnose.set_defaults(func=cmd_diagnose)

    xgb_cal = sub.add_parser("xgb-calibration", help="检查/运行 XGB 25分箱全量回测校准")
    xgb_cal.add_argument("--top-n", type=int, default=50)
    xgb_cal.add_argument("--run-backtest", action="store_true")
    xgb_cal.add_argument("--start-date", type=int, default=0, help="YYYYMMDD；0 表示使用训练配置里的 test_start")
    xgb_cal.add_argument("--min-stocks-per-day", type=int, default=500)
    xgb_cal.add_argument("--timeout", type=int, default=900)
    xgb_cal.set_defaults(func=cmd_xgb_calibration)

    tail_once = sub.add_parser("tail-once", help="尾盘单轮分析")
    tail_once.add_argument("--push", action="store_true")
    tail_once.set_defaults(func=cmd_tail_once)

    tail_watch = sub.add_parser("tail-watch", help="14:50-14:57 尾盘多轮监控")
    tail_watch.add_argument("--push", action="store_true")
    tail_watch.add_argument("--no-wait", action="store_true")
    tail_watch.add_argument("--max-cycles", type=int)
    tail_watch.set_defaults(func=cmd_tail_watch)

    tail_ready = sub.add_parser("tail-readiness", help="尾盘就绪审计")
    tail_ready.add_argument("--push", action="store_true")
    tail_ready.set_defaults(func=cmd_tail_ready)

    snapshot = sub.add_parser("snapshot", help="查看快照目录")
    snapshot.add_argument("--path")
    snapshot.add_argument("--stats", action="store_true")
    snapshot.set_defaults(func=cmd_snapshot)

    snapshot_prepare = sub.add_parser("snapshot-prepare", help="把只读上游多源快照导入为 V2.0 正式运行快照")
    snapshot_prepare.add_argument("--source", help="指定上游快照目录；默认按配置候选自动选择")
    snapshot_prepare.add_argument("--force", action="store_true", help="导入后质检不通过时仍保留，仅用于排查")
    snapshot_prepare.set_defaults(func=cmd_snapshot_prepare)

    native_snapshot = sub.add_parser("native-snapshot", help="V2.0 原生多源构建快照：tushare_rt_k 主源，多源校验补齐")
    native_snapshot.add_argument("--promote", action="store_true", help="质检通过后提升为 V2.0 live current 快照")
    native_snapshot.add_argument("--official", action="store_true", help="按正式尾盘口径要求快照交易日等于今天")
    native_snapshot.add_argument("--allow-single-source", action="store_true", help="仅排查用：允许单源通过上游校验")
    native_snapshot.add_argument("--force-promote", action="store_true", help="仅排查用：质检失败也尝试提升")
    native_snapshot.add_argument("--timeout", type=int, default=900)
    native_snapshot.set_defaults(func=cmd_native_snapshot)

    native_promote = sub.add_parser("native-promote", help="把最近一次 OK 的 V2.0 原生快照提升为 live current")
    native_promote.add_argument("--force", action="store_true", help="仅排查用：质检失败仍尝试提升")
    native_promote.set_defaults(func=cmd_native_promote)

    pre_tail = sub.add_parser("pre-tail-prep", help="尾盘前准备：V2快照 + 正式质检 + X1Beam预热 + 健康审计")
    pre_tail.add_argument("--source", help="指定上游快照目录；默认按配置候选自动选择")
    pre_tail.add_argument("--allow-stale", action="store_true", help="排查/非交易日使用：不要求快照日期等于今天")
    pre_tail.add_argument("--force", action="store_true", help="质量闸门不通过仍继续，仅用于排查")
    pre_tail.add_argument("--skip-x1", action="store_true", help="只准备快照和健康审计，不跑 X1Beam 预热")
    pre_tail.add_argument("--allow-x1-fail", action="store_true", help="X1 预热失败时不让命令失败")
    pre_tail.add_argument("--workers", type=int, default=0)
    pre_tail.add_argument("--timeout", type=int, default=0)
    pre_tail.add_argument("--time-budget", type=float, default=0)
    pre_tail.set_defaults(func=cmd_pre_tail_prep)

    x1_preheat = sub.add_parser("x1-preheat", help="非尾盘生成 X1Beam 完整预热缓存")
    x1_preheat.add_argument("--snapshot")
    x1_preheat.add_argument("--workers", type=int, default=0, help="0 表示使用配置 strategies.x1beam.preheat.workers")
    x1_preheat.add_argument("--top-n", type=int, default=10)
    x1_preheat.add_argument("--keep-per-tier", type=int, default=0, help="0 表示使用配置 strategies.x1beam.preheat.keep_per_tier")
    x1_preheat.add_argument("--timeout", type=int, default=0, help="0 表示使用配置 strategies.x1beam.preheat.timeout_seconds")
    x1_preheat.add_argument("--time-budget", type=float, default=0)
    x1_preheat.add_argument("--force", action="store_true")
    x1_preheat.add_argument("--no-freeze", action="store_true", help="排查用：不冻结快照，正式尾盘建议保持默认冻结")
    x1_preheat.set_defaults(func=cmd_x1_preheat)

    test_push = sub.add_parser("test-push", help="测试企业微信推送")
    test_push.add_argument("--dry-run", action="store_true")
    test_push.set_defaults(func=cmd_test_push)

    dashboard = sub.add_parser("dashboard", help="启动 Web 控制台")
    dashboard.add_argument("--host", default="127.0.0.1")
    dashboard.add_argument("--port", type=int, default=8766)
    dashboard.add_argument("--no-open", action="store_true")
    dashboard.set_defaults(func=cmd_dashboard)

    tracking_ingest = sub.add_parser("tracking-ingest", help="把 pipeline 候选写入 V2.0 追踪库")
    tracking_ingest.add_argument("--latest", action="store_true", help="只处理最新 pipeline；默认即为最新")
    tracking_ingest.add_argument("--all", action="store_true", help="回放全部 pipeline；默认只处理最新一个")
    tracking_ingest.add_argument("--pipeline", action="append", help="指定 pipeline_v2_*.json，可重复传入")
    tracking_ingest.set_defaults(func=cmd_tracking_ingest)

    tracking_report = sub.add_parser("tracking-report", help="汇总当前候选追踪库")
    tracking_report.add_argument("--days", type=int, default=0, help="只统计最近 N 天；0 表示全部")
    tracking_report.set_defaults(func=cmd_tracking_report)

    factor_panel = sub.add_parser("factor-panel", help="生成候选因子宽表")
    factor_panel.add_argument("--latest", action="store_true", help="只处理最新 pipeline；默认即为最新")
    factor_panel.add_argument("--all", action="store_true", help="回放全部 pipeline；默认只处理最新一个")
    factor_panel.add_argument("--pipeline", action="append", help="指定 pipeline_v2_*.json，可重复传入")
    factor_panel.add_argument(
        "--selection-layer",
        choices=["all", "strategy_top", "overlap"],
        default="all",
        help="宽表层级：全部、策略 Top、或多策略交集",
    )
    factor_panel.set_defaults(func=cmd_factor_panel)

    outcome_update = sub.add_parser("outcome-update", help="按快照 TXT 回填次日/5日收益追踪")
    outcome_update.add_argument("--max-days", type=int, default=5)
    outcome_update.add_argument("--next-profit-threshold", type=float, default=0.0, help="次日冲高盈利阈值，0 表示高于出票价")
    outcome_update.add_argument("--five-day-target", type=float, default=0.05, help="5日内最高收益目标，默认 5%")
    outcome_update.set_defaults(func=cmd_outcome_update)

    outcome_rep = sub.add_parser("outcome-report", help="汇总当前收益追踪结果")
    outcome_rep.add_argument("--detail", action="store_true")
    outcome_rep.set_defaults(func=cmd_outcome_report)

    post_market = sub.add_parser("post-market-refresh", help="盘后刷新：追踪入库、收益回填、因子宽表、历史模式标签")
    post_market.add_argument("--all", action="store_true", help="回放全部 pipeline；默认只处理最新")
    post_market.add_argument("--max-days", type=int, default=5)
    post_market.add_argument("--next-profit-threshold", type=float, default=0.0)
    post_market.add_argument("--five-day-target", type=float, default=0.05)
    post_market.add_argument("--selection-layer", choices=["all", "strategy_top", "overlap"], default="all")
    post_market.add_argument("--min-samples", type=int, default=3)
    post_market.set_defaults(func=cmd_post_market_refresh)

    health = sub.add_parser("health", help="端到端健康审计：数据、策略、XGB、X1Beam、追踪")
    health.add_argument("--official", action="store_true", help="按正式尾盘口径检查交易日")
    health.set_defaults(func=cmd_health)

    pattern_tags = sub.add_parser("pattern-tags", help="生成历史稳定模式标签并回贴最新候选")
    pattern_tags.add_argument("--all", action="store_true", help="候选回贴范围使用全部 pipeline；默认只回贴最新")
    pattern_tags.add_argument("--min-samples", type=int, default=3, help="形成历史标签的最小样本数")
    pattern_tags.set_defaults(func=cmd_pattern_tags)

    args = parser.parse_args()
    if not args.cmd:
        parser.print_help()
        return 0
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
