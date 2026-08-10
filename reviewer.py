"""Bridge to a headless Claude Code session that reviews one rewritten paragraph.

The reviewer never edits the manuscript. It returns a verdict, the specific
things the rewrite dropped or invented, grammar problems, a judgement on whether
the new text reads as machine-written, and a corrected variant the author may
take instead of their own. Choosing what gets written stays with the author.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

# Replaces Claude Code's default agent system prompt. Reviewing two paragraphs of
# prose needs none of the coding scaffolding, and dropping it cuts the per-review
# cost by roughly a factor of ten.
SYSTEM_PROMPT = (
    "You are a meticulous editorial reviewer for a long-form document written in "
    "LaTeX. You check one paragraph at a time against the version it replaces. "
    "You are precise about facts, numbers, hedges and citations, and you never "
    "soften a finding to be agreeable. You reply with a single JSON object and "
    "nothing else."
)

REVIEW_SCHEMA = """{
  "verdict": "pass" | "warn" | "fail",
  "summary": "one sentence, plain, on whether the rewrite is safe to use",
  "lost": [ {"item": "the fact, number, citation or qualifier that is missing or altered",
             "severity": "high" | "low",
             "where": "the phrase in the ORIGINAL that carried it"} ],
  "added": [ {"item": "a claim in the REWRITE that the original does not support",
              "severity": "high" | "low"} ],
  "grammar": [ {"quote": "the exact wrong phrase from the REWRITE",
                "issue": "what is wrong",
                "fix": "the corrected phrase"} ],
  "voice": {"sounds_ai": true | false,
            "notes": ["the specific phrase or habit that reads as machine-written"]},
  "latex": ["any broken or missing LaTeX: dropped \\\\citep, unbalanced braces, bad escapes"],
  "corrected": "the full corrected LaTeX paragraph",
  "corrected_changes": ["one short line per change made in `corrected`"]
}"""

STYLE_BLOCK = """
The author's own writing conventions, which the rewrite is expected to follow:
<style-guide>
{style_guide}
</style-guide>
"""

# Everything above the paragraphs is identical on every review, so it forms a
# cacheable prefix. The two paragraphs are the only part that changes and are
# therefore last: anything placed after them would be re-billed every time.
PROMPT = """You are reviewing one paragraph of a document that the author is \
rewriting in their own words. You are a checker, not a co-author.

The ORIGINAL paragraph and the author's REWRITE are at the end of this message. \
Read these rules first, then apply them to those two paragraphs.
{style_block}
Common tells that a sentence was written by a language model rather than by this \
author. Flag these in `voice.notes` when they appear in the REWRITE:
- em-dashes and semicolons used to weld two sentences together
- "delve", "leverage", "underscore", "crucial", "pivotal", "robust", "seamless",
  "landscape", "realm", "tapestry", "testament to", "it is important to note",
  "plays a vital role", "not only ... but also"
- tricolon padding: three adjectives or three noun phrases where one would do
- an opening clause that restates the sentence before it
- hedging stacked on hedging ("may potentially", "could possibly suggest")
- a closing sentence that summarises what was just said without adding anything

Check, in this order:

1. Meaning. Does the REWRITE preserve every fact, number, unit, statistical value, \
citation key, hedge and qualifier in the ORIGINAL? A dropped hedge is a real loss: \
"suggests" becoming "shows" is a `high` severity change, not a style choice. So is a \
lost \\citep, a changed number, or a claim that got stronger.
2. Invention. Does the REWRITE assert anything the ORIGINAL does not?
3. Grammar and spelling in the REWRITE. Infer the spelling convention from the \
ORIGINAL rather than imposing one, and flag only departures from it.
4. LaTeX. Two separate things, and both matter:
   (a) Validity of the REWRITE: balanced braces, correctly escaped %, &, _, #, and \
intact command syntax.
   (b) Markup carried over from the ORIGINAL. Every one of these is meaning, not \
decoration, and dropping one is a finding:
       - \\citep, \\citet, \\citealp, \\ref, \\label, \\eqref
       - \\textbf, \\textit, \\emph, \\texttt, \\textsc
       - inline maths $...$, and units or symbols such as \\textdegree, \\%, \\times
       - non-breaking ties (Figure~\\ref{...}, 3.3~ms)
   If the ORIGINAL bolded a term and the REWRITE says it plainly, that is a lost \
item and `corrected` must put the markup back. Emphasis often marks a term being \
defined, or a parallel structure running across neighbouring paragraphs, so restore \
it in the same place it occupied before.
5. Voice, against the tells above{style_ref}.

Verdict rules, applied strictly:
- "fail" if anything in `lost` or `added` is `high` severity, or LaTeX is broken.
- "warn" if only `low` severity losses, grammar problems, or AI-sounding phrasing.
- "pass" if nothing of substance changed and the prose is clean.

