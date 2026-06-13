"""策略适配器基类 — 所有策略的统一接口"""

from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass
class StrategyResult:
    """策略运行结果"""
    strategy_name: str
    display_name: str
    top: List[Dict[str, Any]] = field(default_factory=list)
    top_n: int = 10
    error: str = ""
    elapsed_seconds: float = 0.0
    quality_fields: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return len(self.top) > 0 and not self.error


class StrategyAdapter(ABC):
    """策略适配器基类"""

    def __init__(self, name: str, display_name: str, top_n: int = 10):
        self._name = name
        self._display_name = display_name
        self._top_n = top_n

    @property
    def name(self) -> str:
        return self._name

    @property
    def display_name(self) -> str:
        return self._display_name

    @property
    def top_n(self) -> int:
        return self._top_n

    @abstractmethod
    def validate_environment(self) -> bool:
        ...

    @abstractmethod
    def run(self, snapshot_dir: Path, cfg: Dict[str, Any]) -> StrategyResult:
        ...
