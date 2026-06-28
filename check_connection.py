"""Verify the Alpaca paper trading credentials connect successfully.

Run:  .venv/bin/python check_connection.py
"""

import os
import sys

from dotenv import load_dotenv
from alpaca.trading.client import TradingClient

load_dotenv()

API_KEY = os.getenv("ALPACA_API_KEY")
API_SECRET = os.getenv("ALPACA_API_SECRET")


def main() -> int:
    if not API_KEY or not API_SECRET:
        print("Missing ALPACA_API_KEY or ALPACA_API_SECRET in .env")
        return 1

    # paper=True targets https://paper-api.alpaca.markets
    client = TradingClient(API_KEY, API_SECRET, paper=True)

    try:
        account = client.get_account()
    except Exception as exc:  # noqa: BLE001
        print(f"Connection failed: {exc}")
        return 1

    print("Connected to Alpaca paper account")
    print(f"  Account number : {account.account_number}")
    print(f"  Status         : {account.status}")
    print(f"  Buying power    : ${account.buying_power}")
    print(f"  Cash           : ${account.cash}")
    print(f"  Portfolio value: ${account.portfolio_value}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
