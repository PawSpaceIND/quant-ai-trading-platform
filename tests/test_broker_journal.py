import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from copy import deepcopy
from datetime import timedelta
from pathlib import Path

import pytest
from test_broker_observation import NOW, Transport, capture, order, trade
from test_recovery_bundle import fixture as paper_fixture

from quant_ai.execution.broker_journal import BrokerJournal, validate_capture
from quant_ai.execution.broker_observation import digest
from quant_ai.execution.broker_reads import BrokerReadError
from quant_ai.operations import recovery_bundle
from quant_ai.operations.research_recovery import inspect


def stamp(c,seconds=0):
    c=deepcopy(c)
    c['startedAt']=c['finishedAt']=(NOW+timedelta(seconds=seconds)).isoformat(timespec='milliseconds')
    c['sha256']=digest({k:v for k,v in c.items() if k!='sha256'})
    return c


def history():
    first=capture(Transport([order(status='OPEN',filled_quantity=1,pending_quantity=2,average_price=0)], [trade()]))
    complete=stamp(capture(),30)
    corrupted=deepcopy(complete)
    corrupted['orders'][0]['filled']=1
    corrupted['ordersBefore']=deepcopy(corrupted['orders'])
    corrupted['trades']=corrupted['trades'][:1]
    corrupted['tradesBefore']=deepcopy(corrupted['trades'])
    return [stamp(first),complete,stamp(corrupted,60),stamp(capture(Transport([],[])),90),stamp(capture(Transport([],[])),120),stamp(complete,150)]


def test_journal_restarts_preserve_partial_completion_and_persistent_missing_records(tmp_path):
    records=history();file=tmp_path/'broker.sqlite'
    for index,c in enumerate(records,1):
        with closing(BrokerJournal(file,'india-paper',c['accountRef'])) as journal:
            assert journal.append(c,now=NOW+timedelta(days=1))['sequence']==index
    with closing(BrokerJournal(file,'india-paper',records[0]['accountRef'],readonly=True)) as journal:
        report=journal.report()
        assert report['captureCount']==6 and len(report['history'])==6
        first=journal.report(1)['selected'];assert first['temporal']['status']=='baseline'
        second=journal.report(2)['selected'];assert second['temporal']['status']=='compared'
        assert any(c['kind']=='status' and c['before']=='OPEN' and c['after']=='COMPLETE' for c in second['temporal']['changes'])
        third=journal.report(3)['selected'];codes={i['code'] for i in third['temporal']['issues']}
        assert {'filled_quantity_regressed','terminal_order_changed','previously_observed_trade_missing'} <= codes
        for seq in (4,5):
            current=journal.report(seq)['selected'];assert current['temporal']['status']=='issues'
            assert any(i['code']=='previously_observed_order_missing' for i in current['temporal']['issues'])
        assert report['selected']['temporal']['issueCount']==0
        assert report['selected']['cumulativeIssueCount']>0,'Recovered observations cannot erase earlier contradictions'
        assert first['capture']['orders'][0]['status']=='OPEN','Replay must not mutate earlier captures'


def test_duplicate_capture_is_idempotent_across_concurrent_writers(tmp_path):
    c=history()[0];file=tmp_path/'broker.sqlite'
    with closing(BrokerJournal(file,'india-paper',c['accountRef'])):
        pass
    def append(_):
        with closing(BrokerJournal(file,'india-paper',c['accountRef'])) as j:
            return j.append(c,now=NOW)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results=list(pool.map(append,range(4)))
    assert [r['sequence'] for r in results]==[1,1,1,1]
    assert sum(not r['duplicate'] for r in results)==1
    with closing(BrokerJournal(file,readonly=True)) as j:
        assert j.report()['captureCount']==1


def test_changed_capture_does_not_replace_stable_baseline_and_day_reset_is_explicit(tmp_path):
    c=history()[0];changing=stamp(history()[1],30)
    changing['ordersBefore']=deepcopy(c['orders']);changing['tradesBefore']=deepcopy(c['trades']);changing=stamp(changing,30)
    records=[c,changing,stamp(c,60),stamp(capture(Transport([],[])),86400)]
    with closing(BrokerJournal(tmp_path/'broker.sqlite','india-paper',c['accountRef'])) as j:
        for row in records:j.append(row,now=NOW+timedelta(days=2))
        assert j.report(2)['selected']['temporal']['status']=='unverified'
        assert j.report(3)['selected']['temporal']['issueCount']==0
        reset=j.report(4)['selected']['temporal']
        assert reset['dayBoundary'] and reset['unresolvedPriorDayOrders']==1
        assert [i['code'] for i in reset['issues']]==['day_boundary_unresolved_orders']


