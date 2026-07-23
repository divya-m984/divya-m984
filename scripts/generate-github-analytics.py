#!/usr/bin/env python3
"""
GitHub Analytics SVG Generator

Queries GitHub GraphQL and REST APIs for real contribution and language data,
then generates an animated SVG dashboard.

Environment variables:
  GITHUB_USERNAME  GitHub username (default: divya-m984)
  GH_TOKEN         GitHub token (required)
  OUTPUT_PATH      Output SVG file path (default: assets/github-analytics.svg)
"""

import json
import math
import os
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone

# ── Configuration ──────────────────────────────────────────────────────────────

USERNAME = os.environ.get("GITHUB_USERNAME", "divya-m984")
TOKEN = os.environ.get("GH_TOKEN", "")
OUTPUT_PATH = os.environ.get("OUTPUT_PATH", "assets/github-analytics.svg")

GRAPHQL_URL = "https://api.github.com/graphql"

# ── GitHub API ─────────────────────────────────────────────────────────────────


def graphql_request(query: str, variables: dict | None = None) -> dict:
    """Execute a GitHub GraphQL request. Exits on any error."""
    if not TOKEN:
        print("ERROR: GH_TOKEN is not set. Cannot query GitHub API.", file=sys.stderr)
        sys.exit(1)

    payload = json.dumps({"query": query, "variables": variables or {}}).encode("utf-8")
    req = urllib.request.Request(
        GRAPHQL_URL,
        data=payload,
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Content-Type": "application/json",
            "User-Agent": "github-analytics-generator/1.0",
            "Accept": "application/vnd.github+json",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        print(f"HTTP {exc.code} from GitHub API: {detail}", file=sys.stderr)
        sys.exit(1)
    except urllib.error.URLError as exc:
        print(f"Network error: {exc.reason}", file=sys.stderr)
        sys.exit(1)

    if "errors" in body:
        print(
            f"GraphQL errors: {json.dumps(body['errors'], indent=2)}", file=sys.stderr
        )
        sys.exit(1)

    return body["data"]


def fetch_contributions(
    username: str, from_dt: datetime, to_dt: datetime
) -> tuple[list[tuple[str, int]], int]:
    """
    Fetch contribution calendar for the given UTC datetime range.

    Returns:
        days: list of (date_str, count) sorted chronologically
        total: sum of counts (computed from the returned days, not from the API total)
    """
    query = """
    query($login: String!, $from: DateTime!, $to: DateTime!) {
      user(login: $login) {
        contributionsCollection(from: $from, to: $to) {
          contributionCalendar {
            weeks {
              contributionDays {
                date
                contributionCount
              }
            }
          }
        }
      }
    }
    """
    data = graphql_request(
        query,
        {
            "login": username,
            "from": from_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "to": to_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
    )

    user = data.get("user")
    if user is None:
        print(f"ERROR: GitHub user '{username}' not found.", file=sys.stderr)
        sys.exit(1)

    cal = user["contributionsCollection"]["contributionCalendar"]
    days: list[tuple[str, int]] = []
    for week in cal["weeks"]:
        for day in week["contributionDays"]:
            days.append((day["date"], int(day["contributionCount"])))

    days.sort(key=lambda x: x[0])

    # Recompute total from the same dataset used to draw the graph — they always match.
    total = sum(c for _, c in days)
    return days, total


def fetch_languages(username: str) -> tuple[dict[str, int], dict[str, str]]:
    """
    Fetch public, non-forked repository language data (paginated).

    Returns:
        lang_totals: {name: total_bytes}
        lang_colors: {name: hex_color}
    """
    query = """
    query($login: String!, $cursor: String) {
      user(login: $login) {
        repositories(
          first: 100,
          after: $cursor,
          isFork: false,
          privacy: PUBLIC,
          orderBy: {field: UPDATED_AT, direction: DESC}
        ) {
          pageInfo { hasNextPage endCursor }
          nodes {
            languages(first: 10, orderBy: {field: SIZE, direction: DESC}) {
              edges {
                size
                node { name color }
              }
            }
          }
        }
      }
    }
    """
    lang_totals: dict[str, int] = {}
    lang_colors: dict[str, str] = {}
    cursor = None

    while True:
        data = graphql_request(query, {"login": username, "cursor": cursor})
        repos = data["user"]["repositories"]

        for repo in repos["nodes"]:
            for edge in repo["languages"]["edges"]:
                name: str = edge["node"]["name"]
                size: int = int(edge["size"])
                color: str = edge["node"]["color"] or "#8888AA"
                lang_totals[name] = lang_totals.get(name, 0) + size
                if name not in lang_colors:
                    lang_colors[name] = color

        if not repos["pageInfo"]["hasNextPage"]:
            break
        cursor = repos["pageInfo"]["endCursor"]

    return lang_totals, lang_colors


# ── Streak calculation ─────────────────────────────────────────────────────────


def calculate_streaks(days: list[tuple[str, int]]) -> tuple[int, int]:
    """
    Calculate current and longest contribution streaks.

    Uses UTC dates. Current streak counts backward from today; if today has
    no contributions yet (day still in progress) it starts from yesterday.

    Returns:
        current_streak: number of consecutive days up to today (or yesterday)
        longest_streak: longest consecutive run in the period
    """
    if not days:
        return 0, 0

    today_utc: date = datetime.now(timezone.utc).date()
    parsed: list[tuple[date, int]] = [
        (datetime.strptime(d, "%Y-%m-%d").date(), c) for d, c in days
    ]
    parsed.sort()

    # Longest streak in the displayed period
    longest = 0
    run = 0
    prev_date: date | None = None
    for d, c in parsed:
        if c > 0:
            if prev_date is not None and (d - prev_date).days == 1:
                run += 1
            else:
                run = 1
            longest = max(longest, run)
            prev_date = d
        else:
            prev_date = None

    # Current streak: walk backward from today
    active: set[date] = {d for d, c in parsed if c > 0}

    # If today has no contributions, allow starting from yesterday
    start: date = today_utc if today_utc in active else today_utc - timedelta(days=1)

    current = 0
    check = start
    while check in active:
        current += 1
        check -= timedelta(days=1)

    return current, longest


# ── Graph helpers ──────────────────────────────────────────────────────────────


def group_by_week(
    days: list[tuple[str, int]]
) -> tuple[list[date], list[int]]:
    """
    Aggregate daily data into weekly totals (7-day chunks).

    Returns:
        week_starts: first date of each week
        week_counts: contribution total per week
    """
    parsed: list[tuple[date, int]] = [
        (datetime.strptime(d, "%Y-%m-%d").date(), c) for d, c in days
    ]
    parsed.sort()

    week_starts: list[date] = []
    week_counts: list[int] = []
    chunk: list[tuple[date, int]] = []

    for i, (d, c) in enumerate(parsed):
        chunk.append((d, c))
        if len(chunk) == 7 or i == len(parsed) - 1:
            week_starts.append(chunk[0][0])
            week_counts.append(sum(ct for _, ct in chunk))
            chunk = []

    return week_starts, week_counts


def smooth_path(points: list[tuple[float, float]], tension: float = 0.25) -> str:
    """
    Build a smooth cubic bezier path string (Catmull-Rom to bezier).
    """
    if not points:
        return ""
    if len(points) == 1:
        return f"M {points[0][0]:.2f},{points[0][1]:.2f}"

    n = len(points)
    segs = [f"M {points[0][0]:.2f},{points[0][1]:.2f}"]

    for i in range(n - 1):
        p0 = points[max(0, i - 1)]
        p1 = points[i]
        p2 = points[i + 1]
        p3 = points[min(n - 1, i + 2)]

        cp1x = p1[0] + (p2[0] - p0[0]) * tension
        cp1y = p1[1] + (p2[1] - p0[1]) * tension
        cp2x = p2[0] - (p3[0] - p1[0]) * tension
        cp2y = p2[1] - (p3[1] - p1[1]) * tension

        segs.append(
            f"C {cp1x:.2f},{cp1y:.2f} {cp2x:.2f},{cp2y:.2f}"
            f" {p2[0]:.2f},{p2[1]:.2f}"
        )

    return " ".join(segs)


def month_markers(
    week_dates: list[date], gx1: float, gw: float, n_weeks: int
) -> list[tuple[float, str]]:
    """
    Return (x_position, label) for each calendar month boundary in the data.
    """
    if not week_dates or n_weeks == 0:
        return []

    step = gw / max(n_weeks - 1, 1)
    first_seen: dict[tuple[int, int], int] = {}
    for i, d in enumerate(week_dates):
        key = (d.year, d.month)
        if key not in first_seen:
            first_seen[key] = i

    result: list[tuple[float, str]] = []
    for (year, month), i in sorted(first_seen.items()):
        x = gx1 + i * step
        label = datetime(year, month, 1).strftime("%b %y")
        result.append((x, label))

    return result


# ── XML helper ─────────────────────────────────────────────────────────────────


def xe(s: object) -> str:
    """Escape a value for use in XML attribute values or text content."""
    return (
        str(s)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


# ── SVG generation ─────────────────────────────────────────────────────────────


def generate_svg(
    days: list[tuple[str, int]],
    total: int,
    current_streak: int,
    longest_streak: int,
    lang_totals: dict[str, int],
    lang_colors: dict[str, str],
    from_dt: datetime,
    to_dt: datetime,
    updated_utc: str,
) -> str:
    """Build and return the complete animated SVG string."""

    # Canvas sized to 900×490 so it renders at roughly 1:1 on GitHub's ~904 px
    # profile width, keeping all text legible without any browser scaling.
    W, H = 900, 490

    # Graph canvas bounds
    GX1, GY1 = 60.0, 146.0
    GX2, GY2 = 870.0, 336.0
    GW = GX2 - GX1   # 810
    GH = GY2 - GY1   # 190

    # Weekly aggregation
    week_dates, week_counts = group_by_week(days)
    n_weeks = len(week_counts)
    max_weekly = max(week_counts) if week_counts and max(week_counts) > 0 else 1

    # Graph pixel coordinates
    step = GW / max(n_weeks - 1, 1)
    graph_pts: list[tuple[float, float]] = [
        (GX1 + i * step, GY2 - (c / max_weekly) * GH)
        for i, c in enumerate(week_counts)
    ]

    line_path = smooth_path(graph_pts)

    # Area fill closes at the bottom baseline
    if graph_pts:
        area_path = (
            line_path
            + f" L {graph_pts[-1][0]:.2f},{GY2}"
            + f" L {graph_pts[0][0]:.2f},{GY2} Z"
        )
    else:
        area_path = ""

    # Month x-axis markers
    markers = month_markers(week_dates, GX1, GW, n_weeks)

    # Top 5 languages by byte count
    total_lang = sum(lang_totals.values()) or 1
    top5 = sorted(lang_totals.items(), key=lambda x: x[1], reverse=True)[:5]

    # Display labels
    from_label = from_dt.strftime("%d %b %Y").upper()
    to_label = to_dt.strftime("%d %b %Y").upper()
    date_range = f"{from_label} \u2014 {to_label}"

    # Y-axis grid (4 horizontal lines + baseline)
    grid_steps = 4
    grid_vals = [round(max_weekly * i / grid_steps) for i in range(grid_steps + 1)]

    # Top 3 weekly peaks get a pulse dot (indices of the 3 highest weeks)
    if graph_pts and week_counts:
        sorted_idx = sorted(range(len(week_counts)), key=lambda i: week_counts[i], reverse=True)
        peak_idx = set(sorted_idx[:3])
        peak_pts = [graph_pts[i] for i in sorted(peak_idx) if i < len(graph_pts) and week_counts[i] > 0]
    else:
        peak_pts = []

    # ── Assemble SVG ──────────────────────────────────────────────────────────

    L: list[str] = []
    a = L.append

    a('<?xml version="1.0" encoding="UTF-8"?>')
    a(
        f'<svg xmlns="http://www.w3.org/2000/svg"'
        f' xmlns:xlink="http://www.w3.org/1999/xlink"'
        f' viewBox="0 0 {W} {H}"'
        f' width="{W}" height="{H}"'
        f' role="img"'
        f' aria-label="GitHub analytics for {xe(USERNAME)}">'
    )

    # ── Defs ──────────────────────────────────────────────────────────────────
    a("<defs>")

    a("""  <linearGradient id="bg" x1="0" y1="0" x2="0" y2="1">
    <stop offset="0%" stop-color="#060B18"/>
    <stop offset="100%" stop-color="#06101E"/>
  </linearGradient>""")

    a("""  <linearGradient id="area-fill" x1="0" y1="0" x2="0" y2="1">
    <stop offset="0%" stop-color="#28D7FE" stop-opacity="0.16"/>
    <stop offset="100%" stop-color="#28D7FE" stop-opacity="0.01"/>
  </linearGradient>""")

    a("""  <linearGradient id="border" x1="0" y1="0" x2="1" y2="0">
    <stop offset="0%" stop-color="#28D7FE" stop-opacity="0.35"/>
    <stop offset="50%" stop-color="#8B5CF6" stop-opacity="0.55"/>
    <stop offset="100%" stop-color="#4F8CFF" stop-opacity="0.35"/>
  </linearGradient>""")

    a("""  <linearGradient id="scan" x1="0" y1="0" x2="1" y2="0">
    <stop offset="0%"   stop-color="#28D7FE" stop-opacity="0"/>
    <stop offset="50%"  stop-color="#28D7FE" stop-opacity="0.10"/>
    <stop offset="100%" stop-color="#28D7FE" stop-opacity="0"/>
  </linearGradient>""")

    a("""  <filter id="cx" x="-30%" y="-80%" width="160%" height="260%">
    <feGaussianBlur in="SourceGraphic" stdDeviation="3.5" result="b"/>
    <feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge>
  </filter>""")

    a("""  <filter id="vx" x="-30%" y="-80%" width="160%" height="260%">
    <feGaussianBlur in="SourceGraphic" stdDeviation="7" result="b"/>
    <feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge>
  </filter>""")

    a(
        f'  <clipPath id="gc">'
        f'<rect x="{GX1}" y="{GY1 - 8}" width="{GW}" height="{GH + 18}"/>'
        f"</clipPath>"
    )

    a("</defs>")

    # ── Background + border ───────────────────────────────────────────────────
    a(f'<rect width="{W}" height="{H}" fill="url(#bg)"/>')
    a(
        f'<rect x="1" y="1" width="{W - 2}" height="{H - 2}" rx="8" ry="8"'
        f' fill="none" stroke="url(#border)" stroke-width="1.5" opacity="0.75"/>'
    )

    # ── Header ────────────────────────────────────────────────────────────────
    a('<line x1="30" y1="50" x2="870" y2="50" stroke="#1A2840" stroke-width="1"/>')

    a(
        '<text x="30" y="30"'
        ' font-family="\'Courier New\',monospace"'
        ' font-size="9" font-weight="700" letter-spacing="2.6"'
        f' fill="#28D7FE" opacity="0.9">GITHUB ANALYTICS</text>'
    )
    a(
        f'<text x="30" y="43"'
        f' font-family="\'Courier New\',monospace"'
        f' font-size="7" letter-spacing="1.1"'
        f' fill="#4F8CFF" opacity="0.6">{xe(USERNAME)}</text>'
    )
    a(
        f'<text x="870" y="30" text-anchor="end"'
        f' font-family="\'Courier New\',monospace"'
        f' font-size="7" letter-spacing="1.1"'
        f' fill="#A7B2CE" opacity="0.5">UPDATED {xe(updated_utc)} UTC</text>'
    )
    a(
        f'<text x="870" y="43" text-anchor="end"'
        f' font-family="\'Courier New\',monospace"'
        f' font-size="6" letter-spacing="0.75"'
        f' fill="#4F8CFF" opacity="0.4">{xe(date_range)}</text>'
    )

    # ── Stat boxes ────────────────────────────────────────────────────────────
    # Three equal-width boxes: x=30 / x=323 / x=616, each 260px wide, gap 33px
    boxes = [
        (30,  260, "CONTRIBUTIONS",  "LAST 12 MONTHS",   str(total),          "#28D7FE"),
        (323, 260, "CURRENT STREAK", "CONSECUTIVE DAYS",  str(current_streak), "#8B5CF6"),
        (616, 260, "LONGEST STREAK", "IN PERIOD",         str(longest_streak), "#4F8CFF"),
    ]

    for bx, bw, cat, sub, val, col in boxes:
        a(
            f'<rect x="{bx}" y="60" width="{bw}" height="66"'
            f' rx="4" fill="#080F1F" opacity="0.85"/>'
        )
        a(
            f'<rect x="{bx}" y="60" width="{bw}" height="2"'
            f' rx="1" fill="{col}" opacity="0.65"/>'
        )
        a(
            f'<text x="{bx + 12}" y="74"'
            f' font-family="\'Courier New\',monospace"'
            f' font-size="6" font-weight="700" letter-spacing="1.5"'
            f' fill="{col}" opacity="0.85">{xe(cat)}</text>'
        )
        a(
            f'<text x="{bx + 12}" y="83"'
            f' font-family="\'Courier New\',monospace"'
            f' font-size="6" letter-spacing="1.1"'
            f' fill="#A7B2CE" opacity="0.45">{xe(sub)}</text>'
        )
        a(
            f'<text x="{bx + 12}" y="116"'
            f' font-family="\'Courier New\',monospace"'
            f' font-size="29" font-weight="700"'
            f' fill="{col}" filter="url(#cx)">{xe(val)}</text>'
        )

    # ── Graph section label ───────────────────────────────────────────────────
    a(
        f'<text x="45" y="{GY1 - 8:.0f}"'
        f' font-family="\'Courier New\',monospace"'
        f' font-size="6" font-weight="700" letter-spacing="1.9"'
        f' fill="#A7B2CE" opacity="0.4">CONTRIBUTION ACTIVITY</text>'
    )

    # Graph background
    a(
        f'<rect x="{GX1}" y="{GY1}" width="{GW}" height="{GH}"'
        f' rx="2" fill="#050C1A" opacity="0.7"/>'
    )

    # Horizontal grid lines + y-axis labels
    for i in range(grid_steps + 1):
        gy = GY1 + (i / grid_steps) * GH
        gval = grid_vals[grid_steps - i]
        a(
            f'<line x1="{GX1}" y1="{gy:.1f}" x2="{GX2}" y2="{gy:.1f}"'
            f' stroke="#182336" stroke-width="1" opacity="0.9"/>'
        )
        if gval > 0:
            a(
                f'<text x="{GX1 - 6}" y="{gy + 3:.1f}" text-anchor="end"'
                f' font-family="\'Courier New\',monospace"'
                f' font-size="6" fill="#4F8CFF" opacity="0.4">{gval}</text>'
            )

    # Month labels (x-axis); skip labels that would overlap
    prev_mx = -999.0
    for mx, mlabel in markers:
        if mx - prev_mx >= 44:
            a(
                f'<line x1="{mx:.1f}" y1="{GY2}" x2="{mx:.1f}" y2="{GY2 + 3}"'
                f' stroke="#182336" stroke-width="1" opacity="0.7"/>'
            )
            a(
                f'<text x="{mx:.1f}" y="{GY2 + 11}" text-anchor="middle"'
                f' font-family="\'Courier New\',monospace"'
                f' font-size="6" fill="#A7B2CE" opacity="0.38">{xe(mlabel)}</text>'
            )
            prev_mx = mx

    # ── Graph paths and animations ────────────────────────────────────────────
    if line_path and graph_pts:

        # 1. Area fill — fades in after line draws
        a(
            f'<path d="{xe(area_path)}"'
            f' fill="url(#area-fill)" clip-path="url(#gc)" opacity="0">'
        )
        a('  <animate attributeName="opacity" from="0" to="1"'
          ' dur="1.2s" begin="2.8s" fill="freeze"/>')
        a("</path>")

        # 2. Violet glow line (wider, blurred) — draws alongside main line
        a(
            f'<path d="{xe(line_path)}"'
            f' fill="none" stroke="#8B5CF6" stroke-width="6"'
            f' opacity="0.22" filter="url(#vx)" clip-path="url(#gc)"'
            f' stroke-dasharray="10000 10000" stroke-dashoffset="10000">'
        )
        a('  <animate attributeName="stroke-dashoffset"'
          ' from="10000" to="0" dur="3s" begin="0.1s" fill="freeze"/>')
        a("</path>")

        # 3. Main cyan line — draws from left to right
        a(
            f'<path id="analytics-line" d="{xe(line_path)}"'
            f' fill="none" stroke="#28D7FE" stroke-width="2"'
            f' stroke-linecap="round" stroke-linejoin="round"'
            f' filter="url(#cx)" clip-path="url(#gc)"'
            f' stroke-dasharray="10000 10000" stroke-dashoffset="10000">'
        )
        a('  <animate attributeName="stroke-dashoffset"'
          ' from="10000" to="0" dur="3s" fill="freeze"/>')
        a("</path>")

        # 4. Moving cyan dot along the path (loops indefinitely)
        a('<circle r="3.5" fill="#28D7FE" opacity="0.92" filter="url(#cx)"'
          ' clip-path="url(#gc)">')
        a('  <animateMotion dur="14s" repeatCount="indefinite">')
        a(f'    <mpath xlink:href="#analytics-line"/>')
        a('  </animateMotion>')
        a('</circle>')

        # 5. Scanning vertical highlight — slow sweep, low opacity
        a(
            f'<rect y="{GY1:.0f}" width="38" height="{GH:.0f}"'
            f' fill="url(#scan)" clip-path="url(#gc)" opacity="0.9">'
        )
        a(
            f'  <animate attributeName="x"'
            f' from="{GX1 - 38:.0f}" to="{GX2:.0f}"'
            f' dur="9s" repeatCount="indefinite" calcMode="linear"/>'
        )
        a("</rect>")

        # 6. Pulse dots at the top 3 contribution peaks
        for px, py in peak_pts:
            a(
                f'<circle cx="{px:.1f}" cy="{py:.1f}" r="2.5"'
                f' fill="#8B5CF6" opacity="0" clip-path="url(#gc)">'
            )
            # Appear after line finishes drawing
            a('  <animate attributeName="opacity"'
              ' from="0" to="0.85" dur="0.4s" begin="3.2s" fill="freeze"/>')
            # Then pulse
            a('  <animate attributeName="r"'
              ' values="2.5;4.5;2.5" dur="2.8s" begin="3.6s" repeatCount="indefinite"/>')
            a('  <animate attributeName="opacity"'
              ' values="0.85;0.25;0.85" dur="2.8s" begin="3.6s" repeatCount="indefinite"/>')
            a("</circle>")

    else:
        # No contribution data — show a clear message rather than a zeroed graph
        a(
            f'<text x="{(GX1 + GX2) / 2:.0f}" y="{(GY1 + GY2) / 2:.0f}"'
            f' text-anchor="middle"'
            f' font-family="\'Courier New\',monospace"'
            f' font-size="8" fill="#A7B2CE" opacity="0.35">'
            f'No contribution data returned for this period</text>'
        )

    # ── Language bars ─────────────────────────────────────────────────────────
    LY = 357.0          # section top
    BAR_X = 165.0       # bar left edge
    BAR_W = 657.0       # bar maximum width
    BAR_H = 8.0         # bar height
    ROW_H = 17.0        # pixels per language row

    a(
        f'<text x="45" y="{LY - 7:.0f}"'
        f' font-family="\'Courier New\',monospace"'
        f' font-size="6" font-weight="700" letter-spacing="1.9"'
        f' fill="#A7B2CE" opacity="0.4">LANGUAGE DISTRIBUTION</text>'
    )

    if top5:
        for i, (lang_name, lang_bytes) in enumerate(top5):
            ry = LY + 3 + i * ROW_H
            pct = lang_bytes / total_lang * 100.0
            bw = (lang_bytes / total_lang) * BAR_W
            col = lang_colors.get(lang_name, "#8888AA")

            # Language name label
            a(
                f'<text x="{BAR_X - 6:.0f}" y="{ry + BAR_H:.0f}" text-anchor="end"'
                f' font-family="\'Courier New\',monospace"'
                f' font-size="6.5" fill="#A7B2CE" opacity="0.75">{xe(lang_name)}</text>'
            )
            # Track (background)
            a(
                f'<rect x="{BAR_X}" y="{ry}" width="{BAR_W}" height="{BAR_H}"'
                f' rx="2" fill="#0A1525" opacity="0.8"/>'
            )
            # Filled bar — animates in
            a(
                f'<rect x="{BAR_X}" y="{ry}" width="0" height="{BAR_H}"'
                f' rx="2" fill="{xe(col)}" opacity="0.82">'
            )
            a(
                f'  <animate attributeName="width" from="0" to="{bw:.2f}"'
                f' dur="1.4s" begin="{0.4 + i * 0.12:.2f}s" fill="freeze"/>'
            )
            a("</rect>")
            # Percentage label
            a(
                f'<text x="{BAR_X + BAR_W + 6:.0f}" y="{ry + BAR_H:.0f}"'
                f' font-family="\'Courier New\',monospace"'
                f' font-size="6.5" fill="{xe(col)}" opacity="0.75">{pct:.1f}%</text>'
            )
    else:
        a(
            f'<text x="{BAR_X}" y="{LY + 21}"'
            f' font-family="\'Courier New\',monospace"'
            f' font-size="7" fill="#A7B2CE" opacity="0.35">'
            f'No public repository language data available</text>'
        )

    # ── Footer ────────────────────────────────────────────────────────────────
    a('<line x1="30" y1="465" x2="870" y2="465" stroke="#182336" stroke-width="1" opacity="0.6"/>')
    a(
        '<text x="450" y="478" text-anchor="middle"'
        ' font-family="\'Courier New\',monospace"'
        ' font-size="6" fill="#A7B2CE" opacity="0.3">'
        "Public repository language distribution reflects repository contents, not proficiency."
        "</text>"
    )

    a("</svg>")
    return "\n".join(L)


# ── Entry point ────────────────────────────────────────────────────────────────


def main() -> None:
    print(f"Generating GitHub analytics for: {USERNAME}")

    now_utc = datetime.now(timezone.utc)
    to_dt = now_utc
    from_dt = now_utc - timedelta(days=365)

    print(f"Period: {from_dt.date()} to {to_dt.date()} UTC")

    print("Fetching contribution calendar...")
    days, total = fetch_contributions(USERNAME, from_dt, to_dt)
    print(f"  Days returned: {len(days)}")
    print(f"  Total (computed from dataset): {total}")

    current_streak, longest_streak = calculate_streaks(days)
    print(f"  Current streak : {current_streak} days")
    print(f"  Longest streak : {longest_streak} days")

    print("Fetching repository language data...")
    lang_totals, lang_colors = fetch_languages(USERNAME)
    print(f"  Languages found: {len(lang_totals)}, total bytes: {sum(lang_totals.values())}")

    updated_utc = now_utc.strftime("%Y-%m-%d %H:%M")

    print("Generating SVG...")
    svg = generate_svg(
        days=days,
        total=total,
        current_streak=current_streak,
        longest_streak=longest_streak,
        lang_totals=lang_totals,
        lang_colors=lang_colors,
        from_dt=from_dt,
        to_dt=to_dt,
        updated_utc=updated_utc,
    )

    # Validate that the output is well-formed XML before writing
    try:
        ET.fromstring(svg)
        print("SVG XML validation: passed")
    except ET.ParseError as exc:
        print(f"ERROR: Generated SVG is not valid XML: {exc}", file=sys.stderr)
        sys.exit(1)

    out_dir = os.path.dirname(OUTPUT_PATH)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)

    with open(OUTPUT_PATH, "w", encoding="utf-8") as fh:
        fh.write(svg)

    print(f"Written: {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
