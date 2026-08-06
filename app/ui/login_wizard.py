from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from app.models import ProviderConfig
from app.ui.chat_widgets import AvatarLabel


class LoginWizard(QDialog):
    open_requested = Signal(str)
    open_all_requested = Signal()
    check_requested = Signal(str)
    confirmed = Signal(str)

    def __init__(self, providers: list[ProviderConfig], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.providers = providers
        self.setWindowTitle(f"{len(providers)} AI 独立 Edge 登录向导")
        self.resize(620, 410)
        layout = QVBoxLayout(self)
        self.step_label = QLabel()
        self.step_label.setProperty("wizardStep", True)
        heading = QHBoxLayout()
        heading.addWidget(self.step_label)
        heading.addStretch()
        open_all = QPushButton(f"一键打开 {len(providers)} 个独立 Edge")
        open_all.setObjectName("primaryButton")
        open_all.clicked.connect(self.open_all_requested.emit)
        heading.addWidget(open_all)
        layout.addLayout(heading)
        self.stack = QStackedWidget()
        self.status_labels: dict[str, QLabel] = {}
        for provider in providers:
            self.stack.addWidget(self._page(provider))
        layout.addWidget(self.stack, 1)
        nav = QHBoxLayout()
        self.back_button = QPushButton("上一步")
        self.next_button = QPushButton("下一步")
        self.finish_button = QPushButton("完成")
        self.finish_button.setObjectName("primaryButton")
        self.back_button.clicked.connect(self._back)
        self.next_button.clicked.connect(self._next)
        self.finish_button.clicked.connect(self.accept)
        nav.addWidget(self.back_button)
        nav.addStretch()
        nav.addWidget(self.next_button)
        nav.addWidget(self.finish_button)
        layout.addLayout(nav)
        self.stack.currentChanged.connect(self._update_navigation)
        self._update_navigation(0)

    def _page(self, provider: ProviderConfig) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        title_row = QHBoxLayout()
        title_row.addWidget(
            AvatarLabel(provider.avatar_text, provider.accent_color, provider.avatar_path)
        )
        title = QLabel(f"登录 {provider.display_name or provider.name}")
        title.setProperty("wizardTitle", True)
        title_row.addWidget(title)
        title_row.addStretch()
        layout.addLayout(title_row)
        description = QLabel(
            "应用将打开独立的持久化 Edge 配置。首次只需在官方网页中自行登录；以后提问、发送和读取回答均由软件自动完成。应用不会读取或保存密码、Cookie 或 Token。"
        )
        description.setWordWrap(True)
        layout.addWidget(description)
        status = QLabel("状态：尚未确认")
        self.status_labels[provider.name] = status
        layout.addWidget(status)
        buttons = QHBoxLayout()
        open_button = QPushButton("打开官方登录页")
        check_button = QPushButton("检测登录状态")
        confirm_button = QPushButton("我已登录并启用")
        open_button.clicked.connect(lambda _checked=False, name=provider.name: self.open_requested.emit(name))
        check_button.clicked.connect(lambda _checked=False, name=provider.name: self.check_requested.emit(name))
        confirm_button.clicked.connect(lambda _checked=False, name=provider.name: self._confirm(name))
        buttons.addWidget(open_button)
        buttons.addWidget(check_button)
        buttons.addWidget(confirm_button)
        layout.addLayout(buttons)
        layout.addStretch()
        return page

    def _confirm(self, name: str) -> None:
        self.status_labels[name].setText("状态：用户已确认登录，平台已启用")
        self.confirmed.emit(name)

    def set_provider_status(self, name: str, text: str) -> None:
        if name in self.status_labels:
            self.status_labels[name].setText(f"状态：{text}")

    def _back(self) -> None:
        self.stack.setCurrentIndex(max(0, self.stack.currentIndex() - 1))

    def _next(self) -> None:
        self.stack.setCurrentIndex(min(self.stack.count() - 1, self.stack.currentIndex() + 1))

    def _update_navigation(self, index: int) -> None:
        self.step_label.setText(f"第 {index + 1} / {self.stack.count()} 步")
        self.back_button.setEnabled(index > 0)
        self.next_button.setVisible(index < self.stack.count() - 1)
        self.finish_button.setVisible(index == self.stack.count() - 1)
