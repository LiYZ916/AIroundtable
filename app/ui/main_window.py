from __future__ import annotations

import html
import json
from concurrent.futures import Future
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QUrl, Signal
from PySide6.QtGui import QCloseEvent, QDesktopServices, QIcon
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QTabWidget,
    QTextBrowser,
    QTextEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from app.core.enums import DiscussionStage, ProviderKind, ProviderMode, RunStatus
from app.models import DiscussionRecord, ProjectConfig, ProviderConfig, UserQuestion
from app.orchestration import EngineEvent, RoundtableEngine
from app.prompts.templates import DISCUSSION_STRATEGIES
from app.providers import ProviderRegistry
from app.services.exporter import DiscussionExporter, PRIVACY_NOTICE
from app.services.handoff import build_handoff_context
from app.services.local_data import LocalDataManager
from app.services.privacy import contains_sensitive_hint
from app.storage import SQLiteStorage
from app.ui.async_runner import AsyncRunner
from app.ui.chat_widgets import AvatarLabel, ChatStream, ScoreRadarWidget, StageProgressWidget
from app.ui.login_wizard import LoginWizard
from app.ui.styles import APP_STYLE
from app.ui.view_models import (
    ChatMessageView,
    DIMENSION_LABELS,
    MessageKind,
    format_ai_content,
    record_to_chat_messages,
)


class UiBridge(QObject):
    engine_event = Signal(object)
    run_finished = Signal(object)
    run_failed = Signal(str)
    provider_operation = Signal(object)


