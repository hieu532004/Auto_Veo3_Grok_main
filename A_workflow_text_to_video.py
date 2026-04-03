import asyncio
import threading
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
import uuid
import traceback
from datetime import datetime
from pathlib import Path
import requests
from PyQt6.QtCore import QThread, pyqtSignal as Signal
from PyQt6.QtWidgets import QMessageBox

from settings_manager import SettingsManager, DATA_GENERAL_DIR, WORKFLOWS_DIR
from A_workflow_get_token import TokenCollector
from token_pool import TokenPool
import API_text_to_video as t2v_api
from chrome_process_manager import ChromeProcessManager
from workflow_run_control import get_running_video_count, get_max_in_flight
from watermark_remover import remove_watermark, apply_download_resolution


# Toggle token Chrome window visibility for debug.
# True  -> move window off-screen
# False -> keep window on-screen (easier to debug)


class TextToVideoWorkflow(QThread):
	"""Workflow Text to Video qua API (khong Playwright)."""

	log_message = Signal(str)
	video_updated = Signal(dict)
	automation_complete = Signal()
	video_folder_updated = Signal(str)

	def __init__(self, project_name=None, project_data=None, parent=None):
		super().__init__(parent)
		self.project_name = project_name or (project_data or {}).get("project_name", "Unknown")
		self.project_data = project_data or {}
		self._auto_noi_canh = bool(self.project_data.get("_auto_noi_canh"))
		self._keep_chrome_open = bool(self.project_data.get("_keep_chrome_open")) or self._auto_noi_canh
		self._close_chrome_after_token = bool(self.project_data.get("_close_chrome_after_token"))
		self._close_chrome_on_finish = bool(self.project_data.get("_close_chrome_on_finish", True))
		self.STOP = 0
		self._download_executor = ThreadPoolExecutor(max_workers=5, thread_name_prefix="t2v-dl")
		self._scene_status = {}
		self._scene_to_prompt = {}
		self._prompt_scene_order = {}
		self._last_submit_ts = 0
		self._status_log_ts = 0
		self._pending_log_interval = 3
		self._resend_items = None  # ✅ KHỞI TẠO RESEND ITEMS
		self._all_prompts_submitted = False  # ✅ Flag - tất cả prompts đã gửi xong
		self._complete_wait_timeout = 0
		self._complete_wait_start_ts = 0
		self._status_poll_fail_streak = 0
		self._last_status_change_ts = 0
		self._in_flight_block_start_ts = 0
		self._scene_next_check_at = {}
		self._scene_status_change_ts = {}
		self._state_status_logged = set()
		self._active_prompt_ids = set()
		self._worker_controls_lifecycle = bool(self.project_data.get("_worker_controls_lifecycle", False))
		self._inline_retry_queue = []  # ✅ Queue cho inline retry AUDIO_FILTERED
		self._inline_retry_counts = {}  # ✅ {prompt_id: count} đếm số lần retry

	def run(self):
		try:
			running_loop = asyncio.get_running_loop()
		except RuntimeError:
			running_loop = None

		if running_loop and running_loop.is_running():
			self._log("⚠️  Đang có event loop chạy, chuyển workflow sang thread mới...")
			worker = threading.Thread(target=self._run_with_new_loop, daemon=True)
			worker.start()
			worker.join(timeout=60)
			# Ensure thread is stopped and Chrome is closed
			if worker.is_alive() and (not self._worker_controls_lifecycle):
				self._log("⚠️ Thread workflow vẫn chưa thoát (Chrome vẫn mở)")
			return

		self._run_with_new_loop()

	def _run_with_new_loop(self):
		import os
		from pathlib import Path
		loop = asyncio.new_event_loop()
		asyncio.set_event_loop(loop)
		need_close_after_stop = False
		try:
			# ✅ KIỂM TRA RESEND_ITEMS - NẾU CÓ THÌ CHẠY RESEND, KHÔNG THÌ CHẠY WORKFLOW BÌNH THƯỜNG
			if hasattr(self, '_resend_items') and self._resend_items:
				self._log(f"🔄 Chạy resend workflow với {len(self._resend_items)} item(s)")
				loop.run_until_complete(self._run_resend_workflow(self._resend_items))
			else:
				self._log("📝 Chạy workflow Tạo Video Từ TEXT")
				loop.run_until_complete(self._run_workflow())
			
			# ✅ Inline retry AUDIO_FILTERED đã được xử lý bên trong _run_workflow
			
		except asyncio.CancelledError:
			self._log("🛑 Workflow bị cancel")
		except Exception as exc:
			self._log(f"❌ Lỗi workflow: {type(exc).__name__}: {exc}")
			self._log(traceback.format_exc()[:500])
		finally:
			try:
				loop.close()
			except Exception:
				pass
			# Chrome giữ nguyên mở để tái sử dụng cho lần tạo tiếp theo
			self._log("✅ Workflow kết thúc (Chrome vẫn mở để tái sử dụng)")
			# Xóa các file lock trong profile để tránh block, KHÔNG xóa profile, giữ login
			try:
				chrome_userdata_root = self.project_data.get("chrome_userdata_root")
				profile_name = self.project_data.get("profile_name") or "Default"
				from chrome import resolve_profile_dir
				profile_dir = resolve_profile_dir(profile_name)
				lock_files = ["LOCK", "SingletonLock", "SingletonCookie", "SingletonSocket"]
				deleted = []
				for fname in lock_files:
					fpath = Path(profile_dir) / fname
					if fpath.exists():
						try:
							fpath.unlink()
							deleted.append(str(fpath))
						except Exception:
							pass
				if deleted:
					self._log(f"🧹 Đã xóa file lock Chrome: {deleted}")
			except Exception as e:
				self._log(f"⚠️ Lỗi xóa file lock Chrome: {e}")
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

	def _collect_retryable_failed_prompts(self):
		"""Lấy danh sách prompt bị lỗi có thể retry (TOKEN, 400, 429, 403, v.v.) từ state.json.
		Loại trừ lỗi vĩnh viễn: AUTH, SAFETY_FILTER, CONTENT_FILTERED."""
		NON_RETRYABLE_ERRORS = {"AUTH", "SAFETY_FILTER", "CONTENT_FILTERED", "PUBLIC_ERROR_UNSAFE_GENERATION", "NO_CHARACTER", "UPLOAD"}
		try:
			state_data = self._load_state_json()
			failed_map = {}  # prompt_id -> {prompt_id, prompt_text}
			prompts = state_data.get("prompts", {})
			for prompt_key, prompt_data in prompts.items():
				if not isinstance(prompt_data, dict):
					continue
				prompt_id = str(prompt_data.get("id", ""))
				prompt_text = prompt_data.get("prompt", "")
				statuses = prompt_data.get("statuses", [])
				errors = prompt_data.get("errors", [])
				
				has_success = any(s == "SUCCESSFUL" for s in statuses)
				if has_success:
					continue  # Prompt đã thành công, không retry
				
				has_retryable_fail = False
				for idx, status in enumerate(statuses):
					if status != "FAILED":
						continue
					err_code = str(errors[idx] if idx < len(errors) else "").strip().upper()
					if err_code in NON_RETRYABLE_ERRORS:
						continue  # Lỗi vĩnh viễn, không retry
					has_retryable_fail = True
				
				if has_retryable_fail and prompt_id not in failed_map:
					failed_map[prompt_id] = {
						"prompt_id": prompt_id,
						"prompt_text": prompt_text,
					}
			
			result = list(failed_map.values())
			if result:
				self._log(f"📋 Tìm thấy {len(result)} prompt lỗi có thể retry")
			return result
		except Exception as e:
			self._log(f"⚠️ Lỗi đọc state.json cho auto-retry: {e}")
			return []

	def _close_token_chrome_later(self):
		# Chrome giữ mở để tái sử dụng
		self._log("ℹ️ Chrome vẫn mở để tái sử dụng cho lần tạo tiếp theo")

	def _log(self, message):
		try:
			self.log_message.emit(message)
		except Exception:
			pass

	def _save_request_json(self, payload, prompt_id, prompt_text, flow="text_to_video"):
		"""Lưu lịch sử request payload vào Workflows/{project}/request.json (dễ đọc, có format)."""
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
					raw_text = request_file.read_text(encoding="utf-8").strip()
					if raw_text:
						parsed = json.loads(raw_text)
						if isinstance(parsed, list):
							entries = parsed
						elif isinstance(parsed, dict):
							entries = [parsed]
				except Exception:
					try:
						with open(request_file, "r", encoding="utf-8") as f:
							for line in f:
								line = line.strip()
								if not line:
									continue
								try:
									obj = json.loads(line)
									if isinstance(obj, dict):
										entries.append(obj)
								except Exception:
									pass
					except Exception:
						entries = []

			entries.append(request_data)
			with open(request_file, "w", encoding="utf-8") as f:
				json.dump(entries, f, ensure_ascii=False, indent=2)
		except Exception as e:
			self._log(f"⚠️ Không thể lưu request.json: {e}")

	def stop(self):
		if self.STOP:
			return
		self.STOP = 1
		try:
			self.requestInterruption()
		except Exception:
			pass
		self._log("🛑 Nhận lệnh dừng: set STOP=1")

		# Chrome giữ mở, không đóng khi dừng
		self._log("🛑 Đã dừng workflow (Chrome vẫn mở)")

	def _should_stop(self):
		return bool(self.STOP)

	async def _sleep_with_stop(self, seconds, step=0.2):
		total = max(0.0, float(seconds or 0.0))
		if total <= 0:
			return not self._should_stop()
		end_ts = time.time() + total
		while time.time() < end_ts:
			if self._should_stop():
				return False
			remain = end_ts - time.time()
			await asyncio.sleep(max(0.01, min(float(step), remain)))
		return not self._should_stop()

	async def _process_video_prompt(
		self, collector, prompt_id, prompt_text,
		session_id, project_id, access_token, cookie, auth,
		video_model_key, video_aspect_ratio, output_count,
		token_option, max_token_retries, token_retry_delay,
		get_token_timeout, clear_data_every, retry_with_error,
		wait_resend, prompt_retry_counts,
	):
		"""Xử lý 1 prompt video song song — lấy token, gửi request, retry nếu lỗi."""
		retry_count = prompt_retry_counts.get(prompt_id, 0)
		token_request_count = 0

		# ✅ Đưa ngay vào state.json để đếm luồng chính xác, tránh tạo ồ ạt
		for i in range(output_count):
			self._update_state_entry(prompt_id, prompt_text, "", i, "ACTIVE")

		# ✅ Emit ACTIVE status để UI cập nhật cột status
		self.video_updated.emit({
			"prompt_idx": f"{prompt_id}_1",
			"status": "ACTIVE",
			"scene_id": "",
			"prompt": prompt_text,
			"_prompt_id": prompt_id,
		})

		while True:
			if self.STOP:
				return

			# ── Lấy token ──
			token = None
			token_project_id = ""
			for attempt in range(max_token_retries):
				if self.STOP:
					return
				try:
					self._log(f"🔐 Đang lấy token... prompt {prompt_id} | lần {attempt + 1}/{max_token_retries}")
					token_request_count += 1
					clear_storage = clear_data_every > 0 and (token_request_count % clear_data_every == 0)
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
						break
				except asyncio.TimeoutError:
					self._log(f"⏱️ Timeout lấy token (prompt {prompt_id}, lần {attempt + 1})")
					timeout_key = f"_token_timeout_{prompt_id}"
					streak = prompt_retry_counts.get(timeout_key, 0) + 1
					prompt_retry_counts[timeout_key] = streak
					if streak >= 2:
						self._log("⚠️ Timeout liên tiếp, restart Chrome...")
						await collector.restart_browser()
						prompt_retry_counts[timeout_key] = 0
				except Exception as e:
					self._log(f"⚠️ Lỗi lấy token: {e}")
				if attempt < max_token_retries - 1:
					if self.STOP:
						return
					await self._sleep_with_stop(token_retry_delay)

			if not token:
				self._log(f"❌ Không lấy được token (prompt {prompt_id})")
				fail_scene_ids = [str(uuid.uuid4()) for _ in range(output_count)]
				for idx, scene_id in enumerate(fail_scene_ids):
					self._update_state_entry(prompt_id, prompt_text, scene_id, idx, "FAILED", error="TOKEN", message="Token timeout")
					self.video_updated.emit({
						"prompt_idx": f"{prompt_id}_{idx + 1}", "status": "FAILED",
						"scene_id": scene_id, "prompt": prompt_text, "_prompt_id": prompt_id,
						"error_code": "TOKEN", "error_message": "Token timeout",
					})
				return
			if self._should_stop():
				return

			# ── Build payload & gửi request ──
			# Dùng project_id từ token (mỗi Chrome có project riêng) nếu có
			effective_project_id = token_project_id if token_project_id else project_id
			payload = t2v_api.build_create_payload(
				prompt_text, session_id, effective_project_id, token,
				model_key=video_model_key, aspect_ratio=video_aspect_ratio, output_count=output_count,
			)
			if self._should_stop():
				return

			scene_ids = self._assign_scene_ids(payload, prompt_id, output_count)
			self._last_submit_ts = time.time()
			self._save_request_json(payload, prompt_id, prompt_text, flow="text_to_video")
			self._log(f"🚀 [{time.strftime('%H:%M:%S')}] Gửi request tạo video (prompt {prompt_id})...")

			# ✅ LUÔN reload access_token mới nhất từ config trước mỗi request
			try:
				_fresh = self._load_auth_config()
				if _fresh and _fresh.get("access_token"):
					access_token = _fresh["access_token"]
					auth["access_token"] = access_token
				if _fresh and _fresh.get("cookie"):
					cookie = _fresh["cookie"]
					auth["cookie"] = cookie
			except Exception:
				pass

			# ✅ Lấy page_ref từ Chrome đã sinh ra token (giống Character Sync)
			import random
			await asyncio.sleep(random.uniform(0.5, 2.5))
			page_ref = None
			if hasattr(collector, "_token_to_idx"):
				instance_idx = collector._token_to_idx.get(token)
				if instance_idx is not None:
					colls = getattr(collector, "_collectors", [])
					if instance_idx < len(colls):
						c = colls[instance_idx]
						if c and getattr(c, "page", None) and not c.page.is_closed():
							page_ref = c.page
			elif hasattr(collector, "page") and collector.page and not collector.page.is_closed():
				page_ref = collector.page

			if page_ref and not page_ref.is_closed():
				response = await t2v_api.request_create_video_via_browser(
					page_ref, t2v_api.URL_GENERATE_TEXT_TO_VIDEO, payload, access_token,
				)
			else:
				response = await t2v_api.request_create_video(payload, access_token, cookie=cookie)

			if self._should_stop():
				return

			response_body = response.get("body", "")
			error_code, error_message = self._extract_error_info(response_body)
			retryable_errors = {"403", "3", "13", "53", "16", "401", "429", "400", "500", "503"}
			error_code_str = str(error_code or "").strip()
			if not error_code_str and not response.get("ok", True):
				error_code_str = str(response.get("status", ""))
			
			consecutive_403 = prompt_retry_counts.get(f"{prompt_id}_403_count", 0) + (1 if error_code_str == "403" else 0)
			is_auth_error = (
				error_code_str in ("16", "401") 
				or "authentication credentials" in str(error_message).lower()
			)

			# ── Xử lý lỗi retryable ──
			if (not response.get("ok", True) or error_code_str) and (error_code_str in retryable_errors or is_auth_error):
				if is_auth_error:
					auth_retries = prompt_retry_counts.get(f"{prompt_id}_auth_count", 0)
					self._log(f"⚠️ Token OAuth hết hạn (lần {auth_retries + 1}), tự động lấy mới...")
					try:
						if hasattr(collector, 'refresh_auth_from_browser'):
							fresh_token, fresh_cookie = await collector.refresh_auth_from_browser(project_id)
							if fresh_token:
								access_token = fresh_token
								auth["access_token"] = fresh_token
								self._log("✅ Đã renew OAuth token từ Chrome browser")
							if fresh_cookie:
								cookie = fresh_cookie
								auth["cookie"] = fresh_cookie
						# Nếu đã thử cách thường 1 lần mà vẫn fail auth → force restart Chrome
						if auth_retries >= 1 and hasattr(collector, 'force_auto_login'):
							self._log("🛑 Auth error lặp lại, restart Chrome để lấy token MỚI...")
							try:
								result = await asyncio.wait_for(collector.force_auto_login(), timeout=120)
							except Exception as e:
								self._log(f"⚠️ Hết thời gian chờ (120s) khi force_auto_login: {e}")
								result = None
							if isinstance(result, tuple) and len(result) == 2:
								forced_token, forced_cookie = result
								if forced_token:
									access_token = forced_token
									auth["access_token"] = forced_token
									self._log("✅ Chrome restart: token mới sẵn sàng")
								else:
									self._log("❌ KHÔNG THỂ LẤY TOKEN! Phiên đăng nhập Google đã hết hạn. Hãy đăng nhập lại.")
									fail_scene_ids = scene_ids if scene_ids else [str(uuid.uuid4()) for _ in range(output_count)]
									for idx, sid in enumerate(fail_scene_ids):
										self._update_state_entry(prompt_id, prompt_text, sid, idx, "FAILED", error="AUTH", message="Phiên đăng nhập Google đã hết hạn. Hãy đăng nhập lại!")
										self.video_updated.emit({
											"prompt_idx": f"{prompt_id}_{idx + 1}", "status": "FAILED",
											"scene_id": sid, "prompt": prompt_text, "_prompt_id": prompt_id,
											"error_code": "AUTH", "error_message": "Phiên đăng nhập Google đã hết hạn. Hãy đăng nhập lại!",
										})
									return
								if forced_cookie:
									cookie = forced_cookie
									auth["cookie"] = forced_cookie
					except Exception as e:
						self._log(f"⚠️ Lỗi refresh auth: {e}")
					
					prompt_retry_counts[f"{prompt_id}_auth_count"] = auth_retries + 1

				if self.STOP:
					return

				if error_code_str == "403":
					consecutive = prompt_retry_counts.get(f"{prompt_id}_403_count", 0) + 1
					prompt_retry_counts[f"{prompt_id}_403_count"] = consecutive
				else:
					prompt_retry_counts[f"{prompt_id}_403_count"] = 0
					consecutive = 0

				retry_count += 1
				prompt_retry_counts[prompt_id] = retry_count
				# ✅ Auth errors dùng bộ đếm riêng, KHÔNG tiêu hao retry_count chung
				if is_auth_error:
					retry_count -= 1  # Hoàn lại retry_count chung cho auth error
					prompt_retry_counts[prompt_id] = retry_count
				# ✅ 429 Quota Exhausted: KHÔNG tiêu hao retry_count (đây là rate limit tạm thời)
				if error_code_str == "429":
					retry_count -= 1  # Hoàn lại retry_count cho 429
					prompt_retry_counts[prompt_id] = retry_count
				# ✅ 400 Invalid payload (token sai format): KHÔNG tiêu hao retry_count
				if error_code_str == "400" and "recaptcha" in str(error_message).lower():
					retry_count -= 1  # Hoàn lại retry_count cho token format error
					prompt_retry_counts[prompt_id] = retry_count
				
				self._discard_scene_ids(prompt_id, scene_ids)

				if error_code_str == "403" and consecutive >= 3:
					self._log("⚠️ 403 liên tiếp, tiếp tục chờ...")
					prompt_retry_counts[f"{prompt_id}_403_count"] = 0

				# Kiểm tra giới hạn retry - 403/429 dùng retry_count (max 15), auth dùng auth_retries (max 10)
				max_retries = 15 if error_code_str in ("403", "429") else retry_with_error
				if is_auth_error:
					auth_limit = 10
					current_auth = prompt_retry_counts.get(f"{prompt_id}_auth_count", 0)
					if current_auth >= auth_limit:
						self._log(f"❌ Đạt giới hạn auth retry ({auth_limit}) cho '401', FAILED")
						for idx, sid in enumerate(scene_ids):
							self._update_state_entry(prompt_id, prompt_text, sid, idx, "FAILED", error=error_code_str, message=error_message)
							self.video_updated.emit({
								"prompt_idx": f"{prompt_id}_{idx + 1}", "status": "FAILED",
								"scene_id": sid, "prompt": prompt_text, "_prompt_id": prompt_id,
								"error_code": error_code_str, "error_message": error_message,
							})
						return
				elif retry_count >= max_retries:
					self._log(f"❌ Đạt giới hạn retry ({max_retries}) cho '{error_code_str}', FAILED")
					for idx, sid in enumerate(scene_ids):
						self._update_state_entry(prompt_id, prompt_text, sid, idx, "FAILED", error=error_code_str, message=error_message)
						self.video_updated.emit({
							"prompt_idx": f"{prompt_id}_{idx + 1}", "status": "FAILED",
							"scene_id": sid, "prompt": prompt_text, "_prompt_id": prompt_id,
							"error_code": error_code_str, "error_message": error_message,
						})
					return

				import random
				# 429 quota exhausted hoặc 403 liên tiếp → chờ dãn cách có random (jitter) để tránh nã đạn cùng 1 lúc
				if error_code_str in ("429", "403"):
					base_wait = wait_resend if error_code_str == "403" else 30
					backoff = min(90, base_wait * retry_count + random.uniform(5.0, 25.0))
					self._log(f"⚠️ Lỗi {error_code_str}, chờ {backoff:.1f}s rồi retry {retry_count}/{max_retries} (đã dãn cách)")
					if not await self._sleep_with_stop(backoff):
						return
				elif is_auth_error:
					# ✅ Auth error → chờ random xíu để pool kịp phân bổ auth lại
					jitter = random.uniform(1.0, 4.0)
					self._log(f"⚠️ Lỗi 401, chờ {2 + jitter:.1f}s retry (auth {current_auth}/{auth_limit})")
					if not await self._sleep_with_stop(2 + jitter):
						return
				else:
					jitter = random.uniform(1.0, 3.0)
					self._log(f"⚠️ Lỗi {error_code_str}, chờ {wait_resend + jitter:.1f}s retry ({retry_count}/{max_retries})")
					if not await self._sleep_with_stop(wait_resend + jitter):
						return
				continue

			# ── Xử lý response thành công ──
			operations = self._parse_operations(response_body)
			if not operations and error_message:
				self._log(f"❌ API lỗi: {error_message[:100]}")
				for idx, sid in enumerate(scene_ids):
					self._update_state_entry(prompt_id, prompt_text, sid, idx, "FAILED", error=error_code, message=error_message)
					self.video_updated.emit({
						"prompt_idx": f"{prompt_id}_{idx + 1}", "status": "FAILED",
						"scene_id": sid, "prompt": prompt_text, "_prompt_id": prompt_id,
						"error_code": error_code_str, "error_message": error_message,
					})
				self._discard_scene_ids(prompt_id, scene_ids)
				return

			self._log(f"📨 Đã gửi video (prompt {prompt_id}), operations: {len(operations)}")
			self._handle_create_response(
				prompt_id, prompt_text, scene_ids, operations,
				access_token, session_id, project_id, response=response,
			)
			return  # Thành công, thoát

	async def _run_workflow(self):
		if self._should_stop():
			self._log("🛑 STOP trước khi chạy workflow")
			self._log("[DEBUG] _run_workflow: STOP trước khi chạy, emit automation_complete")
			self.automation_complete.emit()
			return
		
		# ✅ RESET FLAG CHO WORKFLOW MỚI
		self._all_prompts_submitted = False
		self._scene_status = {}
		self._scene_to_prompt = {}
		self._prompt_scene_order = {}
		self._state_status_logged = set()
		self._status_poll_fail_streak = 0
		self._last_status_change_ts = 0
		self._in_flight_block_start_ts = 0
		self._scene_next_check_at = {}
		self._scene_status_change_ts = {}
		self._state_status_logged = set()
		self._active_prompt_ids = set()
		
		# ✅ XÓA HếT Dữ LIẾU CŨ TRở KHÔNG PHẢI test.json
		self._cleanup_workflow_data()
		
		prompts = self._load_text_prompts()
		if not prompts:
			self._log("❌ Không có prompts text_to_video trong test.json")
			self._log("[DEBUG] _run_workflow: Không có prompts, emit automation_complete")
			self.automation_complete.emit()
			return
		self._active_prompt_ids = {str((p or {}).get("id", "")).strip() for p in prompts if str((p or {}).get("id", "")).strip()}

		auth = self._load_auth_config()
		if not auth:
			self._log("❌ Thiếu sessionId/projectId/access_token trong config.json")
			return

		t2v_api.refresh_account_context()

		import auth_helper
		self._log("🛂 Đang kiểm tra token OAuth...")
		new_token = auth_helper.get_valid_access_token(auth.get("cookie", ""), auth.get("projectId", ""))
		if new_token and new_token != auth.get("access_token"):
			self._log("✅ Token OAuth đã được làm mới tự động trước khi chạy workflow")
			auth["access_token"] = new_token

		session_id = auth["sessionId"]
		project_id = auth["projectId"]
		access_token = auth["access_token"]
		cookie = auth.get("cookie")
		project_link = auth.get("URL_GEN_TOKEN")
		chrome_userdata_root = auth.get("folder_user_data_get_token")

		config = SettingsManager.load_config()
		token_option = "Option 1"
		wait_gen_video = int(config.get("WAIT_GEN_VIDEO", 25))
		output_count = self._resolve_output_count(config)
		max_token_retries = self._resolve_int_config(config, "TOKEN_RETRY", 3)
		token_retry_delay = self._resolve_int_config(config, "TOKEN_RETRY_DELAY", 2)
		retry_with_error = self._resolve_int_config(config, "RETRY_WITH_ERROR", 3)
		wait_resend = self._resolve_int_config(config, "WAIT_RESEND_VIDEO", 20)  # ✅ Wait time for 403 retry
		wait_between_prompts = self._resolve_int_config(config, "WAIT_BETWEEN_PROMPTS", 3)  # ✅ Giảm delay vì mỗi Chrome dùng project riêng
		max_in_flight = self._resolve_worker_max_in_flight(self._resolve_int_config(config, "MULTI_VIDEO", 1))
		clear_data_every = self._resolve_int_config(config, "CLEAR_DATA", 1)
		clear_data_wait = self._resolve_int_config(config, "CLEAR_DATA_WAIT", 2)
		base_token_timeout = self._resolve_int_config(config, "TOKEN_TIMEOUT", 60)
		token_timeout = max(60, base_token_timeout)
		get_token_timeout = 150
		self._complete_wait_timeout = self._resolve_int_config(config, "WAIT_COMPLETE_TIMEOUT", 0)
		workflow_timeout = self._resolve_int_config(config, "WORKFLOW_TIMEOUT", 0)
		workflow_start_ts = time.time()
		video_aspect_ratio = self._resolve_video_aspect_ratio()
		video_model_key = self._resolve_video_model_key(video_aspect_ratio)

		profile_name = self.project_data.get("veo_profile")
		if not profile_name:
			profile_name = SettingsManager.load_settings().get("current_profile")

		if not project_link:
			project_link = "https://labs.google/fx/vi/tools/flow"
		if not chrome_userdata_root:
			chrome_userdata_root = SettingsManager.create_chrome_userdata_folder(profile_name)

		token_option = "Option 1"
		self._log(f"⚙️ Cấu hình chạy: MULTI_VIDEO={max_in_flight} | OUTPUT_COUNT={output_count}")


		status_task = asyncio.create_task(self._status_poll_loop(access_token, session_id, cookie, auth_dict=auth))

		prompt_retry_counts = {}
		token_request_count = 0
		self._complete_wait_start_ts = 0
		
		# ✅ WRAP TOKENCOLLECTOR STARTUP TRONG TIMEOUT ĐỂ TRÁNH HANG
		config = SettingsManager.load_config()
		num_chrome_cfg = max(1, int(config.get("NUM_CHROME", 1)))
		token_startup_timeout = 30 if num_chrome_cfg <= 1 else 240  # Tăng timeout vì cần tạo project riêng cho mỗi Chrome
		
		try:
			# ✅ Khởi động TokenCollector với timeout
			try:
				collector = await asyncio.wait_for(
					self._init_token_collector(
						project_link,
						chrome_userdata_root,
						profile_name,
						clear_data_wait,
						40,
						token_timeout,
					),
					timeout=token_startup_timeout
				)
			except asyncio.TimeoutError:
				self._log(f"❌ TokenCollector startup timeout ({token_startup_timeout}s) - Chrome không connect được CDP port")
				self._log("❌ Workflow dừng do không thể lấy token")
				if status_task:
					status_task.cancel()
				self._log("[DEBUG] _run_workflow: TokenCollector startup timeout, emit automation_complete")
				self.automation_complete.emit()
				return
			except Exception as e:
				self._log(f"❌ TokenCollector error: {e}")
				if status_task:
					status_task.cancel()
				self._log("[DEBUG] _run_workflow: TokenCollector error, emit automation_complete")
				self.automation_complete.emit()
				return
			# ✅ TokenCollector đã startup thành công, chạy workflow
			async with collector:
				# ✅ QUAN TRỌNG: Refresh auth từ Chrome browser ngay sau khi Chrome khởi động
				# Chrome có cookie hợp lệ, cập nhật config.json để tất cả API request dùng cookie mới
				self._collector_ref = collector  # Lưu reference cho status poll
				try:
					if hasattr(collector, 'refresh_auth_from_browser'):
						fresh_token, fresh_cookie = await collector.refresh_auth_from_browser(project_id)
						if fresh_token:
							access_token = fresh_token
							auth["access_token"] = fresh_token
							self._log("✅ Đã lấy access_token mới từ Chrome browser")
						if fresh_cookie:
							cookie = fresh_cookie
							auth["cookie"] = fresh_cookie
				except Exception as e:
					self._log(f"⚠️ Không refresh được auth từ Chrome: {e}")

				# ✅ Restart status_task với token mới (cancel cũ, tạo mới)
				if status_task:
					status_task.cancel()
					try:
						await status_task
					except (asyncio.CancelledError, Exception):
						pass
				status_task = asyncio.create_task(self._status_poll_loop(access_token, session_id, cookie, auth_dict=auth))

				# ✅ PROACTIVE TOKEN REFRESH: tự động refresh access_token mỗi 3 phút
				# Tránh 401 bằng cách refresh TRƯỚC KHI token hết hạn
				async def _proactive_token_refresh():
					"""Background task: refresh access_token mỗi 3 phút."""
					refresh_interval = 180  # 3 phút
					while not self._should_stop():
						await asyncio.sleep(refresh_interval)
						if self._should_stop():
							break
						try:
							if hasattr(collector, 'refresh_auth_from_browser'):
								self._log("🔄 [Proactive] Đang refresh access_token định kỳ...")
								fresh_token, fresh_cookie = await collector.refresh_auth_from_browser(project_id)
								if fresh_token:
									auth["access_token"] = fresh_token
									self._log("✅ [Proactive] access_token đã được refresh tự động!")
								if fresh_cookie:
									auth["cookie"] = fresh_cookie
						except asyncio.CancelledError:
							return
						except Exception as e:
							self._log(f"⚠️ [Proactive] Lỗi refresh token: {e}")
				
				proactive_refresh_task = asyncio.create_task(_proactive_token_refresh())

				# ✅ Xử lý SONG SONG: gửi nhiều prompt video cùng lúc
				pending_tasks = []
				
				for idx_prompt, prompt in enumerate(prompts):
					if self.STOP:
						self._log("🛑 STOP trong vòng lặp prompt, dừng workflow")
						break
					if workflow_timeout and (time.time() - workflow_start_ts) >= workflow_timeout:
						self._log("⏳ Workflow timeout, dừng Text to Video")
						break

					prompt_id = prompt["id"]
					prompt_text = prompt["prompt"]
					self._log(f"📝 Gửi prompt {prompt_id},(đầu vào {idx_prompt + 1}/{len(prompts)})")

					# Chờ slot trống nếu đạt giới hạn in-flight
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

					# Tạo task song song cho prompt này
					task = asyncio.create_task(self._process_video_prompt(
						collector, prompt_id, prompt_text,
						session_id, project_id, access_token, cookie, auth,
						video_model_key, video_aspect_ratio, output_count,
						token_option, max_token_retries, token_retry_delay,
						get_token_timeout, clear_data_every, retry_with_error,
						wait_resend, prompt_retry_counts,
					))
					pending_tasks.append(task)

					# Delay ngắn giữa các lần gửi
					if idx_prompt < len(prompts) - 1:
						if not await self._sleep_with_stop(wait_between_prompts):
							break

				# Đánh dấu đã gửi hết prompts
				if not self._auto_noi_canh:
					self._log("✅ Đã gửi hết prompts của workflow hiện tại, tiếp tục chờ video hoàn thành...")
				self._all_prompts_submitted = True

				# Chờ tất cả task gửi request hoàn tất
				if pending_tasks:
					await asyncio.gather(*pending_tasks, return_exceptions=True)
		except RuntimeError as exc:
			message = str(exc)
			if "URL GEN TOKEN" in message:
				self._log(message)
				try:
					QMessageBox.critical(None, "ERROR", message)
				except Exception:
					pass
				self.STOP = 1
				return

		# ✅ ĐẠT CỜ - HẾT TẤT CẢ PROMPTS, SẮP CHỜ HOÀN THÀNH
		if not self._all_prompts_submitted:
			if not self._auto_noi_canh:
				self._log("✅ Hết tất cả prompts, chờ video hoàn thành...")
			self._all_prompts_submitted = True
			self._complete_wait_start_ts = time.time()

		# ✅ INLINE RETRY CONSUMER: xử lý retry AUDIO_FILTERED ngay trong tiến trình
		# Giữ Chrome mở để lấy token cho retry
		inline_retry_deadline = time.time() + 600  # Tối đa 10 phút chờ retry
		while time.time() < inline_retry_deadline and not self.STOP:
			# Chờ có items trong retry queue hoặc tất cả video hoàn thành
			if not self._inline_retry_queue:
				# Kiểm tra xem còn video đang xử lý không
				in_progress = self._count_in_progress()
				if in_progress == 0:
					break  # Không còn video đang xử lý và queue trống → thoát
				await asyncio.sleep(5)
				continue

			# Lấy item từ queue
			retry_item = self._inline_retry_queue.pop(0)
			r_prompt_id = retry_item["prompt_id"]
			r_prompt_text = retry_item["prompt_text"]
			r_scene_id = retry_item["scene_id"]
			r_idx = retry_item["idx"]
			retry_count = self._inline_retry_counts.get(str(r_prompt_id), 1)

			self._log(f"🔁 Inline retry: gửi lại prompt {r_prompt_id} (lần {retry_count})...")

			# Lấy token mới
			token = None
			try:
				token = await asyncio.wait_for(
					collector.get_token(clear_storage=False),
					timeout=get_token_timeout,
				)
			except Exception as e:
				self._log(f"⚠️ Inline retry: không lấy được token cho prompt {r_prompt_id}: {e}")

			if not token:
				self._log(f"⚠️ Inline retry: token rỗng cho prompt {r_prompt_id}, đánh FAILED")
				self._scene_status.setdefault(r_scene_id, {})["status"] = "MEDIA_GENERATION_STATUS_FAILED"
				self._update_state_entry(r_prompt_id, r_prompt_text, r_scene_id, r_idx, "FAILED",
					error="TOKEN", message="Không lấy được token cho retry")
				self.video_updated.emit({
					"prompt_idx": f"{r_prompt_id}_{r_idx + 1}", "status": "FAILED",
					"scene_id": r_scene_id, "prompt": r_prompt_text, "_prompt_id": r_prompt_id,
					"error_code": "TOKEN", "error_message": "Không lấy được token cho retry",
				})
				continue

			# Build payload và gửi lại (giống main loop)
			try:
				payload = t2v_api.build_create_payload(
					r_prompt_text, session_id, project_id, token,
					model_key=video_model_key, aspect_ratio=video_aspect_ratio,
					output_count=output_count,
				)
				new_scene_ids = self._assign_scene_ids(payload, r_prompt_id, output_count)
				self._save_request_json(payload, r_prompt_id, r_prompt_text, flow="text_to_video_retry")

				self._log(f"🚀 Inline retry: gửi request tạo video prompt {r_prompt_id}...")
				# ✅ Lấy access_token mới nhất từ config trước khi retry
				try:
					retry_auth = self._load_auth_config()
					if retry_auth and retry_auth.get("access_token"):
						access_token = retry_auth["access_token"]
					if retry_auth and retry_auth.get("cookie"):
						cookie = retry_auth["cookie"]
				except Exception:
					pass
				
				# ✅ Ưu tiên gửi qua browser (via_browser) để có cookies đúng
				page_ref = None
				collector_ref = getattr(self, '_collector_ref', None)
				if collector_ref:
					if hasattr(collector_ref, '_collectors'):
						for c in collector_ref._collectors:
							if c and getattr(c, 'page', None) and not c.page.is_closed():
								page_ref = c.page
								break
					elif hasattr(collector_ref, 'page') and collector_ref.page and not collector_ref.page.is_closed():
						page_ref = collector_ref.page
				
				if page_ref and not page_ref.is_closed():
					response = await t2v_api.request_create_video_via_browser(
						page_ref, t2v_api.URL_GENERATE_TEXT_TO_VIDEO, payload, access_token,
					)
				else:
					response = await t2v_api.request_create_video(payload, access_token, cookie=cookie)

				response_body = response.get("body", "")
				operations = self._parse_operations(response_body)
				err_code, err_msg = self._extract_error_info(response_body)

				if operations:
					self._handle_create_response(
						r_prompt_id, r_prompt_text, new_scene_ids, operations,
						access_token, session_id, project_id, response=response,
					)
					self._log(f"✅ Inline retry: đã gửi lại prompt {r_prompt_id} thành công")
				else:
					self._log(f"❌ Inline retry: prompt {r_prompt_id} thất bại: {err_msg}")
					for idx2, sid in enumerate(new_scene_ids):
						self._scene_status.setdefault(sid, {})["status"] = "MEDIA_GENERATION_STATUS_FAILED"
						self._update_state_entry(r_prompt_id, r_prompt_text, sid, idx2, "FAILED",
							error=err_code, message=err_msg)
						self.video_updated.emit({
							"prompt_idx": f"{r_prompt_id}_{idx2 + 1}", "status": "FAILED",
							"scene_id": sid, "prompt": r_prompt_text, "_prompt_id": r_prompt_id,
							"error_code": err_code, "error_message": err_msg,
						})
			except Exception as e:
				self._log(f"❌ Inline retry: lỗi gửi lại prompt {r_prompt_id}: {e}")

			await asyncio.sleep(3)

		# ✅ Kiểm tra hoàn thành video và thoát luồng ngay khi xong
		try:
			await self._wait_for_completion()
			self._log("[DEBUG] _run_workflow: Đã hoàn thành tất cả video, huỷ status_task")
		except Exception as e:
			self._log(f"[DEBUG] _run_workflow: Exception in _wait_for_completion: {e}")
		if status_task:
			status_task.cancel()
		# ✅ Cancel proactive refresh task
		try:
			proactive_refresh_task.cancel()
		except Exception:
			pass
		
		# ✅ AUTO-RETRY: Sau khi hoàn thành, tự động retry các prompt bị lỗi (TOKEN/400/429/403)
		if not self.STOP and not self._auto_noi_canh:
			failed_prompts = self._collect_retryable_failed_prompts()
			if failed_prompts:
				self._log(f"🔁 Auto-retry: có {len(failed_prompts)} prompt bị lỗi, restart Chrome và thử lại...")
				try:
					await collector.restart_browser()
					self._log("✅ Chrome đã restart, bắt đầu retry prompts lỗi...")
					
					# ✅ QUAN TRỌNG: Restart status poll loop để theo dõi video retry
					# Status poll đã bị cancel ở trên, cần khởi động lại
					# để state.json được cập nhật khi video retry hoàn thành
					try:
						# Refresh auth trước khi tạo status poll mới
						_fresh_auth = self._load_auth_config()
						if _fresh_auth:
							if _fresh_auth.get("access_token"):
								access_token = _fresh_auth["access_token"]
								auth["access_token"] = access_token
							if _fresh_auth.get("cookie"):
								cookie = _fresh_auth["cookie"]
								auth["cookie"] = cookie
					except Exception:
						pass
					
					status_task = asyncio.create_task(
						self._status_poll_loop(access_token, session_id, cookie, auth_dict=auth)
					)
					self._log("[POLL] Đã restart status poll loop cho auto-retry")
					
					# Reset retry counts cho lần retry mới
					retry_prompt_retry_counts = {}
					retry_tasks = []
					
					for retry_item in failed_prompts:
						if self.STOP:
							break
						r_prompt_id = retry_item["prompt_id"]
						r_prompt_text = retry_item["prompt_text"]
						
						self._log(f"🔁 Auto-retry prompt {r_prompt_id}...")
						task = asyncio.create_task(self._process_video_prompt(
							collector, r_prompt_id, r_prompt_text,
							session_id, project_id, access_token, cookie, auth,
							video_model_key, video_aspect_ratio, output_count,
							token_option, max_token_retries, token_retry_delay,
							get_token_timeout, clear_data_every, retry_with_error,
							wait_resend, retry_prompt_retry_counts,
						))
						retry_tasks.append(task)
						
						# Delay giữa các retry
						if not await self._sleep_with_stop(wait_between_prompts):
							break
					
					if retry_tasks:
						self._log(f"⏳ Đang chờ {len(retry_tasks)} prompt retry hoàn thành...")
						await asyncio.gather(*retry_tasks, return_exceptions=True)
					
					# ✅ Đánh dấu đã gửi hết retry prompts để _wait_for_completion biết khi nào thoát
					self._all_prompts_submitted = True
					
					# Chờ video retry hoàn thành (status poll sẽ cập nhật state.json)
					self._complete_wait_start_ts = time.time()
					try:
						await self._wait_for_completion()
					except Exception:
						pass
					
					# Cancel status poll sau khi retry xong
					if status_task:
						status_task.cancel()
				except Exception as e:
					self._log(f"⚠️ Auto-retry lỗi: {e}")

		# ✅ CHỈ đóng Chrome SAU KHI tất cả video đã hoàn thành
		chrome_closed = False
		if (not self._auto_noi_canh) and (not self._worker_controls_lifecycle):
			try:
				await collector.close_after_workflow()
				self._log("🔒 Đã đóng Chrome sau khi hoàn thành tất cả video")
				chrome_closed = True
			except Exception:
				self._log("⚠️ Lỗi dừng harvest token")
		
		self._log("[DEBUG] _run_workflow: Workflow kết thúc, emit automation_complete")
		self.automation_complete.emit()

	async def _run_resend_workflow(self, resend_items):
		"""Resend workflow cho video đã chọn - resend_items = [(prompt_id, prompt_text, scene_id, idx), ...]"""
		if self._should_stop():
			self._log("🛑 STOP trước khi chạy resend workflow")
			return

		# ✅ RESET FLAG CHO RESEND WORKFLOW MỚI
		self._all_prompts_submitted = False
		self._scene_status = {}
		self._scene_to_prompt = {}
		self._prompt_scene_order = {}
		self._active_prompt_ids = {str(item[0]).strip() for item in (resend_items or []) if str(item[0]).strip()}

		auth = self._load_auth_config()
		if not auth:
			self._log("❌ Thiếu sessionId/projectId/access_token trong config.json")
			return

		t2v_api.refresh_account_context()

		session_id = auth["sessionId"]
		project_id = auth["projectId"]
		access_token = auth["access_token"]
		cookie = auth.get("cookie")
		project_link = auth.get("URL_GEN_TOKEN")
		chrome_userdata_root = auth.get("folder_user_data_get_token")

		config = SettingsManager.load_config()
		token_option = "Option 1"
		wait_gen_video = int(config.get("WAIT_GEN_VIDEO", 25))
		output_count = self._resolve_output_count(config)
		max_token_retries = self._resolve_int_config(config, "TOKEN_RETRY", 3)
		token_retry_delay = self._resolve_int_config(config, "TOKEN_RETRY_DELAY", 2)
		retry_with_error = self._resolve_int_config(config, "RETRY_WITH_ERROR", 3)
		wait_resend = self._resolve_int_config(config, "WAIT_RESEND_VIDEO", 20)  # ✅ Wait time for 403 retry
		wait_between_prompts = self._resolve_int_config(config, "WAIT_BETWEEN_PROMPTS", 12)  # ✅ Delay giữa các prompt
		max_in_flight = self._resolve_worker_max_in_flight(self._resolve_int_config(config, "MULTI_VIDEO", 1))
		clear_data_every = self._resolve_int_config(config, "CLEAR_DATA", 1)
		clear_data_wait = self._resolve_int_config(config, "CLEAR_DATA_WAIT", 2)
		base_token_timeout = self._resolve_int_config(config, "TOKEN_TIMEOUT", 60)
		token_timeout = max(60, base_token_timeout)
		get_token_timeout = 150
		self._complete_wait_timeout = self._resolve_int_config(config, "WAIT_COMPLETE_TIMEOUT", 0)
		video_aspect_ratio = self._resolve_video_aspect_ratio()
		video_model_key = self._resolve_video_model_key(video_aspect_ratio)

		profile_name = self.project_data.get("veo_profile")
		if not profile_name:
			profile_name = SettingsManager.load_settings().get("current_profile")

		if not project_link:
			project_link = "https://labs.google/fx/vi/tools/flow"
		if not chrome_userdata_root:
			chrome_userdata_root = SettingsManager.create_chrome_userdata_folder(profile_name)

		token_option = "Option 1"
		self._log(f"⚙️ Cấu hình resend: MULTI_VIDEO={max_in_flight} | OUTPUT_COUNT=1")

		self._log(f"📝 Số video cần gen lại: {len(resend_items)}")

		status_task = asyncio.create_task(self._status_poll_loop(access_token, session_id, cookie, auth_dict=auth))
		prompt_retry_counts = {}
		token_request_count = 0
		self._complete_wait_start_ts = 0

		# ✅ WRAP TOKENCOLLECTOR STARTUP TRONG TIMEOUT ĐỂ TRÁNH HANG
		token_startup_timeout = 30  # 30 seconds để Chrome startup + connect CDP
		
		try:
			# ✅ Khởi động TokenCollector với timeout
			try:
				collector = await asyncio.wait_for(
					self._init_token_collector(
						project_link,
						chrome_userdata_root,
						profile_name,
						clear_data_wait,
						40,
						token_timeout,
					),
					timeout=token_startup_timeout
				)
			except asyncio.TimeoutError:
				self._log(f"❌ TokenCollector startup timeout ({token_startup_timeout}s) - Chrome không connect được CDP port")
				self._log("❌ Resend workflow dừng do không thể lấy token")
				if status_task:
					status_task.cancel()
				return
			except Exception as e:
				self._log(f"❌ TokenCollector error: {e}")
				if status_task:
					status_task.cancel()
				return
			
			# ✅ TokenCollector đã startup thành công, chạy workflow
			async with collector:
				for prompt_id, prompt_text, scene_id, idx in resend_items:
					if self.STOP:
						self._log("🛑 STOP trong resend workflow")
						break

					# ✅ KIỂM TRA IN-FLIGHT LIMIT TRƯỚC KHI GỬI RESEND REQUEST
					wait_start_ts = time.time()
					while self._count_in_progress() >= max_in_flight:
						if self.STOP:
							self._log("🛑 STOP trong lúc chờ, dừng resend")
							break
						if not self._check_in_flight_block():
							break
						elapsed = int(time.time() - wait_start_ts)
						self._log(f"⏳ Đang tạo video đủ giới hạn {max_in_flight}, chờ {elapsed}s...")
						if not await self._sleep_with_stop(5):
							break
					if self.STOP:
						break
					if self._count_in_progress() < max_in_flight:
						self._in_flight_block_start_ts = 0

					for resend_attempt in range(retry_with_error):
						if self.STOP:
							self._log("🛑 STOP, dừng resend")
							break

						token = None
						for attempt in range(max_token_retries):
							if self.STOP:
								break
							try:
								self._log(
									f"🔐 Đang lấy token resend... prompt {prompt_id} | mode=video | lần {attempt + 1}/{max_token_retries}"
								)
								# ✅ Timeout get_token() để tránh treo vô thời hạn
								token_request_count += 1
								clear_storage = (
									clear_data_every > 0
									and (token_request_count % clear_data_every == 0)
								)
								token = await asyncio.wait_for(
									collector.get_token(clear_storage=clear_storage),
									timeout=get_token_timeout
								)
								if token:
									self._log("✅ Lấy token resend thành công, đã chọn đúng mode lấy token: video")
									break
							except asyncio.TimeoutError:
								self._log(f"⏱️ Timeout lấy token (lần {attempt + 1})")
								timeout_streak = prompt_retry_counts.get("_token_timeout_streak", 0) + 1
								prompt_retry_counts["_token_timeout_streak"] = timeout_streak
								if timeout_streak >= 2:
									self._log("⚠️ Timeout lấy token liên tiếp, khởi động lại Chrome...")
									await collector.restart_browser()
									prompt_retry_counts["_token_timeout_streak"] = 0
							except Exception as e:
								self._log(f"⚠️ Lỗi lấy token: {e}")
							if attempt < max_token_retries - 1:
								if self.STOP:
									break
								if not await self._sleep_with_stop(token_retry_delay):
									break

						if not token:
							self._log(f"❌ Không lấy được token (prompt {prompt_id})")
							self._update_state_entry(
								prompt_id,
								prompt_text,
								scene_id,
								idx,
								"FAILED",
								error="TOKEN",
								message="Token timeout",
							)
							self.video_updated.emit({
								"prompt_idx": f"{prompt_id}_{idx + 1}",
								"status": "FAILED",
								"scene_id": scene_id,
								"prompt": prompt_text,
								"_prompt_id": prompt_id,
								"error_code": "TOKEN",
								"error_message": "Token timeout",
							})
							break
						if self._should_stop():
							self._log(f"🛑 STOP sau khi lấy token (prompt {prompt_id})")
							break
						
						payload = t2v_api.build_create_payload(
							prompt_text,
							session_id,
							project_id,
							token,
							model_key=video_model_key,
							aspect_ratio=video_aspect_ratio,
							output_count=1,
						)
						if self._should_stop():
							self._log(f"🛑 STOP trước khi gửi request (prompt {prompt_id})")
							break

						if payload.get("requests"):
							payload["requests"][0].setdefault("metadata", {})["sceneId"] = scene_id
						self._save_request_json(payload, prompt_id, prompt_text, flow="text_to_video_resend")

						self._log(f"🚀 [{time.strftime('%H:%M:%S')}] Gen lại request (prompt {prompt_id}, scene {scene_id[:8]})...")
						
						# ✅ LUÔN reload access_token mới nhất từ config trước mỗi resend request
						try:
							_fresh = self._load_auth_config()
							if _fresh and _fresh.get("access_token"):
								access_token = _fresh["access_token"]
							if _fresh and _fresh.get("cookie"):
								cookie = _fresh["cookie"]
						except Exception:
							pass
						
						self._log(f"🔧 Token Option: {token_option}")
						
						# ✅ Lấy page_ref từ Chrome đã sinh ra token (giống Character Sync)
						page_ref = None
						if hasattr(collector, "_token_to_idx"):
							instance_idx = collector._token_to_idx.get(token)
							if instance_idx is not None:
								colls = getattr(collector, "_collectors", [])
								if instance_idx < len(colls):
									c = colls[instance_idx]
									if c and getattr(c, "page", None) and not c.page.is_closed():
										page_ref = c.page
						elif hasattr(collector, "page") and collector.page and not collector.page.is_closed():
							page_ref = collector.page

						if page_ref and not page_ref.is_closed():
							response = await t2v_api.request_create_video_via_browser(
								page_ref,
								t2v_api.URL_GENERATE_TEXT_TO_VIDEO,
								payload,
								access_token
							)
						else:
							response = await t2v_api.request_create_video(payload, access_token, cookie=cookie)
						
						if self._should_stop():
							self._log(f"🛑 STOP sau khi gửi request (prompt {prompt_id})")
							break
						response_body = response.get("body", "")
						error_code, error_message = self._extract_error_info(response_body)
						retryable_errors = {"403", "3", "13", "53", "16", "401", "429", "400", "500", "503"}
						error_code_str = str(error_code or "").strip()

						# ✅ Handle 403 error: 2 lần = clear storage, 3+ lần = restart chrome
						if not response.get("ok", True) and error_code_str in retryable_errors:
							if self.STOP:
								break
							
							# Track consecutive 403 per scene_id
							scene_key = f"{prompt_id}_{scene_id}"
							if error_code_str == "403":
								consecutive_403_count = prompt_retry_counts.get(f"{scene_key}_403_count", 0) + 1
								prompt_retry_counts[f"{scene_key}_403_count"] = consecutive_403_count
							else:
								prompt_retry_counts[f"{scene_key}_403_count"] = 0
								consecutive_403_count = 0
							
							# 🔧 Lần 2 consecutive 403: clear storage (có cooldown để tránh clear liên tục)
							if error_code_str == "403" and consecutive_403_count == 2:
								cooldown_key = f"{scene_key}_403_clear_cooldown_until"
								cooldown_until = float(prompt_retry_counts.get(cooldown_key, 0) or 0)
								now_ts = time.time()
								if now_ts < cooldown_until:
									self._log("⚠️ Vừa clear storage gần đây, bỏ qua clear và restart Chrome...")
									await collector.restart_browser()
									prompt_retry_counts[f"{scene_key}_403_count"] = 0
									continue
								self._log(f"⚠️ Lỗi 403 lần {consecutive_403_count}, chạy clear storage...")
								try:
									await asyncio.wait_for(
										collector.get_token(clear_storage=True),
										timeout=60
									)
									prompt_retry_counts[f"{scene_key}_403_count"] = 0
									prompt_retry_counts[cooldown_key] = time.time() + 120
									token_request_count = 0
									self._log("✅ Clear storage xong, retry request")
								except Exception as e:
									self._log(f"⚠️ Clear storage lỗi: {e}")
								if not await self._sleep_with_stop(wait_resend):
									break
								continue
							
							# 🔧 Lần 3+ consecutive 403: restart chrome
							if error_code_str == "403" and consecutive_403_count >= 3:
								self._log("⚠️ Lỗi 403 liên tiếp, khởi động lại Chrome...")
								await collector.restart_browser()
								prompt_retry_counts[f"{scene_key}_403_count"] = 0
								continue
							
							# Other retryable errors
							self._log(
								f"⚠️ Lỗi {error_code_str or 'UNKNOWN'}, chờ {wait_resend}s rồi retry ({resend_attempt + 1}/{retry_with_error})"
							)
							if not await self._sleep_with_stop(wait_resend):
								break
							continue

						operations = self._parse_operations(response_body)

						if not operations and error_message:
							self._log(f"❌ API lỗi: {error_message[:100]}")
							self._update_state_entry(
								prompt_id,
								prompt_text,
								scene_id,
								idx,
								"FAILED",
								error=error_code,
								message=error_message,
							)
							self.video_updated.emit({
								"prompt_idx": f"{prompt_id}_{idx + 1}",
								"status": "FAILED",
								"scene_id": scene_id,
								"prompt": prompt_text,
								"_prompt_id": prompt_id,
								"error_code": error_code_str,
								"error_message": error_message,
							})
							break

						self._log(f"📨 Resend create video (prompt {prompt_id}), operations: {len(operations)}")
						self._scene_status[scene_id] = {
							"status": "MEDIA_GENERATION_STATUS_PENDING",
							"operation_name": "",
						}
						self._scene_to_prompt[scene_id] = {"prompt_id": prompt_id, "index": idx}

						for op in operations:
							op_scene_id = op.get("sceneId")
							if op_scene_id == scene_id:
								status = op.get("status") or "MEDIA_GENERATION_STATUS_PENDING"
								op_name = op.get("operation", {}).get("name", "")
								self._scene_status[scene_id]["operation_name"] = op_name
								self._update_state_entry(
									prompt_id,
									prompt_text,
									scene_id,
									idx,
									self._short_status(status),
								)
								self._last_status_change_ts = time.time()
								self._scene_next_check_at[scene_id] = time.time() + 6
								self._scene_status_change_ts[scene_id] = time.time()

						current_running = self._count_in_progress()
					# ✅ Delay ngắn 2s giữa các prompt, chờ slot trống nếu đạt giới hạn
					if not await self._sleep_with_stop(2):
						break
					while self._count_in_progress() >= max_in_flight:
						if self._should_stop():
							break
						self._log(f"⏳ Đang chờ {self._count_in_progress()} video hoàn thành...")
						if not await self._sleep_with_stop(5):
							break
					break

		except RuntimeError as exc:
			message = str(exc)
			if "URL GEN TOKEN" in message:
				self._log(message)
				self.STOP = 1
				return

		# ✅ ĐẠT CỜ - HẾT TẤT CẢ ITEMS RESEND, SẮP CHỜ HOÀN THÀNH
		if not self._auto_noi_canh:
			self._log("✅ Hết tất cả items resend, chờ video hoàn thành...")
		self._all_prompts_submitted = True
		self._complete_wait_start_ts = time.time()
		
		# ✅ GIỮ Chrome mở đến khi TẤT CẢ video hoàn thành (tránh 401 khi poll status)
		await self._wait_for_completion()
		if status_task:
			status_task.cancel()
		
		# ✅ CHỈ đóng Chrome SAU KHI tất cả video đã hoàn thành
		if (not self._auto_noi_canh) and (not self._worker_controls_lifecycle):
			try:
				await collector.close_after_workflow()
				self._log("🔒 Đã đóng Chrome sau khi hoàn thành tất cả video")
			except Exception:
				pass

	def get_failed_scenes(self):
		"""Lấy danh sách video lỗi từ state.json - return [(prompt_id, prompt_text, scene_id, idx), ...]"""
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

			for idx, status in enumerate(statuses):
				if status and status != "SUCCESSFUL":
					scene_id = scene_ids[idx] if idx < len(scene_ids) else ""
					if scene_id:
						failed_items.append((prompt_id, prompt_text, scene_id, idx))

		return failed_items

	def run_resend(self, resend_items):
		"""Start resend workflow với danh sách items được chọn"""
		loop = asyncio.new_event_loop()
		asyncio.set_event_loop(loop)
		try:
			loop.run_until_complete(self._run_resend_workflow(resend_items))
		except Exception as exc:
			self.log_message.emit(f"❌ Lỗi resend workflow: {exc}")
			self.log_message.emit(traceback.format_exc()[:200])
		finally:
			try:
				loop.close()
			except Exception:
				pass
			if self.STOP:
				self._log("🛑 Stop (Chrome vẫn mở để tái sử dụng)")
			self.automation_complete.emit()

	def _resolve_output_count(self, config):
		value = self.project_data.get("output_count")
		if value is None or value == "":
			value = config.get("OUTPUT_COUNT", 1)
		try:
			count = int(value)
		except Exception:
			count = 1
		return max(1, count)

	def _resolve_int_config(self, config, key, default_value):
		value = config.get(key, default_value)
		try:
			return int(value)
		except Exception:
			return default_value

	def _resolve_video_aspect_ratio(self):
		aspect_ratio = str(self.project_data.get("aspect_ratio", "")).lower()
		is_portrait = "dọc" in aspect_ratio or "9:16" in aspect_ratio or "portrait" in aspect_ratio
		if is_portrait:
			return t2v_api.VIDEO_ASPECT_RATIO_PORTRAIT
		return t2v_api.VIDEO_ASPECT_RATIO_LANDSCAPE

	def _resolve_video_model_key(self, video_aspect_ratio):
		# Refresh to pick up current account tier (NORMAL/PRO => non-ultra)
		t2v_api.refresh_account_context()
		veo_model = self.project_data.get("veo_model", "")
		return t2v_api.select_video_model_key(video_aspect_ratio, veo_model)

	def _load_text_prompts(self):
		if self.project_data.get("_use_project_prompts"):
			items = self.project_data.get("prompts", {}).get("text_to_video", [])
			return self._build_prompt_list(items)

		test_file = WORKFLOWS_DIR / self.project_name / "test.json"
		if test_file.exists():
			try:
				with open(test_file, "r", encoding="utf-8") as f:
					data = json.load(f)
				items = data.get("prompts", {}).get("text_to_video", [])
			except Exception:
				items = []
		else:
			items = self.project_data.get("prompts", {}).get("text_to_video", [])

		return self._build_prompt_list(items)

	def _build_prompt_list(self, items):
		prompts_list = []

		for item in items:
			prompt_id = item.get("id")
			prompt_text = item.get("description") or item.get("prompt") or ""
			if prompt_id and prompt_text:
				prompts_list.append({"id": prompt_id, "prompt": prompt_text})
		return prompts_list

	def _load_auth_config(self):
		config = SettingsManager.load_config()
		account = config.get("account1", {}) if isinstance(config, dict) else {}
		session_id = account.get("sessionId")
		project_id = account.get("projectId")
		access_token = account.get("access_token")
		cookie = account.get("cookie")
		# ✅ Lấy URL_GEN_TOKEN và folder_user_data từ account1
		url_gen_token = account.get("URL_GEN_TOKEN")
		folder_user_data = account.get("folder_user_data_get_token")
		if not (session_id and project_id and access_token):
			return None
		return {
			"sessionId": session_id,
			"projectId": project_id,
			"access_token": access_token,
			"cookie": cookie,
			"URL_GEN_TOKEN": url_gen_token,
			"folder_user_data_get_token": folder_user_data,
		}

	def _get_state_file_path(self):
		state_dir = WORKFLOWS_DIR / self.project_name
		state_dir.mkdir(parents=True, exist_ok=True)
		return state_dir / "state.json"

	def _load_state_json(self):
		state_file = self._get_state_file_path()
		if not state_file.exists():
			return {}
		try:
			with open(state_file, "r", encoding="utf-8") as f:
				return json.load(f)
		except Exception:
			return {}

	def _cleanup_workflow_data(self):
		"""✅ Xóa tất cả dữ liệu cũ trong folder Workflows/{project} NGOẠI TRỪ test.json"""
		try:
			self._save_state_json({})
			if self.project_data.get("_skip_clear_download"):
				self._log("🧹 Giữ file download cũ, đã reset state.json cho phiên chạy mới")
				return

			project_dir = WORKFLOWS_DIR / self.project_name
			if not project_dir.exists():
				return
			
			self._log("🧹 Dọn dữ liệu cũ (giữ test.json, status.json, Download)...")
			keep_files = {"test.json", "status.json"}
			keep_dirs = {"Download", "thumbnails"}
			
			# Duyệt tất cả files/folders trong project_dir
			for item in project_dir.iterdir():
				if item.name in keep_files or item.name in keep_dirs:
					continue
				
				# Xóa files
				if item.is_file():
					try:
						item.unlink()
						self._log(f"  ✓ Xóa file: {item.name}")
					except Exception as e:
						self._log(f"  ⚠️ Không xóa được {item.name}: {e}")
				
				# Xóa folders
				elif item.is_dir():
					try:
						import shutil
						shutil.rmtree(item)
						self._log(f"  ✓ Xóa folder: {item.name}")
					except Exception as e:
						self._log(f"  ⚠️ Không xóa được folder {item.name}: {e}")
			
			self._log("✅ Dữ liệu cũ đã dọn, giữ nguyên file media cũ")
		except Exception as e:
			self._log(f"⚠️ Lỗi cleanup data: {e}")

	def _count_in_progress_from_state(self):
		state_data = self._load_state_json()
		prompts = state_data.get("prompts", {}) if isinstance(state_data, dict) else {}
		count = 0
		running_markers = {"PENDING", "ACTIVE", "REQUESTED", "DOWNLOADING", "TOKEN", "QUEUED", "SUBMIT", "CREATING", "GENERATING"}
		active_prompt_ids = {str(pid).strip() for pid in (self._active_prompt_ids or set()) if str(pid).strip()}
		for prompt_key, prompt_data in prompts.items():
			if active_prompt_ids and str(prompt_key).strip() not in active_prompt_ids:
				continue
			statuses = prompt_data.get("statuses", []) if isinstance(prompt_data, dict) else []
			prompt_running = False
			for status in statuses:
				status_upper = str(status or "").upper()
				if any(marker in status_upper for marker in running_markers):
					prompt_running = True
					break
			if prompt_running:
				count += 1
		return count

	def _count_in_progress(self):
		return int(self._count_in_progress_from_state())

	def _resolve_worker_max_in_flight(self, fallback_value):
		return max(1, int(get_max_in_flight(default_value=int(fallback_value or 1))))

	def _save_state_json(self, state_data):
		state_file = self._get_state_file_path()
		try:
			with open(state_file, "w", encoding="utf-8") as f:
				json.dump(state_data, f, ensure_ascii=False, indent=2)
			return True
		except Exception:
			return False

	def _ensure_prompt_entry(self, state_data, prompt_id, prompt_text):
		if "prompts" not in state_data:
			state_data["prompts"] = {}
		prompt_key = str(prompt_id)
		if prompt_key not in state_data["prompts"]:
			state_data["prompts"][prompt_key] = {
				"id": prompt_id,
				"prompt": prompt_text,
				"scene_ids": [],
				"statuses": [],
				"image_paths": [],
				"video_paths": [],
				"image_urls": [],
				"video_urls": [],
				"errors": [],
				"messages": [],
				"created_at": "",
			}
		return state_data["prompts"][prompt_key]

	def _update_state_entry(self, prompt_id, prompt_text, scene_id, idx, status, video_url="", image_url="", video_path="", image_path="", error="", message=""):
		state_data = self._load_state_json()
		prompt_data = self._ensure_prompt_entry(state_data, prompt_id, prompt_text)

		if "scene_id_map" not in state_data:
			state_data["scene_id_map"] = {}

		while len(prompt_data["scene_ids"]) <= idx:
			prompt_data["scene_ids"].append("")
		prompt_data["scene_ids"][idx] = scene_id
		state_data["scene_id_map"][scene_id] = prompt_id

		while len(prompt_data["statuses"]) <= idx:
			prompt_data["statuses"].append("PENDING")
		prompt_data["statuses"][idx] = status

		while len(prompt_data["video_paths"]) <= idx:
			prompt_data["video_paths"].append("")
		if video_path:
			prompt_data["video_paths"][idx] = video_path

		while len(prompt_data["image_paths"]) <= idx:
			prompt_data["image_paths"].append("")
		if image_path:
			prompt_data["image_paths"][idx] = image_path

		while len(prompt_data["video_urls"]) <= idx:
			prompt_data["video_urls"].append("")
		if video_url:
			prompt_data["video_urls"][idx] = video_url

		while len(prompt_data["image_urls"]) <= idx:
			prompt_data["image_urls"].append("")
		if image_url:
			prompt_data["image_urls"][idx] = image_url

		while len(prompt_data["errors"]) <= idx:
			prompt_data["errors"].append("")
		# ✅ LUÔN CẬP NHẬT ERROR - nếu không có error mới thì clear lại thành ""
		prompt_data["errors"][idx] = error if error else ""

		# ✅ Đồng bộ thêm error_codes/error_messages để UI cũ đọc được
		if "error_codes" not in prompt_data:
			prompt_data["error_codes"] = []
		while len(prompt_data["error_codes"]) <= idx:
			prompt_data["error_codes"].append("")
		prompt_data["error_codes"][idx] = error if error else ""

		while len(prompt_data["messages"]) <= idx:
			prompt_data["messages"].append("")
		# ✅ LUÔN CẬP NHẬT MESSAGE - nếu không có message mới thì clear lại thành ""
		prompt_data["messages"][idx] = message if message else ""

		if "error_messages" not in prompt_data:
			prompt_data["error_messages"] = []
		while len(prompt_data["error_messages"]) <= idx:
			prompt_data["error_messages"].append("")
		prompt_data["error_messages"][idx] = message if message else ""

		self._save_state_json(state_data)
		status_key = f"{prompt_id}:{status}"
		if status_key not in self._state_status_logged:
			self._state_status_logged.add(status_key)
			self._log(f"🧾 Update state: prompt {prompt_id} -> {status}")

	def _save_auth_to_state(self, access_token, session_id, project_id):
		state_data = self._load_state_json()
		state_data["auth"] = {
			"access_token": access_token,
			"sessionId": session_id,
			"projectId": project_id,
		}
		self._save_state_json(state_data)

	def _assign_scene_ids(self, payload, prompt_id, output_count):
		scene_ids = []
		requests = payload.get("requests", [])
		for idx, req in enumerate(requests):
			scene_id = str(uuid.uuid4())
			req.setdefault("metadata", {})["sceneId"] = scene_id
			scene_ids.append(scene_id)
		self._prompt_scene_order[prompt_id] = scene_ids
		for idx, scene_id in enumerate(scene_ids):
			self._scene_to_prompt[scene_id] = {"prompt_id": prompt_id, "index": idx}
			self._scene_status[scene_id] = {
				"status": "MEDIA_GENERATION_STATUS_PENDING",
				"operation_name": "",
			}
			self._scene_next_check_at[scene_id] = time.time() + 999999
		return scene_ids

	def _discard_scene_ids(self, prompt_id, scene_ids):
		for scene_id in scene_ids:
			self._scene_status.pop(scene_id, None)
			self._scene_to_prompt.pop(scene_id, None)
		if prompt_id in self._prompt_scene_order:
			self._prompt_scene_order[prompt_id] = [
				sid for sid in self._prompt_scene_order[prompt_id] if sid not in scene_ids
			]

	def _parse_operations(self, response_body):
		try:
			body_json = json.loads(response_body)
			return body_json.get("operations", [])
		except Exception:
			return []

	def _extract_error_info(self, response_body):
		try:
			body_json = json.loads(response_body)
		except Exception:
			return "", ""
		error = body_json.get("error") if isinstance(body_json, dict) else None
		if not isinstance(error, dict):
			return "", ""
		code = str(error.get("code", "")) if error.get("code") is not None else ""
		message = str(error.get("message", "")) if error.get("message") is not None else ""
		return code, message

	def _handle_create_response(
		self,
		prompt_id,
		prompt_text,
		scene_ids,
		operations,
		access_token,
		session_id,
		project_id,
		response=None,
	):
		error_code = ""
		error_message = ""
		if response and not response.get("ok", True):
			response_body = response.get("body", "")
			error_code, error_message = self._extract_error_info(response_body)
			if error_message:
				self._log(f"❌ Create API lỗi: {error_message}")

		op_map = {}
		for op in operations:
			scene_id = op.get("sceneId")
			if scene_id:
				op_map[scene_id] = op

		self._save_auth_to_state(access_token, session_id, project_id)

		for idx, scene_id in enumerate(scene_ids):
			op = op_map.get(scene_id)
			if op is None and idx < len(operations):
				op = operations[idx]
			if not isinstance(op, dict):
				op = {}
			status = self._normalize_status_full(op.get("status") or "MEDIA_GENERATION_STATUS_PENDING")
			operation_name = op.get("operation", {}).get("name", "")
			
			if not operation_name or (response and not response.get("ok", True)):
				status = "MEDIA_GENERATION_STATUS_FAILED"
				
			self._scene_status[scene_id]["status"] = status
			self._scene_status[scene_id]["operation_name"] = operation_name
			self._last_status_change_ts = time.time()
			self._scene_next_check_at[scene_id] = time.time() + 6
			self._scene_status_change_ts[scene_id] = time.time()

			self._update_state_entry(
				prompt_id,
				prompt_text,
				scene_id,
				idx,
				self._short_status(status),
				error=error_code,
				message=error_message,
			)

			prompt_idx = f"{prompt_id}_{idx + 1}"
			self.video_updated.emit({
				"prompt_idx": prompt_idx,
				"status": self._short_status(status),
				"scene_id": scene_id,
				"prompt": prompt_text,
				"_prompt_id": prompt_id,
			})

	async def _status_poll_loop(self, access_token, session_id, cookie=None, auth_dict=None):
		"""Status poll loop - tự động reload token mới nhất từ config trước mỗi lần check."""
		while not self.STOP:
			pending = [
				sid for sid, info in self._scene_status.items()
				if self._is_running_status(info.get("status"))
			]
			if not pending:
				if not await self._sleep_with_stop(1):
					break
				continue

			eligible = [
				sid for sid in pending
				if self._scene_next_check_at.get(sid, 0) <= time.time()
			]
			if not eligible:
				if not await self._sleep_with_stop(1):
					break
				continue

			# Giới hạn số lượng operations tối đa mỗi lần poll để tránh lỗi HTTP 400
			eligible = eligible[:5]

			now = time.time()
			if (now - self._status_log_ts) >= self._pending_log_interval:
				self._status_log_ts = now
				prompt_ids = []
				for sid in pending:
					prompt_info = self._scene_to_prompt.get(sid) or {}
					prompt_id = prompt_info.get("prompt_id")
					if prompt_id is not None:
						prompt_ids.append(prompt_id)
				# ✅ Log periodic để user biết workflow đang chờ
				if prompt_ids:
					self._log(f"⏳ Đang chờ {len(pending)} video hoàn thành (prompts: {', '.join(str(p) for p in set(prompt_ids))})")

			operations_payload = []
			for scene_id in eligible:
				info = self._scene_status.get(scene_id, {})
				op_name = info.get("operation_name")
				op_block = {"sceneId": scene_id}
				if op_name:
					op_block["operation"] = {"name": op_name}
				operations_payload.append(op_block)

			payload = {"operations": operations_payload}

			# ✅ Luôn đọc token mới nhất từ auth dict (được cập nhật bởi prompt tasks)
			if auth_dict and auth_dict.get("access_token"):
				access_token = auth_dict["access_token"]
			if auth_dict and auth_dict.get("cookie"):
				cookie = auth_dict["cookie"]

			# ✅ Ưu tiên check status qua browser nếu Chrome còn mở
			response = None
			collector_ref = getattr(self, '_collector_ref', None)
			if collector_ref:
				browser_page = getattr(collector_ref, 'page', None)
				if browser_page and not browser_page.is_closed():
					try:
						response = await t2v_api.request_check_status_via_browser(
							browser_page, payload, access_token
						)
					except Exception:
						response = None  # Fallback to urllib

			# Fallback: dùng urllib nếu browser không khả dụng
			if response is None:
				try:
					response = await t2v_api.request_check_status(payload, access_token, cookie=cookie)
				except Exception as exc:
					self._status_poll_fail_streak += 1
					self._log(
						f"⚠️ Lỗi check status (lần {self._status_poll_fail_streak}/4): {exc}"
					)
					if not await self._sleep_with_stop(5):
						break
					continue

			if not response.get("ok", True):
				status_code = response.get("status")
				reason = response.get("reason")
				body_err = str(response.get("body", ""))[:200]
				self._status_poll_fail_streak += 1
				
				# ✅ Xử lý 401 — token hết hạn (giống Character Sync: 3 bước)
				if status_code == 401:
					# Track token đã thất bại để không dùng lại
					failed_token = access_token
					self._log(
						f"⚠️ Check status 401 (lần {self._status_poll_fail_streak}/8)"
					)
					try:
						self._log("🔄 Check 401: Đang thử reload/refresh token...")
						
						# 1. Check auth_dict - chỉ dùng nếu token KHÁC token đã lỗi
						if auth_dict:
							mem_token = auth_dict.get("access_token", "")
							if mem_token and mem_token != failed_token:
								access_token = mem_token
								cookie = auth_dict.get("cookie", cookie)
								self._log("✅ [Check Status] Đã tiếp nhận token MỚI từ background worker")
								self._status_poll_fail_streak = 0
								continue

						# 2. Dùng auth_helper fetch trực tiếp
						import auth_helper
						auth_helper.invalidate_cache()
						auth_cfg = self._load_auth_config()
						fallback_cookie = auth_cfg.get("cookie", cookie) if auth_cfg else cookie
						fallback_proj = auth_cfg.get("projectId", "") if auth_cfg else ""
						
						new_token = auth_helper.get_valid_access_token(
							fallback_cookie, fallback_proj, force_refresh=True
						)
						if new_token and new_token != failed_token:
							access_token = new_token
							if auth_dict:
								auth_dict["access_token"] = new_token
							self._log("✅ Đã refresh access_token thủ công cho status poll")
							self._status_poll_fail_streak = 0
							continue

						# 3. Ép Chrome Browser refresh token ngay lập tức
						if collector_ref and hasattr(collector_ref, 'refresh_auth_from_browser'):
							self._log("🔥 Cứu hộ 401: Ép Chrome Browser lấy token mới...")
							try:
								ft, fc = await collector_ref.refresh_auth_from_browser(fallback_proj)
								if ft and ft != failed_token:
									access_token = ft
									if auth_dict:
										auth_dict["access_token"] = ft
										if fc: auth_dict["cookie"] = fc
									cookie = fc or cookie
									self._log("✅ Cứu hộ thành công! Chrome Browser đã đẩy Token vào status poll!")
									self._status_poll_fail_streak = 0
									continue
							except Exception as e:
								self._log(f"⚠️ Cứu hộ lỗi: {e}")

						self._log("⚠️ Status poll không thể làm mới token, chờ đợi...")

					except Exception as e:
						self._log(f"⚠️ Lỗi xử lý token trong status poll: {e}")
				else:
					self._log(
						f"⚠️ Check status thất bại "
						f"(lần {self._status_poll_fail_streak}/8, status={status_code}, reason={reason}, body={body_err})"
					)
				
				# ✅ Cap fail streak: giống Character Sync (8 lần)
				if self._status_poll_fail_streak >= 8:
					self._log("❌ Status poll thất bại 8 lần liên tiếp, đánh FAILED tất cả video đang chờ")
					self._mark_pending_failed("Status poll failed liên tiếp")
					break
				if not await self._sleep_with_stop(5):
					break
				continue

			response_body = response.get("body", "")
			if not self._handle_status_response(response_body):
				self._status_poll_fail_streak += 1
				self._log(
					f"⚠️ Check status parse lỗi (lần {self._status_poll_fail_streak}/4)"
				)
			else:
				self._status_poll_fail_streak = 0
				if hasattr(self, '_consecutive_401_count'):
					self._consecutive_401_count = 0
				self._mark_stuck_pending(time.time())
			if not await self._sleep_with_stop(5):
				break

	def _handle_status_response(self, response_body):
		try:
			body_json = json.loads(response_body)
			operations = body_json.get("operations", [])
		except Exception:
			return False

		updated = False

		for op in operations:
			scene_id = op.get("sceneId")
			status = self._normalize_status_full(op.get("status"))
			if not scene_id:
				continue

			prev = self._scene_status.get(scene_id, {}).get("status")
			error = None
			if isinstance(op.get("error"), dict):
				error = op.get("error")
			elif isinstance(op.get("operation"), dict) and isinstance(op["operation"].get("error"), dict):
				error = op["operation"].get("error")
			if error:
				status = "MEDIA_GENERATION_STATUS_FAILED"
			self._scene_status.setdefault(scene_id, {})["status"] = status
			if prev != status:
				self._scene_status_change_ts[scene_id] = time.time()
			force_update = bool(error)
			if not force_update and (not prev or prev == status):
				continue

			prompt_info = self._scene_to_prompt.get(scene_id)
			if not prompt_info:
				continue

			prompt_id = prompt_info["prompt_id"]
			idx = prompt_info["index"]
			prompt_text = self._get_prompt_text(prompt_id)
			prompt_idx = f"{prompt_id}_{idx + 1}"

			video_url, image_url = self._extract_media_urls(op)
			video_path = ""
			image_path = ""
			error_code = ""
			error_message = ""
			if error:
				error_code = str(error.get("code", "")) if error.get("code") is not None else ""
				error_message = str(error.get("message", "")) if error.get("message") is not None else ""
				log_msg = "❌"
				if prompt_id is not None:
					log_msg += f" Prompt {prompt_id}"
				else:
					log_msg += f" Scene {scene_id[:8]}"
				if error_code:
					log_msg += f": [{error_code}]"
				if error_message:
					log_msg += f" {error_message}"
				self._log(log_msg)

				# ✅ INLINE RETRY: TẤT CẢ lỗi → retry ngay trong tiến trình (theo RETRY_WITH_ERROR)
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
					# Reset scene về PENDING để không bị đếm là FAILED
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

			message_to_store = error_message
			if status == "MEDIA_GENERATION_STATUS_FAILED" and not message_to_store:
				op_summary = ""
				try:
					op_summary = json.dumps(op, ensure_ascii=True)
				except Exception:
					op_summary = ""
				if op_summary:
					op_summary = op_summary[:600]
				message_to_store = op_summary

			if status == "MEDIA_GENERATION_STATUS_SUCCESSFUL":
				self.video_updated.emit({
					"prompt_idx": prompt_idx,
					"status": "DOWNLOADING",
					"scene_id": scene_id,
					"prompt": prompt_text,
					"_prompt_id": prompt_id,
				})
				self._update_state_entry(
					prompt_id, prompt_text, scene_id, idx, "DOWNLOADING",
					video_url=video_url, image_url=image_url,
					video_path=video_path, image_path=image_path,
					error=error_code, message=message_to_store,
				)

				def dl_task(p_id, p_text, s_id, i_idx, v_url, i_url, v_path, i_path, e_code, c_msg, p_idx_str, err_msg):
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
						error=e_code, message=c_msg,
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
					error_code, message_to_store, prompt_idx, error_message
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
				message=message_to_store,
			)

			self.video_updated.emit({
				"prompt_idx": prompt_idx,
				"status": self._short_status(status),
				"scene_id": scene_id,
				"prompt": prompt_text,
				"video_path": video_path,
				"image_path": image_path,
				"_prompt_id": prompt_id,
				"error_code": error_code,
				"error_message": error_message,
			})
			updated = True

		if updated:
			self._last_status_change_ts = time.time()
		return True

	def _check_in_flight_block(self):
		"""Kiểm tra kẹt giới hạn in-flight quá lâu."""
		if not self._in_flight_block_start_ts:
			self._in_flight_block_start_ts = time.time()
		elapsed = time.time() - self._in_flight_block_start_ts
		if elapsed < 420:
			return True
		if self._last_status_change_ts and self._last_status_change_ts > self._in_flight_block_start_ts:
			self._in_flight_block_start_ts = time.time()
			return True
		self._log("⚠️ Kẹt giới hạn tạo video quá 7 phút, tiếp tục chờ...")
		self._in_flight_block_start_ts = time.time()
		return True

	def _mark_stuck_pending(self, now_ts):
		"""Đánh dấu FAILED nếu status PENDING/ACTIVE không đổi quá 7 phút."""
		for scene_id, info in list(self._scene_status.items()):
			status = info.get("status")
			if status not in {"MEDIA_GENERATION_STATUS_ACTIVE", "MEDIA_GENERATION_STATUS_PENDING", "ACTIVE", "PENDING"}:
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
			prompt_id = prompt_info.get("prompt_id")
			idx = prompt_info.get("index", 0)
			prompt_text = self._get_prompt_text(prompt_id)
			self._scene_status[scene_id]["status"] = "MEDIA_GENERATION_STATUS_FAILED"
			self._update_state_entry(
				prompt_id,
				prompt_text,
				scene_id,
				idx,
				"FAILED",
				error="STATUS_TIMEOUT",
				message="Timeout 7p khong thay doi status",
			)
			self.video_updated.emit({
				"prompt_idx": f"{prompt_id}_{idx + 1}",
				"status": "FAILED",
				"scene_id": scene_id,
				"prompt": prompt_text,
				"_prompt_id": prompt_id,
			})

	def _mark_pending_failed(self, message):
		"""Đánh dấu tất cả pending/active là FAILED và cập nhật state.json."""
		for scene_id, info in list(self._scene_status.items()):
			status = info.get("status")
			if status not in {"MEDIA_GENERATION_STATUS_ACTIVE", "MEDIA_GENERATION_STATUS_PENDING", "ACTIVE", "PENDING"}:
				continue
			prompt_info = self._scene_to_prompt.get(scene_id)
			if not prompt_info:
				continue
			prompt_id = prompt_info.get("prompt_id")
			idx = prompt_info.get("index", 0)
			prompt_text = self._get_prompt_text(prompt_id)
			self._scene_status[scene_id]["status"] = "MEDIA_GENERATION_STATUS_FAILED"
			self._update_state_entry(
				prompt_id,
				prompt_text,
				scene_id,
				idx,
				"FAILED",
				error="STATUS",
				message=message,
			)
			self.video_updated.emit({
				"prompt_idx": f"{prompt_id}_{idx + 1}",
				"status": "FAILED",
				"scene_id": scene_id,
				"prompt": prompt_text,
				"_prompt_id": prompt_id,
			})

	def _extract_media_urls(self, op):
		fife_url = ""
		serving_base_uri = ""
		image_url = ""
		operation = op.get("operation", {}) if isinstance(op.get("operation"), dict) else {}
		metadata = operation.get("metadata", {}) if isinstance(operation.get("metadata"), dict) else {}
		video = metadata.get("video", {}) if isinstance(metadata.get("video"), dict) else {}
		fife_url = video.get("fifeUrl", "") or ""
		serving_base_uri = video.get("servingBaseUri", "") or ""
		image = metadata.get("image", {}) if isinstance(metadata.get("image"), dict) else {}
		image_url = image.get("fifeUrl", "") or image.get("uri", "") or ""

		try:
			config = SettingsManager.load_config()
			dl_mode = str(config.get("DOWNLOAD_MODE", "720") or "720").strip()
		except Exception:
			dl_mode = "720"

		if fife_url:
			fife_url = apply_download_resolution(fife_url, dl_mode)

		return fife_url, (image_url or serving_base_uri)

	def _output_root_dir(self):
		try:
			config = SettingsManager.load_config()
			root = str(config.get("VIDEO_OUTPUT_DIR") or "").strip() if isinstance(config, dict) else ""
			if root:
				p = Path(root)
				p.mkdir(parents=True, exist_ok=True)
				return p
		except Exception:
			pass
		p = WORKFLOWS_DIR / self.project_name / "Download"
		p.mkdir(parents=True, exist_ok=True)
		return p

	def _build_timestamped_media_path(self, output_dir: Path, prompt_idx: str, suffix: str) -> Path:
		timestamp = datetime.now().strftime("%d%m%Y_%H%M%S")
		base_name = f"{prompt_idx}_{timestamp}"
		file_path = output_dir / f"{base_name}{suffix}"
		counter = 1
		while file_path.exists():
			file_path = output_dir / f"{base_name}_{counter}{suffix}"
			counter += 1
		return file_path

	def _download_video(self, url, prompt_idx):
		if not url:
			return ""
		if self._should_stop():
			return ""
		video_dir = self._output_root_dir() / "video"
		video_dir.mkdir(parents=True, exist_ok=True)
		file_path = self._build_timestamped_media_path(video_dir, str(prompt_idx), ".mp4")
		try:
			self._log(f"⬇️  Đang tải video: {prompt_idx}")
			with requests.get(url, stream=True, timeout=(8, 6)) as resp:
				resp.raise_for_status()
				with open(file_path, "wb") as f:
					for chunk in resp.iter_content(chunk_size=1024 * 1024):
						if self._should_stop():
							self._log(f"🛑 Dừng tải video: {prompt_idx}")
							return ""
						if chunk:
							f.write(chunk)
			self._log(f"⬇️  Tải video xong: {file_path}")
			video_str = str(file_path)
			# remove_watermark(video_str, log_callback=self._log)
			return video_str
		except Exception:
			self._log("⚠️  Không tải được video")
			return ""

	def _download_image(self, url, prompt_idx):
		if not url:
			return ""
		if self._should_stop():
			return ""
		image_dir = self._output_root_dir() / "image"
		image_dir.mkdir(parents=True, exist_ok=True)
		file_path = self._build_timestamped_media_path(image_dir, str(prompt_idx), ".jpg")
		try:
			with requests.get(url, stream=True, timeout=(8, 6)) as resp:
				resp.raise_for_status()
				with open(file_path, "wb") as f:
					for chunk in resp.iter_content(chunk_size=1024 * 256):
						if self._should_stop():
							return ""
						if chunk:
							f.write(chunk)
			return str(file_path)
		except Exception:
			self._log("⚠️  Không tải được image")
			return ""

	def _get_prompt_text(self, prompt_id):
		prompts = self._load_text_prompts()
		for item in prompts:
			if str(item.get("id")) == str(prompt_id):
				return item.get("prompt", "")
		return ""

	async def _init_token_collector(
		self,
		project_link,
		chrome_userdata_root,
		profile_name,
		clear_data_interval,
		idle_timeout,
		token_timeout,
	):
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
				keep_chrome_open=self._keep_chrome_open,
				close_chrome_after_token=getattr(self, '_close_chrome_after_token', False),
				hide_window=True,
			)
			await pool.start()
			return pool

		self._log("🧭 Khởi tạo TokenCollector cho Text to Video | mode=video")
		return TokenCollector(
			project_link,
			chrome_userdata_root=chrome_userdata_root,
			profile_name=profile_name,
			debug_port=9222,
			headless=False,
			token_timeout=token_timeout,
			idle_timeout=idle_timeout,
			log_callback=self._log,
			stop_check=self._should_stop,
			clear_data_interval=clear_data_interval,
			keep_chrome_open=self._keep_chrome_open,
			close_chrome_after_token=self._close_chrome_after_token,
			mode="video",
			hide_window=True,
		)

	async def _wait_for_completion(self):
		"""
		✅ Chờ tất cả video hoàn thành
		- Thoát khi: hết prompts (flag=True) + tất cả video đã SUCCESSFUL/FAILED
		- Hoặc khi bấm STOP
		"""
		_last_pending_log_ts = 0
		_no_pending_count = 0  # Đếm số lần liên tiếp không có pending
		
		while True:
			if self.STOP:
				self._log(f"🛑 STOP nhận được, thoát loop chờ - Thoát luồng lấy token")
				break

			# Timeout check
			if self._all_prompts_submitted and self._complete_wait_timeout > 0:
				if not self._complete_wait_start_ts:
					self._complete_wait_start_ts = time.time()
				elapsed = time.time() - self._complete_wait_start_ts
				if elapsed >= self._complete_wait_timeout:
					if not self._auto_noi_canh:
						self._log(
							f"⏱️  Quá thời gian chờ hoàn thành ({self._complete_wait_timeout}s), dừng workflow"
						)
					if not self._auto_noi_canh:
						self.STOP = 1
					break
			
			# ✅ KIỂM TRA TỪ STATE.JSON (nguồn chính xác nhất)
			state_pending = self._count_in_progress()
			
			# ✅ KIỂM TRA TỪ _scene_status (cả FULL và SHORT status)
			scene_pending = [
				info for info in self._scene_status.values()
				if self._is_running_status(info.get("status"))
			]
			
			# ✅ ĐIỀU KIỆN THOÁT: đã gửi hết prompts VÀ không còn video pending
			if self._all_prompts_submitted:
				# Ưu tiên kiểm tra state.json vì nó được cập nhật chính xác
				if state_pending == 0:
					_no_pending_count += 1
					self._log(f"🔍 Kiểm tra: state_pending={state_pending}, scene_pending={len(scene_pending)}, count={_no_pending_count}")
					# Chờ 2 lần liên tiếp để chắc chắn không còn pending
					if _no_pending_count >= 2:
						if not self._auto_noi_canh:
							self._log("✅ Tất cả video đã hoàn thành (từ state.json) - thoát workflow")
						break
				else:
					_no_pending_count = 0
				
				# Fallback: kiểm tra _scene_status nếu có data
				if len(self._scene_status) > 0 and (not scene_pending) and state_pending == 0:
					if not self._auto_noi_canh:
						self._log("✅ Tất cả video đã hoàn thành (từ scene_status) - thoát workflow")
					break
			
			# Log trạng thái chờ mỗi 15s
			now = time.time()
			total_pending = max(state_pending, len(scene_pending))
			if total_pending > 0 and (now - _last_pending_log_ts) >= 15:
				_last_pending_log_ts = now
				self._log(f"⏳ Đang chờ {total_pending} video hoàn thành...")
			elif not self._all_prompts_submitted and (now - _last_pending_log_ts) >= 15:
				_last_pending_log_ts = now
				self._log(f"⏳ Chưa đủ prompts gửi, chờ thêm...")
			
			if not await self._sleep_with_stop(2):
				break

	def _short_status(self, status):
		if not status:
			return "PENDING"
		# Chấp nhận cả dạng ngắn (PENDING/ACTIVE/SUCCESSFUL/FAILED)
		upper = str(status).upper()
		if upper in {"PENDING", "ACTIVE", "SUCCESSFUL", "FAILED"}:
			return upper
		if any(marker in upper for marker in {"RUNNING", "PROCESS", "PROGRESS", "QUEUED", "SUBMIT", "CREATING", "GENERATING", "STARTED"}):
			return "ACTIVE"
		if "PENDING" in status:
			return "PENDING"
		if "ACTIVE" in status:
			return "ACTIVE"
		if "SUCCESSFUL" in status:
			return "SUCCESSFUL"
		if "FAILED" in status:
			return "FAILED"
		return status.replace("MEDIA_GENERATION_STATUS_", "")

	def _is_running_status(self, status: str) -> bool:
		upper = str(status or "").upper()
		if not upper:
			return False
		return not self._is_terminal_status(upper)

	def _is_terminal_status(self, status: str) -> bool:
		upper = str(status or "").upper()
		if not upper:
			return False
		return any(marker in upper for marker in {"SUCCESS", "FAILED", "CANCEL", "ERROR"})

	def _normalize_status_full(self, status: str) -> str:
		"""Chuẩn hoá status trả về từ API về dạng MEDIA_GENERATION_STATUS_* cho nội bộ."""
		if not status:
			return "MEDIA_GENERATION_STATUS_PENDING"
		text = str(status).strip()
		upper = text.upper()
		# Nếu đã là full form
		if upper.startswith("MEDIA_GENERATION_STATUS_"):
			return upper
		# Nếu là short form
		if upper in {"PENDING", "ACTIVE", "SUCCESSFUL", "FAILED"}:
			return f"MEDIA_GENERATION_STATUS_{upper}"
		# Fallback: nếu có chứa keyword thì map tương ứng
		if "PENDING" in upper:
			return "MEDIA_GENERATION_STATUS_PENDING"
		if "ACTIVE" in upper:
			return "MEDIA_GENERATION_STATUS_ACTIVE"
		if "SUCCESS" in upper:
			return "MEDIA_GENERATION_STATUS_SUCCESSFUL"
		if "FAIL" in upper:
			return "MEDIA_GENERATION_STATUS_FAILED"
		return upper


