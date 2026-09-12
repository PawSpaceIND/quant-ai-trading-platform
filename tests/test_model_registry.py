from datetime import datetime, timezone

import pytest

from quant_ai.models.registry import ModelRegistry, ModelVersion


def test_model_registry_requires_unique_versions() -> None:
    registry = ModelRegistry()
    version = ModelVersion("regime", "1.0", datetime(2026, 1, 1, tzinfo=timezone.utc), "abc", True)
    registry.register(version)
    assert registry.approved_versions("regime") == (version,)
    with pytest.raises(ValueError):
        registry.register(version)
