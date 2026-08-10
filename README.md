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
  or `xattr -d com.apple.quarantine ./Vartika-macos-*` once. Apple-silicon only;
  Intel Macs need to run from source.
- **Linux**: `chmod +x Vartika-linux` if the download drops the flag.

The URL it opens carries a one-time token, and the server refuses API calls
without it. Open the link from the console rather than typing the address, or
the page will not be able to talk to the server.

From source instead:

```sh
pip install -r requirements.txt
python launcher.py            # finds the manuscript, opens a browser
python app.py                 # server only, port 8000, no browser
python build.py               # build the standalone binary (needs pyinstaller)
```

## Which manuscript it opens

A manuscript folder is one containing a `.tex` file with `\documentclass` in it.

**Choosing one happens in the app**, on a first page that lists every manuscript
it found near the binary with its file count and when it was last edited, and a
**Choose a folder…** button that opens your usual folder dialog. A path can be
pasted instead.

The dialogs are opened by the server, which runs on your own machine — a browser
will not reveal where a chosen file lives, but this program can ask the operating
system directly.

It only skips that page when the answer is unambiguous: a single manuscript
sitting beside the executable, or one you named explicitly. It will not choose
between a working copy, a snapshot and a template. Click the manuscript name in
the header to switch at any time, or start with `--pick`.

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
| **Accept as written** (`Ctrl S`) | Write your text. No review needed. |
| **Review** (`Ctrl ⏎`) | Check it first. Runs in the background. |
| **Accept corrected** (`Ctrl ⇧ S`) | Write the reviewer's corrected variant instead. |
| **Skip** | Leave it and move on. |
| **Reopen** | Put a done or skipped paragraph back in the queue. |
| **Copy original** (`Ctrl K`) | Load the original into the editor as a starting point. |
| `Alt ←` / `Alt →` | Previous / next paragraph. |

**Reviewing is optional.** When you are happy with a paragraph, accept it and
move on — nothing is checked and nothing is changed. The reviewer only ever
interrupts you if you asked for a review and it came back `fail`, and even then
only to say what it thinks is being lost before you confirm.

**Reviews do not block you.** A review takes 10 to 30 seconds, so it runs in the
background. Start one, move to the next paragraph, and keep writing; the answer
lands against the paragraph it belongs to and waits there. The sidebar shows
which paragraphs have a review running (`◐`), one waiting to be read (`✓ pass`,
`✓ warn`, `✓ fail`, highlighted), and which have text you have typed but not yet
accepted (`✎`). Your drafts are held per paragraph, so moving away never loses
what you wrote.

Reviews are queued one at a time on the server. Several can be in flight from
your side, but they run in order, because the session modes resume a single
conversation and two overlapping resumes would interleave turns in it.

The queue holds prose only. Everything inside `figure`, `table`, `longtable`,
`itemize`, `enumerate`, `equation`, `lstlisting` or `verbatim` is hidden, along
with headings, labels, captions and comments. Blocks under six words are
dropped as fragments.

Accepted text is re-flowed to the column the file is already wrapped at,
detected per file, so the source stays consistent and each diff shows only the
words that changed. A paragraph containing an explicit `\\` line break is left
exactly as written.

## Seeing and undoing what changed

Accepting writes the paragraph into the `.tex` immediately. **Changes** in the
header opens everything this app has replaced, newest first, with the old and
new text side by side, the word delta, and the verdict it was accepted under.

From there you can **revert a single paragraph** to exactly what it was, leaving
the rest of the document alone, or **download a `.patch`** — a unified diff of
the untouched originals against the files as they stand, which you can read,
keep, or reverse with `patch -R`.

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

To have it enforce your own conventions, click **Style**, then **Choose a
file…**, and pick any markdown file in your usual file dialog. It is read fresh
on every review, so editing it changes the reviewer straight away. If the file
later moves, the header says so rather than quietly reverting to no rules.

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

Vartika edits your `.tex` files in place. Two things make that recoverable.

**An untouched copy of every file it changes.** The first time a file is
modified, it is copied to `.rewrite-progress/originals/` exactly as it was, and
never overwritten after that. However many paragraphs you go on to replace, that
copy is still the version from before Vartika touched anything.

**A full log of every change.** `audit.jsonl` records each review, accept, skip
and reopen, including the complete original and replacement text of every
paragraph. Any single paragraph can be put back by hand from it.

Both live in `.rewrite-progress/` beside your manuscript, which ignores itself in
git so it never appears in your history.

Vartika does not touch version control. If you keep your manuscript in git —
which is worth doing — commit before you start, and commit the results as you
like. Deciding what your history looks like is your business, not this tool's.

Paragraphs are identified by a hash of their own text, so progress survives
edits made elsewhere in the file. Line endings are preserved byte for byte, and
accepted text is re-flowed to the column the file already uses, so a diff shows
only the words that changed.

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
| `check_page.js` | Loads the page, runs its JavaScript, asserts the controls rendered |
| `make_icon.py` | Draws the application icon |

Point it at any manuscript with the `REWRITE_MANUSCRIPT`, `REWRITE_REPO` and
`REWRITE_DATA` environment variables, or just pass the folder on the command
line to `compare_modes.py`.

## Name

A *vārttika* (वार्त्तिक) is a note written against an existing rule — the form Kātyāyana used to annotate Pāṇini's grammar, examining each rule and
saying what it missed or overstated.

That is what this tool does to a manuscript: takes it a rule at a time, and
says plainly what a rewrite lost.
