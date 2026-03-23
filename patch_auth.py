import os

target_files = [
    "API_sync_chactacter.py",
    "API_text_to_video.py"
]

target = """        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {access_token}",
        }"""

replacement = """        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {access_token}",
            "Origin": "https://labs.google",
            "Referer": "https://labs.google/",
            "X-Goog-AuthUser": "0",
        }"""

for target_file in target_files:
    if os.path.exists(target_file):
        with open(target_file, "r", encoding="utf-8") as f:
            data = f.read()
        if target in data:
            data = data.replace(target, replacement)
            with open(target_file, "w", encoding="utf-8") as f:
                f.write(data)
            print(f"Patched {target_file}")
            
# Also fix A_workflow_sync_chactacter.py reloading auth
target_workflow = "A_workflow_sync_chactacter.py"
wf_target = """                # ── Build payload & gửi request ──
                video_aspect_ratio = self._resolve_video_aspect_ratio()"""

wf_rep = """                # ✅ LUÔN reload access_token mới nhất từ config trước mỗi request
                try:
                    _fresh = self._load_auth_config()
                    if _fresh and _fresh.get("access_token"):
                        token = _fresh["access_token"]
                        auth["access_token"] = token
                    if _fresh and _fresh.get("cookie"):
                        auth["cookie"] = _fresh["cookie"]
                    if _fresh and _fresh.get("sessionId"):
                        auth["sessionId"] = _fresh["sessionId"]
                except Exception:
                    pass

                # ── Build payload & gửi request ──
                video_aspect_ratio = self._resolve_video_aspect_ratio()"""

if os.path.exists(target_workflow):
    with open(target_workflow, "r", encoding="utf-8") as f:
        data = f.read()
    if wf_target in data:
        data = data.replace(wf_target, wf_rep)
        with open(target_workflow, "w", encoding="utf-8") as f:
            f.write(data)
        print(f"Patched {target_workflow}")
