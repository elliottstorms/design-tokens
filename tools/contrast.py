#!/usr/bin/env python3
"""Measure every shipped color pair against its promised WCAG level.

Run it as a gate (exit 1 on any failure) or with --table to regenerate the
contrast table in the README. The values are parsed out of css/tokens.css
rather than duplicated here, so the CSS stays the single source of truth and a
renamed token fails loudly instead of being silently skipped.

    python3 tools/contrast.py                # gate: prints failures, exits 1
    python3 tools/contrast.py --table        # markdown table for the README
    python3 tools/contrast.py --all          # every pair, passing or not
    python3 tools/contrast.py --annotations  # gate: the ratio numbers written
                                             # into tokens.css comments must
                                             # equal the measured values

Stdlib only, no dependencies, Python 3.8+.
"""
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
TOKENS = ROOT / "css" / "tokens.css"
PAIRS = ROOT / "tools" / "pairs.json"

# --custom-prop: #rrggbb, ignoring anything inside a comment block.
TOKEN_RE = re.compile(r"--([a-z0-9-]+)\s*:\s*(#[0-9a-fA-F]{6})\s*;")

# A single token declaration and whatever trailing text follows it on the line,
# so an annotation is attributed to the token it sits beside rather than to some
# other mention of the same number elsewhere in the file.
DECL_RE = re.compile(r"--([a-z0-9-]+)\s*:\s*#[0-9a-fA-F]{6}\s*;(.*)$")
# "15.95 on ink" and "13.81 card" both count; the surface is one of the two the
# whole system is measured against.
ANNOTATION_RE = re.compile(r"(\d+\.\d+)\s+(?:on\s+)?(ink|card)\b")


def read_tokens(path=TOKENS):
    """Map token name to hex, from the CSS itself."""
    text = path.read_text(encoding="utf-8")
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)  # strip comments first
    return {name: value.lower() for name, value in TOKEN_RE.findall(text)}


def relative_luminance(hex_color):
    """WCAG 2.x relative luminance."""
    h = hex_color.lstrip("#")
    channels = []
    for i in (0, 2, 4):
        c = int(h[i:i + 2], 16) / 255
        channels.append(c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4)
    r, g, b = channels
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(fg, bg):
    a, b = relative_luminance(fg), relative_luminance(bg)
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)


def achieved(ratio):
    """The strongest level a ratio actually earns."""
    if ratio >= 7.0:
        return "AAA"
    if ratio >= 4.5:
        return "AA"
    if ratio >= 3.0:
        return "AA-large"
    return "FAIL"


def evaluate():
    tokens = read_tokens()
    spec = json.loads(PAIRS.read_text(encoding="utf-8"))
    levels = spec["_levels"]
    rows, missing = [], []

    for pair in spec["pairs"]:
        fg_name, bg_name = pair["fg"], pair["bg"]
        if fg_name not in tokens or bg_name not in tokens:
            missing.append((fg_name, bg_name))
            continue
        ratio = contrast_ratio(tokens[fg_name], tokens[bg_name])
        required = levels[pair["min"]]
        rows.append({
            "fg": fg_name, "bg": bg_name,
            "fg_hex": tokens[fg_name], "bg_hex": tokens[bg_name],
            "ratio": ratio, "required_name": pair["min"], "required": required,
            "achieved": achieved(ratio), "ok": ratio >= required,
            "use": pair.get("use", ""),
        })
    return rows, missing


def read_annotations(path=TOKENS):
    """Pull the ratio numbers written beside each token in tokens.css.

    Returns (token, surface, stated) triples, one per "N.NN on <surface>"
    annotation. A token's comment may run onto the next line (--steel does), so
    a declaration's comment is followed until it closes and continuation lines
    are attributed to that token. A ratio in a section header (a standalone
    comment that names its token in prose rather than declaring it) sits under
    no open declaration and is deliberately left alone, so it is never
    double-counted against the token line.
    """
    found = []
    current = None  # token whose comment is still open across lines
    for line in path.read_text(encoding="utf-8").splitlines():
        decl = DECL_RE.search(line)
        if decl:
            current, text = decl.group(1), decl.group(2)
        elif current is not None:
            text = line
        else:
            continue
        for stated, surface in ANNOTATION_RE.findall(text):
            found.append((current, surface, float(stated)))
        # The comment closes on this line, or (for a bare declaration with no
        # trailing "/*") never opened; either way the token stops collecting.
        if "*/" in text or (decl and "/*" not in text):
            current = None
    return found


def check_annotations():
    """The ratio numbers documented in tokens.css must match the measured ones.

    The pair gate proves a color still clears its promised level. It does not
    prove the specific ratio a comment claims is still true: a hex can move,
    stay above its threshold, and leave "15.95 on ink" quietly wrong. This
    closes that gap so the annotations cannot become confident fiction.

    Returns (checked, mismatches). A mismatch is
    (token, surface, stated, measured).
    """
    tokens = read_tokens()
    mismatches = []
    annotations = read_annotations()
    for name, surface, stated in annotations:
        if name not in tokens or surface not in tokens:
            mismatches.append((name, surface, stated, None))
            continue
        measured = contrast_ratio(tokens[name], tokens[surface])
        # Comments are rounded to two decimals; compare at the same precision
        # rather than demanding the comment carry the full float.
        if f"{measured:.2f}" != f"{stated:.2f}":
            mismatches.append((name, surface, stated, measured))
    return annotations, mismatches


def main():
    args = sys.argv[1:]

    if "--annotations" in args:
        annotations, mismatches = check_annotations()
        for name, surface, stated, measured in mismatches:
            if measured is None:
                print(f"FAIL --{name} on --{surface}: annotation references an "
                      f"unknown token", file=sys.stderr)
            else:
                print(f"FAIL --{name} on --{surface}: comment says {stated:.2f}, "
                      f"measures {measured:.2f}", file=sys.stderr)
        if mismatches:
            print(f"\n{len(mismatches)} of {len(annotations)} tokens.css "
                  f"annotations disagree with the measured ratio.",
                  file=sys.stderr)
            return 1
        print(f"all {len(annotations)} tokens.css ratio annotations match the "
              f"measured values")
        return 0

    rows, missing = evaluate()

    # A pair naming a token that no longer exists is a failure, not a skip.
    # Silently ignoring it is how a palette loses its guarantees one rename at
    # a time.
    if missing:
        for fg, bg in missing:
            print(f"ERROR: pairs.json references unknown token(s): --{fg} / --{bg}",
                  file=sys.stderr)
        return 1

    if "--table" in args:
        print("| Foreground | Background | Ratio | Required | Achieved |")
        print("|---|---|---:|---|---|")
        for r in rows:
            print(f"| `--{r['fg']}` `{r['fg_hex']}` | `--{r['bg']}` `{r['bg_hex']}` "
                  f"| {r['ratio']:.2f} | {r['required_name']} | {r['achieved']} |")
        return 0

    failures = [r for r in rows if not r["ok"]]
    show = rows if "--all" in args else failures

    for r in show:
        mark = "ok  " if r["ok"] else "FAIL"
        print(f"{mark} --{r['fg']} on --{r['bg']}: {r['ratio']:.2f} "
              f"(needs {r['required']:.1f} for {r['required_name']}, gets {r['achieved']})")

    if failures:
        print(f"\n{len(failures)} of {len(rows)} pairs below their promised level.",
              file=sys.stderr)
        return 1

    print(f"all {len(rows)} pairs meet their promised contrast level")
    return 0


if __name__ == "__main__":
    sys.exit(main())
