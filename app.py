"""Paragraph-by-paragraph rewrite tool for the dissertation submission.

Left column shows a paragraph from the manuscript, right column takes the
author's replacement. A headless Claude Code session checks the replacement for
lost meaning, grammar, LaTeX validity and machine-written phrasing, and offers a
corrected variant. The author chooses what is written, and every accepted
paragraph is written straight into the .tex.

Run:  python app.py            (then open http://127.0.0.1:8000)
"""

from __future__ import annotations

import difflib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel

import texparse
import reviewer
from reviewer import ReviewError, review

# --------------------------------------------------------------------------
# Paths and configuration
# --------------------------------------------------------------------------

def bundle_dir() -> Path:
    """Where index.html and the bundled style guide live.

    Under PyInstaller the sources are unpacked to a temporary directory rather
    than sitting next to the executable.
    """
    packed = getattr(sys, "_MEIPASS", None)
    return Path(packed) if packed else Path(__file__).resolve().parent


APP_DIR = bundle_dir()


# Defaults for running the server directly. The launcher calls configure() with
# a manuscript it has resolved; these only matter for `python app.py`, which
# works on the current directory unless told otherwise.
MANUSCRIPT = Path(os.environ.get("REWRITE_MANUSCRIPT") or Path.cwd()).resolve()
DATA_DIR = Path(
    os.environ.get("REWRITE_DATA") or MANUSCRIPT.parent / ".rewrite-progress"
).resolve()

STATE_PATH = DATA_DIR / "state.json"
AUDIT_PATH = DATA_DIR / "audit.jsonl"
ORIGINALS_DIR = DATA_DIR / "originals"

# Filled by discover_files() on first use: [(relpath, display title), ...]
FILE_ORDER: list[tuple[str, str]] = []
ROOT_TEX: Path | None = None

# False until a manuscript has been chosen. The frontend shows its setup page
# rather than an empty queue while this is unset.
CONFIGURED = False

# Set by the launcher. Every API call must present it, so that a page on some
# other site cannot drive this server just because it guessed the port. The app
# writes to the manuscript, so an open endpoint is not acceptable.
ACCESS_TOKEN: str = ""


def configure(
    manuscript: Path,
    data_dir: Path,
    root_tex: Path | None = None,
) -> None:
    """Point the app at a manuscript. Called by the launcher before serving.

    `root_tex` names the main document explicitly. Passing it records the
    choice, so a manuscript with several candidate roots is only asked about
    once.
    """
    global MANUSCRIPT, DATA_DIR, STATE_PATH, AUDIT_PATH, ORIGINALS_DIR
    global FILE_ORDER, ROOT_TEX, CONFIGURED
    MANUSCRIPT = manuscript.resolve()
    DATA_DIR = data_dir.resolve()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    # Progress belongs to the author, not the repository. Ignoring the whole
    # directory (itself included) keeps it out of any version control the
    # author happens to be using.
    ignore = DATA_DIR / ".gitignore"
    if not ignore.exists():
        ignore.write_text("*\n", encoding="utf-8")
    STATE_PATH = DATA_DIR / "state.json"
    AUDIT_PATH = DATA_DIR / "audit.jsonl"
    ORIGINALS_DIR = DATA_DIR / "originals"

    state = load_state()
    if root_tex is None:
        stored = state["settings"].get("root_tex")
        if stored and (MANUSCRIPT / stored).exists():
            root_tex = MANUSCRIPT / stored
    else:
        root_tex = root_tex if root_tex.is_absolute() else MANUSCRIPT / root_tex
        state["settings"]["root_tex"] = root_tex.resolve().relative_to(MANUSCRIPT).as_posix()
        save_state(state)

    ROOT_TEX = find_root_tex(MANUSCRIPT, root_tex)
    FILE_ORDER = discover_files(MANUSCRIPT, ROOT_TEX)
    REVIEW_SESSION.reset()
    CONFIGURED = True


# --------------------------------------------------------------------------
# Working out which .tex files make up the manuscript, and in what order
# --------------------------------------------------------------------------

_INCLUDE_RE = re.compile(r"\\(?:include|input)\s*\{([^}]+)\}")
_CHAPTER_RE = re.compile(r"\\chapter\*?\s*(?:\[[^\]]*\])?\{([^}]*)\}")
_TITLE_CLEAN_RE = re.compile(r"\\[a-zA-Z]+\s*|[{}]")


ROOT_NAME_HINTS = ("thesis", "main", "dissertation", "report", "document", "root")