def test_wrong_account_overlap_future_and_malformed_capture_fail_without_appending(tmp_path):
    c=history()[0]
    with closing(BrokerJournal(tmp_path/'broker.sqlite','india-paper',c['accountRef'])) as j:
        j.append(c,now=NOW)
        for bad in [stamp(c,-1),stamp(c,10)]:
            with pytest.raises(BrokerReadError):j.append(bad,now=NOW)
        bad=stamp(c,30);bad['accountRef']='0'*64;bad=stamp(bad,30)
        with pytest.raises(BrokerReadError):j.append(bad,now=NOW+timedelta(hours=1))
        bad=stamp(c,30);bad['orders'][0]['quantity']=1.5;bad=stamp(bad,30)
        with pytest.raises(BrokerReadError):j.append(bad,now=NOW+timedelta(hours=1))
        assert j.report()['captureCount']==1
        with pytest.raises(BrokerReadError):j.report(2)
    with pytest.raises(BrokerReadError):BrokerJournal(tmp_path/'broker.sqlite','another',c['accountRef'])


def test_append_only_guards_and_chain_payload_tampering(tmp_path):
    records=history()[:2];file=tmp_path/'broker.sqlite'
    with closing(BrokerJournal(file,'india-paper',records[0]['accountRef'])) as j:
        for row in records:j.append(row,now=NOW+timedelta(days=1))
        with pytest.raises(sqlite3.IntegrityError,match='append-only'):
            j.db.execute('DELETE FROM broker_captures WHERE sequence=2')
        with pytest.raises(sqlite3.IntegrityError,match='append-only'):
            j.db.execute("UPDATE broker_journal_meta SET tenant='other'")
        j.db.execute('DROP TRIGGER broker_captures_update')
        j.db.execute("UPDATE broker_captures SET payload='{}' WHERE sequence=1")
        with pytest.raises(BrokerReadError):j.report()
    with pytest.raises(BrokerReadError):BrokerJournal(file,readonly=True)


def test_foreign_database_is_not_changed_and_selected_recovery_replays_journal(tmp_path):
    foreign=tmp_path/'foreign.db'
    with closing(sqlite3.connect(foreign)) as db:
        db.execute('CREATE TABLE untouched(value)');db.commit()
    original=foreign.read_bytes();c=history()[0]
    with pytest.raises(BrokerReadError):BrokerJournal(foreign,'india-paper',c['accountRef'])
    assert foreign.read_bytes()==original and not Path(str(foreign)+'-wal').exists()
    spec=paper_fixture(tmp_path);file=tmp_path/'broker.sqlite'
    with closing(BrokerJournal(file,'india-paper',c['accountRef'])) as j:
        for row in history():j.append(row,now=NOW+timedelta(days=1))
        expected=j.report()
    spec['research_state']={'broker-history':{'kind':'broker_journal','path':str(file)}}
    backup=recovery_bundle.create(spec,tmp_path/'bundle',writers_stopped=True)
    restored=recovery_bundle.restore(tmp_path/'bundle',tmp_path/'restored',manifest_sha256=backup['manifestSha256'])
    path=tmp_path/'restored'/restored['researchRecovery']['sources']['broker-history']['path']
    assert inspect(path,'broker_journal')==backup['researchState']['broker-history']['verification']
    with closing(BrokerJournal(path,readonly=True)) as j:assert j.report()==expected


def test_normalized_capture_rejects_extra_fields_wrong_hash_and_fractional_json_shape():
    c=history()[0]
    for key,value in [('sha256','0'*64),('extra','untrusted')]:
        bad=deepcopy(c);bad[key]=value
        with pytest.raises(BrokerReadError):validate_capture(bad,'india-paper',c['accountRef'])
    bad=deepcopy(c);bad['orders'][0]['quantity']=3.0;bad=stamp(bad)
    with pytest.raises(BrokerReadError):validate_capture(bad,'india-paper',c['accountRef'])
    assert json.loads(json.dumps(validate_capture(c,'india-paper',c['accountRef'])))==c


def test_cancelled_order_pending_and_unqualified_average_cleanup_is_observed_without_reversal(tmp_path):
    first=capture(Transport([order(quantity=3,status='CANCELLED',filled_quantity=1,pending_quantity=2,cancelled_quantity=2,average_price=0)],[trade()]))
    later=deepcopy(first)
    later['orders'][0].update(pending=0,averagePrice='98.00000000')
    later['ordersBefore']=deepcopy(later['orders']);later=stamp(later,30)
    with closing(BrokerJournal(tmp_path/'broker.sqlite','india-paper',first['accountRef'])) as j:
        j.append(first,now=NOW);j.append(later,now=NOW+timedelta(seconds=30))
        result=j.report()['selected']
        assert result['inspection']['issueCount']==result['temporal']['issueCount']==0
        assert {c['kind'] for c in result['temporal']['changes']}=={'pending','averagePrice'}
