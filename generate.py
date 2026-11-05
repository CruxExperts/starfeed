#!/usr/bin/env python3
"""Build the "StarFeed": an Atom feed of releases from starred repos."""
import json, os, sys, urllib.request
from datetime import datetime, timezone
from xml.sax.saxutils import escape

USER = os.environ.get("STAR_USER", "CruxExperts")
TOKEN = os.environ.get("GH_TOKEN", "")
MAX_ENTRIES = 100
PER_REPO = 3


def api(url):
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        **({"Authorization": f"Bearer {TOKEN}"} if TOKEN else {}),
    })
    with urllib.request.urlopen(req) as r:
        return json.load(r)


def starred():
    page = 1
    while True:
        items = api(f"https://api.github.com/users/{USER}/starred?per_page=100&page={page}")
        if not items:
            return
        yield from items
        page += 1


entries = []
for repo in starred():
    name = repo["full_name"]
    try:
        rels = api(f"https://api.github.com/repos/{name}/releases?per_page={PER_REPO}")
    except Exception as e:
        print(f"skip {name}: {e}", file=sys.stderr)
        continue
    for r in rels:
        if r.get("draft"):
            continue
        entries.append({
            "repo": name,
            "title": f"{name} {r.get('name') or r['tag_name']}",
            "url": r["html_url"],
            "date": r.get("published_at") or r["created_at"],
            "body": r.get("body") or "",
        })

entries.sort(key=lambda e: e["date"], reverse=True)
entries = entries[:MAX_ENTRIES]
now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

out = ['<?xml version="1.0" encoding="utf-8"?>',
       '<feed xmlns="http://www.w3.org/2005/Atom">',
       "<title>StarFeed</title>",
       f"<subtitle>Releases from repos starred by {escape(USER)}</subtitle>",
       f"<id>tag:github.com,2026:starfeed:{escape(USER)}</id>",
       f"<updated>{now}</updated>"]
for e in entries:
    out += ["<entry>",
            f"<title>{escape(e['title'])}</title>",
            f'<link href="{escape(e["url"])}"/>',
            f"<id>{escape(e['url'])}</id>",
            f"<updated>{e['date']}</updated>",
            f'<content type="text">{escape(e["body"][:2000])}</content>',
            "</entry>"]
out.append("</feed>")

os.makedirs("public", exist_ok=True)
with open("public/feed.xml", "w", encoding="utf-8") as f:
    f.write("\n".join(out))
print(f"wrote {len(entries)} entries")
