from importlib.util import find_spec


def test_actual_offline_fitter_is_available():
    assert find_spec("quant_ai.learning.fitting") is not None, "No actual numeric fitter is connected to the existing training wrapper"


def test_operator_training_entry_point_exists():
    from pathlib import Path
    assert (Path(__file__).resolve().parents[1] / "scripts/fit_shadow_candidate.py").is_file(), "No explicit offline candidate-fitting command"

import importlib.util
import json
import os
import subprocess
import sys
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Context, Decimal, localcontext
from pathlib import Path

import pytest

from quant_ai.learning import fitting
from quant_ai.learning.contracts import AccessPlane, KnowledgeCategory, RightsStatus, SourceGrant
from quant_ai.learning.shadow import (
    ShadowForecastWriter,
    ShadowModelBundle,
    predict,
    validate_shadow_lineage,
)

NOW = datetime(2025, 3, 1, tzinfo=timezone.utc)
ROOT = Path(__file__).resolve().parents[1]


def dataset():
    start = NOW - timedelta(days=5)
    rows = []
    for i in range(60):
        decision = start + timedelta(hours=i)
        rows.append({"row_id": f"row-{i}", "subject": "synthetic:NSE:ONE", "decision_at": decision.isoformat(),
            "observed_at": (decision-timedelta(seconds=30)).isoformat(),
            "available_at": (decision-timedelta(seconds=5)).isoformat(),
            "resolve_after": (decision+timedelta(hours=1)).isoformat(),
            "outcome_available_at": (decision+timedelta(hours=1,seconds=2)).isoformat(),
            "source_ids": ["synthetic"], "values": {"signal": "2" if i%2 else "-2"},
            "gross_return": "0.04" if i%2 else "0.004", "cost_fraction": "0.01"})
    return {"schema": fitting.SCHEMA, "partition": "training", "dataset_id": "synthetic-training",
        "cutoff": (NOW-timedelta(days=1)).isoformat(), "source_ids": ["synthetic"],
        "feature_names": ["signal"], "horizon_seconds": 3600, "maximum_feature_age_seconds": 120,
        "cost_policy_id": "synthetic-test-costs", "cost_policy_sha256": "a"*64,
        "adjustment_policy_id": "synthetic-unadjusted", "rows": rows}


def grant():
    return SourceGrant("synthetic", "synthetic-test-fixture", frozenset({KnowledgeCategory.MARKET}),
        frozenset({AccessPlane.TRAINING}), True, RightsStatus.INTERNAL, 120)


def raw(data):
    return json.dumps(data, sort_keys=True, separators=(",", ":")).encode()


def fit(data=None, **kwargs):
    return fitting.fit_candidate(raw(dataset() if data is None else data),
        source_grants=(grant(),), run_id="synthetic-run", candidate_id="synthetic-candidate",
        clock=kwargs.pop("clock",lambda: NOW), **kwargs)


def vector(value="2", now=NOW):
    return {"schema": "pramana.numeric_features.v1", "subject": "synthetic:NSE:ONE",
        "observed_at": now.isoformat(), "available_at": now.isoformat(),
        "source_ids": ["synthetic"], "values": {"signal": value}}


def test_fits_nonzero_coefficients_and_uses_supplied_costs_not_gross_success():
    result=fit()
    bundle=result.bundle
    model=bundle.validate()
    assert Decimal(model["coefficients"][0]) > 0
    assert abs(Decimal(model["intercept"])) < Decimal("0.000001")
    assert result.diagnostics["positive_after_cost_rows"] == 30
    assert result.diagnostics["optimization"]["converged"] is True
    assert Decimal(result.diagnostics["optimization"]["final_objective"]) < Decimal(result.diagnostics["optimization"]["initial_objective"])
    assert predict(bundle,vector("2"),now=NOW)[0] > Decimal("0.8")
    assert predict(bundle,vector("-2"),now=NOW)[0] < Decimal("0.2")
    assert bundle.dataset.source_snapshot_sha256 == fitting._digest(raw(dataset()))
    assert bundle.training_run.artifact_sha256 == fitting._digest(bundle.artifact)
    assert result.diagnostics["out_of_sample_evaluated"] is False
    assert result.diagnostics["calibration_verified"] is False
    assert result.diagnostics["trading_authorized"] is False


