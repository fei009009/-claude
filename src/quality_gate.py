"""Snapshot quality gate for V2.0 formal tail picking.

The gate is intentionally stricter than a plain file-count check: it verifies
freshness, empty files, previous-day continuity and zero close prices before the
four strategy adapters are allowed to use a snapshot.
"""
from __future__ import annotations

import json
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


def quality_config(cfg: Dict[str, Any]) -> Dict[str, Any]:
    tail = (cfg.get("automation") or {}).get("tail") or {}
    q = tail.get("quality") or {}
    return {
        "min_snapshot_files": int(q.get("min_snapshot_files", 2000)),
        "max_empty_ratio": float(q.get("max_empty_ratio", 0.05)),
        "require_today_trade_date": bool(q.get("require_today_trade_date", True)),
        "max_consecutive_failures": int(q.get("max_consecutive_failures", 3)),
        "max_prev_gap_days": int(q.get("max_prev_gap_days", 4)),
        "max_stale_ratio": float(q.get("max_stale_ratio", 0.02)),
        "max_discontinuous_ratio": float(q.get("max_discontinuous_ratio", 0.02)),
        "max_zero_close_rows": int(q.get("max_zero_close_rows", 0)),
    }


def resolve_snapshot(cfg: Dict[str, Any]) -> Tuple[Path, str]:
    paths = cfg.get("paths") or {}
    candidates = [
        (Path(str(paths.get("snapshot_root") or "")), "snapshot_root"),
        (Path(str(paths.get("v1_work") or "")), "v1_work"),
        (Path(str(paths.get("v1_snapshot") or "")), "v1_snapshot"),
    ]
    for path, label in candidates:
        if path.exists() and len(list(path.glob("SH#*.txt"))) >= 100:
            return path, label
    for path, label in candidates:
        if path.exists() and list(path.glob("*.txt")):
            return path, label
    return candidates[0]


def audit_snapshot(snapshot_dir: Path, cfg: Dict[str, Any], *, official: bool = True) -> Dict[str, Any]:
    q = quality_config(cfg)
    snapshot_dir = Path(snapshot_dir)
    blockers: List[str] = []
    warnings: List[str] = []
    samples: Dict[str, List[Dict[str, Any]]] = {
        "stale": [],
        "missing_previous": [],
        "discontinuous": [],
        "zero_close": [],
        "empty": [],
    }

    if not snapshot_dir.exists():
        return _finish(False, [f"快照目录不存在: {snapshot_dir}"], warnings, samples, {
            "snapshot_dir": str(snapshot_dir),
            "file_count": 0,
            "checked_files": 0,
        })

    files = sorted(p for p in snapshot_dir.glob("*.txt") if p.is_file())
    file_count = len(files)
    empty_files = [p for p in files if _safe_size(p) < 50]
    empty_ratio = len(empty_files) / max(file_count, 1)
    for p in empty_files[:8]:
        samples["empty"].append({"file": p.name, "size": _safe_size(p)})

    if file_count < q["min_snapshot_files"]:
        blockers.append(f"快照文件不足: {file_count} < {q['min_snapshot_files']}")
    if empty_ratio > q["max_empty_ratio"]:
        blockers.append(f"空文件率过高: {len(empty_files)}/{file_count} = {empty_ratio:.2%}")

    meta = load_snapshot_meta(snapshot_dir)
    meta_trade_date = _parse_date(meta.get("trade_date"))
    today = date.today()

    checked = 0
    last_dates: Counter[date] = Counter()
    stale_count = 0
    missing_prev_count = 0
    discontinuous_count = 0
    zero_close_count = 0

    row_cache: List[Tuple[Path, Optional[Dict[str, Any]], Optional[Dict[str, Any]]]] = []
    for path in files:
        if _safe_size(path) < 50:
            continue
        prev_row, last_row = _last_two_data_rows(path)
        if not last_row:
            missing_prev_count += 1
            _sample(samples["missing_previous"], path, "no_last_row")
            continue
        checked += 1
        last_dates[last_row["date"]] += 1
        row_cache.append((path, prev_row, last_row))

    observed_trade_date = last_dates.most_common(1)[0][0] if last_dates else None
    expected_trade_date = meta_trade_date or observed_trade_date

    if official and q["require_today_trade_date"]:
        if expected_trade_date != today:
            blockers.append(
                f"交易日不匹配: 快照={_fmt_date(expected_trade_date)} 今日={today.isoformat()}"
            )

    for path, prev_row, last_row in row_cache:
        if expected_trade_date and last_row and last_row["date"] != expected_trade_date:
            stale_count += 1
            _sample(samples["stale"], path, f"last={last_row['date'].isoformat()}")
        if not prev_row:
            missing_prev_count += 1
            _sample(samples["missing_previous"], path, "missing_prev_row")
        else:
            gap = (last_row["date"] - prev_row["date"]).days
            if gap <= 0 or gap > q["max_prev_gap_days"] or prev_row.get("close", 0) <= 0:
                discontinuous_count += 1
                _sample(
                    samples["discontinuous"],
                    path,
                    f"prev={prev_row['date'].isoformat()} last={last_row['date'].isoformat()} gap={gap}",
                )
        if last_row and last_row.get("close", 0) <= 0:
            zero_close_count += 1
            _sample(samples["zero_close"], path, f"close={last_row.get('close')}")

    stale_ratio = stale_count / max(checked, 1)
    missing_prev_ratio = missing_prev_count / max(checked, 1)
    discontinuous_ratio = discontinuous_count / max(checked, 1)

    if checked == 0:
        blockers.append("没有可解析的日线数据")
    if zero_close_count > q["max_zero_close_rows"]:
        blockers.append(f"尾行零收盘价: {zero_close_count} 只，不能进入正式出票")
    if stale_ratio > q["max_stale_ratio"]:
        blockers.append(f"非当日/非目标交易日数据过多: {stale_count}/{checked} = {stale_ratio:.2%}")
    if missing_prev_ratio > q["max_discontinuous_ratio"]:
        blockers.append(f"缺少前一交易日K线过多: {missing_prev_count}/{checked} = {missing_prev_ratio:.2%}")
    if discontinuous_ratio > q["max_discontinuous_ratio"]:
        blockers.append(f"最近交易日不连续过多: {discontinuous_count}/{checked} = {discontinuous_ratio:.2%}")

    if stale_count:
        warnings.append(f"有 {stale_count} 只股票尾行日期不是目标交易日")
    if discontinuous_count:
        warnings.append(f"有 {discontinuous_count} 只股票最近两根K线间隔异常")

    metrics = {
        "snapshot_dir": str(snapshot_dir),
        "file_count": file_count,
        "empty_files": len(empty_files),
        "empty_ratio": round(empty_ratio, 6),
        "checked_files": checked,
        "meta_trade_date": _fmt_date(meta_trade_date),
        "observed_trade_date": _fmt_date(observed_trade_date),
        "expected_trade_date": _fmt_date(expected_trade_date),
        "today": today.isoformat(),
        "stale_count": stale_count,
        "stale_ratio": round(stale_ratio, 6),
        "missing_previous_count": missing_prev_count,
        "missing_previous_ratio": round(missing_prev_ratio, 6),
        "discontinuous_count": discontinuous_count,
        "discontinuous_ratio": round(discontinuous_ratio, 6),
        "zero_close_count": zero_close_count,
        "last_date_counts": {d.isoformat(): n for d, n in last_dates.most_common(5)},
        "quality_config": q,
    }
    return _finish(len(blockers) == 0, blockers, warnings, samples, metrics, meta=meta)


