"""Cross-language mapping lifecycle and microsecond cutoff fixture."""

import json
import sys
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from company_event_fixture import build, export
from test_research_extensions import at, feed

from quant_ai.research.company_events import CompanyEvents


def fixture(path):
    build(path)
    with closing(CompanyEvents(path)) as events:
        with events.db:
            events.db.execute("INSERT INTO symbol_mappings VALUES (?,?,?,?)", (
                "Infosys Limited", "2026-01-01T04:00:12.000100+00:00", "", "synthetic withdrawal"))
        events.map_company("Infosys Limited", "NSE:TCS", "2026-01-01T04:00:12.000200+00:00", "synthetic remapping")
        events.map_company("Infosys Limited", "NSE:INFY", at(13), "synthetic conflicting mapping")
        with events.db:
            events.db.execute("INSERT INTO symbol_mappings VALUES (?,?,?,?)", (
                "Infosys Limited", "2026-01-01T09:30:13+05:30", "", "same instant, different offset"))
        events.map_company("Infosys Limited", "NSE:INFY", at(14), "resolving review")
        for fraction, text in [("000100", "Microsecond original"), ("000200", "Microsecond correction")]:
            events.ingest(feed(text).replace(b"<guid>one</guid>", b"<guid>micro</guid>"),
                          observed_at=f"2026-01-01T04:00:15.{fraction}+00:00")
    result = export(path)
    with closing(CompanyEvents(path, readonly=True)) as events:
        result["lifecycleSources"] = [
            {"at": cutoff, "symbol": symbol, "sources": events.sources_as_of(symbol, cutoff)}
            for cutoff in [at(11), "2026-01-01T04:00:12.000099Z", "2026-01-01T04:00:12.000100Z",
                           "2026-01-01T04:00:12.000199Z", "2026-01-01T04:00:12.000200Z", at(13), at(14),
                           "2026-01-01T04:00:15.000099Z", "2026-01-01T04:00:15.000100Z",
                           "2026-01-01T04:00:15.000199Z", "2026-01-01T04:00:15.000200Z"]
            for symbol in ("NSE:INFY", "NSE:TCS")
        ]
    return result


if __name__ == "__main__":
    with TemporaryDirectory() as temporary:
        data = fixture(Path(temporary) / "events.sqlite")
        Path(sys.argv[1]).write_text(json.dumps(data, indent=2, ensure_ascii=True) + "\n")
