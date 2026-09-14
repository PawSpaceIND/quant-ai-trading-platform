import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone

import pytest
from test_research_extensions import at, feed

from quant_ai.research.company_events import CompanyEvents


def test_new_unmapped_correction_cannot_resurrect_old_mapped_revision(tmp_path):
    with closing(CompanyEvents(tmp_path/'events')) as events:
        events.ingest(feed(),observed_at=at(10))
        events.map_company('Infosys Limited','NSE:INFY',at(11),'test')
        events.ingest(feed('Correction').replace(b'Infosys Limited',b'Renamed Limited'),observed_at=at(20))
        assert len(events.sources_as_of('NSE:INFY',at(15)))==1
        assert events.sources_as_of('NSE:INFY',at(25))==[]
        events.map_company('Renamed Limited','NSE:INFY',at(30),'reviewed rename')
        assert events.sources_as_of('NSE:INFY',at(25))==[]
        assert events.sources_as_of('NSE:INFY',at(35))[0]['data']['description']=='Correction'


def test_equal_first_seen_conflict_is_withheld_until_later_unambiguous_revision(tmp_path):
    with closing(CompanyEvents(tmp_path/'events')) as events:
        events.map_company('Infosys Limited','NSE:INFY',at(1),'test')
        events.ingest(feed('A'),observed_at=at(10));events.ingest(feed('B'),observed_at=at(10))
        assert events.sources_as_of('NSE:INFY',at(15))==[]
        events.ingest(feed('C'),observed_at=at(20))
        assert events.sources_as_of('NSE:INFY',at(25))[0]['data']['description']=='C'


def test_readonly_event_connection_cannot_create_or_write(tmp_path):
    path=tmp_path/'events'
    with pytest.raises(sqlite3.OperationalError): CompanyEvents(path,readonly=True)
    assert not path.exists()
    with closing(CompanyEvents(path)) as events: events.ingest(feed(),observed_at=at(10))
    before=path.read_bytes()
    with closing(CompanyEvents(path,readonly=True)) as events:
        assert events.status()['revisions']==1
        with pytest.raises(sqlite3.OperationalError): events.map_company('Infosys Limited','NSE:INFY',at(11),'test')
    assert path.read_bytes()==before


def test_withdrawal_is_server_timed_append_only_and_survives_later_review(tmp_path):
    with closing(CompanyEvents(tmp_path / "events")) as events:
        events.ingest(feed(), observed_at=at(10))
        events.map_company("Infosys Limited", "NSE:INFY", at(11), "original review")
        before = events.db.execute("SELECT * FROM symbol_mappings").fetchall()
        start = datetime.now(timezone.utc)
        record = events.revoke_company("Infosys Limited", "wrong instrument; withdrawn", at(11))
        withdrawn_at = datetime.fromisoformat(record["verified_at"])
        assert start <= withdrawn_at <= datetime.now(timezone.utc)
        assert events.db.execute("SELECT * FROM symbol_mappings WHERE symbol!=''").fetchall() == before
        assert len(events.sources_as_of("NSE:INFY", at(12))) == 1
        assert events.sources_as_of("NSE:INFY", record["verified_at"]) == []
        assert events.status()["mapping_revocations"] == 1
        with pytest.raises(ValueError, match="mapping_changed"):
            events.revoke_company("Infosys Limited", "stale retry", at(11))
        with pytest.raises(ValueError, match="already_revoked"):
            events.revoke_company("Infosys Limited", "duplicate", record["verified_at"])
        remap_at = (withdrawn_at + timedelta(microseconds=1)).isoformat()
        events.map_company("Infosys Limited", "NSE:TCS", remap_at, "corrected synthetic review")
        assert events.sources_as_of("NSE:TCS", record["verified_at"]) == []
        assert len(events.sources_as_of("NSE:TCS", remap_at)) == 1
        assert events.sources_as_of("NSE:INFY", remap_at) == []


def test_withdrawal_rejects_unknown_future_and_invalid_symbol_without_history_loss(tmp_path):
    with closing(CompanyEvents(tmp_path / "events")) as events:
        with pytest.raises(ValueError, match="mapping_changed"):
            events.revoke_company("Unknown", "review", at(11))
        events.map_company("Infosys Limited", "NSE:INFY", "2999-01-01T00:00:00+00:00", "future review")
        with pytest.raises(ValueError, match="server_time"):
            events.revoke_company("Infosys Limited", "review", "2999-01-01T00:00:00+00:00")
        assert events.status()["mappings"] == 1
        for symbol in ("", "NSE:", "NSE:BAD SYMBOL", "NASDAQ:AAPL"):
            with pytest.raises(ValueError, match="nse_symbol"):
                events.map_company("Infosys Limited", symbol, at(11), "review")
            with pytest.raises(ValueError, match="nse_symbol"):
                events.sources_as_of(symbol, at(12))


def test_mapping_offsets_and_microseconds_have_unambiguous_cutoff_semantics(tmp_path):
    with closing(CompanyEvents(tmp_path / "events")) as events:
        events.ingest(feed(), observed_at=at(10))
        events.map_company("Infosys Limited", "NSE:INFY", "2026-01-01T04:00:11.000100+00:00", "first")
        events.map_company("Infosys Limited", "NSE:TCS", "2026-01-01T04:00:11.000200+00:00", "second")
        assert events.sources_as_of("NSE:INFY", "2026-01-01T04:00:11.000099Z") == []
        assert len(events.sources_as_of("NSE:INFY", "2026-01-01T04:00:11.000100Z")) == 1
        assert len(events.sources_as_of("NSE:TCS", "2026-01-01T04:00:11.000200Z")) == 1
        with events.db:
            events.db.execute("INSERT INTO symbol_mappings VALUES (?,?,?,?)", ("Infosys Limited", "2026-01-01T09:30:11.000200+05:30", "", "same-instant withdrawal conflict"))
        assert events.sources_as_of("NSE:TCS", at(12)) == []
        events.map_company("Infosys Limited", "NSE:INFY", at(13), "later resolving review")
        assert len(events.sources_as_of("NSE:INFY", at(14))) == 1
