from __future__ import annotations

import os
import shutil
import threading
import json
import subprocess
from pathlib import Path
from urllib.request import urlopen
from urllib.error import URLError

from PyQt6.QtCore import QTimer, Qt, QObject, QThread, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QIntValidator
from PyQt6.QtWidgets import (
    QWidget,
    QFormLayout,
    QLineEdit,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QComboBox,
    QMessageBox,
    QGroupBox,
    QVBoxLayout,
    QPlainTextEdit,
    QSizePolicy,
    QDialog,
    QTextEdit,
    QStyle,
    QGridLayout,
)


class SettingsTab(QWidget):
    REQUIRED_PROJECT_URL_PREFIX = "https://labs.google/fx/vi/tools/flow/project/"

    def __init__(self, config, parent: QWidget | None = None):
        super().__init__(parent)
        self._cfg = config
        self.setObjectName("SettingsTab")
        self.setStyleSheet(
            """
            QWidget#SettingsTab QComboBox#SettingsCombo {
                font-size: 12px;
                min-height: 30px;
                padding: 4px 8px;
            }
            QWidget#SettingsTab QComboBox#SettingsCombo QAbstractItemView {
                font-size: 12px;
                outline: none;
            }
            QWidget#SettingsTab QComboBox#SettingsCombo QAbstractItemView::item {
                min-height: 30px;
                padding: 4px 8px;
            }
            """
        )

        root = QHBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(14)

        def int_edit(min_v: int, max_v: int, val: int, width: int = 70) -> QLineEdit:
            e = QLineEdit(str(int(val)))
            e.setValidator(QIntValidator(int(min_v), int(max_v), e))
            e.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            e.setFixedWidth(int(width))
            e.setFixedHeight(34)
            return e

        def combo(items: list[str], cur: str) -> QComboBox:
            c = QComboBox()
            c.setObjectName("SettingsCombo")
            c.addItems(items)
            c.setCurrentText(str(cur))
            c.setFixedWidth(90)
            c.setFixedHeight(34)
            return c

        # ========== LEFT: Setting Input ==========
        left_box = QGroupBox("Setting Input")
        left_box.setStyleSheet("QGroupBox{font-weight:800;}")
        left = QVBoxLayout(left_box)
        left.setContentsMargins(10, 10, 10, 10)
        left.setSpacing(8)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        form.setFormAlignment(Qt.AlignmentFlag.AlignTop)
        form.setHorizontalSpacing(10)
        form.setVerticalSpacing(8)

        output_cur = 1
        try:
            output_cur = int(getattr(config, "output_count", 1) or 1)
        except Exception:
            output_cur = 1
        if output_cur < 1:
            output_cur = 1
        if output_cur > 4:
            output_cur = 4
        self.output_count = combo(["1", "2", "3", "4"], str(output_cur))
        form.addRow("Số đầu ra:", self.output_count)

        self.multi_video = int_edit(1, 20, int(getattr(config, "multi_video", 3) or 3))
        form.addRow("Số Luồng Chạy:", self.multi_video)

        self.num_chrome = int_edit(1, 4, int(getattr(config, "num_chrome", 1) or 1))
        form.addRow("Số Chrome:", self.num_chrome)

        self.wait_gen_video = int_edit(0, 999, int(getattr(config, "wait_gen_video", 15) or 15))
        form.addRow("WAIT_GEN_VIDEO:", self.wait_gen_video)

        self.wait_gen_image = int_edit(0, 999, int(getattr(config, "wait_gen_image", 15) or 15))
        form.addRow("WAIT_GEN_IMAGE:", self.wait_gen_image)

        self.retry_with_error = int_edit(0, 99, int(getattr(config, "retry_with_error", 3) or 3))
        form.addRow("RETRY_WITH_ERROR:", self.retry_with_error)

        self.CLEAR_DATA_IMAGE = int_edit(0, 999, int(getattr(config, "CLEAR_DATA_IMAGE", 11) or 11))
        form.addRow("CLEAR_DATA_IMAGE:", self.CLEAR_DATA_IMAGE)

        self.clear_data = int_edit(0, 999, int(getattr(config, "clear_data", 5) or 5))
        form.addRow("CLEAR_DATA:", self.clear_data)

        self.clear_data_wait = int_edit(0, 999, int(getattr(config, "clear_data_wait", 4) or 4))
        form.addRow("CLEAR_DATA_WAIT:", self.clear_data_wait)

        self.wait_resend_video = int_edit(0, 999, int(getattr(config, "wait_resend_video", 10) or 10))
        form.addRow("WAIT_RESEND_VIDEO:", self.wait_resend_video)

        self.wait_between_prompts = int_edit(0, 999, int(getattr(config, "wait_between_prompts", 12) or 12))
        form.addRow("WAIT_BETWEEN_PROMPTS:", self.wait_between_prompts)

        self.download_mode = combo(["720", "1080", "2K", "4K"], str(getattr(config, "download_mode", "720") or "720"))
        form.addRow("Download Mode:", self.download_mode)

        self.seed_mode = combo(["Random", "Fixed"], str(getattr(config, "seed_mode", "Random") or "Random"))
        form.addRow("Seed Mode:", self.seed_mode)

        self.seed_value = int_edit(0, 999999, int(getattr(config, "seed_value", 9797) or 9797))
        form.addRow("Seed Value:", self.seed_value)

        left.addLayout(form)
        left.addStretch(1)

        # ========== RIGHT: Account & Chrome ==========
        right_box = QGroupBox("Tài khoản & Chrome")
        right_box.setStyleSheet("QGroupBox{font-weight:800;}")
        right = QVBoxLayout(right_box)
        right.setContentsMargins(10, 10, 10, 10)
        right.setSpacing(8)

        acct_form = QFormLayout()
        acct_form.setHorizontalSpacing(10)
        acct_form.setVerticalSpacing(8)

        self.veo3_user = QLineEdit(str(getattr(config, "veo3_user", "") or getattr(config, "USER", "") or ""))
        self.veo3_user.setFixedHeight(34)
        acct_form.addRow("TK:", self.veo3_user)

        pw_row = QHBoxLayout()
        self.veo3_pass = QLineEdit(str(getattr(config, "veo3_pass", "") or getattr(config, "PASS", "") or ""))
        self.veo3_pass.setEchoMode(QLineEdit.EchoMode.Password)
        self.veo3_pass.setFixedHeight(34)

        self._pw_pinned_visible = False
        self._pw_hide_timer = QTimer(self)
        self._pw_hide_timer.setSingleShot(True)
        self._pw_hide_timer.timeout.connect(self._auto_hide_pw)
        self.veo3_pass.textEdited.connect(self._on_pw_edited)

        self.btn_eye = QPushButton("Hiện")
        self.btn_eye.setFixedSize(40, 34)
        self.btn_eye.setStyleSheet("font-size:12px; font-weight: bold;")
        self.btn_eye.clicked.connect(self._toggle_pw)
        pw_row.addWidget(self.veo3_pass, 1)
        pw_row.addWidget(self.btn_eye, 0)
        acct_form.addRow("MK:", pw_row)

        right.addLayout(acct_form)

        # Cookie Lab Google
        cookie_label = QLabel("Cookie Lab Google:")
        cookie_label.setStyleSheet("font-weight:700;")
        right.addWidget(cookie_label)

        existing_cookie = str(getattr(config, "cookie_lab", "") or "")
        self.cookie_lab = QLineEdit(existing_cookie)
        self.cookie_lab.setFixedHeight(34)
        self.cookie_lab.setPlaceholderText("Dán cookie từ LabGoogle vào đây...")
        right.addWidget(self.cookie_lab)

        self.cookie_status = QLabel("KHÔNG CÓ COOKIE VEO3" if not existing_cookie else "✅ Đã có Cookie")
        self.cookie_status.setStyleSheet(
            "color: #ff4444; font-weight:700; font-size:12px;"
            if not existing_cookie else
            "color: #00cc66; font-weight:700; font-size:12px;"
        )
        right.addWidget(self.cookie_status)
        self.cookie_lab.textChanged.connect(self._on_cookie_changed)

        # Chrome control buttons
        chrome_grid = QGridLayout()
        chrome_grid.setSpacing(8)

        self.btn_load_captcha = QPushButton("Load Captcha Mới")
        self.btn_load_captcha.setObjectName("Accent")
        self.btn_load_captcha.setFixedHeight(36)
        self.btn_load_captcha.clicked.connect(self._load_captcha_new)

        self.btn_xoa_chrome = QPushButton("Xóa Chrome")
        self.btn_xoa_chrome.setObjectName("Danger")
        self.btn_xoa_chrome.setFixedHeight(36)
        self.btn_xoa_chrome.clicked.connect(self._xoa_chrome)

        self.btn_mo_labgoogle = QPushButton("Mở LabGoogle")
        self.btn_mo_labgoogle.setObjectName("Warning")
        self.btn_mo_labgoogle.setFixedHeight(36)
        self.btn_mo_labgoogle.clicked.connect(self._mo_labgoogle)

        self.btn_open_profile = QPushButton("Mở Profile Chrome")
        self.btn_open_profile.setFixedHeight(36)
        self.btn_open_profile.clicked.connect(self._open_profile)

        chrome_grid.addWidget(self.btn_load_captcha, 0, 0)
        chrome_grid.addWidget(self.btn_xoa_chrome, 0, 1)
        chrome_grid.addWidget(self.btn_mo_labgoogle, 1, 0)
        chrome_grid.addWidget(self.btn_open_profile, 1, 1)
        right.addLayout(chrome_grid)

        self.btn_auto_login = QPushButton("AUTO Login & Lấy Cookie")
        self.btn_auto_login.setObjectName("Orange")
        self.btn_auto_login.setFixedHeight(38)
        self.btn_auto_login.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon))
        self.btn_auto_login.clicked.connect(self._auto_login_veo3)
        right.addWidget(self.btn_auto_login)

        self._auto_login_thread: QThread | None = None
        self._auto_login_worker = None
        self._auto_login_stopped = False

        # Gemini API Keys
        keys_title = QLabel(
            "Gemini API Keys (mỗi dòng 1 key):\n"
            "API key chỉ dùng cho tính năng tạo video từ Ý Tưởng."
        )
        keys_title.setStyleSheet("QLabel{font-weight:800;}")
        keys_title.setWordWrap(True)
        right.addWidget(keys_title)

        self.gemini_api_keys = QPlainTextEdit()
        self.gemini_api_keys.setPlainText(str(getattr(config, "gemini_api_keys", "") or ""))
        self.gemini_api_keys.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.gemini_api_keys.setFixedHeight(100)
        right.addWidget(self.gemini_api_keys)

        # Save & Delete buttons
        btn_row = QHBoxLayout()
        btn_row.setSpacing(10)

        self.btn_save = QPushButton("Lưu cài đặt")
        self.btn_save.setObjectName("Accent")
        self.btn_save.setFixedHeight(38)
        self.btn_save.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_DialogSaveButton))
        self.btn_save.clicked.connect(self._save)
        btn_row.addWidget(self.btn_save)

        self.btn_delete_profile = QPushButton("Xóa Profile")
        self.btn_delete_profile.setObjectName("Danger")
        self.btn_delete_profile.setFixedHeight(38)
        self.btn_delete_profile.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_TrashIcon))
        self.btn_delete_profile.clicked.connect(self._delete_profile)
        btn_row.addWidget(self.btn_delete_profile)

        right.addLayout(btn_row)
        right.addStretch(1)

        self._last_profile_dir: str = ""
        self._last_profile_cdp_host: str = "127.0.0.1"
        self._last_profile_cdp_port: int = 0

        root.addWidget(left_box, 2)
        root.addWidget(right_box, 3)

    def _on_cookie_changed(self, text: str) -> None:
        has_cookie = bool(text.strip())
        self.cookie_status.setText("✅ Đã có Cookie" if has_cookie else "KHÔNG CÓ COOKIE VEO3")
        self.cookie_status.setStyleSheet(
            "color: #00cc66; font-weight:700; font-size:12px;"
            if has_cookie else
            "color: #ff4444; font-weight:700; font-size:12px;"
        )

    def _toggle_pw(self) -> None:
        self._pw_pinned_visible = not bool(self._pw_pinned_visible)
        if self._pw_pinned_visible:
            try:
                self._pw_hide_timer.stop()
            except Exception:
                pass
            self.veo3_pass.setEchoMode(QLineEdit.EchoMode.Normal)
            self.btn_eye.setText("Ẩn")
        else:
            self.veo3_pass.setEchoMode(QLineEdit.EchoMode.Password)
            self.btn_eye.setText("Hiện")

    def _on_pw_edited(self, _text: str) -> None:
        if self._pw_pinned_visible:
            return
        self.veo3_pass.setEchoMode(QLineEdit.EchoMode.Normal)
        self.btn_eye.setText("Ẩn")
        try:
            self._pw_hide_timer.start(900)
        except Exception:
            pass

    def _auto_hide_pw(self) -> None:
        if self._pw_pinned_visible:
            return
        try:
            self.veo3_pass.setEchoMode(QLineEdit.EchoMode.Password)
            self.btn_eye.setText("Hiện")
        except Exception:
            pass

    def _auto_login_veo3(self) -> None:
        user = self.veo3_user.text().strip()
        pwd = self.veo3_pass.text()
        if not user or not pwd:
            QMessageBox.warning(self, "Lỗi", "Nhập TK và MK trước khi Auto Login.")
            return
        if self._auto_login_thread is not None:
            QMessageBox.information(self, "Thông báo", "Auto login đang chạy.")
            return

        self._auto_login_stopped = False
        self.btn_auto_login.setEnabled(False)
        self.btn_auto_login.setText("⏳ Đang Auto Login...")

        profile_name = self._current_profile_name()

        class _Worker(QObject):
            log_signal = pyqtSignal(str)
            result_signal = pyqtSignal(dict)
            finished = pyqtSignal()

            def __init__(self, username, password, profile):
                super().__init__()
                self._u = username
                self._p = password
                self._profile = profile
                self._stop = threading.Event()

            def stop(self):
                self._stop.set()

            def run(self):
                try:
                    from login import auto_login_veo3
                    result = auto_login_veo3(
                        self._u, self._p,
                        profile_name=self._profile,
                        logger=lambda m: self.log_signal.emit(str(m)),
                        stop_check=self._stop.is_set,
                    )
                    if not isinstance(result, dict):
                        result = {"success": False, "message": "Kết quả không hợp lệ"}
                    self.result_signal.emit(result)
                except Exception as exc:
                    self.result_signal.emit({"success": False, "message": str(exc)})
                finally:
                    self.finished.emit()

        thread = QThread(self)
        worker = _Worker(user, pwd, profile_name)
        worker.moveToThread(thread)

        thread.started.connect(worker.run)
        worker.log_signal.connect(lambda m: None)
        worker.result_signal.connect(self._on_auto_login_result)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._on_auto_login_finished)

        self._auto_login_thread = thread
        self._auto_login_worker = worker
        thread.start()

    def _on_auto_login_result(self, result: dict) -> None:
        ok = bool(result.get("success"))
        msg = str(result.get("message") or "")
        if ok:
            try:
                from settings_manager import SettingsManager
                cfg = SettingsManager.load_config()
                account = cfg.get("account1", {}) if isinstance(cfg, dict) else {}
                cookie = str(account.get("cookie") or "")
                if cookie:
                    self.cookie_lab.setText(cookie)
                    self._on_cookie_changed(cookie)
            except Exception:
                pass
            QMessageBox.information(self, "Thành công", msg or "Auto login thành công! Cookie đã được tự động điền.")
        else:
            QMessageBox.warning(self, "Lỗi", msg or "Auto login thất bại.")

    def _on_auto_login_finished(self) -> None:
        self._auto_login_thread = None
        self._auto_login_worker = None
        self.btn_auto_login.setEnabled(True)
        self.btn_auto_login.setText("AUTO Login & Lấy Cookie")

    def _get_num_chrome(self) -> int:
        try:
            val = int(self.num_chrome.text().strip() or "1")
            return max(1, min(4, val))
        except Exception:
            return 1

    def _load_captcha_new(self) -> None:
        try:
            from chrome import (
                get_chrome_executable_path,
                start_chrome_debug,
                pick_cdp_port_for_new_session,
                resolve_profile_dir,
                CDP_HOST,
            )
            from token_pool import TokenPool

            num = self._get_num_chrome()
            base_profile = resolve_profile_dir(self._current_profile_name())
            chrome_exe = get_chrome_executable_path()

            opened = 0
            for i in range(num):
                try:
                    profile_dir = TokenPool.get_pool_profile_dir(str(base_profile), i)
                    Path(profile_dir).mkdir(parents=True, exist_ok=True)
                    port = pick_cdp_port_for_new_session(CDP_HOST, 9222 + i)
                    start_chrome_debug(
                        chrome_exe=chrome_exe,
                        host=CDP_HOST,
                        port=port,
                        user_data_dir=Path(profile_dir),
                        url="https://labs.google/fx/vi/tools/flow",
                        offscreen=False,
                    )
                    opened += 1
                except Exception:
                    pass

            if opened > 0:
                self.btn_load_captcha.setText(f"✅ {opened} Chrome đang chạy")
                QMessageBox.information(
                    self,
                    "Load Captcha",
                    f"Đã mở {opened}/{num} Chrome instances.\n"
                    "Mỗi Chrome sử dụng profile riêng biệt.\n"
                    "Đăng nhập Google trên mỗi Chrome để giải captcha."
                )
            else:
                QMessageBox.warning(self, "Lỗi", "Không mở được Chrome nào.")
        except Exception as exc:
            QMessageBox.critical(self, "Lỗi", f"Không mở được Chrome: {exc}")

    def _xoa_chrome(self) -> None:
        try:
            from chrome import kill_profile_chrome, resolve_profile_dir
            from token_pool import TokenPool

            base_profile = resolve_profile_dir(self._current_profile_name())
            num = self._get_num_chrome()

            for i in range(num):
                try:
                    profile_dir = TokenPool.get_pool_profile_dir(str(base_profile), i)
                    kill_profile_chrome(profile_dir)
                except Exception:
                    pass

            self.btn_load_captcha.setText("Load Captcha Mới")
            QMessageBox.information(self, "Thông báo", f"Đã đóng {num} Chrome instances.")
        except Exception as exc:
            QMessageBox.warning(self, "Lỗi", f"Không đóng được Chrome: {exc}")

    def _mo_labgoogle(self) -> None:
        try:
            from chrome import (
                get_chrome_executable_path,
                start_chrome_debug,
                pick_cdp_port_for_new_session,
                resolve_profile_dir,
                CDP_HOST,
            )

            profile_dir = resolve_profile_dir(self._current_profile_name())
            profile_dir.mkdir(parents=True, exist_ok=True)
            chrome_exe = get_chrome_executable_path()
            port = pick_cdp_port_for_new_session(CDP_HOST, 9250)

            start_chrome_debug(
                chrome_exe=chrome_exe,
                host=CDP_HOST,
                port=port,
                user_data_dir=profile_dir,
                url="https://labs.google/fx/vi/tools/flow",
                offscreen=False,
            )

            self._last_profile_dir = str(profile_dir)
            self._last_profile_cdp_port = port

            QMessageBox.information(
                self,
                "Mở LabGoogle",
                f"Đã mở Chrome với profile riêng.\n"
                f"Profile: {profile_dir.name}\n"
                f"CDP Port: {port}\n\n"
                "1. Đăng nhập tài khoản Google\n"
                "2. Tạo 1 dự án mới\n"
                "3. Copy cookie từ DevTools → dán vào ô Cookie\n"
                "4. Bấm Lưu cài đặt"
            )
        except Exception as exc:
            QMessageBox.critical(self, "Lỗi", f"Không mở được Chrome: {exc}")

    def _save(self) -> None:
        def _as_int(e: QLineEdit, default: int = 0) -> int:
            t = (e.text() or "").strip()
            try:
                return int(t)
            except Exception:
                return int(default)

        setattr(self._cfg, "multi_video", _as_int(self.multi_video, 1))
        setattr(self._cfg, "num_chrome", _as_int(self.num_chrome, 1))
        try:
            setattr(self._cfg, "output_count", int(self.output_count.currentText().strip() or "1"))
        except Exception:
            setattr(self._cfg, "output_count", 1)
        setattr(self._cfg, "wait_gen_video", _as_int(self.wait_gen_video, 15))
        setattr(self._cfg, "wait_gen_image", _as_int(self.wait_gen_image, 15))
        setattr(self._cfg, "retry_with_error", _as_int(self.retry_with_error, 3))
        setattr(self._cfg, "CLEAR_DATA_IMAGE", _as_int(self.CLEAR_DATA_IMAGE, 11))
        setattr(self._cfg, "clear_data", _as_int(self.clear_data, 5))
        setattr(self._cfg, "clear_data_wait", _as_int(self.clear_data_wait, 4))
        setattr(self._cfg, "wait_resend_video", _as_int(self.wait_resend_video, 10))
        setattr(self._cfg, "wait_between_prompts", _as_int(self.wait_between_prompts, 12))
        setattr(self._cfg, "download_mode", self.download_mode.currentText().strip() or "720")
        setattr(self._cfg, "token_option", "Option2")
        setattr(self._cfg, "seed_mode", self.seed_mode.currentText().strip() or "Random")
        setattr(self._cfg, "seed_value", _as_int(self.seed_value, 9797))

        setattr(self._cfg, "veo3_user", self.veo3_user.text().strip())
        setattr(self._cfg, "veo3_pass", self.veo3_pass.text())
        setattr(self._cfg, "gemini_api_keys", self.gemini_api_keys.toPlainText().strip())

        cookie_text = self.cookie_lab.text().strip()
        try:
            from settings_manager import SettingsManager
            cfg = SettingsManager.load_config()
            if not isinstance(cfg, dict):
                cfg = {}
            account = cfg.get("account1") if isinstance(cfg.get("account1"), dict) else {}
            account = dict(account or {})
            account["cookie"] = cookie_text
            cfg["account1"] = account
            SettingsManager.save_config(cfg)
        except Exception:
            pass

        try:
            self._cfg.save()
            QMessageBox.information(self, "Thông báo", "Cấu hình đã được lưu.")
        except Exception as exc:
            QMessageBox.critical(self, "Lỗi", f"Không lưu được cấu hình: {exc}")

    def _profile_dir(self) -> Path:
        profile_name = self._current_profile_name()
        try:
            from chrome import resolve_profile_dir
            return resolve_profile_dir(profile_name)
        except Exception:
            from settings_manager import BASE_DIR
            root = Path(BASE_DIR)
            chrome_root = Path(os.getenv("CHROME_USER_DATA_ROOT", str(root / "chrome_user_data")))
            return chrome_root / profile_name

    def _current_profile_name(self) -> str:
        try:
            from settings_manager import SettingsManager
            settings = SettingsManager.load_settings()
            if isinstance(settings, dict):
                cur = str(settings.get("current_profile") or "").strip()
                if cur:
                    return cur
        except Exception:
            pass
        return str(os.getenv("PROFILE_NAME", "PROFILE_1") or "PROFILE_1").strip() or "PROFILE_1"

    def _open_profile(self) -> None:
        self._mo_labgoogle()

    def _delete_profile(self) -> None:
        p = self._profile_dir()
        if not p.exists():
            QMessageBox.information(self, "Thông báo", "Profile không tồn tại.")
            return

        msg = QMessageBox(self)
        msg.setIcon(QMessageBox.Icon.Warning)
        msg.setWindowTitle("Xác nhận")
        msg.setText("Bạn chắc chắn muốn xóa Profile?\n(Chrome đang chạy với profile này có thể bị tắt)")
        msg.setInformativeText(str(p))
        msg.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        msg.setDefaultButton(QMessageBox.StandardButton.No)
        if msg.exec() != QMessageBox.StandardButton.Yes:
            return

        try:
            self._xoa_chrome()
            shutil.rmtree(p, ignore_errors=True)

            from token_pool import TokenPool
            base = str(p)
            for i in range(1, 5):
                pool_dir = TokenPool.get_pool_profile_dir(base, i)
                try:
                    shutil.rmtree(pool_dir, ignore_errors=True)
                except Exception:
                    pass

            QMessageBox.information(self, "Thông báo", "Đã xóa Profile và tất cả pool profiles.")
        except Exception as exc:
            QMessageBox.critical(self, "Lỗi", f"Không xóa được profile: {exc}")

