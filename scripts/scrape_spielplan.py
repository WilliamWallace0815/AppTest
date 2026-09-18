#!/usr/bin/env python3
"""Scrapes the upcoming fixtures for USK St. Koloman from ligaportal.at
and writes them as JSON for the static site to render.

The source page is a JS-driven ticker app, so we first look for an
embedded state blob (Next.js/__NEXT_DATA__ or __NUXT__ style) that
frameworks commonly inline for server-side rendering, and fall back to
parsing the rendered HTML table if no such blob is present.
"""
import json
import re
import sys
from datetime import datetime, date

import requests
from bs4 import BeautifulSoup

SOURCE_URL = "https://ticker.ligaportal.at/mannschaft/507/usk-st-koloman/spielplan"
OUTPUT_PATH = "data/spielplan.json"
TEAM_NAME_HINT = "koloman"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}

DATE_RE = re.compile(r"(\d{1,2})\.(\d{1,2})\.(\d{2,4})")
TIME_RE = re.compile(r"(\d{1,2}):(\d{2})")


def fetch_html(url: str) -> str:
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.text


def find_embedded_json(html: str):
    """Looks for common SSR state blobs embedded by JS frameworks."""
    patterns = [
        r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>',
        r"window\.__NUXT__\s*=\s*(\{.*?\});?\s*</script>",
        r"window\.__INITIAL_STATE__\s*=\s*(\{.*?\});?\s*</script>",
    ]
    for pattern in patterns:
        match = re.search(pattern, html, re.DOTALL)
        if not match:
            continue
        blob = match.group(1)
        try:
            return json.loads(blob)
        except json.JSONDecodeError:
            print(f"[warn] found blob for pattern {pattern!r} but it wasn't valid JSON", file=sys.stderr)
    return None


def walk_for_fixture_dicts(node, found):
    """Recursively walks a parsed JSON tree, collecting dicts that look
    like a single fixture (contain both a home- and away-team-ish key)."""
    if isinstance(node, dict):
        keys = {k.lower() for k in node.keys()}
        home_keys = {"hometeam", "heim", "heimteam", "team1"}
        away_keys = {"awayteam", "gast", "gastteam", "team2"}
        if keys & home_keys and keys & away_keys:
            found.append(node)
        for value in node.values():
            walk_for_fixture_dicts(value, found)
    elif isinstance(node, list):
        for item in node:
            walk_for_fixture_dicts(item, found)


def normalize_from_json_fixture(fixture: dict):
    def get_any(d, keys):
        for k in keys:
            for actual_key in d:
                if actual_key.lower() == k:
                    return d[actual_key]
        return None

    home = get_any(fixture, ["hometeam", "heim", "heimteam", "team1"])
    away = get_any(fixture, ["awayteam", "gast", "gastteam", "team2"])
    raw_date = get_any(fixture, ["date", "datum", "kickoff", "spieltermin"])
    venue = get_any(fixture, ["venue", "ort", "spielort", "location"])
    competition = get_any(fixture, ["competition", "liga", "bewerb", "league"])

    if isinstance(home, dict):
        home = home.get("name") or home.get("bezeichnung")
    if isinstance(away, dict):
        away = away.get("name") or away.get("bezeichnung")

    if not home or not away:
        return None

    parsed_date, parsed_time = parse_date_time_text(str(raw_date)) if raw_date else (None, None)

    return {
        "date": parsed_date,
        "time": parsed_time,
        "home": str(home).strip(),
        "away": str(away).strip(),
        "venue": str(venue).strip() if venue else None,
        "competition": str(competition).strip() if competition else None,
    }


def parse_date_time_text(text: str):
    date_match = DATE_RE.search(text)
    time_match = TIME_RE.search(text)
    parsed_date = None
    if date_match:
        day, month, year = date_match.groups()
        year = int(year)
        if year < 100:
            year += 2000
        try:
            parsed_date = date(year, int(month), int(day)).isoformat()
        except ValueError:
            parsed_date = None
    parsed_time = f"{time_match.group(1).zfill(2)}:{time_match.group(2)}" if time_match else None
    return parsed_date, parsed_time


def extract_from_json(html: str):
    data = find_embedded_json(html)
    if data is None:
        return []
    candidates = []
    walk_for_fixture_dicts(data, candidates)
    fixtures = []
    for candidate in candidates:
        normalized = normalize_from_json_fixture(candidate)
        if normalized:
            fixtures.append(normalized)
    return fixtures


def extract_from_html_table(html: str):
    """Fallback: parse rendered HTML for rows that look like fixtures.
    This only works if the page ships server-rendered markup; if the
    ticker app is fully client-side rendered, this will find nothing
    and the caller falls back to an empty result."""
    soup = BeautifulSoup(html, "html.parser")
    fixtures = []

    rows = soup.find_all(["tr", "li", "div"], class_=re.compile(r"(spiel|match|fixture|game|row)", re.I))
    for row in rows:
        text = row.get_text(" ", strip=True)
        if not text or len(text) > 300:
            continue
        if "-" not in text and ":" not in text:
            continue
        date_str, time_str = parse_date_time_text(text)
        if not date_str:
            continue
        vs_match = re.search(r"([A-Za-zÄÖÜäöüß0-9.\- ]{3,40})\s+(?:-|:|vs\.?)\s+([A-Za-zÄÖÜäöüß0-9.\- ]{3,40})", text)
        if not vs_match:
            continue
        home, away = vs_match.group(1).strip(), vs_match.group(2).strip()
        fixtures.append({
            "date": date_str,
            "time": time_str,
            "home": home,
            "away": away,
            "venue": None,
            "competition": None,
        })
    return fixtures


def main():
    try:
        html = fetch_html(SOURCE_URL)
    except requests.RequestException as exc:
        write_output([], error=f"Fetch failed: {exc}")
        print(f"[error] fetch failed: {exc}", file=sys.stderr)
        sys.exit(0)  # don't fail the workflow; keep last known good data

    fixtures = extract_from_json(html)
    strategy = "embedded-json"
    if not fixtures:
        fixtures = extract_from_html_table(html)
        strategy = "html-table"
    if not fixtures:
        strategy = "none"

    print(f"[info] extraction strategy: {strategy}, raw fixtures found: {len(fixtures)}", file=sys.stderr)

    today = date.today().isoformat()
    upcoming = [f for f in fixtures if f["date"] and f["date"] >= today]
    upcoming.sort(key=lambda f: (f["date"], f["time"] or "99:99"))
    next_three = upcoming[:3]

    if not next_three:
        write_output([], error="No upcoming fixtures could be extracted from the source page.")
        print("[warn] no upcoming fixtures found; check the source page structure", file=sys.stderr)
        return

    write_output(next_three)
    print(f"[info] wrote {len(next_three)} upcoming fixtures to {OUTPUT_PATH}")


def write_output(games, error=None):
    import os
    os.makedirs("data", exist_ok=True)
    payload = {
        "updatedAt": datetime.utcnow().isoformat() + "Z",
        "source": SOURCE_URL,
        "games": games,
    }
    if error:
        payload["error"] = error
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
