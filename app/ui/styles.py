APP_STYLE = """
QMainWindow, QWidget {
  background: #f3f5f9;
  color: #172033;
  font-family: "Microsoft YaHei UI", "Segoe UI";
  font-size: 13px;
}
#appTitle { font-size: 24px; font-weight: 600; color: #101828; }
#appSubtitle { color: #667085; }
#globalStatus {
  background: #e8eefc; color: #284b9b; border-radius: 12px; padding: 6px 12px;
}
#sidePanel, #chatPanel, #insightPanel {
  background: #ffffff; border: 1px solid #dfe4ec; border-radius: 12px;
}
#chatPanel { background: #f8fafc; }
QScrollArea, QScrollArea > QWidget > QWidget { background: transparent; }
QLabel[sectionLabel="true"] { color: #667085; padding-top: 7px; }
QFrame[providerRow="true"] {
  background: #f8fafc; border: 1px solid #e5e9f0; border-radius: 9px;
}
QLabel[providerName="true"] { font-weight: 600; }
QLabel[providerStatus="true"] { color: #667085; font-size: 12px; }
QLabel[providerStatus="true"][state="ok"] { color: #168a62; }
QLabel[providerStatus="true"][state="error"] { color: #b42318; }
QLineEdit, QTextEdit, QTextBrowser, QComboBox, QSpinBox, QListWidget {
  background: #ffffff; border: 1px solid #d0d7e2; border-radius: 7px; padding: 5px;
  selection-background-color: #315efb;
}
QTextBrowser { background: #fbfcfe; }
QPushButton {
  background: #edf1f7; border: 1px solid #d5dce8; border-radius: 6px; padding: 6px 10px;
}
QPushButton:hover { background: #e2e8f2; }
QPushButton:disabled { color: #98a2b3; background: #f2f4f7; }
QPushButton#primaryButton {
  background: #315efb; color: white; border-color: #315efb; font-weight: 600;
}
QPushButton#primaryButton:hover { background: #254bce; }
QPushButton#dangerButton { background: #fff2f0; color: #b42318; border-color: #f2c6c2; }
QPushButton[compactAction="true"] { padding: 3px 7px; font-size: 12px; }
QFrame#composer { background: #ffffff; border: 1px solid #dfe4ec; border-radius: 10px; }
QToolButton { color: #475467; border: 0; padding: 3px; }
QLabel[stageState="pending"] {
  background: #eef1f5; color: #7b8495; border-radius: 8px; padding: 7px 5px;
}
QLabel[stageState="active"] {
  background: #315efb; color: white; border-radius: 8px; padding: 7px 5px; font-weight: 600;
}
QLabel[stageState="done"] {
  background: #dff4eb; color: #147552; border-radius: 8px; padding: 7px 5px;
}
QFrame[bubbleKind="ai"] {
  background: #ffffff; border: 1px solid #dfe5ee; border-radius: 11px;
}
QFrame[bubbleKind="user"] {
  background: #e7edff; border: 1px solid #c9d5ff; border-radius: 11px;
}
QFrame[bubbleKind="final"] {
  background: #e8f7f1; border: 1px solid #b7e2d2; border-radius: 11px;
}
QFrame[bubbleKind="error"] {
  background: #fff1ef; border: 1px solid #f0c3bd; border-radius: 11px;
}
QLabel[bubbleKind="system"] {
  background: #edf1f7; color: #667085; border-radius: 10px; padding: 4px 10px;
}
QLabel[bubbleHeader="true"] { font-weight: 600; }
QLabel[stageBadge="true"] {
  color: #475467; background: #eef1f5; border-radius: 8px; padding: 2px 6px; font-size: 11px;
}
QLabel[bubbleMeta="true"], QLabel[privacyHint="true"] { color: #7b8495; font-size: 11px; }
QLabel[privacyNotice="true"] {
  background: #fff7df; color: #725900; border-radius: 7px; padding: 8px;
}
QLabel[wizardStep="true"] { color: #315efb; font-weight: 600; }
QLabel[wizardTitle="true"] { font-size: 20px; font-weight: 600; }
QTabWidget::pane { border: 0; }
QTabBar::tab { background: #edf1f7; padding: 7px 10px; margin-right: 2px; }
QTabBar::tab:selected { background: #ffffff; color: #315efb; font-weight: 600; }
QSplitter::handle { background: transparent; width: 7px; }
QStatusBar { background: #e9edf4; }
"""