def find_root_candidates(manuscript: Path, max_depth: int | None = None) -> list[Path]:
    """Every .tex carrying \\documentclass — each a document LaTeX could build.

    A folder often holds more than one: a thesis beside a standalone paper, a
    poster, or a leftover template. Ranked most-likely first, but the choice
    between them belongs to the author.

    `max_depth` bounds how far down to look. Depth 1 answers "is this folder
    itself a manuscript", which is not the same question as "does a manuscript
    live somewhere under here" — every ancestor directory would pass that one.
    """
    found: list[Path] = []
    for path in sorted(manuscript.rglob("*.tex")):
        parts = path.relative_to(manuscript).parts
        if any(part.startswith(".") for part in parts):
            continue
        if max_depth is not None and len(parts) > max_depth:
            continue
        try:
            head = path.read_text(encoding="utf-8", errors="replace")[:4000]
        except OSError:
            continue
        if "\\documentclass" in head:
            found.append(path)

    def rank(path: Path) -> tuple:
        depth = len(path.relative_to(manuscript).parts)
        stem = path.stem.lower()
        hinted = 0 if any(h in stem for h in ROOT_NAME_HINTS) else 1
        includes = 0
        try:
            includes = -len(_INCLUDE_RE.findall(path.read_text(encoding="utf-8", errors="replace")))
        except OSError:
            pass
        # Shallowest first, then a recognisable name, then whoever includes most.
        return (depth, hinted, includes, path.name.lower())

    return sorted(found, key=rank)


def find_root_tex(manuscript: Path, preferred: Path | None = None) -> Path | None:
    """The main document. `preferred` wins when it is still a real file."""
    if preferred is not None:
        candidate = preferred if preferred.is_absolute() else manuscript / preferred
        if candidate.exists():
            return candidate
    candidates = find_root_candidates(manuscript)
    if candidates:
        return candidates[0]
    any_tex = sorted(manuscript.glob("*.tex"))
    return any_tex[0] if any_tex else None


def remembered_root(data_dir: Path, manuscript: Path) -> Path | None:
    """A previously chosen main file for this manuscript, if still valid."""
    path = data_dir / "state.json"
    if not path.exists():
        return None
    try:
        stored = json.loads(path.read_text(encoding="utf-8")).get("settings", {}).get("root_tex")
    except (OSError, json.JSONDecodeError):
        return None
    if not stored:
        return None
    candidate = manuscript / stored
    return candidate if candidate.exists() else None


def discover_files(manuscript: Path, root: Path | None = None) -> list[tuple[str, str]]:
    """Chapter files in the order the document includes them.

    The root file comes first, because on this manuscript it carries the
    abstract and the acknowledgments. Anything the root does not include is
    appended afterwards so a stray chapter still reaches the queue.
    """
    root = root or find_root_tex(manuscript)
    if root is None:
        return []

    source = root.read_text(encoding="utf-8", errors="replace")
    root_rel = root.relative_to(manuscript).as_posix()
    ordered: list[tuple[str, str]] = [(root_rel, "Front matter")]
    seen = {root_rel}
    chapter_number = 0

    for raw in _INCLUDE_RE.findall(source):
        name = raw.strip().lstrip("./")
        if not name.endswith(".tex"):
            name += ".tex"
        # \include paths resolve against the main file's directory, which is not
        # the manuscript root when the document lives in a subfolder.
        path = root.parent / name
        if not path.exists():
            path = manuscript / name
        if not path.exists():
            continue
        try:
            rel = path.resolve().relative_to(manuscript.resolve()).as_posix()
        except ValueError:
            continue
        if rel in seen:
            continue
        seen.add(rel)

        body = path.read_text(encoding="utf-8", errors="replace")
        match = _CHAPTER_RE.search(body)
        title = _TITLE_CLEAN_RE.sub("", match.group(1)).strip() if match else path.stem
        if match and "appendi" not in rel.lower():
            chapter_number += 1
            title = f"{chapter_number}. {title}"
        ordered.append((rel, title))

    # Sweep up prose the root never includes, so nothing is silently dropped.
    # Other candidate roots are skipped: a poster or a leftover template beside
    # the thesis is a separate document, not a chapter of this one.
    other_roots = {
        p.relative_to(manuscript).as_posix()
        for p in find_root_candidates(manuscript)
    } - seen
    for path in sorted(manuscript.rglob("*.tex")):
        rel = path.relative_to(manuscript).as_posix()
        if rel in seen or rel in other_roots:
            continue
        if any(part.startswith(".") for part in Path(rel).parts):
            continue
        ordered.append((rel, path.stem))

    return ordered

# Offered in the frontend picker. The CLI resolves these aliases to the current
# model in each family, so they do not go stale.
AVAILABLE_MODELS = [
    {"id": "sonnet", "label": "Sonnet",
     "note": "the default. About $0.05 and 20s per review."},
    {"id": "haiku", "label": "Haiku",
     "note": "not the cheap option here. Measured slower and barely cheaper "
             "than Sonnet, because it writes far longer findings."},
    {"id": "opus", "label": "Opus",
     "note": "most thorough, slowest and dearest. Worth it for a final pass."},
]

