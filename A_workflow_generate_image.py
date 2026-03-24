import asyncio
import threading
import importlib
import json
import os
import shutil
import time
import requests
import json
import logging
import uuid
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse
from datetime import datetime
try:
	_qtcore = importlib.import_module("PySide6.QtCore")
except Exception:
	_qtcore = importlib.import_module("PyQt6.QtCore")

QThread = _qtcore.QThread
Signal = getattr(_qtcore, "Signal", None) or getattr(_qtcore, "pyqtSignal")

from settings_manager import SettingsManager, WORKFLOWS_DIR
from A_workflow_get_token import TokenCollector
from token_pool import TokenPool
from API_Create_image import (
	build_generate_image_payload,
	build_generate_image_url,
	request_generate_images,
	request_generate_images_via_browser,
	parse_media_from_response,
	IMAGE_ASPECT_RATIO_LANDSCAPE,
	IMAGE_ASPECT_RATIO_PORTRAIT,
	refresh_account_context,
)
from workflow_run_control import get_running_video_count, get_max_in_flight


class GenerateImageWorkflow(QThread):
	"""Workflow tạo ảnh qua API flowMedia:batchGenerateImages."""

	log_message = Signal(str)
	video_updated = Signal(dict)
	automation_complete = Signal()

	def __init__(self, project_name=None, project_data=None, parent=None, prompt_ids_filter=None):
		super().__init__(parent)
		self.project_name = project_name or (project_data or {}).get("project_name", "Unknown")
		self.project_data = project_data or {}
		self._keep_chrome_open = bool(self.project_data.get("_keep_chrome_open"))
		self.STOP = 0
		self._token_timeouts = 0
		self._prompt_ids_filter = set(str(x) for x in prompt_ids_filter) if prompt_ids_filter else None
		self._preserve_existing_data = bool(self._prompt_ids_filter)
		self._in_flight_block_start_ts = 0
		self._active_prompt_ids = set()
		self._download_executor = ThreadPoolExecutor(max_workers=5, thread_name_prefix="gen-img-dl")

	def run(self):
		try:
			running_loop = asyncio.get_running_loop()
		except RuntimeError:
			running_loop = None

		if running_loop and running_loop.is_running():
			self._log("⚠️  Đang có event loop chạy, tạo luồng mới cho Generate Image...")
			worker = threading.Thread(target=self._run_with_new_loop, daemon=True)
			worker.start()
			worker.join()
			return

		self._run_with_new_loop()

	def _run_with_new_loop(self):
		loop = asyncio.new_event_loop()
		asyncio.set_event_loop(loop)
		try:
			loop.run_until_complete(self._run_workflow())
			# ✅ Inline retry ALL errors đã được xử lý bên trong _run_workflow (retry_with_error)
			
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
			self.automation_complete.emit()

	def _collect_audio_filtered_failures(self):
		"""Lấy danh sách ảnh bị lỗi error code 3 / AUDIO_FILTERED từ state.json để auto-retry."""
		try:
			state_data = self._load_state_json()
			failed_items = []
			prompts = state_data.get("prompts", {})
			for prompt_key, prompt_data in prompts.items():
				if not isinstance(prompt_data, dict):
					continue
				prompt_id = prompt_data.get("id") or prompt_data.get("prompt_id")
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

	def _log(self, message):
		try:
			self.log_message.emit(message)
		except Exception:
			pass

	def stop(self):
		self.STOP = 1

	def _should_stop(self):
		return bool(self.STOP)

	async def _sleep_with_stop(self, seconds):
		end_ts = time.time() + float(seconds)
		while time.time() < end_ts:
			if self._should_stop():
				return False
			await asyncio.sleep(0.2)
		return True

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

	def _save_state_json(self, state_data):
		state_file = self._get_state_file_path()
		try:
			tmp_file = state_file.with_suffix(".json.tmp")
			with open(tmp_file, "w", encoding="utf-8") as f:
				json.dump(state_data, f, ensure_ascii=False, indent=2)
				f.flush()
				os.fsync(f.fileno())
			os.replace(tmp_file, state_file)
			return True
		except Exception:
			try:
				tmp_file.unlink(missing_ok=True)
			except Exception:
				pass
			return False

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
			if any(any(marker in str(status or "").upper() for marker in running_markers) for status in statuses):
				count += 1
		return count

	def _count_in_progress(self):
		return int(self._count_in_progress_from_state())

	def _resolve_worker_max_in_flight(self, fallback_value):
		return max(1, int(get_max_in_flight(default_value=int(fallback_value or 1))))

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

	def _update_state_entry(self, prompt_id, prompt_text, scene_id, idx, status, image_url="", image_path="", error="", message=""):
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

		while len(prompt_data["image_paths"]) <= idx:
			prompt_data["image_paths"].append("")
		if image_path:
			prompt_data["image_paths"][idx] = image_path

		while len(prompt_data["image_urls"]) <= idx:
			prompt_data["image_urls"].append("")
		if image_url:
			prompt_data["image_urls"][idx] = image_url

		while len(prompt_data["errors"]) <= idx:
			prompt_data["errors"].append("")
		prompt_data["errors"][idx] = error if error else ""

		if "error_codes" not in prompt_data:
			prompt_data["error_codes"] = []
		while len(prompt_data["error_codes"]) <= idx:
			prompt_data["error_codes"].append("")
		prompt_data["error_codes"][idx] = error if error else ""

		while len(prompt_data["messages"]) <= idx:
			prompt_data["messages"].append("")
		prompt_data["messages"][idx] = message if message else ""

		if "error_messages" not in prompt_data:
			prompt_data["error_messages"] = []
		while len(prompt_data["error_messages"]) <= idx:
			prompt_data["error_messages"].append("")
		prompt_data["error_messages"][idx] = message if message else ""

		self._save_state_json(state_data)
		self._log(f"🧾 Update state: prompt {prompt_id} scene {scene_id[:8]} -> {status}")

	def _save_auth_to_state(self, access_token, session_id, project_id):
		state_data = self._load_state_json()
		state_data["auth"] = {
			"access_token": access_token,
			"sessionId": session_id,
			"projectId": project_id,
		}
		self._save_state_json(state_data)

	def _assign_scene_ids(self, payload, prompt_id, prompt_text):
		scene_ids = []
		requests = payload.get("requests", [])
		for idx, _ in enumerate(requests):
			scene_id = str(uuid.uuid4())
			scene_ids.append(scene_id)
			self._update_state_entry(prompt_id, prompt_text, scene_id, idx, "PENDING")
		return scene_ids

	def _short_status(self, status):
		if not status:
			return "PENDING"
		if "PENDING" in status:
			return "PENDING"
		if "ACTIVE" in status:
			return "ACTIVE"
		if "SUCCESSFUL" in status:
			return "SUCCESSFUL"
		if "FAILED" in status:
			return "FAILED"
		return status

	def _load_text_prompts(self):
		if self.project_data.get("_use_project_prompts"):
			items = self.project_data.get("prompts", {}).get("text_to_video", [])
			if self._prompt_ids_filter:
				items = [p for p in items if str(p.get("id")) in self._prompt_ids_filter]
			return items or []

		project_dir = WORKFLOWS_DIR / self.project_name
		test_file = project_dir / "test.json"
		if not test_file.exists():
			items = self.project_data.get("prompts", {}).get("text_to_video", [])
			if self._prompt_ids_filter:
				items = [p for p in items if str(p.get("id")) in self._prompt_ids_filter]
			return items or []
		try:
			with open(test_file, "r", encoding="utf-8") as f:
				data = json.load(f)
		except Exception:
			return []
		prompts_data = data.get("prompts", {}) if isinstance(data, dict) else {}
		text_prompts = prompts_data.get("text_to_video", []) if isinstance(prompts_data, dict) else []
		if self._prompt_ids_filter:
			text_prompts = [p for p in text_prompts if str(p.get("id")) in self._prompt_ids_filter]
		return text_prompts or []

	def _load_auth_config(self):
		try:
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
		except Exception:
			return None

	def _resolve_int_config(self, config, key, default_value):
		try:
			val = config.get(key)
			if val is not None:
				return int(val)
			val_lower = config.get(key.lower())
			if val_lower is not None:
				return int(val_lower)
			return int(default_value)
		except Exception:
			return default_value

	def _resolve_output_count(self, config):
		try:
			value = self.project_data.get("output_count") or config.get("OUTPUT_COUNT")
			return int(value)
		except Exception:
			return 1

	def _resolve_aspect_ratio(self, aspect_source=None):
		source = aspect_source or {}
		text = str(source.get("aspect_ratio") or self.project_data.get("aspect_ratio") or "").lower()
		if "9:16" in text or "dọc" in text or "doc" in text:
			return IMAGE_ASPECT_RATIO_PORTRAIT
		return IMAGE_ASPECT_RATIO_LANDSCAPE

	def _output_root_dir(self) -> Path:
		raw = str(self.project_data.get("video_output_dir") or "").strip()
		if not raw:
			raw = str(self.project_data.get("output_dir") or "").strip()
		if not raw:
			raw = str(WORKFLOWS_DIR / self.project_name / "Download")
		path = Path(raw)
		path.mkdir(parents=True, exist_ok=True)
		return path

	def _image_output_dir(self) -> Path:
		path = self._output_root_dir() / "image"
		path.mkdir(parents=True, exist_ok=True)
		return path

	def _build_timestamped_media_path(self, output_dir: Path, prompt_idx: str, suffix: str) -> Path:
		timestamp = datetime.now().strftime("%d%m%Y_%H%M%S")
		base_name = f"{prompt_idx}_{timestamp}"
		file_path = output_dir / f"{base_name}{suffix}"
		counter = 1
		while file_path.exists():
			file_path = output_dir / f"{base_name}_{counter}{suffix}"
			counter += 1
		return file_path

	def _download_image(self, url, prompt_idx):
		if not url:
			return ""
		url_text = str(url or "").strip()
		if not (url_text.startswith("http://") or url_text.startswith("https://")):
			self._log(f"⚠️ Không tải image: URL không hợp lệ ({url_text[:140]})")
			return ""
		image_dir = self._image_output_dir()
		image_dir.mkdir(parents=True, exist_ok=True)
		file_path = self._build_timestamped_media_path(image_dir, str(prompt_idx), ".jpg")
		try:
			with requests.get(url_text, stream=True, timeout=60) as resp:
				resp.raise_for_status()
				with open(file_path, "wb") as f:
					for chunk in resp.iter_content(chunk_size=1024 * 256):
						if chunk:
							f.write(chunk)
			return str(file_path.resolve())
		except requests.exceptions.RequestException as exc:
			self._log(f"⚠️ Không tải được image: {exc} | url={url_text[:140]}")
			return ""
		except Exception as exc:
			self._log(f"⚠️ Không tải được image: {exc} | url={url_text[:140]}")
			return ""

	def _clear_previous_data(self):
		project_dir = WORKFLOWS_DIR / self.project_name
		if not project_dir.exists():
			return
		keep_files = {"test.json", "status.json"}
		keep_dirs = {"Download", "thumbnails"}
		for item in project_dir.iterdir():
			if item.name in keep_files or item.name in keep_dirs:
				continue
			try:
				if item.is_dir():
					shutil.rmtree(item, ignore_errors=True)
				else:
					item.unlink(missing_ok=True)
			except Exception as e:
				self._log(f"⚠️ Không thể xóa {item.name}: {e}")

	def _save_request_json(self, payload, prompt_id, prompt_text):
		try:
			project_dir = WORKFLOWS_DIR / str(self.project_name)
			project_dir.mkdir(parents=True, exist_ok=True)
			request_file = project_dir / "request.json"
			request_data = {
				"timestamp": int(time.time()),
				"project_name": self.project_name,
				"flow": "generate_image",
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
					pass
			entries.append(request_data)
			with open(request_file, "w", encoding="utf-8") as f:
				json.dump(entries, f, ensure_ascii=False, indent=2)
		except Exception as e:
			self._log(f"⚠️ Không thể lưu request.json: {e}")

	def _save_response_json(self, response, prompt_id, prompt_text):
		try:
			project_dir = WORKFLOWS_DIR / str(self.project_name)
			project_dir.mkdir(parents=True, exist_ok=True)
			response_file = project_dir / "respone_anh.json"
			entry = {
				"timestamp": int(time.time()),
				"project_name": self.project_name,
				"flow": "generate_image",
				"prompt_id": prompt_id,
				"prompt_text": prompt_text,
				"ok": response.get("ok"),
				"status": response.get("status"),
				"reason": response.get("reason"),
				"error": response.get("error"),
				"body": response.get("body"),
			}
			entries = []
			if response_file.exists():
				try:
					raw_text = response_file.read_text(encoding="utf-8").strip()
					if raw_text:
						parsed = json.loads(raw_text)
						if isinstance(parsed, list):
							entries = parsed
						elif isinstance(parsed, dict):
							entries = [parsed]
				except Exception:
					pass
			entries.append(entry)
			with open(response_file, "w", encoding="utf-8") as f:
				json.dump(entries, f, ensure_ascii=False, indent=2)
		except Exception as e:
			self._log(f"⚠️ Không thể lưu respone_anh.json: {e}")

	def _extract_error_info(self, response_body):
		try:
			body_json = json.loads(response_body)
		except Exception:
			return "", "", ""
		error = body_json.get("error") if isinstance(body_json, dict) else None
		if not isinstance(error, dict):
			return "", "", ""
		code = str(error.get("code", "")) if error.get("code") is not None else ""
		message = str(error.get("message", "")) if error.get("message") is not None else ""
		# Trích xuất reason từ error.details (vd: PUBLIC_ERROR_UNSAFE_GENERATION)
		error_reason = ""
		details = error.get("details")
		if isinstance(details, list):
			for detail in details:
				if isinstance(detail, dict) and detail.get("reason"):
					error_reason = str(detail["reason"])
					break
		return code, message, error_reason

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
				mode="generate_image",
				token_timeout=token_timeout,
				clear_data_interval=clear_data_interval,
				clear_data_wait=clear_data_wait,
				keep_chrome_open=self._keep_chrome_open,
				close_chrome_after_token=getattr(self, '_close_chrome_after_token', False),
				hide_window=True,
			)
			await pool.start()
			return pool

		self._log(f"🧭 Khởi tạo TokenCollector cho Generate Image")
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
			mode="generate_image",
			hide_window=True,
		)

	async def _run_workflow(self):
		if self._should_stop():
			self._log("🛑 STOP trước khi chạy workflow")
			return

		self._in_flight_block_start_ts = 0

		self._log(f"🚀 Bắt đầu workflow Tạo Ảnh cho project '{self.project_name}'")
		if self._preserve_existing_data:
			self._log("🧹 Bỏ qua xóa dữ liệu cũ (resend giữ lại state/ảnh/video hiện có)")
		else:
			self._clear_previous_data()

		prompts = self._load_text_prompts()
		if not prompts:
			self._log("❌ Không có prompts text_to_video trong test.json")
			return
		self._active_prompt_ids = {
			str((p or {}).get("id") or (idx + 1)).strip()
			for idx, p in enumerate(prompts)
			if str((p or {}).get("id") or (idx + 1)).strip()
		}
		if self._prompt_ids_filter:
			self._log(f"🧾 Đã nạp {len(prompts)} / {len(self._prompt_ids_filter)} prompt được chọn từ test.json")
		else:
			self._log(f"🧾 Đã nạp {len(prompts)} prompt từ test.json")

		auth = self._load_auth_config()
		if not auth:
			self._log("❌ Thiếu sessionId/projectId/access_token trong config.json")
			return

		refresh_account_context()

		import auth_helper
		self._log("🛂 Đang kiểm tra token OAuth...")
		new_token = auth_helper.get_valid_access_token(auth.get("cookie", ""), auth.get("projectId", ""))
		if new_token and new_token != auth.get("access_token"):
			self._log("✅ Token OAuth đã được làm mới tự động trước khi bắt đầu")
			auth["access_token"] = new_token

		session_id = auth["sessionId"]
		project_id = auth["projectId"]
		access_token = auth["access_token"]
		cookie = auth.get("cookie")
		project_link = auth.get("URL_GEN_TOKEN") or "https://labs.google/fx/vi/tools/flow"
		chrome_userdata_root = auth.get("folder_user_data_get_token")
		profile_name = self.project_data.get("veo_profile") or SettingsManager.load_settings().get("current_profile")

		config = SettingsManager.load_config()
		output_count = self._resolve_output_count(config)
		wait_between = int(config.get("WAIT_BETWEEN_PROMPTS", config.get("WAIT_GEN_IMAGE", config.get("WAIT_GEN_VIDEO", 3))))
		# ✅ Không thêm extra_wait — inflight_lock đã kiểm soát số luồng đồng thời
		wait_between_effective = wait_between
		max_token_retries = int(config.get("TOKEN_RETRY", 3))
		token_retry_delay = int(config.get("TOKEN_RETRY_DELAY", 2))
		retry_with_error = int(config.get("RETRY_WITH_ERROR", 3))
		wait_resend_image = int(config.get("WAIT_RESEND_IMAGE", config.get("WAIT_RESEND_VIDEO", 20)))
		clear_data_token_image = int(config.get("CLEAR_DATA_IMAGE", config.get("CLEAR_DATA", 50)))
		clear_data_wait = int(config.get("CLEAR_DATA_WAIT", 2))
		response_timeout = int(config.get("IMAGE_RESPONSE_TIMEOUT", 80))
		get_token_timeout = 60
		max_in_flight = self._resolve_worker_max_in_flight(max(self._resolve_int_config(config, "MULTI_VIDEO", 3), 1))

		self._log(
			f"⚙️  Cấu hình: output_count={output_count}, "
			f"timeout_ảnh={response_timeout}s, token_timeout={get_token_timeout}s, wait_between={wait_between_effective}s, max_in_flight={max_in_flight}"
		)

		collector = await self._init_token_collector(
			project_link,
			chrome_userdata_root,
			profile_name,
			clear_data_token_image,
			40,
			get_token_timeout,
		)

		token_lock = asyncio.Lock()
		inflight_lock = asyncio.Lock()
		token_counter = {"count": 0}

		async with collector:
			# ✅ Refresh auth từ Chrome browser ngay sau khi Chrome khởi động
			self._collector_ref = collector
			try:
				if hasattr(collector, 'refresh_auth_from_browser'):
					fresh_token, fresh_cookie = await collector.refresh_auth_from_browser(project_id)
					if fresh_token:
						access_token = fresh_token
						self._log("✅ Đã lấy access_token mới từ Chrome browser")
					if fresh_cookie:
						cookie = fresh_cookie
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
							ft, fc = await collector.refresh_auth_from_browser(project_id)
							if ft:
								access_token = ft
								self._log("✅ [Proactive] access_token đã được refresh!")
					except asyncio.CancelledError:
						return
					except Exception as e:
						self._log(f"⚠️ [Proactive] Lỗi refresh token: {e}")

			proactive_refresh_task = asyncio.create_task(_proactive_token_refresh())

			# ✅ Xử lý SONG SONG: gửi nhiều prompt cùng lúc (giới hạn bởi max_in_flight)
			pending_tasks = []
			prompt_delay = max(0.1, wait_between_effective)  # ✅ Áp dụng đúng thời gian delay cấu hình để tránh rate limit

			for idx_prompt, prompt in enumerate(prompts):
				if self._should_stop():
					self._log("🛑 STOP trong vòng lặp prompt")
					break

				prompt_id = prompt.get("id", idx_prompt + 1)
				prompt_text = prompt.get("description", "") or prompt.get("prompt", "") or ""
				aspect_ratio = self._resolve_aspect_ratio(self.project_data)

				# Chờ nếu đã đạt giới hạn in-flight
				wait_start = time.time()
				while self._count_in_progress() >= max_in_flight:
					if self._should_stop():
						break
					elapsed = int(time.time() - wait_start)
					self._log(f"⏳ Đang tạo ảnh đủ giới hạn {max_in_flight}, chờ {elapsed}s...")
					await asyncio.sleep(3)
				if self._should_stop():
					break

				# Tạo task song song cho prompt này
				task = asyncio.create_task(self._process_prompt(
					collector,
					prompt_id,
					prompt_text,
					aspect_ratio,
					session_id,
					project_id,
					access_token,
					cookie,
					auth,
					output_count,
					response_timeout,
					max_token_retries,
					token_retry_delay,
					max_in_flight,
					inflight_lock,
					token_lock,
					token_counter,
					clear_data_token_image,
					get_token_timeout,
					retry_with_error,
					wait_resend_image,
				))
				pending_tasks.append(task)

				# Delay ngắn giữa các lần gửi để token pool kịp tạo token mới
				if idx_prompt < len(prompts) - 1:
					if not await self._sleep_with_stop(prompt_delay):
						break

			# Chờ tất cả task hoàn tất
			if pending_tasks:
				self._log(f"⏳ Đang chờ {len(pending_tasks)} prompt hoàn thành việc gửi request...")
				await asyncio.gather(*pending_tasks, return_exceptions=True)

			# ✅ Đợi các luồng tải ảnh nền (download futures) hoàn tất trước khi đóng Chrome
			if hasattr(self, "_download_futures") and self._download_futures:
				self._log(f"⏳ Đang chờ tải {len(self._download_futures)} ảnh về máy...")
				try:
					import concurrent.futures
					await asyncio.get_running_loop().run_in_executor(
						None, 
						lambda: concurrent.futures.wait(self._download_futures, timeout=120)
					)
				except Exception as e:
					self._log(f"⚠️ Lỗi khi chờ tải ảnh: {e}")
				finally:
					self._download_futures.clear()

			# ✅ Cancel proactive refresh task
			if proactive_refresh_task:
				proactive_refresh_task.cancel()
				try:
					await proactive_refresh_task
				except (asyncio.CancelledError, Exception):
					pass

		# Sau khi gửi hết prompt, chủ động đóng collector (Chrome + thread token)
		try:
			await collector.close_after_workflow()
		except Exception:
			pass

	async def _process_prompt(
		self,
		collector,
		prompt_id,
		prompt_text,
		aspect_ratio,
		session_id,
		project_id,
		access_token,
		cookie,
		auth,
		output_count,
		response_timeout,
		max_token_retries,
		token_retry_delay,
		max_in_flight,
		inflight_lock,
		token_lock,
		token_counter,
		clear_data_token_image,
		get_token_timeout,
		retry_with_error,
		wait_resend_image,
	):
		if self._should_stop():
			return

		# ✅ Đưa ngay vào state.json để đếm luồng chính xác (giống Text to Video)
		for i in range(output_count):
			self._update_state_entry(prompt_id, prompt_text, "", i, "ACTIVE")

		# ✅ Emit ACTIVE status để UI cập nhật
		self.video_updated.emit({
			"prompt_idx": f"{prompt_id}_1",
			"status": "ACTIVE",
			"scene_id": "",
			"prompt": prompt_text,
			"_prompt_id": prompt_id,
		})

		scene_ids = None
		last_error_msg = ""
		consecutive_403_count = 0
		clear_403_cooldown_until = 0.0
		token_timeout_streak = 0
		token_request_count = 0

		for retry_count in range(retry_with_error):
			try:
				if self._should_stop():
					return

				# ── Lấy token (KHÔNG dùng token_lock — cho phép song song giống Text to Video) ──
				token = None
				token_project_id = ""
				for attempt in range(max_token_retries):
					if self._should_stop():
						return
					try:
						token_request_count += 1
						clear_storage = clear_data_token_image > 0 and (token_request_count % clear_data_token_image == 0)
						token_timeout_for_call = max(get_token_timeout, 60) if clear_storage else get_token_timeout
						token_result = await asyncio.wait_for(
							collector.get_token(clear_storage=clear_storage, token_timeout_override=token_timeout_for_call),
							timeout=token_timeout_for_call,
						)
						# Token pool trả về (token, project_id) hoặc string
						if isinstance(token_result, tuple) and len(token_result) == 2:
							token, token_project_id = token_result
						elif token_result:
							token = token_result
							token_project_id = ""
						if token:
							token_timeout_streak = 0
							break
					except asyncio.TimeoutError:
						self._log(f"⏱️ Timeout lấy token (prompt {prompt_id}, lần {attempt + 1})")
						token_timeout_streak += 1
						if token_timeout_streak >= 2:
							self._log("⚠️ Timeout lấy token liên tiếp, khởi động lại Chrome...")
							try:
								await collector.restart_browser()
							except Exception as e:
								self._log(f"⚠️ Restart Chrome lỗi: {e}")
							token_timeout_streak = 0
					except Exception as e:
						self._log(f"⚠️ Lỗi lấy token: {e}")
					if attempt < max_token_retries - 1:
						await asyncio.sleep(token_retry_delay)

				if not token:
					last_error_msg = "Không lấy được token recaptcha"
					self._log(f"❌ {last_error_msg} (prompt {prompt_id})")
					if retry_count < retry_with_error - 1:
						if not await self._sleep_with_stop(wait_resend_image):
							return
						continue
					# Hết retry → mark FAILED
					fail_scene_ids = scene_ids if scene_ids else [str(uuid.uuid4()) for _ in range(output_count)]
					for idx, sid in enumerate(fail_scene_ids):
						self._update_state_entry(prompt_id, prompt_text, sid, idx, "FAILED", error="TOKEN", message=last_error_msg)
						self.video_updated.emit({
							"prompt_idx": f"{prompt_id}_{idx + 1}", "status": "FAILED",
							"scene_id": sid, "prompt": prompt_text, "_prompt_id": prompt_id,
							"error_code": "TOKEN", "error_message": last_error_msg,
						})
					return

				if self._should_stop():
					return

				# ── Build payload & gửi request (KHÔNG dùng inflight_lock — outer loop đã kiểm soát) ──
				effective_project_id = token_project_id if token_project_id else project_id

				payload = build_generate_image_payload(
					prompt_text,
					session_id,
					effective_project_id,
					token,
					aspect_ratio=aspect_ratio,
					output_count=output_count,
				)
				if scene_ids is None:
					scene_ids = self._assign_scene_ids(payload, prompt_id, prompt_text)
				else:
					for idx, scene_id in enumerate(scene_ids or []):
						self._update_state_entry(prompt_id, prompt_text, scene_id, idx, "PENDING")

				self._save_request_json(payload, prompt_id, prompt_text)

				self._log(f"🚀 [{time.strftime('%H:%M:%S')}] Gửi request tạo ảnh (prompt {prompt_id}), retry {retry_count + 1}/{retry_with_error}...")
				send_started = time.time()

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

				image_api_url = build_generate_image_url(effective_project_id)
				browser_req_timeout_ms = max(30000, int(response_timeout * 1000))
				
				if page_ref and not page_ref.is_closed():
					send_task = asyncio.create_task(request_generate_images_via_browser(
						page_ref,
						image_api_url,
						payload,
						access_token,
						timeout_ms=browser_req_timeout_ms,
					))
				else:
					send_task = asyncio.create_task(request_generate_images(
						payload, access_token, cookie=cookie, project_id=project_id
					))

				try:
					await asyncio.sleep(3)
					if not send_task.done():
						for idx, scene_id in enumerate(scene_ids or []):
							self._update_state_entry(prompt_id, prompt_text, scene_id, idx, "ACTIVE")
							self.video_updated.emit({
								"prompt_idx": f"{prompt_id}_{idx + 1}",
								"status": "ACTIVE",
								"scene_id": scene_id,
								"prompt": prompt_text,
								"_prompt_id": prompt_id,
							})

					remaining = response_timeout - (time.time() - send_started)
					if remaining <= 0:
						raise asyncio.TimeoutError()
					response = await asyncio.wait_for(send_task, timeout=remaining)
					self._save_response_json(response, prompt_id, prompt_text)
				except asyncio.TimeoutError:
					last_error_msg = "Timeout chờ ảnh"
					
					if retry_count < retry_with_error - 1:
						self._log(f"⏱️ {response_timeout}s timeout tạo ảnh (prompt {prompt_id}), chờ để retry ({retry_count + 1}/{retry_with_error})...")
						if not await self._sleep_with_stop(wait_resend_image):
							return
						continue

					self._log(f"⏱️ {response_timeout}s timeout tạo ảnh (prompt {prompt_id})")
					for idx, scene_id in enumerate(scene_ids or []):
						self._update_state_entry(prompt_id, prompt_text, scene_id, idx, "FAILED", error="TIMEOUT", message=last_error_msg)
						self.video_updated.emit({
							"prompt_idx": f"{prompt_id}_{idx + 1}",
							"status": "FAILED",
							"scene_id": scene_id,
							"prompt": prompt_text,
							"_prompt_id": prompt_id,
							"error_code": "TIMEOUT",
							"error_message": last_error_msg,
						})
					return
				except Exception as e:
					last_error_msg = str(e)
					
					if retry_count < retry_with_error - 1:
						self._log(f"⚠️ Lỗi gửi request tạo ảnh: {e}, chờ để retry ({retry_count + 1}/{retry_with_error})...")
						if not await self._sleep_with_stop(wait_resend_image):
							return
						continue

					self._log(f"❌ Lỗi gửi request tạo ảnh: {e}")
					for idx, scene_id in enumerate(scene_ids or []):
						self._update_state_entry(prompt_id, prompt_text, scene_id, idx, "FAILED", error="REQUEST", message=last_error_msg)
						self.video_updated.emit({
							"prompt_idx": f"{prompt_id}_{idx + 1}",
							"status": "FAILED",
							"scene_id": scene_id,
							"prompt": prompt_text,
							"_prompt_id": prompt_id,
							"error_code": "REQUEST",
							"error_message": last_error_msg,
						})
					return

				response_body = response.get("body", "")
				error_code, error_message, error_reason = self._extract_error_info(response_body)
				http_status = str(response.get("status") or "").strip()
				body_error_code = str(error_code or "").strip()
				# Dùng body error code nếu có, nếu không thì dùng HTTP status
				error_code_str = body_error_code if body_error_code else http_status

				# ✅ Lỗi nội dung (UNSAFE/CENSORED): KHÔNG retry, skip prompt ngay
				# QUAN TRỌNG: Nếu message là "invalid argument" thì đây là lỗi tham số, CẦN retry
				non_retryable_reasons = {
					"PUBLIC_ERROR_UNSAFE_GENERATION",
					"PUBLIC_ERROR_SOMETHING_WENT_WRONG_UNSAFE",
					"SAFETY_FILTER",
					"CONTENT_FILTERED",
				}
				# Phân biệt: "invalid argument" = lỗi tham số (retryable), không phải nội dung
				is_actually_content_error = (
					error_reason and error_reason.upper() in {r.upper() for r in non_retryable_reasons}
					and "invalid argument" not in (error_message or "").lower()
				)
				if is_actually_content_error:
					msg = error_message or error_reason
					self._log(f"🚫 Prompt {prompt_id} bị chặn bởi bộ lọc nội dung: {error_reason}")
					self._log(f"   → Bỏ qua prompt này (không retry)")
					for idx, scene_id in enumerate(scene_ids or []):
						self._update_state_entry(prompt_id, prompt_text, scene_id, idx, "FAILED", error=error_reason, message=msg)
						self.video_updated.emit({
							"prompt_idx": f"{prompt_id}_{idx + 1}",
							"status": "FAILED",
							"scene_id": scene_id,
							"prompt": prompt_text,
							"_prompt_id": prompt_id,
							"error_code": error_reason,
							"error_message": msg,
						})
					return  # Skip ngay, không retry

				# ✅ Handle retryable errors (403, 401, 3, 13, 53)
				retryable_errors = {"403", "401", "3", "13", "53", "16", "429", "400", "500", "503"}
				is_retryable = not response.get("ok", True) and (
					error_code_str in retryable_errors or http_status in retryable_errors
				)
				if is_retryable:
					if error_code_str == "403" or http_status == "403":
						consecutive_403_count += 1
					else:
						consecutive_403_count = 0

					msg = error_message or response.get("reason") or response.get("error") or "Unknown error"
					last_error_msg = msg
					self._log(f"⚠️ Lỗi {error_code_str} (prompt {prompt_id}): {msg}")
					# Debug: log response body ngắn gọn
					body_preview = str(response_body or "")[:300]
					if body_preview:
						self._log(f"[DEBUG] Response body: {body_preview}")
					
					if retry_count >= retry_with_error - 1:
						for idx, scene_id in enumerate(scene_ids or []):
							self._update_state_entry(prompt_id, prompt_text, scene_id, idx, "FAILED", error=error_code_str, message=msg)
							self.video_updated.emit({
								"prompt_idx": f"{prompt_id}_{idx + 1}",
								"status": "FAILED",
								"scene_id": scene_id,
								"prompt": prompt_text,
								"_prompt_id": prompt_id,
								"error_code": error_code_str,
								"error_message": msg,
							})

					# 🔧 Handle 401 - refresh OAuth token từ Chrome browser (có lock)
					is_auth_error = error_code_str in ("401", "16") or http_status in ("401",)
					if is_auth_error:
						self._log("🔄 Token OAuth hết hạn, lấy mới từ Chrome browser...")
						try:
							if hasattr(collector, 'refresh_auth_from_browser'):
								fresh_token, fresh_cookie = await collector.refresh_auth_from_browser(project_id)
								if fresh_token:
									access_token = fresh_token
									if auth:
										auth["access_token"] = fresh_token
									self._log("✅ Token OAuth đã được làm mới từ Chrome")
								if fresh_cookie:
									cookie = fresh_cookie
							else:
								import auth_helper
								new_token = auth_helper.get_valid_access_token(cookie, project_id)
								if new_token:
									access_token = new_token
						except Exception as e:
							self._log(f"⚠️ Lỗi refresh OAuth: {e}")
						# ✅ Auth error KHÔNG đếm retry
						if not await self._sleep_with_stop(2):
							return
						continue

					# 🔧 403 lần đầu: chờ 10s rồi retry (clear storage nhẹ)
					if (error_code_str == "403" or http_status == "403") and consecutive_403_count == 1:
						self._log(f"⚠️ Lỗi 403 lần 1, chờ 10s rồi retry...")
						if not await self._sleep_with_stop(10):
							return
						continue

					# 🔧 Lần 2 consecutive 403: clear storage (có cooldown để tránh clear liên tục)
					if (error_code_str == "403" or http_status == "403") and consecutive_403_count == 2:
						now_ts = time.time()
						if now_ts < clear_403_cooldown_until:
							self._log("⚠️ Vừa clear storage gần đây, bỏ qua clear và restart Chrome...")
							await collector.restart_browser()
							consecutive_403_count = 0
							continue
						self._log(f"⚠️ Lỗi 403 lần {consecutive_403_count}, chạy clear storage...")
						try:
							await asyncio.wait_for(
								collector.get_token(clear_storage=True),
								timeout=60
							)
							consecutive_403_count = 0
							clear_403_cooldown_until = time.time() + 120
							try:
								token_counter["count"] = 0
							except Exception:
								pass
							self._log("✅ Clear storage xong, retry prompt")
						except Exception as e:
							self._log(f"⚠️ Clear storage lỗi: {e}")
						if not await self._sleep_with_stop(wait_resend_image):
							return
						continue

					# 🔧 Lần 3+ consecutive 403: restart chrome
					if (error_code_str == "403" or http_status == "403") and consecutive_403_count >= 3:
						self._log("⚠️ Lỗi 403 liên tiếp, khởi động lại Chrome...")
						try:
							await collector.restart_browser()
						except Exception as e:
							self._log(f"⚠️ Restart Chrome lỗi: {e}")
						consecutive_403_count = 0
						continue

					# Other retryable errors (400, 3, 13, 53)
					if retry_count < retry_with_error - 1:
						self._log(f"⚠️ Chờ {wait_resend_image}s rồi retry prompt {prompt_id} ({retry_count + 1}/{retry_with_error})")
						if not await self._sleep_with_stop(wait_resend_image):
							return
						continue
					return

				if not response.get("ok", True) or error_message:
					code = str(response.get("status") or error_code or "")
					msg = error_message or response.get("reason") or response.get("error") or "Unknown error"
					last_error_msg = msg
					
					if retry_count < retry_with_error - 1:
						self._log(f"⚠️ Chờ {wait_resend_image}s rồi retry prompt {prompt_id} ({retry_count + 1}/{retry_with_error})")
						if not await self._sleep_with_stop(wait_resend_image):
							return
						continue

					self._log(f"❌ API lỗi (prompt {prompt_id}): {msg}")
					for idx, scene_id in enumerate(scene_ids or []):
						self._update_state_entry(prompt_id, prompt_text, scene_id, idx, "FAILED", error=code, message=msg)
						self.video_updated.emit({
							"prompt_idx": f"{prompt_id}_{idx + 1}",
							"status": "FAILED",
							"scene_id": scene_id,
							"prompt": prompt_text,
							"_prompt_id": prompt_id,
							"error_code": code,
							"error_message": msg,
						})
					return

				medias = parse_media_from_response(response_body)
				if not medias:
					last_error_msg = "Không nhận được ảnh"
					self._log(f"⚠️ API không trả về ảnh (prompt {prompt_id})")
					for idx, scene_id in enumerate(scene_ids or []):
						self._update_state_entry(prompt_id, prompt_text, scene_id, idx, "FAILED", message=last_error_msg)
						self.video_updated.emit({
							"prompt_idx": f"{prompt_id}_{idx + 1}",
							"status": "FAILED",
							"scene_id": scene_id,
							"prompt": prompt_text,
							"_prompt_id": prompt_id,
							"error_message": last_error_msg,
						})
					if retry_count < retry_with_error - 1:
						if not await self._sleep_with_stop(wait_resend_image):
							return
						continue
					return

				for idx, scene_id in enumerate(scene_ids or []):
					media = medias[idx] if idx < len(medias) else {}
					image_url = media.get("downloadUrl") or media.get("uri") or ""
					if not image_url:
						self._log(f"⚠️ Prompt {prompt_id} scene {idx + 1}: API không có downloadUrl/uri")
					
					self.video_updated.emit({
						"prompt_idx": f"{prompt_id}_{idx + 1}",
						"status": "DOWNLOADING",
						"scene_id": scene_id,
						"prompt": prompt_text,
						"_prompt_id": prompt_id,
					})
					self._update_state_entry(
						prompt_id, prompt_text, scene_id, idx, "DOWNLOADING",
						image_url=image_url
					)

					def dl_task(p_id, p_text, s_id, i_idx, i_url, p_idx_str):
						i_path = ""
						if i_url:
							i_path = self._download_image(i_url, p_idx_str)
						
						self._update_state_entry(
							p_id, p_text, s_id, i_idx, "SUCCESSFUL",
							image_url=i_url, image_path=i_path
						)
						self.video_updated.emit({
							"prompt_idx": p_idx_str,
							"status": "SUCCESSFUL",
							"scene_id": s_id,
							"prompt": p_text,
							"image_path": i_path,
							"_prompt_id": p_id,
						})

					if not hasattr(self, "_download_futures"):
						self._download_futures = []
					future = self._download_executor.submit(
						dl_task,
						prompt_id, prompt_text, scene_id, idx,
						image_url, f"{prompt_id}_{idx + 1}"
					)
					self._download_futures.append(future)

				self._save_auth_to_state(access_token, session_id, project_id)
				return
			finally:
				pass

		self._log(f"❌ Hết số lần retry ({retry_with_error}) cho prompt {prompt_id}: {last_error_msg}")


def start_generate_image(app, project_name, project_data, project_file, *, manage_buttons=True, prompt_ids_filter=None):
	"""Start image generation workflow from UI app context."""
	try:
		if hasattr(app, "add_log"):
			app.add_log(f"🚦 Bắt đầu Tạo Ảnh cho project '{project_name}'")
		app.workflow = GenerateImageWorkflow(
			project_name=project_name,
			project_data=project_data,
			prompt_ids_filter=prompt_ids_filter,
		)
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
	except Exception as e:
		try:
			app.add_log(f"❌ Lỗi chạy Generate Image: {e}")
		except Exception:
			pass
		return False

	return True
