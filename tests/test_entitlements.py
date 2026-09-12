from quant_ai.entitlements.plans import PLAN_ENTITLEMENTS, Plan


def test_institutional_plan_is_multi_account() -> None:
    assert PLAN_ENTITLEMENTS[Plan.INSTITUTIONAL].multi_account
    assert not PLAN_ENTITLEMENTS[Plan.RESEARCH].api_access