DEFAULT_SETTINGS = {
    "word_limit": 30000,
    "model": "sonnet",
    # None detects the file's own wrap column, 0 disables re-wrapping.
    "wrap_width": None,
    # Path to a markdown file, or None for no writing rules at all.
    "style_guide": None,
    # How the rules reach the reviewer. See REVIEW_MODES.
    "review_mode": "hybrid",
    "hybrid_turns": 4,
    "session_turns": 30,
}

# Figures below are measured, not estimated: 24 paragraphs damaged identically
# and reviewed by all three modes. See compare_modes.py and the README.
REVIEW_MODES = [
    {
        "id": "fresh",
        "label": "Fresh each review",
        "summary": "Every review is its own process carrying the full rules.",
        "cost": 0.0655,
        "knob": None,
        "pros": [
            "Nothing carries over: no paragraph can influence the verdict on another.",
            "Graded severity most strictly of the three in testing.",
            "Cost per review never changes, however long you work.",
        ],
        "cons": [
            "Dearest: about $16.50 for a 252-paragraph pass.",
            "Slowest, around 19s per review.",
            "Re-sends the same rules every time, which is most of what you pay for.",
        ],
    },
    {
        "id": "hybrid",
        "label": "Hybrid",
        "summary": "Sends the rules once, resumes for a few reviews, then starts over.",
        "cost": 0.0437,
        "knob": {"setting": "hybrid_turns", "label": "Re-send the rules every",
                 "suffix": "reviews", "min": 2, "max": 20},
        "pros": [
            "About a third cheaper than Fresh: roughly $11 for a full pass.",
            "Detected the same faults as Fresh on 21 of 24 paragraphs.",
            "Cost per review stays flat, because history is cleared regularly.",
            "Re-reads the full rules often, so it cannot drift far from them.",
        ],
        "cons": [
            "Grades severity slightly more leniently than Fresh.",
            "Within a run of reviews the reviewer can see the previous few.",
        ],
    },
    {
        "id": "session",
        "label": "One long session",
        "summary": "Sends the rules once and keeps resuming the same conversation.",
        "cost": 0.0390,
        "knob": {"setting": "session_turns", "label": "Start a new session every",
                 "suffix": "reviews", "min": 5, "max": 100},
        "pros": [
            "Cheapest per review early on, and the fastest at about 12s.",
            "Agreed with Fresh on every verdict in testing.",
        ],
        "cons": [
            "Cost climbs as it runs: 62% more by the 24th review than the first few.",
            "Over a full cycle it saves no more than Hybrid, for more accumulated context.",
            "The reviewer carries every paragraph it has already seen.",
        ],
    },
]


def mode_turns(settings: dict) -> int:
    """How many reviews one primed conversation serves. 0 means never reuse."""
    mode = settings.get("review_mode", "hybrid")
    if mode == "hybrid":
        return max(2, int(settings.get("hybrid_turns") or 4))
    if mode == "session":
        return max(5, int(settings.get("session_turns") or 30))
    return 0

# The live conversation, when session mode is on. Deliberately not persisted:
# a session id belongs to a running CLI, not to the manuscript.
REVIEW_SESSION = reviewer.ReviewSession()

_lock = threading.Lock()

# Reviews may now be started from one paragraph and collected from another, so
# several can be in flight. They are still run one at a time: session modes
# resume a single conversation, and two overlapping resumes would interleave
# turns in it. Queueing keeps the editor responsive without that risk.
_review_lock = threading.Lock()


# --------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------

EMPTY_USAGE = {
    "reviews": 0, "cost_usd": 0.0, "input_tokens": 0, "output_tokens": 0,
    "cache_read_tokens": 0, "cache_write_tokens": 0, "duration_ms": 0,
}


def load_state() -> dict:
    if STATE_PATH.exists():
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    else:
        state = {"version": 1, "records": {}, "settings": {}}
    state.setdefault("records", {})
    state["usage"] = {**EMPTY_USAGE, **state.get("usage", {})}
    state.setdefault("usage_by_model", {})
    settings = {**DEFAULT_SETTINGS, **state.get("settings", {})}
    state["settings"] = settings
    return state


