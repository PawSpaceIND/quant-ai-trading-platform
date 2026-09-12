from decimal import Decimal

from quant_ai.accounting.portfolio import PaperAccount
from quant_ai.domain.models import Side


def test_paper_account_tracks_realized_and_unrealized_pnl() -> None:
    account = PaperAccount(Decimal(10000))
    account.apply_fill("AAPL", Side.BUY, 10, Decimal(100), Decimal(1))
    assert account.position("AAPL").quantity == 10
    assert account.unrealized_pnl({"AAPL": Decimal(110)}) == Decimal(100)
    account.apply_fill("AAPL", Side.SELL, 5, Decimal(110), Decimal(1))
    assert account.realized_pnl == Decimal(49)
    assert account.equity({"AAPL": Decimal(110)}) == Decimal(10098)