def test_known_symmetric_optimum_matches_an_independent_stationarity_oracle():
    model=fit().bundle.validate()
    with localcontext(Context(prec=40)):
        low, high = Decimal(0), Decimal(5)
        for _ in range(120):
            mid=(low+high)/2
            # For balanced x=+/-1 and l2=.1: .1*w = sigmoid(-w).
            if Decimal("0.1")*mid < 1/(1+mid.exp()): low=mid
            else: high=mid
        assert abs(Decimal(model["coefficients"][0])*2 - (low+high)/2) < Decimal("0.00001")


def test_gradient_matches_finite_difference_including_intercept():
    with localcontext(Context(prec=45)):
        rows=((Decimal("0.3"),Decimal("-0.7")),(Decimal("0.8"),Decimal("0.1")))
        labels=(Decimal(0),Decimal(1)); w=(Decimal("0.2"),Decimal("-0.4")); b=Decimal("0.1")
        _, g, gb=fitting._objective_gradient(rows,labels,w,b,Decimal("0.1"))
        eps=Decimal("1e-9")
        for j in range(3):
            wp=list(w); wm=list(w); bp=b; bm=b
            if j<2: wp[j]+=eps; wm[j]-=eps
            else: bp+=eps; bm-=eps
            lp=fitting._objective_gradient(rows,labels,wp,bp,Decimal("0.1"))[0]
            lm=fitting._objective_gradient(rows,labels,wm,bm,Decimal("0.1"))[0]
            assert abs((lp-lm)/(2*eps)-(g[j] if j<2 else gb)) < Decimal("1e-16")


def test_training_scale_is_folded_into_raw_serving_coefficients():
    data=dataset()
    for r in data["rows"]: r["values"]["signal"]=str(Decimal(r["values"]["signal"])*1000)
    a,b=fit(),fit(data)
    assert b.diagnostics["optimization"]["training_scales"] == ["2000"]
    assert abs(predict(a.bundle,vector("2"),now=NOW)[0]-predict(b.bundle,vector("2000"),now=NOW)[0]) < Decimal("1e-30")


def test_labels_change_learned_direction_without_a_handwritten_weight():
    original=fit()
    data=dataset()
    for r in data["rows"]: r["gross_return"]="0.004" if Decimal(r["values"]["signal"])>0 else "0.04"
    reversed_result=fit(data)
    assert Decimal(reversed_result.bundle.validate()["coefficients"][0]) < 0
    assert predict(reversed_result.bundle,vector(),now=NOW)[0] < predict(original.bundle,vector(),now=NOW)[0]


def test_determinism_does_not_depend_on_callers_decimal_context():
    original=fit()
    with localcontext(Context(prec=8,rounding="ROUND_DOWN")):
        second=fit()
    assert original.bundle.payload() == second.bundle.payload()
    assert original.diagnostics_json == second.diagnostics_json
    view=second.diagnostics; view["trading_authorized"]=True
    assert second.diagnostics["trading_authorized"] is False


def test_zero_features_do_not_invent_discrimination():
    data=dataset()
    for r in data["rows"]: r["values"]["signal"]="0"
    result=fit(data)
    assert result.bundle.validate()["coefficients"] == ["0"]
    assert predict(result.bundle,vector(),now=NOW)[0] == Decimal("0.5")


def test_finished_training_time_is_not_the_start_time():
    ended=NOW+timedelta(seconds=40); moments=iter([NOW,ended])
    result=fit(clock=lambda: next(moments))
    assert result.bundle.training_run.trained_at == ended
    with pytest.raises(ValueError,match="training_time"):
        predict(result.bundle,vector(),now=NOW)
    assert predict(result.bundle,vector(now=ended),now=ended)[0] > Decimal("0.8")


def test_real_fitted_bundle_records_through_existing_shadow_writer_once(tmp_path):
    result=fit()
    restored=ShadowModelBundle.from_payload(json.loads(json.dumps(result.bundle.payload())))
    with ShadowForecastWriter(tmp_path/"shadow.sqlite",tenant_id="synthetic",clock=lambda: NOW) as writer:
        first=writer.record(restored,vector(),pair_id="future-one")
        assert writer.record(restored,vector(),pair_id="future-one") == first
        assert validate_shadow_lineage(writer.journal.db,tenant_id="synthetic")["verified_forecasts"] == 1
    with ShadowForecastWriter(tmp_path/"shadow.sqlite",tenant_id="synthetic",clock=lambda: NOW+timedelta(minutes=3)) as writer:
        assert writer.record(restored,vector(),pair_id="future-one") == first
    assert restored.training_run.model_family == "decimal_logistic"