def accumulate_usage(state: dict, meta: dict) -> None:
    """Add one review's token spend to the running totals."""
    model = meta.get("model") or state["settings"]["model"]
    buckets = [state["usage"], state["usage_by_model"].setdefault(model, dict(EMPTY_USAGE))]
    for bucket in buckets:
        bucket["reviews"] += 1
        bucket["cost_usd"] = round(bucket["cost_usd"] + (meta.get("cost_usd") or 0.0), 6)
        for field in ("input_tokens", "output_tokens", "cache_read_tokens",
                      "cache_write_tokens", "duration_ms"):
            bucket[field] += meta.get(field) or 0


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def audit(entry: dict) -> None:
    entry["ts"] = datetime.now(timezone.utc).isoformat()
    with AUDIT_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


def style_guide_file() -> Path:
    """Where the chosen writing rules are kept.

    A browser cannot tell the server where a picked file lives — it only hands
    over the contents — so the text is stored here. It is a plain markdown file,
    read fresh on every review, so it can also be edited in place.
    """
    return DATA_DIR / "style-guide.md"


def style_guide_text() -> str:
    """The writing conventions in force, or nothing at all."""
    path = style_guide_file()
    if not load_state()["settings"].get("style_guide") or not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def file_order() -> list[tuple[str, str]]:
    """FILE_ORDER, discovered on first use when running from source."""
    global FILE_ORDER, ROOT_TEX
    if not FILE_ORDER:
        ROOT_TEX = ROOT_TEX or find_root_tex(MANUSCRIPT)
        FILE_ORDER = discover_files(MANUSCRIPT, ROOT_TEX)
    return FILE_ORDER


# --------------------------------------------------------------------------
# Manuscript access
# --------------------------------------------------------------------------

def resolve(relpath: str) -> Path:
    path = (MANUSCRIPT / relpath).resolve()
    if MANUSCRIPT.resolve() not in path.parents and path != MANUSCRIPT.resolve():
        raise HTTPException(400, f"{relpath} is outside the manuscript directory")
    if not path.exists():
        raise HTTPException(404, f"{relpath} does not exist")
    return path


def read_source(path: Path) -> str:
    """Read a .tex file without translating its line endings."""
    with path.open("r", encoding="utf-8", newline="") as handle:
        return handle.read()


def write_source(path: Path, text: str) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        handle.write(text)


def read_blocks(relpath: str) -> tuple[str, list[texparse.Block]]:
    source = read_source(resolve(relpath))
    return source, texparse.prose_blocks(source)


def record_id(relpath: str, key: str) -> str:
    return f"{relpath}::{key}"


def status_of(state: dict, relpath: str, block: texparse.Block) -> tuple[str, dict | None]:
    """Whether a block has already been dealt with.

    A block is matched by its own key first. If it has been rewritten, the text
    in the file is the accepted version, so the record is found by the key that
    version hashes to instead.
    """
    records = state["records"]
    direct = records.get(record_id(relpath, block.key))
    if direct:
        return direct["status"], direct
    for record in records.values():
        if record.get("file") == relpath and record.get("final_key") == block.key:
            return record["status"], record
    return "pending", None


def find_block(relpath: str, key: str) -> tuple[str, texparse.Block]:
    source, blocks = read_blocks(relpath)
    for block in blocks:
        if block.key == key:
            return source, block
    # The paragraph may already carry an accepted rewrite, in which case its key
    # has moved on. Fall back to the recorded final text.
    state = load_state()
    record = state["records"].get(record_id(relpath, key))
    if record and record.get("final_key"):
        for block in blocks:
            if block.key == record["final_key"]:
                return source, block
    raise HTTPException(404, f"Paragraph {key} was not found in {relpath}")


def keep_original(relpath: str, source: str) -> None:
    """Keep one untouched copy of a file, before this app first changes it.

    Made once per file and never overwritten, so it always holds the text as it
    was before any paragraph was replaced. Paired with audit.jsonl — which
    records every original and its replacement — that is enough to undo
    anything, without assuming the manuscript is in version control.
    """
    target = ORIGINALS_DIR / relpath.replace("\\", "/")
    if target.exists():
        return
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("w", encoding="utf-8", newline="") as handle:
            handle.write(source)
    except OSError:
        pass          # a missing backup must not stop the author working


# --------------------------------------------------------------------------
# Aggregates
# --------------------------------------------------------------------------

def file_summary(state: dict, relpath: str, title: str) -> dict:
    path = MANUSCRIPT / relpath
    if not path.exists():
        return {"file": relpath, "title": title, "missing": True,
                "total": 0, "done": 0, "skipped": 0, "pending": 0, "words": 0}
    source, blocks = read_blocks(relpath)
    counts: dict[str, int] = {"done": 0, "skipped": 0, "pending": 0}
    for block in blocks:
        counts[status_of(state, relpath, block)[0]] += 1
    return {
        "file": relpath,
        "title": title,
        "missing": False,
        "total": len(blocks),
        "words": sum(b.words for b in blocks),
        "file_words": texparse.count_words(source),
        **counts,
    }


