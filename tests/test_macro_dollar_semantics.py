from quant_ai.intelligence.external.fred import FredMacroProvider


def test_fred_broad_dollar_proxy_is_not_mislabeled_as_dxy():
    assert FredMacroProvider.series["USD_BROAD"] == "DTWEXBGS"
    assert "DXY" not in FredMacroProvider.series
