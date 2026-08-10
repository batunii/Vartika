"""Build the double-clickable executable.

    pip install pyinstaller
    python build.py

Produces dist/DissertationRewrite.exe (or a matching binary on macOS/Linux):
one file, no Python needed on the target machine. The Claude Code CLI is not
bundled and cannot be — it is a separate tool with its own login, and the
launcher checks for it at startup.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
NAME = "Vartika"

# No style guide is bundled. Writing rules are one author's voice, and baking a
# particular set into a shared binary would impose them on everyone who runs it.
# Users point at their own file, or run without one.


def main() -> int:
    if shutil.which("pyinstaller") is None:
        try:
            import PyInstaller  # noqa: F401
        except ImportError:
            print("PyInstaller is not installed. Run:  pip install pyinstaller")
            return 1

    separator = ";" if sys.platform == "win32" else ":"
    command = [
        sys.executable, "-m", "PyInstaller",
        "--onefile",
        "--name", NAME,
        # A console window is the app's status display and its stop button.
        "--console",
        "--add-data", f"{HERE / 'index.html'}{separator}.",
        # Uvicorn resolves these at runtime, so PyInstaller cannot see them.
        "--hidden-import", "uvicorn.logging",
        "--hidden-import", "uvicorn.loops.auto",
        "--hidden-import", "uvicorn.loops.asyncio",
        "--hidden-import", "uvicorn.protocols.http.auto",
        "--hidden-import", "uvicorn.protocols.http.h11_impl",
        "--hidden-import", "uvicorn.protocols.websockets.auto",
        "--hidden-import", "uvicorn.lifespan.on",
        "--collect-submodules", "uvicorn",
        "--noconfirm",
        "--clean",
        str(HERE / "launcher.py"),
    ]

    print("Building. This takes a minute.\n")
    result = subprocess.run(command, cwd=str(HERE))
    if result.returncode != 0:
        return result.returncode

    built = next(iter((HERE / "dist").glob(f"{NAME}*")), None)
    print()
    if built:
        size = built.stat().st_size / (1024 * 1024)
        print(f"Built {built}  ({size:.0f} MB)")
        print("Double-click it, or put it beside your manuscript folder first.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
