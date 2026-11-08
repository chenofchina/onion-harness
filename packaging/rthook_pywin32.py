"""Runtime hook: make pywin32 modules importable in the frozen build.

pywin32 ships its extension modules inside a ``win32`` directory and relies on a
``.pth`` file (``pywin32.pth``) to put that directory on ``sys.path``, which is what
makes ``import win32cred`` work in a normal install. A frozen build has no ``.pth``
processing at all, so every ``import win32cred`` fails.

The symptom is not an import error the user sees. ``WindowsCredentialStore`` treats
an unavailable ``win32cred`` as "no credential store", the key vault falls back to an
in-memory store, and the next launch has forgotten every API key - which reads as
"the key does not persist" rather than "the bundling is wrong".

This hook runs before the app is imported, so the path is in place by the time
:mod:`onion_harness.api.secrets` is loaded.
"""

import os
import sys


def _bootstrap_pywin32() -> None:
    root = getattr(sys, "_MEIPASS", None)
    if not root:
        return
    internal = os.path.join(root, "_internal")
    # PyInstaller 6 lays the payload under _internal; older layouts put it at root.
    base = internal if os.path.isdir(internal) else root

    # `win32/lib` is where win32timezone lives; win32cred imports it internally and
    # fails without it even though the module itself is present.
    for candidate in (
        os.path.join(base, "win32"),
        os.path.join(base, "win32", "lib"),
        os.path.join(base, "win32", "lib", "win32timezone"),
        os.path.join(base, "pywin32_system32"),
        base,
    ):
        if os.path.isdir(candidate) and candidate not in sys.path:
            sys.path.insert(0, candidate)

    # The DLLs live in pywin32_system32; the .pyd files need them on the search path.
    dll_dir = os.path.join(base, "pywin32_system32")
    if os.path.isdir(dll_dir):
        try:
            os.add_dll_directory(dll_dir)
        except (AttributeError, OSError):
            # add_dll_directory is 3.8+; a missing call only means the fallback
            # PATH search is used, which PyInstaller already arranged.
            pass


_bootstrap_pywin32()