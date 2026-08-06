from __future__ import annotations

import math
from collections import defaultdict
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QPolygonF,
)
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from app.core.enums import DiscussionStage, RunStatus
from app.models import JudgeScore, ProviderConfig
from app.ui.view_models import ChatMessageView, MessageKind


STAGE_LABELS = [
    (DiscussionStage.INDEPENDENT, "独立回答"),
    (DiscussionStage.REVIEW, "匿名互评"),
    (DiscussionStage.REVISION, "方案修订"),
    (DiscussionStage.JUDGE, "裁判评分"),
    (DiscussionStage.SYNTHESIS, "主持综合"),
]


class StageProgressWidget(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        self.labels: dict[DiscussionStage, QLabel] = {}
        for index, (stage, text) in enumerate(STAGE_LABELS, 1):
            label = QLabel(f"{index}  {text}")
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            label.setProperty("stageState", "pending")
            self.labels[stage] = label
            layout.addWidget(label, 1)
        self.set_stage(DiscussionStage.PREPARING)

    def set_stage(self, stage: DiscussionStage) -> None:
        active_index = next(
            (index for index, (item, _) in enumerate(STAGE_LABELS) if item == stage),
            -1,
        )
        if stage == DiscussionStage.COMPLETED:
            active_index = len(STAGE_LABELS)
        for index, (item, _) in enumerate(STAGE_LABELS):
            state = "done" if index < active_index else "active" if index == active_index else "pending"
            self.labels[item].setProperty("stageState", state)
            self.labels[item].style().unpolish(self.labels[item])
            self.labels[item].style().polish(self.labels[item])


class AvatarLabel(QLabel):
    def __init__(
        self,
        text: str,
        color: str,
        image_path: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setFixedSize(36, 36)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image_loaded = False
        resolved = Path(image_path)
        if image_path and not resolved.is_absolute():
            resolved = Path(__file__).resolve().parents[2] / resolved
        source = QPixmap(str(resolved)) if image_path else QPixmap()
        if not source.isNull():
            size = self.width()
            scaled = source.scaled(
                size,
                size,
                Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                Qt.TransformationMode.SmoothTransformation,
            )
            canvas = QPixmap(size, size)
            canvas.fill(Qt.GlobalColor.transparent)
            painter = QPainter(canvas)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            clip = QPainterPath()
            clip.addEllipse(QRectF(0, 0, size, size))
            painter.setClipPath(clip)
            x = (size - scaled.width()) // 2
            y = (size - scaled.height()) // 2
            painter.drawPixmap(x, y, scaled)
            painter.end()
            self.setPixmap(canvas)
            self.setToolTip(text)
            self.setStyleSheet(f"border:1px solid {color};border-radius:18px;")
            self.image_loaded = True
        else:
            self.setText(text)
            self.setStyleSheet(
                f"background:{color};color:white;border-radius:18px;font-weight:600;"
            )


class ChatBubbleWidget(QWidget):
    action_requested = Signal(str, str)

    def __init__(
        self,
        message: ChatMessageView,
        provider: ProviderConfig | None = None,
        call_id: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.message_id = message.id
        self.call_id = call_id or message.id
        self.provider = provider
        self._build(message)

    def _build(self, message: ChatMessageView) -> None:
        outer = QHBoxLayout(self)
        outer.setContentsMargins(8, 4, 8, 4)
        outer.setSpacing(8)
        is_user = message.kind == MessageKind.USER
        is_system = message.kind == MessageKind.SYSTEM
        if is_system:
            system = QLabel(message.content)
            system.setAlignment(Qt.AlignmentFlag.AlignCenter)
            system.setProperty("bubbleKind", "system")
            outer.addStretch()
            outer.addWidget(system)
            outer.addStretch()
            self.content_label = system
            self.meta_label = QLabel()
            self.action_row = QWidget()
            return

        avatar_text = "你" if is_user else (self.provider.avatar_text if self.provider else "AI")
        color = "#315efb" if is_user else (self.provider.accent_color if self.provider else "#64748b")
        avatar_path = "" if is_user else (self.provider.avatar_path if self.provider else "")
        avatar = AvatarLabel(avatar_text, color, avatar_path)
        bubble = QFrame()
        bubble.setProperty(
            "bubbleKind",
            "user" if is_user else "final" if message.kind == MessageKind.FINAL else "error" if message.kind == MessageKind.ERROR else "ai",
        )
        bubble.setMaximumWidth(660)
        bubble.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        content_layout = QVBoxLayout(bubble)
        content_layout.setContentsMargins(12, 9, 12, 9)
        content_layout.setSpacing(5)
        header = QHBoxLayout()
        name = QLabel(message.provider_name)
        name.setProperty("bubbleHeader", True)
        stage_text = dict(STAGE_LABELS).get(message.stage, message.stage.value)
        stage = QLabel(stage_text)
        stage.setProperty("stageBadge", True)
        header.addWidget(name)
        header.addWidget(stage)
        header.addStretch()
        content_layout.addLayout(header)
        self.content_label = QLabel(message.content)
        self.content_label.setWordWrap(True)
        self.content_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.content_label.setTextFormat(Qt.TextFormat.PlainText)
        content_layout.addWidget(self.content_label)
        self.meta_label = QLabel()
        self.meta_label.setProperty("bubbleMeta", True)
        content_layout.addWidget(self.meta_label)
        self.action_row = QWidget()
        self.action_layout = QHBoxLayout(self.action_row)
        self.action_layout.setContentsMargins(0, 2, 0, 0)
        self.action_layout.setSpacing(5)
        self.action_row.hide()
        content_layout.addWidget(self.action_row)
        self.update_message(message)

        if is_user:
            outer.addStretch(1)
            outer.addWidget(bubble)
            outer.addWidget(avatar)
        else:
            outer.addWidget(avatar)
            outer.addWidget(bubble)
            outer.addStretch(1)

    def update_message(self, message: ChatMessageView) -> None:
        self.content_label.setText(message.content)
        parts = []
        if message.status:
            parts.append(message.status.value)
        if message.elapsed_seconds:
            parts.append(f"{message.elapsed_seconds:.1f}s")
        if message.retry_count:
            parts.append(f"重试 {message.retry_count}")
        self.meta_label.setText(" · ".join(parts))

    def show_recovery_actions(self) -> None:
        while self.action_layout.count():
            item = self.action_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        for text, action in (("人工接管", "manual"), ("重试一次", "retry"), ("跳过", "skip")):
            button = QPushButton(text)
            button.setProperty("compactAction", True)
            button.clicked.connect(
                lambda _checked=False, value=action: self.action_requested.emit(self.call_id, value)
            )
            self.action_layout.addWidget(button)
        self.action_layout.addStretch()
        self.action_row.show()

    def hide_recovery_actions(self) -> None:
        self.action_row.hide()


class ChatStream(QScrollArea):
    action_requested = Signal(str, str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.container = QWidget()
        self.layout = QVBoxLayout(self.container)
        self.layout.setContentsMargins(4, 8, 4, 8)
        self.layout.setSpacing(2)
        self.layout.addStretch(1)
        self.setWidget(self.container)
        self.bubbles: dict[str, ChatBubbleWidget] = {}

    def clear_messages(self) -> None:
        while self.layout.count() > 1:
            item = self.layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.bubbles.clear()

    def upsert(
        self,
        message: ChatMessageView,
        provider: ProviderConfig | None = None,
        call_id: str = "",
        recovery: bool = False,
    ) -> ChatBubbleWidget:
        key = call_id or message.id
        bubble = self.bubbles.get(key)
        if bubble:
            bubble.update_message(message)
        else:
            bubble = ChatBubbleWidget(message, provider, key)
            bubble.action_requested.connect(self.action_requested)
            self.layout.insertWidget(self.layout.count() - 1, bubble)
            self.bubbles[key] = bubble
        if recovery:
            bubble.show_recovery_actions()
        elif message.status == RunStatus.SUCCEEDED:
            bubble.hide_recovery_actions()
        self.verticalScrollBar().setValue(self.verticalScrollBar().maximum())
        return bubble


class ScoreRadarWidget(QWidget):
    DIMENSIONS = [
        ("correctness", "正确"),
        ("logical_completeness", "逻辑"),
        ("executability", "执行"),
        ("objectivity", "客观"),
        ("risk_control", "风险"),
        ("constraint_alignment", "约束"),
        ("evidence_grounding", "证据"),
        ("uncertainty_expression", "不确定性"),
    ]
    COLORS = ["#168a62", "#6d5bd0", "#2563b8", "#d05a32"]

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(250)
        self._series: dict[str, dict[str, float]] = {}

    def set_scores(self, scores: list[JudgeScore]) -> None:
        grouped: dict[str, dict[str, list[float]]] = defaultdict(
            lambda: defaultdict(list)
        )
        for score in scores:
            for key, value in score.dimensions.model_dump().items():
                grouped[score.candidate_alias][key].append(float(value))
        self._series = {
            alias: {
                key: sum(values) / len(values)
                for key, values in dimensions.items()
            }
            for alias, dimensions in grouped.items()
        }
        self.update()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        center = QPointF(self.width() / 2, self.height() / 2 + 4)
        radius = max(34.0, min(self.width(), self.height()) * 0.31)
        count = len(self.DIMENSIONS)
        grid_color = QColor(130, 142, 160, 90)
        text_color = self.palette().text().color()
        for level in range(1, 6):
            points = QPolygonF()
            for index in range(count):
                angle = -math.pi / 2 + 2 * math.pi * index / count
                scaled = radius * level / 5
                points.append(
                    QPointF(center.x() + math.cos(angle) * scaled, center.y() + math.sin(angle) * scaled)
                )
            points.append(points[0])
            painter.setPen(QPen(grid_color, 1))
            painter.drawPolyline(points)
        painter.setPen(QPen(grid_color, 1))
        for index, (_, label) in enumerate(self.DIMENSIONS):
            angle = -math.pi / 2 + 2 * math.pi * index / count
            end = QPointF(center.x() + math.cos(angle) * radius, center.y() + math.sin(angle) * radius)
            painter.drawLine(center, end)
            label_point = QPointF(
                center.x() + math.cos(angle) * (radius + 22),
                center.y() + math.sin(angle) * (radius + 22),
            )
            painter.setPen(text_color)
            painter.drawText(
                int(label_point.x() - 28),
                int(label_point.y() - 9),
                56,
                18,
                Qt.AlignmentFlag.AlignCenter,
                label,
            )
        for series_index, (alias, values) in enumerate(self._series.items()):
            color = QColor(self.COLORS[series_index % len(self.COLORS)])
            points = QPolygonF()
            for index, (key, _) in enumerate(self.DIMENSIONS):
                angle = -math.pi / 2 + 2 * math.pi * index / count
                scaled = radius * max(0, min(10, values.get(key, 0))) / 10
                points.append(
                    QPointF(center.x() + math.cos(angle) * scaled, center.y() + math.sin(angle) * scaled)
                )
            points.append(points[0])
            fill = QColor(color)
            fill.setAlpha(28)
            painter.setBrush(fill)
            painter.setPen(QPen(color, 2))
            painter.drawPolygon(points)
            painter.setPen(color)
            painter.drawText(8, 18 + series_index * 18, alias)
        if not self._series:
            painter.setPen(text_color)
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "完成评分后显示八维雷达图")
