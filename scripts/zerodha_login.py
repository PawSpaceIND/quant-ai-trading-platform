"""Interactive Zerodha Kite login for the paper pilot; same as ``pramana zerodha-login``.

Writes ~/.config/pramana/zerodha-session.json (mode 0600). Never prints or stores
api_secret or the access token. Refuses to run when TRADING_LIVE_MONEY_ACTIVE is set.
"""
from quant_ai.operations.zerodha_login import main

if __name__ == "__main__":
    raise SystemExit(main())
