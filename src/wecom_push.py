"""Enterprise WeChat markdown push for V2.0."""
from __future__ import annotations

import time
from datetime import datetime
from typing import Any, Dict, List, Optional

try:
    import requests
except Exception:  # pragma: no cover - dependency may be absent in tests
    requests = None

from src.sentiment_overlay import sentiment_summary


XGB_CONFIRM_SIGNALS = {"STRONG_BUY", "BUY"}


def _fmt_pct(value: Any) -> str:
    if value in (None, ""):
        return "-"
    try:
        number = float(value)
        return f"{number:+.2f}%"
    except Exception:
        return str(value)


def _short(text: Any, n: int = 10) -> str:
    value = str(text or "")
    return value if len(value) <= n else value[: max(1, n - 1)] + "."


def _signal_tag(signal: str) -> str:
    return {"STRONG_BUY": "强确认", "BUY": "确认", "WATCH": "观察"}.get(signal, signal or "-")


def _xgb_map(diagnosis_results: Optional[List[dict]]) -> Dict[str, dict]:
    mapping: Dict[str, dict] = {}
    for item in diagnosis_results or []:
        code = str(item.get("code") or "")
        if code:
            mapping[code] = item
    return mapping


def build_run_markdown(
    results: List[dict],
    overlap: dict,
    diagnosis_results: Optional[List[dict]] = None,
    sentiment_report: Optional[dict] = None,
    cfg: Optional[dict] = None,
    test: bool = False,
    label: str = "",
) -> str:
    now = datetime.now().strftime("%m-%d %H:%M:%S")
    prefix = "[TEST] " if test else ""
    lines = [
        f"## {prefix}分仓之神 V2.0 尾盘分析",
        f"> {now}" + (f" | {label}" if label else ""),
        "",
    ]

    ok_count = sum(1 for item in results if item.get("ok"))
    lines.append(f"**四策略状态**：{ok_count}/{len(results)} 成功")
    for item in results:
        name = item.get("display_name", item.get("strategy_name", "?"))
        status = "OK" if item.get("ok") else "FAIL"
        lines.append(f"- {name}: {status} | 候选 {len(item.get('top', []))} | 耗时 {float(item.get('elapsed_seconds', 0) or 0):.1f}s")

    sent = sentiment_summary(sentiment_report)
    if sent.get("ok"):
        align = "对齐" if sent.get("fresh_for_snapshot") else "需核对"
        lines.append("")
        lines.append(
            f"**情绪周期**：{sent.get('date') or '-'} {sent.get('state') or '-'}"
            f"({sent.get('value', 0)}) | 风险偏好 {sent.get('risk_appetite') or '-'} | "
            f"仓位系数 {float(sent.get('position_multiplier') or 0):.2f} | {align}"
        )
        lines.append(
            f"> 涨停 {sent.get('uplimit_num', 0)} / 跌停 {sent.get('downlimit_num', 0)} / "
            f"炸板 {sent.get('fried_board_num', 0)}；{sent.get('tail_guidance') or ''}"
        )
    elif sentiment_report:
        lines.append("")
        lines.append("**情绪周期**：暂不可用，候选不做情绪降权。")

    xgb_by_code = _xgb_map(diagnosis_results)
    xgb_confirmed = {code for code, item in xgb_by_code.items() if item.get("signal") in XGB_CONFIRM_SIGNALS}
    overlaps = overlap.get("overlaps", []) or []
    consensus_3_plus = [item for item in overlaps if int(item.get("strategy_count", 0)) >= 3]
    consensus_2 = [item for item in overlaps if int(item.get("strategy_count", 0)) == 2]

    tier1 = [item for item in consensus_3_plus if item.get("code") in xgb_confirmed]
    tier2 = [item for item in consensus_3_plus if item.get("code") not in xgb_confirmed]
    tier3 = [item for item in consensus_2 if item.get("code") in xgb_confirmed]

    lines.append("")
    lines.append("### 核心候选")
    if tier1:
        lines.append(f"**★★★★ 极高**（{len(tier1)} 只：3+策略共识 + XGB双重确认）")
        for item in tier1[:6]:
            lines.append(_format_overlap_line(item, xgb_by_code, strong=True))
    if tier2:
        lines.append(f"\n**★★★ 高**（{len(tier2)} 只：3+策略共识）")
        for item in tier2[:6]:
            lines.append(_format_overlap_line(item, xgb_by_code))
    if tier3:
        lines.append(f"\n**★★ 中**（{len(tier3)} 只：2策略共识 + XGB确认）")
        for item in tier3[:6]:
            lines.append(_format_overlap_line(item, xgb_by_code))
    if not (tier1 or tier2 or tier3):
        lines.append("> 暂无高置信共识候选，建议观望或只做复盘观察。")

    for item in results:
        top = item.get("top", []) or []
        if not item.get("ok") or not top:
            continue
        name = item.get("display_name", item.get("strategy_name", "?"))
        lines.append("")
        lines.append(f"### {name} Top3")
        for idx, row in enumerate(top[:3], 1):
            extra = ""
            if row.get("lift_score") not in (None, ""):
                try:
                    extra = f" Lift={float(row['lift_score']):.2f}"
                except Exception:
                    extra = f" Lift={row['lift_score']}"
            elif row.get("wr") not in (None, ""):
                try:
                    extra = f" WR={float(row['wr']):.0%}"
                except Exception:
                    extra = f" WR={row['wr']}"
            env = row.get("sentiment_action")
            env_text = f" | {env}" if env else ""
            lines.append(f"{idx}. {row.get('code', '-')} {_short(row.get('name', ''), 8)} {_fmt_pct(row.get('pct_chg'))}{extra}{env_text}")

    risk_items = [item for item in (diagnosis_results or []) if item.get("risk_flags")]
    if risk_items:
        lines.append("")
        lines.append("### XGB诊断风险提示")
        for item in risk_items[:6]:
            lines.append(f"- {item.get('code')} {_short(item.get('name', ''), 8)}: {', '.join(item.get('risk_flags', []))}")

    lines.append("")
    lines.append("---")
    lines.append("> XGB 仅作为诊断验证层，不参与四策略交集计数。")
    lines.append("> 情绪周期仅作为市场环境、仓位系数和风险降权层，不作为第五个选股策略。")
    lines.append("> 自动化结果仅供复盘和投资参考，不构成任何买卖建议。")

    markdown = "\n".join(lines)
    max_chars = int((cfg or {}).get("push", {}).get("markdown_max_chars", 3500))
    if len(markdown) > max_chars:
        markdown = markdown[: max_chars - 40] + "\n\n> 内容较长已截断"
    return markdown


