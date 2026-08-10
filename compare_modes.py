"""Compare review modes on identical input.

Three ways to get the rules to the reviewer:

  fresh    every review carries the full rules. Independent, most expensive.
  session  rules sent once, then the conversation is resumed. Cheapest, but the
           reviewer accumulates everything it has already seen.
  hybrid   a session that is retired and re-primed every few turns, so history
           stays short and the reviewer is re-anchored on the rules regularly.

Each paragraph is damaged the same way for every mode — citations stripped,
bold and italic unwrapped, the closing sentence cut — so all three see the same
known faults and can be compared verdict by verdict.

    python compare_modes.py /path/to/manuscript --paragraphs 24
    python compare_modes.py . --modes fresh,hybrid --style STYLE.md
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import time
from pathlib import Path

import app
import reviewer
import texparse

HERE = Path(__file__).resolve().parent

CITE_RE = re.compile(r"\s*\\cite[a-z]*\s*(?:\[[^\]]*\])?\{[^}]*\}")
MARKUP_RE = re.compile(r"\\(?:textbf|textit|emph|texttt)\{([^{}]*)\}")


def damage(text: str) -> tuple[str, list[str]]:
    """Introduce known, detectable faults. Deterministic, so all modes match."""
    faults: list[str] = []
    out = text

    if CITE_RE.search(out):
        faults.append("citation dropped")
        out = CITE_RE.sub("", out)

    if MARKUP_RE.search(out):
        faults.append("markup unwrapped")
        out = MARKUP_RE.sub(r"\1", out)

    sentences = re.split(r"(?<=[.?!])\s+", out.strip())
    if len(sentences) > 2:
        faults.append("final sentence cut")
        out = " ".join(sentences[:-1])

    return " ".join(out.split()), faults


def collect(manuscript: Path, limit: int) -> list[tuple[str, object, str]]:
    """Paragraphs long enough to damage in at least two ways, in document order."""
    cases: list[tuple[str, object, str]] = []
    for relpath, _title in app.discover_files(manuscript):
        path = manuscript / relpath
        if not path.exists():
            continue
        for block in texparse.prose_blocks(path.read_text(encoding="utf-8", errors="replace")):
            if block.words < 60:
                continue
            broken, faults = damage(block.text)
            if len(faults) >= 2 and broken != block.text:
                cases.append((relpath, block, broken))
            if len(cases) >= limit:
                return cases
    return cases


def make_session(mode: str, hybrid_turns: int) -> reviewer.ReviewSession | None:
    if mode == "fresh":
        return None
    if mode == "hybrid":
        return reviewer.ReviewSession(max_turns=hybrid_turns)
    return reviewer.ReviewSession(max_turns=10_000)


def run_mode(mode: str, cases: list, style_guide: str, hybrid_turns: int,
             cwd: Path) -> list[dict]:
    session = make_session(mode, hybrid_turns)
    rows: list[dict] = []
    print(f"\n--- {mode} ---", flush=True)
    print(f"{'#':>3} {'ev':<8}{'t':>3} {'verdict':<7} {'lost':>4} {'hi':>3} {'ltx':>4} "
          f"{'write':>6} {'read':>7} {'out':>5} {'cost':>8} {'s':>4}", flush=True)
    for index, (relpath, block, broken) in enumerate(cases, 1):
        started = time.monotonic()
        try:
            result = reviewer.review(
                original=block.text, rewrite=broken, relpath=relpath,
                style_guide=style_guide, cwd=cwd, session=session,
            )
        except reviewer.ReviewError as exc:
            print(f"{index:>3} FAILED: {exc}", flush=True)
            continue
        meta = result.get("meta", {})
        row = {
            "n": index,
            "mode": mode,
            "event": meta.get("session", "single"),
            "turn": meta.get("turn", 1),
            "verdict": result["verdict"],
            "lost": len(result["lost"]),
            "lost_high": sum(1 for i in result["lost"] if i.get("severity") == "high"),
            "latex": len(result["latex"]),
            "grammar": len(result["grammar"]),
            "findings": len(result["lost"]) + len(result["latex"]) + len(result["grammar"]),
            "cost": meta.get("cost_usd", 0.0),
            "write": meta.get("cache_write_tokens", 0),
            "read": meta.get("cache_read_tokens", 0),
            "output": meta.get("output_tokens", 0),
            "elapsed": time.monotonic() - started,
        }
        rows.append(row)
        print(f"{index:>3} {row['event']:<8}{row['turn']:>3} {row['verdict']:<7} "
              f"{row['lost']:>4} {row['lost_high']:>3} {row['latex']:>4} {row['write']:>6} "
              f"{row['read']:>7} {row['output']:>5} ${row['cost']:>7.4f} {row['elapsed']:>4.0f}",
              flush=True)
    return rows


def halves(rows: list[dict], field: str) -> tuple[float, float]:
    """Mean of a field over the first and second half — the drift signal."""
    if len(rows) < 4:
        return (0.0, 0.0)
    middle = len(rows) // 2
    first = statistics.mean(r[field] for r in rows[:middle])
    second = statistics.mean(r[field] for r in rows[middle:])
    return (first, second)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manuscript", nargs="?", default=".",
                        help="folder holding the .tex files (default: current directory)")
    parser.add_argument("--paragraphs", type=int, default=24)
    parser.add_argument("--modes", default="fresh,session,hybrid")
    parser.add_argument("--hybrid-turns", type=int, default=4)
    parser.add_argument("--style", default="",
                        help="path to a style guide to send with every review")
    parser.add_argument("--out", default="compare-results.json")
    args = parser.parse_args()

    manuscript = Path(args.manuscript).resolve()
    if not manuscript.is_dir():
        print(f"{manuscript} is not a folder.")
        return 1

    style_guide = ""
    if args.style:
        style_path = Path(args.style)
        if not style_path.exists():
            print(f"{style_path} does not exist.")
            return 1
        style_guide = style_path.read_text(encoding="utf-8", errors="replace")

    cases = collect(manuscript, args.paragraphs)
    if not cases:
        print(f"No paragraphs in {manuscript} were long enough to damage two ways.")
        return 1
    modes = [m.strip() for m in args.modes.split(",") if m.strip()]
    print(f"{manuscript}", flush=True)
    print(f"{len(cases)} paragraphs, modes: {', '.join(modes)}, "
          f"hybrid re-primes every {args.hybrid_turns} turns, "
          f"style guide: {args.style or 'none'}", flush=True)

    cwd = app.git_root_for(manuscript)
    results = {mode: run_mode(mode, cases, style_guide, args.hybrid_turns, cwd)
               for mode in modes}
    Path(args.out).write_text(json.dumps(results, indent=1), encoding="utf-8")

    print("\n" + "=" * 78, flush=True)
    print(f"{'mode':<9}{'n':>3} {'total':>9} {'mean':>8} {'s':>4} "
          f"{'find/rev':>9} {'high':>5} {'out 1st':>8} {'out 2nd':>8}", flush=True)
    for mode, rows in results.items():
        if not rows:
            continue
        n = len(rows)
        out_a, out_b = halves(rows, "output")
        print(f"{mode:<9}{n:>3} ${sum(r['cost'] for r in rows):>8.4f} "
              f"${sum(r['cost'] for r in rows)/n:>7.4f} "
              f"{sum(r['elapsed'] for r in rows)/n:>4.0f} "
              f"{sum(r['findings'] for r in rows)/n:>9.2f} "
              f"{sum(r['lost_high'] for r in rows):>5} "
              f"{out_a:>8.0f} {out_b:>8.0f}", flush=True)

    base = results.get("fresh")
    if base:
        print("\nagainst fresh:", flush=True)
        for mode, rows in results.items():
            if mode == "fresh" or not rows:
                continue
            pairs = [(a, b) for a, b in zip(base, rows) if a["n"] == b["n"]]
            agree = sum(1 for a, b in pairs if a["verdict"] == b["verdict"])
            missed = sum(max(0, a["lost_high"] - b["lost_high"]) for a, b in pairs)
            extra = sum(max(0, b["lost_high"] - a["lost_high"]) for a, b in pairs)
            saving = 1 - sum(r["cost"] for r in rows) / sum(r["cost"] for r in base)
            print(f"  {mode:<8} verdicts {agree}/{len(pairs)}  "
                  f"high-severity missed {missed}, extra {extra}  "
                  f"cost {saving * 100:+.0f}%", flush=True)
    print(f"\nraw rows written to {args.out}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