def overview(state: dict) -> dict:
    if not CONFIGURED:
        return {"configured": False, "files": [], "settings": state["settings"],
                "models": AVAILABLE_MODELS, "review_modes": REVIEW_MODES,
                "usage": state["usage"], "usage_by_model": state["usage_by_model"],
                "totals": {"prose_words": 0, "document_words": 0, "paragraphs": 0,
                           "done": 0, "skipped": 0, "pending": 0}}
    files = [file_summary(state, rel, title) for rel, title in file_order()]
    present = [f for f in files if not f["missing"]]
    candidates = find_root_candidates(MANUSCRIPT)
    return {
        "configured": True,
        "files": files,
        "settings": state["settings"],
        "models": AVAILABLE_MODELS,
        "review_modes": REVIEW_MODES,
        "manuscript": str(MANUSCRIPT),
        "root_tex": ROOT_TEX.relative_to(MANUSCRIPT).as_posix() if ROOT_TEX else None,
        "root_choices": [p.relative_to(MANUSCRIPT).as_posix() for p in candidates],
        "style": state["settings"].get("style_guide"),
        "style_file": str(style_guide_file()),
        "style_words": len(style_guide_text().split()),
        "usage": state["usage"],
        "usage_by_model": state["usage_by_model"],
        "totals": {
            "prose_words": sum(f["words"] for f in present),
            "document_words": sum(f["file_words"] for f in present),
            "paragraphs": sum(f["total"] for f in present),
            "done": sum(f["done"] for f in present),
            "skipped": sum(f["skipped"] for f in present),
            "pending": sum(f["pending"] for f in present),
        },
    }


# --------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------

app = FastAPI(title="Dissertation rewrite")


class ReviewRequest(BaseModel):
    file: str
    key: str
    rewrite: str


class AcceptRequest(BaseModel):
    file: str
    key: str
    text: str
    source: str = "mine"          # mine | corrected | overwrite
    verdict: str | None = None


class KeyRequest(BaseModel):
    file: str
    key: str


class SettingsRequest(BaseModel):
    word_limit: int | None = None
    model: str | None = None
    style_guide: str | None = None
    review_mode: str | None = None
    hybrid_turns: int | None = None
    session_turns: int | None = None


@app.middleware("http")
async def require_token(request, call_next):
    """Reject API calls that do not carry the launcher's token.

    The token arrives once as a query parameter on the page URL and is stored
    in a cookie. Without this, any page in the browser could post to this
    server, and this server writes to the manuscript.
    """
    path = request.url.path
    if ACCESS_TOKEN and path.startswith("/api/"):
        supplied = request.cookies.get("rewrite_token") or request.headers.get("x-rewrite-token")
        if supplied != ACCESS_TOKEN:
            return JSONResponse({"error": "This page is out of date. Reopen the "
                                          "link printed in the console."}, status_code=403)
    return await call_next(request)


@app.get("/")
def index(token: str = "") -> FileResponse:
    response = FileResponse(APP_DIR / "index.html")
    if ACCESS_TOKEN and token == ACCESS_TOKEN:
        response.set_cookie("rewrite_token", ACCESS_TOKEN, httponly=True, samesite="strict")
    return response


# --------------------------------------------------------------------------
# Choosing a manuscript, from inside the app
# --------------------------------------------------------------------------

def describe_folder(folder: Path) -> dict:
    """Enough about a folder for someone to recognise their own work."""
    try:
        tex = [p for p in folder.rglob("*.tex")
               if not any(part.startswith(".") for part in p.relative_to(folder).parts)]
    except OSError:
        tex = []
    newest = max((p.stat().st_mtime for p in tex), default=0.0)
    roots = find_root_candidates(folder, max_depth=1)
    return {
        "path": str(folder),
        "name": folder.name or str(folder),
        "tex_count": len(tex),
        "modified": datetime.fromtimestamp(newest, tz=timezone.utc).isoformat() if newest else None,
        "roots": [p.name for p in roots],
        "is_manuscript": bool(roots),
    }


def nearby_manuscripts(start: Path) -> list[dict]:
    """Manuscript folders at or just below the given directory, newest first."""
    found: list[Path] = []
    for base in (start, *list(start.parents)[:3]):
        candidates = [base]
        try:
            candidates += [p for p in base.iterdir()
                           if p.is_dir() and not p.name.startswith((".", "__"))]
        except OSError:
            continue
        for folder in candidates:
            if folder not in found and find_root_candidates(folder, max_depth=1):
                found.append(folder)
    described = [describe_folder(f) for f in found]
    described.sort(key=lambda d: d["modified"] or "", reverse=True)
    return described[:30]


