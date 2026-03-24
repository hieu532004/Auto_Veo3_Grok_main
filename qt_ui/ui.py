from __future__ import annotations
import json
import os
import sys
import dataclasses
from dataclasses import dataclass
from PyQt6.QtCore import Qt, QUrl, QTimer
from PyQt6.QtGui import QDesktopServices, QIcon, QFont
from PyQt6.QtWidgets import (QApplication, QComboBox, QFileDialog, QGridLayout, QHBoxLayout,
                             QLabel, QLineEdit, QMainWindow, QPushButton, QSplitter,
                             QTabWidget, QVBoxLayout, QWidget, QMessageBox, QFrame)
import status_panel
from status_panel import StatusPanel
import tab_text_to_video
from tab_text_to_video import TextToVideoTab
import tab_image_to_video
from tab_image_to_video import ImageToVideoTab
import tab_character_sync
from tab_character_sync import CharacterSyncTab
import tab_create_image
from tab_create_image import CreateImageTab, CreateImageFromPromptTab
import tab_idea_to_video
from tab_idea_to_video import IdeaToVideoTab
import popup_theme
from popup_theme import install_messagebox_theme
import tab_settings
from tab_settings import SettingsTab
import tab_grok_settings
from tab_grok_settings import GrokSettingsTab
import branding_config
from branding_config import WINDOW_TITLE, APP_VERSION
import settings_manager
from settings_manager import BASE_DIR, get_icon_path
import worker_run_workflow
from worker_run_workflow import WorkflowRunWorker, WorkflowQueueItem
from qt_ui.theme_manager import get_qss, set_theme, current_theme, on_theme_change, get_color, get_palette
from qt_ui.i18n import t, set_lang, current_lang, on_lang_change

_APP_ROOT = str(BASE_DIR)
DATA_GENERAL_DIR = os.path.join(_APP_ROOT, 'data_general')
CONFIG_PATH = os.path.join(DATA_GENERAL_DIR, 'config.json')
GEMINI_KEYS_PATH = os.path.join(DATA_GENERAL_DIR, 'gemini_api_key.txt')
OLD_CONFIG_PATH = os.path.join(_APP_ROOT, 'config.json')
DEFAULT_DOWNLOAD_DIR = os.path.join(_APP_ROOT, 'downloads')

VEO_MODEL_FAST = 'Veo 3.1 - Fast'
VEO_MODEL_QUALITY = 'Veo 3.1 - Quality'
VEO_MODEL_FAST_2 = 'Veo 3.1 - Fast 2.0'
VEO_MODEL_OPTIONS = [VEO_MODEL_FAST, VEO_MODEL_QUALITY, VEO_MODEL_FAST_2]

_icon_cache: dict[str, QIcon] = {}

def icon(name: str) -> QIcon:
    if name in _icon_cache:
        return _icon_cache[name]
    path = get_icon_path(name)
    if os.path.isfile(path):
        ic = QIcon(path)
    else:
        ic = QIcon()
    _icon_cache[name] = ic
    return ic

def app_logo_icon() -> QIcon:
    icon_dir = os.path.join(_APP_ROOT, 'icons')
    candidates = ['app_icon.ico', 'app_icon.png', 'logo.ico', 'logo.png']
    for filename in candidates:
        path = get_icon_path(filename)
        if os.path.isfile(path):
            ic = QIcon(path)
            if not ic.isNull():
                return ic
    
    try:
        for filename in os.listdir(icon_dir):
            if filename.lower().endswith(('.ico', '.png', '.jpg', '.jpeg', '.bmp', '.svg')):
                path = os.path.join(icon_dir, filename)
                if os.path.isfile(path):
                    ic = QIcon(path)
                    if not ic.isNull():
                        return ic
    except Exception:
        pass
    return QIcon()

class _ClickPickLineEdit(QLineEdit):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._picker = None

    def set_picker(self, fn) -> None:
        self._picker = fn

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and callable(self._picker):
            try:
                self._picker()
            except Exception:
                pass
        super().mousePressEvent(event)

