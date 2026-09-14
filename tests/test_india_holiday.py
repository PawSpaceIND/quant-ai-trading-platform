from datetime import datetime, timezone

from quant_ai.domain.models import Market
from quant_ai.execution.session import MarketCalendar, default_holidays


def test_ganesh_chaturthi_2026_is_closed():
    calendar = MarketCalendar(default_holidays())
    assert calendar.state(Market.INDIA, datetime(2026, 9, 14, 7, tzinfo=timezone.utc)).value == "CLOSED"
    assert calendar.state(Market.INDIA, datetime(2026, 9, 15, 7, tzinfo=timezone.utc)).value == "REGULAR_HOURS"