@app.get("/api/setup")
def api_setup() -> dict:
    start = Path(os.environ.get("REWRITE_START") or Path.cwd())
    return {
        "configured": CONFIGURED,
        "manuscript": str(MANUSCRIPT) if CONFIGURED else None,
        "suggestions": nearby_manuscripts(start),
        "start": str(start),
        "home": str(Path.home()),
    }


@app.get("/api/browse")
def api_browse(path: str = "", files: str = "") -> dict:
    """List sub-folders, so a manuscript can be found without typing a path.

    With `files` set to an extension, matching files are listed too — used to
    pick a style guide without having to type its path.
    """
    folder = Path(path).expanduser() if path else Path.home()
    try:
        folder = folder.resolve()
        if not folder.is_dir():
            raise HTTPException(400, f"{folder} is not a folder")
        entries = sorted(
            (p for p in folder.iterdir() if p.is_dir() and not p.name.startswith(".")),
            key=lambda p: p.name.lower(),
        )
    except HTTPException:
        raise
    except OSError as exc:
        raise HTTPException(400, f"Cannot open {folder}: {exc}") from exc

    matching: list[dict] = []
    if files:
        suffix = files if files.startswith(".") else f".{files}"
        try:
            matching = [
                {"name": p.name, "path": str(p), "size": p.stat().st_size}
                for p in sorted(folder.iterdir())
                if p.is_file() and p.suffix.lower() == suffix.lower()
                and not p.name.startswith(".")
            ][:400]
        except OSError:
            matching = []

    return {
        "path": str(folder),
        "parent": str(folder.parent) if folder.parent != folder else None,
        "self": describe_folder(folder),
        "folders": [
            {"name": p.name, "path": str(p),
             "is_manuscript": bool(find_root_candidates(p, max_depth=1))}
            for p in entries[:400]
        ],
        "files": matching,
    }


class StyleRequest(BaseModel):
    name: str = ""
    text: str = ""


@app.post("/api/style")
def api_style(request: StyleRequest) -> dict:
    """Store the writing rules the browser read from a file the author picked.

    The page sends the text because it cannot send a path. An empty request
    clears the rules.
    """
    with _lock:
        state = load_state()
        path = style_guide_file()
        if not request.text.strip():
            state["settings"]["style_guide"] = None
            path.unlink(missing_ok=True)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(request.text, encoding="utf-8")
            state["settings"]["style_guide"] = request.name or "style-guide.md"
        save_state(state)
        REVIEW_SESSION.reset()     # a primed session holds the old rules
        return {"ok": True, "style": state["settings"]["style_guide"],
                "words": len(style_guide_text().split()),
                "stored_at": str(path)}


class SetupRequest(BaseModel):
    path: str
    root_tex: str | None = None


@app.post("/api/setup")
def api_choose(request: SetupRequest) -> dict:
    folder = Path(request.path).expanduser()
    try:
        folder = folder.resolve()
    except OSError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not folder.is_dir():
        raise HTTPException(400, f"{folder} is not a folder.")

    roots = find_root_candidates(folder, max_depth=1)
    if not roots:
        raise HTTPException(
            400,
            f"No .tex file in {folder.name} contains \\documentclass, so this is "
            f"not a manuscript folder. Pick the folder holding the main document.",
        )

    # More than one main document is a real choice, so ask rather than guess.
    if len(roots) > 1 and not request.root_tex:
        return {
            "needs_root": True,
            "path": str(folder),
            "roots": [
                {"name": p.name,
                 "rel": p.relative_to(folder).as_posix(),
                 "includes": len(_INCLUDE_RE.findall(
                     p.read_text(encoding="utf-8", errors="replace")))}
                for p in roots
            ],
        }

    configure(
        manuscript=folder,
        data_dir=folder.parent / ".rewrite-progress",
        root_tex=Path(request.root_tex) if request.root_tex else None,
    )
    return {"ok": True, "configured": True, "manuscript": str(MANUSCRIPT),
            "root_tex": ROOT_TEX.name if ROOT_TEX else None,
            "files": len(file_order())}


@app.get("/api/overview")
def api_overview() -> dict:
    return overview(load_state())


@app.get("/api/paragraphs")
def api_paragraphs(file: str) -> dict:
    state = load_state()
    _, blocks = read_blocks(file)
    items = []
    for block in blocks:
        status, record = status_of(state, file, block)
        items.append({
            "key": block.key,
            "index": block.index,
            "words": block.words,
            "status": status,
            "preview": texparse.normalise(texparse.strip_tex(block.text))[:110],
            "source": (record or {}).get("source"),
        })
    return {"file": file, "paragraphs": items}


