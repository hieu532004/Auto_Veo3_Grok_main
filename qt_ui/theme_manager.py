from __future__ import annotations

import os
from pathlib import Path
from typing import Callable

_current_theme = "dark"
_listeners: list[Callable[[str], None]] = []

_SETTINGS_DIR = Path(__file__).resolve().parent.parent / "data_general"
_THEME_FILE = _SETTINGS_DIR / "app_theme.txt"


PALETTE = {
    "light": {
        "bg_primary": "#f0f4f8",
        "bg_secondary": "#ffffff",
        "bg_tertiary": "#e8edf5",
        "bg_card": "#ffffff",
        "bg_input": "#f8fafc",
        "bg_hover": "#e2e8f0",
        "bg_sidebar": "#1e293b",
        "bg_tab_active": "#4f46e5",
        "bg_tab_inactive": "#e2e8f0",
        "bg_accent": "#4f46e5",
        "bg_accent_hover": "#4338ca",
        "bg_success": "#059669",
        "bg_success_hover": "#047857",
        "bg_danger": "#dc2626",
        "bg_danger_hover": "#b91c1c",
        "bg_warning": "#f59e0b",
        "bg_warning_hover": "#d97706",
        "bg_config_bar": "#1e293b",
        "bg_config_input": "#334155",
        "text_primary": "#0f172a",
        "text_secondary": "#475569",
        "text_muted": "#94a3b8",
        "text_on_accent": "#ffffff",
        "text_on_config": "#e2e8f0",
        "border_primary": "#e2e8f0",
        "border_secondary": "#cbd5e1",
        "border_input": "#cbd5e1",
        "border_focus": "#4f46e5",
        "scrollbar_bg": "#f1f5f9",
        "scrollbar_handle": "#94a3b8",
        "scrollbar_hover": "#64748b",
        "table_header_bg": "#f1f5f9",
        "table_row_alt": "#f8fafc",
        "table_selection": "#dbeafe",
        "gutter_bg": "#f1f5f9",
        "gutter_border": "#e2e8f0",
        "gutter_text": "#64748b",
        "separator": "#e2e8f0",
        "shadow": "rgba(0, 0, 0, 0.08)",
    },
    "dark": {
        "bg_primary": "#0f172a",
        "bg_secondary": "#1e293b",
        "bg_tertiary": "#1e293b",
        "bg_card": "#1e293b",
        "bg_input": "#0f172a",
        "bg_hover": "#334155",
        "bg_sidebar": "#020617",
        "bg_tab_active": "#6366f1",
        "bg_tab_inactive": "#1e293b",
        "bg_accent": "#6366f1",
        "bg_accent_hover": "#4f46e5",
        "bg_success": "#10b981",
        "bg_success_hover": "#059669",
        "bg_danger": "#ef4444",
        "bg_danger_hover": "#dc2626",
        "bg_warning": "#f59e0b",
        "bg_warning_hover": "#d97706",
        "bg_config_bar": "#020617",
        "bg_config_input": "#1e293b",
        "text_primary": "#f1f5f9",
        "text_secondary": "#94a3b8",
        "text_muted": "#64748b",
        "text_on_accent": "#ffffff",
        "text_on_config": "#e2e8f0",
        "border_primary": "#334155",
        "border_secondary": "#475569",
        "border_input": "#334155",
        "border_focus": "#6366f1",
        "scrollbar_bg": "#1e293b",
        "scrollbar_handle": "#475569",
        "scrollbar_hover": "#64748b",
        "table_header_bg": "#1e293b",
        "table_row_alt": "#0f172a",
        "table_selection": "#1e3a5f",
        "gutter_bg": "#1e293b",
        "gutter_border": "#334155",
        "gutter_text": "#64748b",
        "separator": "#334155",
        "shadow": "rgba(0, 0, 0, 0.3)",
    },
}


