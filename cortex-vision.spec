# PyInstaller spec for the cortex-vision sidecar.
#
# Build:   pyinstaller cortex-vision.spec --noconfirm --clean
# Output:  dist/cortex-vision/cortex-vision.exe  (--onedir for fast startup)
# Zip:     7z a cortex-vision-0.1.0-windows-cpu.zip dist/cortex-vision/
#
# v0.1 ships CPU-only — there's no torch/transformers/parakeet usage in the
# actual code (cortex-vision relies on LM Studio for vision and Whisper-style
# servers for audio, both via HTTP). If we ever add local model loading, add
# a separate gpu spec then.
#
# Bundle size target: ~250-400 MB. Most of that is opencv-python's DLLs.

from pathlib import Path

from PyInstaller.utils.hooks import (
    collect_all,
    collect_data_files,
    collect_submodules,
)

block_cipher = None


# ---------------------------------------------------------------------------
# yt-dlp ships ~1700 site extractors loaded via importlib at runtime.
# PyInstaller's static analysis finds approximately none of them, which is
# why "every URL fails with 'No suitable extractor found'" is the canonical
# yt-dlp + PyInstaller bug. Pull them all in explicitly.
# ---------------------------------------------------------------------------
ytdlp_submodules = collect_submodules("yt_dlp.extractor")
ytdlp_postprocessor = collect_submodules("yt_dlp.postprocessor")


# ---------------------------------------------------------------------------
# cv2 (opencv-python): native DLLs + codec data files. collect_all handles
# binaries, datas, and any submodules in one call.
# ---------------------------------------------------------------------------
cv2_datas, cv2_binaries, cv2_hidden = collect_all("cv2")


# ---------------------------------------------------------------------------
# scenedetect: data files (default detector configs) + cv2 transitive.
# ---------------------------------------------------------------------------
scenedetect_datas, scenedetect_binaries, scenedetect_hidden = collect_all("scenedetect")


# ---------------------------------------------------------------------------
# cortex_vision itself: server.py imports many submodules lazily inside the
# lifespan handler and inside endpoints (live, audio, capture, description).
# Static analysis catches most but it's cheap insurance to grab all of them.
# ---------------------------------------------------------------------------
cortex_vision_submodules = collect_submodules("cortex_vision")


# ---------------------------------------------------------------------------
# pydantic v2: some discriminated-union machinery loads via importlib.
# pydantic_core is already a hard dep so it gets picked up automatically.
# ---------------------------------------------------------------------------
pydantic_hidden = collect_submodules("pydantic")


# ---------------------------------------------------------------------------
# uvicorn[standard]: every protocol implementation is loaded via getattr at
# runtime (uvicorn.loops.auto -> uvloop OR asyncio; same for http/websockets).
# Without these, the bundled .exe fails on startup with
# "ModuleNotFoundError: No module named 'uvicorn.protocols.http.h11_impl'".
# ---------------------------------------------------------------------------
uvicorn_hidden = [
    "uvicorn.logging",
    "uvicorn.loops",
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    # uvloop is unix-only; keep it in case we ever target Linux/macOS bundles
    "uvicorn.loops.uvloop",
    "uvicorn.protocols",
    "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.http.httptools_impl",
    "uvicorn.protocols.websockets",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.protocols.websockets.websockets_impl",
    "uvicorn.protocols.websockets.wsproto_impl",
    "uvicorn.lifespan",
    "uvicorn.lifespan.on",
    "uvicorn.lifespan.off",
]


# ---------------------------------------------------------------------------
# httpx + httpcore: usually clean but the async backend is selected via
# importlib at first use. Belt-and-suspenders.
# ---------------------------------------------------------------------------
httpx_hidden = [
    "httpx",
    "httpcore",
    "httpcore._async.connection",
    "httpcore._async.connection_pool",
    "httpcore._async.http11",
    "httpcore._sync.connection",
    "httpcore._sync.connection_pool",
    "httpcore._sync.http11",
    "h11",
]


a = Analysis(
    # __main__.py is the canonical entry point — it has the `serve` subcommand
    # dispatcher that cortex-desktop's plugin manager invokes via
    # `cortex-vision.exe serve --port N`. Pointing at server.py directly would
    # bypass the subcommand layer and fail on "unrecognized arguments: serve".
    ["cortex_vision/__main__.py"],
    pathex=["."],
    binaries=[
        *cv2_binaries,
        *scenedetect_binaries,
    ],
    datas=[
        # plugin.json sits next to the .exe so the plugin manager can read it
        # post-install if it ever needs to (currently the manifest endpoint
        # serves the same data, but bundling it is cheap insurance).
        ("plugin.json", "."),
        *cv2_datas,
        *scenedetect_datas,
    ],
    hiddenimports=[
        *uvicorn_hidden,
        *httpx_hidden,
        *cortex_vision_submodules,
        *cv2_hidden,
        *scenedetect_hidden,
        *pydantic_hidden,
        *ytdlp_submodules,
        *ytdlp_postprocessor,
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # We don't use these in v0.1 — explicit excludes shrink the bundle and
        # prevent transitive deps from accidentally pulling them in.
        "torch",
        "torchvision",
        "torchaudio",
        "transformers",
        "ultralytics",
        "nemo",
        "tensorflow",
        "matplotlib",
        "pandas",
        "scipy",
        "sklearn",
        "jupyter",
        "IPython",
        "notebook",
        "pytest",
        "_pytest",
        "tkinter",
        "PIL.ImageTk",                         # tkinter dep
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="cortex-vision",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                                  # UPX corrupts cv2 DLLs
    console=True,                               # cortex-desktop captures stdout for logs
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    # icon="assets/cortex-vision.ico",          # add when we have an icon
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="cortex-vision",
)
