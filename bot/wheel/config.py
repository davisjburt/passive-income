"""Load the wheel config (config.wheel.yaml) and credentials from the env."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml
from dotenv import load_dotenv

from .strategy import LegRules

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")


@dataclass
class Safeguards:
    halt_new_puts_drawdown_pct: float = 0.20
    order_type: str = "limit"
    limit_slippage_pct: float = 0.02


@dataclass
class WheelConfig:
    enabled: bool
    universe: list[str]
    max_wheel_tickers: int
    per_stock_cap_pct: float
    portfolio_wheel_cap_pct: float
    put: LegRules
    call: LegRules
    safeguards: Safeguards
    api_key: str
    api_secret: str
    paper: bool = True   # hardcoded paper; going live is a deliberate code edit
    account: str = "default"  # slug used for ledger/report file names + alert tagging;
                              # "default" preserves the original untagged file names

    def validate(self) -> None:
        if not 0 < self.per_stock_cap_pct <= 1.0:
            raise ValueError("per_stock_cap_pct must be in (0, 1.0]")
        if not 0 < self.portfolio_wheel_cap_pct <= 1.0:
            raise ValueError("portfolio_wheel_cap_pct must be in (0, 1.0]")
        if not self.universe:
            raise ValueError("wheel universe is empty")


def load_wheel_config(path: str | os.PathLike | None = None,
                      account: str = "default") -> WheelConfig:
    cfg_path = Path(path) if path else ROOT / "config.wheel.yaml"
    raw = yaml.safe_load(cfg_path.read_text()) or {}

    if account == "default":
        key_env, secret_env = "ALPACA_API_KEY", "ALPACA_API_SECRET"
    else:
        prefix = f"ALPACA_{account.upper()}_API"
        key_env, secret_env = f"{prefix}_KEY", f"{prefix}_SECRET"

    try:
        api_key = os.environ[key_env]
        api_secret = os.environ[secret_env]
    except KeyError as exc:
        raise SystemExit(f"Missing {exc.args[0]} — set it in .env or CI secrets.")

    put = raw.get("put") or {}
    call = raw.get("call") or {}
    cfg = WheelConfig(
        enabled=bool(raw.get("enabled", False)),
        universe=list(raw.get("universe", [])),
        max_wheel_tickers=int(raw.get("max_wheel_tickers", 5)),
        per_stock_cap_pct=float(raw.get("per_stock_cap_pct", 0.08)),
        portfolio_wheel_cap_pct=float(raw.get("portfolio_wheel_cap_pct", 0.45)),
        put=LegRules(
            band=tuple(put.get("otm_pct", [0.10, 0.15])),
            dte=tuple(put.get("dte", [30, 45])),
            min_annual_yield=float(put.get("min_annual_yield", 0.10)),
        ),
        call=LegRules(
            band=tuple(call.get("otm_above_basis_pct", [0.05, 0.10])),
            dte=tuple(call.get("dte", [30, 45])),
            min_annual_yield=float(call.get("min_annual_yield", 0.10)),
        ),
        safeguards=Safeguards(**(raw.get("safeguards") or {})),
        api_key=api_key,
        api_secret=api_secret,
        account=account,
    )
    cfg.validate()
    return cfg
