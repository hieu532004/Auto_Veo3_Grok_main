from __future__ import annotations

from typing import Callable

from PyQt6.QtWidgets import QDialogButtonBox, QMessageBox


_INSTALLED = False
_ORIG_EXEC: Callable | None = None
_ORIG_INFO: Callable | None = None
_ORIG_WARN: Callable | None = None
_ORIG_CRIT: Callable | None = None
_ORIG_QUESTION: Callable | None = None


from qt_ui.theme_manager import get_color, current_theme

def _style_box(box: QMessageBox) -> None:
    try:
        box.setOption(QMessageBox.Option.DontUseNativeDialog, True)
    except Exception:
        pass

    try:
        bg_card = get_color("bg_card")
        text_primary = get_color("text_primary")
        border_primary = get_color("border_primary")
        bg_accent = get_color("bg_accent")
        text_on_accent = get_color("text_on_accent")
        button_bg = get_color("bg_tertiary")
        button_hover = get_color("bg_hover")
        
        if current_theme() == 'light':
            bg_gradient = "qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #f8fbff, stop:1 #eef6ff)"
        else:
            bg_gradient = bg_card

        qss = f"""
QMessageBox {{
    background: {bg_gradient};
    color: {text_primary};
}}
QMessageBox QLabel {{
    font-size: 13px;
    color: {text_primary};
}}
QMessageBox QLabel#qt_msgbox_label {{
    font-size: 14px;
    font-weight: 700;
    padding: 4px 2px;
}}
QMessageBox QLabel#qt_msgbox_informativelabel {{
    font-size: 12px;
}}
QMessageBox QPushButton {{
    min-width: 88px;
    min-height: 30px;
    padding: 6px 14px;
    border: 1px solid {border_primary};
    border-radius: 8px;
    background: {button_bg};
    color: {text_primary};
    font-weight: 700;
}}
QMessageBox QPushButton:hover {{
    background: {button_hover};
    border: 1px solid {bg_accent};
}}
QMessageBox QPushButton:pressed {{
    background: {bg_accent};
    color: {text_on_accent};
}}
"""
        box.setStyleSheet(qss)
    except Exception:
        pass

    try:
        btn_box = box.findChild(QDialogButtonBox)
        if btn_box is not None:
            btn_box.setCenterButtons(True)
    except Exception:
        pass


def _build_and_exec(
    *,
    parent,
    icon: QMessageBox.Icon,
    title: str,
    text: str,
    buttons: QMessageBox.StandardButton,
    default: QMessageBox.StandardButton,
) -> QMessageBox.StandardButton:
    box = QMessageBox(parent)
    box.setIcon(icon)
    box.setWindowTitle(str(title or "Thông báo"))
    box.setText(str(text or ""))
    box.setStandardButtons(buttons)
    if default != QMessageBox.StandardButton.NoButton:
        box.setDefaultButton(default)
    _style_box(box)
    return QMessageBox.StandardButton(box.exec())


def install_messagebox_theme() -> None:
    global _INSTALLED, _ORIG_EXEC, _ORIG_INFO, _ORIG_WARN, _ORIG_CRIT, _ORIG_QUESTION
    if _INSTALLED:
        return

    _INSTALLED = True
    _ORIG_EXEC = QMessageBox.exec
    _ORIG_INFO = QMessageBox.information
    _ORIG_WARN = QMessageBox.warning
    _ORIG_CRIT = QMessageBox.critical
    _ORIG_QUESTION = QMessageBox.question

    def _patched_exec(self: QMessageBox) -> int:
        _style_box(self)
        return int(_ORIG_EXEC(self))

    def _patched_information(
        parent,
        title,
        text,
        buttons=QMessageBox.StandardButton.Ok,
        defaultButton=QMessageBox.StandardButton.NoButton,
    ):
        return _build_and_exec(
            parent=parent,
            icon=QMessageBox.Icon.Information,
            title=str(title or "Thông báo"),
            text=str(text or ""),
            buttons=buttons,
            default=defaultButton,
        )

    def _patched_warning(
        parent,
        title,
        text,
        buttons=QMessageBox.StandardButton.Ok,
        defaultButton=QMessageBox.StandardButton.NoButton,
    ):
        return _build_and_exec(
            parent=parent,
            icon=QMessageBox.Icon.Warning,
            title=str(title or "Cảnh báo"),
            text=str(text or ""),
            buttons=buttons,
            default=defaultButton,
        )

    def _patched_critical(
        parent,
        title,
        text,
        buttons=QMessageBox.StandardButton.Ok,
        defaultButton=QMessageBox.StandardButton.NoButton,
    ):
        return _build_and_exec(
            parent=parent,
            icon=QMessageBox.Icon.Critical,
            title=str(title or "Lỗi"),
            text=str(text or ""),
            buttons=buttons,
            default=defaultButton,
        )

    def _patched_question(
        parent,
        title,
        text,
        buttons=QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        defaultButton=QMessageBox.StandardButton.NoButton,
    ):
        resolved_default = defaultButton
        if resolved_default == QMessageBox.StandardButton.NoButton:
            if buttons & QMessageBox.StandardButton.No:
                resolved_default = QMessageBox.StandardButton.No
            elif buttons & QMessageBox.StandardButton.Yes:
                resolved_default = QMessageBox.StandardButton.Yes
        return _build_and_exec(
            parent=parent,
            icon=QMessageBox.Icon.Question,
            title=str(title or "Xác nhận"),
            text=str(text or ""),
            buttons=buttons,
            default=resolved_default,
        )

    QMessageBox.exec = _patched_exec
    QMessageBox.information = _patched_information
    QMessageBox.warning = _patched_warning
    QMessageBox.critical = _patched_critical
    QMessageBox.question = _patched_question
