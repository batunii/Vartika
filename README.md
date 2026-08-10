# Vartika

Rewrite a LaTeX manuscript one paragraph at a time, with a reviewer that checks
you have not lost anything in the process.

The original paragraph sits on the left, your version on the right. When you ask
for a review, a headless [Claude Code](https://claude.com/claude-code) session
reads both and reports what your version dropped, what it invented, what is
ungrammatical, what LaTeX it broke, and whether it reads as machine-written. It
then offers a corrected variant that is *your sentences with the faults
repaired*, not its rewrite of them.

You decide what gets written. The tool never edits your prose on its own.

## Requirements

**Claude Code, installed and logged in.** Vartika runs `claude` to do the
reviewing, so it uses your own Claude account and your own quota. It cannot be
bundled into the binary — it is a separate tool with its own login. Vartika
checks for it at startup and says so plainly if it is missing.

No API key is needed. Nothing is uploaded anywhere: the server binds to
localhost and your files never leave your machine.

## Running it

Download the binary for your platform from
[Releases](../../releases) and run it. It finds your manuscript, starts a local
server on a free port, and opens your browser. The console window it opens is
the status display and the stop button.

- **macOS**: unsigned, so the first run is blocked. Right-click, Open, confirm —
  or `xattr -d com.apple.quarantine ./Vartika-macos-*` once.
- **Linux**: `chmod +x Vartika-linux` if the download drops the flag.

From source instead:

```sh
pip install -r requirements.txt
python launcher.py            # finds the manuscript, opens a browser
python app.py                 # server only, port 8000, no browser
python build.py               # build the standalone binary (needs pyinstaller)
```

## Which manuscript it opens

A manuscript folder is one containing a `.tex` file with `\documentclass` in it.

In order of preference: a single manuscript sitting beside the executable wins
outright, then the folder you chose last time, then a single folder found
nearby. **If several are found it asks**, listing each with its file count and
last-edited date, newest first. It will not guess between a working copy, a
snapshot and a template. Run with `--pick` to choose again.

Chapter order is read from the `\include` lines of the main `.tex`, and chapter
names from each file's `\chapter{...}`. If a folder holds several files with
`\documentclass` — a thesis beside a poster or a paper — it asks which is the
main document, ranking them by how much each pulls in. That choice is remembered
per manuscript. The file in use is shown beside **Files** in the sidebar, marked
amber when there are alternatives.

Progress lives in `.rewrite-progress/` beside the manuscript, so it travels with
the work rather than the executable. That folder ignores itself in git.

## Working through it

| Action | Effect |
|---|---|
| **Review** (`Ctrl ⏎`) | Send both versions to the reviewer. Nothing is written. |
| **Accept mine** (`Ctrl S`) | Write your text. Disabled on a `fail` verdict. |
| **Accept corrected** (`Ctrl ⇧ S`) | Write the reviewer's corrected variant instead. |
| **Overwrite anyway** | Write your text regardless, after confirming what you are dropping. |
| **Skip** | Leave it and move on. |
| **Reopen** | Put a done or skipped paragraph back in the queue. |
| **Copy original** (`Ctrl K`) | Load the original into the editor as a starting point. |
| `Alt ←` / `Alt →` | Previous / next paragraph. |

The queue holds prose only. Everything inside `figure`, `table`, `longtable`,
`itemize`, `enumerate`, `equation`, `lstlisting` or `verbatim` is hidden, along
with headings, labels, captions and comments. Blocks under six words are
dropped as fragments.

Accepted text is re-flowed to the column the file is already wrapped at,
detected per file, so the source stays consistent and each diff shows only the
words that changed. A paragraph containing an explicit `\\` line break is left
exactly as written.

## Word counts

Three numbers, measuring different things:

- **Per column**, live: this paragraph with LaTeX stripped. Citations count as
  zero, `\textbf{word}` as one, `$x$` as one.
- **Estimate / limit** in the header: the same counter over every file.
- **texcount**: the authoritative figure, if `texcount` is installed. Click to
  recount. Trust this one against a limit.

## Writing rules are optional

By default **no style guide is sent**. The reviewer still checks meaning,
grammar, LaTeX and machine-sounding phrasing — those are built in, and the
spelling convention is inferred from your original rather than imposed.

To have it enforce your own conventions, use the **Style** picker in the header.
It lists Markdown files beside the manuscript, in its parent, and any project
skill files. A file named `STYLE.md` beside the manuscript is picked up
automatically. The file is read per request, so editing it changes the reviewer
with no restart.

Nothing is bundled: writing rules are one author's voice, and shipping a
particular set would impose them on everyone.

## Review modes

Every review needs the same rules — the checks, the JSON schema, your style
guide if you chose one. That text is most of what a review costs. The modes
differ only in how often it is re-sent. Click **Mode** in the header.

| Mode | What it does | Per review |
|---|---|---:|
| Fresh | Every review is its own process carrying the full rules | $0.0655 |
| **Hybrid** (default) | Sends the rules, resumes for a few reviews, starts over | **$0.0437** |
| One long session | Sends the rules once and keeps resuming | $0.0390 |

Measured over 24 paragraphs damaged identically — citations stripped, bold and
italic unwrapped, closing sentence cut — and reviewed by all three:

| | Fresh | Hybrid (4) | Session (24) |
|---|---:|---:|---:|
| Cost per review | $0.0655 | $0.0437 | $0.0390 |
| 250-paragraph pass | ~$16.40 | **~$10.90** | ~$9.80 |
| Mean time | 19s | 15s | 12s |
| Verdict agreement with Fresh | — | 24/24 | 24/24 |
| Items detected identically | — | 21/24 | 21/24 |
| High-severity total | 72 | 67 | 66 |

**Hybrid is the default.** Over a full cycle it costs about what a long session
costs, holds a flat cost per review instead of creeping upward, and re-reads the
rules six times more often.

The high-severity gap is not a detection failure. On 21 of 24 paragraphs all
three modes found the same items, and one paragraph accounts for most of the
difference: every mode found its 9 lost items, but Fresh graded 9 of them high
where the session modes graded 5 high and moved the rest into the LaTeX
category. The session modes reported *more* items overall (180 against 159).

Cost per review climbs inside a long session — 62% higher by the 24th review
than the first few, as cached context grows from 5,800 to 48,000 tokens — where
Hybrid stays within 24%. The turn count is adjustable per mode.

Any change of model, style guide, mode or turn count retires the current
conversation, so the reviewer is never working from rules you have since
changed. A failed review retires it too.

Reproduce with:

```sh
python compare_modes.py /path/to/manuscript --paragraphs 24
```

> **Benchmarking note.** Identical prompts hit the cache even across separate
> `claude -p` calls, so re-running the same comparison makes Fresh look ~45%
> cheaper than it is. Real passes never repeat a prompt. The figures above are
> corrected for that.

## What is actually sent

One stateless call per review carrying the original paragraph, your rewrite, the
instructions, the JSON schema, and your style guide if you selected one. No
conversation history in Fresh mode, no other paragraphs, no chapter, no
repository. Roughly:

| | Tokens |
|---|---:|
| Session scaffolding | ~1,300 |
| Style guide (optional) | ~2,000 |
| Instructions + JSON schema | ~1,600 |
| The two paragraphs | ~250 |
| Output | ~1,200 |

The paragraphs are a rounding error — you pay for the rules, not the text.

The reviewer runs with `--tools ""`, so it has no file access at all. Note that
`--allowedTools ""` alone is not enough: it withholds permission but still sends
every tool schema, about 27,900 tokens per call. Together with
`--setting-sources ""`, `--disable-slash-commands` and an empty MCP config, the
per-call floor drops from $0.090 to $0.001.

## Safety

If the manuscript is in a git repository, each accepted paragraph becomes its
own commit, so any single change can be reverted alone:

```sh
git log --oneline --grep '^rewrite:'
git revert <sha>
```

Commits use an explicit pathspec, so only the file you edited is committed and
other modified files are left alone. **If that file already had uncommitted
changes of your own, the paragraph is written but not committed**, because
committing would sweep your unrelated edits into a "rewrite:" commit. Commit or
stash them and later paragraphs commit cleanly.

Outside a repository, auto-commit turns itself off.

`audit.jsonl` records every review, accept, skip and reopen, including the full
original and replacement text. Paragraphs are identified by a hash of their own
text, so progress survives edits elsewhere in the file. Line endings are
preserved byte for byte.

## Files

| File | Role |
|---|---|
| `launcher.py` | Entry point: finds the manuscript, opens the browser |
| `app.py` | Server, queue, writes, git commits |
| `texparse.py` | Paragraph extraction, LaTeX-aware word counting |
| `reviewer.py` | Headless `claude -p` bridge, review prompt, sessions |
| `index.html` | The whole frontend |
| `build.py` | Builds the standalone binary |
| `compare_modes.py` | Reproduces the review-mode comparison |

Point it at any manuscript with the `REWRITE_MANUSCRIPT`, `REWRITE_REPO` and
`REWRITE_DATA` environment variables, or just pass the folder on the command
line to `compare_modes.py`.

## Name

*Vartika* (वर्तिका) — a wick, or a brush.
