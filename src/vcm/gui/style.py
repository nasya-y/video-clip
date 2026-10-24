"""Dark theme for the application window."""

ACCENT = "#7C5CFF"
ACCENT_2 = "#00D4FF"

STYLESHEET = """
* { outline: none; }
QWidget { background: #12141A; color: #E8EAF0; font-size: 10pt; }
QMainWindow, QScrollArea, QScrollArea > QWidget > QWidget { background: #12141A; }
QToolTip { background: #232838; color: #E8EAF0; border: 1px solid #3A4156; padding: 6px; border-radius: 6px; }

QFrame#Header { background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #1E1838, stop:1 #10202E);
                border: none; border-bottom: 1px solid #262B38; }
QLabel#AppTitle { background: transparent; font-size: 16pt; font-weight: 700; color: #FFFFFF; }
QLabel#AppSubtitle { background: transparent; color: #A9B0C3; font-size: 10pt; }
QLabel#OfflineBadge { background: #173A2C; color: #6EF0B0; border: 1px solid #23573F; border-radius: 10px;
                      padding: 3px 10px; font-size: 9pt; font-weight: 600; }
QLabel#HeaderIcon { background: transparent; }

QFrame#Footer { background: #161922; border-top: 1px solid #262B38; }
QFrame#Card { background: #1A1E28; border: 1px solid #262B38; border-radius: 14px; }
QFrame#Card QLabel, QFrame#Card QCheckBox { background: transparent; }
QLabel#CardTitle { font-size: 12pt; font-weight: 650; color: #FFFFFF; }
QFrame#Card QLabel#StepBadge, QLabel#StepBadge { background: #7C5CFF; color: #FFFFFF; border-radius: 12px; font-weight: 700;
                   min-width: 24px; max-width: 24px; min-height: 24px; max-height: 24px; }
QLabel#Hint { color: #8C93A8; font-size: 9pt; }
QLabel#SectionLabel { color: #B9A8FF; font-weight: 650; padding-top: 6px; }
QLabel#Big { font-size: 13pt; font-weight: 650; color: #FFFFFF; }
QLabel#Stat { color: #CFD4E2; }
QLabel#Derived { color: #6EF0B0; font-weight: 650; }

QPushButton { background: #262C3B; border: 1px solid #343B50; border-radius: 9px; padding: 6px 14px; color: #E8EAF0; }
QPushButton:checked { background: #5B4BD6; border: 1px solid #8C7BFF; color: #FFFFFF; font-weight: 600; }
QPushButton:hover { background: #2F3649; border-color: #4A5370; }
QPushButton:pressed { background: #232837; }
QPushButton:disabled { color: #5D6478; background: #1C202B; border-color: #262B38; }
QPushButton#Primary { background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #7C5CFF, stop:1 #5B8CFF);
                      border: none; color: #FFFFFF; font-weight: 700; font-size: 11pt; padding: 9px 28px; }
QPushButton#Primary:hover { background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #8F73FF, stop:1 #6E9BFF); }
QPushButton#Primary:disabled { background: #2A2F40; color: #6B7186; }
QPushButton#Danger { background: #34202B; border: 1px solid #5A2A3D; color: #FFB3C4; font-weight: 600; padding: 9px 20px; }
QPushButton#Danger:hover { background: #432535; }
QPushButton#Danger:disabled { background: #1C202B; color: #5D6478; border-color: #262B38; }
QPushButton#Link { background: transparent; border: none; color: #9F8CFF; padding: 2px 4px; }
QPushButton#Link:hover { color: #C3B6FF; text-decoration: underline; }
QPushButton#Swatch { border-radius: 8px; min-width: 34px; max-width: 34px; min-height: 26px; padding: 0; }

QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QPlainTextEdit {
    background: #11141B; border: 1px solid #2E3446; border-radius: 8px; padding: 4px 8px; min-height: 22px;
    selection-background-color: #7C5CFF; }
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus { border-color: #7C5CFF; }
QComboBox::drop-down { border: none; width: 22px; }
QComboBox QAbstractItemView { background: #1A1E28; border: 1px solid #343B50; selection-background-color: #343B60; }
QSpinBox::up-button, QDoubleSpinBox::up-button, QSpinBox::down-button, QDoubleSpinBox::down-button { width: 18px; border: none; }

QListWidget { background: #11141B; border: 1px solid #262B38; border-radius: 10px; padding: 4px; }
QListWidget::item { padding: 6px; border-radius: 7px; }
QListWidget::item:selected { background: #2C2852; color: #FFFFFF; }
QListWidget::item:hover { background: #1F2433; }
QListWidget#Errors { color: #FFB3C4; }

QProgressBar { background: #11141B; border: 1px solid #262B38; border-radius: 9px; min-height: 20px;
               text-align: center; color: #FFFFFF; font-weight: 600; }
QProgressBar::chunk { border-radius: 8px;
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #7C5CFF, stop:1 #00D4FF); }

QCheckBox::indicator { width: 16px; height: 16px; }
QScrollBar:vertical { background: transparent; width: 10px; margin: 2px; }
QScrollBar::handle:vertical { background: #2E3446; border-radius: 4px; min-height: 30px; }
QScrollBar::add-line, QScrollBar::sub-line { height: 0; }
QMenuBar { background: #12141A; }
QMenuBar::item:selected { background: #262C3B; }
QMenu { background: #1A1E28; border: 1px solid #343B50; }
QMenu::item:selected { background: #2C2852; }
QMessageBox { background: #1A1E28; }
"""