def _format_overlap_line(item: dict, xgb_by_code: Dict[str, dict], *, strong: bool = False) -> str:
    code = item.get("code", "")
    strategies = "+".join(str(name).upper() for name in item.get("strategies", []))
    diag = xgb_by_code.get(code)
    suffix = ""
    if diag:
        score = diag.get("blended_score", diag.get("score", 0))
        try:
            score_text = f"{float(score):.0%}"
        except Exception:
            score_text = str(score)
        suffix = f" | XGB {score_text} {_signal_tag(str(diag.get('signal', '')))}"
    sent = item.get("sentiment_context") or {}
    if sent:
        try:
            env_score = f"{float(sent.get('tradeability_score') or 0):.2f}"
        except Exception:
            env_score = "-"
        suffix += f" | 情绪 {sent.get('action') or '-'} {env_score}"
    lead = "- **" if strong else "- "
    tail = "**" if strong else ""
    return f"{lead}{code}{tail} {_short(item.get('name', ''), 8)} [{strategies}]{suffix}"


def _parse_urls(cfg: Dict[str, Any]) -> List[str]:
    runtime = cfg.get("runtime", {})
    urls: List[str] = []
    multi = str(runtime.get("wecom_webhook_urls") or "").strip()
    if multi:
        urls.extend([item.strip() for item in multi.split(";") if item.strip()])
    single = str(runtime.get("wecom_webhook_url") or "").strip()
    if single:
        urls.append(single)
    return urls


def push_wecom(markdown: str, cfg: Dict[str, Any], url: Optional[str] = None, retries: Optional[int] = None) -> bool:
    return bool(push_wecom_report(markdown, cfg, url=url, retries=retries).get("ok"))