def rejection(data, grants=None, config=None):
    try:
        fitting._read_dataset(raw(data), (grant(),) if grants is None else grants,
                              fitting.FitConfig() if config is None else config, NOW)
    except (ValueError, TypeError, KeyError, AttributeError) as error:
        return type(error).__name__,str(error)
    return None


BAD = [
 ("partition", "training_partition_required"), ("schema", "dataset_schema"),
 ("future_cutoff", "future_dataset"), ("cost_digest", "cost_digest"),
 ("small", "row_count"), ("empty_features", "feature_schema"), ("illegal_name", "feature_schema"),
 ("horizon", "time_budget"), ("row_schema", "row_schema"),
 ("future_feature", "future_or_stale_feature"), ("stale_feature", "future_or_stale_feature"),
 ("unknown_label", "outcome_not_known_at_cutoff"), ("wrong_horizon", "outcome_not_known_at_cutoff"),
 ("order", "nonchronological_rows"), ("duplicate", "duplicate_row"),
 ("overlap", "overlapping_outcomes"), ("unknown_source", "unknown_row_source"),
 ("row_features", "row_feature_schema"), ("negative_cost", "return_or_cost_bound"),
 ("single_class", "class_support"), ("unused_source", "unused_declared_source")]


@pytest.mark.parametrize("case,reason", BAD, ids=[c[0] for c in BAD])
def test_rejects_bad_training_evidence_before_fit(case,reason,monkeypatch):
    data=dataset(); gs=(grant(),); first=data["rows"][0]
    if case=="partition": data["partition"]="holdout"
    elif case=="schema": data["extra"]=True
    elif case=="future_cutoff": data["cutoff"]=(NOW+timedelta(seconds=1)).isoformat()
    elif case=="cost_digest": data["cost_policy_sha256"]="unknown"
    elif case=="small": data["rows"]=data["rows"][:29]
    elif case=="empty_features": data["feature_names"]=[]
    elif case=="illegal_name": data["feature_names"]=["invalid-name"]
    elif case=="horizon": data["horizon_seconds"]=True
    elif case=="row_schema": first["label"]=True
    elif case=="future_feature": first["available_at"]=(NOW+timedelta(seconds=1)).isoformat()
    elif case=="stale_feature": first["observed_at"]=(datetime.fromisoformat(first["decision_at"])-timedelta(seconds=121)).isoformat()
    elif case=="unknown_label": first["outcome_available_at"]=NOW.isoformat()
    elif case=="wrong_horizon": first["resolve_after"]=first["decision_at"]
    elif case=="order": data["rows"][1],data["rows"][0]=data["rows"][0],data["rows"][1]
    elif case=="duplicate": data["rows"][1]["row_id"]=first["row_id"]
    elif case=="overlap":
        for key in ("decision_at","observed_at","available_at","resolve_after","outcome_available_at"):
            data["rows"][1][key]=(datetime.fromisoformat(data["rows"][1][key])-timedelta(minutes=30)).isoformat()
    elif case=="unknown_source": first["source_ids"]=["unknown"]
    elif case=="row_features": first["values"]={"other":"0"}
    elif case=="negative_cost": first["cost_fraction"]="-0.01"
    elif case=="single_class":
        for r in data["rows"]: r["gross_return"]="1"
    elif case=="unused_source":
        data["source_ids"].append("second"); gs=(grant(),replace(grant(),source_id="second"))
    assert rejection(data,gs) == ("ValueError","logistic_fit_"+reason)
    called=[]
    monkeypatch.setattr(fitting,"_fit",lambda *_: called.append(True))
    with pytest.raises(ValueError,match="logistic_fit_"+reason):
        fitting.fit_candidate(raw(data),source_grants=gs,run_id="r",candidate_id="c",clock=lambda:NOW)
    assert not called


@pytest.mark.parametrize("kind,reason",[("type","typed_source_grants_required"),("duplicate","duplicate_grant"),
    ("scope","source_grant_scope"),("plane","training_source_not_permitted"),
    ("pit","training_source_not_permitted"),("rights","training_source_not_permitted"),
    ("age","source_age_limit")])
