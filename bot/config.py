"""Load configuration from config.yaml and credentials from the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


@dataclass
class StrategyConfig:
    bar_timeframe: str = "Day"
    rsi_period: int = 2
    rsi_entry: float = 10.0
    rsi_exit: float = 60.0
    trend_sma: int = 200
    exit_sma: int = 5
    max_hold_days: int = 10


@dataclass
class RiskConfig:
    max_positions: int = 3
    max_position_pct: float = 0.20
    cash_buffer_pct: float = 0.25
    stop_loss_pct: float = 0.08
    daily_loss_halt_pct: float = 0.04
    use_margin: bool = False


@dataclass
class ExecConfig:
    order_type: str = "market"
    time_in_force: str = "day"
    lookback_days: int = 400


@dataclass
class Config:
    universe: list[str]
    strategy: StrategyConfig
    risk: RiskConfig
    execution: ExecConfig
    api_key: str
    api_secret: str
    # Hardcoded to paper. There is intentionally no way to set this to live
    # trading from config — flipping to real money must be a deliberate code edit.
    paper: bool = field(default=True)

    def validate(self) -> None:
        if self.risk.use_margin:
            raise ValueError(
                "use_margin is true. This bot is built to be cash-only; "
                "set risk.use_margin: false in config.yaml."
            )
        if not 0 <= self.risk.cash_buffer_pct < 1:
            raise ValueError("cash_buffer_pct must be in [0, 1)")
        if not 0 < self.risk.max_position_pct <= 1:
            raise ValueError("max_position_pct must be in (0, 1]")
        if not self.universe:
            raise ValueError("universe is empty")


def load_config(path: str | os.PathLike | None = None) -> Config:
    cfg_path = Path(path) if path else ROOT / "config.yaml"
    raw = yaml.safe_load(cfg_path.read_text()) or {}

    try:
        api_key = os.environ["ALPACA_API_KEY"]
        api_secret = os.environ["ALPACA_API_SECRET"]
    except KeyError as exc:
        raise SystemExit(
            f"Missing {exc.args[0]} — set it in .env (local) or as a "
            "GitHub Actions secret (CI)."
        )

    cfg = Config(
        universe=list(raw.get("universe", [])),
        strategy=StrategyConfig(**(raw.get("strategy") or {})),
        risk=RiskConfig(**(raw.get("risk") or {})),
        execution=ExecConfig(**(raw.get("execution") or {})),
        api_key=api_key,
        api_secret=api_secret,
    )
    cfg.validate()
    return cfg