def push_wecom_report(markdown: str, cfg: Dict[str, Any], url: Optional[str] = None, retries: Optional[int] = None) -> Dict[str, Any]:
    started = time.perf_counter()
    if requests is None:
        print("[wecom] requests 未安装")
        return {
            "ok": False,
            "error": "requests 未安装",
            "channel_count": 0,
            "ok_count": 0,
            "failed_count": 0,
            "skipped_count": 0,
            "channels": [],
            "elapsed_seconds": 0,
        }

    push_cfg = cfg.get("push", {})
    urls = [url] if url else _parse_urls(cfg)
    if not urls:
        print("[wecom] 未配置 webhook")
        return {
            "ok": False,
            "error": "未配置 webhook",
            "channel_count": 0,
            "ok_count": 0,
            "failed_count": 0,
            "skipped_count": 0,
            "channels": [],
            "elapsed_seconds": 0,
        }

    retries = int(retries if retries is not None else push_cfg.get("retry_attempts", 2))
    timeout = float(push_cfg.get("request_timeout_seconds", 10))
    max_total = float(push_cfg.get("max_total_seconds", 0) or 0)
    deadline = started + max_total if max_total > 0 else 0.0
    payload = {"msgtype": "markdown", "markdown": {"content": markdown}}
    channels: List[Dict[str, Any]] = []

    for index, target in enumerate(urls, 1):
        label = f"ch{index}" if len(urls) > 1 else "default"
        channel_started = time.perf_counter()
        channel: Dict[str, Any] = {"label": label, "ok": False, "attempts": 0}
        if deadline and time.perf_counter() >= deadline:
            channel.update({"skipped": True, "error": "push budget exhausted before channel"})
            channels.append(_finish_channel(channel, channel_started))
            print(f"[wecom] {label} SKIP budget exhausted")
            continue
        for attempt in range(retries + 1):
            channel["attempts"] = attempt + 1
            remaining = deadline - time.perf_counter() if deadline else timeout
            if deadline and remaining < 1:
                channel.update({"skipped": True, "error": "push budget exhausted"})
                print(f"[wecom] {label} SKIP budget exhausted")
                break
            req_timeout = min(timeout, max(1.0, remaining)) if deadline else timeout
            try:
                response = requests.post(target, json=payload, timeout=req_timeout)
                try:
                    data = response.json() if response.content else {}
                except Exception:
                    data = {"raw": response.text[:200]}
                channel.update(
                    {
                        "status_code": response.status_code,
                        "errcode": data.get("errcode"),
                        "errmsg": data.get("errmsg", ""),
                    }
                )
                if response.status_code == 200 and data.get("errcode") == 0:
                    print(f"[wecom] {label} OK")
                    channel["ok"] = True
                    break
                if data.get("errcode") == 45009 and attempt < retries:
                    _sleep_with_budget(3, deadline)
                    continue
                print(f"[wecom] {label} FAIL {response.status_code} {data}")
                channel["error"] = f"FAIL {response.status_code} {data}"
                break
            except requests.exceptions.Timeout:
                channel["timed_out"] = True
                if attempt < retries:
                    _sleep_with_budget(2, deadline)
                    continue
                print(f"[wecom] {label} TIMEOUT")
                channel["error"] = "TIMEOUT"
            except Exception as exc:
                print(f"[wecom] {label} ERROR {exc}")
                channel["error"] = str(exc)
                break
        channels.append(_finish_channel(channel, channel_started))

    ok_count = sum(1 for item in channels if item.get("ok"))
    skipped_count = sum(1 for item in channels if item.get("skipped"))
    failed_count = len(channels) - ok_count - skipped_count
    report = {
        "ok": ok_count > 0,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "channel_count": len(urls),
        "ok_count": ok_count,
        "failed_count": failed_count,
        "skipped_count": skipped_count,
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "timeout_seconds": timeout,
        "max_total_seconds": max_total,
        "retries": retries,
        "channels": channels,
    }
    print(
        f"[wecom] summary ok={report['ok']} channels={ok_count}/{len(urls)} "
        f"failed={failed_count} skipped={skipped_count} elapsed={report['elapsed_seconds']}s"
    )
    return report


def _sleep_with_budget(seconds: float, deadline: float) -> None:
    if not deadline:
        time.sleep(seconds)
        return
    remaining = deadline - time.perf_counter()
    if remaining > 0:
        time.sleep(max(0, min(seconds, remaining)))


def _finish_channel(channel: Dict[str, Any], started: float) -> Dict[str, Any]:
    channel["elapsed_seconds"] = round(time.perf_counter() - started, 3)
    return channel


def push_test_markdown(cfg: Dict[str, Any]) -> bool:
    markdown = (
        "## 分仓之神 V2.0 推送测试\n\n"
        f"> {datetime.now():%Y-%m-%d %H:%M:%S}\n\n"
        "企业微信 Webhook 通道可用。"
    )
    return push_wecom(markdown, cfg)
