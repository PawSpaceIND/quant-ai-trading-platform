from quant_ai.domain.models import AssetClass, Instrument, Market


def starter_catalog() -> tuple[Instrument, ...]:
    """Return the declared research universe with non-equity candidates observation-only."""
    return (
        Instrument("NIFTY50", Market.INDIA, AssetClass.INDEX, "INR", "NSE", False),
        Instrument("RELIANCE", Market.INDIA, AssetClass.EQUITY, "INR", "NSE"),
        Instrument("GOLDBEES", Market.INDIA, AssetClass.ETF, "INR", "NSE"),
        Instrument(
            "GOLD", Market.INDIA, AssetClass.METAL, "INR", "MCX", False,
            {"requires_contract": "true"},
        ),
        Instrument(
            "SILVER", Market.INDIA, AssetClass.METAL, "INR", "MCX", False,
            {"requires_contract": "true"},
        ),
        Instrument(
            "COPPER", Market.INDIA, AssetClass.COMMODITY, "INR", "MCX", False,
            {"requires_contract": "true"},
        ),
        Instrument(
            "ALUMINIUM", Market.INDIA, AssetClass.COMMODITY, "INR", "MCX", False,
            {"requires_contract": "true"},
        ),
        Instrument(
            "ZINC", Market.INDIA, AssetClass.COMMODITY, "INR", "MCX", False,
            {"requires_contract": "true"},
        ),
        Instrument(
            "CRUDEOIL", Market.INDIA, AssetClass.COMMODITY, "INR", "MCX", False,
            {"requires_contract": "true"},
        ),
        Instrument(
            "NATURALGAS", Market.INDIA, AssetClass.COMMODITY, "INR", "MCX", False,
            {"requires_contract": "true"},
        ),
        Instrument(
            "USDINR", Market.INDIA, AssetClass.FX, "INR", "CDS", False,
            {"requires_contract": "true"},
        ),
        Instrument(
            "EURINR", Market.INDIA, AssetClass.FX, "INR", "CDS", False,
            {"requires_contract": "true"},
        ),
        Instrument(
            "GBPINR", Market.INDIA, AssetClass.FX, "INR", "CDS", False,
            {"requires_contract": "true"},
        ),
        Instrument(
            "JPYINR", Market.INDIA, AssetClass.FX, "INR", "CDS", False,
            {"requires_contract": "true"},
        ),
        Instrument("AAPL", Market.USA, AssetClass.EQUITY, "USD", "NASDAQ"),
        Instrument("SPY", Market.USA, AssetClass.ETF, "USD", "NYSEARCA"),
        Instrument("GLD", Market.USA, AssetClass.ETF, "USD", "NYSEARCA"),
        Instrument(
            "GC", Market.GLOBAL, AssetClass.METAL, "USD", "COMEX", False,
            {"requires_contract": "true"},
        ),
        Instrument("EURUSD", Market.GLOBAL, AssetClass.FX, "USD", "OTC", False),
        Instrument("BTCUSD", Market.GLOBAL, AssetClass.CRYPTO, "USD", "GLOBAL", False),
    )
