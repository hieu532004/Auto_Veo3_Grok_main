import asyncio
import base64
import importlib
import json
import mimetypes
import re
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import requests

_qtcore = None
_qtwidgets = None
try:
    _qtcore = importlib.import_module("PySide6.QtCore")
    _qtwidgets = importlib.import_module("PySide6.QtWidgets")
except Exception:
    _qtcore = importlib.import_module("PyQt6.QtCore")
    _qtwidgets = importlib.import_module("PyQt6.QtWidgets")

QThread = _qtcore.QThread
Signal = getattr(_qtcore, "Signal", None) or getattr(_qtcore, "pyqtSignal")
QMessageBox = _qtwidgets.QMessageBox

import API_sync_chactacter as sync_api

from A_workflow_get_token import TokenCollector
from token_pool import TokenPool
from chrome_process_manager import ChromeProcessManager
from settings_manager import SettingsManager, WORKFLOWS_DIR
from workflow_run_control import get_running_video_count, get_max_in_flight
from watermark_remover import remove_watermark, apply_download_resolution

TOKEN_CHROME_HIDE_WINDOW = True


class CharacterSyncWorkflow(QThread):
    log_message = Signal(str)
    video_updated = Signal(dict)
    automation_complete = Signal()
    video_folder_updated = Signal(str)

    def __init__(self, project_name=None, project_data=None, parent=None):
        super().__init__(parent)
        self.project_name = project_name or (project_data or {}).get("project_name", "default_project")
        self.project_data = project_data or {}
        self.STOP = 0
        self._upload_executor = ThreadPoolExecutor(max_workers=5, thread_name_prefix="sync-char-upload")
        self._download_executor = ThreadPoolExecutor(max_workers=5, thread_name_prefix="sync-dl")
        self._scene_status: dict[str, dict] = {}
        self._scene_to_prompt: dict[str, dict] = {}
        self._scene_next_check_at: dict[str, float] = {}
        self._scene_status_change_ts: dict[str, float] = {}
        self._status_log_ts = 0.0
        self._pending_log_interval = 15.0
        self._status_poll_fail_streak = 0
        self._last_status_change_ts = 0.0
        self._all_prompts_submitted = False
        self._complete_wait_start_ts = 0.0
        self._complete_wait_timeout = 0
        self._upload_failed = False
        self._in_flight_block_start_ts = 0
        self._active_prompt_ids = set()
        self._inline_retry_queue = []  # ✅ Queue cho inline retry ALL errors
        self._inline_retry_counts = {}  # ✅ {prompt_id: count}

    def _resolve_int_config(self, config, key, default_value):
        try:
            return int(config.get(key, default_value))
        except Exception:
            return default_value

    def _resolve_worker_max_in_flight(self, fallback_value):
        return max(1, int(get_max_in_flight(default_value=int(fallback_value or 1))))

    def _check_in_flight_block(self):
        if self._in_flight_block_start_ts <= 0:
            self._in_flight_block_start_ts = time.time()
            return True
        if time.time() - self._in_flight_block_start_ts > 900:  # 15 mins format
            self._log("⚠️ Đã chờ queue trống quá 15 phút, có thể bị kẹt process_status, tiếp tục workflow...")
            self._in_flight_block_start_ts = 0
            return True
        return True

    def run(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._run_workflow())
            # ✅ Inline retry ALL errors đã được xử lý bên trong _run_workflow
            
        except Exception as exc:
            self._log(f"❌ Lỗi workflow sync character: {exc}")
            self._log(traceback.format_exc()[:1200])
        finally:
            try:
                loop.close()
            except Exception:
                pass
            try:
                if self._upload_executor is not None:
                    self._upload_executor.shutdown(wait=False)
                    self._upload_executor = None
            except Exception:
                pass
            # Chrome giữ mở để tái sử dụng
            self._log("✅ Workflow kết thúc (Chrome vẫn mở để tái sử dụng)")
            self.automation_complete.emit()

    def _collect_audio_filtered_failures(self):
        """Lấy danh sách video bị lỗi AUDIO_FILTERED (error code 3) từ state.json để auto-retry."""
        try:
            state_data = self._load_state_json()
            failed_items = []
            prompts = state_data.get("prompts", {})
            for prompt_key, prompt_data in prompts.items():
                if not isinstance(prompt_data, dict):
                    continue
                prompt_id = prompt_data.get("id")
                prompt_text = prompt_data.get("prompt", "")
                statuses = prompt_data.get("statuses", [])
                scene_ids = prompt_data.get("scene_ids", [])
                errors = prompt_data.get("errors", [])
                error_messages = prompt_data.get("error_messages", [])
                for idx, status in enumerate(statuses):
                    if status != "FAILED":
                        continue
                    err_code = str(errors[idx] if idx < len(errors) else "").strip()
                    err_msg = str(error_messages[idx] if idx < len(error_messages) else "").strip().upper()
                    if err_code == "3" or "AUDIO_FILTERED" in err_msg or "AUDIO_FILTERED" in err_code.upper():
                        scene_id = scene_ids[idx] if idx < len(scene_ids) else str(uuid.uuid4())
                        failed_items.append((prompt_id, prompt_text, scene_id, idx))
            return failed_items
        except Exception as e:
            self._log(f"⚠️ Lỗi đọc state.json cho auto-retry: {e}")
            return []

    def stop(self):
        self.STOP = 1

    def _should_stop(self):
        return bool(self.STOP)

    def _log(self, message):
        try:
            self.log_message.emit(str(message or ""))
        except Exception:
            pass

    async def _sleep_with_stop(self, seconds, step=0.2):
        end_ts = time.time() + max(0.0, float(seconds or 0.0))
        while time.time() < end_ts:
            if self._should_stop():
                return False
            await asyncio.sleep(min(step, max(0.01, end_ts - time.time())))
        return not self._should_stop()

    async def _run_workflow(self):
        if self._should_stop():
            return

        # ✅ Nếu đang retry, không xóa data cũ
        retry_filter = getattr(self, '_retry_prompt_ids_filter', None)
        if not retry_filter:
            self._cleanup_workflow_data()
        
        prompts = self._load_text_prompts()
        if not prompts:
            self._log("❌ Không có prompt cho sync character")
            return

        character_profiles = self._load_character_profiles()
        if not character_profiles:
            self._log("❌ Không có ảnh nhân vật hoặc tên nhân vật")
            return

        plans, overflow_items = self._build_prompt_plans(prompts, character_profiles)
        
        # ✅ Nếu đang retry, chỉ giữ plans của prompts bị lỗi
        if retry_filter:
            plans = [p for p in plans if str(p.get("id", "")) in retry_filter]
            self._log(f"🔁 Retry mode: chỉ xử lý {len(plans)} prompt bị lỗi AUDIO_FILTERED")
            overflow_items = []
        
        self._active_prompt_ids = {str((p or {}).get("id") or "").strip() for p in (plans or []) if str((p or {}).get("id") or "").strip()}
        if overflow_items:
            ok = self._ask_continue_with_overflow(overflow_items)
            if not ok:
                self._log("⛔ Người dùng hủy do prompt có >3 nhân vật")
                return

        auth = self._load_auth_config()
        if not auth:
            self._log("❌ Thiếu sessionId/projectId/access_token trong config")
            return

        import auth_helper
        self._log("🛂 Đang kiểm tra token OAuth...")
        new_token = auth_helper.get_valid_access_token(auth.get("cookie", ""), auth.get("projectId", ""))
        if new_token and new_token != auth.get("access_token"):
            self._log("✅ Token OAuth đã được làm mới tự động trước khi chạy workflow")
            auth["access_token"] = new_token

        config = SettingsManager.load_config()
        wait_gen_video = self._resolve_int_config(config, "WAIT_BETWEEN_PROMPTS", self._resolve_int_config(config, "WAIT_GEN_VIDEO", 12))
        token_retry = self._resolve_int_config(config, "TOKEN_RETRY", 3)
        token_retry_delay = self._resolve_int_config(config, "TOKEN_RETRY_DELAY", 2)
        get_token_timeout = max(150, self._resolve_int_config(config, "TOKEN_TIMEOUT", 60))
        self._complete_wait_timeout = self._resolve_int_config(config, "WAIT_COMPLETE_TIMEOUT", 0)
        output_count = max(1, self._resolve_int_config(config, "OUTPUT_COUNT", 1))

        all_profiles: dict[str, dict] = {}
        for plan in plans:
            for prof in plan.get("profiles", []):
                all_profiles[str(prof["name_key"])] = prof

        self._log(f"⬆️ Upload ảnh nhân vật dùng chung: {len(all_profiles)} ảnh (tối đa 5 thread)")
        
        # ✅ Khởi tạo Chrome Collector TRƯỚC KHI upload để dùng browser tránh 401
        profile_name = self.project_data.get("veo_profile") or SettingsManager.load_settings().get("current_profile")
        project_link = auth.get("URL_GEN_TOKEN") or self.project_data.get("project_link") or "https://labs.google/fx/vi/tools/flow"
        chrome_userdata_root = auth.get("folder_user_data_get_token") or SettingsManager.create_chrome_userdata_folder(profile_name)

        collector = None
        try:
            collector = await asyncio.wait_for(
                self._init_token_collector(
                    project_link, chrome_userdata_root, profile_name,
                    self._resolve_int_config(config, "CLEAR_DATA_WAIT", 2), 40, get_token_timeout,
                ),
                timeout=180,
            )
            # Khởi động Chrome (tương đương context manager open)
            if hasattr(collector, '__aenter__'):
                await collector.__aenter__()
            self._collector_ref = collector
            
            # Làm mới token ngay
            try:
                if hasattr(collector, 'refresh_auth_from_browser'):
                    fresh_token, fresh_cookie = await collector.refresh_auth_from_browser(auth.get("projectId", ""))
                    if fresh_token:
                        auth["access_token"] = fresh_token
                        self._log("✅ Đã lấy access_token mới từ Chrome browser")
                    if fresh_cookie:
                        auth["cookie"] = fresh_cookie
            except Exception as e:
                self._log(f"⚠️ Không refresh được auth từ Chrome: {e}")
        except Exception as exc:
            self._log(f"❌ Không khởi tạo được TokenCollector: {exc}")
            return
            
        async def _close_collector():
            try:
                if collector and hasattr(collector, '__aexit__'):
                    await collector.__aexit__(None, None, None)
            except: pass

        media_cache = await self._upload_all_character_media(
            list(all_profiles.values()),
            auth["sessionId"],
            auth["access_token"],
            auth.get("cookie"),
        )
        if self._should_stop():
            await _close_collector()
            return

        for key, profile in all_profiles.items():
            if key not in media_cache:
                prof_name = profile.get('name', key)
                self._log(f"⚠️ Upload ảnh nhân vật thất bại: {prof_name} — các prompt dùng nhân vật này sẽ bị bỏ qua")
        
        # ✅ Nếu KHÔNG có ảnh nào upload thành công → sẽ được xử lý ở vòng lặp valid_plans bên dưới
        
        # ✅ Lọc bỏ plans mà TẤT CẢ nhân vật đều upload thất bại
        valid_plans = []
        for plan in plans:
            plan_profiles = plan.get("profiles", [])
            valid_refs = [p for p in plan_profiles if str(p.get("name_key", "")) in media_cache]
            if valid_refs:
                valid_plans.append(plan)
            else:
                pid = plan.get("id", "?")
                self._log(f"⚠️ Bỏ qua prompt {pid}: tất cả nhân vật của prompt này upload thất bại")
                self._mark_prompt_failed(str(pid), str(plan.get("prompt", "")), "CHAR_UPLOAD_FAILED", "Upload ảnh nhân vật thất bại")
        plans = valid_plans
        if not plans:
            self._log("❌ Không còn prompt nào có nhân vật upload thành công, dừng workflow")
            await _close_collector()
            return

        if media_cache:
            self._log("⏳ Đang đợi hệ thống Google xử lý ảnh tham chiếu (12 giây)...")
            await self._sleep_with_stop(12)

        status_task = asyncio.create_task(
            self._status_poll_loop(auth["access_token"], auth.get("cookie"), auth_dict=auth)
        )



        retry_token_counter = 0
        timeout_streak = 0
        max_retries = self._resolve_int_config(config, "RETRY_WITH_ERROR", 3)
        wait_resend_video = self._resolve_int_config(config, "WAIT_RESEND_VIDEO", 15)
        max_in_flight = self._resolve_worker_max_in_flight(self._resolve_int_config(config, "MULTI_VIDEO", 1))

        async def _process_one_prompt(plan_item, plan_index):
            """Xử lý 1 prompt sync character song song."""
            nonlocal retry_token_counter, timeout_streak

            prompt_id = str(plan_item["id"])
            prompt_text = str(plan_item["prompt"])
            profiles = list(plan_item.get("profiles") or [])[:3]
            if not profiles:
                self._mark_prompt_failed(prompt_id, prompt_text, "NO_CHARACTER", "Prompt không nhắc đến nhân vật nào")
                return

            ref_media_ids = []
            for prof in profiles:
                mid = media_cache.get(str(prof["name_key"]), "")
                if mid:
                    ref_media_ids.append(mid)
            if not ref_media_ids:
                self._mark_prompt_failed(prompt_id, prompt_text, "UPLOAD", "Không có mediaId ảnh nhân vật")
                return

            # ✅ Đưa ngay vào state.json để đếm luồng chính xác, tránh tạo ồ ạt
            for i in range(output_count):
                self._update_state_entry(prompt_id, prompt_text, "", i, "ACTIVE")

            self.video_updated.emit({
                "prompt_idx": f"{prompt_id}_1", "status": "ACTIVE",
                "scene_id": "", "prompt": prompt_text, "_prompt_id": prompt_id,
            })

            for retry_attempt in range(20):
                if self._should_stop():
                    return

                # ── Lấy token ──
                token = None
                token_project_id = ""
                for attempt in range(token_retry):
                    if self._should_stop():
                        return
                    try:
                        retry_token_counter += 1
                        clear_storage = False
                        clear_every = self._resolve_int_config(config, "CLEAR_DATA", 0)
                        if clear_every > 0 and (retry_token_counter % clear_every == 0):
                            clear_storage = True
                        token_result = await asyncio.wait_for(
                            collector.get_token(clear_storage=clear_storage),
                            timeout=get_token_timeout,
                        )
                        # Token pool trả về (token, project_id) hoặc string
                        if isinstance(token_result, tuple) and len(token_result) == 2:
                            token, token_project_id = token_result
                        elif token_result:
                            token = token_result
                            token_project_id = ""
                        if token:
                            self._log(f"✅ Prompt {prompt_id}: Lấy token thành công")
                            timeout_streak = 0
                            break
                    except asyncio.TimeoutError:
                        self._log(f"⏱️ Timeout lấy token (prompt {prompt_id}, lần {attempt + 1})")
                        timeout_streak += 1
                        if timeout_streak >= 2:
                            self._log("⚠️ Timeout liên tiếp, restart Chrome...")
                            try:
                                await asyncio.wait_for(collector.restart_browser(), timeout=60)
                            except Exception as e:
                                self._log(f"⚠️ Hết thời gian chờ restart_browser: {e}")
                            timeout_streak = 0
                    except Exception as exc:
                        self._log(f"⚠️ Lỗi lấy token (prompt {prompt_id}): {exc}")
                    if attempt < token_retry - 1:
                        await self._sleep_with_stop(token_retry_delay)

                if not token:
                    self._mark_prompt_failed(prompt_id, prompt_text, "TOKEN", "Không lấy được token")
                    return

                # ✅ LUÔN reload access_token mới nhất từ config trước mỗi request
                access_token = auth.get("access_token", "")
                try:
                    _fresh = self._load_auth_config()
                    if _fresh and _fresh.get("sessionId"):
                        # Chỉ reload sessionId lỡ người dùng đổi tay, không phá hỏng access_token sinh từ Browser
                        auth["sessionId"] = _fresh["sessionId"]
                except Exception:
                    pass

                # ── Build payload & gửi request ──
                video_aspect_ratio = self._resolve_video_aspect_ratio()
                model_key = sync_api.select_video_model_key(
                    video_aspect_ratio, self.project_data.get("veo_model"),
                )
                effective_project_id = token_project_id if token_project_id else auth["projectId"]
                payload = sync_api.build_payload_generate_video_reference(
                    token=token, session_id=auth["sessionId"], project_id=effective_project_id,
                    prompt=prompt_text, seed=self._resolve_seed(config, plan_index),
                    video_model_key=model_key, reference_media_ids=ref_media_ids,
                    scene_id=None, aspect_ratio=video_aspect_ratio, output_count=output_count,
                )
                scene_ids = self._assign_scene_ids_to_payload(payload, prompt_id)
                self._save_request_json(payload, prompt_id, prompt_text, flow="character_sync")
                self._log(f"🚀 Gửi request sync character prompt {prompt_id} ({plan_index + 1}/{len(plans)})")

                import random
                await asyncio.sleep(random.uniform(0.5, 2.5))

                # ✅ Lấy chính xác page_ref của Chrome đã sinh ra mã reCAPTCHA token này
                page_ref = None
                collector_ref = getattr(self, "_collector_ref", None)
                if collector_ref:
                    if hasattr(collector_ref, "_token_to_idx"):
                        # TokenPool mode
                        instance_idx = collector_ref._token_to_idx.get(token)
                        if instance_idx is not None:
                            colls = getattr(collector_ref, "_collectors", [])
                            if instance_idx < len(colls):
                                c = colls[instance_idx]
                                if c and getattr(c, "page", None) and not c.page.is_closed():
                                    page_ref = c.page
                                    self._log(f"🔗 Mapped token -> Chrome-{instance_idx}")
                    elif hasattr(collector_ref, "page") and collector_ref.page and not collector_ref.page.is_closed():
                        # Single TokenCollector mode
                        page_ref = collector_ref.page
                        self._log(f"🔗 Mapped token -> Single Chrome")

                if page_ref and not page_ref.is_closed():
                    self._log(f"🌐 (Browser) Gửi request sync character {prompt_id} qua browser API...")
                    response = await sync_api.request_create_video_via_browser(
                        page_ref, payload, auth.get("cookie"), access_token
                    )
                else:
                    self._log(f"⚠️ Không map được token -> browser tab, fallback urllib...")
                    response = await sync_api.request_create_video(payload, access_token, cookie=auth.get("cookie"))

                response_body = response.get("body", "")
                operations = self._parse_operations(response_body)
                err_code, err_msg = self._extract_error_info(response_body)
                if not err_msg and response.get("error"):
                    err_msg = response.get("error")

                # ── Xử lý lỗi ──
                if (not response.get("ok", True) or err_code) and err_msg and not operations:
                    err_code_str = str(err_code or "").strip()
                    if not err_code_str and not response.get("ok", True):
                        err_code_str = str(response.get("status", ""))
                    is_auth_error = err_code_str in ("16", "401") or "authentication credentials" in str(err_msg).lower()
                    retryable = err_code_str in ("13", "403", "429", "500", "503", "16", "401")
                    retryable = retryable or is_auth_error or any(k in str(err_msg).upper() for k in ["HIGH_TRAFFIC", "RECAPTCHA", "CAPTCHA"])

                    if retryable:
                        if is_auth_error:
                            # ✅ Tự động lấy token mới từ Chrome browser (có lock)
                            try:
                                if hasattr(collector, 'refresh_auth_from_browser'):
                                    self._log("⚠️ Token OAuth hết hạn, lấy mới từ Chrome browser...")
                                    fresh_token, fresh_cookie = await collector.refresh_auth_from_browser(auth.get("projectId", ""))
                                    if fresh_token:
                                        auth["access_token"] = fresh_token
                                        self._log("✅ Đã renew OAuth token từ Chrome browser")
                                    if fresh_cookie:
                                        auth["cookie"] = fresh_cookie
                                        
                                    if retry_attempt >= 1 and hasattr(collector, 'force_auto_login'):
                                        self._log("🛑 Auth error lặp lại, restart Chrome để lấy token MỚI...")
                                        try:
                                            result = await asyncio.wait_for(collector.force_auto_login(), timeout=120)
                                        except Exception as e:
                                            self._log(f"⚠️ Hết thời gian chờ (120s) khi force_auto_login: {e}")
                                            result = None
                                        if isinstance(result, tuple) and len(result) == 2:
                                            forced_token, forced_cookie = result
                                            if forced_token:
                                                auth["access_token"] = forced_token
                                                self._log("✅ Chrome restart: token mới sẵn sàng")
                                            else:
                                                self._log("❌ KHÔNG THỂ LẤY TOKEN! Phiên đăng nhập Google đã hết hạn.")
                                                self._mark_prompt_failed(prompt_id, prompt_text, "AUTH", "Phiên đăng nhập Google đã hết hạn. Hãy đăng nhập lại.")
                                                return
                                            if forced_cookie:
                                                auth["cookie"] = forced_cookie
                                else:
                                    import auth_helper
                                    self._log("⚠️ Token OAuth hết hạn, tự động lấy mới...")
                                    new_access = auth_helper.get_valid_access_token(auth.get("cookie", ""), auth.get("projectId", ""), force_refresh=True)
                                    if new_access and new_access != auth.get("access_token", ""):
                                        self._log("✅ Đã renew OAuth token")
                                        auth["access_token"] = new_access
                            except Exception as e:
                                self._log(f"⚠️ Lỗi refresh auth: {e}")

                        effective_max_retries = 15 if err_code in ("403", "429") else max_retries
                        if retry_attempt < effective_max_retries:
                            import random
                            base_wait = 20 if err_code == "403" else 5
                            backoff = min(60, base_wait * (retry_attempt + 1) + random.uniform(5.0, 20.0))
                            if is_auth_error:
                                backoff = random.uniform(2.0, 6.0)
                            self._log(f"⚠️ Prompt {prompt_id}: {err_code} {err_msg} → retry {retry_attempt + 1}/{effective_max_retries} sau {backoff:.1f}s")
                            await self._sleep_with_stop(backoff)
                            continue
                    self._mark_prompt_failed(prompt_id, prompt_text, err_code or "REQUEST", err_msg)
                    return

                if not operations:
                    self._mark_prompt_failed(prompt_id, prompt_text, "REQUEST", "Không có operations trả về")
                    return

                # ── Thành công ──
                self._handle_create_response(prompt_id, prompt_text, scene_ids, operations)
                return

        # ✅ Xử lý SONG SONG: gửi nhiều prompt cùng lúc
        if collector: # Replaced async with collector:
            # Collector đã mở & token đã được refresh trước đó
            # ✅ Refresh auth từ Chrome browser ngay sau khi Chrome khởi động
            self._collector_ref = collector
            try:
                if hasattr(collector, 'refresh_auth_from_browser'):
                    fresh_token, fresh_cookie = await collector.refresh_auth_from_browser(auth.get("projectId", ""))
                    if fresh_token:
                        auth["access_token"] = fresh_token
                        self._log("✅ Đã lấy access_token mới từ Chrome browser")
                    if fresh_cookie:
                        auth["cookie"] = fresh_cookie
            except Exception as e:
                self._log(f"⚠️ Không refresh được auth từ Chrome: {e}")

            # ✅ Proactive token refresh background task
            async def _proactive_token_refresh():
                refresh_interval = 180  # 3 phút
                while not self._should_stop():
                    await asyncio.sleep(refresh_interval)
                    if self._should_stop():
                        break
                    try:
                        if hasattr(collector, 'refresh_auth_from_browser'):
                            self._log("🔄 [Proactive] Đang refresh access_token định kỳ...")
                            ft, fc = await collector.refresh_auth_from_browser(auth.get("projectId", ""))
                            if ft:
                                auth["access_token"] = ft
                                self._log("✅ [Proactive] access_token đã được refresh!")
                            if fc:
                                auth["cookie"] = fc
                    except asyncio.CancelledError:
                        return
                    except Exception as e:
                        self._log(f"⚠️ [Proactive] Lỗi refresh token: {e}")

            proactive_refresh_task = asyncio.create_task(_proactive_token_refresh())

            pending_tasks = []

            for i, plan in enumerate(plans):
                if self._should_stop():
                    break

                # Chờ slot trống nếu đạt giới hạn
                wait_start_ts = time.time()
                while self._count_in_progress() >= max_in_flight:
                    if self.STOP:
                        break
                    elapsed = int(time.time() - wait_start_ts)
                    self._log(f"⏳ Đang chờ {self._count_in_progress()} video hoàn thành... ({elapsed}s)")
                    if not await self._sleep_with_stop(5):
                        break
                if self.STOP:
                    break

                # Tạo task song song
                task = asyncio.create_task(_process_one_prompt(plan, i))
                pending_tasks.append(task)

                # Delay ngắn giữa mỗi task
                if i < len(plans) - 1:
                    if not await self._sleep_with_stop(wait_gen_video):
                        break

            # Chờ tất cả task hoàn tất
            if pending_tasks:
                self._log(f"⏳ Đang chờ {len(pending_tasks)} prompt hoàn thành...")
                await asyncio.gather(*pending_tasks, return_exceptions=True)

            # ✅ Cancel proactive refresh task
            if proactive_refresh_task:
                proactive_refresh_task.cancel()
                try:
                    await proactive_refresh_task
                except (asyncio.CancelledError, Exception):
                    pass

        self._all_prompts_submitted = True
        self._complete_wait_start_ts = time.time()

        # ✅ INLINE RETRY CONSUMER: xử lý retry lỗi ngay trong tiến trình
        inline_retry_deadline = time.time() + 600
        while time.time() < inline_retry_deadline and not self._should_stop():
            if not self._inline_retry_queue:
                in_progress = self._count_in_progress()
                if in_progress == 0:
                    break
                await asyncio.sleep(5)
                continue

            retry_item = self._inline_retry_queue.pop(0)
            r_prompt_id = retry_item["prompt_id"]
            r_prompt_text = retry_item["prompt_text"]
            r_scene_id = retry_item["scene_id"]
            r_idx = retry_item["idx"]
            retry_count = self._inline_retry_counts.get(str(r_prompt_id), 1)

            self._log(f"🔁 Inline retry: gửi lại prompt {r_prompt_id} (lần {retry_count})...")

            # Lấy token mới
            token = None
            token_project_id = ""
            try:
                token_result = await asyncio.wait_for(
                    collector.get_token(clear_storage=False),
                    timeout=get_token_timeout,
                )
                if isinstance(token_result, tuple) and len(token_result) == 2:
                    token, token_project_id = token_result
                elif token_result:
                    token = token_result
            except Exception as e:
                self._log(f"⚠️ Inline retry: không lấy được token: {e}")

            if not token:
                self._log(f"⚠️ Inline retry: token rỗng, đánh FAILED prompt {r_prompt_id}")
                self._mark_prompt_failed(r_prompt_id, r_prompt_text, "TOKEN", "Không lấy được token cho retry")
                continue

            # Lấy lại plan info
            plan_info = None
            for plan in plans:
                if str(plan.get("id")) == str(r_prompt_id):
                    plan_info = plan
                    break

            try:
                # Build lại reference media IDs từ plan profiles + media_cache
                profiles = list((plan_info or {}).get("profiles", []))[:3]
                ref_media_ids = []
                for prof in profiles:
                    pk = str(prof.get("name_key", ""))
                    mid = media_cache.get(pk)
                    if mid:
                        ref_media_ids.append(mid)

                video_aspect_ratio = self._resolve_video_aspect_ratio()
                r_model_key = sync_api.select_video_model_key(
                    video_aspect_ratio, self.project_data.get("veo_model"),
                )

                effective_project_id = token_project_id if token_project_id else auth["projectId"]
                payload = sync_api.build_payload_generate_video_reference(
                    token=token,
                    session_id=auth["sessionId"],
                    project_id=effective_project_id,
                    prompt=r_prompt_text,
                    seed=self._resolve_seed(config, 0),
                    video_model_key=r_model_key,
                    reference_media_ids=ref_media_ids,
                    scene_id=None,
                    aspect_ratio=video_aspect_ratio,
                    output_count=output_count,
                )
                scene_ids_new = self._assign_scene_ids_to_payload(payload, r_prompt_id)
                self._save_request_json(payload, r_prompt_id, r_prompt_text, flow="character_sync_retry")

                self._log(f"🚀 Inline retry: gửi request tạo video prompt {r_prompt_id}...")
                
                # Resolving page_ref identical to main loop
                page_ref = None
                if collector:
                    if hasattr(collector, "_token_to_idx"):
                        instance_idx = collector._token_to_idx.get(token)
                        if instance_idx is not None:
                            colls = getattr(collector, "_collectors", [])
                            if instance_idx < len(colls):
                                c = colls[instance_idx]
                                if c and getattr(c, "page", None) and not c.page.is_closed():
                                    page_ref = c.page
                                    self._log(f"🔗 Mapped token -> Chrome-{instance_idx}")
                    elif hasattr(collector, "page") and collector.page and not collector.page.is_closed():
                        page_ref = collector.page
                        self._log(f"🔗 Mapped token -> Single Chrome")

                if page_ref and not page_ref.is_closed():
                    self._log(f"🌐 (Browser) Gửi request retry {r_prompt_id} qua browser API...")
                    response = await sync_api.request_create_video_via_browser(
                        page_ref, payload, auth.get("cookie"), auth.get("access_token", "")
                    )
                else:
                    self._log(f"⚠️ Inline retry: Không map được token -> browser tab, fallback urllib...")
                    response = await sync_api.request_create_video(payload, auth.get("access_token", ""), cookie=auth.get("cookie"))

                response_body = response.get("body", "")
                operations = self._parse_operations(response_body)
                err_code, err_msg = self._extract_error_info(response_body)

                if operations:
                    self._handle_create_response(r_prompt_id, r_prompt_text, scene_ids_new, operations)
                    self._log(f"✅ Inline retry: đã gửi lại prompt {r_prompt_id} thành công")
                else:
                    self._log(f"❌ Inline retry: prompt {r_prompt_id} thất bại: {err_msg}")
                    self._mark_prompt_failed(r_prompt_id, r_prompt_text, err_code or "RETRY", err_msg or "Retry thất bại")
            except Exception as e:
                self._log(f"❌ Inline retry: lỗi gửi lại prompt {r_prompt_id}: {e}")

            await asyncio.sleep(3)

        # ✅ Kiểm tra hoàn thành video và thoát luồng ngay khi xong
        try:
            await self._wait_for_completion()
            self._log("[DEBUG] _run_workflow: Đã hoàn thành tất cả video, huỷ status_task")
        except Exception as e:
            self._log(f"[DEBUG] _run_workflow: Exception in _wait_for_completion: {e}")

        # ✅ Cancel status poll loop
        if status_task:
            status_task.cancel()
            try:
                await status_task
            except (asyncio.CancelledError, Exception):
                pass

        # ✅ Cancel proactive refresh task
        try:
            proactive_refresh_task.cancel()
        except Exception:
            pass

        # ✅ Đóng collector sau khi hoàn thành (dừng harvest + cleanup Chrome)
        if collector:
            try:
                await collector.close_after_workflow()
                self._log("🔒 Đã đóng Chrome sau khi hoàn thành tất cả video")
            except Exception:
                self._log("⚠️ Lỗi dừng harvest token")

        self._log("[DEBUG] _run_workflow: Workflow kết thúc")

    def _resolve_seed(self, config, index):
        seed_mode = str(config.get("SEED_MODE", "Random")).strip().lower()
        if seed_mode == "fixed":
            return self._resolve_int_config(config, "SEED_VALUE", sync_api.DEFAULT_SEED)
        return int(time.time() * 1000 + int(index)) % 100000

    def _resolve_video_aspect_ratio(self):
        ar = str(self.project_data.get("aspect_ratio", "")).lower()
        is_portrait = "dọc" in ar or "9:16" in ar or "portrait" in ar
        if is_portrait:
            return sync_api.VIDEO_ASPECT_RATIO_PORTRAIT
        return sync_api.VIDEO_ASPECT_RATIO_LANDSCAPE

    def _build_prompt_plans(self, prompts, profiles):
        plans = []
        overflows = []
        for idx, item in enumerate(prompts):
            prompt_id = str(item.get("id") or idx + 1)
            prompt_text = str(item.get("prompt") or item.get("description") or "").strip()
            found = self._find_profiles_in_prompt(prompt_text, profiles)
            
            # If no character explicitly mentioned in prompt, just use the first 3 characters provided
            if not found and profiles:
                found = list(profiles)
            
            if len(found) > 3:
                overflows.append(
                    {
                        "prompt_id": prompt_id,
                        "prompt": prompt_text,
                        "all_names": [x.get("name") for x in found],
                    }
                )
            plans.append(
                {
                    "id": prompt_id,
                    "prompt": prompt_text,
                    "profiles": found[:3],
                }
            )
        return plans, overflows

    def _find_profiles_in_prompt(self, prompt_text, profiles):
        text = str(prompt_text or "")
        lowered = text.lower()
        hits = []
        for profile in profiles:
            name = str(profile.get("name") or "").strip()
            if not name:
                continue
            escaped = re.escape(name.lower())
            pattern = r"(?<![\w])" + escaped + r"(?![\w])"
            m = re.search(pattern, lowered, flags=re.IGNORECASE)
            if not m and name.lower() in lowered:
                pos = lowered.find(name.lower())
                if pos >= 0:
                    hits.append((pos, profile))
                continue
            if m:
                hits.append((m.start(), profile))
        hits.sort(key=lambda x: x[0])
        return [h[1] for h in hits]

    def _ask_continue_with_overflow(self, overflow_items):
        try:
            detail = []
            for item in overflow_items[:5]:
                names = ", ".join(list(item.get("all_names") or []))
                detail.append(f"- Prompt {item.get('prompt_id')}: {names}")
            detail_text = "\n".join(detail)
            msg = (
                "⚠️ Một số prompt nhắc tới hơn 3 nhân vật.\n"
                "Hệ thống tự động CHỈ DÙNG 3 nhân vật đầu tiên cho mỗi prompt.\n\n"
                f"{detail_text}"
            )
            self._log(msg)
            return True
        except Exception:
            return True

    async def _upload_all_character_media(self, profiles, session_id, access_token, cookie):
        # 🛑 Đổi thành Semaphore(1) để upload tuần tự, tránh quá tải browser API / chặn IP
        sem = asyncio.Semaphore(1)

        async def _upload_one(profile):
            async with sem:
                return await self._upload_profile_media(profile, session_id, access_token, cookie)

        tasks = [asyncio.create_task(_upload_one(p)) for p in profiles]
        results = await asyncio.gather(*tasks)

        media_cache = {}
        for ok, key, media_id, message in results:
            if ok and key and media_id:
                media_cache[key] = media_id
            else:
                self._log(f"❌ Upload ảnh nhân vật lỗi: {message}")
        return media_cache

    async def _upload_profile_media(self, profile, session_id, access_token, cookie):
        key = str(profile.get("name_key") or "")
        path = str(profile.get("path") or "")
        name = str(profile.get("name") or key)
        if not key or not path:
            return False, key, "", f"{name}: thiếu dữ liệu"

        image_bytes, mime_type = self._read_image_bytes(path)
        if not image_bytes:
            return False, key, "", f"{name}: không đọc được ảnh"

        b64 = base64.b64encode(image_bytes).decode("utf-8")
        payload = sync_api.build_payload_upload_image(
            b64,
            mime_type,
            session_id,
            aspect_ratio=sync_api.IMAGE_ASPECT_RATIO_PORTRAIT,
        )
        
        # ✅ Retry upload tối đa 3 lần (lấy token mới nếu 401)
        max_upload_retries = 3
        last_error = ""
        current_token = access_token
        current_cookie = cookie
        for attempt in range(max_upload_retries):
            # ✅ Retry: reload token mới từ config
            if attempt > 0:
                try:
                    fresh = self._load_auth_config()
                    if fresh and fresh.get("access_token"):
                        current_token = fresh["access_token"]
                    if fresh and fresh.get("cookie"):
                        current_cookie = fresh["cookie"]
                except Exception:
                    pass

            try:
                _tok = current_token
                _ck = current_cookie

                # Ưu tiên upload qua Chrome browser nếu có
                page = getattr(self, "page", None)
                collector_ref = getattr(self, "_collector_ref", None)
                if not page and collector_ref:
                    if hasattr(collector_ref, '_collectors') and collector_ref._collectors:
                        c0 = collector_ref._collectors[0]
                        if c0 and hasattr(c0, 'page') and c0.page and not c0.page.is_closed():
                            page = c0.page
                    if not page and hasattr(collector_ref, 'page'):
                        page = collector_ref.page

                if page and not page.is_closed():
                    # Gọi browser upload
                    response = await sync_api.request_upload_image_via_browser(page, payload, _tok)
                else:
                    # Fallback urllib
                    response = await asyncio.get_running_loop().run_in_executor(
                        self._upload_executor,
                        lambda: asyncio.run(sync_api.request_upload_image(payload, _tok, cookie=_ck)),
                    )
            except Exception as exc:
                last_error = f"{name}: upload exception {exc}"
                if attempt < max_upload_retries - 1:
                    self._log(f"⚠️ Upload ảnh {name} lỗi (lần {attempt + 1}/{max_upload_retries}): {exc}, retry sau 3s...")
                    await asyncio.sleep(3)
                    continue
                return False, key, "", last_error

            # ✅ 401 → retry với token mới
            http_status = response.get("status")
            if http_status == 401 and attempt < max_upload_retries - 1:
                self._log(f"⚠️ Upload {name}: 401, đang lấy token mới (lần {attempt + 1})...")
                await asyncio.sleep(3)
                continue

            body = response.get("body", "")
            media_id = self._extract_media_id(body)
            if not response.get("ok", True) or not media_id:
                err_detail = response.get("error") or response.get("body") or response.get("reason") or "No response body"
                last_error = f"{name}: upload thất bại ({err_detail})"
                if attempt < max_upload_retries - 1:
                    self._log(f"⚠️ Upload ảnh {name} thất bại (lần {attempt + 1}/{max_upload_retries}) | Chi tiết: {err_detail[:100]} | Retry sau 3s...")
                    await asyncio.sleep(3)
                    continue
                return False, key, "", last_error

            self._log(f"✅ Upload ảnh nhân vật: {name}")
            return True, key, str(media_id), ""
        
        return False, key, "", last_error

    def _assign_scene_ids_to_payload(self, payload, prompt_id):
        scene_ids = []
        for idx, req in enumerate(list(payload.get("requests") or [])):
            scene_id = str(uuid.uuid4())
            metadata = req.get("metadata") if isinstance(req.get("metadata"), dict) else {}
            metadata["sceneId"] = scene_id
            req["metadata"] = metadata
            scene_ids.append(scene_id)
            self._scene_to_prompt[scene_id] = {"prompt_id": str(prompt_id), "index": idx}
        return scene_ids

    def _handle_create_response(self, prompt_id, prompt_text, scene_ids, operations):
        op_map = {}
        for op in operations:
            scene_id = op.get("sceneId")
            if scene_id:
                op_map[str(scene_id)] = op

        for idx, scene_id in enumerate(scene_ids):
            op = op_map.get(scene_id) or (operations[idx] if idx < len(operations) else {})
            status = self._normalize_status_full(op.get("status"))
            op_name = str(((op.get("operation") or {}) if isinstance(op.get("operation"), dict) else {}).get("name") or "")
            
            if not op_name:
                status = "MEDIA_GENERATION_STATUS_FAILED"
                
            if scene_id not in self._scene_status:
                self._scene_status[scene_id] = {}
            self._scene_status[scene_id]["status"] = status
            self._scene_status[scene_id]["operation_name"] = op_name
            self._scene_next_check_at[scene_id] = time.time() + 6
            self._scene_status_change_ts[scene_id] = time.time()
            self._last_status_change_ts = time.time()
            self._log(
                f"📨 Prompt {prompt_id}_{idx + 1} create response: {self._short_status(status)}"
            )

            self._update_state_entry(
                prompt_id,
                prompt_text,
                scene_id,
                idx,
                self._short_status(status),
            )

            self.video_updated.emit(
                {
                    "prompt_idx": f"{prompt_id}_{idx + 1}",
                    "status": self._short_status(status),
                    "scene_id": scene_id,
                    "prompt": prompt_text,
                    "_prompt_id": prompt_id,
                }
            )

    async def _status_poll_loop(self, access_token, cookie=None, auth_dict=None):
        while not self._should_stop():
            pending = [
                sid
                for sid, info in self._scene_status.items()
                if self._is_running_status(info.get("status"))
            ]
            if not pending:
                # ✅ Nếu tất cả prompts đã submitted VÀ không còn scene nào pending → thoát
                if self._all_prompts_submitted:
                    self._log("🏁 Status poll: tất cả scene đã hoàn thành, thoát poll loop")
                    break
                if not await self._sleep_with_stop(1):
                    break
                continue

            eligible = [sid for sid in pending if self._scene_next_check_at.get(sid, 0) <= time.time()]
            if not eligible:
                if not await self._sleep_with_stop(1):
                    break
                continue

            # Giới hạn số lượng operations mỗi lần check để không bị lỗi HTTP 400
            eligible = eligible[:5]

            now = time.time()
            if (now - self._status_log_ts) >= self._pending_log_interval:
                self._status_log_ts = now
                self._log(f"🔄 Check status: {len(pending)} scene đang chờ/đang tạo")

            payload = {"operations": []}
            for scene_id in eligible:
                info = self._scene_status.get(scene_id, {})
                op = {"sceneId": scene_id, "status": info.get("status", "")}
                op_name = info.get("operation_name")
                if op_name:
                    op["operation"] = {"name": op_name}
                payload["operations"].append(op)

            try:
                # ✅ Luôn đọc token mới nhất từ auth dict
                if auth_dict and auth_dict.get("access_token"):
                    access_token = auth_dict["access_token"]
                if auth_dict and auth_dict.get("cookie"):
                    cookie = auth_dict["cookie"]

                # ✅ Ưu tiên check status qua browser (giống Text to Video / Image to Video)
                # Browser có cookies session tự động, tránh 401
                response = None
                collector_ref = getattr(self, '_collector_ref', None)
                browser_page = None
                if collector_ref:
                    # TokenPool mode: tìm page từ bất kỳ collector nào trong pool
                    if hasattr(collector_ref, '_collectors'):
                        for c in getattr(collector_ref, '_collectors', []):
                            if c and getattr(c, 'page', None) and not c.page.is_closed():
                                browser_page = c.page
                                break
                    # Single collector mode
                    elif getattr(collector_ref, 'page', None) and not collector_ref.page.is_closed():
                        browser_page = collector_ref.page

                if browser_page:
                    try:
                        response = await sync_api.request_check_status_via_browser(
                            browser_page, payload, access_token
                        )
                    except Exception:
                        response = None  # Fallback to urllib

                # Fallback: dùng urllib nếu browser không khả dụng
                if response is None:
                    response = await sync_api.request_check_status(payload, access_token, cookie=cookie)
            except Exception as exc:
                self._status_poll_fail_streak += 1
                self._log(f"⚠️ Check status lỗi (lần {self._status_poll_fail_streak}/4): {exc}")
                if not await self._sleep_with_stop(5):
                    break
                continue

            if not response.get("ok", True):
                self._status_poll_fail_streak += 1
                status_code = response.get("status")
                reason = response.get("reason")
                body_err = str(response.get("body", ""))[:200]
                self._log(
                    f"⚠️ Check status thất bại (lần {self._status_poll_fail_streak}/4, status={status_code}, reason={reason}, body={body_err})"
                )
                # ✅ Nếu 401 (token expired), thử dùng token mới ngầm định từ chạy nền
                if status_code == 401:
                    try:
                        self._log("🔄 Check 401: Đang thử reload/refresh token...")
                        
                        # 1. Update từ token chạy nền (nếu collector đã lấy dc token mới)
                        if auth_dict:
                            current_mem_token = auth_dict.get("access_token", "")
                            if current_mem_token and current_mem_token != access_token:
                                access_token = current_mem_token
                                cookie = auth_dict.get("cookie", cookie)
                                self._log("✅ [Check Status] Đã tiếp nhận token MỚI từ background worker (Collector)")
                                self._status_poll_fail_streak = 0
                                continue
                        
                        # 2. Xui lắm mới dùng auth_helper fetch thẳng urllib
                        import auth_helper
                        fallback_cookie = auth_dict.get("cookie") if auth_dict else cookie
                        fallback_proj = auth_dict.get("projectId", "") if auth_dict else ""
                        
                        new_token = auth_helper.get_valid_access_token(
                            fallback_cookie, fallback_proj, force_refresh=True
                        )
                        if new_token and new_token != access_token:
                            access_token = new_token
                            if auth_dict:
                                auth_dict["access_token"] = new_token
                            self._log("✅ Đã refresh access_token thủ công cho status poll bằng vòng lập phụ")
                            self._status_poll_fail_streak = 0
                            continue
                            
                        # 3. Ép Collector Chrome Browser refresh token ngay lập tức!
                        if hasattr(self, '_collector_ref') and self._collector_ref and hasattr(self._collector_ref, 'refresh_auth_from_browser'):
                            self._log("🔥 Cứu hộ 401: Ép Chrome Browser lấy token mới NGAY LẬP TỨC...")
                            try:
                                ft, fc = await self._collector_ref.refresh_auth_from_browser(fallback_proj)
                                if ft and ft != access_token:
                                    access_token = ft
                                    if auth_dict:
                                        auth_dict["access_token"] = ft
                                        if fc: auth_dict["cookie"] = fc
                                    self._log("✅ Cứu hộ thành công! Chrome Browser đã đẩy Token vào status poll!")
                                    self._status_poll_fail_streak = 0
                                    continue
                            except Exception as e:
                                self._log(f"⚠️ Cứu hộ lỗi: {e}")

                        self._log("⚠️ Status poll không thể làm mới token qua urllib/browser, chờ đợi...")
                            
                    except Exception as e:
                        self._log(f"⚠️ Lỗi xử lý token bảo mật trong status poll: {e}")
                # ✅ Cap fail streak
                if self._status_poll_fail_streak >= 8:
                    self._log("❌ Status poll thất bại 8 lần liên tiếp, đánh FAILED tất cả video đang chờ")
                    self._mark_pending_failed("Status poll failed liên tiếp")
                    break
                if not await self._sleep_with_stop(5):
                    break
                continue

            body = response.get("body", "")
            try:
                ok_parse = self._handle_status_response(body)
            except Exception as exc:
                ok_parse = False
                self._status_poll_fail_streak += 1
                self._log(f"❌ Check status exception (lần {self._status_poll_fail_streak}/4): {exc}")
                self._log(traceback.format_exc()[:800])

            if not ok_parse:
                self._status_poll_fail_streak += 1
                self._log(f"⚠️ Check status parse lỗi (lần {self._status_poll_fail_streak}/4)")
            else:
                self._status_poll_fail_streak = 0
                self._mark_stuck_pending(time.time())
            if not await self._sleep_with_stop(5):
                break

    def _handle_status_response(self, response_body):
        try:
            operations = (json.loads(response_body) or {}).get("operations", [])
        except Exception:
            return False

        updated = False

        for op in operations:
            scene_id = str(op.get("sceneId") or "")
            if not scene_id:
                continue
            if scene_id not in self._scene_to_prompt:
                continue

            status = self._normalize_status_full(op.get("status"))
            prev = self._scene_status.get(scene_id, {}).get("status")
            if prev == "MEDIA_GENERATION_STATUS_SUCCESSFUL":
                continue
            error = op.get("error") if isinstance(op.get("error"), dict) else None
            if error is None:
                operation_obj = op.get("operation") if isinstance(op.get("operation"), dict) else {}
                op_error = operation_obj.get("error")
                if isinstance(op_error, dict):
                    error = op_error
            if error:
                status = "MEDIA_GENERATION_STATUS_FAILED"

            pinfo = self._scene_to_prompt[scene_id]
            prompt_id = str(pinfo.get("prompt_id"))
            idx = int(pinfo.get("index", 0))
            prompt_text = self._get_prompt_text(prompt_id)
            prompt_idx = f"{prompt_id}_{idx + 1}"

            self._scene_status.setdefault(scene_id, {})["status"] = status
            if prev != status:
                self._scene_status_change_ts[scene_id] = time.time()
                self._log(
                    f"🔄 Prompt {prompt_id}_{idx + 1}: {self._short_status(prev)} → {self._short_status(status)}"
                )
            self._scene_next_check_at[scene_id] = time.time() + 6

            force_update = bool(error)
            if not force_update and prev == status:
                continue

            video_url, image_url = self._extract_media_urls(op)
            video_path = ""
            image_path = ""
            error_code = ""
            error_message = ""

            if isinstance(error, dict):
                error_code = str(error.get("code") or "")
                error_message = str(error.get("message") or "")
                if error_code or error_message:
                    log_msg = f"❌ Prompt {prompt_id}"
                    if error_code:
                        log_msg += f" [{error_code}]"
                    if error_message:
                        log_msg += f" {error_message}"
                    self._log(log_msg)

                # ✅ INLINE RETRY: TẤT CẢ lỗi → retry ngay trong tiến trình
                if error:
                    max_retries = self._resolve_int_config(
                        SettingsManager.load_config(), "RETRY_WITH_ERROR", 3
                    )
                    retry_key = str(prompt_id)
                    current_count = self._inline_retry_counts.get(retry_key, 0)
                    if current_count < max_retries:
                        self._inline_retry_counts[retry_key] = current_count + 1
                        self._log(
                            f"🔁 Auto-retry prompt {prompt_id} "
                            f"({current_count + 1}/{max_retries}) — lỗi: {error_code} {error_message[:60]}"
                        )
                        self._scene_status[scene_id]["status"] = "MEDIA_GENERATION_STATUS_PENDING"
                        self._scene_status_change_ts[scene_id] = time.time()
                        self._scene_next_check_at[scene_id] = time.time() + 999999
                        self._inline_retry_queue.append({
                            "prompt_id": prompt_id,
                            "prompt_text": prompt_text,
                            "scene_id": scene_id,
                            "idx": idx,
                        })
                        continue  # Không đánh FAILED, đợi retry

            if status == "MEDIA_GENERATION_STATUS_SUCCESSFUL":
                self.video_updated.emit(
                    {
                        "prompt_idx": prompt_idx,
                        "status": "DOWNLOADING",
                        "scene_id": scene_id,
                        "prompt": prompt_text,
                        "_prompt_id": prompt_id,
                    }
                )
                self._update_state_entry(
                    prompt_id, prompt_text, scene_id, idx, "DOWNLOADING",
                    video_url=video_url, image_url=image_url,
                    video_path=video_path, image_path=image_path,
                    error=error_code, message=error_message,
                )

                def dl_task(p_id, p_text, s_id, i_idx, v_url, i_url, v_path, i_path, e_code, err_msg, p_idx_str):
                    try:
                        if v_url:
                            v_path = self._download_video(v_url, p_idx_str)
                        if i_url:
                            i_path = self._download_image(i_url, p_idx_str)
                    except Exception as e:
                        self._log(f"⚠️ Lỗi tải media: {e}")
                        
                    self._update_state_entry(
                        p_id, p_text, s_id, i_idx, "SUCCESSFUL",
                        video_url=v_url, image_url=i_url,
                        video_path=v_path, image_path=i_path,
                        error=e_code, message=err_msg,
                    )
                    self.video_updated.emit({
                        "prompt_idx": p_idx_str,
                        "status": "SUCCESSFUL",
                        "scene_id": s_id,
                        "prompt": p_text,
                        "video_path": v_path,
                        "image_path": i_path,
                        "_prompt_id": p_id,
                        "error_code": e_code,
                        "error_message": err_msg,
                    })

                self._download_executor.submit(
                    dl_task,
                    prompt_id, prompt_text, scene_id, idx,
                    video_url, image_url, video_path, image_path,
                    error_code, error_message, prompt_idx
                )
                updated = True
                continue

            self._update_state_entry(
                prompt_id,
                prompt_text,
                scene_id,
                idx,
                self._short_status(status),
                video_url=video_url,
                image_url=image_url,
                video_path=video_path,
                image_path=image_path,
                error=error_code,
                message=error_message,
            )

            self.video_updated.emit(
                {
                    "prompt_idx": prompt_idx,
                    "status": self._short_status(status),
                    "scene_id": scene_id,
                    "prompt": prompt_text,
                    "video_path": video_path,
                    "image_path": image_path,
                    "_prompt_id": prompt_id,
                    "error_code": error_code,
                    "error_message": error_message,
                }
            )
            updated = True

        if updated:
            self._last_status_change_ts = time.time()
        return True

    def _mark_stuck_pending(self, now_ts):
        for scene_id, info in list(self._scene_status.items()):
            status = str(info.get("status") or "")
            if status not in {
                "MEDIA_GENERATION_STATUS_ACTIVE",
                "MEDIA_GENERATION_STATUS_PENDING",
                "ACTIVE",
                "PENDING",
            }:
                continue
            last_change = self._scene_status_change_ts.get(scene_id)
            if not last_change:
                self._scene_status_change_ts[scene_id] = now_ts
                continue
            if (now_ts - last_change) < 420:
                continue

            prompt_info = self._scene_to_prompt.get(scene_id)
            if not prompt_info:
                continue

            prompt_id = str(prompt_info.get("prompt_id") or "")
            idx = int(prompt_info.get("index", 0))
            prompt_text = self._get_prompt_text(prompt_id)
            self._scene_status[scene_id]["status"] = "MEDIA_GENERATION_STATUS_FAILED"
            self._update_state_entry(
                prompt_id,
                prompt_text,
                scene_id,
                idx,
                "FAILED",
                error="STATUS_TIMEOUT",
                message="Timeout 7p không thay đổi status",
            )
            self.video_updated.emit(
                {
                    "prompt_idx": f"{prompt_id}_{idx + 1}",
                    "status": "FAILED",
                    "scene_id": scene_id,
                    "prompt": prompt_text,
                    "_prompt_id": prompt_id,
                    "error_code": "STATUS_TIMEOUT",
                    "error_message": "Timeout 7p không thay đổi status",
                }
            )

    def _mark_pending_failed(self, message):
        """Đánh dấu tất cả pending/active là FAILED."""
        for scene_id, info in list(self._scene_status.items()):
            status = str(info.get("status") or "")
            if status not in {
                "MEDIA_GENERATION_STATUS_ACTIVE",
                "MEDIA_GENERATION_STATUS_PENDING",
                "ACTIVE",
                "PENDING",
            }:
                continue
            prompt_info = self._scene_to_prompt.get(scene_id)
            if not prompt_info:
                continue
            prompt_id = str(prompt_info.get("prompt_id") or "")
            idx = int(prompt_info.get("index", 0))
            prompt_text = self._get_prompt_text(prompt_id)
            self._scene_status[scene_id]["status"] = "MEDIA_GENERATION_STATUS_FAILED"
            self._update_state_entry(
                prompt_id, prompt_text, scene_id, idx, "FAILED",
                error="STATUS", message=message,
            )
            self.video_updated.emit({
                "prompt_idx": f"{prompt_id}_{idx + 1}",
                "status": "FAILED",
                "scene_id": scene_id,
                "prompt": prompt_text,
                "_prompt_id": prompt_id,
            })

    def _mark_prompt_failed(self, prompt_id, prompt_text, error_code, message):
        """Đánh dấu 1 prompt là FAILED."""
        scene_id = str(uuid.uuid4())
        self._update_state_entry(
            prompt_id,
            prompt_text,
            scene_id,
            0,
            "FAILED",
            error=error_code,
            message=message,
        )
        self.video_updated.emit(
            {
                "prompt_idx": f"{prompt_id}_1",
                "status": "FAILED",
                "scene_id": scene_id,
                "prompt": prompt_text,
                "_prompt_id": prompt_id,
                "error_code": str(error_code or ""),
                "error_message": str(message or ""),
            }
        )

    async def _wait_for_completion(self):
        """✅ Chờ tất cả video hoàn thành — pattern giống text-to-video.
        Thoát khi: _all_prompts_submitted=True + tất cả video đã SUCCESSFUL/FAILED
        Hoặc khi bấm STOP hoặc timeout.
        """
        _last_pending_log_ts = 0
        _no_pending_count = 0  # Đếm số lần liên tiếp không có pending

        while True:
            if self._should_stop():
                self._log("🛑 STOP nhận được, thoát loop chờ")
                break

            # Timeout check
            if self._all_prompts_submitted and self._complete_wait_timeout > 0:
                if not self._complete_wait_start_ts:
                    self._complete_wait_start_ts = time.time()
                elapsed = time.time() - self._complete_wait_start_ts
                if elapsed >= self._complete_wait_timeout:
                    self._log("⏱️ Quá thời gian chờ hoàn thành, dừng workflow")
                    break

            # ✅ KIỂM TRA TỪ STATE.JSON (nguồn chính xác nhất)
            state_pending = self._count_in_progress()

            # ✅ KIỂM TRA TỪ _scene_status
            scene_pending = [
                info for info in self._scene_status.values()
                if self._is_running_status(info.get("status"))
            ]

            # ✅ ĐIỀU KIỆN THOÁT: đã gửi hết prompts VÀ không còn video pending
            if self._all_prompts_submitted:
                if state_pending == 0:
                    _no_pending_count += 1
                    self._log(f"🔍 Kiểm tra: state_pending={state_pending}, scene_pending={len(scene_pending)}, count={_no_pending_count}")
                    # Chờ 2 lần liên tiếp để chắc chắn
                    if _no_pending_count >= 2:
                        self._log("✅ Sync character hoàn tất (từ state.json)")
                        break
                else:
                    _no_pending_count = 0

                # Fallback: kiểm tra _scene_status
                if len(self._scene_status) > 0 and (not scene_pending) and state_pending == 0:
                    self._log("✅ Sync character hoàn tất (từ scene_status)")
                    break

            # Log trạng thái chờ mỗi 15s
            now = time.time()
            total_pending = max(state_pending, len(scene_pending))
            if total_pending > 0 and (now - _last_pending_log_ts) >= 15:
                _last_pending_log_ts = now
                self._log(f"⏳ Đang chờ {total_pending} video hoàn thành...")

            if not await self._sleep_with_stop(2):
                break

    def _short_status(self, status):
        text = str(status or "")
        if "PENDING" in text:
            return "PENDING"
        if "ACTIVE" in text:
            return "ACTIVE"
        if "SUCCESSFUL" in text:
            return "SUCCESSFUL"
        if "FAILED" in text:
            return "FAILED"
        if not text:
            return "UNKNOWN"
        return text

    def _is_running_status(self, status):
        """Trả về True nếu status chưa hoàn thành (đang chạy/chờ)."""
        upper = str(status or "").upper()
        if not upper:
            return False
        return not self._is_terminal_status(upper)

    def _is_terminal_status(self, status):
        """Trả về True nếu status đã kết thúc (SUCCESSFUL/FAILED/CANCEL)."""
        upper = str(status or "").upper()
        if not upper:
            return False
        return any(marker in upper for marker in {"SUCCESS", "FAILED", "CANCEL", "ERROR"})

    def _normalize_status_full(self, value):
        s = str(value or "").strip().upper()
        if not s:
            return "MEDIA_GENERATION_STATUS_PENDING"
        if s in {
            "MEDIA_GENERATION_STATUS_PENDING",
            "MEDIA_GENERATION_STATUS_ACTIVE",
            "MEDIA_GENERATION_STATUS_SUCCESSFUL",
            "MEDIA_GENERATION_STATUS_FAILED",
        }:
            return s
        if s == "PENDING":
            return "MEDIA_GENERATION_STATUS_PENDING"
        if s == "ACTIVE":
            return "MEDIA_GENERATION_STATUS_ACTIVE"
        if s in {"SUCCESS", "SUCCESSFUL"}:
            return "MEDIA_GENERATION_STATUS_SUCCESSFUL"
        if s in {"FAIL", "FAILED", "ERROR"}:
            return "MEDIA_GENERATION_STATUS_FAILED"
        return s

    def _extract_media_urls(self, op):
        operation = op.get("operation", {}) if isinstance(op.get("operation"), dict) else {}
        metadata = operation.get("metadata", {}) if isinstance(operation.get("metadata"), dict) else {}
        video = metadata.get("video", {}) if isinstance(metadata.get("video"), dict) else {}
        image = metadata.get("image", {}) if isinstance(metadata.get("image"), dict) else {}

        fife_url = str(video.get("fifeUrl") or "")
        serving_base_uri = str(video.get("servingBaseUri") or "")
        image_url = str(image.get("fifeUrl") or image.get("uri") or "")

        try:
            config = SettingsManager.load_config()
            dl_mode = str(config.get("DOWNLOAD_MODE", "720") or "720").strip()
        except Exception:
            dl_mode = "720"

        video_url = fife_url or serving_base_uri
        if video_url and "googleusercontent.com" in video_url:
            video_url = apply_download_resolution(video_url, dl_mode)

        if not image_url:
            image_url = serving_base_uri
        return video_url, image_url

    def _download_video(self, url, prompt_idx):
        if not url:
            return ""
        output_dir = self._video_output_dir()
        file_path = self._build_timestamped_media_path(output_dir, str(prompt_idx), ".mp4")
        try:
            with requests.get(url, stream=True, timeout=60) as resp:
                resp.raise_for_status()
                with open(file_path, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=1024 * 1024):
                        if chunk:
                            f.write(chunk)
            out_path = str(file_path.resolve())
            # remove_watermark(out_path, log_callback=self._log)
            self.video_folder_updated.emit(str(output_dir.resolve()))
            return out_path
        except Exception:
            return ""

    def _download_image(self, url, prompt_idx):
        if not url:
            return ""
        output_dir = self._image_output_dir()
        file_path = self._build_timestamped_media_path(output_dir, str(prompt_idx), ".jpg")
        try:
            with requests.get(url, stream=True, timeout=60) as resp:
                resp.raise_for_status()
                with open(file_path, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=1024 * 256):
                        if chunk:
                            f.write(chunk)
            return str(file_path.resolve())
        except Exception:
            return ""

    def _build_timestamped_media_path(self, output_dir: Path, prompt_idx: str, suffix: str) -> Path:
        timestamp = datetime.now().strftime("%d%m%Y_%H%M%S")
        base_name = f"{prompt_idx}_{timestamp}"
        file_path = output_dir / f"{base_name}{suffix}"
        counter = 1
        while file_path.exists():
            file_path = output_dir / f"{base_name}_{counter}{suffix}"
            counter += 1
        return file_path

    def _read_image_bytes(self, image_link):
        parsed = urlparse(str(image_link or ""))
        is_url = parsed.scheme in {"http", "https"}
        try:
            if is_url:
                resp = requests.get(str(image_link), timeout=30)
                resp.raise_for_status()
                mime_type = resp.headers.get("Content-Type") or ""
                if ";" in mime_type:
                    mime_type = mime_type.split(";", 1)[0].strip()
                if not mime_type:
                    mime_type = self._guess_mime_type(str(image_link))
                return resp.content, mime_type

            path = Path(str(image_link))
            if not path.exists():
                return b"", ""
            return path.read_bytes(), self._guess_mime_type(str(path))
        except Exception:
            return b"", ""

    def _guess_mime_type(self, path_value):
        mime_type, _ = mimetypes.guess_type(path_value)
        return mime_type or "image/jpeg"

    def _extract_media_id(self, response_body):
        try:
            body_json = json.loads(response_body)
        except Exception:
            return ""
        if not isinstance(body_json, dict):
            return ""
        mg = body_json.get("mediaGenerationId")
        if isinstance(mg, dict):
            mid = mg.get("mediaGenerationId")
            if mid:
                return str(mid)
        media = body_json.get("media")
        if isinstance(media, dict):
            mid = media.get("mediaId") or media.get("id")
            if mid:
                return str(mid)
        return str(body_json.get("mediaId") or "")

    def _parse_operations(self, response_body):
        try:
            return (json.loads(response_body) or {}).get("operations", [])
        except Exception:
            return []

    def _extract_error_info(self, response_body):
        try:
            err = (json.loads(response_body) or {}).get("error")
        except Exception:
            err = None
        if not isinstance(err, dict):
            return "", ""
        return str(err.get("code") or ""), str(err.get("message") or "")

    def _save_request_json(self, payload, prompt_id, prompt_text, flow="character_sync"):
        try:
            project_dir = WORKFLOWS_DIR / str(self.project_name)
            project_dir.mkdir(parents=True, exist_ok=True)
            request_file = project_dir / "request.json"
            request_data = {
                "timestamp": int(time.time()),
                "project_name": self.project_name,
                "flow": flow,
                "prompt_id": prompt_id,
                "prompt_text": prompt_text,
                "request": payload,
            }
            entries = []
            if request_file.exists():
                try:
                    txt = request_file.read_text(encoding="utf-8").strip()
                    if txt:
                        parsed = json.loads(txt)
                        if isinstance(parsed, list):
                            entries = parsed
                        elif isinstance(parsed, dict):
                            entries = [parsed]
                except Exception:
                    entries = []
            entries.append(request_data)
            request_file.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:
            self._log(f"⚠️ Không thể lưu request.json: {exc}")

    def _get_state_file_path(self):
        project_dir = WORKFLOWS_DIR / str(self.project_name)
        project_dir.mkdir(parents=True, exist_ok=True)
        return project_dir / "state.json"

    def _load_state_json(self):
        state_file = self._get_state_file_path()
        if not state_file.exists():
            return {}
        try:
            return json.loads(state_file.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save_state_json(self, data):
        try:
            self._get_state_file_path().write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            return True
        except Exception:
            return False

    def _ensure_prompt_entry(self, state_data, prompt_id, prompt_text):
        if "prompts" not in state_data:
            state_data["prompts"] = {}
        key = str(prompt_id)
        if key not in state_data["prompts"]:
            state_data["prompts"][key] = {
                "id": str(prompt_id),
                "prompt": str(prompt_text or ""),
                "scene_ids": [],
                "statuses": [],
                "video_paths": [],
                "image_paths": [],
                "video_urls": [],
                "image_urls": [],
                "errors": [],
                "messages": [],
            }
        return state_data["prompts"][key]

    def _update_state_entry(self, prompt_id, prompt_text, scene_id, idx, status, video_url="", image_url="", video_path="", image_path="", error="", message=""):
        state_data = self._load_state_json()
        pdata = self._ensure_prompt_entry(state_data, prompt_id, prompt_text)

        if "scene_id_map" not in state_data:
            state_data["scene_id_map"] = {}

        while len(pdata["scene_ids"]) <= idx:
            pdata["scene_ids"].append("")
        pdata["scene_ids"][idx] = str(scene_id or "")
        state_data["scene_id_map"][str(scene_id or "")] = str(prompt_id)

        for key, val in [
            ("statuses", status),
            ("video_paths", video_path),
            ("image_paths", image_path),
            ("video_urls", video_url),
            ("image_urls", image_url),
            ("errors", error),
            ("messages", message),
        ]:
            while len(pdata[key]) <= idx:
                pdata[key].append("")
            pdata[key][idx] = str(val or "")

        self._save_state_json(state_data)

    def _count_in_progress_from_state(self):
        state_data = self._load_state_json()
        prompts = state_data.get("prompts", {}) if isinstance(state_data, dict) else {}
        count = 0
        running_markers = {"PENDING", "ACTIVE", "REQUESTED", "DOWNLOADING", "TOKEN", "QUEUED", "SUBMIT", "CREATING", "GENERATING", "RUNNING", "PROCESS", "PROGRESS", "STARTED"}
        active_prompt_ids = {str(pid).strip() for pid in (self._active_prompt_ids or set()) if str(pid).strip()}
        for prompt_key, prompt_data in prompts.items():
            if active_prompt_ids and str(prompt_key).strip() not in active_prompt_ids:
                continue
            statuses = prompt_data.get("statuses", []) if isinstance(prompt_data, dict) else []
            if any(any(marker in str(s or "").upper() for marker in running_markers) for s in statuses):
                count += 1
        return count

    def _count_in_progress(self):
        return int(self._count_in_progress_from_state())

    def _cleanup_workflow_data(self):
        try:
            self._save_state_json({})
            project_dir = WORKFLOWS_DIR / str(self.project_name)
            if not project_dir.exists():
                return
            keep_files = {"test.json", "status.json"}
            keep_dirs = {"Download", "thumbnails"}
            for item in project_dir.iterdir():
                if item.name in keep_files or item.name in keep_dirs:
                    continue
                try:
                    if item.is_file():
                        item.unlink()
                    elif item.is_dir():
                        import shutil
                        shutil.rmtree(item, ignore_errors=True)
                except Exception:
                    pass
        except Exception:
            pass

    def _resolve_int_config(self, config, key, default_value):
        try:
            return int(config.get(key, default_value))
        except Exception:
            return int(default_value)

    def _output_root_dir(self):
        raw = str(self.project_data.get("video_output_dir") or self.project_data.get("output_dir") or "").strip()
        if not raw:
            raw = str(WORKFLOWS_DIR / self.project_name / "Download")
        p = Path(raw)
        p.mkdir(parents=True, exist_ok=True)
        return p

    def _video_output_dir(self):
        p = self._output_root_dir() / "video"
        p.mkdir(parents=True, exist_ok=True)
        return p

    def _image_output_dir(self):
        p = self._output_root_dir() / "image"
        p.mkdir(parents=True, exist_ok=True)
        return p

    def _load_auth_config(self):
        config = SettingsManager.load_config()
        account = config.get("account1", {}) if isinstance(config, dict) else {}
        session_id = account.get("sessionId")
        project_id = account.get("projectId")
        access_token = account.get("access_token")
        cookie = account.get("cookie")
        if not (session_id and project_id and access_token):
            return None
        return {
            "sessionId": session_id,
            "projectId": project_id,
            "access_token": access_token,
            "cookie": cookie,
            "URL_GEN_TOKEN": account.get("URL_GEN_TOKEN"),
            "folder_user_data_get_token": account.get("folder_user_data_get_token"),
        }

    def _load_text_prompts(self):
        if self.project_data.get("_use_project_prompts"):
            prompts_root = self.project_data.get("prompts", {}) if isinstance(self.project_data.get("prompts"), dict) else {}
            items = prompts_root.get("character_sync", [])
            if not items:
                items = prompts_root.get("text_to_video", [])
        else:
            test_file = WORKFLOWS_DIR / self.project_name / "test.json"
            items = []
            if test_file.exists():
                try:
                    data = json.loads(test_file.read_text(encoding="utf-8"))
                    prompts_root = data.get("prompts", {}) if isinstance(data, dict) else {}
                    items = prompts_root.get("character_sync", []) or prompts_root.get("text_to_video", [])
                except Exception:
                    items = []

        out = []
        for idx, item in enumerate(list(items or []), start=1):
            if not isinstance(item, dict):
                continue
            prompt_text = str(item.get("prompt") or item.get("description") or "").strip()
            if not prompt_text:
                continue
            out.append({"id": str(item.get("id") or idx), "prompt": prompt_text})
        return out

    def _load_character_profiles(self):
        candidates = []

        roots = []
        pdata_chars = self.project_data.get("characters")
        if isinstance(pdata_chars, list):
            roots.extend(pdata_chars)
        pdata_chars2 = self.project_data.get("character_profiles")
        if isinstance(pdata_chars2, list):
            roots.extend(pdata_chars2)

        test_file = WORKFLOWS_DIR / self.project_name / "test.json"
        if test_file.exists():
            try:
                data = json.loads(test_file.read_text(encoding="utf-8"))
                chars = data.get("characters")
                if isinstance(chars, list):
                    roots.extend(chars)
            except Exception:
                pass

        seen = set()
        for item in roots:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or item.get("character_name") or item.get("label") or "").strip()
            path = str(item.get("path") or item.get("image") or item.get("image_path") or "").strip()
            if not name or not path:
                continue
            key = name.lower()
            if key in seen:
                continue
            seen.add(key)
            candidates.append({"name": name, "name_key": key, "path": path})

        return candidates

    def _get_prompt_text(self, prompt_id):
        for item in self._load_text_prompts():
            if str(item.get("id")) == str(prompt_id):
                return str(item.get("prompt") or "")
        return ""

    async def _init_token_collector(self, project_link, chrome_userdata_root, profile_name, clear_data_interval, idle_timeout, token_timeout):
        from settings_manager import SettingsManager
        config = SettingsManager.load_config()
        num_chrome = max(1, int(config.get("NUM_CHROME", 1)))
        clear_data_wait = max(1, int(config.get("CLEAR_DATA_WAIT", 2)))

        if num_chrome > 1:
            self._log(f"🚀 Khởi động TokenPool với {num_chrome} Chrome instances")
            pool = TokenPool(
                num_chrome=num_chrome,
                project_url=project_link,
                chrome_userdata_root=chrome_userdata_root,
                profile_name=profile_name,
                log_callback=self._log,
                stop_check=self._should_stop,
                mode="video",
                token_timeout=token_timeout,
                clear_data_interval=clear_data_interval,
                clear_data_wait=clear_data_wait,
                keep_chrome_open=True,
                close_chrome_after_token=getattr(self, '_close_chrome_after_token', False),
                hide_window=True,
            )
            await pool.start()
            return pool

        self._log(f"🧭 Khởi tạo TokenCollector cho Sync Character")
        return TokenCollector(
            project_link,
            chrome_userdata_root=chrome_userdata_root,
            profile_name=profile_name,
            debug_port=9222,
            headless=False,
            hide_window=True,
            token_timeout=token_timeout,
            idle_timeout=idle_timeout,
            log_callback=self._log,
            stop_check=self._should_stop,
            clear_data_interval=clear_data_interval,
            keep_chrome_open=True,
            mode="video",
        )


SyncCharacterWorkflow = CharacterSyncWorkflow
TextToVideoWorkflow = CharacterSyncWorkflow
