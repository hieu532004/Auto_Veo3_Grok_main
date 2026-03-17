import os
import sys
import glob
import subprocess
import shutil

SRC_DIR = os.path.dirname(os.path.abspath(__file__))
DIST_DIR = os.path.join(SRC_DIR, "dist")

EXCLUDE_FILES = {
    "build_exe.py", "build_exe_v2.py", "build_final.py", "build_release.py",
    "create_setup.iss", "launcher.py",
}

EXCLUDE_PREFIXES = ("test_",)

THIRD_PARTY_HIDDEN = [
    "imageio_ffmpeg",
    "imageio_ffmpeg._utils",
    "requests",
    "requests.adapters",
    "requests.auth",
    "requests.sessions",
    "urllib3",
    "charset_normalizer",
    "certifi",
    "idna",
    "PyQt6",
    "PyQt6.QtCore",
    "PyQt6.QtGui",
    "PyQt6.QtWidgets",
    "PyQt6.sip",
    "playwright",
    "playwright.async_api",
    "playwright.sync_api",
    "websockets",
    "websockets.client",
    "websockets.legacy",
    "websockets.legacy.client",
    "asyncio",
    "google",
    "google.genai",
    "google.genai.client",
    "google.genai.types",
    "google.genai.models",
    "google.genai.errors",
    "google_genai",
    "pydantic",
    "pydantic.main",
    "pydantic.types",
    "pydantic.fields",
    "httpx",
    "PIL",
    "PIL.Image",
    "numpy",
    "tkinter",
    "tkinter.messagebox",
]

DATA_DIRS = [
    "data_general",
    "Workflows",
]


def collect_hidden_imports():
    hi = [f"--hidden-import={m}" for m in THIRD_PARTY_HIDDEN]

    for f in glob.glob(os.path.join(SRC_DIR, "*.py")):
        name = os.path.splitext(os.path.basename(f))[0]
        if name == "License" or name in EXCLUDE_FILES or any(name.startswith(p) for p in EXCLUDE_PREFIXES):
            continue
        if name.startswith("build_"):
            continue
        hi.append(f"--hidden-import={name}")

    for f in glob.glob(os.path.join(SRC_DIR, "qt_ui", "*.py")):
        name = os.path.splitext(os.path.basename(f))[0]
        if name == "__init__":
            hi.append("--hidden-import=qt_ui")
        else:
            hi.append(f"--hidden-import=qt_ui.{name}")

    return hi


def collect_datas():
    datas = []
    for d in DATA_DIRS:
        src_path = os.path.join(SRC_DIR, d)
        if os.path.isdir(src_path):
            datas.append(f"--add-data={src_path};{d}")

    icon_path = os.path.join(SRC_DIR, "app_icon.ico")
    if os.path.isfile(icon_path):
        datas.append(f"--add-data={icon_path};.")

    return datas


def build():
    print("=" * 60)
    print("  BUILD AUTO VEO3 GROK HIEUMMO - FULL PACKAGE")
    print("=" * 60)

    try:
        subprocess.run(["taskkill", "/F", "/IM", "Auto_Veo3_Grok_HieuMMO.exe", "/T"],
                        capture_output=True, timeout=5)
    except Exception:
        pass

    hidden = collect_hidden_imports()
    datas = collect_datas()

    print(f"\n[1/3] Detected {len(hidden)} hidden imports")
    print(f"[1/3] Detected {len(datas)} data bundles")

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--onedir",
        "--windowed",
        "--icon=app_icon.ico",
        "--name=Auto_Veo3_Grok_HieuMMO",
        "--distpath=dist2",
        "--workpath=build2",
        f"--paths={SRC_DIR}",
    ] + hidden + datas + ["License.py"]

    print(f"\n[2/3] Running PyInstaller (onedir mode)...")
    print(f"  Command: {' '.join(cmd[:8])} ... ({len(cmd)} args total)")
    subprocess.check_call(cmd, cwd=SRC_DIR)

    app_dist = os.path.join(SRC_DIR, "dist2", "Auto_Veo3_Grok_HieuMMO")
    print(f"\n[3/3] Post-build: copying runtime data to {app_dist}")

    for d in ["data_general", "Workflows", "downloads"]:
        src = os.path.join(SRC_DIR, d)
        dst = os.path.join(app_dist, d)
        if os.path.isdir(src):
            if os.path.exists(dst):
                shutil.rmtree(dst, ignore_errors=True)
            shutil.copytree(src, dst, dirs_exist_ok=True)
            print(f"  Copied: {d}/")

    ico_src = os.path.join(SRC_DIR, "app_icon.ico")
    ico_dst = os.path.join(app_dist, "app_icon.ico")
    if os.path.isfile(ico_src) and not os.path.isfile(ico_dst):
        shutil.copy2(ico_src, ico_dst)
        print("  Copied: app_icon.ico")

    lock_file = os.path.join(app_dist, "data_general", "license_checker.lock")
    if os.path.exists(lock_file):
        try:
            os.remove(lock_file)
        except Exception:
            pass

    user_data = os.path.join(app_dist, "data_general", "user_data.txt")
    if os.path.exists(user_data):
        try:
            os.remove(user_data)
        except Exception:
            pass

    license_state = os.path.join(app_dist, "data_general", "license_state.json")
    if os.path.exists(license_state):
        try:
            os.remove(license_state)
        except Exception:
            pass

    print("\n" + "=" * 60)
    print("  BUILD SUCCESSFUL!")
    print("=" * 60)
    print(f"\n  Output folder: {app_dist}")
    print(f"  EXE file:      {os.path.join(app_dist, 'Auto_Veo3_Grok_HieuMMO.exe')}")
    print(f"\n  To distribute: zip the entire '{os.path.basename(app_dist)}' folder")
    print("=" * 60)


if __name__ == "__main__":
    build()
