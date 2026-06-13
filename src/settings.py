"""分仓之神 V2.0 — 配置加载与环境变量"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict

try:
    import yaml
except Exception:
    yaml = None


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = PROJECT_ROOT / "config" / "config.yaml"
ENV_PATH = PROJECT_ROOT / "config" / ".env"


def _load_dotenv(path: Path) -> None:
    """加载 .env 文件到 os.environ（不覆盖已有环境变量）"""
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def load_settings(config_path: Path | None = None) -> Dict[str, Any]:
    """加载全局配置

    Args:
        config_path: 自定义配置文件路径，默认 PROJECT_ROOT/config/config.yaml

    Returns:
        dict: 合并了 .env 环境变量的完整配置
    """
    _load_dotenv(ENV_PATH)
    path = config_path or CONFIG_PATH
    if yaml is None:
        raise RuntimeError("PyYAML is not installed; configuration loading is unavailable")
    with path.open("r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}

    # 填充运行时环境变量
    cfg.setdefault("runtime", {})
    cfg["runtime"]["project_root"] = str(PROJECT_ROOT)
    cfg["runtime"]["tushare_token"] = os.getenv("TUSHARE_TOKEN", "")
    cfg["runtime"]["zzshare_token"] = os.getenv("ZZSHARE_TOKEN", "")
    cfg["runtime"]["wecom_webhook_url"] = os.getenv("WECOM_WEBHOOK_URL", "")
    cfg["runtime"]["wecom_webhook_urls"] = os.getenv("WECOM_WEBHOOK_URLS", "")
    return cfg


def ensure_output_dirs(cfg: Dict[str, Any]) -> None:
    """创建必要的输出目录（snapshots, outputs/json, outputs/csv, outputs/logs）"""
    paths = cfg.get("paths", {})
    for key in ("snapshot_root", "output_root"):
        if paths.get(key):
            Path(paths[key]).mkdir(parents=True, exist_ok=True)
    output_root = Path(paths["output_root"])
    for child in ("csv", "json", "logs"):
        (output_root / child).mkdir(parents=True, exist_ok=True)
