"""Named passing-control/removed-protection checks on disposable source copies."""
from __future__ import annotations

import ast
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from test_kite_history_mutations import (
    test_kite_history_guard_requires_passing_control_and_failing_mutant as run_guard,
)

ROOT=Path(__file__).resolve().parents[1]
TEST="tests/test_intelligence_input_integrity.py::"
MODULE="src/quant_ai/operations/intelligence_inputs.py"
TARGETS={
    "paper_only":"test_configuration_live_mode_refuses_before_factory",
    "aware_clock":"test_configuration_clock_refuses_before_factory",
    "provider_graph":"test_unrecognized_provider_graph_is_not_certified[provider_graph]",
    "unexpected_wrapper":"test_unrecognized_provider_graph_is_not_certified[unexpected_wrapper]",
    "registry_binding":"test_unrecognized_provider_graph_is_not_certified[registry_binding]",
    "adapter_count":"test_unrecognized_provider_graph_is_not_certified[adapter_count]",
    "unexpected_adapter":"test_unrecognized_provider_graph_is_not_certified[unexpected_adapter]",
    "unexpected_category":"test_unrecognized_provider_graph_is_not_certified[unexpected_category]",
}


def cases():
    source=(ROOT/MODULE).read_text()
    result=[]
    for node in ast.walk(ast.parse(source)):
        if isinstance(node,ast.Call) and isinstance(node.func,ast.Name) and node.func.id=="_require":
            name=node.args[1].value
            result.append({"id":name,"path":MODULE,"old":ast.get_source_segment(source,node),
                           "new":"None","test":TEST+TARGETS[name]})
    assert {row["id"] for row in result}==set(TARGETS)
    fred="src/quant_ai/intelligence/external/fred.py"
    entries=[
        ("macro_numeric_error",fred,"except InvalidOperation:","except OverflowError:","test_invalid_numeric_macro_record_refuses_as_provider_failure[garbled]"),
        ("macro_oldest_time",fred,"min(observed, default=now)","max(observed, default=now)","test_fred_mixed_age_cannot_refresh_an_older_indicator"),
        ("macro_clock",fred,'raise ValueError("fred_clock_must_be_aware")','pass',"test_fred_clock_must_be_aware_before_transport"),
        ("macro_finite",fred,'if not value.is_finite():','if False:',"test_fred_nonfinite_observation_refuses[NaN]"),
        ("macro_future",fred,'if stamp > now:','if False:',"test_fred_future_observation_cannot_hide_behind_oldest_time"),
        ("macro_date",fred,'if raw_date != day.isoformat():','if False:',"test_fred_observation_date_is_not_silently_reinterpreted[20260918]"),
        ("cli_arguments","scripts/inspect_intelligence_inputs.py",'if args:','if False:',"test_real_cli_redacts_and_never_claims_live_acceptance[bad_argument]"),
        ("cli_redaction","scripts/inspect_intelligence_inputs.py",'print("Intelligence configuration inspection refused; inspect the private configuration locally.", file=sys.stderr)','print(str(sys.exc_info()[1]), file=sys.stderr)',"test_real_cli_redacts_and_never_claims_live_acceptance[bad_config]"),
    ]
    for name,path,old,new,target in entries:
        result.append({"id":name,"path":path,"old":old,"new":new,"test":TEST+target})
    return result


CASES=cases()


@pytest.mark.parametrize("case",CASES,ids=[row["id"] for row in CASES])
def test_intelligence_guard_needs_control_and_specific_assertion_failure(tmp_path,case):
    run_guard(tmp_path,case)
    failures=[(row.get("name"),failure.get("message",""))
              for row in ET.parse(tmp_path/"mutant.xml").iter("testcase")
              for failure in row.findall("failure")]
    assert failures and all(name and message.startswith(("assert ","AssertionError:","Failed:"))
                            for name,message in failures),failures
