"""Double-clickable entry point for the dissertation rewrite tool.

Finds the manuscript, checks the reviewer is reachable, starts the local server
on a free port and opens a browser at it. The console window it runs in is the
stop button: closing it stops the server.

Run from source with `python launcher.py`, or build a single executable with
`python build.py`.
"""

from __future__ import annotations

import os
import re
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
  --------------------
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


# --------------------------------------------------------------------------
# Finding things
# --------------------------------------------------------------------------

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
    thesis folder from the drive it lives on.
    """
    if not folder.is_dir():
        return False
    return bool(app.find_root_candidates(folder, max_depth=1))


def scan_for_manuscripts(bases: list[Path]) -> list[Path]:
    """Manuscript folders in the given directories and their children, newest first."""
    found: dict[Path, float] = {}
    for base in bases:
        folders = [base]
        try:
            folders += [p for p in base.iterdir()
                        if p.is_dir() and not p.name.startswith((".", "__"))]
        except OSError:
            pass
        for folder in folders:
            resolved = folder.resolve()
            if resolved in found or not looks_like_manuscript(resolved):
                continue
            root = app.find_root_tex(resolved)
            newest = max(
                (p.stat().st_mtime for p in resolved.rglob("*.tex")), default=0.0
            )
            found[resolved] = newest if root else 0.0
    return sorted(found, key=lambda p: found[p], reverse=True)


def local_candidates() -> list[Path]:
    """Manuscripts sitting with the executable — the strongest signal there is.

    Someone who drops the binary next to their thesis means that thesis, so a
    single local hit outranks anything remembered from a previous run.
    """
    return scan_for_manuscripts([here()])


def candidate_manuscripts() -> list[Path]:
    """Every plausible manuscript folder nearby, newest first.

    Working copies, snapshots and templates often sit side by side, so this
    collects all of them rather than returning the first hit. Choosing between
    them is a decision for the author, not for a filename sort.
    """
    return scan_for_manuscripts([here(), *here().parents[:4]])


def describe(folder: Path) -> str:
    root = app.find_root_tex(folder)
    newest = max((p.stat().st_mtime for p in folder.rglob("*.tex")), default=0.0)
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(newest)) if newest else "unknown"
    count = sum(1 for _ in folder.rglob("*.tex"))
    return f"{folder.name}  —  {count} .tex, last edited {when}  ({root.name if root else '?'})"


def choose_from(candidates: list[Path]) -> Path | None:
    """Ask which manuscript to open. Never guesses between several."""
    log("  More than one manuscript folder is nearby:")
    for index, folder in enumerate(candidates, 1):
        log(f"    {index}. {describe(folder)}")
    log()

    try:
        import tkinter as tk
        from tkinter import filedialog
    except ImportError:
        return choose_from_console(candidates)

    picked: dict[str, Path] = {}
    root = tk.Tk()
    root.title("Which manuscript?")
    root.geometry("760x340")

    tk.Label(
        root, text="More than one manuscript folder was found. Choose the one to edit.",
        anchor="w", padx=12, pady=10, justify="left",
    ).pack(fill="x")

    listbox = tk.Listbox(root, font=("Consolas", 10), activestyle="none")
    for folder in candidates:
        listbox.insert("end", f"  {describe(folder)}")
    listbox.selection_set(0)          # most recently edited
    listbox.pack(fill="both", expand=True, padx=12)

    tk.Label(
        root, text="Newest first. The full path of the highlighted folder is shown below.",
        anchor="w", padx=12, fg="#555",
    ).pack(fill="x")
    path_label = tk.Label(root, text=str(candidates[0]), anchor="w", padx=12, fg="#333")
    path_label.pack(fill="x")

    def on_select(_event=None) -> None:
        selection = listbox.curselection()
        if selection:
            path_label.config(text=str(candidates[selection[0]]))

    def use_selected() -> None:
        selection = listbox.curselection()
        if selection:
            picked["folder"] = candidates[selection[0]]
        root.destroy()

    def browse() -> None:
        chosen = filedialog.askdirectory(title="Where is your manuscript?")
        if chosen and looks_like_manuscript(Path(chosen)):
            picked["folder"] = Path(chosen).resolve()
            root.destroy()

    listbox.bind("<<ListboxSelect>>", on_select)
    listbox.bind("<Double-Button-1>", lambda _e: use_selected())

    buttons = tk.Frame(root, pady=10)
    buttons.pack()
    tk.Button(buttons, text="Open this one", width=16, command=use_selected).pack(side="left", padx=6)
    tk.Button(buttons, text="Choose another folder...", width=22, command=browse).pack(side="left", padx=6)
    tk.Button(buttons, text="Cancel", width=10, command=root.destroy).pack(side="left", padx=6)

    root.mainloop()
    return picked.get("folder")


def describe_root(path: Path, manuscript: Path) -> str:
    """One line about a candidate main file: how much of the document it pulls in."""
    rel = path.relative_to(manuscript).as_posix()
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return rel
    includes = len(app._INCLUDE_RE.findall(source))
    title = ""
    match = re.search(r"\\(?:title|thesistitle)\s*\{([^}]{0,60})", source)
    if match:
        title = app._TITLE_CLEAN_RE.sub("", match.group(1)).strip()
    parts = [f"{includes} included file{'' if includes == 1 else 's'}"]
    if title:
        parts.append(f'"{title}"')
    return f"{rel}  —  {', '.join(parts)}"


def choose_root_tex(candidates: list[Path], manuscript: Path) -> Path | None:
    """Ask which .tex is the main document. Chapter order depends on it."""
    log("  This manuscript has more than one main .tex file:")
    for index, path in enumerate(candidates, 1):
        log(f"    {index}. {describe_root(path, manuscript)}")
    log()

    try:
        import tkinter as tk
    except ImportError:
        return choose_from_console(candidates)

    picked: dict[str, Path] = {}
    root = tk.Tk()
    root.title("Which is the main document?")
    root.geometry("720x300")

    tk.Label(
        root, justify="left", anchor="w", padx=12, pady=10,
        text="More than one file here has \\documentclass. Chapter order is read\n"
             "from the one you choose, so pick the main document.",
    ).pack(fill="x")

    listbox = tk.Listbox(root, font=("Consolas", 10), activestyle="none")
    for path in candidates:
        listbox.insert("end", f"  {describe_root(path, manuscript)}")
    listbox.selection_set(0)
    listbox.pack(fill="both", expand=True, padx=12)

    def use_selected() -> None:
        selection = listbox.curselection()
        if selection:
            picked["path"] = candidates[selection[0]]
        root.destroy()

    listbox.bind("<Double-Button-1>", lambda _e: use_selected())
    buttons = tk.Frame(root, pady=10)
    buttons.pack()
    tk.Button(buttons, text="Use this one", width=16, command=use_selected).pack(side="left", padx=6)
    tk.Button(buttons, text="Cancel", width=10, command=root.destroy).pack(side="left", padx=6)

    root.mainloop()
    return picked.get("path")


def choose_from_console(candidates: list[Path]) -> Path | None:
    try:
        answer = input(f"  Which one? [1-{len(candidates)}, or Enter for 1]: ").strip()
    except EOFError:
        return None
    if not answer:
        return candidates[0]
    if answer.isdigit() and 1 <= int(answer) <= len(candidates):
        return candidates[int(answer) - 1]
    return None


def find_manuscript(force_pick: bool = False) -> Path | None:
    """Resolve which manuscript to open, asking only when genuinely ambiguous."""
    override = os.environ.get("REWRITE_MANUSCRIPT")
    if override:
        return Path(override).resolve()

    if not force_pick:
        # A single manuscript beside the executable wins outright: it is what
        # the person meant by putting the binary there.
        local = local_candidates()
        if len(local) == 1:
            return local[0]

        remembered = read_remembered()
        if remembered and looks_like_manuscript(remembered):
            return remembered

    candidates = candidate_manuscripts()
    if len(candidates) == 1 and not force_pick:
        return candidates[0]
    if candidates:
        return choose_from(candidates)
    return ask_for_manuscript()


def ask_for_manuscript() -> Path | None:
    """Native folder picker, used only when the search comes up empty."""
    try:
        import tkinter as tk
        from tkinter import filedialog, messagebox
    except ImportError:
        return None

    root = tk.Tk()
    root.withdraw()
    try:
        messagebox.showinfo(
            "Dissertation rewrite",
            "Choose the folder holding your LaTeX manuscript.\n\n"
            "That is the folder containing the main .tex file, the one with "
            "\\documentclass in it.",
        )
        while True:
            chosen = filedialog.askdirectory(title="Where is your manuscript?")
            if not chosen:
                return None
            folder = Path(chosen).resolve()
            if looks_like_manuscript(folder):
                return folder
            messagebox.showwarning(
                "No LaTeX found",
                f"{folder}\n\nhas no .tex file containing \\documentclass. "
                "Pick the folder with the main document in it.",
            )
    finally:
        root.destroy()


def config_path() -> Path:
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return Path(base) / ".dissertation-rewrite" / "last-manuscript.txt"


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


def repo_root_for(manuscript: Path) -> Path:
    """The enclosing git repository, if there is one.

    Used for the style guide and for per-paragraph commits. Without a
    repository the app still runs; it just cannot commit.
    """
    for folder in [manuscript, *manuscript.parents]:
        if (folder / ".git").exists():
            return folder
    return manuscript


def data_dir_for(manuscript: Path) -> Path:
    """Where state.json and audit.jsonl live.

    Beside the manuscript, so progress travels with the work rather than with
    the executable, which may sit somewhere read-only.
    """
    return manuscript.parent / ".dissertation-rewrite"


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


# --------------------------------------------------------------------------
# Startup
# --------------------------------------------------------------------------

def preflight() -> None:
    if shutil.which("claude") or shutil.which("claude.cmd") or shutil.which("claude.exe"):
        return
    fail(
        "the Claude Code CLI was not found.",
        "The reviewer runs a headless Claude Code session, so `claude` must be\n"
        "installed and logged in on this machine.\n\n"
        "Install it, run `claude` once to log in, then start this again.",
    )


def main() -> None:
    log(BANNER)

    preflight()

    # --pick forces the chooser even when a previous choice is remembered.
    force_pick = "--pick" in sys.argv or bool(os.environ.get("REWRITE_PICK"))

    manuscript = find_manuscript(force_pick=force_pick)
    if manuscript is None and not force_pick:
        log("  No manuscript found nearby. Opening a folder picker...")
        manuscript = ask_for_manuscript()
    if manuscript is None or not looks_like_manuscript(manuscript):
        fail(
            "no LaTeX manuscript was chosen.",
            "Put this next to the folder holding your .tex files, or pick that\n"
            "folder when the chooser appears.",
        )

    repo = repo_root_for(manuscript)
    data = data_dir_for(manuscript)

    # Which .tex is the main document decides the chapter order, so ask when
    # there is a real choice and nothing was chosen before.
    roots = app.find_root_candidates(manuscript)
    chosen_root: Path | None = None
    if len(roots) > 1 and (force_pick or app.remembered_root(data, manuscript) is None):
        chosen_root = choose_root_tex(roots, manuscript)
        if chosen_root is None:
            fail("no main document was chosen.",
                 "Chapter order is read from the main .tex file, so one has to be picked.")

    app.configure(manuscript=manuscript, repo_root=repo, data_dir=data, root_tex=chosen_root)
    remember(manuscript)

    files = app.file_order()
    if not files:
        fail("that folder has no .tex files to work on.", str(manuscript))

    paragraphs = 0
    for rel, _ in files:
        if (manuscript / rel).exists():
            paragraphs += len(app.read_blocks(rel)[1])

    committing = (repo / ".git").exists()
    main_file = app.ROOT_TEX.relative_to(manuscript).as_posix() if app.ROOT_TEX else "?"
    extra = f"   ({len(roots) - 1} other candidate{'' if len(roots) == 2 else 's'})" if len(roots) > 1 else ""
    log(f"  Manuscript : {manuscript}")
    log(f"  Main file  : {main_file}{extra}")
    log(f"  Progress   : {data}")
    log(f"  Chapters   : {len(files)}   Paragraphs: {paragraphs}")
    log(f"  Committing : {'yes, one commit per paragraph' if committing else 'no (not a git repository)'}")
    if not committing:
        state = app.load_state()
        state["settings"]["auto_commit"] = False
        app.save_state(state)

    port = int(os.environ.get("REWRITE_PORT") or free_port())
    url = f"http://127.0.0.1:{port}"

    def open_browser() -> None:
        time.sleep(1.2)
        try:
            webbrowser.open(url)
        except Exception:
            pass

    threading.Thread(target=open_browser, daemon=True).start()

    log()
    log(f"  Open in your browser:  {url}")
    log("  Close this window to stop.  Run with --pick to choose a different folder.")
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
    except Exception as exc:                     # never vanish without a message
        import traceback
        fail("something went wrong.", traceback.format_exc() or str(exc))
