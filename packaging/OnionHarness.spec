"""PyInstaller spec for Onion Harness (onedir).

Deliberately onedir, not onefile: onefile unpacks the whole runtime to a temp
directory on every launch, which makes startup slow and breaks the WebView2
user-data folder. onedir starts instantly and keeps a stable install layout.

The React source tree is not bundled — only the built `static/` output is, so the
shipped app carries no node_modules or TS sources.
"""

from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

ROOT = Path(SPECPATH).resolve().parent
PACKAGE = ROOT / "onion_harness"

# Everything the app reads at runtime.
datas = [
    (str(PACKAGE / "app" / "static"), "onion_harness/app/static"),
    (str(PACKAGE / "app" / "assets"), "onion_harness/app/assets"),
]

# The agent engine is imported by name at runtime, so its submodules must be
# collected explicitly or the frozen build cannot resolve them.
# ``loong_agent.skills`` and ``loong_agent.mcp`` import a bare ``loong`` package
# that the distribution does not ship. Collecting them aborts the build, and the
# app never uses those code paths (it injects its own tools), so skip them.
_ENGINE_SKIP = ("skills", "mcp")

def _engine_submodules() -> list:
    out = []
    for name in collect_submodules("loong_agent"):
        tail = name.split(".")[-1]
        if any(part in ("skills", "mcp") for part in name.split(".")):
            continue
        out.append(name)
    del tail
    return out


hiddenimports = _engine_submodules()

hiddenimports += [
    "webview.platforms.winforms",
    "webview.platforms.edgechromium",
    "clr_loader",
    "pythonnet",
    "httpx",
    "httpcore",
    "anyio",
    "h11",
    "certifi",
    "idna",
    "truststore",
    # Secret storage and the encrypted memory database. ``win32ctypes`` is not
    # imported directly; it ships inside pywin32 and pyinstaller's win32 hook
    # expects to find it, so naming it avoids an analysis warning.
    "win32cred",
    "win32api",
    "win32ctypes",
    "cryptography",
    "cryptography.hazmat.primitives.ciphers.aead",
    "cryptography.hazmat.primitives.kdf.pbkdf2",
]

# Modules that must NOT be pulled in: the React tree and test-only packages.
excludes = [
    "tkinter",
    "matplotlib",
    "numpy",
    "pandas",
    "pytest",
    "IPython",
    "notebook",
    "PIL",
]

a = Analysis(
    [str(PACKAGE / "app" / "main.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="OnionHarness",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    icon=str(PACKAGE / "app" / "assets" / "onion.ico"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="OnionHarness",
)