from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from typing import Any

from app.core.enums import DiscussionStage, RunStatus
from app.models import ProviderConfig, ProviderSession


class AIProvider(ABC):
    """Isolated provider contract used by the orchestration engine."""

    def __init__(self, config: ProviderConfig) -> None:
        self.config = config
        self.session = ProviderSession(
            provider_id=config.id,
            provider_name=config.name,
            local_profile_path=config.browser_profile_dir,
        )
        self._current_stage = DiscussionStage.PREPARING
        self._current_message = ""
        self._runtime_context: dict[str, Any] = {}
        self._stopped = False
        # One browser conversation cannot safely handle overlapping messages.
        # Calls are serialized per provider while different providers still run concurrently.
        self._call_lock = asyncio.Lock()

    @property
    def name(self) -> str:
        return self.config.name

    @property
    def requires_manual_response(self) -> bool:
        return False

    @abstractmethod
    async def open_login_page(self) -> None:
        """Open the official login page without handling credentials."""

    @abstractmethod
    async def check_login_status(self) -> bool:
        """Return whether the current locally persisted browser session appears logged in."""

    @abstractmethod
    async def send_message(self, message: str) -> None:
        """Submit or prepare a message for this provider."""

    @abstractmethod
    async def wait_for_response(self) -> str:
        """Wait until a non-empty response is available."""

    async def ask(
        self,
        message: str,
        stage: DiscussionStage,
        **runtime_context: Any,
    ) -> str:
        async with self._call_lock:
            cancel_check = runtime_context.get("_cancel_check")
            if callable(cancel_check):
                cancel_check()
            self._current_stage = stage
            self._current_message = message
            self._runtime_context = runtime_context
            self._stopped = False
            self.session.status = RunStatus.RUNNING
            await self.send_message(message)
            manual_callback = runtime_context.get("_manual_callback")
            if self.requires_manual_response and callable(manual_callback):
                manual_callback()
            result = await self.wait_for_response()
            if self._stopped:
                raise asyncio.CancelledError
            self.session.status = RunStatus.SUCCEEDED
            self.session.error_message = ""
            return result

    async def stop_generation(self) -> None:
        self._stopped = True
        self.session.status = RunStatus.CANCELLED

    async def reset_conversation(self) -> None:
        self._current_message = ""
        self._runtime_context = {}
        self._stopped = False
        self.session.status = RunStatus.IDLE

    async def clear_login_state(self) -> None:
        self.session.is_logged_in = False

    def submit_manual_response(self, text: str) -> bool:
        return False

    async def close(self) -> None:
        """Release adapter resources."""