def start_text_to_video(
	app,
	project_name,
	project_data,
	project_file,
	*,
	prepare_project=False,
	manage_buttons=True,
	log_start=True,
):
	"""Start Text to Video workflow from UI app context."""
	try:
		if prepare_project:
			with open(project_file, "w", encoding="utf-8") as f:
				json.dump(project_data, f, ensure_ascii=False, indent=2)

			app._clear_download_media(project_name)

			state_file = app.get_state_file_path()
			if state_file and state_file.exists():
				state_file.unlink()

			app.table_video.setRowCount(0)

		if log_start:
			app.add_log("🎬 Khởi động luồng Text to Video...")

		app.workflow = TextToVideoWorkflow(project_name=project_name, project_data=project_data)
		app.workflow.log_message.connect(app.add_log)
		app.workflow.video_updated.connect(app.on_video_updated)
		app.workflow.automation_complete.connect(app.on_automation_complete)
		app.workflow.start()

		if manage_buttons:
			if hasattr(app, "btn_run_all"):
				app.btn_run_all.setEnabled(False)
				app.btn_run_all.setStyleSheet(
					"background- border: 1px solid #666666; border-radius: 6px;"
				)

			if hasattr(app, "btn_start"):
				app.btn_start.setEnabled(False)
				app.btn_start.setStyleSheet(
					"background- border: 1px solid #666666; border-radius: 6px;"
				)

			if hasattr(app, "btn_stop"):
				app.btn_stop.setEnabled(True)
		return True
	except Exception as e:
		try:
			app.add_log(f"❌ Lỗi chạy Text to Video: {e}")
		except Exception:
			pass
		return False


