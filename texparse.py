"""LaTeX paragraph extraction and word counting for the dissertation rewrite app.

The manuscript is plain LaTeX with blank-line-separated paragraphs. This module
splits a .tex file into blocks, keeps only the ones that are genuine prose, and
gives each one a content-derived identifier so a paragraph can still be found
after unrelated parts of the file have changed.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

# Environments whose contents are never offered for rewriting. Floats, tables,
# maths and code carry no prose the author would paraphrase.
EXCLUDED_ENVIRONMENTS = {
    "figure", "figure*", "table", "table*", "longtable", "tabular", "tabularx",
    "array", "itemize", "enumerate", "description", "equation", "equation*",
    "align", "align*", "eqnarray", "eqnarray*", "gather", "gather*",
    "lstlisting", "verbatim", "minted", "tikzpicture", "center", "picture",
    "thebibliography", "tabbing",
}

# Environments that wrap prose and must not hide it. Anything not listed in
# either set is treated as transparent.
PROSE_ENVIRONMENTS = {
    "document", "appendix", "thesisabstract", "thesisacknowledgments",
    "quote", "quotation", "abstract",
}

# A block starting with one of these is structural, not prose.
STRUCTURAL_PREFIXES = (
    r"\chapter", r"\section", r"\subsection", r"\subsubsection",
    r"\paragraph{", r"\subparagraph{", r"\label", r"\input", r"\include",
    r"\bibliography", r"\bibliographystyle", r"\addcontentsline",
    r"\phantomsection", r"\cleardoublepage", r"\clearpage", r"\newpage",
    r"\pagebreak", r"\caption", r"\includegraphics", r"\figplaceholder",
    r"\centering", r"\hline", r"\toprule", r"\midrule", r"\bottomrule",
    r"\vspace", r"\hspace", r"\setlength", r"\renewcommand", r"\newcommand",
    r"\usepackage", r"\documentclass", r"\hypersetup", r"\tableofcontents",
    r"\listoftables", r"\listoffigures", r"\begin{", r"\end{", r"\item",
    r"\maketitle", r"\thesistitlepage", r"\thesisdeclarationpage",
    r"\makeatletter", r"\makeatother", r"\mastersthesis", r"\leftchapter",
    r"\oneandhalfspace", r"\thesisdraft",
)

# Below this, a "paragraph" is a fragment (a stray label, a one-word line) and
# is not worth putting in the queue.
MIN_PROSE_WORDS = 6

_BEGIN_RE = re.compile(r"\\begin\s*\{([^}]*)\}")
_END_RE = re.compile(r"\\end\s*\{([^}]*)\}")
_DELIMITER_ONLY_RE = re.compile(r"^\s*\\(?:begin|end)\s*\{([^}]*)\}\s*$")


def _is_prose_delimiter(line: str) -> bool:
    """A lone \\begin{thesisabstract} or \\end{quote} on its own line.

    These wrap prose but sit flush against it with no blank line, so they must
    break a block the way a blank line does. Otherwise the first paragraph of
    the abstract reads as a structural block and never reaches the queue.
    """
    match = _DELIMITER_ONLY_RE.match(line)
    return bool(match) and match.group(1) in PROSE_ENVIRONMENTS


@dataclass
class Block:
    """One blank-line-delimited chunk of a .tex file."""

    index: int          # ordinal among prose blocks in the file
    raw_index: int      # ordinal among all blocks, prose or not
    start: int          # character offset of the first character
    end: int            # character offset just past the last character
    text: str
    is_prose: bool
    reason: str = ""    # why it was excluded, for debugging
    key: str = ""       # stable content-derived identifier
    words: int = 0
    envs: set = field(default_factory=set)


def _environment_context(lines: list[str]) -> list[set]:
    """For each line, the set of environments it sits inside.

    A line carrying \\begin{x} or \\end{x} counts as inside x, so the delimiters
    are excluded along with the body.
    """
    contexts: list[set] = []
    stack: list[str] = []
    for line in lines:
        opened = _BEGIN_RE.findall(line)
        closed = _END_RE.findall(line)
        contexts.append(set(stack) | set(opened) | set(closed))
        for name in opened:
            stack.append(name)
        for name in closed:
            for i in range(len(stack) - 1, -1, -1):
                if stack[i] == name:
                    del stack[i]
                    break
    return contexts


def _looks_like_table_row(text: str) -> bool:
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return False
    tabular = sum(1 for ln in lines if "&" in ln or ln.rstrip().endswith(r"\\"))
    return tabular >= max(1, len(lines) // 2)


def _classify(text: str, envs: set) -> tuple[bool, str]:
    stripped = text.strip()
    if not stripped:
        return False, "empty"

    blocking = (envs & EXCLUDED_ENVIRONMENTS) - PROSE_ENVIRONMENTS
    if blocking:
        return False, f"inside {sorted(blocking)[0]}"

    # A block of nothing but comments.
    if all(ln.strip().startswith("%") for ln in stripped.splitlines() if ln.strip()):
        return False, "comment"

    for prefix in STRUCTURAL_PREFIXES:
        if stripped.startswith(prefix):
            return False, "structural"

    if _looks_like_table_row(stripped):
        return False, "table row"

    words = count_words(stripped)
    if words < MIN_PROSE_WORDS:
        return False, f"too short ({words} words)"

    return True, ""


def normalise(text: str) -> str:
    """Whitespace-insensitive form, so re-wrapping a paragraph keeps its key."""
    return " ".join(text.split())


def content_key(text: str) -> str:
    return hashlib.sha1(normalise(text).encode("utf-8")).hexdigest()[:16]


def parse(source: str) -> list[Block]:
    """Split a .tex source into blocks, flagging which ones are prose."""
    lines = source.splitlines(keepends=True)
    contexts = _environment_context(lines)

    blocks: list[Block] = []
    offset = 0
    current: list[int] = []          # indices of lines in the block being built
    current_start = 0

    def flush() -> None:
        nonlocal current, current_start
        if not current:
            return
        first, last = current[0], current[-1]
        text = "".join(lines[first:last + 1]).rstrip("\r\n")
        end = current_start + len(text)
        envs: set = set()
        for i in current:
            envs |= contexts[i]
        is_prose, reason = _classify(text, envs)
        blocks.append(Block(
            index=-1, raw_index=len(blocks), start=current_start, end=end,
            text=text, is_prose=is_prose, reason=reason, envs=envs,
        ))
        current = []

    for i, line in enumerate(lines):
        if not line.strip() or _is_prose_delimiter(line):
            flush()
        else:
            if not current:
                current_start = offset
            current.append(i)
        offset += len(line)
    flush()

    # Number the prose blocks and give each a key that survives edits elsewhere
    # in the file. Identical paragraphs get an occurrence suffix.
    seen: dict[str, int] = {}
    prose_index = 0
    for block in blocks:
        if not block.is_prose:
            continue
        base = content_key(block.text)
        occurrence = seen.get(base, 0)
        seen[base] = occurrence + 1
        block.key = base if occurrence == 0 else f"{base}~{occurrence}"
        block.index = prose_index
        block.words = count_words(block.text)
        prose_index += 1

    return blocks


def prose_blocks(source: str) -> list[Block]:
    return [b for b in parse(source) if b.is_prose]


def dominant_newline(source: str) -> str:
    """The line ending the file already uses.

    Blocks are spliced back verbatim, so a replacement typed in a browser (which
    always yields \\n) must be converted to the file's own ending. Without this a
    single edit to a CRLF file would rewrite every line in it.
    """
    crlf = source.count("\r\n")
    lf = source.count("\n") - crlf
    return "\r\n" if crlf > lf else "\n"


def detect_wrap_width(source: str) -> int:
    """The column the file is hard-wrapped at, or 0 if it is not wrapped.

    Only the non-final lines of a paragraph reveal the wrap column, since the
    last line stops wherever the paragraph ends. Returns 0 when the answer looks
    implausible, so an unwrapped file is left unwrapped.
    """
    lengths: list[int] = []
    for block in prose_blocks(source):
        lines = block.text.splitlines()
        lengths.extend(len(line) for line in lines[:-1])
    if len(lengths) < 5:
        return 0
    lengths.sort()
    p90 = lengths[int(len(lengths) * 0.9)]
    return p90 if 60 <= p90 <= 120 else 0


def rewrap(text: str, width: int) -> str:
    """Re-flow a paragraph to the file's wrap column.

    Text typed into a browser arrives as one long line. Writing that into a
    manuscript hard-wrapped at 101 columns would leave the source inconsistent
    and make every diff unreadable, so it is re-flowed to match.
    """
    import textwrap

    if width <= 0 or "\\\\" in text:
        # A paragraph containing an explicit LaTeX line break was laid out
        # deliberately. Leave it exactly as written.
        return text

    chunks = re.split(r"\n\s*\n", text.strip())
    wrapped = []
    for chunk in chunks:
        flat = " ".join(chunk.split())
        if not flat:
            continue
        wrapped.append("\n".join(textwrap.wrap(
            flat, width=width, break_long_words=False, break_on_hyphens=False,
        )))
    return "\n\n".join(wrapped)


def splice(source: str, block: Block, replacement: str) -> str:
    """Return source with block's span replaced by replacement."""
    newline = dominant_newline(source)
    body = replacement.replace("\r\n", "\n").replace("\r", "\n").rstrip("\n")
    if newline != "\n":
        body = body.replace("\n", newline)
    return source[:block.start] + body + source[block.end:]


