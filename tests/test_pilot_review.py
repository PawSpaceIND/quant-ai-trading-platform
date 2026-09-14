import json
from datetime import datetime, timezone

import pytest

from quant_ai.governance.pilot_review import review, sign_review


def test_baseline_cannot_approve_ai_strategy():
    with pytest.raises(ValueError, match="strategy-specific"):
        review({"tenant_id":"pilot","release_revision":"a"*40,"schema":"pramana.research.v1"},"strategy",tenant="pilot",revision="a"*40)


def test_review_is_release_bound_and_refuses_incomplete_recovery(tmp_path):
    artifact={"schema":"pramana.recovery.review.v1","tenant_id":"pilot","release_revision":"a"*40,"target_host":"qa","evidence_references":["isolated-fixture"]}
    with pytest.raises(ValueError, match="incomplete"):
        review(artifact,"recovery",tenant="pilot",revision="a"*40)
    artifact.update({k:True for k in ("clean_start","private_access","token_renewal","independent_alert_received","full_bundle_restored","rollback_verified")})
    source=tmp_path/'review.json';source.write_text(json.dumps(artifact))
    result=sign_review(source,gate="recovery",tenant="pilot",revision="a"*40,reviewer="QA fixture",secret="x"*32,now=datetime(2000,1,1,tzinfo=timezone.utc))
    assert len(result["signature"])==64
    assert json.loads(result["payload"])["expires_at"].startswith("2000-01-08")
    with pytest.raises(ValueError,match="exact release"):
        sign_review(source,gate="recovery",tenant="pilot",revision="b"*40,reviewer="QA",secret="x"*32)


def test_strategy_review_applies_numeric_policy_and_rejects_invalid_metrics():
    artifact={"schema":"pramana.strategy.review.v1","tenant_id":"pilot","release_revision":"a"*40,"strategy_id":"synthetic-test-only","strategy_config_sha256":"b"*64,"evidence_references":["synthetic-unit-fixture"],"holdout_reviewed":True,"costs_reviewed":True,"trial_register_reviewed":True,"ai_calibration_reviewed":True,"sample_trades":100,"expectancy":"1","max_drawdown":"0.05","profit_factor":"1.5","profitable_regimes":2,"paper_days":30}
    review(artifact,"strategy",tenant="pilot",revision="a"*40)
    with pytest.raises(ValueError,match="policy rejected"):
        review({**artifact,"expectancy":"-1"},"strategy",tenant="pilot",revision="a"*40)
    with pytest.raises(ValueError,match="finite"):
        review({**artifact,"profit_factor":"Infinity"},"strategy",tenant="pilot",revision="a"*40)