class InteractiveCheckBox(QCheckBox):
    """Make the full visible checkbox area clickable, not only its label glyphs."""

    def __init__(self, text: str = "", parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def hitButton(self, position) -> bool:  # noqa: N802 - Qt virtual method name
        return self.rect().contains(position)


class ProviderRow(QFrame):
    open_requested = Signal(str)
    check_requested = Signal(str)

    def __init__(self, config: ProviderConfig, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.config = config
        self.action_buttons: list[QPushButton] = []
        self.setProperty("providerRow", True)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 7, 8, 7)
        layout.setSpacing(7)
        self.avatar_label = AvatarLabel(
            config.avatar_text, config.accent_color, config.avatar_path
        )
        layout.addWidget(self.avatar_label)
        text = QVBoxLayout()
        text.setSpacing(1)
        self.name_label = QLabel(config.display_name or config.name)
        self.name_label.setProperty("providerName", True)
        self.status_label = QLabel("离线可用" if config.kind == ProviderKind.MOCK else "尚未登录")
        self.status_label.setProperty("providerStatus", True)
        text.addWidget(self.name_label)
        text.addWidget(self.status_label)
        layout.addLayout(text, 1)
        controls = QVBoxLayout()
        controls.setSpacing(3)
        self.enabled_check = InteractiveCheckBox("参与")
        self.enabled_check.setChecked(config.enabled)
        self.mode_combo = QComboBox()
        if config.kind == ProviderKind.MOCK:
            self.mode_combo.addItem("模拟", ProviderMode.MOCK)
        else:
            self.mode_combo.addItem("全自动", ProviderMode.AUTOMATIC)
            self.mode_combo.addItem("人工备用", ProviderMode.SEMI_AUTOMATIC)
        index = self.mode_combo.findData(config.mode)
        self.mode_combo.setCurrentIndex(max(index, 0))
        controls.addWidget(self.enabled_check)
        controls.addWidget(self.mode_combo)
        layout.addLayout(controls)
        if config.kind == ProviderKind.WEB:
            actions = QVBoxLayout()
            actions.setSpacing(3)
            self.open_button = QPushButton("登录")
            self.open_button.setProperty("compactAction", True)
            self.check_button = QPushButton("检测")
            self.check_button.setProperty("compactAction", True)
            self.open_button.clicked.connect(lambda: self.open_requested.emit(config.name))
            self.check_button.clicked.connect(lambda: self.check_requested.emit(config.name))
            self.action_buttons = [self.open_button, self.check_button]
            actions.addWidget(self.open_button)
            actions.addWidget(self.check_button)
            layout.addLayout(actions)

    def sync_to_config(self) -> None:
        self.config.enabled = self.enabled_check.isChecked()
        mode = self.mode_combo.currentData()
        self.config.mode = mode if isinstance(mode, ProviderMode) else ProviderMode(str(mode))

    def set_status(self, text: str, *, ok: bool | None = None) -> None:
        self.status_label.setText(text)
        self.status_label.setProperty("state", "ok" if ok is True else "error" if ok is False else "normal")
        self.status_label.style().unpolish(self.status_label)
        self.status_label.style().polish(self.status_label)

    def set_configuration_enabled(self, enabled: bool) -> None:
        self.enabled_check.setEnabled(enabled)
        self.mode_combo.setEnabled(enabled)
        for button in self.action_buttons:
            button.setEnabled(enabled)
        tooltip = "" if enabled else "讨论运行中，结束后可修改或检测平台"
        self.enabled_check.setToolTip(tooltip)
        self.mode_combo.setToolTip(tooltip)
        for button in self.action_buttons:
            button.setToolTip(tooltip)


class ManualResponseDialog(QDialog):
    submitted = Signal(str, str, str)

    def __init__(self, provider_name: str, stage: str, prompt: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.provider_name = provider_name
        self.stage = stage
        self.setWindowTitle(f"人工接管 — {provider_name}")
        self.resize(760, 620)
        layout = QVBoxLayout(self)
        notice = QLabel(
            "提示词已复制到剪贴板。请在官方网页中自行登录和发送；完成后只粘贴 AI 回答，不要粘贴密码、Cookie 或 Token。"
        )
        notice.setWordWrap(True)
        notice.setProperty("privacyNotice", True)
        layout.addWidget(notice)
        layout.addWidget(QLabel(f"阶段：{stage}"))
        prompt_view = QTextEdit()
        prompt_view.setReadOnly(True)
        prompt_view.setPlainText(prompt)
        layout.addWidget(prompt_view, 2)
        layout.addWidget(QLabel("AI 回答"))
        self.response_edit = QTextEdit()
        self.response_edit.setPlaceholderText("在这里粘贴该平台的回答……")
        layout.addWidget(self.response_edit, 2)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        submit = buttons.addButton("提交回答", QDialogButtonBox.ButtonRole.AcceptRole)
        submit.clicked.connect(self._submit)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _submit(self) -> None:
        text = self.response_edit.toPlainText().strip()
        if not text:
            QMessageBox.warning(self, "回答为空", "请先粘贴平台回答。")
            return
        self.submitted.emit(self.provider_name, self.stage, text)
        self.accept()


class MainWindow(QMainWindow):
    def __init__(self, project_root: Path, registry: ProviderRegistry, storage: SQLiteStorage) -> None:
        super().__init__()
        self.project_root = project_root
        self.registry = registry
        self.storage = storage
        self.exporter = DiscussionExporter(project_root / "exports")
        self.data_manager = LocalDataManager(project_root, storage)
        self.runner = AsyncRunner()
        self.bridge = UiBridge()
        self.bridge.engine_event.connect(self._on_engine_event)
        self.bridge.run_finished.connect(self._on_run_finished)
        self.bridge.run_failed.connect(self._on_run_failed)
        self.bridge.provider_operation.connect(self._on_provider_operation)
        self.engine = RoundtableEngine(
            registry,
            storage,
            self.bridge.engine_event.emit,
            interactive_recovery=True,
        )
        self.current_record: DiscussionRecord | None = None
        self.provider_rows: dict[str, ProviderRow] = {}
        self.manual_queue: list[EngineEvent] = []
        self.manual_dialog: ManualResponseDialog | None = None
        self.pending_manual: dict[str, EngineEvent] = {}
        self.login_wizard: LoginWizard | None = None
        self._ui_ready = False
        self._updating_provider_selection = False
        self._build_ui()
        if not self._load_saved_config():
            self._select_online_mode()
        self._refresh_history()
        self._refresh_role_options()
        self._connect_preference_controls()
        self._ui_ready = True
        self._persist_ui_preferences()

    def _build_ui(self) -> None:
        self.setWindowTitle("AI Roundtable — 五 AI 协同圆桌")
        app_icon = self.project_root / "icon" / "softwarecover.png"
        if app_icon.exists():
            self.setWindowIcon(QIcon(str(app_icon)))
        self.resize(1540, 940)
        self.setMinimumSize(1120, 720)
        self.setStyleSheet(APP_STYLE)
        central = QWidget()
        outer = QVBoxLayout(central)
        outer.setContentsMargins(12, 10, 12, 10)
        outer.setSpacing(8)
        header = QHBoxLayout()
        title_box = QVBoxLayout()
        title = QLabel("AI Roundtable")
        title.setObjectName("appTitle")
        subtitle = QLabel("GPT · Kimi · 元宝 · 豆包 · DeepSeek 协同讨论")
        subtitle.setObjectName("appSubtitle")
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        header.addLayout(title_box)
        header.addStretch()
        self.global_status = QLabel("就绪")
        self.global_status.setObjectName("globalStatus")
        header.addWidget(self.global_status)
        outer.addLayout(header)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self._build_left_panel())
        splitter.addWidget(self._build_chat_panel())
        splitter.addWidget(self._build_insight_panel())
        splitter.setSizes([290, 820, 390])
        splitter.setCollapsible(1, False)
        outer.addWidget(splitter, 1)
        self.setCentralWidget(central)
        self.statusBar().showMessage("输入主题后开始圆桌讨论")

    def _build_left_panel(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("sidePanel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(10, 10, 10, 10)
        top = QHBoxLayout()
        top.addWidget(QLabel("参与者"))
        top.addStretch()
        self.login_wizard_button = QPushButton("登录向导")
        self.login_wizard_button.setObjectName("primaryButton")
        self.login_wizard_button.clicked.connect(self._show_login_wizard)
        top.addWidget(self.login_wizard_button)
        layout.addLayout(top)
        provider_scroll = QScrollArea()
        provider_scroll.setWidgetResizable(True)
        provider_scroll.setFrameShape(QFrame.Shape.NoFrame)
        provider_container = QWidget()
        provider_layout = QVBoxLayout(provider_container)
        provider_layout.setContentsMargins(0, 0, 0, 0)
        provider_layout.setSpacing(6)
        web_label = QLabel("在线五 AI（实验性）")
        web_label.setProperty("sectionLabel", True)
        provider_layout.addWidget(web_label)
        for provider in self.registry.web():
            row = ProviderRow(provider.config)
            row.open_requested.connect(self._open_provider)
            row.check_requested.connect(self._check_provider)
            self.provider_rows[provider.name] = row
            provider_layout.addWidget(row)
        mock_label = QLabel("离线演示")
        mock_label.setProperty("sectionLabel", True)
        provider_layout.addWidget(mock_label)
        for provider in self.registry.mocks():
            row = ProviderRow(provider.config)
            self.provider_rows[provider.name] = row
            provider_layout.addWidget(row)
        provider_layout.addStretch()
        provider_scroll.setWidget(provider_container)
        layout.addWidget(provider_scroll, 3)
        mode_row = QHBoxLayout()
        self.online_mode_button = QPushButton("启用全自动五 AI")
        self.no_gpt_mode_button = QPushButton("四 AI 测试（无 GPT）")
        self.offline_mode_button = QPushButton("离线演示")
        self.online_mode_button.clicked.connect(self._activate_online_mode)
        self.no_gpt_mode_button.clicked.connect(self._select_no_gpt_mode)
        self.offline_mode_button.clicked.connect(self._select_offline_mode)
        mode_row.addWidget(self.online_mode_button)
        mode_row.addWidget(self.no_gpt_mode_button)
        mode_row.addWidget(self.offline_mode_button)
        layout.addLayout(mode_row)
        layout.addWidget(QLabel("讨论历史"))
        self.history_search = QLineEdit()
        self.history_search.setClearButtonEnabled(True)
        self.history_search.setPlaceholderText("搜索主题、问题或状态…")
        self.history_search.textChanged.connect(self._refresh_history)
        layout.addWidget(self.history_search)
        self.history_list = QListWidget()
        self.history_list.itemDoubleClicked.connect(lambda _item: self._load_selected_history())
        self.history_list.currentItemChanged.connect(self._update_history_action_state)
        layout.addWidget(self.history_list, 2)
        history_actions = QHBoxLayout()
        self.history_load_button = QPushButton("恢复")
        self.history_delete_button = QPushButton("删除")
        self.history_load_button.setEnabled(False)
        self.history_delete_button.setEnabled(False)
        self.history_load_button.setToolTip("请先选择一条讨论记录")
        self.history_delete_button.setToolTip("请先选择一条讨论记录")
        self.history_load_button.clicked.connect(self._load_selected_history)
        self.history_delete_button.clicked.connect(self._delete_selected_history)
        history_actions.addWidget(self.history_load_button)
        history_actions.addWidget(self.history_delete_button)
        layout.addLayout(history_actions)
        self.clear_data_button = QPushButton("清除全部本地数据")
        self.clear_data_button.setObjectName("dangerButton")
        self.clear_data_button.clicked.connect(self._clear_all_data)
        self.open_logs_button = QPushButton("打开运行日志")
        self.open_logs_button.clicked.connect(self._open_log_directory)
        layout.addWidget(self.open_logs_button)
        layout.addWidget(self.clear_data_button)
        return panel

    def _open_log_directory(self) -> None:
        log_directory = self.project_root / "logs"
        log_directory.mkdir(parents=True, exist_ok=True)
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(log_directory.resolve()))):
            QMessageBox.warning(self, "无法打开日志", f"请手动打开：{log_directory}")

    def _build_chat_panel(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("chatPanel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(12, 10, 12, 10)
        self.stage_progress = StageProgressWidget()
        layout.addWidget(self.stage_progress)
        self.chat_stream = ChatStream()
        self.chat_stream.action_requested.connect(self._resolve_provider_action)
        layout.addWidget(self.chat_stream, 1)
        composer = QFrame()
        composer.setObjectName("composer")
        composer_layout = QVBoxLayout(composer)
        composer_layout.setContentsMargins(10, 9, 10, 9)
        self.question_edit = QTextEdit()
        self.question_edit.setPlaceholderText("输入一个主题，让五个 AI 独立回答、融合互评并给出最终结论……")
        self.question_edit.setMaximumHeight(105)
        composer_layout.addWidget(self.question_edit)
        self.context_toggle = QToolButton()
        self.context_toggle.setText("补充背景与约束 ▾")
        self.context_toggle.setCheckable(True)
        self.context_toggle.toggled.connect(self._toggle_context)
        composer_layout.addWidget(self.context_toggle)
        self.context_panel = QWidget()
        context_layout = QHBoxLayout(self.context_panel)
        context_layout.setContentsMargins(0, 0, 0, 0)
        self.background_edit = QTextEdit()
        self.background_edit.setPlaceholderText("背景信息")
        self.background_edit.setMaximumHeight(80)
        self.constraints_edit = QTextEdit()
        self.constraints_edit.setPlaceholderText("预算、时间、风险或输出约束")
        self.constraints_edit.setMaximumHeight(80)
        context_layout.addWidget(self.background_edit)
        context_layout.addWidget(self.constraints_edit)
        self.context_panel.hide()
        composer_layout.addWidget(self.context_panel)
        settings = QGridLayout()
        settings.setHorizontalSpacing(8)
        settings.setVerticalSpacing(5)
        settings.addWidget(QLabel("主持"), 0, 0)
        self.moderator_combo = QComboBox()
        self.moderator_combo.addItems([provider.name for provider in self.registry.all()])
        self.moderator_combo.setCurrentText("GPT")
        settings.addWidget(self.moderator_combo, 0, 1)
        settings.addWidget(QLabel("裁判"), 0, 2)
        self.judge_combo = QComboBox()
        self.judge_combo.addItems([provider.name for provider in self.registry.all()])
        self.judge_combo.setCurrentText("Kimi")
        settings.addWidget(self.judge_combo, 0, 3)
        settings.addWidget(QLabel("轮数"), 0, 4)
        self.rounds_spin = QSpinBox()
        self.rounds_spin.setRange(1, 3)
        self.rounds_spin.setValue(1)
        settings.addWidget(self.rounds_spin, 0, 5)
        self.anonymous_check = InteractiveCheckBox("匿名互评")
        self.anonymous_check.setChecked(True)
        self.revision_check = InteractiveCheckBox("修订")
        self.revision_check.setChecked(True)
        settings.addWidget(self.anonymous_check, 1, 0, 1, 2)
        settings.addWidget(self.revision_check, 1, 2)
        self.multi_judge_check = InteractiveCheckBox()
        self.multi_judge_check.setChecked(False)
        self.multi_judge_check.hide()
        self.jury_mode_label = QLabel("评分：自动无利益冲突评审团")
        self.jury_mode_label.setProperty("juryMode", True)
        self.jury_mode_label.setToolTip("参赛裁判不会评价自己的方案；其他成功参与者自动交叉评分")
        settings.addWidget(self.jury_mode_label, 1, 3, 1, 3)
        settings.addWidget(QLabel("策略"), 2, 0)
        self.strategy_combo = QComboBox()
        self.strategy_combo.addItems(DISCUSSION_STRATEGIES)
        self.strategy_combo.setToolTip(
            "选择互评、修订、评分与综合共同遵循的讨论重点"
        )
        settings.addWidget(self.strategy_combo, 2, 1, 1, 2)
        self.preference_status = QLabel("设置会自动保存")
        self.preference_status.setProperty("preferenceStatus", True)
        settings.addWidget(self.preference_status, 2, 3, 1, 3)
        settings.setColumnStretch(6, 1)
        composer_layout.addLayout(settings)
        actions = QHBoxLayout()
        privacy = QLabel("不要提交密码、Cookie、Token 或其他敏感信息")
        privacy.setProperty("privacyHint", True)
        actions.addWidget(privacy)
        actions.addStretch()
        self.stop_button = QPushButton("终止")
        self.stop_button.setObjectName("dangerButton")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self._stop_discussion)
        self.start_button = QPushButton("发送主题")
        self.start_button.setObjectName("primaryButton")
        self.start_button.clicked.connect(self._start_discussion)
        actions.addWidget(self.stop_button)
        actions.addWidget(self.start_button)
        composer_layout.addLayout(actions)
        layout.addWidget(composer)
        return panel

    def _build_insight_panel(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("insightPanel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(10, 10, 10, 10)
        self.insight_tabs = QTabWidget()
        conclusion = QWidget()
        conclusion_layout = QVBoxLayout(conclusion)
        self.result_view = QTextBrowser()
        self.result_view.setPlaceholderText("主持综合完成后显示最终结论")
        conclusion_layout.addWidget(self.result_view)
        self.insight_tabs.addTab(conclusion, "结论")
        structure = QWidget()
        structure_layout = QVBoxLayout(structure)
        self.consensus_view = QTextBrowser()
        self.consensus_view.setPlaceholderText("共识、分歧、风险与待确认条件")
        structure_layout.addWidget(self.consensus_view)
        self.insight_tabs.addTab(structure, "共识 / 分歧")
        synergy_page = QWidget()
        synergy_layout = QVBoxLayout(synergy_page)
        self.synergy_view = QTextBrowser()
        self.synergy_view.setPlaceholderText("显示跨方案组合产生的新价值、关键取舍与验证计划")
        synergy_layout.addWidget(self.synergy_view)
        self.insight_tabs.addTab(synergy_page, "协同增益")
        score_page = QWidget()
        score_layout = QVBoxLayout(score_page)
        self.score_radar = ScoreRadarWidget()
        self.score_summary = QLabel("尚无评分")
        self.score_summary.setWordWrap(True)
        self.effectiveness_view = QTextBrowser()
        self.effectiveness_view.setPlaceholderText("完成评分后显示每轮讨论前后的八维变化")
        score_layout.addWidget(self.score_radar)
        score_layout.addWidget(self.score_summary)
        score_layout.addWidget(self.effectiveness_view, 1)
        score_layout.addStretch()
        self.insight_tabs.addTab(score_page, "评分")
        layout.addWidget(self.insight_tabs, 1)
        export_row = QHBoxLayout()
        self.copy_result_button = QPushButton("复制")
        self.export_markdown_button = QPushButton("Markdown")
        self.export_json_button = QPushButton("JSON")
        self.continue_button = QPushButton("基于本轮继续")
        self.copy_result_button.clicked.connect(self._copy_result)
        self.export_markdown_button.clicked.connect(self._export_markdown)
        self.export_json_button.clicked.connect(self._export_json)
        self.continue_button.clicked.connect(self._continue_from_current_record)
        export_row.addWidget(self.continue_button)
        export_row.addWidget(self.copy_result_button)
        export_row.addWidget(self.export_markdown_button)
        export_row.addWidget(self.export_json_button)
        self._set_result_actions_enabled(False)
        layout.addLayout(export_row)
        notice = QLabel(PRIVACY_NOTICE)
        notice.setWordWrap(True)
        notice.setProperty("privacyHint", True)
        layout.addWidget(notice)
        return panel

    def _toggle_context(self, expanded: bool) -> None:
        self.context_panel.setVisible(expanded)
        self.context_toggle.setText("收起背景与约束 ▴" if expanded else "补充背景与约束 ▾")

    def _connect_preference_controls(self) -> None:
        for name, row in self.provider_rows.items():
            row.enabled_check.toggled.connect(
                lambda checked, provider_name=name: self._on_provider_toggled(
                    provider_name, checked
                )
            )
            row.mode_combo.currentIndexChanged.connect(self._persist_ui_preferences)
        self.rounds_spin.valueChanged.connect(self._persist_ui_preferences)
        self.anonymous_check.toggled.connect(self._persist_ui_preferences)
        self.revision_check.toggled.connect(self._persist_ui_preferences)
        self.strategy_combo.currentTextChanged.connect(self._persist_ui_preferences)
        self.moderator_combo.currentTextChanged.connect(self._persist_ui_preferences)
        self.judge_combo.currentTextChanged.connect(self._persist_ui_preferences)

    def _on_provider_toggled(self, name: str, checked: bool) -> None:
        if not self._ui_ready or self._updating_provider_selection:
            return
        self._updating_provider_selection = True
        try:
            provider = self.registry.get(name)
            if checked:
                opposite = (
                    self.registry.mocks()
                    if provider.config.kind == ProviderKind.WEB
                    else self.registry.web()
                )
                for other in opposite:
                    self.provider_rows[other.name].enabled_check.setChecked(False)
            for row in self.provider_rows.values():
                row.sync_to_config()
        finally:
            self._updating_provider_selection = False
        self._refresh_role_options()
        self._persist_ui_preferences()

    def _refresh_role_options(
        self,
        preferred_moderator: str | None = None,
        preferred_judge: str | None = None,
    ) -> None:
        selected = [
            name for name, row in self.provider_rows.items() if row.enabled_check.isChecked()
        ]
        moderator = preferred_moderator or self.moderator_combo.currentText()
        judge = preferred_judge or self.judge_combo.currentText()
        was_updating = self._updating_provider_selection
        self._updating_provider_selection = True
        try:
            self.moderator_combo.clear()
            self.judge_combo.clear()
            self.moderator_combo.addItems(selected)
            self.judge_combo.addItems(selected)
            if selected:
                moderator = moderator if moderator in selected else selected[0]
                judge = (
                    judge
                    if judge in selected
                    else "Kimi"
                    if "Kimi" in selected
                    else selected[min(1, len(selected) - 1)]
                )
                self.moderator_combo.setCurrentText(moderator)
                self.judge_combo.setCurrentText(judge)
        finally:
            self._updating_provider_selection = was_updating

    def _apply_provider_preset(
        self,
        selected: set[str],
        moderator: str,
        judge: str,
        status: str,
    ) -> None:
        self._updating_provider_selection = True
        try:
            for provider in self.registry.all():
                row = self.provider_rows[provider.name]
                row.enabled_check.setChecked(provider.name in selected)
                if provider.name in selected and provider.config.kind == ProviderKind.WEB:
                    automatic_index = row.mode_combo.findData(ProviderMode.AUTOMATIC)
                    if automatic_index >= 0:
                        row.mode_combo.setCurrentIndex(automatic_index)
                row.sync_to_config()
        finally:
            self._updating_provider_selection = False
        self._refresh_role_options(moderator, judge)
        self.global_status.setText(status)
        self._persist_ui_preferences()

    def _select_online_mode(self) -> None:
        self._apply_provider_preset(
            {provider.name for provider in self.registry.web()},
            "GPT",
            "Kimi",
            "全自动五 AI",
        )

    def _activate_online_mode(self) -> None:
        self._select_online_mode()
        self._show_login_wizard()

    def _select_three_ai_mode(self) -> None:
        self._select_no_gpt_mode()

    def _select_no_gpt_mode(self) -> None:
        selected = {"Kimi", "元宝", "豆包", "DeepSeek"}
        self._apply_provider_preset(
            selected,
            "DeepSeek",
            "Kimi",
            "四 AI 测试：Kimi · 元宝 · 豆包 · DeepSeek",
        )

    def _select_offline_mode(self) -> None:
        self._apply_provider_preset(
            {provider.name for provider in self.registry.mocks()},
            "模拟分析师",
            "模拟质疑者",
            "离线演示模式",
        )

    def _sync_provider_config(self) -> list[str]:
        web_selected = any(
            self.provider_rows[provider.name].enabled_check.isChecked()
            for provider in self.registry.web()
        )
        if web_selected:
            for provider in self.registry.mocks():
                row = self.provider_rows[provider.name]
                row.enabled_check.setChecked(False)
                provider.config.enabled = False
        enabled = []
        for name, row in self.provider_rows.items():
            row.sync_to_config()
            if row.config.enabled:
                enabled.append(name)
        return enabled

    def _persist_ui_preferences(self, *_args: object) -> None:
        if not self._ui_ready or self._updating_provider_selection:
            return
        for row in self.provider_rows.values():
            row.sync_to_config()
        try:
            self._save_config()
            self.preference_status.setText("设置已自动保存")
            self.preference_status.setProperty("state", "ok")
        except Exception as exc:
            self.preference_status.setText(f"设置保存失败：{exc}")
            self.preference_status.setProperty("state", "error")
        self.preference_status.style().unpolish(self.preference_status)
        self.preference_status.style().polish(self.preference_status)

    def _set_configuration_enabled(self, enabled: bool) -> None:
        for row in self.provider_rows.values():
            row.set_configuration_enabled(enabled)
        for widget in (
            self.login_wizard_button,
            self.online_mode_button,
            self.no_gpt_mode_button,
            self.offline_mode_button,
            self.clear_data_button,
            self.question_edit,
            self.context_toggle,
            self.background_edit,
            self.constraints_edit,
            self.strategy_combo,
            self.moderator_combo,
            self.judge_combo,
            self.rounds_spin,
            self.anonymous_check,
            self.revision_check,
        ):
            widget.setEnabled(enabled)
        self.history_search.setEnabled(enabled)
        self.history_list.setEnabled(enabled)
        if enabled:
            self._update_history_action_state(self.history_list.currentItem())
        else:
            self.history_load_button.setEnabled(False)
            self.history_delete_button.setEnabled(False)
        self.preference_status.setText(
            "设置已自动保存" if enabled else "本轮运行中；结束后可修改设置"
        )

    def _set_result_actions_enabled(self, enabled: bool) -> None:
        for button in (
            self.copy_result_button,
            self.export_markdown_button,
            self.export_json_button,
            self.continue_button,
        ):
            button.setEnabled(enabled)
            button.setToolTip("" if enabled else "请先完成或恢复一轮讨论")

    def _update_history_action_state(
        self,
        current: QListWidgetItem | None,
        _previous: QListWidgetItem | None = None,
    ) -> None:
        enabled = current is not None
        self.history_load_button.setEnabled(enabled)
        self.history_delete_button.setEnabled(enabled)
        tooltip = "" if enabled else "请先选择一条讨论记录"
        self.history_load_button.setToolTip(tooltip)
        self.history_delete_button.setToolTip(tooltip)

    def _save_config(self) -> None:
        self.storage.save_config(
            ProjectConfig(
                rounds=self.rounds_spin.value(),
                concurrency=4,
                anonymous_review=self.anonymous_check.isChecked(),
                enable_revision=self.revision_check.isChecked(),
                multi_judge=False,
                moderator_name=self.moderator_combo.currentText(),
                judge_name=self.judge_combo.currentText(),
                discussion_strategy=self.strategy_combo.currentText(),
                providers=[provider.config for provider in self.registry.all()],
            )
        )

    def _load_saved_config(self) -> bool:
        saved = self.storage.load_config()
        if not saved:
            return False
        saved_by_name = {item.name: item for item in saved.providers}
        for provider in self.registry.all():
            legacy_names = provider.config.metadata.get("legacy_names", [])
            item = saved_by_name.get(provider.name)
            if item is None:
                item = next((saved_by_name.get(name) for name in legacy_names if saved_by_name.get(name)), None)
            if item is None:
                continue
            provider.config.enabled = item.enabled
            provider.config.mode = item.mode
            provider.config.timeout_seconds = (
                min(item.timeout_seconds, 180)
                if provider.config.kind == ProviderKind.WEB
                else item.timeout_seconds
            )
            provider.config.max_retries = item.max_retries
            row = self.provider_rows[provider.name]
            row.enabled_check.setChecked(item.enabled)
            mode_index = row.mode_combo.findData(item.mode)
            if mode_index >= 0:
                row.mode_combo.setCurrentIndex(mode_index)
        self.rounds_spin.setValue(min(saved.rounds, 3))
        self.anonymous_check.setChecked(saved.anonymous_review)
        self.revision_check.setChecked(saved.enable_revision)
        strategy_index = self.strategy_combo.findText(saved.discussion_strategy)
        self.strategy_combo.setCurrentIndex(max(strategy_index, 0))
        self.multi_judge_check.setChecked(False)
        if any(
            self.provider_rows[provider.name].enabled_check.isChecked()
            for provider in self.registry.web()
        ):
            for provider in self.registry.mocks():
                provider.config.enabled = False
                self.provider_rows[provider.name].enabled_check.setChecked(False)
        self._refresh_role_options(saved.moderator_name, saved.judge_name)
        return True

    def _show_login_wizard(self) -> None:
        self.login_wizard = LoginWizard(
            [provider.config for provider in self.registry.web()], self
        )
        self.login_wizard.open_requested.connect(self._open_provider)
        self.login_wizard.open_all_requested.connect(self._open_all_providers)
        self.login_wizard.check_requested.connect(self._check_provider)
        self.login_wizard.confirmed.connect(self._confirm_provider_login)
        self.login_wizard.show()

    def _open_all_providers(self) -> None:
        for provider in self.registry.web():
            self._open_provider(provider.name)

    def _open_provider(self, name: str) -> None:
        provider = self.registry.get(name)
        future = self.runner.submit(provider.open_login_page())
        self._future_to_operation(future, "打开登录页", name)

    def _check_provider(self, name: str) -> None:
        provider = self.registry.get(name)
        future = self.runner.submit(provider.check_login_status())
        self._future_to_operation(future, "检测登录", name)

    def _confirm_provider_login(self, name: str) -> None:
        provider = self.registry.get(name)
        provider.session.is_logged_in = True
        provider.config.enabled = True
        self.provider_rows[name].enabled_check.setChecked(True)
        self.provider_rows[name].set_status("用户已确认登录", ok=True)

    def _future_to_operation(self, future: Future, operation: str, name: str) -> None:
        def done(item: Future) -> None:
            try:
                self.bridge.provider_operation.emit((True, operation, name, item.result()))
            except Exception as exc:
                self.bridge.provider_operation.emit((False, operation, name, str(exc)))
        future.add_done_callback(done)

    def _on_provider_operation(self, result: tuple[bool, str, str, object]) -> None:
        ok, operation, name, detail = result
        if operation == "检测登录" and ok:
            logged_in = bool(detail)
            self.provider_rows[name].set_status("已登录" if logged_in else "未登录 / 需确认", ok=logged_in)
            if self.login_wizard:
                self.login_wizard.set_provider_status(name, "已登录" if logged_in else "未检测到登录")
        elif ok:
            self.provider_rows[name].set_status(f"{operation}完成", ok=True)
        else:
            self.provider_rows[name].set_status(f"{operation}失败", ok=False)
            if self.login_wizard:
                self.login_wizard.set_provider_status(name, f"失败：{detail}")
            QMessageBox.warning(self, f"{operation}失败", f"{name}：{detail}")

    def _start_discussion(self) -> None:
        names = self._sync_provider_config()
        if len(names) < 2:
            QMessageBox.warning(self, "参与者不足", "请至少选择两个 AI。")
            return
        try:
            question = UserQuestion(
                question=self.question_edit.toPlainText(),
                background=self.background_edit.toPlainText().strip(),
                constraints=self.constraints_edit.toPlainText().strip(),
                template_name=self.strategy_combo.currentText(),
            )
        except Exception as exc:
            QMessageBox.warning(self, "主题无效", str(exc))
            return
        combined = "\n".join((question.question, question.background, question.constraints))
        warning = "发送前请确认内容不含密码、Cookie、Token 或个人敏感信息。"
        if contains_sensitive_hint(combined):
            warning = "检测到可能的敏感信息关键词，建议先删除。\n\n" + warning
        if QMessageBox.question(self, "隐私确认", warning + "\n\n是否继续？") != QMessageBox.StandardButton.Yes:
            return
        moderator = self.moderator_combo.currentText()
        if moderator not in names:
            moderator = "GPT" if "GPT" in names else names[0]
            self.moderator_combo.setCurrentText(moderator)
        judge = self.judge_combo.currentText()
        if judge not in names:
            judge = "Kimi" if "Kimi" in names else names[min(1, len(names) - 1)]
            self.judge_combo.setCurrentText(judge)
        judges = [judge]
        self._save_config()
        self.chat_stream.clear_messages()
        self.result_view.clear()
        self.consensus_view.clear()
        self.score_radar.set_scores([])
        self.score_summary.setText("尚无评分")
        self.current_record = None
        self._set_result_actions_enabled(False)
        self.chat_stream.upsert(
            ChatMessageView(
                id=question.id,
                provider_name="你",
                stage=DiscussionStage.PREPARING,
                content=question.question,
                kind=MessageKind.USER,
            )
        )
        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self._set_configuration_enabled(False)
        self.global_status.setText("讨论进行中")
        future = self.runner.submit(
            self.engine.run(
                question,
                names,
                moderator,
                judges,
                rounds=self.rounds_spin.value(),
                concurrency=4,
                anonymous_review=self.anonymous_check.isChecked(),
                enable_revision=self.revision_check.isChecked(),
                multi_judge=False,
            )
        )
        def done(item: Future) -> None:
            try:
                self.bridge.run_finished.emit(item.result())
            except Exception as exc:
                self.bridge.run_failed.emit(str(exc))
        future.add_done_callback(done)

    def _stop_discussion(self) -> None:
        self.stop_button.setEnabled(False)
        self.global_status.setText("正在终止")
        self.runner.submit(self.engine.cancel())

    @staticmethod
    def _format_raw(raw: str) -> str:
        return format_ai_content(raw)

    def _on_engine_event(self, event: EngineEvent) -> None:
        self.stage_progress.set_stage(event.stage)
        self.statusBar().showMessage(event.message)
        if event.event_type in {
            "stage_started",
            "stage_completed",
            "stage_barrier",
            "judge_fallback",
            "moderator_fallback",
        }:
            self.chat_stream.upsert(
                ChatMessageView(
                    id=f"system-{event.created_at.timestamp()}-{event.event_type}",
                    provider_name="系统",
                    stage=event.stage,
                    content=event.message,
                    kind=MessageKind.SYSTEM,
                    status=event.status,
                    created_at=event.created_at,
                )
            )
            return
        if event.event_type == "manual_input_required":
            self.pending_manual[event.provider_name] = event
            self.manual_queue.append(event)
            self._show_next_manual_dialog()
        if not event.provider_name:
            return
        provider = self.registry.get(event.provider_name)
        raw = str(event.payload.get("raw", ""))
        content = event.message
        kind = MessageKind.AI
        status = event.status
        recovery = False
        if event.event_type == "provider_started":
            content = "正在思考…"
            status = RunStatus.RUNNING
        elif event.event_type == "provider_progress":
            content = format_ai_content(raw, pending=True) if raw else "正在生成…"
        elif event.event_type == "provider_completed":
            content = self._format_raw(raw)
            kind = MessageKind.FINAL if event.stage == DiscussionStage.SYNTHESIS else MessageKind.AI
        elif event.event_type == "provider_retry":
            content = event.message
            kind = MessageKind.ERROR
        elif event.event_type == "provider_action_required":
            content = f"{event.payload.get('error', event.message)}\n请选择人工接管、重试一次或跳过。"
            kind = MessageKind.ERROR
            recovery = True
        elif event.event_type == "provider_failed":
            content = event.message
            kind = MessageKind.ERROR
        elif event.event_type == "manual_input_required":
            content = "等待你在官方网页发送提示词并粘贴回答…"
            status = RunStatus.WAITING
        self.chat_stream.upsert(
            ChatMessageView(
                id=event.call_id or f"event-{event.created_at.timestamp()}",
                provider_name=event.provider_name,
                stage=event.stage,
                content=content,
                kind=kind,
                status=status,
                created_at=event.created_at,
                elapsed_seconds=float(event.payload.get("elapsed_seconds", 0)),
                retry_count=int(event.payload.get("retry_count", event.payload.get("attempt", 0))),
            ),
            provider.config,
            event.call_id,
            recovery=recovery,
        )
        row_state = True if event.status == RunStatus.SUCCEEDED else False if event.status == RunStatus.FAILED else None
        self.provider_rows[event.provider_name].set_status(event.status.value, ok=row_state)

    def _resolve_provider_action(self, call_id: str, action: str) -> None:
        self.runner.submit(self.engine.resolve_provider_action(call_id, action))
        bubble = self.chat_stream.bubbles.get(call_id)
        if bubble:
            bubble.hide_recovery_actions()

    def _show_next_manual_dialog(self) -> None:
        if self.manual_dialog is not None or not self.manual_queue:
            return
        event = self.manual_queue.pop(0)
        prompt = str(event.payload.get("prompt", ""))
        QApplication.clipboard().setText(prompt)
        self.manual_dialog = ManualResponseDialog(event.provider_name, event.stage.value, prompt, self)
        self.manual_dialog.submitted.connect(self._submit_manual_response)
        self.manual_dialog.finished.connect(self._manual_dialog_finished)
        self.manual_dialog.show()

    def _manual_dialog_finished(self, _result: int = 0) -> None:
        self.manual_dialog = None
        self._show_next_manual_dialog()

    def _submit_manual_response(self, name: str, stage: str, text: str) -> None:
        provider = self.registry.get(name)
        if not provider.submit_manual_response(text):
            QMessageBox.warning(self, "提交失败", "该平台已不再等待人工回答，可能已经超时。")
            return
        self.pending_manual.pop(name, None)
        if self.engine.current_record:
            self.storage.save_manual_response(self.engine.current_record.id, name, stage, text)

    def _on_run_finished(self, record: DiscussionRecord) -> None:
        self.current_record = record
        self.start_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        self._set_configuration_enabled(True)
        self._set_result_actions_enabled(True)
        self.global_status.setText("已完成" if record.status == RunStatus.SUCCEEDED else record.status.value)
        self.stage_progress.set_stage(record.current_stage)
        self._populate_insights(record)
        self._refresh_history()
        if self.engine.run_log_path:
            self.statusBar().showMessage(f"运行日志：{self.engine.run_log_path.name}")

    def _on_run_failed(self, message: str) -> None:
        self.start_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        self._set_configuration_enabled(True)
        self.global_status.setText("讨论失败")
        QMessageBox.critical(self, "讨论失败", message)
        self._refresh_history()

    def _populate_insights(self, record: DiscussionRecord) -> None:
        final = record.final_synthesis
        if final:
            self.result_view.setMarkdown(
                f"## 明确推荐方案\n\n{final.recommended_candidate or '未确定'}\n\n"
                f"## 推荐结论\n\n{final.recommendation}\n\n"
                + "## 选择依据\n\n"
                + ("\n".join(f"- {item}" for item in final.selection_rationale) or "- 无")
                + "\n\n"
                + "## 执行步骤\n\n"
                + "\n".join(f"{index}. {item}" for index, item in enumerate(final.execution_steps, 1))
                + f"\n\n**可信度：{final.confidence.value}**"
            )
            sections = [
                ("共识", final.consensus),
                ("分歧", final.disagreements),
                ("风险", final.risks),
                ("待确认", final.user_confirmations),
                ("未解决", final.unresolved_questions),
            ]
            self.consensus_view.setMarkdown(
                "\n\n".join(
                    f"## {title}\n\n" + ("\n".join(f"- {item}" for item in items) if items else "- 无")
                    for title, items in sections
                )
            )
            synergy_sections = [
                ("组合产生的新价值", final.synergy_gains),
                ("关键取舍", final.decisive_tradeoffs),
                ("验证与止损计划", final.validation_plan),
                ("少数意见保留", final.minority_report),
            ]
            contribution_lines = [
                f"- **{name}**：{value}" for name, value in final.contributions.items()
            ]
            self.synergy_view.setMarkdown(
                "\n\n".join(
                    f"## {title}\n\n"
                    + ("\n".join(f"- {item}" for item in items) if items else "- 无")
                    for title, items in synergy_sections
                )
                + "\n\n## 各 AI 的不可替代贡献\n\n"
                + ("\n".join(contribution_lines) if contribution_lines else "- 无")
            )
        scores = record.rounds[-1].scores if record.rounds else []
        self.score_radar.set_scores(scores)
        if final and (final.decision_scores or final.score_averages):
            displayed_scores = final.decision_scores or final.score_averages
            ranking = final.candidate_ranking or list(displayed_scores)
            self.score_summary.setText(
                "   ".join(
                    f"#{index} {alias} {displayed_scores.get(alias, 0):.2f}/10"
                    for index, alias in enumerate(ranking, 1)
                    if alias in displayed_scores
                )
            )
        effectiveness_sections = []
        for round_item in record.rounds:
            effect = round_item.effectiveness
            if not effect:
                continue
            dimensions = "，".join(
                f"{DIMENSION_LABELS.get(key, key)} {value:+.2f}"
                for key, value in effect.average_dimension_deltas.items()
            )
            providers = "\n".join(
                f"- {item.provider_name}：{item.before_score:.2f} → {item.after_score:.2f}（{item.overall_delta:+.2f}）"
                for item in effect.provider_results
            )
            effectiveness_sections.append(
                f"## 第 {effect.round_number} 轮 · {effect.verdict}\n\n"
                f"{effect.comparison_basis}；平均决策分 {effect.average_overall_delta:+.2f}\n\n"
                f"{providers or '- 缺少配对评分'}\n\n八维平均变化：{dimensions or '证据不足'}"
            )
        self.effectiveness_view.setMarkdown(
            "\n\n".join(effectiveness_sections) or "尚无讨论效果对比"
        )

    def _refresh_history(self, *_args: object) -> None:
        self.history_list.clear()
        query = self.history_search.text() if hasattr(self, "history_search") else ""
        for row in self.storage.list_discussions(query=query):
            item = QListWidgetItem(f"{row['updated_at'][:16]}  {row['title']}")
            item.setData(Qt.ItemDataRole.UserRole, row["id"])
            self.history_list.addItem(item)
        self._update_history_action_state(self.history_list.currentItem())

    def _load_selected_history(self) -> None:
        item = self.history_list.currentItem()
        if not item:
            return
        record = self.storage.load_discussion(item.data(Qt.ItemDataRole.UserRole))
        if not record:
            return
        self.current_record = record
        self._set_result_actions_enabled(True)
        self.chat_stream.clear_messages()
        for message in record_to_chat_messages(record):
            provider = None
            if message.provider_name not in {"你", "系统", "orchestrator", "本地降级综合器"}:
                try:
                    provider = self.registry.get(message.provider_name).config
                except KeyError:
                    provider = None
            self.chat_stream.upsert(message, provider)
        self.question_edit.setPlainText(record.question.question)
        self.background_edit.setPlainText(record.question.background)
        self.constraints_edit.setPlainText(record.question.constraints)
        strategy_index = self.strategy_combo.findText(record.question.template_name)
        if strategy_index >= 0:
            self.strategy_combo.setCurrentIndex(strategy_index)
        self.stage_progress.set_stage(record.current_stage)
        self._populate_insights(record)
        self.global_status.setText(f"已恢复 · {record.status.value}")

    def _continue_from_current_record(self) -> None:
        record = self.current_record
        if record is None or record.final_synthesis is None:
            QMessageBox.information(self, "无法继续", "请先完成或恢复一轮含最终结论的讨论。")
            return
        handoff = build_handoff_context(record)
        previous_background = record.question.background.strip()
        background_parts = [
            part
            for part in (previous_background, "【上轮讨论交接】\n" + handoff)
            if part
        ]
        self.question_edit.setPlainText(
            record.question.question
            + "\n\n请基于上轮交接材料，重点解决仍未解决的问题并给出下一步。"
        )
        self.background_edit.setPlainText("\n\n".join(background_parts))
        self.constraints_edit.setPlainText(record.question.constraints)
        strategy_index = self.strategy_combo.findText(record.question.template_name)
        if strategy_index >= 0:
            self.strategy_combo.setCurrentIndex(strategy_index)
        self.context_toggle.setChecked(True)
        self.question_edit.setFocus()
        self.statusBar().showMessage("已载入上轮摘要交接；编辑主题后可开始一轮新讨论")

    def _delete_selected_history(self) -> None:
        item = self.history_list.currentItem()
        if not item:
            return
        if QMessageBox.question(self, "删除记录", "确定删除所选讨论记录？此操作不可恢复。") != QMessageBox.StandardButton.Yes:
            return
        self.storage.delete_discussion(item.data(Qt.ItemDataRole.UserRole))
        self._refresh_history()

    def _copy_result(self) -> None:
        if not self.current_record:
            QMessageBox.information(self, "暂无结果", "请先完成或恢复一轮讨论。")
            return
        QApplication.clipboard().setText(DiscussionExporter.to_markdown(self.current_record))
        self.statusBar().showMessage("完整结果已复制")

    def _export_markdown(self) -> None:
        if not self.current_record:
            QMessageBox.information(self, "暂无结果", "请先完成或恢复一轮讨论。")
            return
        path = self.exporter.export_markdown(self.current_record)
        QMessageBox.information(self, "导出完成", f"已导出：\n{path}\n\n{PRIVACY_NOTICE}")

    def _export_json(self) -> None:
        if not self.current_record:
            QMessageBox.information(self, "暂无结果", "请先完成或恢复一轮讨论。")
            return
        path = self.exporter.export_json(self.current_record)
        QMessageBox.information(self, "导出完成", f"已导出：\n{path}\n\n{PRIVACY_NOTICE}")

    def _clear_all_data(self) -> None:
        if self.engine.current_record and self.engine.current_record.status == RunStatus.RUNNING:
            QMessageBox.warning(self, "任务运行中", "请先终止当前讨论。")
            return
        answer = QMessageBox.warning(
            self,
            "清除全部本地数据",
            "将删除讨论历史、导出、日志和五个平台的独立登录配置。无法恢复。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            self.runner.submit(self.registry.close_all()).result(timeout=8)
            removed = self.data_manager.clear_all()
            self.current_record = None
            self._set_result_actions_enabled(False)
            self.chat_stream.clear_messages()
            self.result_view.clear()
            self.consensus_view.clear()
            self.score_radar.set_scores([])
            self._refresh_history()
            QMessageBox.information(self, "清理完成", f"已清除 {len(removed)} 个本地数据项。")
        except Exception as exc:
            QMessageBox.critical(self, "清理失败", str(exc))

    def closeEvent(self, event: QCloseEvent) -> None:
        try:
            self.runner.submit(self.registry.close_all()).result(timeout=4)
        except Exception:
            pass
        self.runner.stop()
        event.accept()
