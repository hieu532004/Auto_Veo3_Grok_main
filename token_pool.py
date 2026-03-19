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

		self._collectors = []
		self._token_queue = asyncio.Queue(maxsize=16)
		self._harvest_tasks = []
		self._started = False
		self._stop_flag = False
		self._base_port = 9222
		self._lock = asyncio.Lock()
		self._total_tokens = 0
		self._token_request_counts = {}

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
		"""Inject cookies vào Chrome pool instance qua CDP, rồi navigate lại."""
		if collector_idx >= len(self._collectors) or not cookies:
			return False
		collector = self._collectors[collector_idx]
		if not collector.context or not collector.page:
			return False
		try:
			await collector.context.add_cookies(cookies)
			# Navigate tới project URL để apply cookies
			try:
				await collector.page.goto(
					self.project_url,
					wait_until="domcontentloaded",
					timeout=20000,
				)
			except Exception:
				pass
			try:
				await collector.page.wait_for_load_state("networkidle", timeout=10000)
			except Exception:
				pass
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
				hide_window=False,
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
						self._log(f"🍪 Chrome-{i}: inject {len(cookies)} cookies + navigate OK")
					else:
						self._log(f"⚠️ Chrome-{i}: inject cookies thất bại")
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

	# ───────────────────────────────────────────────────────────────────
	#  HARVEST LOOP
	# ───────────────────────────────────────────────────────────────────

	async def _harvest_loop(self, idx, collector):
		fail_streak = 0
		self._token_request_counts[idx] = 0

		while not self._should_stop():
			try:
				# Token hợp lệ 120s, không cần queue quá nhiều
				queue_limit = 6
				if self._token_queue.qsize() >= queue_limit:
					await asyncio.sleep(2)
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
					await self._token_queue.put((token, time.time()))
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
							# Sau khi restart, re-inject cookies từ Chrome-0
							if idx > 0:
								await self._resync_cookies_to_instance(idx)
						except Exception:
							pass
						fail_streak = 0
					await asyncio.sleep(3)

				wait_time = max(1, self.clear_data_wait)
				for _ in range(int(wait_time * 2)):
					if self._should_stop():
						return
					await asyncio.sleep(0.5)

			except asyncio.TimeoutError:
				fail_streak += 1
				self._log(f"[Chrome-{idx}] ⏱️ Timeout lấy token")
				if fail_streak >= 2:
					if getattr(collector, "_login_required", False):
						self._log(f"[Chrome-{idx}] ⚠️ Phát hiện màn hình Sign In (cần login), nhưng app không có macro Auto-login. Hãy tự trỏ vào tab để đăng nhập! Chrome sẽ tự động reload...")
					try:
						await collector.restart_browser()
						if idx > 0:
							await self._resync_cookies_to_instance(idx)
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

	# ───────────────────────────────────────────────────────────────────
	#  GET TOKEN / CONTROL
	# ───────────────────────────────────────────────────────────────────

	async def get_token(self, timeout=120, **kwargs):
		if not self._started:
			await self.start()

		start_wait = time.time()
		while time.time() - start_wait < timeout:
			if self._should_stop():
				return None
			try:
				token_data = await asyncio.wait_for(self._token_queue.get(), timeout=2.0)
				if isinstance(token_data, tuple) and len(token_data) == 2:
					token, ts = token_data
					token_age = time.time() - ts
					if token_age < 120:
						return token
					else:
						self._log(f"♻️ Loại bỏ token quá hạn ({int(time.time() - ts)}s)")
						continue
				else:
					return token_data
			except asyncio.TimeoutError:
				pass
			except Exception as e:
				self._log(f"Lỗi lấy token từ queue: {e}")
				await asyncio.sleep(1)

		self._log(f"⏱️ TokenPool: timeout {timeout}s chờ token")
		return None

	async def force_auto_login(self):
		"""Được gọi khi 401 fail liên tục trong status poll.
		
		Cơ chế: 
		- Restart Chrome-1 (KHÔNG phải Chrome-0 đang harvest)
		- Chrome-1 load lại trang Google Labs → Google render access_token MỚI vào HTML
		- Bóc access_token từ page mới
		- Lưu vào config để status poll dùng
		"""
		self._log("🚨 Token hết hạn. Restart Chrome-1 để lấy access_token MỚI từ Google...")
		new_token = None
		new_cookie = ""
		try:
			# 1. Drain stale recaptcha tokens
			drained = 0
			while not self._token_queue.empty():
				try:
					self._token_queue.get_nowait()
					drained += 1
				except Exception:
					break
			if drained:
				self._log(f"🗑️ Đã loại bỏ {drained} recaptcha token cũ")
			
			# 2. Chọn Chrome instance để restart (ưu tiên Chrome-1, tránh Chrome-0)
			restart_idx = min(1, len(self._collectors) - 1)
			if restart_idx < 0 or restart_idx >= len(self._collectors):
				self._log("⚠️ Không có Chrome instance để restart")
				return None, ""
			
			collector = self._collectors[restart_idx]
			self._log(f"🔄 Restart Chrome-{restart_idx} để lấy token mới...")
			
			# 3. Restart Chrome instance → page load mới từ Google server
			try:
				await collector.restart_browser()
				self._log(f"✅ Chrome-{restart_idx} đã restart xong")
			except Exception as e:
				self._log(f"⚠️ Lỗi restart Chrome-{restart_idx}: {e}")
				return None, ""
			
			# 4. Đợi page load hoàn tất
			await asyncio.sleep(5)
			
			# 5. Bóc access_token từ page MỚI của Chrome-{restart_idx}
			page = getattr(collector, 'page', None) 
			if page and not page.is_closed():
				try:
					# Đợi page content load
					await page.wait_for_load_state("domcontentloaded", timeout=15000)
					await asyncio.sleep(2)
					
					js_token = await page.evaluate("""() => {
						try {
							const html = document.documentElement.innerHTML;
							const m = html.match(/"access_token":"([^"]+)"/);
							if (m) return m[1];
							const scripts = document.querySelectorAll('script');
							for (const s of scripts) {
								const t = s.textContent;
								if (t && t.includes('access_token')) {
									const m2 = t.match(/"access_token":"([^"]+)"/);
									if (m2) return m2[1];
								}
							}
							return null;
						} catch(e) { return null; }
					}""")
					
					if js_token and len(js_token) > 20:
						new_token = js_token
						self._log(f"✅ Đã bóc access_token MỚI từ Chrome-{restart_idx} (sau restart)!")
					else:
						self._log(f"⚠️ Không tìm thấy access_token trong Chrome-{restart_idx} page sau restart")
				except Exception as e:
					self._log(f"⚠️ Lỗi đọc token từ Chrome-{restart_idx}: {e}")
			
			# 6. Lấy cookies mới từ Chrome-0 (Chrome-0 vẫn có session hợp lệ)
			new_cookie = await self.get_browser_cookie_string()
			
			# 7. Nếu không lấy được token từ Chrome-1, thử Chrome-0 DOM
			if not new_token:
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
							new_token = js_token
							self._log("ℹ️ Fallback: lấy token từ Chrome-0 DOM (có thể cũ)")
					except Exception:
						pass
			
			# 8. Fallback cuối: HTTP fetch
			if not new_token and new_cookie:
				from auth_helper import invalidate_cache
				invalidate_cache()
				import auth_helper
				new_token = auth_helper.get_valid_access_token(
					new_cookie, self._get_project_id(), force_refresh=True
				)
				if new_token:
					self._log("✅ Fallback: lấy token từ HTTP request")
			
			# 9. Lưu vào config
			if new_token or new_cookie:
				config = SettingsManager.load_config()
				if "account1" not in config:
					config["account1"] = {}
				if new_token:
					config["account1"]["access_token"] = new_token
				if new_cookie:
					config["account1"]["cookie"] = new_cookie
				SettingsManager.save_config(config)
			
			if new_token:
				self._log("✅ Force refresh thành công! Token mới từ Chrome restart.")
			else:
				self._log("⚠️ Force refresh thất bại - hãy kiểm tra đăng nhập Google trong Chrome")
			
			return new_token, new_cookie
		except Exception as e:
			self._log(f"⚠️ Lỗi force_auto_login: {e}")
			return new_token, new_cookie
			
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
		
		Gọi /fx/api/auth/session qua Chrome browser (NextAuth endpoint)
		→ trả về JSON chứa access_token mới nhất
		"""
		access_token = None
		cookie_str = ""

		try:
			# 1. Lấy cookies từ Chrome browser
			cookie_str = await self.get_browser_cookie_string()
			if not cookie_str:
				self._log("⚠️ Không lấy được cookies từ Chrome browser")
				return None, ""

			# 2. ƯU TIÊN: Gọi NextAuth session API qua Chrome để lấy token mới
			if self._collectors and len(self._collectors) > 0:
				collector0 = self._collectors[0]
				if collector0 and getattr(collector0, 'page', None) and not collector0.page.is_closed():
					try:
						session_data = await collector0.page.evaluate("""() => {
							return fetch('/fx/api/auth/session', {
								credentials: 'include',
								headers: { 'Accept': 'application/json' }
							})
							.then(r => r.json())
							.catch(e => null);
						}""")
						
						if session_data and isinstance(session_data, dict):
							# NextAuth session trả về accessToken hoặc access_token
							token = (session_data.get("accessToken") 
							         or session_data.get("access_token")
							         or "")
							if token and len(token) > 20:
								access_token = token
								self._log("✅ Đã lấy access_token MỚI từ NextAuth session API!")
							else:
								self._log(f"Session API response keys: {list(session_data.keys())}")
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
