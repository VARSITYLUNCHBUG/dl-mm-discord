"""Small inline SVG charts for the staff dashboard. Server-rendered, so no chart library.

Colors are CSS custom properties set by the page, so light and dark both work. Each mark carries
a `data-tip` the page's script shows on hover, and every chart has a table of the same numbers.
"""

from __future__ import annotations

from html import escape

from league.services.calibration import Bin

# Small on purpose: the charts are drawn at 280-500px, so text in these units stays readable.
WIDTH = 420
HEIGHT = 300
PAD_LEFT, PAD_RIGHT, PAD_TOP, PAD_BOTTOM = 62, 24, 14, 52


def _x(value: float) -> float:
    return PAD_LEFT + value * (WIDTH - PAD_LEFT - PAD_RIGHT)


def _y(value: float) -> float:
    return HEIGHT - PAD_BOTTOM - value * (HEIGHT - PAD_TOP - PAD_BOTTOM)


def calibration_svg(bins: list[Bin]) -> str:
    """Predicted chance against how often that team won, per 10% bin, with 95% intervals.
    Points on the dashed diagonal are perfectly calibrated."""
    parts = [
        f'<svg viewBox="0 0 {WIDTH} {HEIGHT}" role="img" class="viz" '
        'aria-label="Predicted chance against actual win rate">'
    ]
    for tick in (0, 0.25, 0.5, 0.75, 1):
        x, y = _x(tick), _y(tick)
        parts.append(
            f'<line class="grid" x1="{PAD_LEFT}" x2="{WIDTH - PAD_RIGHT}" '
            f'y1="{y:.1f}" y2="{y:.1f}"/>'
            f'<text class="tick" x="{PAD_LEFT - 8}" y="{y + 4:.1f}" text-anchor="end">'
            f"{tick:.0%}</text>"
            f'<text class="tick" x="{x:.1f}" y="{HEIGHT - PAD_BOTTOM + 18}" '
            f'text-anchor="middle">{tick:.0%}</text>'
        )
    parts.append(
        f'<line class="reference" x1="{_x(0):.1f}" y1="{_y(0):.1f}" '
        f'x2="{_x(1):.1f}" y2="{_y(1):.1f}"/>'
        f'<text class="axis" x="{(PAD_LEFT + WIDTH - PAD_RIGHT) / 2:.1f}" y="{HEIGHT - 6}" '
        'text-anchor="middle">Predicted chance for team A</text>'
        '<text class="axis" transform="rotate(-90)" '
        f'x="{-(PAD_TOP + HEIGHT - PAD_BOTTOM) / 2:.1f}" '
        'y="14" text-anchor="middle">Team A actually won</text>'
    )
    for b in bins:
        lo, hi = b.interval
        x = _x(b.mean_predicted)
        tip = escape(
            f"Predicted {b.low:.0%}-{b.high:.0%} (mean {b.mean_predicted:.0%}): "
            f"won {b.wins} of {b.games}, {b.rate:.0%}. 95% interval {lo:.0%}-{hi:.0%}",
            quote=True,
        )
        parts.append(
            f'<g class="mark" data-tip="{tip}" tabindex="0">'
            f'<rect class="hit" x="{x - 14:.1f}" y="{PAD_TOP}" width="28" '
            f'height="{HEIGHT - PAD_TOP - PAD_BOTTOM}"/>'
            f'<line class="whisker" x1="{x:.1f}" x2="{x:.1f}" y1="{_y(lo):.1f}" y2="{_y(hi):.1f}"/>'
            f'<circle class="dot" cx="{x:.1f}" cy="{_y(b.rate):.1f}" r="5"/>'
            "</g>"
        )
    parts.append("</svg>")
    return "".join(parts)


def histogram_svg(buckets: list[tuple[str, int]], axis_label: str) -> str:
    """Counts per bucket, as bars on a shared baseline."""
    peak = max((count for _label, count in buckets), default=0) or 1
    inner = WIDTH - PAD_LEFT - PAD_RIGHT
    slot = inner / max(len(buckets), 1)
    bar = max(slot - 2, 1)  # A 2px gap between bars.
    parts = [
        f'<svg viewBox="0 0 {WIDTH} {HEIGHT}" role="img" class="viz" '
        f'aria-label="{escape(axis_label, quote=True)}">'
    ]
    for tick in (0, 0.5, 1):
        y = _y(tick)
        parts.append(
            f'<line class="grid" x1="{PAD_LEFT}" x2="{WIDTH - PAD_RIGHT}" '
            f'y1="{y:.1f}" y2="{y:.1f}"/>'
            f'<text class="tick" x="{PAD_LEFT - 8}" y="{y + 4:.1f}" text-anchor="end">'
            f"{round(peak * tick)}</text>"
        )
    for index, (label, count) in enumerate(buckets):
        x = PAD_LEFT + index * slot + 1
        top = _y(count / peak)
        height = _y(0) - top
        tip = escape(f"Uncertainty {label}: {count} players", quote=True)
        bar_path = _rounded_top(x, top, bar, height) if height > 0 else ""
        parts.append(
            f'<g class="mark" data-tip="{tip}" tabindex="0">'
            f'<rect class="hit" x="{x:.1f}" y="{PAD_TOP}" width="{bar:.1f}" '
            f'height="{HEIGHT - PAD_TOP - PAD_BOTTOM}"/>'
            f'<path class="bar" d="{bar_path}"/>'
            f'<text class="tick" x="{x + bar / 2:.1f}" y="{HEIGHT - PAD_BOTTOM + 18}" '
            f'text-anchor="middle">{escape(label)}</text>'
            "</g>"
        )
    parts.append(
        f'<text class="axis" x="{(PAD_LEFT + WIDTH - PAD_RIGHT) / 2:.1f}" y="{HEIGHT - 6}" '
        f'text-anchor="middle">{escape(axis_label)}</text></svg>'
    )
    return "".join(parts)


def _rounded_top(x: float, top: float, width: float, height: float) -> str:
    """A bar with 4px rounded corners at its data end, square on the baseline."""
    r = min(4.0, width / 2, height)
    bottom = top + height
    return (
        f"M{x:.1f},{bottom:.1f} V{top + r:.1f} Q{x:.1f},{top:.1f} {x + r:.1f},{top:.1f} "
        f"H{x + width - r:.1f} Q{x + width:.1f},{top:.1f} {x + width:.1f},{top + r:.1f} "
        f"V{bottom:.1f} Z"
    )
