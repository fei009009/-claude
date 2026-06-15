"""V2.0 交互式控制台"""
from __future__ import annotations

import json, subprocess, sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PY = r"C:\Program Files\Python312\python.exe"
V1 = Path(r"D:\ZTFHQ\分仓之神")

def run(cmd: str) -> None:
    print(f"\n{'='*58}")
    print(f"  >>> python main.py {cmd}")
    print(f"{'='*58}\n")
    sys.stdout.flush()
    subprocess.run(f'"{PY}" main.py {cmd}', shell=True, cwd=str(ROOT))
    input("\n[回车] 返回菜单...")

def status() -> None:
    print(f"\n{'='*58}\n  分仓之神 V2.0 状态  {datetime.now():%Y-%m-%d %H:%M}\n{'='*58}")

    snap = V1 / "snapshots"
    if snap.exists():
        f = list(snap.glob("SH#*.txt"))
        m = snap / "snapshot_meta.json"
        td, gr, ps = "?", "?", "?"
        if m.exists():
            try:
                d = json.loads(m.read_text(encoding="utf-8")); td = d.get("trade_date","?")
                gr = d.get("grade","?"); ps = d.get("validation",{}).get("primary_source","?")
            except: pass
        ok = "正常" if td == datetime.now().strftime("%Y-%m-%d") and len(f) >= 2000 else "过期"
        print(f"  V1.0 快照: {len(f)} 文件 | {td} | 等级{gr} | {ps} | [{ok}]")
    else: print(f"  V1.0 快照: 缺失!")

    ws = V1 / "outputs" / "cache" / "tail_work_snapshots" / "work"
    if ws.exists(): print(f"  V1.0 工作: {len(list(ws.glob('SH#*.txt')))} 文件")

    out = ROOT / "outputs" / "json"
    pipes = sorted(out.glob("pipeline_v2_*.json"), key=lambda p: p.stat().st_mtime, reverse=True) if out.exists() else []
    if pipes:
        try:
            p = json.loads(pipes[0].read_text(encoding="utf-8"))
            s = p.get("summary",{}); dg = p.get("diagnosis"); diag = ""
            if dg and dg.get("results"):
                sb = sum(1 for x in dg["results"] if x.get("signal")=="STRONG_BUY")
                bu = sum(1 for x in dg["results"] if x.get("signal")=="BUY")
                if sb or bu: diag = f" | XGB: 强烈{sb}/推荐{bu}"
            print(f"  V2.0 最近: {s.get('strategies_ok',0)}/{s.get('strategies_run',0)} 策略 | {s.get('overlap_candidates',0)} 交集{diag}")
        except: pass
    else: print(f"  V2.0 最近: 暂无结果")

    scripts = [("V10", V1/"vendor"/"VIP"/"screener_vip_v10.py"),("V1", V1/"vendor"/"legacy_screeners"/"screener_app.py"),
               ("V4", V1/"vendor"/"legacy_screeners"/"screener_v4.py"),("X1B", Path(r"D:\ZTFHQ\X1-XIN\screener_beam.py"))]
    parts = [f"{n}={'有' if p.exists() else '缺'}" for n,p in scripts]
    print(f"  筛选脚本: {'  '.join(parts)}")
    xgb = Path(r"D:\ZTFHQ\XGB\model_v2\xgb_5d_v2.json")
    print(f"  XGB模型: {'有' if xgb.exists() else '缺'}")
    env = ROOT / "config" / ".env"
    if env.exists():
        t = env.read_text(encoding="utf-8"); ch = t.count("https://qyapi.weixin.qq.com")
        print(f"  企微推送: {ch} 通道")
    print()

def menu() -> None:
    while True:
        print("  [1] 全部运行    [2] 质量检查    [3] 个股诊断")
        print("  [4] 尾盘单次    [5] 尾盘监控    [6] 快照管理")
        print("  [7] 分析报告    [8] 控制台      [9] 测试推送")
        print("  [R] 就绪审计    [S] 状态概览    [Q] 退出")
        c = input("\n  选择 [1-9/R/S/Q]: ").strip().upper()
        if c=="1": run("run --push")
        elif c=="2": run("quality")
        elif c=="3":
            codes = input("股票代码 (如 SH600000,SZ000001): ").strip()
            if codes: run(f"diagnose --codes {codes}")
        elif c=="4": run("tail-once --push")
        elif c=="5":
            m = input("测试1轮[T] / 完整[F]? ").strip().upper()
            run("tail-watch --push --no-wait --max-cycles 1" if m=="T" else "tail-watch --push")
        elif c=="6": run("snapshot --stats")
        elif c=="7": run("report --type daily --days 7 --persist")
        elif c=="8": subprocess.Popen(f'"{PY}" main.py dashboard', shell=True, cwd=str(ROOT))
        elif c=="9": run("test-push")
        elif c=="R": run("tail-readiness --push")
        elif c=="S": status()
        elif c=="Q": print("\n再见。\n"); break

if __name__ == "__main__":
    status(); menu()
