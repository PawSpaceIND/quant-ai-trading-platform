from quant_ai.domain.models import AssetClass
from quant_ai.instruments.catalog import starter_catalog


def test_catalog_spans_multiple_asset_classes() -> None:
    catalog = starter_catalog()
    asset_classes = {item.asset_class for item in catalog}
    assert AssetClass.EQUITY in asset_classes
    assert AssetClass.ETF in asset_classes
    assert AssetClass.METAL in asset_classes
    assert AssetClass.FX in asset_classes
    assert AssetClass.CRYPTO in asset_classes
    by_symbol = {item.symbol: item for item in catalog}
    for symbol in ("GOLD", "SILVER", "CRUDEOIL", "NATURALGAS", "USDINR", "EURINR", "GBPINR", "JPYINR"):
        assert by_symbol[symbol].tradable is False
        assert by_symbol[symbol].metadata["requires_contract"] == "true"
    assert by_symbol["SENSEX"].exchange == "BSE"
    assert by_symbol["SENSEX"].metadata["research_only"] == "true"
    for symbol in ("NIFTY", "BANKNIFTY", "SENSEX-FUT", "GUARSEED", "BCD-USDINR", "NFO-NIFTY", "BFO-SENSEX", "MCX-COTTON", "NCDEX-COTTON", "GIFT-NIFTY"):
        assert by_symbol[symbol].tradable is False
        assert by_symbol[symbol].metadata["requires_contract"] == "true"
    for symbol in ("BSE-GSEC", "NSE-MF", "BSE-MF", "NSE-IPO", "BSE-IPO", "NSE-SLB", "SGB", "REIT", "INVIT"):
        assert by_symbol[symbol].tradable is False
        assert by_symbol[symbol].metadata["research_only"] == "true"
