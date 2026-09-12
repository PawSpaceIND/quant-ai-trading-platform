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
