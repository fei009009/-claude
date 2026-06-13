from __future__ import annotations

import csv
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

from src.settings import load_settings

_CODE_RE = re.compile(r"(SH|SZ|BJ)?#?([0-9]{6})(?:\.(?:TXT|CSV))?(SH|SZ|BJ)?", re.IGNORECASE)


def norm_code(value: Any) -> str:
    text = str(value or "").strip().upper().replace(" ", "")
    text = text.replace(".TXT", "").replace("TXT", "").replace(".", "")
    match = _CODE_RE.search(text)
    if not match:
        return text
    prefix, code, suffix = match.groups()
    market = (prefix or suffix or ("SH" if code.startswith(("5", "6")) else "BJ" if code.startswith(("4", "8", "9")) else "SZ")).upper()
    return f"{market}{code}"


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        text = str(value).strip().replace("%", "").replace(",", "")
        if not text:
            return default
        return float(text)
    except Exception:
        return default


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(str(value).strip().replace("%", "")))
    except Exception:
        return default


_MOJIBAKE_MARKERS = (
    "鑺", "鍏", "娴", "榫", "鐢", "绉", "灞", "闀", "閫", "锛", "涓", "唤", "濂",
    "鐩", "鍦", "瓒", "鏅", "鎬", "浜", "椹", "绁", "姝", "闃", "鍩", "鏄", "鏂",
    "鍦", "鍒", "鍙", "鍚", "鏃", "鐣", "缇", "胯", "櫣", "庤", "彵", "\ufffd",
)


def has_mojibake(value: Any) -> bool:
    text = str(value or "")
    return bool(text) and any(marker in text for marker in _MOJIBAKE_MARKERS)


def repair_mojibake(value: Any) -> str:
    """Repair common UTF-8-as-GBK mojibake seen in stock names.

    Some legacy screeners or subprocess stdout paths may decode UTF-8 stock
    names as GBK before writing UTF-8 JSON/CSV. The transformed text is still
    reversible in many cases, e.g. "鑺卞洯鐢熺墿" -> "花园生物".
    """
    text = str(value or "")
    if not text:
        return ""
    if not has_mojibake(text):
        return text
    candidates = [text]
    for source_encoding in ("gb18030", "gbk"):
        try:
            repaired = text.encode(source_encoding, errors="strict").decode("utf-8", errors="strict")
            candidates.append(repaired)
        except Exception:
            continue
    return min(candidates, key=_mojibake_score)


def _mojibake_score(text: str) -> int:
    score = sum(text.count(marker) for marker in _MOJIBAKE_MARKERS) * 5
    score += sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff" and ch in _MOJIBAKE_MARKERS)
    score -= sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    return score


def pick(row: Dict[str, Any], names: Iterable[str], default: Any = "") -> Any:
    lower = {str(key).strip().lower(): key for key in row.keys()}
    for name in names:
        key = name if name in row else lower.get(str(name).lower())
        if key and row.get(key) not in (None, ""):
            return row.get(key)
    return default


def read_csv_rows(path: Path) -> Tuple[List[Dict[str, str]], str]:
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "gbk"):
        try:
            with path.open("r", encoding=encoding, newline="") as f:
                sample = f.read(4096)
                f.seek(0)
                dialect = csv.Sniffer().sniff(sample, delimiters=",\t;") if sample.strip() else csv.excel
                return list(csv.DictReader(f, dialect=dialect)), encoding
        except Exception:
            continue
    return [], ""


def latest_file(root: Path, patterns: Iterable[str]) -> Path | None:
    files: List[Path] = []
    if not root.exists():
        return None
    for pattern in patterns:
        files.extend(root.glob(pattern))
    if not files:
        return None
    return sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)[0]


def output_root(cfg: Dict[str, Any] | None = None) -> Path:
    cfg = cfg or load_settings()
    return Path(str((cfg.get("paths") or {}).get("output_root") or "outputs"))


def write_report(kind: str, payload: Dict[str, Any], cfg: Dict[str, Any] | None = None) -> Path:
    root = output_root(cfg) / "reports"
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{kind}_{datetime.now():%Y%m%d_%H%M%S}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path
