"""Plug-and-play strategies package for shubya_ai."""
from __future__ import annotations

from trading.strategies.orb_multi_ma import ORBMultiMAStrategy
from trading.strategies.orb_ma200 import ORBMA200Strategy  # Paused / legacy

# Default strategy is the new ORB Multi-Timeframe MA Strategy
DefaultStrategy = ORBMultiMAStrategy

__all__ = ["ORBMultiMAStrategy", "DefaultStrategy", "ORBMA200Strategy"]
