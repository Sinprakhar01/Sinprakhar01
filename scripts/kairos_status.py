#!/usr/bin/env python3
"""
KAIROS live telemetry generator.

Renders the "Agent Status" terminal panel on the profile README as SVG
(dark + light variants) using REAL data from the GitHub API — no
hand-typed metrics. Runs on a schedule via .github/workflows/kairos.yml
and publishes to the `kairos-output` branch (single force-pushed commit,
so main history stays clean and repo size stays bounded).

Honesty rules this panel lives by:
  - Every metric is fetched at render time; the panel prints its own
    last-sync timestamp instead of promising a refresh cadence
    (GitHub throttles scheduled workflows, so cadence is best-effort).
  - Profile machinery (this repo, the github-readme-stats fork) never
    headlines `last_push` — telemetry should surface real work, not
    work on the telemetry.
  - A stale `last_push` (>45 days) is omitted rather than advertised.
  - The mission line is data too: it comes from scripts/mission.json,
    which is edited by a human and versioned in main.

Data sources (all live):
  - /users/{USER}/repos                -> own non-fork repos: last push
  - repo languages endpoints           -> real language mix (by bytes)
  - GraphQL contributionsCollection    -> commits in the last 7 days
    (falls back to the public events API when no token is available)

Zero third-party dependencies: Python 3 stdlib only.
"""

import argparse
import datetime as dt
import json
import os
import sys
import urllib.error
import urllib.request
from xml.sax.saxutils import escape

USER = "Sinprakhar01"
API = "https://api.github.com"
TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()

# Repos that exist to power this profile. Real work never happens here,
# so they are barred from headlining the ACTIVITY section.
MACHINERY = {USER.lower(), "github-readme-stats"}

# last_push older than this is hidden instead of displayed.
STALE_DAYS = 45

MISSION_FILE = os.path.join(os.path.dirname(__file__), "mission.json")
MISSION_FALLBACK = {
    "focus": "AI systems · applied ML",
    "mission": "exploring the search space",
    "status": "QUEUED",
}


def load_mission() -> dict:
    try:
        with open(MISSION_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
        return {**MISSION_FALLBACK, **{k: v for k, v in data.items() if v}}
    except (OSError, ValueError):
        return dict(MISSION_FALLBACK)


def _request(url: str, payload: dict | None = None):
    headers = {
        "User-Agent": f"{USER}-kairos",
        "Accept": "application/vnd.github+json",
    }
    if TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.load(resp)


def gh(path: str):
    return _request(f"{API}{path}")


def relative_time(iso: str, now: dt.datetime) -> str:
    then = dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))
    seconds = max(0, int((now - then).total_seconds()))
    if seconds < 3600:
        return f"{max(1, seconds // 60)}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


def commits_last_7d(now: dt.datetime):
    """(commit_count, repo_count) for the last 7 days. Bot commits excluded
    by nature of both APIs (they attribute to github-actions, not the user)."""
    cutoff = now - dt.timedelta(days=7)
    if TOKEN:
        query = """
        query($login: String!, $from: DateTime!) {
          user(login: $login) {
            contributionsCollection(from: $from) {
              totalCommitContributions
              commitContributionsByRepository(maxRepositories: 100) {
                repository { name }
              }
            }
          }
        }"""
        variables = {"login": USER, "from": cutoff.isoformat()}
        out = _request(
            f"{API}/graphql", {"query": query, "variables": variables}
        )
        coll = out["data"]["user"]["contributionsCollection"]
        return (
            coll["totalCommitContributions"],
            len(coll["commitContributionsByRepository"]),
        )
    # Tokenless fallback: public events (approximate, last ~90 days window).
    commits, repos = 0, set()
    for page in (1, 2, 3):
        events = gh(f"/users/{USER}/events/public?per_page=100&page={page}")
        if not events:
            break
        for ev in events:
            if ev.get("type") != "PushEvent":
                continue
            created = dt.datetime.fromisoformat(
                ev["created_at"].replace("Z", "+00:00")
            )
            if created >= cutoff:
                commits += ev["payload"].get("distinct_size", 0)
                repos.add(ev["repo"]["name"])
    return commits, len(repos)


