from __future__ import annotations

import asyncio
import json
from collections import Counter

from app.core.enums import DiscussionStage, ProviderKind, ProviderMode
from app.models import ProviderConfig
from app.providers import ManualResponseProvider, MockAIProvider, build_default_registry
from app.providers.web_base import ConfiguredWebProvider


def test_default_registry_has_three_enabled_mocks_and_experimental_web() -> None:
    registry = build_default_registry()
    enabled = registry.enabled()
    assert len(enabled) == 3
    assert all(provider.config.kind == ProviderKind.MOCK for provider in enabled)
    web = registry.get("ChatGPT 网页（实验性）")
    assert web.config.experimental is True
    assert web.config.enabled is False
    assert web.config.mode == ProviderMode.AUTOMATIC
    assert web.config.allow_manual_fallback is False
    assert {provider.name for provider in registry.web()} == {
        "GPT", "Kimi", "元宝", "豆包", "DeepSeek"
    }
    deepseek = registry.get("DeepSeek")
    assert deepseek.config.login_url == "https://chat.deepseek.com/"
    assert deepseek.config.browser_profile_dir == "browser_profiles/deepseek"
    assert deepseek.config.selector_config == "configs/deepseek_selectors.json"
    assert deepseek.config.avatar_path == "icon/deepseek.png"
    assert deepseek.config.allowed_hosts == ["deepseek.com"]


def test_online_providers_use_managed_edge_without_manual_fallback() -> None:
    registry = build_default_registry()
    for provider in registry.web():
        assert provider.config.browser_channel == "msedge"
        assert provider.config.browser_profile_dir.startswith("browser_profiles/")
        assert provider.config.allow_manual_fallback is False
        assert provider.config.timeout_seconds == 180
        assert provider.requires_manual_response is False


def test_mock_provider_supports_every_ai_stage() -> None:
    async def scenario() -> None:
        provider = MockAIProvider(
            ProviderConfig(name="mock", kind=ProviderKind.MOCK, mode=ProviderMode.MOCK),
            role="skeptic",
            delay=0,
        )
        for stage in (
            DiscussionStage.INDEPENDENT,
            DiscussionStage.REVIEW,
            DiscussionStage.REVISION,
            DiscussionStage.JUDGE,
            DiscussionStage.SYNTHESIS,
        ):
            raw = await provider.ask("用户问题：测试\n待评方案别名：方案 A", stage)
            assert isinstance(json.loads(raw), dict)

    asyncio.run(scenario())


def test_manual_provider_accepts_thread_safe_paste_back() -> None:
    async def scenario() -> None:
        provider = ManualResponseProvider(
            ProviderConfig(
                name="manual",
                kind=ProviderKind.MANUAL,
                mode=ProviderMode.SEMI_AUTOMATIC,
                login_url="https://example.invalid/",
            )
        )
        task = asyncio.create_task(provider.ask("prompt", DiscussionStage.INDEPENDENT))
        await asyncio.sleep(0)
        assert provider.submit_manual_response("人工回答") is True
        assert await task == "人工回答"
        assert provider.submit_manual_response("too late") is False

    asyncio.run(scenario())


def test_web_provider_ignores_historical_error_markers() -> None:
    before = Counter(["生成失败", "服务繁忙"])
    assert ConfiguredWebProvider._new_page_error_message(
        before, ["生成失败", "服务繁忙"]
    ) == ""
    assert ConfiguredWebProvider._new_page_error_message(
        before, ["生成失败", "服务繁忙", "生成失败"]
    ) == "生成失败"
    assert ConfiguredWebProvider._normalize_page_error_text("复制成功") == ""
    assert ConfiguredWebProvider._normalize_page_error_text(
        "系统服务繁忙，请稍后重试"
    ) == "服务繁忙"
