from quant_ai.domain.models import AssetClass, Instrument, Market


def starter_catalog() -> tuple[Instrument, ...]:
    return (
        Instrument("NIFTY50", Market.INDIA, AssetClass.INDEX, "INR", "NSE", False),
        Instrument("RELIANCE", Market.INDIA, AssetClass.EQUITY, "INR", "NSE"),
        Instrument("GOLDBEES", Market.INDIA, AssetClass.ETF, "INR", "NSE"),
        Instrument("GOLD", Market.INDIA, AssetClass.METAL, "INR", "MCX"),
        Instrument("SILVER", Market.INDIA, AssetClass.METAL, "INR", "MCX"),
        Instrument("AAPL", Market.USA, AssetClass.EQUITY, "USD", "NASDAQ"),
        Instrument("SPY", Market.USA, AssetClass.ETF, "USD", "NYSEARCA"),
        Instrument("GLD", Market.USA, AssetClass.ETF, "USD", "NYSEARCA"),
        Instrument("GC", Market.GLOBAL, AssetClass.METAL, "USD", "COMEX"),
        Instrument("EURUSD", Market.GLOBAL, AssetClass.FX, "USD", "OTC"),
        Instrument("BTCUSD", Market.GLOBAL, AssetClass.CRYPTO, "USD", "GLOBAL", False),
    )
