#!/usr/bin/env python3
"""Rewrite the marked sections of README.md from live GitHub data.

Two sections, each delimited by HTML comments so the hand-written parts of the
README are never touched:

    <!-- PORTFOLIO:REPOS:START -->     a table of public repos, live star counts
    <!-- PORTFOLIO:ACTIVITY:START -->  the last few things that got shipped

Runs on stdlib only so the workflow needs no install step. Reads GITHUB_TOKEN
from the environment; without one the API still works but at 60 requests/hour.
"""

import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

USER = os.environ.get("PORTFOLIO_USER", "AnkitaSontakke")
README = os.environ.get("PORTFOLIO_README", "README.md")
ACTIVITY_COUNT = 5

# The profile repo itself is the page, not a project on it.
SKIP_REPOS = {USER.lower()}

# Add this topic to any repo on GitHub to keep it off the profile. Doing it
# with a topic rather than a list in here means hiding something later is a
# click on the repo page, not a code change.
HIDE_TOPIC = "hide-from-profile"

API = "https://api.github.com"


def get(path):
    req = urllib.request.Request(
        API + path,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": f"{USER}-portfolio",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def ago(iso):
    """'3 days ago' from a GitHub ISO timestamp."""
    then = datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    seconds = (datetime.now(timezone.utc) - then).total_seconds()
    days = int(seconds // 86400)
    if seconds < 3600:
        return "just now"
    if days < 1:
        return f"{int(seconds // 3600)}h ago"
    if days == 1:
        return "yesterday"
    if days < 30:
        return f"{days} days ago"
    if days < 365:
        months = days // 30
        return f"{months} month{'s' if months > 1 else ''} ago"
    years = days // 365
    return f"{years} year{'s' if years > 1 else ''} ago"


def fetch_repos():
    repos = []
    page = 1
    while True:
        batch = get(f"/users/{USER}/repos?per_page=100&type=owner&sort=pushed&page={page}")
        if not batch:
            break
        repos.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    keep = [
        r
        for r in repos
        if not r["private"]
        and not r["fork"]
        and not r["archived"]
        and r["name"].lower() not in SKIP_REPOS
        and HIDE_TOPIC not in (r.get("topics") or [])
    ]
    # Most-starred first, then most-recently-pushed. Stars are the signal a
    # visitor reads first, but a brand-new repo with 0 stars still deserves to
    # outrank a stale one.
    keep.sort(key=lambda r: (-r["stargazers_count"], -_epoch(r["pushed_at"])))
    return keep


def _epoch(iso):
    return datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()


def render_repos(repos):
    if not repos:
        return "_No public repos yet._"
    lines = [
        "| Project | Stars | Last push |",
        "|---|---|---|",
    ]
    for r in repos:
        desc = (r["description"] or "").strip().replace("|", "\\|")
        name = f"[**{r['name']}**]({r['html_url']})"
        cell = f"{name}<br>{desc}" if desc else name
        lines.append(f"| {cell} | ⭐ {r['stargazers_count']} | {ago(r['pushed_at'])} |")
    return "\n".join(lines)


def fetch_activity():
    """Public events, collapsed into one line per thing that happened."""
    try:
        events = get(f"/users/{USER}/events/public?per_page=100")
    except urllib.error.HTTPError as exc:
        print(f"activity fetch failed: {exc}", file=sys.stderr)
        return []

    out = []
    seen_pushes = set()
    for e in events:
        repo = e["repo"]["name"].split("/")[-1]
        if repo.lower() in SKIP_REPOS:
            continue
        url = f"https://github.com/{e['repo']['name']}"
        when = ago(e["created_at"])
        kind = e["type"]
        payload = e.get("payload", {})

        if kind == "PushEvent":
            commits = [c for c in payload.get("commits", []) if c.get("distinct")]
            if not commits:
                continue
            msg = commits[-1]["message"].splitlines()[0].strip()
            # The bot's own README refresh is not a thing Ankita shipped.
            if msg.startswith("portfolio: refresh"):
                continue
            # Several pushes to one repo in a session read as one line.
            key = (repo, e["created_at"][:10])
            if key in seen_pushes:
                continue
            seen_pushes.add(key)
            n = len(commits)
            out.append(f"**[{repo}]({url})** — pushed {n} commit{'s' if n > 1 else ''}, _{msg}_ · {when}")

        elif kind == "PullRequestEvent":
            pr = payload["pull_request"]
            action = payload["action"]
            if action == "closed" and pr.get("merged"):
                action = "merged"
            elif action not in ("opened", "reopened"):
                continue
            out.append(
                f"**[{repo}]({url})** — {action} PR "
                f"[#{pr['number']}]({pr['html_url']}) _{pr['title']}_ · {when}"
            )

        elif kind == "ReleaseEvent" and payload.get("action") == "published":
            rel = payload["release"]
            out.append(
                f"**[{repo}]({url})** — released "
                f"[{rel.get('tag_name') or rel.get('name')}]({rel['html_url']}) · {when}"
            )

        elif kind == "CreateEvent":
            ref_type, ref = payload.get("ref_type"), payload.get("ref")
            # A repo pushed for the first time logs the default branch being
            # created, not the repo itself, so both spellings mean "new repo".
            if ref_type == "repository" or (ref_type == "branch" and ref in ("main", "master")):
                out.append(f"**[{repo}]({url})** — new repo · {when}")
            elif ref_type == "tag":
                out.append(f"**[{repo}]({url})** — tagged `{ref}` · {when}")
            else:
                continue

        elif kind == "IssuesEvent" and payload.get("action") == "opened":
            issue = payload["issue"]
            out.append(
                f"**[{repo}]({url})** — opened issue "
                f"[#{issue['number']}]({issue['html_url']}) _{issue['title']}_ · {when}"
            )

        if len(out) >= ACTIVITY_COUNT:
            break
    return out


def render_activity(items):
    if not items:
        return "_Nothing public in the last 90 days._"
    return "\n".join(f"- {i}" for i in items)


def replace_section(text, key, body):
    start, end = f"<!-- PORTFOLIO:{key}:START -->", f"<!-- PORTFOLIO:{key}:END -->"
    pattern = re.compile(
        re.escape(start) + r".*?" + re.escape(end),
        re.DOTALL,
    )
    if not pattern.search(text):
        raise SystemExit(f"markers for {key} not found in {README}")
    return pattern.sub(f"{start}\n{body}\n{end}", text)


def main():
    with open(README, encoding="utf-8") as f:
        original = f.read()

    updated = replace_section(original, "REPOS", render_repos(fetch_repos()))
    updated = replace_section(updated, "ACTIVITY", render_activity(fetch_activity()))

    if updated == original:
        print("no change")
        return

    with open(README, "w", encoding="utf-8") as f:
        f.write(updated)
    print("README updated")


if __name__ == "__main__":
    main()
