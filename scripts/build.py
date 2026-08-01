#!/usr/bin/env python3
"""Generate README.md and the two terminal banners from data/projects.yml.

Live values (last push, primary language, recent commits) come from the GitHub
API. Everything the API cannot answer -- lines of code, test counts, blurbs --
lives in data/projects.yml.

Run locally:
    pip install -r requirements.txt
    python scripts/build.py

Dates are deliberately absolute rather than relative ("2026-08-01", not
"3 days ago"). A relative date would change on every run and the daily workflow
would commit even when nothing actually happened.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import yaml
from jinja2 import Environment, FileSystemLoader, StrictUndefined

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = Path(__file__).resolve().parent / "templates"

API = "https://api.github.com"
TIMEOUT = 20

# Banner geometry. CELL is the advance width of one character at FONT size for
# a 0.6em monospace font; every <text> is pinned to a multiple of it via
# textLength, so the layout holds whatever font the viewer actually has.
W = 900
PAD = 28
FONT = 15
CELL = 9           # advance width of one character at FONT
STATUS_CELL = 7.2  # ... and at the 12px status-bar size
FIRST_Y = 74
LINE_H = 26
BLANK_H = 14
STATUS_H = 30
BOTTOM_GAP = 30    # breathing room between the last line and the status bar

THEMES = {
    "dark": {
        "bg": "#0B0E14",
        "chrome": "#121821",
        "border": "#222B38",
        "dim": "#6B7787",
        "body": "#D3DCE8",
        "accent": "#7FE7C4",
        "amber": "#F2B45C",
        "on_accent": "#06231B",
        "scan": "#FFFFFF",
        "scan_op": "0.018",
    },
    "light": {
        "bg": "#FBFAF7",
        "chrome": "#F1EEE7",
        "border": "#DED9CD",
        "dim": "#6E6A5F",
        "body": "#23201C",
        "accent": "#0E7C5A",
        "amber": "#A9670C",
        "on_accent": "#FFFFFF",
        "scan": "#000000",
        "scan_op": "0.020",
    },
}


# --------------------------------------------------------------------------- api


def _get(path: str):
    """GET a GitHub API path. Returns None on any failure.

    The build must never fail because GitHub is unreachable -- a stale README
    beats a broken one, and the workflow would otherwise commit garbage.
    """
    request = urllib.request.Request(
        f"{API}{path}",
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "profile-readme-builder",
        },
    )
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return json.load(response)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        print(f"  ! {path}: {exc}", file=sys.stderr)
        return None


def fetch_repo(owner: str, repo: str) -> dict:
    data = _get(f"/repos/{owner}/{repo}")
    if not data:
        return {"url": f"https://github.com/{owner}/{repo}", "pushed": None}
    return {
        "url": data.get("html_url") or f"https://github.com/{owner}/{repo}",
        "pushed": data.get("pushed_at"),
        "language": data.get("language"),
        "stars": data.get("stargazers_count", 0),
        "private": data.get("private", False),
    }


def fetch_activity(owner: str, repos: list[str], limit: int = 5) -> list[dict]:
    """Most recent commits across the featured repositories, newest first.

    Deliberately not /users/{owner}/events/public: that feed returns PushEvents
    with an empty `commits` array (force pushes, and seemingly at random), so it
    silently yields nothing. Asking each repository directly is reliable.
    """
    collected: list[dict] = []
    for repo in repos:
        commits = _get(f"/repos/{owner}/{repo}/commits?per_page=3")
        if not commits:
            continue
        for item in commits:
            commit = item.get("commit", {})
            message = (commit.get("message") or "").splitlines()[0].strip()
            date = commit.get("committer", {}).get("date")
            if not message or not date:
                continue
            collected.append(
                {
                    "sha": (item.get("sha") or "")[:7],
                    "repo": repo,
                    "message": message,
                    "date": date,
                }
            )
    collected.sort(key=lambda c: c["date"], reverse=True)
    return collected[:limit]


# ------------------------------------------------------------------------ layout


def iso_date(stamp: str | None) -> str:
    if not stamp:
        return "—"
    return datetime.fromisoformat(stamp.replace("Z", "+00:00")).strftime("%Y-%m-%d")


def build_lines(identity: dict, stack: dict, totals: dict) -> tuple[list[dict], float, float, int, int]:
    """Lay out the terminal session and its reveal timings."""
    stack_line = " · ".join(stack.get("Languages", [])[:3] + stack.get("Backend", [])[:2])
    summary = (
        f"{totals['projects']} featured projects · "
        f"{totals['tests']} tests written · {totals['loc']:,} lines"
    )

    rows: list[list[tuple[str, str]] | None] = [
        [("$ ", "prompt"), ("whoami", "cmd")],
        [(f"{identity['name']} · {identity['role']} · {identity['location']}", "lit")],
        None,
        [("$ ", "prompt"), ("cat stack.txt", "cmd")],
        [(stack_line, "out")],
        None,
        [("$ ", "prompt"), ("status", "cmd")],
        [("● ", "warn"), (identity["status"], "lit"), (f"  {summary}", "out")],
    ]

    lines: list[dict] = []
    y = FIRST_Y
    delay = 0.28
    for row in rows:
        if row is None:
            y += BLANK_H
            continue
        chars = sum(len(text) for text, _ in row)
        duration = max(0.18, chars * 0.018)
        lines.append(
            {
                "y": y,
                "chars": chars,
                "width": chars * CELL,
                "dur": duration,
                "delay": delay,
                "segments": [{"text": text, "cls": cls} for text, cls in row],
            }
        )
        delay += duration + 0.12
        y += LINE_H

    last = lines[-1]
    caret_x = PAD + last["width"] + 2  # 2px so the block never kisses the last glyph
    caret_y = last["y"] - 13
    return lines, delay, delay + 0.1, caret_x, caret_y


def build_status(totals: dict, updated: str) -> list[dict]:
    """Status-bar segments. Each label is pinned with textLength so segments
    cannot overlap when the viewer's monospace font is wider than assumed."""
    labels = [
        "MAIN",
        f"{totals['projects']} projects",
        f"{totals['tests']} tests",
        f"updated {updated}",
    ]
    segments = []
    x = 0
    for label in labels:
        text_width = round(len(label) * STATUS_CELL, 1)
        width = int(text_width) + 28
        segments.append({"text": label, "x": x, "w": width, "tw": text_width})
        x += width
    return segments