def start_text_to_video_resend(
	app,
	project_name,
	project_data,
	resend_items,
	*,
	automation_complete=None,
	manage_buttons=True,
):
	"""Start Text to Video resend workflow from UI app context."""
	try:
		app.workflow = TextToVideoWorkflow(project_name=project_name, project_data=project_data, parent=app)
		app.workflow.log_message.connect(app.add_log)
		app.workflow.video_updated.connect(app.on_video_updated)
		if automation_complete is None:
			app.workflow.automation_complete.connect(app.on_automation_complete)
		else:
			app.workflow.automation_complete.connect(automation_complete)

		app.workflow._resend_items = resend_items
		app.workflow.start()
		app.add_log("✅ Đã khởi động Text to Video Resend workflow")

		if manage_buttons and hasattr(app, "btn_stop"):
			app.btn_stop.setEnabled(True)
		return True
	except Exception as e:
		try:
			app.add_log(f"❌ Lỗi khởi động Text to Video Resend: {e}")
		except Exception:
			pass
		return False


# ========= APP ENTRY HELPERS (Text to Video) =========

def _load_test_json(project_name):
	path = WORKFLOWS_DIR / project_name / "test.json"
	if not path.exists():
		return None
	try:
		with open(path, "r", encoding="utf-8") as f:
			return json.load(f)
	except Exception:
		return None


