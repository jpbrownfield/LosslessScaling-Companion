"""
Root launcher for LS Companion.
Run: python run_companion.py [--no-elevate]
"""

import sys
import os


def _ensure_admin():
    # Packaged exe already ships requireAdministrator in its manifest;
    # only source runs need an explicit UAC relaunch. Set
    # LS_COMPANION_NO_ELEVATE=1 or pass --no-elevate to stay non-elevated
    # (CI, tests, read-only dashboard checks).
    if os.name != "nt" or getattr(sys, "frozen", False):
        return
    if "--no-elevate" in sys.argv:
        sys.argv.remove("--no-elevate")
        return
    if os.environ.get("LS_COMPANION_NO_ELEVATE") == "1":
        return
    try:
        import ctypes

        if ctypes.windll.shell32.IsUserAnAdmin():
            return
    except Exception:
        return
    script = os.path.abspath(__file__)
    params = " ".join([f'"{script}"'] + [f'"{a}"' for a in sys.argv[1:]])
    try:
        import ctypes

        rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, params, os.path.dirname(script), 1)
        # ShellExecute returns >32 on success; parent exits and the
        # elevated child continues. On cancel/failure fall through
        # non-elevated so local runs never hard-exit from UAC.
        if rc and int(rc) > 32:
            sys.exit(0)
    except Exception as error:
        print(f"UAC elevation declined ({error}); continuing non-elevated.", file=sys.stderr)


_ensure_admin()

# Add workspace directory to sys.path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from companion.services.test_binaries import sync as _sync_test_binaries

    _sync_test_binaries()
except Exception as _sync_error:
    print(f"Test binary sync skipped ({_sync_error}); using build/native.", file=sys.stderr)

from companion.main import main

if __name__ == "__main__":
    main()