def load_snapshot_meta(snapshot_dir: Path) -> Dict[str, Any]:
    meta_path = Path(snapshot_dir) / "snapshot_meta.json"
    if not meta_path.exists():
        return {}
    try:
        return json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def format_quality_summary(report: Dict[str, Any]) -> str:
    m = report.get("metrics") or {}
    status = "通过" if report.get("ok") else "不合格"
    return (
        f"{status}: {m.get('file_count', 0)} 文件, 空文件 {m.get('empty_files', 0)}, "
        f"交易日 {m.get('expected_trade_date') or '?'}, "
        f"断档 {m.get('discontinuous_count', 0)}, 零价 {m.get('zero_close_count', 0)}"
    )


def _finish(
    ok: bool,
    blockers: List[str],
    warnings: List[str],
    samples: Dict[str, List[Dict[str, Any]]],
    metrics: Dict[str, Any],
    *,
    meta: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    return {
        "ok": ok,
        "status": "pass" if ok else "blocked",
        "blocking": len(blockers),
        "blockers": blockers,
        "warnings": warnings,
        "samples": samples,
        "metrics": metrics,
        "meta": meta or {},
    }


def _safe_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _sample(bucket: List[Dict[str, Any]], path: Path, reason: str) -> None:
    if len(bucket) < 8:
        bucket.append({"file": path.name, "reason": reason})


def _parse_date(value: Any) -> Optional[date]:
    if value in (None, ""):
        return None
    text = str(value).strip().replace("/", "-")
    try:
        return datetime.strptime(text[:10], "%Y-%m-%d").date()
    except Exception:
        return None


def _fmt_date(value: Optional[date]) -> str:
    return value.isoformat() if value else ""


def _last_two_data_rows(path: Path) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    rows: List[Dict[str, Any]] = []
    text = _read_tail_text(path)
    for line in text.splitlines():
        row = _parse_data_line(line)
        if row:
            rows.append(row)
    if not rows:
        return None, None
    if len(rows) == 1:
        return None, rows[-1]
    return rows[-2], rows[-1]


def _read_tail_text(path: Path, max_bytes: int = 8192) -> str:
    try:
        with path.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            data = f.read()
        for enc in ("gb18030", "utf-8", "gbk"):
            try:
                return data.decode(enc, errors="replace")
            except Exception:
                continue
    except Exception:
        return ""
    return ""


def _parse_data_line(line: str) -> Optional[Dict[str, Any]]:
    line = line.strip()
    if not line or "\t" not in line:
        return None
    parts = line.split("\t")
    if len(parts) < 5:
        return None
    dt = _parse_date(parts[0])
    if not dt:
        return None
    try:
        return {
            "date": dt,
            "open": float(parts[1]),
            "high": float(parts[2]),
            "low": float(parts[3]),
            "close": float(parts[4]),
            "volume": float(parts[5]) if len(parts) > 5 and parts[5] else 0.0,
        }
    except Exception:
        return None