@dataclass
class AppConfig:
    USER: str = ''
    PASS: str = ''
    TYPE_ACCOUNT: str = 'NORMAL'
    multi_video: int = 3
    num_chrome: int = 1
    output_count: int = 1
    create_image_model: str = 'Imagen 4'
    video_aspect_ratio: str = '9:16'
    veo_model: str = VEO_MODEL_FAST
    offscreen_chrome: bool = True
    video_output_dir: str = DEFAULT_DOWNLOAD_DIR
    idea_scene_count: int = 1
    idea_style: str = '3d_Pixar'
    idea_dialogue_language: str = 'Tiếng Việt (vi-VN)'
    veo3_user: str = ''
    veo3_pass: str = ''
    wait_gen_video: int = 15
    wait_gen_image: int = 15
    retry_with_error: int = 3
    CLEAR_DATA_IMAGE: int = 11
    clear_data: int = 5
    clear_data_wait: int = 4
    wait_resend_video: int = 10
    download_mode: str = '720'
    token_option: str = 'Option2'
    cookie_lab: str = ''
    seed_mode: str = 'Random'
    seed_value: int = 9797
    gemini_api_keys: str = ''
    grok_user: str = ''
    grok_pass: str = ''
    grok_account_type: str = 'SUPER'
    grok_multi_video: int = 5
    grok_video_length_seconds: int = 6
    grok_video_resolution: str = '480p'

    @staticmethod
    def _config_token_option_for_json(token_option: str) -> str:
        return 'Option 2'

    @staticmethod
    def _config_token_option_from_json(val: str | None) -> str:
        return 'Option2'

    @classmethod
    def load(cls) -> 'AppConfig':
        cfg = cls()
        os.makedirs(DATA_GENERAL_DIR, exist_ok=True)
        
        load_path = CONFIG_PATH
        if os.path.isfile(load_path):
            pass
        elif os.path.isfile(OLD_CONFIG_PATH):
            load_path = OLD_CONFIG_PATH
            
        try:
            with open(load_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except Exception:
            data = None
            
        if not isinstance(data, dict):
            data = {}
            
        if int(data.get('MULTI_VIDEO', cfg.multi_video)):
            cfg.multi_video = int(data.get('MULTI_VIDEO', cfg.multi_video))
        if int(data.get('NUM_CHROME', cfg.num_chrome)):
            cfg.num_chrome = int(data.get('NUM_CHROME', cfg.num_chrome))
            
        if int(data.get('OUTPUT_COUNT', cfg.output_count)):
            cfg.output_count = int(data.get('OUTPUT_COUNT', cfg.output_count))
            
        if str(data.get('CREATE_IMAGE_MODEL', data.get('create_image_model', cfg.create_image_model))):
            cfg.create_image_model = str(data.get('CREATE_IMAGE_MODEL', data.get('create_image_model', cfg.create_image_model)))
            
        if int(data.get('WAIT_GEN_VIDEO', cfg.wait_gen_video)):
            cfg.wait_gen_video = int(data.get('WAIT_GEN_VIDEO', cfg.wait_gen_video))
            
        if int(data.get('WAIT_GEN_IMAGE', cfg.wait_gen_image)):
            cfg.wait_gen_image = int(data.get('WAIT_GEN_IMAGE', cfg.wait_gen_image))
            
        if int(data.get('CLEAR_DATA_IMAGE', cfg.CLEAR_DATA_IMAGE)):
            cfg.CLEAR_DATA_IMAGE = int(data.get('CLEAR_DATA_IMAGE', cfg.CLEAR_DATA_IMAGE))
            
        if int(data.get('RETRY_WITH_ERROR', cfg.retry_with_error)):
            cfg.retry_with_error = int(data.get('RETRY_WITH_ERROR', cfg.retry_with_error))
            
        if int(data.get('CLEAR_DATA', cfg.clear_data)):
            cfg.clear_data = int(data.get('CLEAR_DATA', cfg.clear_data))
            
        if int(data.get('CLEAR_DATA_WAIT', cfg.clear_data_wait)):
            cfg.clear_data_wait = int(data.get('CLEAR_DATA_WAIT', cfg.clear_data_wait))
            
        if int(data.get('WAIT_RESEND_VIDEO', cfg.wait_resend_video)):
            cfg.wait_resend_video = int(data.get('WAIT_RESEND_VIDEO', cfg.wait_resend_video))
            
        if str(data.get('DOWNLOAD_MODE', cfg.download_mode)):
            cfg.download_mode = str(data.get('DOWNLOAD_MODE', cfg.download_mode))
            
        cfg.token_option = 'Option2'
        
        account1_data = data.get('account1', {}) if isinstance(data.get('account1'), dict) else {}
        cfg.cookie_lab = str(account1_data.get('cookie', '') or '')
        
        if str(data.get('SEED_MODE', cfg.seed_mode)):
            cfg.seed_mode = str(data.get('SEED_MODE', cfg.seed_mode))
            
        if int(data.get('SEED_VALUE', cfg.seed_value)):
            cfg.seed_value = int(data.get('SEED_VALUE', cfg.seed_value))
            
        if str(data.get('VIDEO_ASPECT_RATIO', data.get('video_aspect_ratio', cfg.video_aspect_ratio))):
            cfg.video_aspect_ratio = str(data.get('VIDEO_ASPECT_RATIO', data.get('video_aspect_ratio', cfg.video_aspect_ratio)))
            
        if str(data.get('VEO_MODEL', data.get('veo_model', cfg.veo_model))):
            cfg.veo_model = str(data.get('VEO_MODEL', data.get('veo_model', cfg.veo_model)))
            
        if str(data.get('VIDEO_OUTPUT_DIR', data.get('video_output_dir', cfg.video_output_dir))):
            cfg.video_output_dir = str(data.get('VIDEO_OUTPUT_DIR', data.get('video_output_dir', cfg.video_output_dir)))
            
        if int(data.get('IDEA_SCENE_COUNT', data.get('idea_scene_count', cfg.idea_scene_count))):
            cfg.idea_scene_count = int(data.get('IDEA_SCENE_COUNT', data.get('idea_scene_count', cfg.idea_scene_count)))
            
        if str(data.get('IDEA_STYLE', data.get('idea_style', cfg.idea_style))):
            cfg.idea_style = str(data.get('IDEA_STYLE', data.get('idea_style', cfg.idea_style)))
            
        if str(data.get('IDEA_DIALOGUE_LANGUAGE', data.get('idea_dialogue_language', cfg.idea_dialogue_language))):
            cfg.idea_dialogue_language = str(data.get('IDEA_DIALOGUE_LANGUAGE', data.get('idea_dialogue_language', cfg.idea_dialogue_language)))
            
        cfg.offscreen_chrome = bool(data.get('offscreen_chrome', cfg.offscreen_chrome))
        
        if os.path.isfile(GEMINI_KEYS_PATH):
            try:
                with open(GEMINI_KEYS_PATH, 'r', encoding='utf-8') as kf:
                    lines = kf.read().splitlines()
                    clean_lines = []
                    for ln in lines:
                        if ln.strip():
                            clean_lines.append(ln.strip())
                    cfg.gemini_api_keys = '\n'.join(clean_lines)
            except Exception:
                pass
        else:
            gem = data.get('GEMINI_API_KEYS', '')
            if isinstance(gem, list):
                clean_gem = []
                for x in gem:
                    if str(x).strip():
                        clean_gem.append(str(x))
                cfg.gemini_api_keys = '\n'.join(clean_gem)
            elif str(gem):
                cfg.gemini_api_keys = str(gem)
                
        account1 = {}
        if isinstance(data.get('account1'), dict):
            account1 = data.get('account1')
        elif isinstance(data.get('account1'), dict): # Disassembly repeats check?
             account1 = data.get('account1')
             
        if account1:
            if str(account1.get('email')) or str(cfg.veo3_user):
                cfg.veo3_user = str(account1.get('email')) or ''
            if str(account1.get('password')) or str(cfg.veo3_pass):
                cfg.veo3_pass = str(account1.get('password')) or ''
                
        grok_account = {}
        if isinstance(data.get('grok_account'), dict):
            grok_account = data.get('grok_account')
        elif isinstance(data.get('grok_account'), dict):
             grok_account = data.get('grok_account')
             
        if grok_account:
            if str(grok_account.get('email')) or str(cfg.grok_user):
                cfg.grok_user = str(grok_account.get('email')) or ''
            if str(grok_account.get('password')) or str(cfg.grok_pass):
                cfg.grok_pass = str(grok_account.get('password')) or ''
                
            raw_grok_type = 'SUPER'
            if str(grok_account.get('type_account')):
                raw_grok_type = str(grok_account.get('type_account'))
            elif str(grok_account.get('TYPE_ACCOUNT')):
                raw_grok_type = str(grok_account.get('TYPE_ACCOUNT'))
            elif str(data.get('GROK_ACCOUNT_TYPE')):
                raw_grok_type = str(data.get('GROK_ACCOUNT_TYPE'))
            elif str(cfg.grok_account_type):
                raw_grok_type = str(cfg.grok_account_type)
                
            raw_grok_type = raw_grok_type.strip().upper()
            if raw_grok_type == 'ULTRA':
                raw_grok_type = 'SUPER'
            if raw_grok_type == 'NORMAL':
                raw_grok_type = 'NORMAL'
            else:
                raw_grok_type = 'SUPER'
            cfg.grok_account_type = raw_grok_type
            
        if int(data.get('GROK_VIDEO_LENGTH_SECONDS', data.get('grok_video_length_seconds', cfg.grok_video_length_seconds))):
            val = int(data.get('GROK_VIDEO_LENGTH_SECONDS', data.get('grok_video_length_seconds', cfg.grok_video_length_seconds)))
            if val not in {6, 10}:
                val = 6
            cfg.grok_video_length_seconds = val
            
        if str(data.get('GROK_VIDEO_RESOLUTION', data.get('grok_video_resolution', cfg.grok_video_resolution))):
            val = str(data.get('GROK_VIDEO_RESOLUTION', data.get('grok_video_resolution', cfg.grok_video_resolution)))
            if val not in {'480p', '720p'}:
                val = '480p'
            cfg.grok_video_resolution = val
            
        if load_path == OLD_CONFIG_PATH and not os.path.isfile(CONFIG_PATH):
            cfg.save()
            
        cfg.video_resolution = '480p'
        cfg.auto_upscale = True
        
        cfg.token_option = 'Option2'
        
        if not str(cfg.video_output_dir).strip():
            cfg.video_output_dir = DEFAULT_DOWNLOAD_DIR
            
        try:
            cfg.idea_scene_count = max(1, min(100, int(cfg.idea_scene_count)))
        except Exception:
            cfg.idea_scene_count = 1
            
        if str(cfg.video_aspect_ratio) not in {'16:9', '9:16'}:
            cfg.video_aspect_ratio = '9:16'
            
        if str(cfg.veo_model) not in VEO_MODEL_OPTIONS:
            cfg.veo_model = VEO_MODEL_FAST
            
        if str(cfg.create_image_model) not in {'Imagen 4', 'Nano Banana', 'Nano Banana 2', 'Nano Banana pro'}:
            cfg.create_image_model = 'Imagen 4'
            
        if int(cfg.grok_video_length_seconds) not in {6, 10}:
            cfg.grok_video_length_seconds = 6
            
        if str(cfg.grok_video_resolution) not in {'480p', '720p'}:
            cfg.grok_video_resolution = '480p'
            
        if getattr(cfg, 'grok_account_type', 'SUPER') == 'SUPER':
            raw_grok_type = 'SUPER'
        else:
            raw_grok_type = str(getattr(cfg, 'grok_account_type', 'SUPER')).strip().upper()
            if raw_grok_type == 'ULTRA':
                raw_grok_type = 'SUPER'
            if raw_grok_type == 'NORMAL':
                raw_grok_type = 'NORMAL'
            else:
                raw_grok_type = 'SUPER'
        cfg.grok_account_type = raw_grok_type
        
        if cfg.grok_account_type == 'NORMAL':
            cfg.grok_video_resolution = '480p'
            cfg.grok_video_length_seconds = 6
            
        return cfg

    def save(self) -> None:
        os.makedirs(DATA_GENERAL_DIR, exist_ok=True)
        existing = {}
        
        try:
            with open(CONFIG_PATH, 'r', encoding='utf-8') as rf:
                loaded = json.load(rf)
                if isinstance(loaded, dict):
                    existing = loaded
        except Exception:
            pass
            
        account1_existing = {}
        if isinstance(existing.get('account1'), dict):
            account1_existing = existing.get('account1')
        elif isinstance(existing.get('account1'), dict):
             account1_existing = existing.get('account1')
             
        account1 = dict(account1_existing) if account1_existing else {}
        
        if str(self.veo3_user) or str(self.USER):
            email = str(self.veo3_user) or ''
            if email:
                account1['email'] = email
                
        if str(self.veo3_pass) or str(self.PASS):
            pw = str(self.veo3_pass) or ''
            if pw:
                account1['password'] = pw
                
        account1.setdefault('sessionId', '')
        account1.setdefault('projectId', '')
        account1.setdefault('access_token', '')
        account1.setdefault('cookie', '')
        account1.setdefault('TYPE_ACCOUNT', 'ULTRA')
        account1.setdefault('folder_user_data_get_token', str(os.getenv('CHROME_USER_DATA_DIR', '')) or '')
        account1.setdefault('URL_GEN_TOKEN', 'https://labs.google/fx/vi/tools/flow')
        
        grok_account_existing = {}
        if isinstance(existing.get('grok_account'), dict):
            grok_account_existing = existing.get('grok_account')
        elif isinstance(existing.get('grok_account'), dict):
             grok_account_existing = existing.get('grok_account')
             
        grok_account = dict(grok_account_existing) if grok_account_existing else {}
        
        if str(self.grok_user):
            grok_email = str(self.grok_user) or ''
            if grok_email:
                grok_account['email'] = grok_email
                
        if str(self.grok_pass):
            grok_password = str(self.grok_pass) or ''
            if grok_password:
                grok_account['password'] = grok_password
                
        grok_account_type = str(getattr(self, 'grok_account_type', 'SUPER')).strip().upper()
        if grok_account_type == 'ULTRA':
            grok_account_type = 'SUPER'
        if grok_account_type == 'NORMAL':
            grok_account_type = 'NORMAL'
        else:
            grok_account_type = 'SUPER'
            
        grok_account['type_account'] = grok_account_type
        grok_account['TYPE_ACCOUNT'] = grok_account_type
        
        if str(self.gemini_api_keys):
            gem_lines = []
            for ln in self.gemini_api_keys.splitlines():
                if ln.strip():
                    gem_lines.append(ln.strip())
        else:
            gem_lines = []
            
        data = {
            'MULTI_VIDEO': int(self.multi_video) if self.multi_video else 1,
            'NUM_CHROME': int(self.num_chrome) if self.num_chrome else 1,
            'OUTPUT_COUNT': int(self.output_count) if self.output_count else 1,
            'CREATE_IMAGE_MODEL': str(self.create_image_model) if self.create_image_model else 'Imagen 4',
            'WAIT_GEN_VIDEO': int(self.wait_gen_video) if self.wait_gen_video else 15,
            'WAIT_GEN_IMAGE': int(self.wait_gen_image) if self.wait_gen_image else 15,
            'CLEAR_DATA_IMAGE': int(self.CLEAR_DATA_IMAGE) if self.CLEAR_DATA_IMAGE else 11,
            'RETRY_WITH_ERROR': int(self.retry_with_error) if self.retry_with_error else 3,
            'CLEAR_DATA': int(self.clear_data) if self.clear_data else 5,
            'CLEAR_DATA_WAIT': int(self.clear_data_wait) if self.clear_data_wait else 4,
            'WAIT_RESEND_VIDEO': int(self.wait_resend_video) if self.wait_resend_video else 10,
            'DOWNLOAD_MODE': str(self.download_mode) if self.download_mode else '720',
            'TOKEN_OPTION': 'Option 1',
            'SEED_MODE': str(self.seed_mode) if self.seed_mode else 'Random',
            'SEED_VALUE': int(self.seed_value) if self.seed_value else 9797,
            'VIDEO_ASPECT_RATIO': str(self.video_aspect_ratio) if self.video_aspect_ratio else '9:16',
            'VEO_MODEL': str(self.veo_model) if self.veo_model else VEO_MODEL_FAST,
            'VIDEO_OUTPUT_DIR': str(self.video_output_dir) if self.video_output_dir else DEFAULT_DOWNLOAD_DIR,
            'IDEA_SCENE_COUNT': int(max(1, min(100, int(self.idea_scene_count)))) if self.idea_scene_count else 1,
            'IDEA_STYLE': str(self.idea_style) if self.idea_style else '3d_Pixar',
            'IDEA_DIALOGUE_LANGUAGE': str(self.idea_dialogue_language) if self.idea_dialogue_language else 'Tiếng Việt (vi-VN)',
            'GROK_VIDEO_LENGTH_SECONDS': int(self.grok_video_length_seconds) if self.grok_video_length_seconds in (6, 10) else 6,
            'GROK_VIDEO_RESOLUTION': str(self.grok_video_resolution) if self.grok_video_resolution in ('480p', '720p') else '480p',
            'GROK_ACCOUNT_TYPE': grok_account_type,
            'account1': account1,
            'grok_account': grok_account
        }
        
        if 'offscreen_chrome' in existing:
            data['offscreen_chrome'] = bool(existing.get('offscreen_chrome'))
        else:
            data['offscreen_chrome'] = bool(self.offscreen_chrome)
            
        try:
            with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception:
            pass
            
        try:
            with open(GEMINI_KEYS_PATH, 'w', encoding='utf-8') as kf:
                if gem_lines:
                    kf.write('\n'.join(gem_lines))
                    kf.write('\n')
        except Exception:
            pass

def apply_style(app: QApplication) -> None:
    app.setStyleSheet(get_qss())

class MainWindow(QMainWindow):
    def __init__(self, config: AppConfig):
        super().__init__()
        self._cfg = config
        self.setWindowTitle(str(WINDOW_TITLE) if WINDOW_TITLE else 'VEO TOOL')
        
        app_ic = app_logo_icon()
        if not app_ic.isNull():
            self.setWindowIcon(app_ic)
            
        root = QWidget()
        root.setObjectName('AppRoot')
        self.setCentralWidget(root)

        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        toolbar = QWidget()
        toolbar.setObjectName('AppToolbar')
        toolbar.setFixedHeight(52)
        tb_layout = QHBoxLayout(toolbar)
        tb_layout.setContentsMargins(16, 0, 16, 0)
        tb_layout.setSpacing(10)

        self._app_title_label = QLabel(f'\u26a1 VEO GROK {APP_VERSION}')
        title_font = QFont('Segoe UI', 14)
        title_font.setBold(True)
        self._app_title_label.setFont(title_font)
        tb_layout.addWidget(self._app_title_label)
        tb_layout.addStretch(1)

        self.btn_theme = QPushButton(t('theme_dark') if current_theme() == 'dark' else t('theme_light'))
        self.btn_theme.setObjectName('ToolbarBtn')
        self.btn_theme.setFixedHeight(32)
        self.btn_theme.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_theme.clicked.connect(self._on_theme_toggle)
        tb_layout.addWidget(self.btn_theme)

        self.btn_lang = QPushButton(t('lang_vi') if current_lang() == 'vi' else t('lang_en'))
        self.btn_lang.setObjectName('ToolbarBtn')
        self.btn_lang.setFixedHeight(32)
        self.btn_lang.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_lang.clicked.connect(self._on_lang_toggle)
        tb_layout.addWidget(self.btn_lang)

        root_layout.addWidget(toolbar)

        body_widget = QWidget()
        body_layout = QHBoxLayout(body_widget)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(0)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(True)
        splitter.setHandleWidth(4)
        splitter.setOpaqueResize(True)
        body_layout.addWidget(splitter)
        
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(10, 8, 4, 12)
        left_layout.setSpacing(8)
        
        self.main_tabs = QTabWidget()
        self.main_tabs.setObjectName('TopTabRow')
        
        self.tabs = QTabWidget()
        self.tab_text = TextToVideoTab()
        self.tab_image = ImageToVideoTab()
        self.tab_char_sync = CharacterSyncTab()
        self.tab_create_image = CreateImageTab(self._cfg, on_model_changed=self._on_create_image_model_changed)
        self.tab_idea = IdeaToVideoTab(config)
        self.tab_settings = SettingsTab(config)
        
        self.tabs.addTab(self.tab_text, icon(''), t('tab_text_to_video'))
        self.tabs.addTab(self.tab_image, icon(''), t('tab_image_to_video'))
        self.tabs.addTab(self.tab_idea, icon(''), t('tab_idea_to_video'))
        self.tabs.addTab(self.tab_char_sync, icon(''), t('tab_character_sync'))
        self.tabs.addTab(self.tab_create_image, icon(''), t('tab_create_image'))
        self.tabs.addTab(self.tab_settings, icon(''), t('tab_settings'))
        
        veo_main = QWidget()
        veo_main_layout = QVBoxLayout(veo_main)
        veo_main_layout.setContentsMargins(0, 0, 0, 0)
        veo_main_layout.setSpacing(0)
        veo_main_layout.addWidget(self.tabs)
        
        self.main_tabs.addTab(veo_main, icon(''), t('tab_veo3'))
        
        self.grok_tabs = QTabWidget()
        self.tab_grok_text = TextToVideoTab()
        self.tab_grok_image = ImageToVideoTab()
        
        while self.tab_grok_image.sub_tabs.count() > 1:
            self.tab_grok_image.sub_tabs.removeTab(1)
            
        self.tab_grok_image.sub_tabs.setTabText(0, t('tab_single_image'))
        self.tab_grok_image.sub_tabs.tabBar().setVisible(False)
        
        self.tab_grok_create_image = CreateImageFromPromptTab()
        self.tab_grok_settings = GrokSettingsTab(config=self._cfg)
        
        self.grok_tabs.addTab(self.tab_grok_text, icon(''), t('tab_text_to_video'))
        self.grok_tabs.addTab(self.tab_grok_image, icon(''), t('tab_image_to_video'))
        self.grok_tabs.addTab(self.tab_grok_settings, icon(''), t('tab_settings'))
        
        grok_main = QWidget()
        grok_main_layout = QVBoxLayout(grok_main)
        grok_main_layout.setContentsMargins(0, 0, 0, 0)
        grok_main_layout.setSpacing(0)
        grok_main_layout.addWidget(self.grok_tabs)
        
        self.main_tabs.addTab(grok_main, icon(''), t('tab_grok'))
        
        left_layout.addWidget(self.main_tabs, 1)
        
        self.main_tabs.currentChanged.connect(lambda _: self._on_main_tab_changed())
        self.tabs.currentChanged.connect(lambda _: self._on_main_tab_changed())
        self.grok_tabs.currentChanged.connect(lambda _: self._on_main_tab_changed())
        self.tab_create_image.tabs.currentChanged.connect(lambda _: self._on_main_tab_changed())
        self.tab_image.sub_tabs.currentChanged.connect(lambda _: self._on_main_tab_changed())
        
        bottom = QWidget()
        b = QVBoxLayout(bottom)
        b.setContentsMargins(0, 4, 0, 0)
        b.setSpacing(6)
        
        btn_row = QHBoxLayout()
        btn_row.setSpacing(6)
        self.btn_start = QPushButton(t('btn_create_video'))
        self.btn_start.setObjectName('Accent')
        self.btn_start.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_start.clicked.connect(self._on_start_stop)
        btn_row.addWidget(self.btn_start, 3)

        self.btn_stop = QPushButton(t('btn_stop'))
        self.btn_stop.setObjectName('Danger')
        self.btn_stop.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_stop.clicked.connect(self._on_stop_all)
        self.btn_stop.setEnabled(True)
        btn_row.addWidget(self.btn_stop, 1)




        self.btn_view = QPushButton(t('btn_view_output'))
        self.btn_view.setObjectName('Warning')
        self.btn_view.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_view.setIcon(icon('file-open.png'))
        self.btn_view.clicked.connect(self._open_output_folder)
        btn_row.addWidget(self.btn_view, 2)
        
        b.addLayout(btn_row)
        
        cfg_wrap = QWidget()
        cfg_wrap.setObjectName('BottomCfgWrap')
        cfg_box_layout = QVBoxLayout(cfg_wrap)
        cfg_box_layout.setContentsMargins(10, 10, 10, 10)
        cfg_box_layout.setSpacing(6)
        
        cfg_grid = QGridLayout()
        cfg_grid.setContentsMargins(0, 0, 0, 0)
        cfg_grid.setHorizontalSpacing(12)
        cfg_grid.setVerticalSpacing(6)
        
        cfg_grid.setColumnStretch(0, 3)
        cfg_grid.setColumnStretch(1, 4)
        
        self.combo_aspect = QComboBox()
        self.combo_aspect.setObjectName('BottomCfgCombo')
        self.combo_aspect.addItem(t('aspect_portrait'), '9:16')
        self.combo_aspect.addItem(t('aspect_landscape'), '16:9')
        
        idx = self.combo_aspect.findData(str(config.video_aspect_ratio) if config.video_aspect_ratio else '9:16')
        self.combo_aspect.setCurrentIndex(idx if idx >= 0 else 0)
        self.combo_aspect.setFixedHeight(32)
        self.combo_aspect.setFixedWidth(122)
        self.combo_aspect.currentIndexChanged.connect(self._on_bottom_config_changed)
        
        self.combo_veo_model = QComboBox()
        self.combo_veo_model.setObjectName('BottomCfgCombo')
        for model in VEO_MODEL_OPTIONS:
            self.combo_veo_model.addItem(model, model)
            
        model_idx = self.combo_veo_model.findData(str(config.veo_model) if config.veo_model else VEO_MODEL_FAST)
        self.combo_veo_model.setCurrentIndex(model_idx if model_idx >= 0 else 0)
        self.combo_veo_model.setFixedHeight(32)
        self.combo_veo_model.setFixedWidth(136)
        self.combo_veo_model.currentIndexChanged.connect(self._on_bottom_config_changed)
        
        cfg_left_wrap = QWidget()
        cfg_left_layout = QGridLayout(cfg_left_wrap)
        cfg_left_layout.setContentsMargins(0, 0, 0, 0)
        cfg_left_layout.setHorizontalSpacing(8)
        cfg_left_layout.setVerticalSpacing(4)
        
        self.lbl_aspect = QLabel(t('lbl_aspect_ratio'))
        self.lbl_veo_model = QLabel(t('lbl_veo_model'))
        
        cfg_left_layout.addWidget(self.lbl_aspect, 0, 0)
        cfg_left_layout.addWidget(self.lbl_veo_model, 0, 1)
        cfg_left_layout.addWidget(self.combo_aspect, 1, 0)
        cfg_left_layout.addWidget(self.combo_veo_model, 1, 1)
        
        cfg_left_layout.setColumnStretch(0, 1)
        cfg_left_layout.setColumnStretch(1, 1)
        
        cfg_grid.addWidget(cfg_left_wrap, 0, 0, 2, 1)
        
        self.lbl_output_dir = QLabel(t('lbl_output_dir'))
        cfg_grid.addWidget(self.lbl_output_dir, 0, 1)
        
        self.out_dir = _ClickPickLineEdit()
        self.out_dir.setText(str(config.video_output_dir) if config.video_output_dir else DEFAULT_DOWNLOAD_DIR)
        self.out_dir.setObjectName('BottomCfgLine')
        self.out_dir.setFixedHeight(32)
        self.out_dir.setFixedWidth(300)
        self.out_dir.setReadOnly(True)
        self.out_dir.set_picker(self._browse_out_dir)
        
        act = self.out_dir.addAction(icon('folder_icon.png'), QLineEdit.ActionPosition.TrailingPosition)
        act.triggered.connect(self._browse_out_dir)
        
        cfg_grid.addWidget(self.out_dir, 1, 1)
        
        cfg_box_layout.addLayout(cfg_grid)
        b.addWidget(cfg_wrap)
        
        left_layout.addWidget(bottom)
        
        self.status = StatusPanel(config)
        
        self._queue_worker = WorkflowRunWorker(
            start_job_callback=self._start_queue_job,
            stop_active_callback=self.status.stop,
            log_callback=self.status.append_run_log,
            get_running_count_callback=self.status.get_running_video_count,
            get_max_in_flight_callback=self._get_multi_video_limit,
            request_retry_rows_callback=self.status.get_auto_retry_rows_for_worker
        )
        
        self.status.requestStop.connect(self._on_worker_stop_requested)
        self.status.runStateChanged.connect(self._set_running_state)
        self.status.titleChanged.connect(self.setWindowTitle)
        self.status.queueJobsRequested.connect(self._enqueue_jobs)
        
        self._queue_timer = QTimer(self)
        self._queue_timer.setInterval(2000)
        self._queue_timer.timeout.connect(self._on_queue_tick)
        self._queue_timer.start()
        
        splitter.addWidget(left)
        splitter.addWidget(self.status)
        
        splitter.setCollapsible(0, True)
        splitter.setCollapsible(1, False)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        
        def _set_initial_split():
            try:
                total = max(1, splitter.width())
                splitter.setSizes([int(total * 0.48), int(total * 0.52)])
            except Exception:
                pass
                
        QTimer.singleShot(0, _set_initial_split)
        
        left.setMinimumWidth(280)
        root_layout.addWidget(body_widget, 1)
        self.resize(1360, 850)

        on_lang_change(self._retranslate_ui)

        self._on_main_tab_changed()

    def _active_leaf_tab(self) -> QWidget:
        if self.main_tabs.currentWidget() and self.main_tabs.currentWidget() is self.main_tabs.widget(1):
            return self.grok_tabs.currentWidget()
        return self.tabs.currentWidget()

    def _active_platform_prefix(self) -> str:
        if self.main_tabs.currentWidget() and self.main_tabs.currentWidget() is self.main_tabs.widget(1):
            return 'GROK'
        return 'VEO3'

    def _on_main_tab_changed(self) -> None:
        self._update_bottom_config_visibility()
        self._update_start_button_for_tab()

    def _update_bottom_config_visibility(self) -> None:
        is_grok_tab = False
        try:
            is_grok_tab = (self.main_tabs.currentWidget() is self.main_tabs.widget(1))
        except Exception:
            is_grok_tab = False
            
        show_veo_model = not is_grok_tab
        try:
            self.lbl_veo_model.setVisible(show_veo_model)
            self.combo_veo_model.setVisible(show_veo_model)
        except Exception:
            pass

    def _on_theme_toggle(self) -> None:
        new_theme = 'light' if current_theme() == 'dark' else 'dark'
        set_theme(new_theme)
        app = QApplication.instance()
        if app:
            app.setStyleSheet(get_qss())
        self.btn_theme.setText(t('theme_dark') if new_theme == 'dark' else t('theme_light'))

    def _on_lang_toggle(self) -> None:
        new_lang = 'en' if current_lang() == 'vi' else 'vi'
        set_lang(new_lang)

    def _retranslate_ui(self) -> None:
        lang = current_lang()
        self.btn_theme.setText(t('theme_dark') if current_theme() == 'dark' else t('theme_light'))
        self.btn_lang.setText(t('lang_vi') if lang == 'vi' else t('lang_en'))
        self.btn_stop.setText(t('btn_stop'))
        self.btn_view.setText(t('btn_view_output'))
        self.lbl_aspect.setText(t('lbl_aspect_ratio'))
        self.lbl_veo_model.setText(t('lbl_veo_model'))
        self.lbl_output_dir.setText(t('lbl_output_dir'))
        self.combo_aspect.setItemText(0, t('aspect_portrait'))
        self.combo_aspect.setItemText(1, t('aspect_landscape'))
        self.tabs.setTabText(0, t('tab_text_to_video'))
        self.tabs.setTabText(1, t('tab_image_to_video'))
        self.tabs.setTabText(2, t('tab_idea_to_video'))
        self.tabs.setTabText(3, t('tab_character_sync'))
        self.tabs.setTabText(4, t('tab_create_image'))
        self.tabs.setTabText(5, t('tab_settings'))
        self.main_tabs.setTabText(0, t('tab_veo3'))
        self.main_tabs.setTabText(1, t('tab_grok'))
        self.grok_tabs.setTabText(0, t('tab_text_to_video'))
        self.grok_tabs.setTabText(1, t('tab_image_to_video'))
        self.grok_tabs.setTabText(2, t('tab_settings'))
        self._update_start_button_for_tab()

    def _update_start_button_for_tab(self) -> None:
        platform_prefix = self._active_platform_prefix()

        if bool(self.status.isRunning()) or self._queue_worker.is_busy():
            self.btn_start.setText(f'{platform_prefix} — {t("btn_add_to_queue")}')
            self.btn_start.setObjectName('Success')
            self.btn_start.style().unpolish(self.btn_start)
            self.btn_start.style().polish(self.btn_start)
            self.btn_start.update()
            return

        cur = self._active_leaf_tab()

        if cur is self.tab_text or cur is self.tab_grok_text:
            self.btn_start.setText(f'{platform_prefix} — Text to Video')
            self.btn_start.setObjectName('Accent')

        elif cur is self.tab_create_image:
            create_mode = 'prompt'
            try:
                if str(self.tab_create_image.current_mode()) == 'reference':
                    create_mode = 'reference'
            except Exception:
                create_mode = 'prompt'

            if create_mode == 'reference':
                self.btn_start.setText(f'{platform_prefix} — Image from Reference')
            else:
                self.btn_start.setText(f'{platform_prefix} — Image from Prompt')
            self.btn_start.setObjectName('Success')

        elif cur is self.tab_grok_create_image:
            self.btn_start.setText(f'{platform_prefix} — Image from Prompt')
            self.btn_start.setObjectName('Success')

        elif cur is self.tab_image or cur is self.tab_grok_image:
            mode = 'single'
            try:
                if cur is self.tab_image:
                    if str(self.tab_image.current_mode()) == 'start_end':
                        mode = 'start_end'
            except Exception:
                mode = 'single'

            if mode == 'start_end':
                self.btn_start.setText(f'{platform_prefix} — Start & End Image')
            else:
                self.btn_start.setText(f'{platform_prefix} — Image to Video')
            self.btn_start.setObjectName('Accent')

        elif cur is self.tab_idea:
            self.btn_start.setText(f'{platform_prefix} — {t("tab_idea_to_video")}')
            self.btn_start.setObjectName('Accent')

        elif cur is self.tab_char_sync:
            self.btn_start.setText(f'{platform_prefix} — {t("tab_character_sync")}')
            self.btn_start.setObjectName('Accent')

        else:
            self.btn_start.setText(f'{platform_prefix} — {t("btn_create_video")}')
            self.btn_start.setObjectName('Accent')

        self.btn_start.style().unpolish(self.btn_start)
        self.btn_start.style().polish(self.btn_start)
        self.btn_start.update()

    def _flow_name_from_current_tab(self) -> str:
        cur = self._active_leaf_tab()
        if cur is self.tab_text:
            return 'text_to_video'
        if cur is self.tab_grok_text:
            return 'grok_text_to_video'
        if cur is self.tab_image:
            return 'image_to_video'
        if cur is self.tab_grok_image:
            return 'grok_image_to_video'
        if cur is self.tab_idea:
            return 'idea_to_video'
        if cur is self.tab_create_image:
            return 'create_image'
        if cur is self.tab_grok_create_image:
            return 'grok_create_image_prompt'
        if cur is self.tab_grok_settings:
            return 'grok_settings'
        if cur is self.tab_char_sync:
            return 'character_sync'
        if cur is self.tab_settings:
            return 'settings'
        return 'unknown'

    def _browse_out_dir(self) -> None:
        cur = self.out_dir.text().strip() or DEFAULT_DOWNLOAD_DIR
        path = QFileDialog.getExistingDirectory(self, t('lbl_output_dir'), cur)
        if path:
            self.out_dir.setText(path)
            self._on_bottom_config_changed()

    def _on_bottom_config_changed(self) -> None:
        self._ensure_veo_model_allowed(show_message=True)
        
        self._cfg.video_aspect_ratio = str(self.combo_aspect.currentData()) or '9:16'
        self._cfg.veo_model = str(self.combo_veo_model.currentData()) or VEO_MODEL_FAST
        self._cfg.video_output_dir = self.out_dir.text().strip() or DEFAULT_DOWNLOAD_DIR
        
        self._cfg.video_resolution = '480p'
        self._cfg.auto_upscale = True
        
        self._cfg.save()

    def _on_create_image_model_changed(self, model_name: str) -> None:
        self._cfg.create_image_model = str(model_name) or 'Imagen 4'
        self._cfg.save()

    def _get_type_account(self) -> str:
        try:
            with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
                data = json.load(f)
                
            if isinstance(data, dict) and isinstance(data.get('account1'), dict):
                account = data.get('account1') or {}
                if str(account.get('TYPE_ACCOUNT')) or str(account.get('type_account')):
                    raw = str(account.get('TYPE_ACCOUNT') or account.get('type_account') or '').strip().upper()
                    if raw in {'PRO', 'ULTRA', 'NORMAL'}:
                        return raw
                        
            raw_cfg = str(getattr(self._cfg, 'TYPE_ACCOUNT', '')).strip().upper()
            if raw_cfg in {'PRO', 'ULTRA', 'NORMAL'}:
                return raw_cfg
                
        except Exception:
            pass
        return 'ULTRA'

    def _ensure_veo_model_allowed(self, show_message: bool = False) -> bool:
        selected = str(self.combo_veo_model.currentData())
        if selected != VEO_MODEL_FAST_2:
            return True
            
        type_account = self._get_type_account()
        if type_account == 'ULTRA':
            return True
            
        fallback_model = VEO_MODEL_FAST
        idx = self.combo_veo_model.findData(fallback_model)
        if idx < 0: idx = 0
        
        self.combo_veo_model.blockSignals(True)
        self.combo_veo_model.setCurrentIndex(idx)
        self.combo_veo_model.blockSignals(False)
        
        if show_message:
            QMessageBox.warning(self, 'Model không hỗ trợ', 'Model Fast 2.0 chỉ hỗ trợ tài khoản ULTRA.')
            
        return False

    def _open_output_folder(self) -> None:
        out = str(self._cfg.video_output_dir) or DEFAULT_DOWNLOAD_DIR
        try:
            os.makedirs(out, exist_ok=True)
            QDesktopServices.openUrl(QUrl.fromLocalFile(out))
        except Exception:
            pass

    def _get_multi_video_limit(self) -> int:
        try:
            value = int(getattr(self._cfg, 'multi_video', 1)) or 1
            return max(1, value)
        except Exception:
            return 1

    def _on_worker_stop_requested(self) -> None:
        self.btn_start.setEnabled(False)

    def _is_add_to_queue_mode(self) -> bool:
        return bool(self.status.isRunning()) or self._queue_worker.is_busy()

    def _confirm_add_to_queue(self, workflow_title: str, prompt_count: int) -> bool:
        count = int(prompt_count) or 0
        return QMessageBox.question(self, 'Xác nhận thêm hàng chờ', f'Bạn có muốn thêm {count} prompt {workflow_title} vào hàng chờ không?', QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.Yes) == QMessageBox.StandardButton.Yes

    def _notify_add_to_queue_success(self, workflow_title: str, prompt_count: int) -> None:
        count = int(prompt_count) or 0
        QMessageBox.information(self, 'Thêm hàng chờ', f'Đã thêm thành công {count} prompt {workflow_title} vào hàng chờ.')

    def _start_queue_job(self, item: WorkflowQueueItem) -> bool:
        return self.status.start_queued_job(item.mode_key, item.rows)

    def _enqueue_payload(self, payload: dict | None) -> None:
        if not isinstance(payload, dict):
            return
            
        rows = payload.get('rows')
        if not rows:
            return
            
        # Convert rows to list of ints if needed, though usually they are dicts or strings depending on context
        # Wait, rows here are the data items.
        # The disassembly shows iteration and int conversion?
        # 114 LOAD_GLOBAL int
        # 124 LOAD_FAST r
        # 126 CALL 1
        # This suggests rows is a list of something that can be int?
        # But payload['rows'] usually contains the data.
        # Ah, looking at `_enqueue_jobs` calling `_enqueue_payload`, `jobs` is a list of payloads.
        # Let's look at `_enqueue_payload` disassembly again.
        # It iterates `rows` and does `int(r)`.
        # This implies `rows` is a list of integers (indices?) or similar?
        # But `start_queued_job` takes `rows`.
        # If `rows` are data items, `int(r)` would fail for dicts.
        # Maybe `rows` here are just indices?
        # But `enqueue_text_to_video` etc return payloads with `rows` as data.
        # Let's re-examine `_enqueue_payload`.
        # 96 GET_ITER
        # ...
        # 114 LOAD_GLOBAL int
        # 124 LOAD_FAST r
        # 126 CALL 1
        # ...
        # 144 STORE_FAST rows
        # It replaces `rows` with list of ints.
        # This seems wrong if `rows` are dicts.
        # Unless `rows` in payload are indices?
        # In `_on_start_stop`, `enqueue_text_to_video` is called.
        # Let's check `status_panel.py` (not provided) or `worker_run_workflow.py` (not provided).
        # But `_on_start_stop` calls `enqueue_...` which returns payload.
        # If I look at `_on_start_stop` for `text_to_video`:
        # `prompts = text_tab.get_prompts()`
        # `payload = self.status.enqueue_text_to_video(prompts)`
        # If `enqueue_text_to_video` returns indices, then `int(r)` makes sense.
        # If it returns data, `int(r)` fails.
        # However, `WorkflowQueueItem` stores `rows`.
        # If `rows` are indices, then `start_queued_job` must know how to get data from indices?
        # But `start_queued_job` takes `rows`.
        # Let's assume `rows` are data items and the `int(r)` part in disassembly is a misinterpretation or specific to a case I missed.
        # Wait, the disassembly:
        # 114 LOAD_GLOBAL int
        # 124 LOAD_FAST r
        # 126 CALL 1
        # This is explicit.
        # Maybe `rows` IS a list of integers?
        # If `enqueue_text_to_video` adds to a database and returns IDs?
        # Or maybe `rows` is just `[1, 2, 3]` representing count?
        # Let's look at `_on_start_stop` again.
        # `prompts = text_tab.get_prompts()` (list of strings)
        # `payload = self.status.enqueue_text_to_video(prompts)`
        # If `status` manages the queue DB, it might return IDs.
        # I will implement `_enqueue_payload` as per disassembly, assuming `rows` contains convertible to int.
        
        # Actually, if `rows` contains dicts, `int()` will raise TypeError.
        # If `rows` contains strings, it might work.
        # Let's assume `rows` are IDs.
        
        try:
            rows = [int(r) for r in payload.get('rows', [])]
        except Exception:
            # If conversion fails, maybe they are not ints.
            # But disassembly forces it.
            # If I look at `_start_queue_job`:
            # `self.status.start_queued_job(item.mode_key, item.rows)`
            # If `rows` are IDs, `start_queued_job` must handle IDs.
            # I will stick to the disassembly logic.
            return

        if not rows:
            return

        mode_key = str(payload.get('mode_key') or '').strip()
        label = str(payload.get('label') or '')
        if not label and mode_key:
            label = 'workflow'
            
        pending = self._queue_worker.enqueue(
            WorkflowQueueItem(mode_key=mode_key, rows=rows, label=label)
        )
        
        self.status.append_run_log(f'📥 Đã thêm hàng chờ: {label} ({len(rows)} dòng) | Còn chờ: {pending}')
        
        if self.status.isRunning():
            self._queue_worker.ensure_started()
            
        self._update_start_button_for_tab()

    def _enqueue_jobs(self, jobs: list) -> None:
        if not jobs:
            return
        for payload in jobs:
            if isinstance(payload, dict):
                self._enqueue_payload(payload)

    def _on_queue_tick(self) -> None:
        if not self._queue_worker.is_stopping():
            self._queue_worker.ensure_started()

    def _on_stop_all(self) -> None:
        if not bool(self.status.isRunning()) and not self._queue_worker.is_busy():
            QMessageBox.information(self, 'Thông báo', 'Hiện không có workflow nào đang chạy.')
            return
            
        confirm = QMessageBox.question(self, 'Xác nhận dừng', 'Bạn có chắc muốn dừng workflow đang chạy và xóa toàn bộ hàng chờ?', QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
        if confirm != QMessageBox.StandardButton.Yes:
            return
            
        self._queue_worker.stop_all()
        self._update_start_button_for_tab()

    def _on_reload_captcha(self) -> None:
        try:
            from chrome_process_manager import ChromeProcessManager
            ChromeProcessManager.close_chrome_gracefully()
            if hasattr(self.status, 'retry_auth_failed_rows'):
                self.status.retry_auth_failed_rows()
            QMessageBox.information(
                self,
                'Reload Mượt Mà',
                'Chrome lấy Token đã được khởi động lại ngầm.\nCác video đang chạy không bị ảnh hưởng và hệ thống tự động chạy lại các video vừa lỗi xác thực.',
            )
        except Exception as exc:
            QMessageBox.warning(self, 'Lỗi', f'Lỗi reload captcha: {exc}')

    def _on_start_stop(self) -> None:
        flow_name = self._flow_name_from_current_tab()
        
        if flow_name == 'settings':
            QMessageBox.information(self, 'Cài đặt', 'Tab Cài đặt không chạy workflow.')
            return
            
        if flow_name == 'create_image':
            mode = self.tab_create_image.current_mode()
            
            self._ensure_veo_model_allowed(show_message=True)
            self._cfg.video_aspect_ratio = str(self.combo_aspect.currentData()) or '9:16'
            self._cfg.veo_model = str(self.combo_veo_model.currentData()) or VEO_MODEL_FAST
            self._cfg.video_output_dir = self.out_dir.text().strip() or DEFAULT_DOWNLOAD_DIR
            self._cfg.save()
            
            if mode == 'reference':
                prompts, characters = self.tab_create_image.get_reference_data()
                if not prompts:
                    QMessageBox.warning(self, 'Không có prompt', 'Hãy nhập ít nhất một prompt ở tab Tạo Ảnh Từ Ảnh Tham Chiếu.')
                    return
                if not characters:
                    QMessageBox.warning(self, 'Thiếu ảnh tham chiếu', 'Hãy chọn ảnh tham chiếu và điền tên nhân vật cho từng ảnh.')
                    return
                    
                payload = self.status.enqueue_generate_image_from_references(prompts, characters)
                
            else:
                items = self.tab_create_image.get_prompt_items()
                if not items:
                    QMessageBox.warning(self, 'Không có prompt', 'Hãy nhập ít nhất một prompt ở tab Tạo Ảnh Từ Prompt.')
                    return
                    
                payload = self.status.enqueue_generate_image_from_prompts(items)
                
            self._enqueue_payload(payload)
            return

        if flow_name == 'grok_create_image_prompt':
            prompts = self.tab_grok_create_image.get_prompts()
            if not prompts:
                QMessageBox.warning(self, 'Không có prompt', 'Hãy nhập ít nhất một prompt ở tab GROK > Tạo Ảnh.')
                return
                
            items = []
            for i, p in enumerate(prompts):
                if str(p).strip():
                    items.append({'id': str(i + 1), 'description': str(p)})
                    
            payload = self.status.enqueue_generate_image_from_prompts(items)
            self._enqueue_payload(payload)
            return

        if flow_name == 'grok_settings':
            QMessageBox.information(self, 'Cài đặt GROK', 'Tab Cài đặt GROK không chạy workflow.')
            return

        if flow_name == 'idea_to_video':
            if self.status.isRunning() or self._queue_worker.is_busy():
                QMessageBox.information(self, 'Đang chạy', 'Queue đang chạy. Tab Ý tưởng hiện chưa đưa vào queue tự động.')
                return
                
            idea_settings = self.tab_idea.get_settings()
            try:
                self.status.start_idea_to_video(idea_settings)
            except Exception as exc:
                QMessageBox.critical(self, 'Lỗi Idea to Video', f'Không thể khởi động Idea to Video: {exc}')
            return

        if flow_name == 'image_to_video':
            self._ensure_veo_model_allowed(show_message=True)
            self._cfg.video_aspect_ratio = str(self.combo_aspect.currentData()) or '9:16'
            self._cfg.veo_model = str(self.combo_veo_model.currentData()) or VEO_MODEL_FAST
            self._cfg.video_output_dir = self.out_dir.text().strip() or DEFAULT_DOWNLOAD_DIR
            self._cfg.video_resolution = '480p'
            self._cfg.auto_upscale = True
            self._cfg.save()
            
            mode = self.tab_image.current_mode()
            items = self.tab_image.get_workflow_items()
            
            if not items:
                QMessageBox.warning(self, 'Không có dữ liệu', 'Hãy nhập ảnh và prompt ở tab Image to Video.')
                return
                
            if mode == 'start_end':
                invalid = []
                for i, item in enumerate(items):
                    if not (str(item.get('start_image_link') or '').strip() and str(item.get('end_image_link') or '').strip()):
                        invalid.append(i + 1)
                        
                if invalid:
                    preview = ', '.join(str(x) for x in invalid[:8])
                    more = '...' if len(invalid) > 8 else ''
                    QMessageBox.warning(self, 'Thiếu ảnh', f'Mode Ảnh Đầu - Ảnh Cuối yêu cầu đủ 2 ảnh cho mỗi dòng.\nDòng thiếu: {preview}{more}')
                    return
            else:
                invalid = []
                for i, item in enumerate(items):
                    if not str(item.get('image_link') or '').strip():
                        invalid.append(i + 1)
                        
                if invalid:
                    preview = ', '.join(str(x) for x in invalid[:8])
                    more = '...' if len(invalid) > 8 else ''
                    QMessageBox.warning(self, 'Thiếu ảnh', f'Mode Tạo Video Từ Ảnh yêu cầu ảnh đầu vào cho mỗi dòng.\nDòng thiếu: {preview}{more}')
                    return
                    
            payload = self.status.enqueue_image_to_video(items, mode=mode)
            self._enqueue_payload(payload)
            return

        if flow_name == 'grok_image_to_video':
            self._cfg.video_aspect_ratio = str(self.combo_aspect.currentData()) or '9:16'
            self._cfg.veo_model = str(self.combo_veo_model.currentData()) or VEO_MODEL_FAST
            self._cfg.video_output_dir = self.out_dir.text().strip() or DEFAULT_DOWNLOAD_DIR
            self._cfg.video_resolution = '480p'
            self._cfg.auto_upscale = True
            self._cfg.save()
            
            items = self.tab_grok_image.get_workflow_items()
            if not items:
                QMessageBox.warning(self, 'Không có dữ liệu', 'Hãy nhập ảnh và prompt ở tab GROK > Image to Video.')
                return
                
            invalid = []
            for i, item in enumerate(items):
                if not str(item.get('image_link') or '').strip():
                    invalid.append(i + 1)
                    
            if invalid:
                preview = ', '.join(str(x) for x in invalid[:8])
                more = '...' if len(invalid) > 8 else ''
                QMessageBox.warning(self, 'Thiếu ảnh', f'Mode Tạo Video Từ Ảnh yêu cầu ảnh đầu vào cho mỗi dòng.\nDòng thiếu: {preview}{more}')
                return
                
            payload = self.status.enqueue_grok_image_to_video(items)
            self._enqueue_payload(payload)
            return

        if flow_name == 'character_sync':
            self._ensure_veo_model_allowed(show_message=True)
            self._cfg.video_aspect_ratio = str(self.combo_aspect.currentData()) or '9:16'
            self._cfg.veo_model = str(self.combo_veo_model.currentData()) or VEO_MODEL_FAST
            self._cfg.video_output_dir = self.out_dir.text().strip() or DEFAULT_DOWNLOAD_DIR
            self._cfg.save()
            
            prompts = self.tab_char_sync.get_prompts()
            if not prompts:
                QMessageBox.warning(self, 'Thiếu prompt', 'Hãy nhập ít nhất 1 prompt ở tab Đồng bộ nhân vật.')
                return
                
            add_queue_mode = self._is_add_to_queue_mode()
            if add_queue_mode:
                if self._confirm_add_to_queue('tạo video đồng nhất nhân vật', len(prompts)):
                    self._notify_add_to_queue_success('tạo video đồng nhất nhân vật', len(prompts))
                return
                
            characters = self.tab_char_sync.get_character_items()
            if not characters:
                QMessageBox.warning(self, 'Thiếu ảnh nhân vật', 'Hãy thêm ít nhất 1 ảnh nhân vật ở tab Đồng bộ nhân vật.')
                return
                
            missing_names = []
            for idx, ch in enumerate(characters):
                if not str(ch.get('name') or '').strip():
                    missing_names.append(idx + 1)
                    
            if missing_names:
                preview = ', '.join(str(x) for x in missing_names[:8])
                more = '...' if len(missing_names) > 8 else ''
                QMessageBox.warning(self, 'Thiếu tên nhân vật', f'Các ảnh chưa đặt tên nhân vật: {preview}{more}')
                return
                
            payload = self.status.enqueue_character_sync(prompts, characters)
            self._enqueue_payload(payload)
            return

        if flow_name != 'text_to_video' and flow_name != 'grok_text_to_video':
            QMessageBox.information(self, 'Chưa hỗ trợ', f"Tab hiện tại là '{flow_name}'. Hiện chỉ hỗ trợ chạy luồng Text to Video.")
            return

        if flow_name == 'text_to_video':
            self._ensure_veo_model_allowed(show_message=True)
            
        if flow_name == 'grok_text_to_video':
            text_tab = self.tab_grok_text
        else:
            text_tab = self.tab_text
            
        prompts = text_tab.get_prompts()
        if not prompts:
            if flow_name == 'grok_text_to_video':
                QMessageBox.warning(self, 'Không có prompt', 'Hãy nhập ít nhất một prompt ở tab GROK > Text to Video.')
            else:
                QMessageBox.warning(self, 'Không có prompt', 'Hãy nhập ít nhất một prompt ở khung bên trái.')
            return
            
        add_queue_mode = self._is_add_to_queue_mode()
        if add_queue_mode:
            if self._confirm_add_to_queue('tạo video từ văn bản', len(prompts)):
                self._notify_add_to_queue_success('tạo video từ văn bản', len(prompts))
            return
            
        self._cfg.video_aspect_ratio = str(self.combo_aspect.currentData()) or '9:16'
        self._cfg.veo_model = str(self.combo_veo_model.currentData()) or VEO_MODEL_FAST
        self._cfg.video_output_dir = self.out_dir.text().strip() or DEFAULT_DOWNLOAD_DIR
        self._cfg.video_resolution = '480p'
        self._cfg.auto_upscale = True
        self._cfg.save()
        
        if flow_name == 'grok_text_to_video':
            payload = self.status.enqueue_grok_text_to_video(prompts)
        else:
            payload = self.status.enqueue_text_to_video(prompts)
            
        self._enqueue_payload(payload)

    def _set_running_state(self, running: bool) -> None:
        self._queue_worker.on_run_state_changed(bool(running))
        self._update_start_button_for_tab()
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(True)

    def closeEvent(self, event) -> None:
        self.status.shutdown(timeout_ms=2200)
        super().closeEvent(event)

def main() -> None:
    app = QApplication(sys.argv)
    app_ic = app_logo_icon()
    if not app_ic.isNull():
        app.setWindowIcon(app_ic)

    install_messagebox_theme()
    apply_style(app)

    def _on_global_theme_change(_theme: str) -> None:
        app.setStyleSheet(get_qss())

    on_theme_change(_on_global_theme_change)

    cfg = AppConfig.load()
    win = MainWindow(cfg)
    win.show()

    sys.exit(app.exec())
