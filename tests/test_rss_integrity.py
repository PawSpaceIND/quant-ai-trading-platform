from datetime import datetime, timezone
from quant_ai.intelligence.external.rss import RssNewsSentimentAdapter


class Client:
    def get_text(self, *args, **kwargs):
        return """<rss><channel>
        <item><title>undated rally</title></item>
        <item><title>bad date</title><pubDate>garbage</pubDate></item>
        <item><title>future</title><pubDate>Tue, 15 Sep 2026 09:00:00 GMT</pubDate></item>
        <item><title>valid</title><pubDate>Mon, 14 Sep 2026 09:00:00 GMT</pubDate></item>
        </channel></rss>"""


def test_invalid_missing_and_future_news_is_not_fresh_evidence():
    rows = RssNewsSentimentAdapter(Client(), ("https://example.test/rss",)).fetch(
        "GEOPOLITICAL", datetime(2026, 9, 14, 10, tzinfo=timezone.utc))
    assert [row.headline for row in rows] == ["valid"]
