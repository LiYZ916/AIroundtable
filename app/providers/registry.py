from __future__ import annotations

from collections.abc import Iterable

from app.core.enums import ProviderKind, ProviderMode
from app.models import ProviderConfig
from app.providers.base import AIProvider
from app.providers.mock import MockAIProvider
from app.providers.web_chatgpt import ChatGPTWebProvider
from app.providers.web_deepseek import DeepSeekWebProvider
from app.providers.web_doubao import DoubaoWebProvider
from app.providers.web_kimi import KimiWebProvider
from app.providers.web_yuanbao import YuanbaoWebProvider


class ProviderRegistry:
    def __init__(self, providers: Iterable[AIProvider] = ()) -> None:
        self._providers: dict[str, AIProvider] = {}
        self._aliases: dict[str, str] = {}
        for provider in providers:
            self.add(provider)

    def add(self, provider: AIProvider) -> None:
        if provider.name in self._providers:
            raise ValueError(f"平台名称重复：{provider.name}")
        self._providers[provider.name] = provider
        for legacy_name in provider.config.metadata.get("legacy_names", []):
            self._aliases[str(legacy_name)] = provider.name

    def get(self, name: str) -> AIProvider:
        name = self._aliases.get(name, name)
        try:
            return self._providers[name]
        except KeyError as exc:
            raise KeyError(f"未注册平台：{name}") from exc

    def all(self) -> list[AIProvider]:
        return list(self._providers.values())

    def enabled(self) -> list[AIProvider]:
        return [item for item in self._providers.values() if item.config.enabled]

    def web(self) -> list[AIProvider]:
        return [item for item in self._providers.values() if item.config.kind == ProviderKind.WEB]

    def mocks(self) -> list[AIProvider]:
        return [item for item in self._providers.values() if item.config.kind == ProviderKind.MOCK]

    async def close_all(self) -> None:
        for provider in self._providers.values():
            await provider.close()


def build_default_registry() -> ProviderRegistry:
    mock_specs = [
        ("模拟分析师", "analyst", "析", "#0f766e"),
        ("模拟质疑者", "skeptic", "疑", "#7c3aed"),
        ("模拟执行顾问", "pragmatist", "行", "#c2410c"),
    ]
    providers: list[AIProvider] = []
    for name, role, avatar, accent in mock_specs:
        config = ProviderConfig(
            name=name,
            display_name=name,
            avatar_text=avatar,
            accent_color=accent,
            kind=ProviderKind.MOCK,
            mode=ProviderMode.MOCK,
            timeout_seconds=10,
            max_retries=1,
            metadata={"role": role, "implementation_status": "完整离线实现"},
        )
        providers.append(MockAIProvider(config, role=role))

    web_specs = [
        (
            "GPT",
            "G",
            "icon/chatGPT.png",
            "#168a62",
            ChatGPTWebProvider,
            "https://chatgpt.com/",
            "browser_profiles/chatgpt",
            "configs/chatgpt_selectors.json",
            ["chatgpt.com", "openai.com"],
            ["ChatGPT 网页（实验性）"],
        ),
        (
            "Kimi",
            "K",
            "icon/Kimi.png",
            "#6d5bd0",
            KimiWebProvider,
            "https://www.kimi.com/",
            "browser_profiles/kimi",
            "configs/kimi_selectors.json",
            ["kimi.com", "moonshot.cn"],
            [],
        ),
        (
            "元宝",
            "元",
            "icon/元宝.png",
            "#2563b8",
            YuanbaoWebProvider,
            "https://yuanbao.tencent.com/",
            "browser_profiles/yuanbao",
            "configs/yuanbao_selectors.json",
            ["yuanbao.tencent.com", "qq.com"],
            [],
        ),
        (
            "豆包",
            "豆",
            "icon/豆包.png",
            "#d05a32",
            DoubaoWebProvider,
            "https://www.doubao.com/chat/",
            "browser_profiles/doubao",
            "configs/doubao_selectors.json",
            ["doubao.com"],
            [],
        ),
        (
            "DeepSeek",
            "D",
            "icon/deepseek.png",
            "#4d6bfe",
            DeepSeekWebProvider,
            "https://chat.deepseek.com/",
            "browser_profiles/deepseek",
            "configs/deepseek_selectors.json",
            ["deepseek.com"],
            [],
        ),
    ]
    for name, avatar, avatar_path, accent, provider_type, url, profile, selectors, hosts, legacy in web_specs:
        config = ProviderConfig(
            name=name,
            display_name=name,
            avatar_text=avatar,
            avatar_path=avatar_path,
            accent_color=accent,
            kind=ProviderKind.WEB,
            enabled=False,
            mode=ProviderMode.AUTOMATIC,
            timeout_seconds=180,
            max_retries=1,
            browser_profile_dir=profile,
            login_url=url,
            selector_config=selectors,
            experimental=True,
            allowed_hosts=hosts,
            browser_channel="msedge",
            onboarding_required=True,
            allow_manual_fallback=False,
            metadata={
                "implementation_status": "实验性；托管 Edge 全自动，失败自动跳过",
                "selector_status": "未进行真实账号端到端验证",
                "legacy_names": legacy,
            },
        )
        providers.append(provider_type(config))
    return ProviderRegistry(providers)