def _build_qss(theme: str) -> str:
    p = PALETTE.get(theme, PALETTE["dark"])
    return f"""
    * {{
        font-family: 'Segoe UI', 'Inter', 'Roboto', sans-serif;
    }}

    QMainWindow {{
        background: {p['bg_primary']};
    }}
    QWidget#AppRoot {{
        background: {p['bg_primary']};
    }}

    QWidget {{
        color: {p['text_primary']};
        background: transparent;
    }}

    QLabel {{
        font-size: 13px;
        color: {p['text_primary']};
        background: transparent;
        border: none;
    }}

    /* ===== TABS ===== */
    QTabWidget::pane {{
        border: 1px solid {p['border_primary']};
        background: {p['bg_secondary']};
        border-radius: 16px;
        top: 2px;
        padding: 8px;
    }}

    QTabWidget::tab-bar {{
        alignment: left;
    }}

    QTabBar::tab {{
        background: transparent;
        color: {p['text_secondary']};
        padding: 8px 20px;
        border: 1px solid transparent;
        border-radius: 14px;
        margin-right: 6px;
        font-weight: 700;
        font-size: 13px;
        min-height: 28px;
    }}
    QTabBar::tab:selected {{
        background: {p['bg_tab_active']};
        color: {p['text_on_accent']};
        border: 1px solid {p['bg_accent']};
    }}
    QTabBar::tab:hover:!selected {{
        background: {p['bg_hover']};
        color: {p['text_primary']};
        border-radius: 14px;
    }}

    /* ===== GROUP BOX ===== */
    QGroupBox {{
        border: 1px solid {p['border_primary']};
        border-radius: 12px;
        margin-top: 14px;
        padding-top: 16px;
        background: {p['bg_secondary']};
        font-weight: 700;
        font-size: 13px;
        color: {p['text_primary']};
    }}
    QGroupBox::title {{
        subcontrol-origin: margin;
        left: 14px;
        padding: 2px 10px;
        color: {p['text_on_accent']};
        background: {p['bg_accent']};
        border-radius: 8px;
    }}

    /* ===== BUTTONS ===== */
    QPushButton {{
        padding: 8px 18px;
        border: 1px solid {p['border_primary']};
        border-radius: 14px;
        background: {p['bg_card']};
        color: {p['text_primary']};
        font-weight: 700;
        font-size: 13px;
        min-height: 38px;
    }}
    QPushButton:hover {{
        background: {p['bg_hover']};
        border-color: {p['border_secondary']};
    }}
    QPushButton:pressed {{
        background: {p['bg_tertiary']};
    }}
    QPushButton:disabled {{
        color: {p['text_muted']};
        background: {p['bg_tertiary']};
        border-color: {p['border_primary']};
    }}
    QToolButton:disabled {{
        color: {p['text_muted']};
        background: {p['bg_tertiary']};
        border-color: {p['border_primary']};
    }}

    QPushButton[topRow="true"] {{
        padding: 4px 10px;
        font-size: 12px;
        min-height: 28px;
    }}

    QPushButton#Accent {{
        background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 {p['bg_accent']}, stop:1 {p['bg_accent_hover']});
        border-color: {p['bg_accent']};
        color: {p['text_on_accent']};
        font-weight: 800;
    }}
    QPushButton#Accent:hover {{
        background: {p['bg_accent_hover']};
    }}

    QPushButton#Success {{
        background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 {p['bg_success']}, stop:1 {p['bg_success_hover']});
        border-color: {p['bg_success']};
        color: {p['text_on_accent']};
        font-weight: 800;
    }}
    QPushButton#Success:hover {{
        background: {p['bg_success_hover']};
    }}

    QPushButton#Danger {{
        background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 {p['bg_danger']}, stop:1 {p['bg_danger_hover']});
        border-color: {p['bg_danger']};
        color: {p['text_on_accent']};
        font-weight: 800;
    }}
    QPushButton#Danger:hover {{
        background: {p['bg_danger_hover']};
    }}

    QPushButton#Warning {{
        background: {p['bg_warning']};
        border-color: {p['bg_warning']};
        color: #1a1a2e;
        font-weight: 800;
    }}
    QPushButton#Warning:hover {{
        background: {p['bg_warning_hover']};
    }}

    QPushButton#Orange {{
        background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #f97316, stop:1 #ea580c);
        border-color: #ea580c;
        color: {p['text_on_accent']};
        font-weight: 800;
    }}
    QPushButton#Orange:hover {{
        background: #ea580c;
    }}

    QPushButton#TopAction {{
        background: {p['bg_accent']};
        border-color: {p['bg_accent']};
        color: {p['text_on_accent']};
        font-weight: 800;
        border-radius: 12px;
    }}
    QPushButton#TopAction:hover {{
        background: {p['bg_accent_hover']};
    }}

    QPushButton#DangerSoft {{
        background: {"#fef2f2" if theme == "light" else "#451a1a"};
        border-color: {"#fecaca" if theme == "light" else "#7f1d1d"};
        color: {"#991b1b" if theme == "light" else "#fca5a5"};
        font-weight: 800;
    }}
    QPushButton#DangerSoft:hover {{
        background: {"#fee2e2" if theme == "light" else "#5c1d1d"};
    }}

    QPushButton#Zalo {{
        background: {p['bg_accent']};
        border-color: {p['bg_accent']};
        color: {p['text_on_accent']};
        font-weight: 800;
    }}
    QPushButton#Zalo:hover {{
        background: {p['bg_accent_hover']};
    }}

    QPushButton#Accent:disabled,
    QPushButton#Success:disabled,
    QPushButton#Warning:disabled,
    QPushButton#Orange:disabled,
    QPushButton#Danger:disabled,
    QPushButton#TopAction:disabled,
    QPushButton#DangerSoft:disabled,
    QPushButton#Zalo:disabled {{
        color: {p['text_muted']};
        background: {p['bg_tertiary']};
        border-color: {p['border_primary']};
    }}

    /* ===== TOOLBAR ===== */
    QWidget#AppToolbar {{
        background: {p['bg_secondary']};
        border-bottom: 2px solid {p['border_primary']};
        border-radius: 0px;
    }}
    QWidget#AppToolbar QLabel {{
        color: {p['text_primary']};
        font-size: 17px;
        font-weight: 900;
        letter-spacing: 1px;
    }}
    QPushButton#ToolbarBtn {{
        background: {p['bg_tertiary']};
        border: 1px solid {p['border_primary']};
        border-radius: 12px;
        padding: 4px 14px;
        font-size: 13px;
        font-weight: 700;
        min-height: 28px;
        color: {p['text_primary']};
    }}
    QPushButton#ToolbarBtn:hover {{
        background: {p['bg_hover']};
        border-color: {p['border_focus']};
        color: {p['bg_accent']};
    }}

    /* ===== BOTTOM CONFIG BAR ===== */
    QWidget#BottomCfgWrap {{
        border: 1px solid {p['border_primary']};
        border-radius: 12px;
        background: {p['bg_config_bar']};
        margin: 4px;
    }}
    QWidget#BottomCfgWrap QLabel {{
        border: none;
        background: transparent;
        font-size: 13px;
        color: {p['text_on_config']};
        font-weight: 700;
    }}
    QComboBox#BottomCfgCombo, QLineEdit#BottomCfgLine {{
        border: 1px solid {p['border_secondary']};
        border-radius: 8px;
        padding: 4px 12px;
        background: {p['bg_config_input']};
        color: {p['text_on_config']};
        font-size: 13px;
        font-weight: 600;
        min-height: 32px;
    }}
    QComboBox#BottomCfgCombo:hover, QLineEdit#BottomCfgLine:hover {{
        border-color: {p['bg_accent']};
    }}
    QComboBox#BottomCfgCombo::drop-down {{
        border-left: 1px solid {p['border_secondary']};
        width: 24px;
        background: transparent;
    }}
    QComboBox#BottomCfgCombo::down-arrow {{
        image: none;
        width: 0; height: 0;
        border-left: 4px solid transparent;
        border-right: 4px solid transparent;
        border-top: 5px solid {p['text_on_config']};
        margin-right: 6px;
    }}
    QComboBox#BottomCfgCombo QAbstractItemView {{
        background: {p['bg_config_input']};
        color: {p['text_on_config']};
        border: 1px solid {p['border_secondary']};
        border-radius: 8px;
        font-size: 13px;
        outline: none;
    }}
    QComboBox#BottomCfgCombo QAbstractItemView::item {{
        min-height: 32px;
        padding: 4px 10px;
    }}

    /* ===== INPUTS ===== */
    QLineEdit, QComboBox, QPlainTextEdit, QTextEdit, QSpinBox {{
        border: 1px solid {p['border_input']};
        border-radius: 10px;
        padding: 8px 14px;
        background: {p['bg_input']};
        color: {p['text_primary']};
        selection-background-color: {p['bg_accent']};
        selection-color: {p['text_on_accent']};
        font-size: 14px;
        font-weight: 500;
    }}
    QLineEdit:focus, QComboBox:focus, QPlainTextEdit:focus, QTextEdit:focus, QSpinBox:focus {{
        border-color: {p['border_focus']};
        background: {p['bg_card']};
    }}
    QComboBox::drop-down {{
        border-left: none;
        width: 28px;
        background: transparent;
    }}
    QComboBox::down-arrow {{
        image: none;
        width: 0; height: 0;
        border-left: 5px solid transparent;
        border-right: 5px solid transparent;
        border-top: 6px solid {p['text_secondary']};
        margin-right: 8px;
    }}
    QComboBox QAbstractItemView {{
        background: {p['bg_card']};
        color: {p['text_primary']};
        border: 1px solid {p['border_focus']};
        border-radius: 10px;
        outline: none;
        selection-background-color: {p['table_selection']};
        selection-color: {p['text_primary']};
    }}
    QComboBox QAbstractItemView::item {{
        min-height: 34px;
        padding: 6px 12px;
    }}
    QComboBox QAbstractItemView::item:selected {{
        border-radius: 6px;
    }}

    /* ===== TABLE ===== */
    QTableWidget {{
        border: 1px solid {p['border_primary']};
        border-radius: 14px;
        gridline-color: transparent;
        background: {p['bg_card']};
        color: {p['text_primary']};
        selection-background-color: {p['table_selection']};
        alternate-background-color: {p['table_row_alt']};
        padding: 4px;
    }}
    QTableWidget::item {{
        border-bottom: 1px solid {p['separator']};
        padding: 4px;
    }}
    QTableWidget::item:selected {{
        background-color: {p['table_selection']};
        color: {p['text_primary']};
        border-radius: 6px;
    }}
    QHeaderView::section {{
        background: {p['table_header_bg']};
        color: {p['text_secondary']};
        padding: 12px 8px;
        border: none;
        font-weight: 800;
        font-size: 13px;
        text-transform: uppercase;
    }}

    /* ===== SCROLLBAR ===== */
    QScrollBar:vertical {{
        background: {p['scrollbar_bg']};
        width: 8px;
        margin: 0px;
        border-radius: 4px;
    }}
    QScrollBar::handle:vertical {{
        background: {p['scrollbar_handle']};
        border-radius: 4px;
        min-height: 40px;
    }}
    QScrollBar::handle:vertical:hover {{
        background: {p['scrollbar_hover']};
    }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0px; }}
    QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}

    QScrollBar:horizontal {{
        background: {p['scrollbar_bg']};
        height: 8px;
        margin: 0px;
        border-radius: 4px;
    }}
    QScrollBar::handle:horizontal {{
        background: {p['scrollbar_handle']};
        border-radius: 4px;
        min-width: 40px;
    }}
    QScrollBar::handle:horizontal:hover {{
        background: {p['scrollbar_hover']};
    }}
    QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0px; }}
    QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {{ background: transparent; }}

    /* ===== SPLITTER ===== */
    QSplitter::handle {{
        background: transparent;
        width: 8px;
    }}
    QSplitter::handle:hover {{
        background: {p['border_focus']};
    }}

    /* ===== FORM LAYOUT ===== */
    QFormLayout {{
        background: transparent;
    }}

    /* ===== PROGRESS BAR ===== */
    QProgressBar {{
        border: none;
        border-radius: 6px;
        background: {p['bg_tertiary']};
        text-align: center;
        color: {p['text_primary']};
        font-weight: 700;
        font-size: 12px;
        min-height: 18px;
        max-height: 18px;
    }}
    QProgressBar::chunk {{
        background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 {p['bg_accent']}, stop:1 #818cf8);
        border-radius: 6px;
    }}

    /* ===== TOOLTIP ===== */
    QToolTip {{
        background: {p['bg_card']};
        color: {p['text_primary']};
        border: 1px solid {p['border_primary']};
        border-radius: 8px;
        padding: 8px 12px;
        font-size: 13px;
        font-weight: 600;
    }}
    
    /* ===== HELP BOX ===== */
    QLabel#HelpBoxItem {{
        border: 1px solid {p['border_primary']};
        border-radius: 8px;
        background: {p['bg_secondary']};
        color: {p['text_primary']};
        padding: 6px 8px;
    }}
    """


