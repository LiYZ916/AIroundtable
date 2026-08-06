from __future__ import annotations

import asyncio
import webbrowser

from app.core.enums import RunStatus
from app.core.exceptions import EmptyResponseError
from app.models import ProviderConfig, utc_now
from app.providers.base import AIProvider


class ManualResponseProvider(AIProvider):
    """Safe semi-automatic adapter: the user controls login and sending."""

    def __init__(self, config: ProviderConfig) -> None:
        super().__init__(config)
        self._response_future: asyncio.Future[str] | None = None

    @property
    def requires_manual_response(self) -> bool:
        return True

    async def open_login_page(self) -> None:
        if not self.config.login_url:
            raise ValueError(f"{self.name} 未配置官方网页地址")
        opened = await asyncio.to_thread(webbrowser.open, self.config.login_url)
        if not opened:
            raise RuntimeError("系统浏览器未能打开网页")

    async def check_login_status(self) -> bool:
        # A generic browser cannot expose authentication state safely. The user
        # confirms it in the UI; no password/cookie/token is read by this adapter.
        self.session.last_checked_at = utc_now()
        return self.session.is_logged_in

    def confirm_login(self, logged_in: bool = True) -> None:
        self.session.is_logged_in = logged_in
        self.session.last_checked_at = utc_now()

    async def send_message(self, message: str) -> None:
        loop = asyncio.get_running_loop()
        if self._response_future and not self._response_future.done():
            self._response_future.cancel()
        self._response_future = loop.create_future()
        self.session.status = RunStatus.WAITING

    async def wait_for_response(self) -> str:
        if self._response_future is None:
            raise RuntimeError("尚未准备人工回答")
        result = (await self._response_future).strip()
        if not result:
            raise EmptyResponseError("人工粘贴的回答为空")
        return result

    def submit_manual_response(self, text: str) -> bool:
        if self._response_future is None or self._response_future.done():
            return False
        self._response_future.get_loop().call_soon_threadsafe(
            self._response_future.set_result, text
        )
        return True

    async def stop_generation(self) -> None:
        await super().stop_generation()
        if self._response_future and not self._response_future.done():
            self._response_future.cancel()