def language_mix(repos: list) -> list:
    totals: dict[str, int] = {}
    try:
        for repo in repos:
            for lang, size in gh(f"/repos/{USER}/{repo['name']}/languages").items():
                totals[lang] = totals.get(lang, 0) + size
    except (urllib.error.URLError, urllib.error.HTTPError, OSError):
        # Rate-limited or offline: degrade to coarse per-repo language field.
        for repo in repos:
            lang = repo.get("language")
            if lang:
                totals[lang] = totals.get(lang, 0) + 1
    grand = sum(totals.values()) or 1
    ranked = sorted(totals.items(), key=lambda kv: -kv[1])[:3]
    mix = [(lang, round(100 * size / grand)) for lang, size in ranked]
    return [(lang, pct) for lang, pct in mix if pct >= 1]


def latest_real_push(repos: list, now: dt.datetime):
    """Most recent push to a non-machinery repo (forks included: eval-harness
    work happens there). Returns (name, when) or (None, None) when everything
    recent is machinery or the freshest push is older than STALE_DAYS —
    a stale timestamp is worse than no timestamp."""
    candidates = [r for r in repos if r["name"].lower() not in MACHINERY]
    if not candidates:
        return None, None
    last = max(candidates, key=lambda r: r["pushed_at"])
    pushed = dt.datetime.fromisoformat(last["pushed_at"].replace("Z", "+00:00"))
    if (now - pushed).days > STALE_DAYS:
        return None, None
    return last["name"], relative_time(last["pushed_at"], now)


def collect():
    now = dt.datetime.now(dt.timezone.utc)
    repos = gh(f"/users/{USER}/repos?per_page=100&type=owner")
    own = [
        r
        for r in repos
        if not r["fork"] and r["name"].lower() != USER.lower()
    ]
    push_repo, push_when = latest_real_push(repos, now)
    commits7, repos7 = commits_last_7d(now)
    mission = load_mission()
    return {
        "sync": now.strftime("%Y-%m-%d %H:%M UTC"),
        "last_push_repo": push_repo,
        "last_push_when": push_when,
        "commits_7d": commits7,
        "repos_7d": repos7,
        "langs": language_mix(own),
        "focus": mission["focus"],
        "mission": mission["mission"],
        "mission_status": str(mission["status"]).upper()[:12],
    }


THEMES = {
    "dark": {
        "bg": "#0d1117", "border": "#30363d", "bar_bg": "#21262d",
        "text": "#c9d1d9", "dim": "#8b949e", "blue": "#58a6ff",
        "green": "#3fb950", "amber": "#d29922", "sep": "#21262d",
        "chrome": ("#ff5f57", "#febc2e", "#28c840"),
    },
    "light": {
        "bg": "#ffffff", "border": "#d0d7de", "bar_bg": "#eaeef2",
        "text": "#24292f", "dim": "#57606a", "blue": "#0969da",
        "green": "#1a7f37", "amber": "#9a6700", "sep": "#d8dee4",
        "chrome": ("#ff5f57", "#febc2e", "#28c840"),
    },
}

WIDTH = 780
PAD_X = 28
LINE_H = 22
KEY_X = 52
VAL_X = 200
FONT = "ui-monospace,SFMono-Regular,'SF Mono',Menlo,Consolas,monospace"


