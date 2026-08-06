from __future__ import annotations

import asyncio
import json
import re
import shutil
import time
import webbrowser
from collections import Counter
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from app.core.enums import ProviderMode, RunStatus
from app.core.exceptions import LoginRequiredError, ProviderError
from app.models import ProviderConfig, utc_now
from app.providers.manual import ManualResponseProvider


class ConfiguredWebProvider(ManualResponseProvider):
    """Experimental selector-driven provider using an isolated Edge profile."""

    def __init__(self, config: ProviderConfig) -> None:
        super().__init__(config)
        self._playwright: Any = None
        self._context: Any = None
        self._page: Any = None
        self._selectors = self._load_selectors(config.selector_config)
        self._automatic_pending = False
        self._response_count_before_send = 0
        self._response_text_before_send = ""
        self._body_lines_before_send: set[str] = set()
        self._error_markers_before_send: Counter[str] = Counter()
        self._sent_at = 0.0
        self.playwright_available = self._detect_playwright()

    @property
    def requires_manual_response(self) -> bool:
        if self.config.mode == ProviderMode.SEMI_AUTOMATIC:
            return True
        return not self.playwright_available and self.config.allow_manual_fallback

    @property
    def _manual_fallback_enabled(self) -> bool:
        return (
            self.config.mode == ProviderMode.SEMI_AUTOMATIC
            or self.config.allow_manual_fallback
        )

    @staticmethod
    def _detect_playwright() -> bool:
        try:
            import playwright.async_api  # noqa: F401
        except ImportError:
            return False
        return True

    @staticmethod
    def _project_root() -> Path:
        return Path(__file__).resolve().parents[2]

    @classmethod
    def _resolve_project_path(cls, path_value: str) -> Path:
        path = Path(path_value)
        return path if path.is_absolute() else cls._project_root() / path

    @classmethod
    def _load_selectors(cls, path_value: str) -> dict[str, Any]:
        if not path_value:
            return {}
        path = cls._resolve_project_path(path_value)
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)

    def _host_allowed(self, url: str) -> bool:
        hostname = (urlparse(url).hostname or "").lower()
        if not hostname or not self.config.allowed_hosts:
            return True
        return any(
            hostname == allowed.lower() or hostname.endswith("." + allowed.lower())
            for allowed in self.config.allowed_hosts
        )

    async def _ensure_page(self) -> Any:
        if self._page and not self._page.is_closed():
            return self._page
        if not self.playwright_available:
            return None
        if not self._host_allowed(self.config.login_url):
            raise ProviderError(f"拒绝打开未授权域名：{self.config.login_url}")
        from playwright.async_api import async_playwright

        profile = self._resolve_project_path(self.config.browser_profile_dir)
        profile_root = (self._project_root() / "browser_profiles").resolve()
        resolved_profile = profile.resolve()
        if profile_root not in resolved_profile.parents:
            raise ProviderError(f"浏览器配置目录必须位于 {profile_root}")
        profile.mkdir(parents=True, exist_ok=True)
        try:
            self._playwright = await async_playwright().start()
            kwargs: dict[str, Any] = {
                "user_data_dir": str(profile),
                "headless": False,
            }
            if self.config.browser_channel:
                kwargs["channel"] = self.config.browser_channel
            self._context = await self._playwright.chromium.launch_persistent_context(**kwargs)
            self._page = (
                self._context.pages[0]
                if self._context.pages
                else await self._context.new_page()
            )
            await self._page.goto(
                self.config.login_url,
                wait_until="domcontentloaded",
                timeout=45_000,
            )
            return self._page
        except Exception as exc:
            self.session.error_message = f"独立 Edge 启动失败：{exc}"
            await self.close()
            if not self._manual_fallback_enabled:
                raise ProviderError(self.session.error_message) from exc
            self.config.mode = ProviderMode.SEMI_AUTOMATIC
            return None

    async def open_login_page(self) -> None:
        if self.playwright_available and await self._ensure_page() is not None:
            return
        if not self._manual_fallback_enabled:
            raise ProviderError("缺少 Playwright，无法启动托管 Edge 自动模式")
        opened = await asyncio.to_thread(webbrowser.open, self.config.login_url)
        if not opened:
            raise RuntimeError(f"系统浏览器未能打开 {self.config.display_name or self.name}")

    async def _first_visible(self, selector_group: str) -> Any:
        if not self._page:
            return None
        for selector in self._selectors.get(selector_group, []):
            try:
                locator = self._page.locator(selector)
                for index in range(min(await locator.count(), 20)):
                    candidate = locator.nth(index)
                    if await candidate.is_visible():
                        return candidate
            except Exception:
                continue
        return None

    async def _captcha_present(self) -> bool:
        return await self._first_visible("captcha_markers") is not None

    async def _logged_out_present(self) -> bool:
        return await self._first_visible("logged_out_markers") is not None

    @staticmethod
    def _normalize_page_error_text(value: str) -> str:
        text = re.sub(r"\s+", " ", value).strip()
        if not text:
            return ""
        signals = (
            ("请求失败", "请求失败"),
            ("生成失败", "生成失败"),
            ("服务繁忙", "服务繁忙"),
            ("服务器繁忙", "服务器繁忙"),
            ("网络异常", "网络异常"),
            ("内容过长", "内容过长"),
            ("发生错误", "发生错误"),
            ("出错了", "出错了"),
            ("请重试", "请重试"),
            ("something went wrong", "Something went wrong"),
            ("request failed", "Request failed"),
            ("generation failed", "Generation failed"),
            ("service unavailable", "Service unavailable"),
            ("server busy", "Server busy"),
            ("context length", "Context length exceeded"),
            ("network error", "Network error"),
            ("try again", "Try again"),
        )
        lowered = text.lower()
        for signal, label in signals:
            if signal.lower() in lowered:
                return label
        # role=alert 也会承载“复制成功”等普通通知；没有错误语义时忽略。
        return ""

    async def _visible_error_messages(self) -> list[str]:
        if not self._page:
            return []
        messages: list[str] = []
        for selector in self._selectors.get("error_markers", []):
            try:
                locator = self._page.locator(selector)
                for index in range(min(await locator.count(), 20)):
                    candidate = locator.nth(index)
                    if not await candidate.is_visible():
                        continue
                    try:
                        value = await candidate.inner_text()
                    except Exception:
                        value = ""
                    normalized = self._normalize_page_error_text(value)
                    if normalized:
                        messages.append(normalized)
            except Exception:
                continue
        return messages

    @staticmethod
    def _new_page_error_message(
        before: Counter[str], current: list[str]
    ) -> str:
        remaining = Counter(current) - before
        for message in current:
            if remaining[message] > 0:
                return message
        return ""

    async def _page_error_message(self) -> str:
        current = await self._visible_error_messages()
        return self._new_page_error_message(self._error_markers_before_send, current)

    async def _visible_body_lines(self) -> list[str]:
        if not self._page:
            return []
        try:
            body_text = await self._page.locator("body").inner_text()
        except Exception:
            return []
        return [line.strip() for line in body_text.splitlines() if line.strip()]

    async def _body_delta_response(self) -> str:
        lines = await self._visible_body_lines()
        new_lines = []
        for line in lines:
            if line in self._body_lines_before_send:
                continue
            if line in self._current_message or self._current_message in line:
                continue
            new_lines.append(line)
        return "\n".join(new_lines).strip()

    async def _response_collection(self) -> tuple[Any, int]:
        if not self._page:
            return None, 0
        for selector in self._selectors.get("response", []):
            try:
                locator = self._page.locator(selector)
                count = await locator.count()
                if count:
                    return locator, count
            except Exception:
                continue
        return None, 0

    async def _use_manual_or_raise(
        self,
        message: str,
        *,
        login_required: bool = False,
    ) -> None:
        self.session.error_message = message
        if not self._manual_fallback_enabled:
            error_type = LoginRequiredError if login_required else ProviderError
            raise error_type(message)
        self.config.mode = ProviderMode.SEMI_AUTOMATIC
        await super().send_message(self._current_message)

    async def check_login_status(self) -> bool:
        self.session.last_checked_at = utc_now()
        if not self.playwright_available or self._page is None:
            return self.session.is_logged_in
        if await self._captcha_present():
            self.session.error_message = "平台要求人工完成验证；程序不会绕过验证码"
            return False
        if await self._logged_out_present():
            self.session.is_logged_in = False
            self.session.error_message = "尚未登录，请先在独立 Edge 窗口完成官方登录"
            return False
        marker = await self._first_visible("login_markers")
        self.session.is_logged_in = marker is not None
        return self.session.is_logged_in

    async def send_message(self, message: str) -> None:
        page = await self._ensure_page() if self.playwright_available else None
        if page is None:
            await self._use_manual_or_raise("自动浏览器不可用，无法发送提示词")
            return
        if await self._captcha_present():
            await self._use_manual_or_raise(
                "检测到人工验证，请先在该平台的独立 Edge 窗口完成验证",
                login_required=True,
            )
            return
        if await self._logged_out_present():
            await self._use_manual_or_raise(
                "尚未登录，请先在该平台的独立 Edge 窗口完成官方登录",
                login_required=True,
            )
            return
        composer = await self._first_visible("composer")
        if composer is None:
            await self._use_manual_or_raise(
                "未找到输入框；请确认已经登录，或平台页面结构是否发生变化",
                login_required=True,
            )
            return
        responses, response_count = await self._response_collection()
        self._body_lines_before_send = set(await self._visible_body_lines())
        self._error_markers_before_send = Counter(await self._visible_error_messages())
        self._response_count_before_send = response_count
        self._response_text_before_send = ""
        if responses is not None and response_count:
            try:
                self._response_text_before_send = (
                    await responses.nth(response_count - 1).inner_text()
                ).strip()
            except Exception:
                pass
        try:
            await composer.fill(message)
        except Exception:
            try:
                await composer.click()
                await page.keyboard.press("Control+A")
                await page.keyboard.type(message)
            except Exception:
                await self._use_manual_or_raise("无法在网页输入框中填写提示词")
                return

        if self.config.mode != ProviderMode.AUTOMATIC:
            await super().send_message(message)
            self.session.status = RunStatus.WAITING
            return
        send_button = await self._first_visible("send_button")
        try:
            if send_button is not None:
                await send_button.click()
            else:
                await composer.press("Enter")
        except Exception as exc:
            await self._use_manual_or_raise(f"无法自动点击发送按钮：{exc}")
            return
        self._automatic_pending = True
        self._sent_at = time.monotonic()

    async def wait_for_response(self) -> str:
        if not self._automatic_pending:
            return await super().wait_for_response()
        stable_text = ""
        stable_checks = 0
        changed_response_seen = False
        stop_was_seen = False
        progress_callback = self._runtime_context.get("_progress_callback")
        while True:
            if self._stopped:
                raise asyncio.CancelledError
            if await self._captcha_present():
                raise LoginRequiredError("响应期间出现人工验证，请先在独立 Edge 窗口完成验证")
            if await self._logged_out_present():
                raise LoginRequiredError("登录状态已失效，请先在独立 Edge 窗口重新登录")
            page_error = await self._page_error_message()
            if page_error:
                self._automatic_pending = False
                raise ProviderError(f"平台页面报错：{page_error[:240]}")
            responses, response_count = await self._response_collection()
            locator = responses.nth(response_count - 1) if responses is not None and response_count else None
            text = (await locator.inner_text()).strip() if locator else ""
            response_changed = (
                response_count > self._response_count_before_send
                or bool(text and text != self._response_text_before_send)
            )
            if not response_changed and time.monotonic() - self._sent_at >= 2:
                fallback_text = await self._body_delta_response()
                if fallback_text:
                    text = fallback_text
                    response_changed = True
            if response_changed:
                changed_response_seen = True
            if not changed_response_seen and time.monotonic() - self._sent_at >= 90:
                self._automatic_pending = False
                raise ProviderError("发送后 90 秒仍未检测到新的回答内容")
            if changed_response_seen and text and text != stable_text and callable(progress_callback):
                progress_callback(text)
            stop_visible = await self._first_visible("stop_button") is not None
            stop_was_seen = stop_was_seen or stop_visible
            if changed_response_seen and text and text == stable_text and not stop_visible:
                stable_checks += 1
            else:
                stable_checks = 0
                if changed_response_seen:
                    stable_text = text
            required_stable_checks = 3 if stop_was_seen else 6
            if (
                stable_checks >= required_stable_checks
                and time.monotonic() - self._sent_at >= 4
            ):
                self._automatic_pending = False
                return text
            await asyncio.sleep(1)

    async def stop_generation(self) -> None:
        await super().stop_generation()
        stop = await self._first_visible("stop_button")
        if stop:
            try:
                await stop.click()
            except Exception:
                pass
        self._automatic_pending = False
        self._response_count_before_send = 0
        self._response_text_before_send = ""
        self._body_lines_before_send.clear()
        self._error_markers_before_send.clear()
        self._sent_at = 0.0

    async def reset_conversation(self) -> None:
        await super().reset_conversation()
        new_chat = await self._first_visible("new_chat")
        if new_chat:
            try:
                await new_chat.click()
                await asyncio.sleep(0.35)
            except Exception:
                pass
        self._automatic_pending = False
        self._body_lines_before_send.clear()
        self._error_markers_before_send.clear()
        self._sent_at = 0.0

    async def clear_login_state(self) -> None:
        await self.close()
        profile = self._resolve_project_path(self.config.browser_profile_dir).resolve()
        profile_root = (self._project_root() / "browser_profiles").resolve()
        if profile_root not in profile.parents:
            raise ProviderError(f"拒绝清理项目外配置目录：{profile}")
        if profile.exists() and profile.is_dir():
            shutil.rmtree(profile)
        profile.mkdir(parents=True, exist_ok=True)
        self.session.is_logged_in = False
        self.session.error_message = ""

    async def close(self) -> None:
        if self._context:
            try:
                await self._context.close()
            except Exception:
                pass
        if self._playwright:
            try:
                await self._playwright.stop()
            except Exception:
                pass
        self._page = None
        self._context = None
        self._playwright = None