def _load_state_json(project_name):
	path = WORKFLOWS_DIR / project_name / "state.json"
	if not path.exists():
		return {}
	try:
		with open(path, "r", encoding="utf-8") as f:
			return json.load(f)
	except Exception:
		return {}


def _find_text_prompt(project_name, prompt_id):
	data = _load_test_json(project_name) or {}
	prompts_data = data.get("prompts", {}) if isinstance(data, dict) else {}
	text_prompts = prompts_data.get("text_to_video", []) if isinstance(prompts_data, dict) else []
	for prompt in text_prompts:
		if str(prompt.get("id")) == str(prompt_id):
			return prompt
	return None


def _build_resend_items_for_prompt(state_data, prompt_id, prompt_text, video_indices=None):
	resend_items = []
	prompt_state = (state_data.get("prompts", {}) or {}).get(str(prompt_id), {})
	scene_ids = prompt_state.get("scene_ids", []) if isinstance(prompt_state, dict) else []
	indices = video_indices if video_indices else range(1, len(scene_ids) + 1)
	for vid_idx in indices:
		if 0 < vid_idx <= len(scene_ids):
			scene_id = scene_ids[vid_idx - 1]
			if scene_id:
				resend_items.append((prompt_id, prompt_text, scene_id, vid_idx - 1))
	return resend_items


