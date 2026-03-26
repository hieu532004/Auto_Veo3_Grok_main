import asyncio
import json
import os
import shutil
import time
from pathlib import Path
from A_workflow_get_token import TokenCollector
from settings_manager import SettingsManager


class TokenPool:
	"""TokenPool: N Chrome instances riêng biệt.
	
	Login session chia sẻ qua:
	1. File sync (copy files KHÔNG bị lock như Local State, Preferences)
	2. CDP cookie injection (inject cookies từ Chrome-0 vào pool Chrome sau khi start)
	"""

	# Các thư mục cache, không cần copy
	_EXCLUDE_DIRS = {
		"Cache", "Code Cache", "GPUCache", "GrShaderCache", "ShaderCache",
		"DawnWebGPUCache", "DawnGraphiteCache", "GraphiteDawnCache",
		"GPUPersistentCache", "blob_storage", "Service Worker",
		"optimization_guide_hint_cache_store", "Safe Browsing Network",
		"VideoDecodeStats", "shared_proto_db", "webrtc_event_logs",
		"Feature Engagement Tracker", "Segmentation Platform",
		"GCM Store", "Download Service",
	}
	_EXCLUDE_FILES = {
		"SingletonLock", "SingletonCookie", "SingletonSocket",
		"lockfile", "LOCK", "LOG", "LOG.old",
	}

	def __init__(
		self,
		num_chrome=2,
		project_url="https://labs.google/fx/vi/tools/flow",
		chrome_userdata_root=None,
		profile_name=None,
		log_callback=None,
		stop_check=None,
		mode="video",
		token_timeout=60,
		clear_data_interval=1,
		clear_data_wait=2,
		keep_chrome_open=True,
		close_chrome_after_token=False,
		hide_window=False,
	):
		self.num_chrome = max(1, min(int(num_chrome), 4))
		self.project_url = project_url
		self.chrome_userdata_root = chrome_userdata_root
		self.profile_name = profile_name
		self.log_callback = log_callback
		self.stop_check = stop_check
		self.mode = mode
		self.token_timeout = token_timeout
		self.clear_data_interval = clear_data_interval
		self.clear_data_wait = clear_data_wait
		self.keep_chrome_open = keep_chrome_open
		self.close_chrome_after_token = close_chrome_after_token
		self.hide_window = hide_window

		self._collectors = []
		self._token_queue = asyncio.Queue(maxsize=16)
		self._harvest_tasks = []
		self._started = False
		self._stop_flag = False
		self._base_port = 9222
		self._lock = asyncio.Lock()
		self._auth_refresh_lock = asyncio.Lock()
		self._last_auth_refresh = 0
		self._total_tokens = 0
		self._token_request_counts = {}
		# Mỗi Chrome instance có project_id riêng để tránh rate limit
		self._instance_project_ids = {}  # {idx: project_id_str}

	def _log(self, msg):
		if callable(self.log_callback):
			try:
				self.log_callback(msg)
			except Exception:
				pass

	def _should_stop(self):
		if self._stop_flag:
			return True
		if callable(self.stop_check):
			try:
				return bool(self.stop_check())
			except Exception:
				pass
		return False

	@staticmethod
	def get_pool_profile_dir(base_profile_dir, idx):
		if idx == 0:
			return str(base_profile_dir)
		parent = str(Path(base_profile_dir).parent)
		name = Path(base_profile_dir).name
		pool_dir = os.path.join(parent, f"{name}_pool_{idx}")
		os.makedirs(pool_dir, exist_ok=True)
		return pool_dir

	@staticmethod
	def get_all_pool_dirs(base_profile_dir, num_chrome):
		dirs = []
		for i in range(num_chrome):
			dirs.append(TokenPool.get_pool_profile_dir(base_profile_dir, i))
		return dirs

	# ───────────────────────────────────────────────────────────────────
	#  FILE SYNC: Copy files không bị lock (Preferences, Local State...)
	# ───────────────────────────────────────────────────────────────────

	@staticmethod
	def _sync_unlocked_files(base_profile_dir, num_chrome, log_func=None):
		"""Copy các files KHÔNG bị Chrome lock từ profile gốc sang pool.
		
		Cookies và DB files bị Chrome lock → sẽ dùng CDP inject sau.
		"""
		base_dir = Path(base_profile_dir)
		if not base_dir.exists():
			return

		def _log(msg):
			if callable(log_func):
				try:
					log_func(msg)
				except Exception:
					pass

		for idx in range(1, num_chrome):
			pool_dir = Path(TokenPool.get_pool_profile_dir(str(base_dir), idx))
			pool_dir.mkdir(parents=True, exist_ok=True)

			copied = 0
			skipped = 0

			# Copy Local State (usually not locked even when Chrome runs)
			src_ls = base_dir / "Local State"
			dst_ls = pool_dir / "Local State"
			if src_ls.exists():
				try:
					shutil.copy2(str(src_ls), str(dst_ls))
					copied += 1
				except Exception:
					skipped += 1

			# Copy Default/ - skip files that are locked
			src_default = base_dir / "Default"
			dst_default = pool_dir / "Default"
			if not src_default.exists():
				continue
			dst_default.mkdir(parents=True, exist_ok=True)

			try:
				items = list(src_default.iterdir())
			except Exception:
				continue

			for item in items:
				if item.is_dir() and item.name in TokenPool._EXCLUDE_DIRS:
					continue
				if item.is_file() and item.name in TokenPool._EXCLUDE_FILES:
					continue
				dst_item = dst_default / item.name
				try:
					if item.is_dir():
						if dst_item.exists():
							shutil.rmtree(str(dst_item), ignore_errors=True)
						shutil.copytree(str(item), str(dst_item), dirs_exist_ok=True)
						copied += 1
					elif item.is_file():
						shutil.copy2(str(item), str(dst_item))
						copied += 1
				except PermissionError:
					skipped += 1
				except Exception:
					skipped += 1

			# Copy root-level files
			for root_item in base_dir.iterdir():
				if root_item.name in ("Default", "Local State"):
					continue
				if root_item.is_dir() and (root_item.name in TokenPool._EXCLUDE_DIRS or "_pool_" in root_item.name):
					continue
				if root_item.is_file() and root_item.name in TokenPool._EXCLUDE_FILES:
					continue
				dst_root = pool_dir / root_item.name
				try:
					if root_item.is_file():
						shutil.copy2(str(root_item), str(dst_root))
						copied += 1
					elif root_item.is_dir():
						if dst_root.exists():
							shutil.rmtree(str(dst_root), ignore_errors=True)
						shutil.copytree(str(root_item), str(dst_root), dirs_exist_ok=True)
						copied += 1
				except Exception:
					pass

			_log(f"🔄 File sync {base_dir.name} → {pool_dir.name} ({copied} ok, {skipped} bị lock)")

	# ───────────────────────────────────────────────────────────────────
	#  CDP COOKIE INJECTION: Lấy cookies từ Chrome-0, inject vào pool
	# ───────────────────────────────────────────────────────────────────

	async def _export_cookies_from_collector(self, collector_idx=0):
		"""Export ALL cookies từ Chrome-0 qua CDP (đã decrypt)."""
		if collector_idx >= len(self._collectors):
			return []
		collector = self._collectors[collector_idx]
		if not collector.context:
			return []
		try:
			cookies = await collector.context.cookies()
			return cookies or []
		except Exception as e:
			self._log(f"⚠️ Export cookies từ Chrome-{collector_idx} lỗi: {e}")
			return []

	async def _inject_cookies_to_collector(self, collector_idx, cookies):
		"""Inject cookies vào Chrome pool instance qua CDP, rồi navigate lại.
		
		Cookies Google Auth cần được set đúng domain nên ta:
		1. Navigate tới accounts.google.com trước để set Google auth cookies
		2. Navigate tới labs.google để set session cookies
		3. Cuối cùng navigate tới project URL
		"""
		if collector_idx >= len(self._collectors) or not cookies:
			return False
		collector = self._collectors[collector_idx]
		if not collector.context or not collector.page:
			return False
		try:
			# Phân loại cookies theo domain
			google_auth_cookies = []
			labs_cookies = []
			other_cookies = []
			for c in cookies:
				domain = str(c.get("domain", "")).lower()
				if "google.com" in domain and "labs" not in domain:
					google_auth_cookies.append(c)
				elif "labs.google" in domain:
					labs_cookies.append(c)
				else:
					other_cookies.append(c)
			
			# Bước 1: Navigate tới Google trước để set auth cookies
			if google_auth_cookies:
				try:
					await collector.page.goto("https://accounts.google.com", wait_until="domcontentloaded", timeout=15000)
				except Exception:
					pass
				await collector.context.add_cookies(google_auth_cookies)
				self._log(f"🍪 Chrome-{collector_idx}: inject {len(google_auth_cookies)} Google auth cookies")
			
			# Bước 2: Navigate tới labs.google để set session cookies
			if labs_cookies:
				try:
					await collector.page.goto("https://labs.google", wait_until="domcontentloaded", timeout=15000)
				except Exception:
					pass
				await collector.context.add_cookies(labs_cookies)
				self._log(f"🍪 Chrome-{collector_idx}: inject {len(labs_cookies)} labs.google cookies")
			
			# Bước 3: Inject tất cả cookies còn lại
			if other_cookies:
				await collector.context.add_cookies(other_cookies)
			
			# Bước 4: Navigate tới project URL để verify đăng nhập
			try:
				await collector.page.goto(
					self.project_url,
					wait_until="domcontentloaded",
					timeout=25000,
				)
			except Exception:
				pass
			try:
				await collector.page.wait_for_load_state("networkidle", timeout=15000)
			except Exception:
				pass
			
			# Bước 5: Kiểm tra xem có bị redirect về trang login không
			current_url = ""
			try:
				current_url = collector.page.url or ""
			except Exception:
				pass
			if "accounts.google.com" in current_url or "signin" in current_url.lower():
				self._log(f"⚠️ Chrome-{collector_idx}: Vẫn bị redirect về login sau inject cookies!")
				return False
			
			return True
		except Exception as e:
			self._log(f"⚠️ Inject cookies vào Chrome-{collector_idx} lỗi: {e}")
			return False

	# ───────────────────────────────────────────────────────────────────
	#  START: 4 Chrome riêng biệt + Cookie injection
	# ───────────────────────────────────────────────────────────────────

	async def start(self):
		if self._started:
			return
		self._started = True
		self._stop_flag = False
		self._log(f"🚀 TokenPool: khởi động {self.num_chrome} Chrome instances")

		# ── Bước 1: Copy files không bị lock ──
		if self.num_chrome > 1:
			base_userdata = self._get_userdata_for_instance(0)
			self._log(f"🔄 Sync files cho {self.num_chrome - 1} pool profiles...")
			try:
				TokenPool._sync_unlocked_files(
					base_userdata, self.num_chrome, log_func=self._log
				)
			except Exception as e:
				self._log(f"⚠️ Lỗi sync files: {e}")

		# ── Bước 2: Tạo collectors ──
		for i in range(self.num_chrome):
			port = self._base_port + i
			userdata = self._get_userdata_for_instance(i)
			self._log(f"🔧 Chrome-{i}: port={port} | profile={Path(userdata).name}")
			collector = TokenCollector(
				project_url=self.project_url,
				chrome_userdata_root=userdata,
				profile_name=self.profile_name,
				debug_port=port,
				headless=False,
				hide_window=self.hide_window,
				token_timeout=self.token_timeout,
				idle_timeout=600,
				log_callback=lambda msg, idx=i: self._log(f"[Chrome-{idx}] {msg}"),
				stop_check=self._should_stop,
				clear_data_interval=self.clear_data_interval,
				keep_chrome_open=True,
				close_chrome_after_token=False,
				mode=self.mode,
			)
			collector._pool_mode = True
			self._collectors.append(collector)

		# ── Bước 3: Khởi động Chrome-0 TRƯỚC ──
		if self._should_stop():
			return
		try:
			await self._collectors[0].__aenter__()
			self._log(f"✅ Chrome-0 đã khởi động (port {self._base_port})")
		except Exception as e:
			self._log(f"❌ Chrome-0 khởi động lỗi: {e}")
		await asyncio.sleep(2)

		# ── Bước 4: Export cookies từ Chrome-0 ──
		cookies = []
		if self.num_chrome > 1:
			self._log("🍪 Lấy cookies từ Chrome-0...")
			cookies = await self._export_cookies_from_collector(0)
			self._log(f"🍪 Đã lấy {len(cookies)} cookies từ Chrome-0")

		# ── Bước 5: Khởi động Chrome-1/2/3 + inject cookies ──
		for i in range(1, len(self._collectors)):
			if self._should_stop():
				return
			try:
				await self._collectors[i].__aenter__()
				self._log(f"✅ Chrome-{i} đã khởi động (port {self._base_port + i})")

				# Inject cookies từ Chrome-0
				if cookies:
					ok = await self._inject_cookies_to_collector(i, cookies)
					if ok:
						self._log(f"🍪 Chrome-{i}: inject cookies + navigate OK")
					else:
						# Retry: lấy lại cookies từ Chrome-0 (có thể Chrome-0 mới xong login)
						self._log(f"⚠️ Chrome-{i}: inject cookies thất bại, thử lấy lại cookies từ Chrome-0...")
						await asyncio.sleep(3)
						cookies = await self._export_cookies_from_collector(0)
						if cookies:
							ok2 = await self._inject_cookies_to_collector(i, cookies)
							if ok2:
								self._log(f"🍪 Chrome-{i}: inject cookies lần 2 OK!")
							else:
								self._log(f"⚠️ Chrome-{i}: inject cookies lần 2 vẫn thất bại. Chrome-{i} sẽ dùng token từ profile gốc.")
			except Exception as e:
				self._log(f"❌ Chrome-{i} khởi động lỗi: {e}")
			await asyncio.sleep(2)

		# ── Bước 6: Bắt đầu harvest loops ──
		for i, collector in enumerate(self._collectors):
			task = asyncio.create_task(self._harvest_loop(i, collector))
			self._harvest_tasks.append(task)
		
		self._log(f"✅ TokenPool: {len(self._harvest_tasks)} Chrome đang harvest token liên tục")

	def _get_userdata_for_instance(self, idx):
		if not self.chrome_userdata_root:
			base = SettingsManager.create_chrome_userdata_folder(self.profile_name)
		else:
			base = self.chrome_userdata_root
		return TokenPool.get_pool_profile_dir(base, idx)


	def get_instance_project_id(self, idx):
		"""Lấy project_id của Chrome instance {idx}."""
		return self._instance_project_ids.get(idx, "")

	# ───────────────────────────────────────────────────────────────────
	#  HARVEST LOOP
	# ───────────────────────────────────────────────────────────────────

	async def _harvest_loop(self, idx, collector):
		fail_streak = 0
		self._token_request_counts[idx] = 0

		while not self._should_stop():
			import random
			await asyncio.sleep(idx * random.uniform(1.0, 3.0)) # Jittered start
			try:
				# Token hợp lệ 120s, không cần queue quá nhiều
				queue_limit = 3
				if self._token_queue.qsize() >= queue_limit:
					await asyncio.sleep(1)
					continue

				clear_storage = (
					self.clear_data_interval > 0
					and self._token_request_counts[idx] > 0
					and (self._token_request_counts[idx] % self.clear_data_interval == 0)
				)

				self._token_request_counts[idx] += 1
				token = await asyncio.wait_for(
					collector.get_token(clear_storage=clear_storage),
					timeout=max(90, self.token_timeout + 30),
				)

				if token:
					# Token kèm project_id của Chrome instance này
					pid = self._instance_project_ids.get(idx, "")
					if not hasattr(self, '_token_to_idx'):
						self._token_to_idx = {}
					self._token_to_idx[token] = idx
					# Giữ dict nhỏ gọn: chỉ giữ 20 token gần nhất
					if len(self._token_to_idx) > 20:
						oldest_keys = list(self._token_to_idx.keys())[:-20]
						for k in oldest_keys:
							self._token_to_idx.pop(k, None)
					await self._token_queue.put((token, time.time(), pid))
					self._total_tokens += 1
					fail_streak = 0
					self._log(f"[Chrome-{idx}] 🎯 Token #{self._total_tokens} (pool={self._token_queue.qsize()})")
				else:
					fail_streak += 1
					self._log(f"[Chrome-{idx}] ⚠️ Không lấy được token (fail #{fail_streak})")
					if fail_streak >= 3:
						if getattr(collector, "_login_required", False):
							self._log(f"[Chrome-{idx}] ⚠️ Phát hiện mất phiên đăng nhập! Chrome sẽ tự động reload để bạn login!...")
						else:
							self._log(f"[Chrome-{idx}] 🔄 Restart Chrome sau {fail_streak} lần thất bại")
						try:
							await collector.restart_browser()
							if idx > 0:
								await self._resync_cookies_to_instance(idx)
							# ✅ Đảm bảo Chrome ở đúng project URL sau restart
							await self._ensure_project_url_for_instance(idx)
						except Exception:
							pass
						fail_streak = 0
					await asyncio.sleep(3)

				import random
				wait_time = max(1, self.clear_data_wait) + random.uniform(0.5, 3.5)
				for _ in range(int(wait_time * 2)):
					if self._should_stop():
						return
					await asyncio.sleep(0.5)

			except asyncio.TimeoutError:
				fail_streak += 1
				self._log(f"[Chrome-{idx}] ⏱️ Timeout lấy token")
				if fail_streak >= 2:
					if getattr(collector, "_login_required", False):
						self._log(f"[Chrome-{idx}] ⚠️ Phát hiện màn hình Sign In. Hãy tự đăng nhập! Chrome sẽ tự động reload...")
					try:
						await collector.restart_browser()
						if idx > 0:
							await self._resync_cookies_to_instance(idx)
						# ✅ Đảm bảo Chrome ở đúng project URL sau restart
						await self._ensure_project_url_for_instance(idx)
					except Exception:
						pass
					fail_streak = 0
				await asyncio.sleep(3)
			except asyncio.CancelledError:
				return
			except Exception as e:
				self._log(f"[Chrome-{idx}] ❌ Lỗi harvest: {e}")
				err_str = str(e).lower()
				if "target closed" in err_str or "connection closed" in err_str or "disconnected" in err_str:
					fail_streak += 3
				await asyncio.sleep(2)

	async def _resync_cookies_to_instance(self, target_idx):
		"""Re-inject cookies từ Chrome-0 vào Chrome-{target_idx} sau restart."""
		if target_idx == 0 or not self._collectors:
			return
		try:
			cookies = await self._export_cookies_from_collector(0)
			if cookies:
				await self._inject_cookies_to_collector(target_idx, cookies)
				self._log(f"🍪 Re-sync cookies cho Chrome-{target_idx} OK")
		except Exception as e:
			self._log(f"⚠️ Re-sync cookies Chrome-{target_idx} lỗi: {e}")

	async def _ensure_project_url_for_instance(self, idx):
		"""Đảm bảo Chrome instance {idx} đang ở đúng project URL (/project/UUID).
		
		Khi Chrome restart, nó có thể là trang /flow (không có Video mode tab).
		Method này check và navigate lại về project URL nếu cần.
		"""
		if idx >= len(self._collectors):
			return
		collector = self._collectors[idx]
		if not collector or not getattr(collector, 'page', None):
			return
		try:
			current_url = collector.page.url or ""
		except Exception:
			return
		
		# Nếu đã ở /project/ URL thì OK
		if "/project/" in current_url:
			return
		
		# Nếu project_url của collector là /project/ URL thì navigate tới đó
		if hasattr(collector, 'project_url') and "/project/" in str(collector.project_url or ""):
			await self._navigate_to_base_project(idx, collector.project_url)
			return
		
		# Thử lấy project URL từ config
		try:
			config = SettingsManager.load_config()
			saved_url = ""
			if isinstance(config, dict):
				saved_url = config.get("account1", {}).get("URL_GEN_TOKEN", "")
			if saved_url and "/project/" in saved_url:
				await self._navigate_to_base_project(idx, saved_url)
				# Cập nhật project_url cho collector
				if hasattr(collector, 'project_url'):
					collector.project_url = saved_url
		except Exception as e:
			self._log(f"⚠️ Chrome-{idx}: lỗi lấy project URL từ config: {e}")

	async def _navigate_to_base_project(self, idx, project_url):
		"""Navigate Chrome instance {idx} tới project URL."""
		if idx >= len(self._collectors):
			return
		collector = self._collectors[idx]
		if not collector or not getattr(collector, 'page', None):
			return
		try:
			self._log(f"🔄 Chrome-{idx}: navigate về project URL...")
			await collector.page.goto(project_url, wait_until="domcontentloaded", timeout=20000)
			try:
				await collector.page.wait_for_load_state("networkidle", timeout=10000)
			except Exception:
				pass
		except Exception as e:
			self._log(f"⚠️ Chrome-{idx}: lỗi navigate về project URL: {e}")

	async def get_token(self, timeout=120, **kwargs):
		"""Trả về (token, project_id) hoặc None nếu timeout."""
		if not self._started:
			await self.start()
		start_wait = time.time()
		while time.time() - start_wait < timeout:
			if self._should_stop():
				return None
			try:
				token_data = await asyncio.wait_for(self._token_queue.get(), timeout=2.0)
				if isinstance(token_data, tuple) and len(token_data) == 3:
					token, ts, pid = token_data
					token_age = time.time() - ts
					if token_age < 40:
						return (token, pid)
					else:
						self._log(f"♻️ Loại bỏ token quá hạn ({int(time.time() - ts)}s)")
						continue
				elif isinstance(token_data, tuple) and len(token_data) == 2:
					# Backward compat: (token, ts) không có project_id
					token, ts = token_data
					token_age = time.time() - ts
					if token_age < 40:
						return (token, "")
					else:
						continue
				else:
					return (token_data, "")
			except asyncio.TimeoutError:
				pass
			except Exception as e:
				self._log(f"Lỗi lấy token từ queue: {e}")
				await asyncio.sleep(1)
		self._log(f"⏱️ TokenPool: timeout {timeout}s cho token")
		return None

	async def force_auto_login(self):
		"""Được gọi khi 401 fail liên tục. RESTART Chrome-0 hoàn toàn để lấy token mới."""
		if not hasattr(self, '_last_force_login'):
			self._last_force_login = 0
		import time
		if time.time() - self._last_force_login < 60:
			self._log("ℹ️ Chrome-0 vừa được restart gần đây. Đang gọi lại refresh_auth_from_browser...")
			return await self.refresh_auth_from_browser()
		self._last_force_login = time.time()
		
		self._log("🚨 Token hết hạn. RESTART Chrome-0 để lấy access_token MỚI...")
		new_token = None
		new_cookie = ""
		try:
			# 1. Xóa tất cả token cũ trong queue
			drained = 0
			while not self._token_queue.empty():
				try:
					self._token_queue.get_nowait()
					drained += 1
				except Exception:
					break
			if drained:
				self._log(f"🗑️ Đã loại bỏ {drained} token cũ")

			# 2. RESTART Chrome-0 hoàn toàn
			if self._collectors and len(self._collectors) > 0:
				collector0 = self._collectors[0]
				try:
					self._log("🔄 Đang restart Chrome-0...")
					await collector0.restart_browser()
					self._log("✅ Chrome-0 đã restart xong")
					await asyncio.sleep(3)
				except Exception as e:
					self._log(f"⚠️ Lỗi restart Chrome-0: {e}")

			# 3. Lấy cookies mới từ Chrome-0
			new_cookie = await self.get_browser_cookie_string()

			# 4. Thử lấy token qua NextAuth session API
			if self._collectors and len(self._collectors) > 0:
				collector0 = self._collectors[0]
				if collector0 and getattr(collector0, 'page', None) and not collector0.page.is_closed():
					try:
						session_data = await asyncio.wait_for(
							collector0.page.evaluate("""() => {
								return fetch('/fx/api/auth/session', {
									credentials: 'include',
									headers: { 'Accept': 'application/json' }
								})
								.then(r => r.json())
								.catch(e => null);
							}"""),
							timeout=15
						)
						if session_data and isinstance(session_data, dict):
							token = (session_data.get("accessToken")
							         or session_data.get("access_token")
							         or "")
							if token and len(token) > 20:
								new_token = token
								self._log("✅ Đã lấy access_token MỚI từ NextAuth session API!")
					except asyncio.TimeoutError:
						self._log("⚠️ NextAuth session API timeout")
					except Exception as e:
						self._log(f"⚠️ Lỗi gọi session API: {e}")

			# 5. Fallback: fetch token từ HTML
			if not new_token and self._collectors and len(self._collectors) > 0:
				collector0 = self._collectors[0]
				if collector0 and getattr(collector0, 'page', None) and not collector0.page.is_closed():
					try:
						fetch_result = await asyncio.wait_for(
							collector0.page.evaluate("""() => {
								return fetch(window.location.href, {
									credentials: 'include',
									headers: {'Accept':'text/html','Cache-Control':'no-cache,no-store','Pragma':'no-cache'}
								}).then(r => r.text()).then(html => {
									const m = html.match(/"access_token":"([^"]+)"/);
									return m ? m[1] : null;
								}).catch(e => null);
							}"""),
							timeout=15
						)
						if fetch_result and len(str(fetch_result)) > 20:
							new_token = fetch_result
							self._log("✅ Đã fetch access_token MỚI từ HTML!")
					except Exception:
						pass

			# 6. Fallback cuối: auth_helper HTTP
			if not new_token and new_cookie:
				from auth_helper import invalidate_cache
				invalidate_cache()
				import auth_helper
				new_token = auth_helper.get_valid_access_token(new_cookie, self._get_project_id(), force_refresh=True)
				if new_token:
					self._log("✅ Fallback: lấy token từ HTTP request")

			# 7. Lưu vào config.json
			if new_token or new_cookie:
				config = SettingsManager.load_config()
				if "account1" not in config:
					config["account1"] = {}
				if new_token:
					config["account1"]["access_token"] = new_token
				if new_cookie:
					config["account1"]["cookie"] = new_cookie
				SettingsManager.save_config(config)

			# 8. Re-sync cookies cho pool instances
			if self.num_chrome > 1:
				try:
					cookies = await self._export_cookies_from_collector(0)
					if cookies:
						for i in range(1, len(self._collectors)):
							await self._inject_cookies_to_collector(i, cookies)
						self._log(f"🍪 Re-sync cookies cho {self.num_chrome - 1} pool instances")
				except Exception:
					pass

			if new_token:
				self._log("✅ Force refresh thành công! Token mới sẵn sàng.")
			else:
				self._log("⚠️ Force refresh thất bại — không lấy được token mới")
			return new_token, new_cookie
		except Exception as e:
			self._log(f"⚠️ Lỗi force_auto_login: {e}")
			return new_token, new_cookie

	def _get_project_id(self):
		"""Helper: lấy project_id từ config."""
		try:
			config = SettingsManager.load_config()
			return config.get("account1", {}).get("projectId", "")
		except Exception:
			return ""

	async def reload_all_chrome(self, skip_zero=False):
		self._log("🔄 Reload Chrome instances...")
		
		async def _reload_one(i, collector):
			if skip_zero and i == 0:
				return
			try:
				await collector.restart_browser()
				self._log(f"✅ Chrome-{i} đã reload")
			except Exception as e:
				self._log(f"⚠️ Chrome-{i} reload lỗi: {e}")
			
		tasks = []
		for i, collector in enumerate(self._collectors):
			tasks.append(asyncio.create_task(_reload_one(i, collector)))
			
		if tasks:
			await asyncio.gather(*tasks)

		# Re-inject cookies từ Chrome-0 sau reload
		if len(self._collectors) > 1:
			cookies = await self._export_cookies_from_collector(0)
			if cookies:
				for i in range(1, len(self._collectors)):
					await self._inject_cookies_to_collector(i, cookies)
		
		self._log("✅ Đã reload tất cả Chrome")

	@property
	def pool_size(self):
		return self._token_queue.qsize()

	@property
	def total_tokens_generated(self):
		return self._total_tokens

	@property
	def page(self):
		for c in self._collectors:
			if c.page and not c.page.is_closed():
				return c.page
		return None

	async def get_browser_cookie_string(self):
		"""Extract cookie header string từ Chrome-0 browser (chỉ Google domain)."""
		if not self._collectors:
			return ""
		collector = self._collectors[0]
		if not collector or not getattr(collector, 'context', None):
			return ""
		try:
			cookies = await collector.context.cookies()
			if not cookies:
				return ""
			# ✅ Chỉ lấy cookies cho Google domains
			google_domains = ('.google.com', '.google.co', 'labs.google', '.googleapis.com', '.gstatic.com')
			filtered = [
				c for c in cookies
				if c.get('name') and c.get('value')
				and any(d in str(c.get('domain', '')) for d in google_domains)
			]
			if not filtered:
				# Fallback: lấy tất cả nếu không có Google cookies
				filtered = [c for c in cookies if c.get('name') and c.get('value')]
			return "; ".join(f"{c['name']}={c['value']}" for c in filtered)
		except Exception:
			return ""

	async def refresh_auth_from_browser(self, project_id=""):
		"""Lấy access_token MỚI từ Google Labs session API.
		
		QUAN TRỌNG: RELOAD trang trước để Google cấp OAuth token MỚI,
		rồi mới gọi /fx/api/auth/session qua Chrome browser (NextAuth endpoint)
		"""
		async with self._auth_refresh_lock:
			# Kiểm tra xem có token mới được refresh gần đây (e.g., 30s) không
			if time.time() - getattr(self, '_last_auth_refresh', 0) < 30:
				self._log("ℹ️ Bỏ qua refresh trang vì token vừa được cấp mới bởi tiến trình khác")
				config = SettingsManager.load_config()
				cached_token = config.get("account1", {}).get("access_token")
				cached_cookie = config.get("account1", {}).get("cookie")
				if cached_token:
					return cached_token, cached_cookie

			access_token = None
			cookie_str = ""

			try:
				# 1. Lấy cookies từ Chrome browser
				cookie_str = await self.get_browser_cookie_string()
				if not cookie_str:
					self._log("⚠️ Không lấy được cookies từ Chrome browser")
					return None, ""

				# 2. RELOAD trang để force Google cấp OAuth token MỚI
				if self._collectors and len(self._collectors) > 0:
					collector0 = self._collectors[0]
					if collector0 and getattr(collector0, 'page', None) and not collector0.page.is_closed():
						try:
							self._log("🔄 Reload trang để lấy OAuth token mới...")
							await collector0.page.reload(wait_until="domcontentloaded", timeout=15000)
							try:
								await collector0.page.wait_for_load_state("networkidle", timeout=10000)
							except Exception:
								pass
							await asyncio.sleep(2)  # Chờ session cập nhật
						except Exception as e:
							self._log(f"⚠️ Reload trang lỗi: {e}")

				# 3. ƯU TIÊN: Gọi NextAuth session API qua Chrome để lấy token mới
				if self._collectors and len(self._collectors) > 0:
					collector0 = self._collectors[0]
					if collector0 and getattr(collector0, 'page', None) and not collector0.page.is_closed():
						try:
							eval_coro = collector0.page.evaluate("""() => {
								return fetch('/fx/api/auth/session', {
									credentials: 'include',
									headers: { 'Accept': 'application/json' }
								})
								.then(r => r.json())
								.catch(e => null);
							}""")
							session_data = await asyncio.wait_for(eval_coro, timeout=30)
							
							if session_data and isinstance(session_data, dict):
								token = (session_data.get("accessToken") 
										 or session_data.get("access_token")
										 or "")
								if token and len(token) > 20:
									access_token = token
									self._log("✅ Đã lấy access_token MỚI từ NextAuth session API!")
								else:
									self._log(f"Session API response keys: {list(session_data.keys())}")
						except asyncio.TimeoutError:
							self._log("⚠️ Timeout khi gọi NextAuth session API")
						except Exception as e:
							self._log(f"⚠️ Lỗi gọi session API: {e}")

				# 3. Fallback: Đọc từ DOM (có thể cũ)
				if not access_token and self._collectors and len(self._collectors) > 0:
					collector0 = self._collectors[0]
					if collector0 and getattr(collector0, 'page', None) and not collector0.page.is_closed():
						try:
							js_token = await collector0.page.evaluate("""() => {
								try {
									const html = document.documentElement.innerHTML;
									const m = html.match(/"access_token":"([^"]+)"/);
									return m ? m[1] : null;
								} catch(e) { return null; }
							}""")
							if js_token and len(js_token) > 20:
								access_token = js_token
								self._log("ℹ️ Fallback: lấy token từ Chrome DOM (có thể cũ)")
						except Exception:
							pass

				# 4. Fallback cuối: HTTP fetch
				if not access_token and cookie_str and project_id:
					import auth_helper
					auth_helper.invalidate_cache()
					access_token = auth_helper.get_valid_access_token(
						cookie_str, project_id, force_refresh=True
					)
					if access_token:
						self._log("✅ Fallback: lấy token từ HTTP (dùng browser cookies)")

				# 5. Lưu vào config.json
				if access_token or cookie_str:
					config = SettingsManager.load_config()
					if "account1" not in config:
						config["account1"] = {}
					if access_token:
						config["account1"]["access_token"] = access_token
						self._last_auth_refresh = time.time()
					if cookie_str:
						config["account1"]["cookie"] = cookie_str
					SettingsManager.save_config(config)

				return access_token, cookie_str
			except Exception as e:
				self._log(f"⚠️ refresh_auth_from_browser error: {e}")
				return access_token, cookie_str

	async def stop(self):
		"""Dừng harvest tasks nhưng GIỮ Chrome mở để tái sử dụng."""
		self._stop_flag = True
		for task in self._harvest_tasks:
			try:
				task.cancel()
			except Exception:
				pass

		# Xóa token queue cũ
		while not self._token_queue.empty():
			try:
				self._token_queue.get_nowait()
			except Exception:
				break

		self._harvest_tasks.clear()
		# KHÔNG clear collectors, KHÔNG đóng Chrome → giữ để tái sử dụng
		self._started = False
		self._log("🛑 TokenPool: đã dừng harvest (Chrome vẫn mở)")

	async def close_after_workflow(self):
		"""Cleanup nhẹ sau workflow - KHÔNG đóng Chrome."""
		await self.stop()

	async def force_close(self):
		"""Đóng hoàn toàn Chrome - chỉ dùng khi cần reload thủ công."""
		self._stop_flag = True
		for task in self._harvest_tasks:
			try:
				task.cancel()
			except Exception:
				pass

		for i, collector in enumerate(self._collectors):
			try:
				await collector.close_after_workflow()
			except Exception:
				pass

		self._harvest_tasks.clear()
		self._collectors.clear()
		self._started = False
		self._log("🛑 TokenPool: đã đóng hoàn toàn Chrome instances")

	async def restart_browser(self):
		await self.reload_all_chrome()

	async def __aenter__(self):
		await self.start()
		return self

	async def __aexit__(self, exc_type, exc, tb):
		pass