# -------------------------------------------------------------------------- main


def main() -> int:
    config = yaml.safe_load((ROOT / "data" / "projects.yml").read_text(encoding="utf-8"))
    owner = config["owner"]
    identity = config["identity"]
    stack = config["stack"]

    print(f"Fetching live data for {owner}…")
    projects = []
    for entry in config["projects"]:
        live = fetch_repo(owner, entry["repo"])
        if live.get("private"):
            print(f"  - {entry['repo']}: private, skipping")
            continue
        projects.append(
            {
                **entry,
                "url": live["url"],
                "pushed": live.get("pushed"),
                "pushed_human": iso_date(live.get("pushed")),
            }
        )
        print(f"  ✓ {entry['repo']}")

    activity = fetch_activity(owner, [p["repo"] for p in projects])
    print(f"  ✓ {len(activity)} recent commits")

    totals = {
        "projects": len(projects),
        "tests": sum(p["tests"] for p in projects),
        "loc": sum(p["loc"] for p in projects),
    }
    stamps = [p["pushed"] for p in projects if p.get("pushed")]
    last_activity = iso_date(max(stamps)) if stamps else datetime.now(timezone.utc).strftime("%Y-%m-%d")

    alt = (
        f"Terminal banner — {identity['name']}, {identity['role']} "
        f"from {identity['location']}. {identity['status']}."
    )

    env = Environment(
        loader=FileSystemLoader(TEMPLATES),
        undefined=StrictUndefined,
        keep_trailing_newline=True,
    )

    lines, status_delay, caret_delay, caret_x, caret_y = build_lines(identity, stack, totals)
    status = build_status(totals, last_activity)
    # Height follows the content instead of being fixed, so adding or removing
    # a line never leaves a band of dead space above the status bar.
    height = lines[-1]["y"] + BOTTOM_GAP + STATUS_H

    svg_template = env.get_template("terminal.svg.j2")
    (ROOT / "assets").mkdir(exist_ok=True)
    for theme, colors in THEMES.items():
        svg = svg_template.render(
            W=W, H=height, PAD=PAD, FONT=FONT, CELL=CELL,
            c=colors,
            lines=lines,
            status=status,
            status_delay=status_delay,
            caret_delay=caret_delay,
            caret_x=caret_x,
            caret_y=caret_y,
            chrome_title=f"{owner.lower()}@github — ~/profile",
            alt=alt,
        )
        (ROOT / "assets" / f"terminal-{theme}.svg").write_text(svg, encoding="utf-8")
        print(f"  ✓ assets/terminal-{theme}.svg")

    readme = env.get_template("README.md.j2").render(
        identity=identity,
        stack=stack,
        projects=projects,
        activity=activity,
        pad=max((len(a["repo"]) for a in activity), default=0),
        last_activity=last_activity,
        alt=alt,
    )
    (ROOT / "README.md").write_text(readme, encoding="utf-8")
    print("  ✓ README.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