@app.get("/api/paragraph")
def api_paragraph(file: str, key: str) -> dict:
    state = load_state()
    _, block = find_block(file, key)
    status, record = status_of(state, file, block)
    return {
        "file": file,
        "key": block.key,
        "index": block.index,
        "text": block.text,
        "words": block.words,
        "status": status,
        "record": record,
    }


@app.post("/api/review")
def api_review(request: ReviewRequest) -> dict:
    if not request.rewrite.strip():
        raise HTTPException(400, "The rewrite is empty.")
    state = load_state()
    _, block = find_block(request.file, request.key)
    settings = state["settings"]
    turns = mode_turns(settings)
    session = None
    if turns:
        REVIEW_SESSION.max_turns = turns
        session = REVIEW_SESSION

    try:
        with _review_lock:
            result = review(
                original=block.text,
                rewrite=request.rewrite,
                relpath=request.file,
                style_guide=style_guide_text(),
                model=settings["model"],
                cwd=MANUSCRIPT,
                session=session,
            )
    except ReviewError as exc:
        if session is not None:
            session.reset()          # a broken session must not poison the next review
        raise HTTPException(502, str(exc)) from exc
    result["words"] = {
        "original": block.words,
        "rewrite": texparse.count_words(request.rewrite),
        "corrected": texparse.count_words(result["corrected"]),
    }
    with _lock:
        state = load_state()
        accumulate_usage(state, result.get("meta") or {})
        save_state(state)
        result["usage"] = state["usage"]
    audit({"event": "review", "file": request.file, "key": request.key,
           "verdict": result["verdict"], "summary": result.get("summary", ""),
           "meta": result.get("meta")})
    return result


