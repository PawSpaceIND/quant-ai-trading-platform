"""Exercise the unchanged full-repository fitter, shadow writer and private CLI."""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

from test_feature_training_dataset import T, build, grants, named_refusal, populate

from quant_ai.learning import feature_dataset as fd
from quant_ai.learning.shadow import ShadowForecastWriter, validate_shadow_lineage

ROOT=Path(__file__).resolve().parents[1]


def test_recorded_dataset_to_actual_fitter_and_shadow_writer(tmp_path):
    source=tmp_path/'source.db'; plan=populate(source,40)
    package=build(source,plan); now=T+timedelta(days=1)
    fitted=fd.fit_training_package(package,source_grants=grants(),run_id='fit',candidate_id='candidate',clock=lambda:now)
    assert fitted.bundle.dataset.row_count==40
    assert fitted.diagnostics['data_package_binding']['package_sha256']==fd._digest(package)
    assert fitted.diagnostics['trading_authorized'] is False
    with ShadowForecastWriter(tmp_path/'shadow.db',tenant_id='ghost',clock=lambda:now+timedelta(seconds=1)) as writer:
        vector={'schema':'pramana.numeric_features.v1','subject':'INDIA:NSE:TEST:INR',
                'observed_at':now.isoformat(),'available_at':now.isoformat(),
                'source_ids':['recorded'],'values':{'signal':'1'}}
        first=writer.record(fitted.bundle,vector,pair_id='one')
        second=writer.record(fitted.bundle,vector,pair_id='one')
        assert first==second
        assert validate_shadow_lineage(writer.journal.db,tenant_id='ghost')['verified_forecasts']==1


def test_fit_grants_have_named_refusal():
    named_refusal(lambda:fd.fit_training_package(b'{}',source_grants=({},),run_id='r',candidate_id='c'), 'fit_grants')


def command_module():
    spec=importlib.util.spec_from_file_location('build_feature_training_data', ROOT/'scripts/build_feature_training_data.py')
    module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


def inputs(tmp_path):
    source=tmp_path/'source.db'; plan=populate(source,40)
    config=tmp_path/'plan.json';config.write_bytes(fd.canonical(plan));config.chmod(0o600)
    g=tmp_path/'grants.json'
    g.write_text(json.dumps([dict(source_id='recorded',provider='synthetic-test',categories=['MARKET','BROKER'],
        planes=['TRAINING'],point_in_time=True,rights_status='INTERNAL',max_age_seconds=None)]));g.chmod(0o600)
    return source,config,g


def run_cli(args,home,mode='false'):
    return subprocess.run([sys.executable,'-B',str(ROOT/'scripts/build_feature_training_data.py'),*map(str,args)],
        env={'PATH':os.environ.get('PATH',''),'HOME':str(home),'PYTHONDONTWRITEBYTECODE':'1',
             'TRADING_LIVE_MONEY_ACTIVE':mode},capture_output=True,text=True,timeout=60,check=False)


def test_actual_build_and_fit_commands_reuse_private_publication(tmp_path):
    source,p,g=inputs(tmp_path); output=tmp_path/'package.json'
    args=['build','--source',source,'--plan',p,'--grants',g,'--output',output]
    completed=run_cli(args,tmp_path)
    assert completed.returncode==0,completed.stderr
    assert json.loads(completed.stdout)==dict(mode='TRAINING_DATA_ONLY',rows=40,trading_authorized=False,
        source_authenticity_verified=False,out_of_sample_evaluated=False)
    assert 'signal' not in completed.stdout and str(tmp_path) not in completed.stdout+completed.stderr
    assert output.stat().st_mode & 0o077==0
    before=output.read_bytes(); repeated=run_cli(args,tmp_path)
    assert repeated.returncode==2 and output.read_bytes()==before
    model=tmp_path/'model.json'
    fitted=run_cli(['fit','--package',output,'--grants',g,'--output',model,'--run-id','r','--candidate-id','c'],tmp_path)
    assert fitted.returncode==0,fitted.stderr
    assert json.loads(fitted.stdout)['mode']=='TRAINING_ONLY'
    assert model.is_file()


def test_command_refuses_live_money_mode(tmp_path):
    source,p,g=inputs(tmp_path);output=tmp_path/'out'
    result=run_cli(['build','--source',source,'--plan',p,'--grants',g,'--output',output],tmp_path,'true')
    assert result.returncode==2 and not output.exists()
    assert str(tmp_path) not in result.stdout+result.stderr


def test_existing_output_refused_before_any_input_read(tmp_path,monkeypatch):
    cli=command_module(); output=tmp_path/'out';output.write_text('preserved')
    monkeypatch.setenv('TRADING_LIVE_MONEY_ACTIVE','false')
    monkeypatch.setattr(sys,'argv',['build_feature_training_data','build','--source','unused',
        '--plan','unused','--grants','unused','--output',str(output)])
    reads=[]
    def reader(*_args):
        reads.append(True)
        raise ValueError('must not read')
    monkeypatch.setattr(cli,'read_private_bytes',reader)
    assert cli.main()==2 and reads==[] and output.read_text()=='preserved'


def test_command_redacts_missing_private_inputs(tmp_path):
    result=run_cli(['build','--source',tmp_path/'private-source','--plan',tmp_path/'private-plan',
        '--grants',tmp_path/'private-grants','--output',tmp_path/'output'],tmp_path)
    assert result.returncode==2
    assert 'private-source' not in result.stderr and 'private-grants' not in result.stderr
    assert not (tmp_path/'output').exists()
