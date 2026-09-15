from quant_ai.domain.models import AssetClass, Instrument, Market


def starter_catalog() -> tuple[Instrument, ...]:
    """Return the declared research universe with non-equity candidates observation-only."""
    return (
        Instrument("NIFTY50", Market.INDIA, AssetClass.INDEX, "INR", "NSE", False),
        Instrument("RELIANCE", Market.INDIA, AssetClass.EQUITY, "INR", "NSE"),
        Instrument("GOLDBEES", Market.INDIA, AssetClass.ETF, "INR", "NSE"),
        Instrument(
            "SENSEX", Market.INDIA, AssetClass.INDEX, "INR", "BSE", False,
            {"research_only": "true"},
        ),
        Instrument(
            "BANKEX", Market.INDIA, AssetClass.INDEX, "INR", "BSE", False,
            {"research_only": "true"},
        ),
        Instrument(
            "G-SEC", Market.INDIA, AssetClass.BOND, "INR", "NSE", False,
            {"research_only": "true"},
        ),
        Instrument(
            "BSE-GSEC", Market.INDIA, AssetClass.DEBT, "INR", "BSE", False,
            {"research_only": "true"},
        ),
        Instrument(
            "NSE-MF", Market.INDIA, AssetClass.FUND, "INR", "NSE", False,
            {"research_only": "true"},
        ),
        Instrument(
            "BSE-MF", Market.INDIA, AssetClass.FUND, "INR", "BSE", False,
            {"research_only": "true"},
        ),
        Instrument(
            "NSE-IPO", Market.INDIA, AssetClass.IPO, "INR", "NSE", False,
            {"research_only": "true"},
        ),
        Instrument(
            "BSE-IPO", Market.INDIA, AssetClass.IPO, "INR", "BSE", False,
            {"research_only": "true"},
        ),
        Instrument(
            "NSE-SLB", Market.INDIA, AssetClass.SLB, "INR", "NSE", False,
            {"research_only": "true"},
        ),
        Instrument(
            "SGB", Market.INDIA, AssetClass.SGB, "INR", "NSE", False,
            {"research_only": "true"},
        ),
        Instrument(
            "REIT", Market.INDIA, AssetClass.REIT, "INR", "NSE", False,
            {"research_only": "true"},
        ),
        Instrument(
            "INVIT", Market.INDIA, AssetClass.INVIT, "INR", "NSE", False,
            {"research_only": "true"},
        ),
        Instrument(
            "NIFTY", Market.INDIA, AssetClass.FUTURE, "INR", "NFO", False,
            {"requires_contract": "true"},
        ),
        Instrument(
            "BANKNIFTY", Market.INDIA, AssetClass.FUTURE, "INR", "NFO", False,
            {"requires_contract": "true"},
        ),
        Instrument(
            "SENSEX-FUT", Market.INDIA, AssetClass.FUTURE, "INR", "BFO", False,
            {"requires_contract": "true"},
        ),
        Instrument(
            "GUARSEED", Market.INDIA, AssetClass.COMMODITY, "INR", "NCDEX", False,
            {"requires_contract": "true"},
        ),
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
        Instrument(
            "BCD-USDINR", Market.INDIA, AssetClass.FX, "INR", "BCD", False,
            {"requires_contract": "true"},
        ),
        Instrument(
            "NFO-NIFTY", Market.INDIA, AssetClass.FUTURE, "INR", "NFO", False,
            {"requires_contract": "true"},
        ),
        Instrument(
            "BFO-SENSEX", Market.INDIA, AssetClass.FUTURE, "INR", "BFO", False,
            {"requires_contract": "true"},
        ),
        Instrument(
            "MCX-COTTON", Market.INDIA, AssetClass.COMMODITY, "INR", "MCX", False,
            {"requires_contract": "true"},
        ),
        Instrument(
            "NCDEX-COTTON", Market.INDIA, AssetClass.COMMODITY, "INR", "NCDEX", False,
            {"requires_contract": "true"},
        ),
        Instrument(
            "MSEI-EQUITY", Market.INDIA, AssetClass.EQUITY, "INR", "MSEI", False,
            {"research_only": "true"},
        ),
        Instrument(
            "MSEI-DERIVATIVE", Market.INDIA, AssetClass.FUTURE, "INR", "MSEI", False,
            {"requires_contract": "true"},
        ),
        Instrument(
            "GIFT-NIFTY", Market.INDIA, AssetClass.FUTURE, "USD", "IFSC", False,
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