def test_source_permissions_are_required_declarations(kind,reason):
    gs=(grant(),)
    if kind=="type": gs=({"source_id":"synthetic"},)
    if kind=="duplicate": gs=(grant(),grant())
    if kind=="scope": gs=(replace(grant(),source_id="other"),)
    if kind=="plane": gs=(replace(grant(),planes=frozenset({AccessPlane.RESEARCH})),)
    if kind=="pit": gs=(replace(grant(),point_in_time=False),)
    if kind=="rights": gs=(replace(grant(),rights_status=RightsStatus.UNVERIFIED),)
    if kind=="age": gs=(replace(grant(),max_age_seconds=10),)
    assert rejection(dataset(),gs) == ("ValueError","logistic_fit_"+reason)


@pytest.mark.parametrize("kind,reason",[("l2","regularization_bound"),("tol","tolerance_bound"),("iterations","iteration_bound")])
def test_optimizer_configuration_is_bounded(kind,reason):
    kwargs={"l2":Decimal(0)} if kind=="l2" else {"gradient_tolerance":Decimal("NaN")} if kind=="tol" else {"maximum_iterations":True}
    with pytest.raises(ValueError,match=reason): fitting.FitConfig(**kwargs)


def test_work_budget_is_checked_before_rows_are_fit(monkeypatch):
    monkeypatch.setattr(fitting,"MAX_WORK",1)
    assert rejection(dataset()) == ("ValueError","logistic_fit_work_budget")


def test_nonconverged_candidate_is_refused():
    with pytest.raises(ValueError,match="not_converged"):
        fit(config=fitting.FitConfig(maximum_iterations=1))


def test_completion_clock_cannot_move_back():
    moments=iter([NOW,NOW-timedelta(seconds=1)])
    with pytest.raises(ValueError,match="completion_clock_reversed"):
        fit(clock=lambda:next(moments))


def test_changed_implementation_is_not_certified(monkeypatch):
    calls=iter(["a"*64,"b"*64])
    monkeypatch.setattr(fitting,"implementation_digest",lambda:next(calls))
    with pytest.raises(ValueError,match="implementation_changed"): fit()


def load_cli():
    spec=importlib.util.spec_from_file_location("fit_shadow_candidate",ROOT/"scripts/fit_shadow_candidate.py")
    module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


def write_inputs(tmp_path):
    data=tmp_path/"training.json"; data.write_bytes(raw(dataset())); data.chmod(0o600)
    grants=tmp_path/"grants.json"; grants.write_text(json.dumps([{
        "source_id":"synthetic","provider":"synthetic-test-fixture","categories":["MARKET"],
        "planes":["TRAINING"],"point_in_time":True,"rights_status":"INTERNAL","max_age_seconds":120}]))
    grants.chmod(0o600)
    return data,grants


def invoke_cli(tmp_path, mode="false"):
    data,gs=write_inputs(tmp_path); output=tmp_path/"candidate.json"
    env={"PATH":os.environ.get("PATH",""),"HOME":str(tmp_path),"PYTHONDONTWRITEBYTECODE":"1","TRADING_LIVE_MONEY_ACTIVE":mode}
    result=subprocess.run([sys.executable,"-B",str(ROOT/"scripts/fit_shadow_candidate.py"),
        "--input",str(data),"--grants",str(gs),"--output",str(output),"--run-id","test-run",
        "--candidate-id","test-candidate"],env=env,capture_output=True,text=True,timeout=30,check=False)
    return result,output


def test_cli_emits_usable_bundle_not_a_trading_authorization(tmp_path):
    result,path=invoke_cli(tmp_path)
    assert result.returncode==0,result.stderr
    message=json.loads(result.stdout)
    assert message["mode"]=="TRAINING_ONLY" and message["trading_authorized"] is False
    assert message["rows"]==60 and "signal" not in result.stdout and "source_ids" not in result.stdout
    assert path.stat().st_mode & 0o777 == 0o600 and path.stat().st_nlink==1
    bundle=ShadowModelBundle.from_payload(json.loads(path.read_text()))
    assert bundle.training_run.candidate_id=="test-candidate"
    assert not list(tmp_path.glob(".shadow-fit-*"))