`corrected` rules. This is the author's own sentences with faults repaired, NOT your \
rewrite of them:
- Keep the author's wording, sentence order and voice. Do not restyle.
- Fix grammar, spelling and LaTeX errors.
- Restore every piece of LaTeX markup listed in check 4(b) that the rewrite dropped, \
wrapping the author's own words rather than the original's.
- Restore any `high` severity lost meaning using the author's own vocabulary, as briefly \
as possible.
- Do not add anything else. If nothing needs fixing, set `corrected` to the REWRITE \
verbatim and leave `corrected_changes` empty.

Return one JSON object and nothing else. No prose before or after, no markdown fence.

{schema}

Now apply all of the above to these two paragraphs.

ORIGINAL (currently in the manuscript, file {relpath}):
<original>
{original}
</original>

REWRITE (the author's replacement):
<rewrite>
{rewrite}
</rewrite>"""


# Sent on every turn after the first in session mode. The rules are already in
# the conversation, so only the paragraphs travel.
FOLLOWUP_PROMPT = """Next paragraph, from {relpath}. Apply exactly the same rules \
and return exactly the same JSON object, nothing else.

Judge this pair on its own. Earlier paragraphs in this conversation are not \
context for it, and your earlier verdicts do not constrain this one.

ORIGINAL (currently in the manuscript):
<original>
{original}
</original>

REWRITE (the author's replacement):
<rewrite>
{rewrite}
</rewrite>"""


class ReviewError(RuntimeError):
    pass


@dataclass
class ReviewSession:
    """A resumable Claude Code conversation primed with the review rules.

    The first review sends the full instructions and records the session id.
    Later reviews resume it and send only the paragraphs, so the rules are read
    from cache instead of re-sent. History grows with every turn, so the session
    is retired after `max_turns` and the next review primes a fresh one.
    """

    session_id: str | None = None
    turns: int = 0
    max_turns: int = 30
    style_fingerprint: str | None = None

    def usable_for(self, fingerprint: str) -> bool:
        """Whether this session can serve a review with these settings.

        A changed style guide or model means the primed rules no longer match
        what the caller is asking for, so the session has to be rebuilt.
        """
        return (
            self.session_id is not None
            and self.turns < self.max_turns
            and self.style_fingerprint == fingerprint
        )

    def reset(self) -> None:
        self.session_id = None
        self.turns = 0
        self.style_fingerprint = None


def _claude_executable() -> str:
    for name in ("claude", "claude.cmd", "claude.exe"):
        found = shutil.which(name)
        if found:
            return found
    raise ReviewError(
        "The `claude` CLI was not found on PATH. The reviewer runs a headless "
        "Claude Code session, so the CLI must be installed and logged in."
    )


def _extract_json(text: str) -> dict:
    """Pull the JSON object out of a model response that may be fenced or padded."""
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if fenced:
        text = fenced.group(1)
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ReviewError(f"The reviewer did not return JSON. It said:\n{text[:800]}")
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError as exc:
        raise ReviewError(
            f"The reviewer returned malformed JSON ({exc}). It said:\n{text[:800]}"
        ) from exc


def build_command(model: str, resume: str | None = None) -> list[str]:
    """The CLI invocation, including the flags that keep a review cheap.

    These flags must be repeated on a resume. `--resume` does not inherit them
    from the call that created the session: without them the resumed turn
    reloads every tool schema, MCP server and setting, which measured three
    times the cost of not using a session at all.
    """
    command = [_claude_executable(), "-p"]
    if resume:
        command += ["--resume", resume]
    command += [
        "--output-format", "json",
        "--model", model,
        "--system-prompt", SYSTEM_PROMPT,
        "--tools", "",
        "--allowedTools", "",
        "--setting-sources", "",
        "--disable-slash-commands",
        "--strict-mcp-config",
        "--mcp-config", '{"mcpServers":{}}',
    ]
    return command


def settings_fingerprint(model: str, style_guide: str) -> str:
    """Identifies the rules a session was primed with."""
    digest = hashlib.sha1(f"{model}\x00{style_guide}".encode("utf-8")).hexdigest()
    return digest[:16]


def build_followup(original: str, rewrite: str, relpath: str) -> str:
    filled = FOLLOWUP_PROMPT
    for name, value in (
        ("original", original.strip()),
        ("rewrite", rewrite.strip()),
        ("relpath", relpath),
    ):
        filled = filled.replace("{" + name + "}", value)
    return filled


def build_prompt(original: str, rewrite: str, relpath: str, style_guide: str) -> str:
    """Fill the template by plain substitution.

    str.format cannot be used here. The template, the style guide and both
    paragraphs are full of LaTeX braces, and format reads every one of them as a
    replacement field.
    """
    # No style guide means no rules section at all, rather than an empty one.
    # An empty <style-guide> block reads as "the author has no conventions",
    # which is a different instruction from not mentioning conventions.
    guide = style_guide.strip()
    style_block = STYLE_BLOCK.replace("{style_guide}", guide) if guide else ""

    filled = PROMPT
    for name, value in (
        ("style_block", style_block),
        ("style_ref", " and the style guide" if guide else ""),
        ("original", original.strip()),
        ("rewrite", rewrite.strip()),
        ("relpath", relpath),
        ("schema", REVIEW_SCHEMA),
    ):
        filled = filled.replace("{" + name + "}", value)
    return filled


def review(
    original: str,
    rewrite: str,
    relpath: str,
    style_guide: str,
    model: str = "sonnet",
    timeout: int = 240,
    cwd: Path | None = None,
    session: ReviewSession | None = None,
) -> dict:
    """Run one headless review. Returns the parsed verdict object.

    With no `session`, every review is an independent call carrying the full
    rules. With one, the rules are sent once and later reviews resume that
    conversation, so the rules come back from cache instead of being re-sent.
    """
    fingerprint = settings_fingerprint(model, style_guide)
    resuming = session is not None and session.usable_for(fingerprint)

    if resuming:
        prompt = build_followup(original, rewrite, relpath)
        command = build_command(model, resume=session.session_id)
    else:
        prompt = build_prompt(original, rewrite, relpath, style_guide)
        command = build_command(model)
        if session is not None:
            session.reset()

    try:
        completed = subprocess.run(
            command,
            input=prompt,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            cwd=str(cwd) if cwd else None,
        )
    except subprocess.TimeoutExpired as exc:
        raise ReviewError(f"The reviewer timed out after {timeout}s.") from exc

    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise ReviewError(f"claude exited {completed.returncode}: {detail[:800]}")

    raw = (completed.stdout or "").strip()
    if not raw:
        raise ReviewError("claude produced no output.")

    # `--output-format json` wraps the answer; older builds print it bare.
    meta: dict = {}
    try:
        envelope = json.loads(raw)
    except json.JSONDecodeError:
        payload = raw
    else:
        if isinstance(envelope, dict) and envelope.get("is_error"):
            raise ReviewError(f"claude reported an error: {envelope.get('result')}")
        if isinstance(envelope, dict):
            payload = envelope.get("result", raw)
            usage = envelope.get("usage") or {}
            meta = {
                "cost_usd": envelope.get("total_cost_usd") or 0.0,
                "duration_ms": envelope.get("duration_api_ms") or 0,
                "model": model,
                "input_tokens": usage.get("input_tokens", 0),
                "output_tokens": usage.get("output_tokens", 0),
                # Cache reads are the bulk of the input and are billed at a
                # tenth of the rate, so they are reported separately rather
                # than folded into the input figure.
                "cache_read_tokens": usage.get("cache_read_input_tokens", 0),
                "cache_write_tokens": usage.get("cache_creation_input_tokens", 0),
                "session": "resumed" if resuming else ("primed" if session else "single"),
                "turn": (session.turns + 1) if session else 1,
            }
            if session is not None:
                new_id = envelope.get("session_id")
                if resuming:
                    session.turns += 1
                    # A resume can be served under a fresh id; follow it so the
                    # next turn keeps the same conversation.
                    if new_id:
                        session.session_id = new_id
                elif new_id:
                    session.session_id = new_id
                    session.turns = 1
                    session.style_fingerprint = fingerprint
        else:
            payload = raw

    result = _extract_json(payload)
    normalised = normalise_result(result, fallback=rewrite)
    normalised["meta"] = meta
    return normalised


def normalise_result(result: dict, fallback: str) -> dict:
    """Fill in anything the model left out so the frontend can trust the shape."""
    verdict = str(result.get("verdict", "warn")).lower()
    if verdict not in {"pass", "warn", "fail"}:
        verdict = "warn"

    def entries(name: str) -> list:
        value = result.get(name) or []
        if isinstance(value, str):
            value = [value]
        cleaned = []
        for item in value:
            cleaned.append({"item": item} if isinstance(item, str) else dict(item))
        return cleaned

    voice = result.get("voice") or {}
    if isinstance(voice, str):
        voice = {"sounds_ai": False, "notes": [voice]}
    notes = voice.get("notes") or []
    if isinstance(notes, str):
        notes = [notes]

    latex = result.get("latex") or []
    if isinstance(latex, str):
        latex = [latex]

    changes = result.get("corrected_changes") or []
    if isinstance(changes, str):
        changes = [changes]

    corrected = result.get("corrected") or fallback

    # A model that lists a high-severity loss but says "pass" is contradicting
    # itself. Trust the findings over the label.
    lost, added = entries("lost"), entries("added")
    severe = any(e.get("severity") == "high" for e in lost + added) or bool(latex)
    if severe:
        verdict = "fail"
    elif verdict == "pass" and (lost or added or entries("grammar") or voice.get("sounds_ai")):
        verdict = "warn"

    return {
        "verdict": verdict,
        "summary": result.get("summary", ""),
        "lost": lost,
        "added": added,
        "grammar": entries("grammar"),
        "voice": {"sounds_ai": bool(voice.get("sounds_ai")), "notes": notes},
        "latex": latex,
        "corrected": corrected,
        "corrected_changes": changes,
    }