# --------------------------------------------------------------------------
# Word counting
# --------------------------------------------------------------------------

_COMMENT_RE = re.compile(r"(?<!\\)%.*")
_ACCENT_SYMBOL_RE = re.compile(r"""\\[`'"^~=.]\{?([A-Za-z])\}?""")
_ACCENT_LETTER_RE = re.compile(r"\\[a-zA-Z]\{([^{}]*)\}")
_ZERO_CONTENT_RE = re.compile(
    r"\\(?:cite[a-zA-Z]*|ref|autoref|eqref|pageref|label|nocite|includegraphics"
    r"|input|include|bibliography|bibliographystyle|addcontentsline|hypersetup"
    r"|vspace|hspace|setlength|index|footnotemark)\s*"
    r"(?:\[[^\]]*\])?\s*(?:\{[^{}]*\})*"
)
_DISPLAY_MATH_RE = re.compile(r"\$\$.*?\$\$", re.S)
_INLINE_MATH_RE = re.compile(r"\$[^$]*\$")
_COMMAND_RE = re.compile(r"\\[a-zA-Z@]+\*?\s*(?:\[[^\]]*\])?")
_ESCAPE_RE = re.compile(r"\\.")
_WORDISH_RE = re.compile(r"[A-Za-z0-9]")


def strip_tex(text: str) -> str:
    """Reduce LaTeX to the words a human word count would see."""
    s = _COMMENT_RE.sub("", text)
    s = _ACCENT_SYMBOL_RE.sub(r"\1", s)
    s = _ACCENT_LETTER_RE.sub(r"\1", s)
    s = _ZERO_CONTENT_RE.sub(" ", s)
    s = _DISPLAY_MATH_RE.sub(" formula ", s)
    s = _INLINE_MATH_RE.sub(" formula ", s)
    s = _COMMAND_RE.sub(" ", s)
    s = _ESCAPE_RE.sub("", s)
    s = s.replace("~", " ").replace("--", "-")
    s = re.sub(r"[{}]", " ", s)
    return s


def count_words(text: str) -> int:
    """Approximate word count. `texcount` remains authoritative for submission."""
    return sum(1 for token in strip_tex(text).split() if _WORDISH_RE.search(token))