def test_cli_refuses_live_money_mode_before_writing(tmp_path):
    result,path=invoke_cli(tmp_path,"true")
    assert result.returncode==2 and not path.exists()


def test_existing_output_is_never_overwritten(tmp_path):
    path=tmp_path/"candidate.json"; path.write_text("prior-private-content")
    result,_=invoke_cli(tmp_path)
    assert result.returncode==2 and path.read_text()=="prior-private-content"
    assert "prior-private-content" not in result.stdout+result.stderr


@pytest.mark.parametrize("kind",["permissions","symlink","hardlink","oversize"])
def test_private_input_checks(kind,tmp_path):
    cli=load_cli(); path=tmp_path/"file"; path.write_bytes(b"x"); path.chmod(0o600)
    if kind=="permissions": path.chmod(0o644)
    if kind=="symlink": link=tmp_path/"link"; link.symlink_to(path); path=link
    if kind=="hardlink": os.link(path,tmp_path/"link")
    with pytest.raises((OSError,ValueError)):
        cli.read_private_bytes(path,0 if kind=="oversize" else 10)


def test_concurrent_output_creator_is_preserved(tmp_path,monkeypatch):
    cli=load_cli(); path=tmp_path/"candidate"; link=cli.os.link
    def competing(src,dst,**kwargs):
        path.write_bytes(b"concurrent-writer")
        return link(src,dst,**kwargs)
    monkeypatch.setattr(cli.os,"link",competing)
    error=None
    try: cli.publish_new_bundle(path,b"our-candidate")
    except OSError as caught: error=caught
    assert type(error) is FileExistsError
    assert path.read_bytes()==b"concurrent-writer"
    assert not list(tmp_path.glob(".shadow-fit-*"))


def test_fsync_failure_before_publish_leaves_no_candidate(tmp_path,monkeypatch):
    cli=load_cli(); path=tmp_path/"candidate"
    def fail(_): raise OSError("synthetic-failure")
    monkeypatch.setattr(cli.os,"fsync",fail)
    with pytest.raises(OSError): cli.publish_new_bundle(path,b"our-candidate")
    assert not path.exists() and not list(tmp_path.glob(".shadow-fit-*"))


def test_input_size_has_a_direct_boundary():
    with pytest.raises(ValueError,match="logistic_fit_input_size"):
        fitting._read_dataset(b"",(grant(),),fitting.FitConfig(),NOW)


def test_numeric_precision_has_a_direct_boundary():
    with pytest.raises(ValueError,match="numeric_precision_bound"): fitting._number("1e-25")


@pytest.mark.parametrize("values,reason",[("synthetic","sources_required"),(["synthetic","synthetic"],"duplicate_source")])
def test_source_list_has_direct_boundaries(values,reason):
    with pytest.raises(ValueError,match="logistic_fit_"+reason): fitting._sources(values)


def test_config_requires_its_typed_contract():
    error=None
    try: fit(config="not-config")
    except (ValueError,TypeError) as caught: error=caught
    assert type(error) is ValueError and str(error)=="logistic_fit_typed_config_required"


def test_numeric_logit_bound_precedes_optimization_arithmetic():
    with pytest.raises(ValueError,match="logit_bound"):
        fitting._objective_gradient(((Decimal(1),),),(Decimal(1),),(Decimal(61),),Decimal(0),Decimal("0.1"))


def test_worsening_objective_is_not_published(monkeypatch):
    called=[]
    def objective(*_):
        called.append(True)
        return Decimal(len(called)),(Decimal(1),),Decimal(1)
    monkeypatch.setattr(fitting,"_objective_gradient",objective)
    with pytest.raises(ValueError,match="invalid_optimization"):
        fitting._fit(((Decimal(1),),),(Decimal(1),),fitting.FitConfig(maximum_iterations=2))


def test_input_stat_change_is_not_adopted(tmp_path,monkeypatch):
    from types import SimpleNamespace
    cli=load_cli(); path=tmp_path/"file"; path.write_bytes(b"bounded"); path.chmod(0o600)
    real=Path.lstat
    def changed(p,*args,**kwargs):
        info=real(p,*args,**kwargs)
        if p != path: return info
        fields={key:getattr(info,key) for key in ("st_dev","st_ino","st_size","st_mtime_ns","st_ctime_ns","st_mode","st_uid","st_nlink")}
        fields["st_mtime_ns"]+=1
        return SimpleNamespace(**fields)
    monkeypatch.setattr(Path,"lstat",changed)
    with pytest.raises(ValueError,match="input changed"): cli.read_private_bytes(path,100)


