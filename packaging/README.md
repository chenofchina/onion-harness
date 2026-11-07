# Packaging

Builds a Windows onedir bundle (not onefile — onefile re-extracts the runtime on
every launch and breaks the WebView2 user-data folder).

```bash
# 1. build the GUI first; the spec bundles static/, not the React sources
cd frontend && npm run build && cd ..

# 2. regenerate the icon if the mark changed (needs Pillow, dev only)
python scripts/make_icon.py

# 3. build
pip install pyinstaller
python -m PyInstaller packaging/OnionHarness.spec --noconfirm
```

Output: `dist/OnionHarness/OnionHarness.exe` plus `_internal/`. Ship the whole
folder.

The app stores its data in `%USERPROFILE%\.onionharness`, not next to the exe, so
upgrading means replacing the folder.
