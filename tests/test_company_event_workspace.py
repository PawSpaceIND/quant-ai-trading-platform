import sqlite3
from contextlib import closing

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