def test_output_parent_must_already_be_private(tmp_path):
    cli=load_cli(); directory=tmp_path/"public"; directory.mkdir(mode=0o755); directory.chmod(0o755)
    with pytest.raises(ValueError,match="private output directory"):
        cli.publish_new_bundle(directory/"model",b"candidate")
    assert not (directory/"model").exists()


def test_existing_output_refuses_before_creating_staging_file(tmp_path,monkeypatch):
    cli=load_cli(); path=tmp_path/"existing"; path.write_bytes(b"old")
    called=[]; original=cli.tempfile.mkstemp
    def watch(*args,**kwargs):
        called.append(True)
        return original(*args,**kwargs)
    monkeypatch.setattr(cli.tempfile,"mkstemp",watch)
    error=None
    try: cli.publish_new_bundle(path,b"new")
    except (ValueError,OSError) as caught: error=caught
    assert type(error) is ValueError and str(error)=="output already exists"
    assert not called


def test_cli_existing_output_refuses_before_trainer(tmp_path,monkeypatch):
    cli=load_cli(); data,gs=write_inputs(tmp_path); path=tmp_path/"existing"; path.write_bytes(b"old")
    monkeypatch.setattr(sys,"argv",["fit","--input",str(data),"--grants",str(gs),"--output",str(path),"--run-id","r","--candidate-id","c"])
    called=[]
    monkeypatch.setattr(cli,"fit_candidate",lambda *args,**kwargs:called.append(True))
    assert cli.main()==2 and not called


@pytest.mark.parametrize("kind",["write_count","readback"])
def test_failed_output_write_is_not_published(kind,tmp_path,monkeypatch):
    cli=load_cli(); path=tmp_path/"model"; original=cli.os.fdopen
    class Handle:
        def __init__(self,handle): self.handle=handle
        def __enter__(self): return self
        def __exit__(self,*args): self.handle.close()
        def __getattr__(self,key): return getattr(self.handle,key)
        def write(self,value):
            count=self.handle.write(value)
            return count-1 if kind=="write_count" else count
        def read(self):
            value=self.handle.read()
            return b"different" if kind=="readback" else value
    monkeypatch.setattr(cli.os,"fdopen",lambda *a,**k:Handle(original(*a,**k)))
    with pytest.raises(OSError): cli.publish_new_bundle(path,b"candidate")
    assert not path.exists()


def test_cli_error_redaction_excludes_private_path(tmp_path):
    canary="synthetic-private-path-marker"
    result=subprocess.run([sys.executable,"-B",str(ROOT/"scripts/fit_shadow_candidate.py"),
        "--input",str(tmp_path/canary),"--grants",str(tmp_path/"grants"),"--output",str(tmp_path/"out"),
        "--run-id","r","--candidate-id","c"],capture_output=True,text=True,timeout=10,check=False,
        env={"PATH":os.environ.get("PATH",""),"HOME":str(tmp_path),"PYTHONDONTWRITEBYTECODE":"1","TRADING_LIVE_MONEY_ACTIVE":"false"})
    assert result.returncode==2 and canary not in result.stdout+result.stderr


def test_duplicate_json_keys_are_not_reinterpreted():
    payload=raw(dataset()).replace(b'"partition":"training"',b'"partition":"holdout","partition":"training"')
    with pytest.raises(ValueError,match="duplicate_json_key"):
        fitting.fit_candidate(payload,source_grants=(grant(),),run_id="r",candidate_id="c",clock=lambda:NOW)


@pytest.mark.parametrize("value",[{},[],["not-object"]])
def test_cli_grant_container_is_explicit(value):
    with pytest.raises(ValueError,match="source grant object required" if value else "source grants required"):
        load_cli().load_grants(json.dumps(value).encode())