@app.post("/api/accept")
def api_accept(request: AcceptRequest) -> dict:
    text = request.text.strip()
    if not text:
        raise HTTPException(400, "Refusing to write an empty paragraph.")

    with _lock:
        state = load_state()
        source, block = find_block(request.file, request.key)
        path = resolve(request.file)

        # Taken before the first change to this file, so it holds the text as
        # the author last left it.
        keep_original(request.file, source)

        # Match the file's own hard-wrap column so the source stays consistent
        # and the diff shows only the words that changed.
        configured = state["settings"].get("wrap_width")
        width = texparse.detect_wrap_width(source) if configured is None else int(configured)
        text = texparse.rewrap(text, width)

        before_words = block.words
        after_words = texparse.count_words(text)
        updated = texparse.splice(source, block, text)
        write_source(path, updated)

        rid = record_id(request.file, request.key)
        existing = state["records"].get(rid, {})
        state["records"][rid] = {
            "file": request.file,
            "status": "done",
            "source": request.source,
            "verdict": request.verdict,
            "original": existing.get("original", block.text),
            "final": text,
            "final_key": texparse.content_key(text),
            "words_before": existing.get("words_before", before_words),
            "words_after": after_words,
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        save_state(state)

        audit({"event": "accept", "file": request.file, "key": request.key,
               "source": request.source, "verdict": request.verdict,
               "original": block.text, "final": text,
               "words_before": before_words, "words_after": after_words})

        return {"ok": True, "words_before": before_words,
                "words_after": after_words, "overview": overview(state)}


@app.post("/api/skip")
def api_skip(request: KeyRequest) -> dict:
    with _lock:
        state = load_state()
        _, block = find_block(request.file, request.key)
        rid = record_id(request.file, request.key)
        record = state["records"].get(rid, {})
        record.update({
            "file": request.file, "status": "skipped",
            "original": record.get("original", block.text),
            "ts": datetime.now(timezone.utc).isoformat(),
        })
        state["records"][rid] = record
        save_state(state)
        audit({"event": "skip", "file": request.file, "key": request.key})
        return {"ok": True, "overview": overview(state)}


@app.post("/api/revert")
def api_revert(request: KeyRequest) -> dict:
    """Put one paragraph back to the text it had before it was replaced.

    The file is edited in place, exactly as accepting does, so this undoes a
    single paragraph without disturbing anything else in the document.
    """
    with _lock:
        state = load_state()
        rid = record_id(request.file, request.key)
        record = state["records"].get(rid)
        if not record or record.get("status") != "done":
            raise HTTPException(400, "That paragraph has not been changed.")

        source, block = find_block(request.file, request.key)
        original = record.get("original")
        if not original:
            raise HTTPException(400, "No original text was recorded for it.")

        write_source(resolve(request.file), texparse.splice(source, block, original))
        state["records"].pop(rid, None)
        save_state(state)
        audit({"event": "revert", "file": request.file, "key": request.key,
               "restored": original, "replaced": record.get("final")})
        return {"ok": True, "overview": overview(state)}


@app.get("/api/changes")
def api_changes() -> dict:
    """Every paragraph this app has replaced, newest first."""
    state = load_state()
    order = {rel: i for i, (rel, _) in enumerate(file_order())}
    titles = dict(file_order())
    rows = []
    for rid, record in state["records"].items():
        if record.get("status") != "done":
            continue
        relpath = record.get("file", "")
        key = rid.split("::", 1)[1] if "::" in rid else ""
        rows.append({
            "file": relpath,
            "title": titles.get(relpath, relpath),
            "key": key,
            "original": record.get("original", ""),
            "final": record.get("final", ""),
            "words_before": record.get("words_before"),
            "words_after": record.get("words_after"),
            "verdict": record.get("verdict"),
            "source": record.get("source"),
            "ts": record.get("ts"),
            "order": order.get(relpath, 999),
        })
    rows.sort(key=lambda r: r["ts"] or "", reverse=True)
    changed_words = sum((r["words_after"] or 0) - (r["words_before"] or 0) for r in rows)
    return {"changes": rows, "count": len(rows), "word_delta": changed_words}


@app.get("/api/diff")
def api_diff() -> PlainTextResponse:
    """A unified diff of everything this app has changed, as a patch file.

    Compares the untouched copies kept before the first edit against the files
    as they stand, so it shows the whole of Vartika's work in one place and can
    be read, kept, or reversed with `patch -R`.
    """
    chunks: list[str] = []
    for relpath, _title in file_order():
        original = ORIGINALS_DIR / relpath
        current = MANUSCRIPT / relpath
        if not original.exists() or not current.exists():
            continue
        before = read_source(original).splitlines(keepends=True)
        after = read_source(current).splitlines(keepends=True)
        chunks.extend(difflib.unified_diff(
            before, after, fromfile=f"a/{relpath}", tofile=f"b/{relpath}", n=3))
    body = "".join(chunks) or "No changes yet.\n"
    return PlainTextResponse(body, headers={
        "Content-Disposition": 'attachment; filename="vartika-changes.patch"'})


@app.post("/api/reopen")
def api_reopen(request: KeyRequest) -> dict:
    """Put a skipped or completed paragraph back in the queue.

    The manuscript is not reverted. This only clears the bookkeeping, so an
    accepted paragraph can be worked on again from its current text.
    """
    with _lock:
        state = load_state()
        _, block = find_block(request.file, request.key)
        for rid in (record_id(request.file, request.key),
                    record_id(request.file, block.key)):
            state["records"].pop(rid, None)
        save_state(state)
        audit({"event": "reopen", "file": request.file, "key": request.key})
        return {"ok": True, "overview": overview(state)}


@app.post("/api/settings")
def api_settings(request: SettingsRequest) -> dict:
    with _lock:
        state = load_state()
        changes = request.model_dump(exclude_none=True)
        for field, value in changes.items():
            state["settings"][field] = value
        save_state(state)
        # A primed conversation carries the old model and style rules, so any
        # change to them retires it rather than reusing a stale priming.
        if {"model", "style_guide", "review_mode",
            "hybrid_turns", "session_turns"} & set(changes):
            REVIEW_SESSION.reset()
        return {"ok": True, "settings": state["settings"]}


@app.post("/api/wordcount")
def api_wordcount(payload: dict) -> dict:
    return {"words": texparse.count_words(payload.get("text", ""))}


@app.get("/api/texcount")
def api_texcount() -> dict:
    """The authoritative word count, from texcount over the whole document.

    The app's own counter is a fast approximation used for the live per-paragraph
    figures. This is the number to trust against the limit, so it is reported
    separately rather than silently replacing the estimate.
    """
    if not shutil.which("texcount"):
        return {"available": False}
    try:
        completed = subprocess.run(
            ["texcount", "-inc", "-total", "-q", "thesis.tex"],
            cwd=str(MANUSCRIPT), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"available": False, "error": str(exc)}

    counts: dict[str, int] = {}
    for line in completed.stdout.splitlines():
        for label, field in (
            ("Words in text:", "text"),
            ("Words in headers:", "headers"),
            ("Words outside text", "captions"),
        ):
            if line.startswith(label):
                digits = "".join(c for c in line.split(":")[-1] if c.isdigit())
                if digits:
                    counts[field] = int(digits)
    if "text" not in counts:
        return {"available": False, "error": "texcount produced no total"}
    counts["total"] = sum(counts.values())
    return {"available": True, **counts}


@app.exception_handler(HTTPException)
def http_error(_request, exc: HTTPException) -> JSONResponse:
    return JSONResponse({"error": exc.detail}, status_code=exc.status_code)


if __name__ == "__main__":
    import uvicorn

    print(f"Manuscript : {MANUSCRIPT}")
    style = style_guide_path()
    print(f"Style guide: {style if style else 'none (writing rules are optional)'}")
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="warning")