def _start_text_workflow(app, project_name, project_data, resend_items, log_cb, video_cb, complete_cb):
	try:
		wf = TextToVideoWorkflow(project_name=project_name, project_data=project_data, parent=app)
		if log_cb:
			wf.log_message.connect(log_cb)
		if video_cb:
			wf.video_updated.connect(video_cb)
		if complete_cb:
			wf.automation_complete.connect(complete_cb)
		else:
			wf.automation_complete.connect(app.on_automation_complete)
		if resend_items:
			wf._resend_items = resend_items
		wf.start()
		return wf
	except Exception as e:
		if log_cb:
			log_cb(f"❌ Lỗi khởi động workflow Text to Video: {e}")
		return None


def start_text_resend_single(parent, project_name, prompt_id, video_idx, log_cb=None, video_cb=None, complete_cb=None):
	project_data = _load_test_json(project_name)
	if not project_data:
		if log_cb:
			log_cb(f"⚠️ Không đọc được test.json cho {project_name}")
		return None
	prompt = _find_text_prompt(project_name, prompt_id)
	if not prompt:
		if log_cb:
			log_cb(f"⚠️ Không tìm thấy prompt ID {prompt_id} trong test.json")
		return None
	state_data = _load_state_json(project_name)
	prompt_text = prompt.get("description", "") or prompt.get("prompt", "")
	resend_items = _build_resend_items_for_prompt(state_data, prompt_id, prompt_text, [video_idx])
	if not resend_items:
		if log_cb:
			log_cb(f"⚠️ Không tìm thấy scene_id cho prompt {prompt_id}, video {video_idx}")
		return None
	project_data.setdefault("prompts", {})
	project_data["prompts"]["text_to_video"] = [prompt]
	project_data["_is_resend"] = True
	project_data["_prompt_id"] = prompt_id
	project_data["_prompt_text"] = prompt_text
	project_data["_resend_prompt_idx"] = f"{prompt_id}_{video_idx}"
	project_data["_resend_video_idx"] = video_idx
	return _start_text_workflow(parent, project_name, project_data, resend_items, log_cb, video_cb, complete_cb)