@pytest.mark.parametrize("stage",["before_link","after_link"])
def test_process_death_during_publication_never_exposes_partial_bytes(stage,tmp_path):
    cli=load_cli(); expected=fit().bundle.payload(); content=raw(expected)
    source=tmp_path/"source.json"; source.write_bytes(content); source.chmod(0o600)
    destination=tmp_path/"candidate.json"
    program = """
import importlib.util, os, sys
from pathlib import Path
spec=importlib.util.spec_from_file_location('fitter_cli',sys.argv[1])
module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
original=module.os.link
stage=sys.argv[4]
def interrupted(source,destination,**kwargs):
    if stage=='before_link': os._exit(71)
    original(source,destination,**kwargs)
    os._exit(72)
module.os.link=interrupted
module.publish_new_bundle(Path(sys.argv[3]),Path(sys.argv[2]).read_bytes())
"""
    result=subprocess.run([sys.executable,"-B","-c",program,str(ROOT/"scripts/fit_shadow_candidate.py"),
        str(source),str(destination),stage],env={"PATH":os.environ.get("PATH",""),"HOME":str(tmp_path),
        "PYTHONDONTWRITEBYTECODE":"1","TRADING_LIVE_MONEY_ACTIVE":"false"},
        capture_output=True,text=True,timeout=15,check=False)
    assert result.returncode == (71 if stage=="before_link" else 72)
    if stage=="before_link":
        assert not destination.exists()
    else:
        assert destination.read_bytes()==content
        assert destination.stat().st_nlink==2
        with pytest.raises(ValueError,match="private bounded input required"):
            cli.read_private_bytes(destination,65536)
        # Cleanup is deliberate and confined to this disposable fixture only.
        for path in tmp_path.glob(".shadow-fit-*"): path.unlink()
        assert cli.read_private_bytes(destination,65536)==content
        restored=ShadowModelBundle.from_payload(json.loads(content))
        assert restored.training_run.artifact_sha256 == fitting._digest(restored.artifact)


def test_multifeature_fit_learns_opposite_signs_and_training_subset_only():
    data=dataset(); data["feature_names"]=["signal","inverse","constant"]
    for row in data["rows"]:
        v=Decimal(row["values"]["signal"])
        row["values"]={"signal":str(v),"inverse":str(-v),"constant":"0"}
    result=fit(data); weights=[Decimal(v) for v in result.bundle.validate()["coefficients"]]
    assert weights[0]>0 and weights[1]<0 and weights[2]==0
    assert abs(weights[0]+weights[1])<Decimal("1e-30")
    assert result.bundle.dataset.row_count==60
    assert result.diagnostics["optimization"]["training_scales"]==["2","2","1"]


def test_intercept_learns_training_base_rate_without_feature_signal():
    data=dataset()
    for i,row in enumerate(data["rows"]):
        row["values"]["signal"]="0"
        row["gross_return"]="0.04" if i<45 else "0.004"
    result=fit(data)
    assert [Decimal(v) for v in result.bundle.validate()["coefficients"]]==[Decimal(0)]
    assert abs(predict(result.bundle,vector(),now=NOW)[0]-Decimal("0.75"))<Decimal("0.00001")


def test_relabelled_declared_contract_changes_manifest_even_with_same_numbers():
    first=fit()
    data=dataset(); data["cost_policy_id"]="second-declared-test-policy"
    second=fit(data)
    assert first.bundle.training_run.configuration_sha256 != second.bundle.training_run.configuration_sha256
    assert first.bundle.dataset.source_snapshot_sha256 != second.bundle.dataset.source_snapshot_sha256
    assert first.bundle.training_run.dataset_manifest_sha256 != second.bundle.training_run.dataset_manifest_sha256
    assert first.bundle.training_run.artifact_sha256 != second.bundle.training_run.artifact_sha256


def test_training_document_cannot_append_an_unscored_holdout_section():
    data=dataset(); data["holdout"]={"values":["secret-unseen-feature"]}
    assert rejection(data)==("ValueError","logistic_fit_dataset_schema")


def test_timestamp_offsets_are_equivalent_in_training_order():
    first=fit(); data=dataset()
    india=timezone(timedelta(hours=5,minutes=30))
    for row in data["rows"]:
        for key in ("decision_at","observed_at","available_at","resolve_after","outcome_available_at"):
            row[key]=datetime.fromisoformat(row[key]).astimezone(india).isoformat()
    second=fit(data)
    assert first.bundle.artifact==second.bundle.artifact
    assert first.bundle.dataset.source_snapshot_sha256 != second.bundle.dataset.source_snapshot_sha256
