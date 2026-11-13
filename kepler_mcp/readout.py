"""Readbacks: the text a client prints after an event. One form per kind of event; plain text, at most 66 columns, no science.

    line        a state changed, or a fact was looked up
    card        a short identity block
    checklist   work was done in stages
    test_table  tests were applied
    table       several things are compared
    bar         something is partly done
    block       something rare and important happened
"""
WIDTH = 66
DOT = " · "


def fit(text, width=WIDTH):
    """Clip a line to the width, marking the cut."""
    text = str(text)
    return text if len(text) <= width else text[:width - 1] + "…"


def num(x, digits=0):
    """1234.5 -> '1,234' ; None -> '--'."""
    if x is None:
        return "--"
    try:
        return f"{float(x):,.{digits}f}"
    except (TypeError, ValueError):
        return str(x)


def join(*parts):
    """Join the non-empty parts with a middle dot."""
    return DOT.join(str(p) for p in parts if p not in (None, ""))


def line(label, *parts, label_width=11):
    """LABEL      part . part . part"""
    return fit(f"{label:<{label_width}} {join(*parts)}".rstrip())


def card(*lines):
    return "\n".join(fit(x) for x in lines if x)


def header(title, right=""):
    """Title on the left, a short right-aligned note (elapsed time) on the right."""
    right = str(right)
    room = WIDTH - len(right) - (2 if right else 0)
    return f"{fit(title, room):<{room}}" + (f"  {right}" if right else "")


def secs(s):
    return "" if s is None else (f"{s:.1f} s" if s < 60 else f"{int(s) // 60} min {int(s) % 60:02d} s")


def clock(elapsed_s):
    e = int(max(0, elapsed_s or 0))
    return f"T+{e // 60:02d}:{e % 60:02d}"


def checklist(title, stages, result_lines=(), total_s=None):
    """stages: [(name, what was done, seconds or None)]. Each stage on one line with its time right-aligned."""
    out = [header(title, secs(total_s))]
    for name, what, s in stages:
        t = secs(s)
        room = WIDTH - 11 - (len(t) + 2 if t else 0)
        out.append(f"  {name:<8} {fit(what, room):<{room}}" + (f"  {t}" if t else ""))
    for i, r in enumerate(result_lines):
        out.append(fit(("RESULT   " if i == 0 else "         ") + r))
    return "\n".join(x.rstrip() for x in out)


def test_table(title, tests, verdict, total_s=None):
    """tests: [(test name, measured, outcome)] with outcome 'pass', 'FAIL', or '--' for not decisive."""
    w_name = max([len("TEST")] + [len(t[0]) for t in tests])
    w_out = max([len("RESULT")] + [len(str(t[2])) for t in tests])
    w_meas = WIDTH - 2 - w_name - 2 - 2 - w_out
    out = [header(title, secs(total_s)), f"  {'TEST':<{w_name}}  {'MEASURED':<{w_meas}}  RESULT"]
    for name, measured, outcome in tests:
        out.append(f"  {name:<{w_name}}  {fit(measured, w_meas):<{w_meas}}  {outcome}")
    out.append(fit("VERDICT  " + verdict))
    return "\n".join(x.rstrip() for x in out)


def table(title, columns, rows, footer=()):
    """columns: [(heading, 'l' or 'r')]. rows: lists of cells. A cell that is a string spanning the rest (e.g. 'running...') is allowed
    by giving a shorter row."""
    cells = [[str(c) for c in r] for r in rows]
    widths = [max([len(h)] + [len(r[i]) for r in cells if i < len(r)]) for i, (h, _) in enumerate(columns)]
    def fmt(vals, head=False):
        parts = []
        for i, v in enumerate(vals):
            align = columns[i][1]
            parts.append(f"{v:>{widths[i]}}" if (align == "r" and not head) else f"{v:<{widths[i]}}")
        return fit("  " + "  ".join(parts).rstrip())
    out = ([title] if title else []) + [fmt([h for h, _ in columns], head=True)] + [fmt(r) for r in cells] + [fit(f) for f in footer]
    return "\n".join(out)


def bar(done, total, width=24):
    full = int(width * done / max(total, 1)) if total else 0
    full = max(0, min(width, full))
    return "▕" + "█" * full + "░" * (width - full) + "▏"


def progress(title, done, total, *notes):
    return "\n".join([fit(title), fit(f"{bar(done, total)} {join(*notes)}")])


def block(*lines):
    rule = "=" * WIDTH
    return "\n".join([rule] + [fit(" " + x) for x in lines if x] + [rule])


def not_done(what, next_step):
    return line("NOT DONE", what, next_step)
