"""Load the momentum config (config.momentum.yaml) and credentials from the env."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")


@dataclass
class MomentumConfig:
    enabled: bool
    risky_universe: list[str]
    safe_symbol: str
    lookback_months: int
    cash_buffer_pct: float
    api_key: str
    api_secret: str
    paper: bool = True  # hardcoded paper; going live is a deliberate code edit
    account: str = "momentum"

    def validate(self) -> None:
        if not self.risky_universe:
            raise ValueError("momentum risky_universe is empty")
        if not self.safe_symbol:
            raise ValueError("momentum safe_symbol is required")
        if self.lookback_months <= 0:
            raise ValueError("lookback_months must be positive")
        if not 0 <= self.cash_buffer_pct < 1:
            raise ValueError("cash_buffer_pct must be in [0, 1)")


def load_momentum_config(path: str | os.PathLike | None = None,
                         account: str = "momentum") -> MomentumConfig:
    cfg_path = Path(path) if path else ROOT / "config.momentum.yaml"
    raw = yaml.safe_load(cfg_path.read_text()) or {}

    prefix = f"ALPACA_{account.upper()}_API"
    key_env, secret_env = f"{prefix}_KEY", f"{prefix}_SECRET"
    try:
        api_key = os.environ[key_env]
        api_secret = os.environ[secret_env]
    except KeyError as exc:
        raise SystemExit(f"Missing {exc.args[0]} — set it in .env or CI secrets.")

    cfg = MomentumConfig(
        enabled=bool(raw.get("enabled", False)),
        risky_universe=list(raw.get("risky_universe", [])),
        safe_symbol=str(raw.get("safe_symbol", "SHY")),
        lookback_months=int(raw.get("lookback_months", 12)),
        cash_buffer_pct=float(raw.get("cash_buffer_pct", 0.01)),
        api_key=api_key,
        api_secret=api_secret,
        account=account,
    )
    cfg.validate()
    return cfg
