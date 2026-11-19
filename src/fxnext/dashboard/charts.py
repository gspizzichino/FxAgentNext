"""Small server-side SVG charts. No JavaScript, no CDN: pages work offline.

Colors come from CSS custom properties so the charts follow light and dark mode.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from html import escape
from zoneinfo import ZoneInfo

from markupsafe import Markup

PALETTE = ("var(--accent)", "var(--warn)", "var(--seal)", "var(--pos)")


def fmt_r(v: float | None, digits: int = 3) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "–"
    sign = "+" if v > 0 else "−" if v < 0 else ""
    return f"{sign}{abs(v):.{digits}f}".replace(".", ",")


def _nice_ticks(lo: float, hi: float, n: int = 5) -> list[float]:
    if hi <= lo:
        hi = lo + 1
    raw = (hi - lo) / n
    mag = 10 ** math.floor(math.log10(raw))
    step = min((m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw), default=raw)
    start = math.floor(lo / step) * step
    ticks = []
    t = start
    while t <= hi + step * 1e-9:
        ticks.append(round(t, 10))
        t += step
    return ticks


def _svg(w: int, h: int, body: str, label: str) -> Markup:
    return Markup(
        f'<svg viewBox="0 0 {w} {h}" width="100%" role="img" aria-label="{escape(label)}" '
        f'style="max-width:{w}px;display:block">{body}</svg>'
    )


def equity(points: Sequence[tuple[int, float]], label: str = "R cumulati") -> Markup:
    """Cumulative R over time, with a zero line and the final value marked."""

    if not points:
        return Markup('<p class="muted">Nessun trade.</p>')
    w, h, left, right, top, bottom = 720, 240, 46, 64, 12, 26
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    x0, x1 = min(xs), max(xs)
    ticks = _nice_ticks(min(min(ys), 0.0), max(max(ys), 0.0))
    lo, hi = ticks[0], ticks[-1]

    def sx(v: float) -> float:
        return left + (v - x0) / max(x1 - x0, 1) * (w - left - right)

    def sy(v: float) -> float:
        return top + (hi - v) / max(hi - lo, 1e-9) * (h - top - bottom)

    body = []
    for t in ticks:
        body.append(
            f'<line x1="{left}" x2="{w - right}" y1="{sy(t):.1f}" y2="{sy(t):.1f}" '
            f'stroke="var(--line)"/><text x="{left - 6}" y="{sy(t) + 4:.1f}" '
            f'text-anchor="end">{t:g}</text>'
        )
    body.append(
        f'<line x1="{left}" x2="{w - right}" y1="{sy(0):.1f}" y2="{sy(0):.1f}" stroke="var(--muted)"/>'
    )
    y_first = datetime.fromtimestamp(x0 / 1e9, tz=UTC).year
    y_last = datetime.fromtimestamp(x1 / 1e9, tz=UTC).year
    for year in range(y_first + 1, y_last + 1):
        ns = int(datetime(year, 1, 1, tzinfo=UTC).timestamp() * 1e9)
        body.append(
            f'<line x1="{sx(ns):.1f}" x2="{sx(ns):.1f}" y1="{top}" y2="{h - bottom}" '
            f'stroke="var(--line)" stroke-dasharray="2 3"/>'
            f'<text x="{sx(ns):.1f}" y="{h - 8}" text-anchor="middle">{year}</text>'
        )
    step = max(len(points) // 900, 1)
    sampled = list(points[::step])
    if sampled[-1] != points[-1]:
        sampled.append(points[-1])
    pts = " ".join(f"{sx(x):.1f},{sy(y):.1f}" for x, y in sampled)
    area = f"{sx(sampled[0][0]):.1f},{sy(0):.1f} {pts} {sx(sampled[-1][0]):.1f},{sy(0):.1f}"
    body.append(f'<polygon points="{area}" fill="var(--accent-soft)" opacity="0.7"/>')
    body.append(f'<polyline points="{pts}" fill="none" stroke="var(--accent)" stroke-width="1.6"/>')
    lx, ly = sx(points[-1][0]), sy(points[-1][1])
    body.append(f'<circle cx="{lx:.1f}" cy="{ly:.1f}" r="3.5" fill="var(--accent)"/>')
    body.append(
        f'<text x="{lx + 6:.1f}" y="{ly + 4:.1f}" class="ink">{fmt_r(points[-1][1], 1)} R</text>'
    )
    return _svg(w, h, "".join(body), label)


def forest(rows: Sequence[dict[str, object]], label: str) -> Markup:
    """Expectancy with CI. rows: {label, value, lo, hi, filled, bold}."""

    vals = [float(v) for r in rows for v in (r["value"], r["lo"], r["hi"]) if v is not None]  # type: ignore[arg-type]
    if not vals:
        return Markup("")
    ticks = _nice_ticks(min(min(vals), 0.0), max(max(vals), 0.0), 6)
    lo, hi = ticks[0], ticks[-1]
    w, left, right, top, rh = 640, 150, 60, 14, 26
    h = top + len(rows) * rh + 28

    def sx(v: float) -> float:
        return left + (v - lo) / (hi - lo) * (w - left - right)

    body = []
    for t in ticks:
        body.append(
            f'<line x1="{sx(t):.1f}" x2="{sx(t):.1f}" y1="{top - 4}" y2="{h - 24}" stroke="var(--line)"/>'
            f'<text x="{sx(t):.1f}" y="{h - 8}" text-anchor="middle">{fmt_r(t, 2) if t else "0"}</text>'
        )
    body.append(
        f'<line x1="{sx(0):.1f}" x2="{sx(0):.1f}" y1="{top - 4}" y2="{h - 24}" '
        f'stroke="var(--muted)" stroke-width="1.3"/>'
    )
    for i, r in enumerate(rows):
        y = top + i * rh + rh / 2
        col = "var(--accent)" if r.get("filled") else "var(--muted)"
        weight = ' font-weight="600"' if r.get("bold") else ""
        body.append(
            f'<text x="0" y="{y + 4:.1f}"{weight} class="ink">{escape(str(r["label"]))}</text>'
        )
        if r["lo"] is not None and r["hi"] is not None:
            body.append(
                f'<line x1="{sx(float(r["lo"])):.1f}" x2="{sx(float(r["hi"])):.1f}" '  # type: ignore[arg-type]
                f'y1="{y:.1f}" y2="{y:.1f}" stroke="{col}" stroke-width="2"/>'
            )
        v = float(r["value"])  # type: ignore[arg-type]
        fill = "var(--accent)" if r.get("filled") else "var(--panel)"
        body.append(
            f'<rect x="{sx(v) - 5:.1f}" y="{y - 5:.1f}" width="10" height="10" fill="{fill}" '
            f'stroke="{col}" stroke-width="1.5"/>'
            f'<text x="{w - 4}" y="{y + 4:.1f}" text-anchor="end" class="ink">{fmt_r(v)}</text>'
        )
    return _svg(w, h, "".join(body), label)


def grouped_bars(
    categories: Sequence[str], series: Sequence[tuple[str, Sequence[float]]], label: str
) -> Markup:
    vals = [v for _, vs in series for v in vs]
    if not vals:
        return Markup("")
    ticks = _nice_ticks(min(min(vals), 0.0), max(max(vals), 0.0))
    lo, hi = ticks[0], ticks[-1]
    w, h, left, right, top, bottom = 640, 230, 40, 10, 12, 30

    def sy(v: float) -> float:
        return top + (hi - v) / (hi - lo) * (h - top - bottom)

    gw = (w - left - right) / max(len(categories), 1)
    bw = (gw - 16) / max(len(series), 1)
    body = []
    for t in ticks:
        body.append(
            f'<line x1="{left}" x2="{w - right}" y1="{sy(t):.1f}" y2="{sy(t):.1f}" stroke="var(--line)"/>'
            f'<text x="{left - 6}" y="{sy(t) + 4:.1f}" text-anchor="end">{t:g}</text>'
        )
    body.append(
        f'<line x1="{left}" x2="{w - right}" y1="{sy(0):.1f}" y2="{sy(0):.1f}" stroke="var(--muted)"/>'
    )
    for i, cat in enumerate(categories):
        gx = left + i * gw + 8
        for j, (name, vs) in enumerate(series):
            v = vs[i]
            y0, y1 = sorted((sy(v), sy(0)))
            body.append(
                f'<rect x="{gx + j * bw:.1f}" y="{y0:.1f}" width="{max(bw - 2, 1):.1f}" '
                f'height="{max(y1 - y0, 1):.1f}" fill="{PALETTE[j % len(PALETTE)]}" rx="1">'
                f"<title>{escape(name)} {escape(cat)}: {fmt_r(v, 1)} R</title></rect>"
            )
        body.append(
            f'<text x="{gx + len(series) * bw / 2:.1f}" y="{h - 10}" text-anchor="middle">'
            f"{escape(cat)}</text>"
        )
    legend = "".join(
        f'<span><i style="background:{PALETTE[j % len(PALETTE)]}"></i>{escape(name)}</span>'
        for j, (name, _) in enumerate(series)
    )
    return Markup(f'<div class="legend">{legend}</div>') + _svg(w, h, "".join(body), label)


def benchmarks(bench: dict[str, object], baseline: float, label: str) -> Markup:
    """Null benchmark ranges (p05–p95, median) against the baseline expectancy."""

    ranges = [(k, v) for k, v in bench.items() if isinstance(v, dict)]
    points = [(k, float(v)) for k, v in bench.items() if isinstance(v, int | float)]
    vals = (
        [baseline]
        + [float(v[x]) for _, v in ranges for x in ("p05", "p95")]
        + [p for _, p in points]
    )
    ticks = _nice_ticks(min(min(vals), 0.0) if max(vals) > 0 else min(vals), max(max(vals), 0.0), 6)
    lo, hi = ticks[0], ticks[-1]
    w, left, right, top, rh = 640, 140, 20, 16, 32
    n = len(ranges) + len(points)
    h = top + n * rh + 28

    def sx(v: float) -> float:
        return left + (v - lo) / (hi - lo) * (w - left - right)

    body = []
    for t in ticks:
        body.append(
            f'<line x1="{sx(t):.1f}" x2="{sx(t):.1f}" y1="{top}" y2="{h - 22}" stroke="var(--line)"/>'
            f'<text x="{sx(t):.1f}" y="{h - 6}" text-anchor="middle">{fmt_r(t, 2) if t else "0"}</text>'
        )
    i = 0
    for name, v in ranges:
        y = top + i * rh + rh / 2
        p05, p95, med = float(v["p05"]), float(v["p95"]), float(v["median"])
        body.append(
            f'<text x="0" y="{y + 4:.1f}">{escape(name)}</text>'
            f'<rect x="{sx(p05):.1f}" y="{y - 7:.1f}" width="{sx(p95) - sx(p05):.1f}" height="14" '
            f'fill="var(--seal-soft)" stroke="var(--seal)" rx="2"/>'
            f'<line x1="{sx(med):.1f}" x2="{sx(med):.1f}" y1="{y - 7:.1f}" y2="{y + 7:.1f}" '
            f'stroke="var(--seal)" stroke-width="2"/>'
        )
        i += 1
    for name, p in points:
        y = top + i * rh + rh / 2
        body.append(
            f'<text x="0" y="{y + 4:.1f}">{escape(name)}</text>'
            f'<circle cx="{sx(p):.1f}" cy="{y:.1f}" r="4.5" fill="var(--seal)"/>'
        )
        i += 1
    body.append(
        f'<line x1="{sx(baseline):.1f}" x2="{sx(baseline):.1f}" y1="{top - 4}" y2="{h - 22}" '
        f'stroke="var(--accent)" stroke-width="2" stroke-dasharray="4 3"/>'
        f'<text x="{sx(baseline) + 5:.1f}" y="{top + 6}" class="ink" font-weight="600">'
        f"strategia {fmt_r(baseline)}</text>"
    )
    return _svg(w, h, "".join(body), label)


__all__ = ["benchmarks", "equity", "fmt_r", "forest", "grouped_bars"]


_ROME = ZoneInfo("Europe/Rome")


def _time_ticks(x0: int, x1: int) -> list[tuple[int, str]]:
    """Midnights (or whole hours on a short span) in Italian time, at most about 8."""

    span_h = (x1 - x0) / 3.6e12
    start = datetime.fromtimestamp(x0 / 1e9, tz=_ROME).replace(tzinfo=None)
    hourly = span_h <= 36
    if hourly:
        every = next((h for h in (1, 2, 3, 6, 12) if span_h / h <= 8), 12)
        t = start.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        step, fmt = timedelta(hours=1), "%H:%M"
    else:
        every = next((d for d in (1, 2, 7, 14, 28) if span_h / 24 / d <= 8), 28)
        t = start.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        step, fmt = timedelta(days=1), "%d/%m"
    out = []
    i = 0
    while (ns := int(t.replace(tzinfo=_ROME).timestamp() * 1e9)) <= x1:
        if (t.hour if hourly else i) % every == 0:
            out.append((ns, t.strftime(fmt)))
        t += step  # wall clock steps, so midnights stay midnights across DST
        i += 1
    return out


def time_lines(
    series: Sequence[tuple[str, str, Sequence[tuple[int, float]], bool]],
    label: str,
    unit: str = "",
    zero: bool = False,
) -> Markup:
    """Lines over time: (name, color, points, step) per series; dates in Italian time."""

    pts_all = [p for _, _, pts, _ in series for p in pts]
    if not pts_all:
        return Markup('<p class="muted">Ancora nessun dato.</p>')
    w, h, left, right, top, bottom = 720, 240, 62, 86, 12, 26
    x0 = min(p[0] for p in pts_all)
    x1 = max(p[0] for p in pts_all)
    if x1 == x0:
        x1 = x0 + 3_600_000_000_000
    ys = [p[1] for p in pts_all] + ([0.0] if zero else [])
    lo_v, hi_v = min(ys), max(ys)
    if hi_v - lo_v < 1e-9:
        pad = max(abs(hi_v) * 0.001, 1.0)
        lo_v, hi_v = lo_v - pad, hi_v + pad
    ticks = _nice_ticks(lo_v, hi_v)
    if len(ticks) > 1:
        while ticks[-1] < hi_v - 1e-9:  # the top line must sit above the highest point
            ticks.append(round(ticks[-1] + (ticks[1] - ticks[0]), 10))
    lo, hi = ticks[0], ticks[-1]

    def sx(v: float) -> float:
        return left + (v - x0) / (x1 - x0) * (w - left - right)

    def sy(v: float) -> float:
        return top + (hi - v) / max(hi - lo, 1e-9) * (h - top - bottom)

    def num(v: float) -> str:
        if abs(v) >= 1000:
            return f"{v:,.0f}".replace(",", ".")
        return f"{v:.2f}".replace(".", ",")

    body = []
    for t in ticks:
        body.append(
            f'<line x1="{left}" x2="{w - right}" y1="{sy(t):.1f}" y2="{sy(t):.1f}" '
            f'stroke="var(--line)"/><text x="{left - 6}" y="{sy(t) + 4:.1f}" '
            f'text-anchor="end">{escape(num(t))}</text>'
        )
    if zero and lo < 0 < hi:
        body.append(
            f'<line x1="{left}" x2="{w - right}" y1="{sy(0):.1f}" y2="{sy(0):.1f}" '
            f'stroke="var(--muted)"/>'
        )
    for ns, text in _time_ticks(x0, x1):
        body.append(
            f'<line x1="{sx(ns):.1f}" x2="{sx(ns):.1f}" y1="{top}" y2="{h - bottom}" '
            f'stroke="var(--line)" stroke-dasharray="2 3"/>'
            f'<text x="{sx(ns):.1f}" y="{h - 8}" text-anchor="middle">{text}</text>'
        )
    used: list[float] = []
    for _name, color, pts, step in series:
        if not pts:
            continue
        seq = list(pts)
        if step:
            stepped = [seq[0]]
            for a, b in zip(seq, seq[1:], strict=False):
                stepped += [(b[0], a[1]), b]
            seq = stepped
        line = " ".join(f"{sx(x):.1f},{sy(y):.1f}" for x, y in seq)
        body.append(f'<polyline points="{line}" fill="none" stroke="{color}" stroke-width="1.8"/>')
        lx, ly = sx(seq[-1][0]), sy(seq[-1][1])
        body.append(f'<circle cx="{lx:.1f}" cy="{ly:.1f}" r="3.5" fill="{color}"/>')
        ty = ly + 4
        while any(abs(ty - u) < 13 for u in used):  # keep end labels apart
            ty += 13
        used.append(ty)
        body.append(
            f'<text x="{lx + 6:.1f}" y="{ty:.1f}" class="ink">'
            f"{escape(num(seq[-1][1]))}{escape(unit)}</text>"
        )
    return _svg(w, h, "".join(body), label)