def start_text_resend_selected(parent, project_name, prompt_ids, log_cb=None, video_cb=None, complete_cb=None):
	project_data = _load_test_json(project_name)
	if not project_data:
		if log_cb:
			log_cb(f"⚠️ Không đọc được test.json cho {project_name}")
		return None
	state_data = _load_state_json(project_name)
	selected_prompts = []
	resend_items = []
	for pid in prompt_ids:
		prompt = _find_text_prompt(project_name, pid)
		if not prompt:
			if log_cb:
				log_cb(f"⚠️ Bỏ qua ID {pid} (không tìm thấy trong test.json)")
			continue
		prompt_text = prompt.get("description", "") or prompt.get("prompt", "")
		selected_prompts.append(prompt)
		resend_items.extend(_build_resend_items_for_prompt(state_data, pid, prompt_text))
	if not selected_prompts or not resend_items:
		if log_cb:
			log_cb("⚠️ Không có prompt nào hợp lệ để resend")
		return None
	project_data.setdefault("prompts", {})
	project_data["prompts"]["text_to_video"] = selected_prompts
	project_data["_is_resend"] = True
	return _start_text_workflow(parent, project_name, project_data, resend_items, log_cb, video_cb, complete_cb)


def start_text_resend_failed(parent, project_name, failed_videos_map, log_cb=None, video_cb=None, complete_cb=None):
	project_data = _load_test_json(project_name)
	if not project_data:
		if log_cb:
			log_cb(f"⚠️ Không đọc được test.json cho {project_name}")
		return None
	state_data = _load_state_json(project_name)
	selected_prompts = []
	resend_items = []
	seen = set()
	for pid, video_indices in (failed_videos_map or {}).items():
		prompt = _find_text_prompt(project_name, pid)
		if not prompt:
			if log_cb:
				log_cb(f"⚠️ Bỏ qua ID {pid} (không tìm thấy trong test.json)")
			continue
		prompt_text = prompt.get("description", "") or prompt.get("prompt", "")
		if pid not in seen:
			selected_prompts.append(prompt)
			seen.add(pid)
		resend_items.extend(_build_resend_items_for_prompt(state_data, pid, prompt_text, video_indices))
	if not selected_prompts or not resend_items:
		if log_cb:
			log_cb("⚠️ Không có dữ liệu resend hợp lệ (failed_map)")
		return None
	project_data.setdefault("prompts", {})
	project_data["prompts"]["text_to_video"] = selected_prompts
	project_data["_is_resend"] = True
	project_data["_is_resend_all"] = True
	return _start_text_workflow(parent, project_name, project_data, resend_items, log_cb, video_cb, complete_cb)
