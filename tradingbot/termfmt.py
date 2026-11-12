"""Plain terminal tables. No Markdown pipes. No scientific notation."""

from __future__ import annotations


def px(x: float) -> str:
    """Price text that stays readable for both BTC and PEPE."""
    if x != x:
        return "n/a"
    ax = abs(x)
    if ax == 0:
        return "0"
    if ax >= 1000:
        return f"{x:,.1f}"
    if ax >= 1:
        text = f"{x:,.4f}".rstrip("0").rstrip(".")
        return text
    # Fixed decimals. Never 4.44e-06.
    text = f"{x:.12f}".rstrip("0").rstrip(".")
    return text


def pct(x: float) -> str:
    return f"{x:.1%}"


def table(headers: list[str], rows: list[list[str]], right: set[int] | None = None) -> str:
    """Aligned columns. `right` is the set of column indexes that align right."""
    right = right or set()
    if not rows:
        return "(none)"
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(str(cell)))

    def line(cells: list[str], *, pad_last: bool) -> str:
        parts = []
        for i, cell in enumerate(cells):
            text = str(cell)
            last = i == len(cells) - 1
            if i in right:
                parts.append(text.rjust(widths[i]))
            elif last and not pad_last:
                parts.append(text)
            else:
                parts.append(text.ljust(widths[i]))
        return "  ".join(parts).rstrip()

    rule = "  ".join("-" * w for w in widths)
    body = [line(headers, pad_last=False), rule]
    body.extend(line(list(row), pad_last=False) for row in rows)
    return "\n".join(body)


def kv(pairs: list[tuple[str, str]]) -> str:
    return table(["Item", "Value"], [[k, v] for k, v in pairs])


def title(text: str, note: str = "") -> str:
    bar = "-" * len(text)
    if note:
        return f"\n{text}\n{bar}\n{note}"
    return f"\n{text}\n{bar}"


def banner(text: str) -> str:
    return text
