"""策略适配器层 — 统一接口，进程隔离并行"""
from src.strategies.base import StrategyAdapter, StrategyResult

__all__ = ["StrategyAdapter", "StrategyResult"]