def current_theme() -> str:
    return _current_theme


def get_palette() -> dict[str, str]:
    return PALETTE.get(_current_theme, PALETTE["dark"])


def get_color(key: str) -> str:
    return get_palette().get(key, "#ffffff")


def get_qss() -> str:
    return _build_qss(_current_theme)


def set_theme(theme: str) -> None:
    global _current_theme
    if theme not in ("light", "dark"):
        theme = "dark"
    _current_theme = theme
    _save_theme(theme)
    for fn in _listeners:
        try:
            fn(theme)
        except Exception:
            pass


def on_theme_change(fn: Callable[[str], None]) -> None:
    if fn not in _listeners:
        _listeners.append(fn)


def remove_theme_listener(fn: Callable[[str], None]) -> None:
    try:
        _listeners.remove(fn)
    except ValueError:
        pass


def _save_theme(theme: str) -> None:
    try:
        _SETTINGS_DIR.mkdir(parents=True, exist_ok=True)
        _THEME_FILE.write_text(theme, encoding="utf-8")
    except Exception:
        pass


def _load_theme() -> str:
    try:
        if _THEME_FILE.exists():
            val = _THEME_FILE.read_text(encoding="utf-8").strip()
            if val in ("light", "dark"):
                return val
    except Exception:
        pass
    return "dark"


_current_theme = _load_theme()