def render(theme_name: str, d: dict) -> str:
    t = THEMES[theme_name]
    parts: list[str] = []
    y = 66

    def text(segments, x=PAD_X):
        nonlocal y
        spans = "".join(
            f'<tspan fill="{color}">{escape(s)}</tspan>' for s, color in segments
        )
        parts.append(
            f'<text x="{x}" y="{y}" font-family="{FONT}" '
            f'font-size="13" xml:space="preserve">{spans}</text>'
        )
        y += LINE_H

    def sep():
        nonlocal y
        y -= 8
        parts.append(
            f'<line x1="{PAD_X}" y1="{y}" x2="{WIDTH - PAD_X}" y2="{y}" '
            f'stroke="{t["sep"]}" stroke-width="1"/>'
        )
        y += 20

    def bar(label: str, pct: int, color: str):
        nonlocal y
        bar_x, bar_w, bar_h = VAL_X, 220, 8
        by = y - 9
        parts.append(
            f'<text x="{KEY_X}" y="{y}" font-family="{FONT}" font-size="13" '
            f'fill="{t["dim"]}" xml:space="preserve">{escape(label)}</text>'
        )
        parts.append(
            f'<rect x="{bar_x}" y="{by}" width="{bar_w}" height="{bar_h}" '
            f'rx="4" fill="{t["bar_bg"]}"/>'
        )
        fill_w = max(4, round(bar_w * min(pct, 100) / 100))
        parts.append(
            f'<rect x="{bar_x}" y="{by}" width="{fill_w}" height="{bar_h}" '
            f'rx="4" fill="{color}"/>'
        )
        parts.append(
            f'<text x="{bar_x + bar_w + 14}" y="{y}" font-family="{FONT}" '
            f'font-size="13" fill="{t["text"]}" xml:space="preserve">{pct}%</text>'
        )
        y += LINE_H

    def kv(dot_color, key, value_segments):
        text(
            [("● ", dot_color), (f"{key:<14}", t["dim"])]
            + value_segments
        )

    text([("$ ", t["green"]), ("kairos status --live", t["text"])])
    text([(f"last sync {d['sync']}  ·  rendered from the GitHub API", t["dim"])])
    sep()
    text([("FOCUS", t["blue"])])
    text([(d["focus"], t["text"])], x=KEY_X)
    sep()
    text([("ACTIVITY", t["blue"]), ("   (live · profile machinery excluded)", t["dim"])])
    if d["last_push_repo"]:
        kv(
            t["green"], "last_push",
            [(d["last_push_repo"], t["text"]),
             (f"  ·  {d['last_push_when']}", t["dim"])],
        )
    if d["commits_7d"] > 0:
        repo_word = "repo" if d["repos_7d"] == 1 else "repos"
        kv(
            t["green"], "commits_7d",
            [(f"{d['commits_7d']} commits", t["text"]),
             (f"  ·  {d['repos_7d']} {repo_word}  ·  last 7 days", t["dim"])],
        )
    else:
        kv(
            t["amber"], "commits_7d",
            [("0 public commits", t["text"]),
             ("  ·  heads-down week", t["dim"])],
        )
    for lang, pct in d["langs"]:
        bar(lang, pct, t["blue"])
    sep()
    status = d["mission_status"]
    status_color = t["green"] if status == "ACTIVE" else t["amber"]
    text(
        [("● ", status_color), (f"{'mission':<14}", t["dim"]),
         (d["mission"], t["text"]), (f"   [{status}]", status_color)]
    )
    prompt_y = y
    text([("$ ", t["green"])])
    height = y + 6

    cursor = (
        f'<rect x="{PAD_X + 18}" y="{prompt_y - 12}" width="8" height="15" '
        f'fill="{t["text"]}"><animate attributeName="opacity" '
        f'values="1;1;0;0" keyTimes="0;0.5;0.5;1" dur="1.2s" '
        f'repeatCount="indefinite"/></rect>'
    )

    c1, c2, c3 = t["chrome"]
    chrome = (
        f'<circle cx="26" cy="21" r="6" fill="{c1}"/>'
        f'<circle cx="46" cy="21" r="6" fill="{c2}"/>'
        f'<circle cx="66" cy="21" r="6" fill="{c3}"/>'
        f'<text x="86" y="26" font-family="{FONT}" font-size="12" '
        f'fill="{t["dim"]}">kairos · live profile telemetry</text>'
        f'<line x1="1" y1="40" x2="{WIDTH - 1}" y2="40" '
        f'stroke="{t["border"]}" stroke-width="1"/>'
    )

    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" '
        f'height="{height}" viewBox="0 0 {WIDTH} {height}" '
        f'role="img" aria-label="KAIROS live GitHub telemetry for {USER}">'
        f'<rect x="0.5" y="0.5" width="{WIDTH - 1}" height="{height - 1}" '
        f'rx="10" fill="{t["bg"]}" stroke="{t["border"]}"/>'
        f"{chrome}{''.join(parts)}{cursor}</svg>"
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default="out", help="output directory")
    args = ap.parse_args()

    data = collect()
    os.makedirs(args.out, exist_ok=True)
    for theme in THEMES:
        path = os.path.join(args.out, f"kairos-{theme}.svg")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(render(theme, data))
        print(f"wrote {path}")
    print(json.dumps(data, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
