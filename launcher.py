"""Double-clickable entry point for Vartika.

Checks the reviewer is reachable, starts the local server on a free port, and
opens a browser at it. Choosing which manuscript to work on happens in the app
itself, so there is one interface rather than a native dialog followed by a web
page. The console window this runs in is the stop button.

Run from source with `python launcher.py`, or build a single executable with
`python build.py`.
"""

from __future__ import annotations

import os
import secrets
import shutil
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

import app

BANNER = r"""
  Vartika
  -------
"""


def log(message: str = "") -> None:
    print(message, flush=True)


def fail(message: str, detail: str = "") -> None:
    """Report a problem and wait, so a double-clicked window does not vanish."""
    log()
    log(f"  Cannot start: {message}")
    if detail:
        for line in detail.splitlines():
            log(f"    {line}")
    log()
    try:
        input("  Press Enter to close. ")
    except EOFError:
        time.sleep(20)
    sys.exit(1)


def here() -> Path:
    """The directory the executable or script actually sits in.

    Distinct from app.bundle_dir(), which under PyInstaller points at the
    temporary unpack directory rather than anywhere the user can see.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def looks_like_manuscript(folder: Path) -> bool:
    """Whether this folder *is* a manuscript, not merely contains one.

    The main document has to sit directly in it. Without that bound every
    parent directory up the tree qualifies, and the search cannot tell the
    manuscript folder from the drive it lives on.
    """
    return folder.is_dir() and bool(app.find_root_candidates(folder, max_depth=1))


def config_path() -> Path:
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return Path(base) / ".vartika" / "last-manuscript.txt"


def read_remembered() -> Path | None:
    path = config_path()
    if not path.exists():
        return None
    try:
        return Path(path.read_text(encoding="utf-8").strip()).resolve()
    except (OSError, ValueError):
        return None


def remember(manuscript: Path) -> None:
    try:
        path = config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(manuscript), encoding="utf-8")
    except OSError:
        pass          # remembering is a convenience, never a reason to fail


def obvious_manuscript(force_pick: bool) -> Path | None:
    """A manuscript clear enough to open without asking.

    Only two cases qualify: one told to us explicitly, and a single manuscript
    sitting beside the executable. Anything less certain is left to the app's
    setup page, which can show the alternatives properly.
    """
    override = os.environ.get("REWRITE_MANUSCRIPT")
    if override and looks_like_manuscript(Path(override)):
        return Path(override).resolve()
    if force_pick:
        return None

    start = here()
    local = [start] if looks_like_manuscript(start) else []
    try:
        local += [p for p in sorted(start.iterdir())
                  if p.is_dir() and not p.name.startswith((".", "__"))
                  and looks_like_manuscript(p)]
    except OSError:
        pass
    if len(local) == 1:
        return local[0].resolve()

    remembered = read_remembered()
    if not local and remembered and looks_like_manuscript(remembered):
        return remembered
    return None


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def preflight() -> None:
    if shutil.which("claude") or shutil.which("claude.cmd") or shutil.which("claude.exe"):
        return
    fail(
        "the Claude Code CLI was not found.",
        "Vartika runs a headless Claude Code session to review your rewrites,\n"
        "so `claude` must be installed and logged in on this machine.\n\n"
        "Install it from https://claude.com/claude-code, run `claude` once to\n"
        "log in, then start Vartika again.",
    )


def main() -> None:
    log(BANNER)
    preflight()

    force_pick = "--pick" in sys.argv or bool(os.environ.get("REWRITE_PICK"))
    app.ACCESS_TOKEN = secrets.token_urlsafe(24)
    os.environ.setdefault("REWRITE_START", str(here()))

    manuscript = obvious_manuscript(force_pick)
    if manuscript is not None:
        repo = app.git_root_for(manuscript)
        app.configure(manuscript=manuscript, repo_root=repo,
                      data_dir=manuscript.parent / ".rewrite-progress")
        remember(manuscript)

        committing = (repo / ".git").exists()
        if not committing:
            state = app.load_state()
            state["settings"]["auto_commit"] = False
            app.save_state(state)

        paragraphs = sum(len(app.read_blocks(rel)[1])
                         for rel, _ in app.file_order() if (manuscript / rel).exists())
        log(f"  Manuscript : {manuscript}")
        log(f"  Main file  : {app.ROOT_TEX.name if app.ROOT_TEX else '?'}")
        log(f"  Chapters   : {len(app.file_order())}   Paragraphs: {paragraphs}")
        log(f"  Committing : {'one commit per paragraph' if committing else 'no (not a git repository)'}")
    else:
        log("  No single obvious manuscript here.")
        log("  Choose one on the page that opens.")

    port = int(os.environ.get("REWRITE_PORT") or free_port())
    url = f"http://127.0.0.1:{port}/?token={app.ACCESS_TOKEN}"

    def open_browser() -> None:
        time.sleep(1.2)
        try:
            webbrowser.open(url)
        except Exception:
            pass

    threading.Thread(target=open_browser, daemon=True).start()

    log()
    log(f"  Open in your browser:  {url}")
    log("  Close this window to stop.")
    log()

    import uvicorn

    try:
        uvicorn.run(app.app, host="127.0.0.1", port=port, log_level="warning")
    except KeyboardInterrupt:
        pass
    except OSError as exc:
        fail(f"the server could not start on port {port}.", str(exc))
    log("  Stopped.")


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:                            # never vanish without a message
        import traceback
        fail("something went wrong.", traceback.format_exc())
