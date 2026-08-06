from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.enums import ProviderKind, ProviderMode
from app.models import ProjectConfig, ProviderConfig, UserQuestion


def test_question_rejects_blank_text() -> None:
    with pytest.raises(ValidationError):
        UserQuestion(question="   ")


def test_provider_timeout_and_retries_are_validated() -> None:
    with pytest.raises(ValidationError):
        ProviderConfig(name="bad", timeout_seconds=0)
    with pytest.raises(ValidationError):
        ProviderConfig(name="bad", max_retries=99)


def test_project_config_round_trip() -> None:
    config = ProjectConfig(
        providers=[
            ProviderConfig(
                name="mock",
                kind=ProviderKind.MOCK,
                mode=ProviderMode.MOCK,
            )
        ]
    )
    restored = ProjectConfig.model_validate_json(config.model_dump_json())
    assert restored == config
    assert restored.providers[0].mode == ProviderMode.MOCK

