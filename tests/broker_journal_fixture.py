"""Synthetic partial/completed/missing/recovered observations for cross-reader QA."""
import argparse
import json
import sys
from contextlib import closing
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from broker_observation_fixture import fixture

from quant_ai.execution.broker_journal import BrokerJournal, replay
from quant_ai.execution.broker_observation import digest


def captures(now):
    local=now.astimezone(ZoneInfo('Asia/Kolkata'))
    day_start=local.replace(hour=0,minute=0,second=0,microsecond=0)
    span=min(150,(local-day_start).total_seconds()/2)
    base=now-timedelta(seconds=span)
    first=fixture(base)
    for group in ('orders','ordersBefore','trades','tradesBefore'):
        for row in first[group]:row['at']=base.isoformat(timespec='seconds')
    next(o for o in first['orders'] if o['symbol']=='TCS')['pending']=2
    complete=deepcopy(first)
    o=next(o for o in complete['orders'] if o['symbol']=='INFY')
    o.update(status='COMPLETE',filled=3,pending=0,averagePrice='100.00000000')
    t=deepcopy(next(t for t in first['trades'] if t['symbol']=='INFY'))
    t.update(tradeId='trade-13-b',quantity=2,price='101.00000000',at=(base+timedelta(seconds=span/5)).isoformat(timespec='seconds'))
    complete['trades'].append(t)
    complete['trades'].sort(key=lambda t:(t['exchange'],t['tradeId'],t['orderId']))
    changed=deepcopy(complete)
    changed['trades']=[t for t in changed['trades'] if t['tradeId']!='trade-13-b']
    next(o for o in changed['orders'] if o['symbol']=='INFY')['filled']=1
    empty={**deepcopy(first),'orders':[],'trades':[]}
    result=[]
    for index,c in enumerate([first,complete,changed,empty,empty,complete]):
        c=deepcopy(c)
        c['startedAt']=c['finishedAt']=(base+timedelta(seconds=span*index/5)).isoformat(timespec='milliseconds')
        c['ordersBefore']=deepcopy(c['orders']);c['tradesBefore']=deepcopy(c['trades'])
        c['sha256']=digest({k:v for k,v in c.items() if k!='sha256'})
        result.append(c)
    return result


def main():
    p=argparse.ArgumentParser();p.add_argument('--database',type=Path);p.add_argument('--output',type=Path);p.add_argument('--now')
    args=p.parse_args();now=datetime.fromisoformat(args.now) if args.now else datetime.now(timezone.utc)
    rows=captures(now)
    if args.output:
        args.output.write_text(json.dumps({'captures':rows,'expected':[{k:v for k,v in item.items() if k!='capture'} for item in replay(rows)]},separators=(',',':'))+'\n')
    if args.database:
        with closing(BrokerJournal(args.database,'default',rows[0]['accountRef'])) as journal:
            for c in rows:journal.append(c,now=now)
            report=journal.report()
        print(json.dumps({'journalId':report['journalId'],'accountRef':report['accountRef'],'captureCount':report['captureCount'],'headHash':report['headHash'],'synthetic':True}))


if __name__=='__main__':main()
