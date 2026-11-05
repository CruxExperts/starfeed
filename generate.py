#!/usr/bin/env python3
"""Build StarFeed: an Atom feed of releases from starred repos, with imagery.

Deterministic: output depends only on GitHub data (no wall-clock values,
stable sorting, stable image selection order).
"""
import base64, json, os, re, sys, urllib.request
from urllib.parse import urljoin
from xml.sax.saxutils import escape

USER = os.environ.get("STAR_USER", "CruxExperts")
TOKEN = os.environ.get("GH_TOKEN", "")
PER_REPO = 10
MAX_ENTRIES = 500
MAX_README_IMAGES = 6
HDRS = {"Accept": "application/vnd.github+json",
        **({"Authorization": f"Bearer {TOKEN}"} if TOKEN else {})}


def api(url, data=None):
    req = urllib.request.Request(url, data=data, headers=HDRS)
    with urllib.request.urlopen(req) as r:
        return json.load(r)


def paged(url):
    page = 1
    while True:
        items = api(f"{url}{'&' if '?' in url else '?'}per_page=100&page={page}")
        if not items:
            return
        yield from items
        page += 1


def social_banner(full_name):
    """Custom social preview if set, else GitHub's generated OpenGraph card."""
    owner, name = full_name.split("/")
    q = ('query($o:String!,$n:String!){repository(owner:$o,name:$n)'
         '{openGraphImageUrl usesCustomOpenGraphImage}}')
    try:
        d = api("https://api.github.com/graphql",
                json.dumps({"query": q, "variables": {"o": owner, "n": name}}).encode())
        return d["data"]["repository"]["openGraphImageUrl"]
    except Exception as e:
        print(f"graphql {full_name}: {e}", file=sys.stderr)
        return f"https://opengraph.githubassets.com/1/{full_name}"


IMG_MD = re.compile(r'!\[[^\]]*\]\(\s*<?([^)\s>]+)')
IMG_HTML = re.compile(r'<img[^>]+src=["\']([^"\']+)["\']', re.I)
SKIP = re.compile(r'shields\.io|badge|travis-ci|codecov|github\.com/[^/]+/[^/]+/(workflows|actions)/', re.I)


def extract_images(text, base):
    found = []
    for m in sorted(list(IMG_MD.finditer(text)) + list(IMG_HTML.finditer(text)),
                    key=lambda m: m.start()):
        url = urljoin(base, m.group(1))
        if url.startswith("http") and not SKIP.search(url) and url not in found:
            found.append(url)
    return found


def readme_images(repo):
    full, branch = repo["full_name"], repo["default_branch"]
    try:
        d = api(f"https://api.github.com/repos/{full}/readme")
        text = base64.b64decode(d["content"]).decode("utf-8", "replace")
    except Exception:
        return []
    base = f"https://raw.githubusercontent.com/{full}/{branch}/"
    return extract_images(text, base)[:MAX_README_IMAGES]


def body_html(body, full):
    try:
        req = urllib.request.Request(
            "https://api.github.com/markdown",
            data=json.dumps({"text": body, "mode": "gfm", "context": full}).encode(),
            headers=HDRS)
        with urllib.request.urlopen(req) as r:
            return r.read().decode()
    except Exception:
        return f"<pre>{escape(body)}</pre>"


MODEL = os.environ.get("SUMMARY_MODEL", "openai/gpt-6-luna")  # latest Luna
CACHE_FILE = "cache/summaries.json"
CATEGORIES = ["security", "breaking", "feature", "fix", "docs", "maintenance", "other"]
try:
    cache = json.load(open(CACHE_FILE))
except Exception:
    cache = {}


def summarize(url, full, title, body):
    """One-sentence summary + category via a cheap model. a
    cache keyed by release URL keep output stable across runs."""
    if url in cache:
        return cache[url]
    result = {"summary": "", "category": "other"}
    if TOKEN and body.strip():
        prompt = (f"Release of {full}: {title}\n\n{body[:6000]}\n\nReply with JSON only: "
                  f'{{"summary": "<one plain sentence>", "category": "<one of {CATEGORIES}>"}}')
        try:
            d = api("https://models.github.ai/inference/chat/completions", json.dumps({
                "model": MODEL, "reasoning_effort": "medium", "max_completion_tokens": 1500,
                "messages": [{"role": "user", "content": prompt}]}).encode())
            raw = d["choices"][0]["message"]["content"].strip().strip("`")
            raw = raw[4:] if raw.startswith("json") else raw
            parsed = json.loads(raw)
            result["summary"] = str(parsed.get("summary", ""))[:400]
            if parsed.get("category") in CATEGORIES:
                result["category"] = parsed["category"]
        except Exception as e:
            print(f"summary {url}: {e}", file=sys.stderr)
            return result  # not cached, retried next run
    cache[url] = result
    return result


entries = []
for repo in sorted(paged(f"https://api.github.com/users/{USER}/starred"),
                   key=lambda r: r["full_name"].lower()):
    full = repo["full_name"]
    try:
        rels = api(f"https://api.github.com/repos/{full}/releases?per_page={PER_REPO}")
    except Exception as e:
        print(f"skip {full}: {e}", file=sys.stderr)
        continue
    rels = [r for r in rels if not r.get("draft")]
    if not rels:
        continue
    banner = social_banner(full)
    readme_imgs = readme_images(repo)
    for r in rels:
        body = r.get("body") or ""
        rel_imgs = extract_images(body, f"https://github.com/{full}/")
        images = [banner] + [i for i in rel_imgs + readme_imgs if i != banner]
        entries.append({
            "repo": full,
            "title": f"{full} {r.get('name') or r['tag_name']}",
            "url": r["html_url"],
            "date": r.get("published_at") or r["created_at"],
            "body": body,
            "images": images[:1 + MAX_README_IMAGES],
            "banner": banner,
            "desc": repo.get("description") or "",
            "prerelease": r.get("prerelease", False),
            **{"ai": summarize(r["html_url"], full, r.get("name") or r["tag_name"], body)},
        })

entries.sort(key=lambda e: (e["date"], e["url"]), reverse=True)
entries = entries[:MAX_ENTRIES]
updated = entries[0]["date"] if entries else "1970-01-01T00:00:00Z"

out = ['<?xml version="1.0" encoding="utf-8"?>',
       '<feed xmlns="http://www.w3.org/2005/Atom" xmlns:media="http://search.yahoo.com/mrss/">',
       "<title>StarFeed</title>",
       f"<subtitle>Releases from repos starred by {escape(USER)}</subtitle>",
       f"<id>tag:github.com,2026:starfeed:{escape(USER)}</id>",
       f"<updated>{updated}</updated>"]
for e in entries:
    imgs = "".join(f'<p><img src="{escape(i)}" alt="" style="max-width:100%"/></p>'
                   for i in e["images"])
    html = (f"<p><b>{escape(e['repo'])}</b>"
            f"{' (pre-release)' if e['prerelease'] else ''}: {escape(e['desc'])}</p>"
            f"{'<p><i>' + escape(e['ai']['summary']) + '</i></p>' if e['ai']['summary'] else ''}"
            f"{imgs}{body_html(e['body'], e['repo']) if e['body'] else ''}")
    out += ["<entry>",
            f"<title>{escape(e['title'])}</title>",
            f'<link href="{escape(e["url"])}"/>',
            f"<id>{escape(e['url'])}</id>",
            f"<updated>{e['date']}</updated>",
            f'<category term="{e["ai"]["category"]}"/>',
            f'<media:thumbnail url="{escape(e["banner"])}"/>']
    out += [f'<media:content url="{escape(i)}" medium="image"/>' for i in e["images"]]
    out += [f'<content type="html">{escape(html)}</content>', "</entry>"]
out.append("</feed>")

os.makedirs("public", exist_ok=True)
with open("public/feed.xml", "w", encoding="utf-8") as f:
    f.write("\n".join(out))
os.makedirs("cache", exist_ok=True)
with open(CACHE_FILE, "w") as f:
    json.dump(cache, f, indent=1, sort_keys=True)
print(f"wrote {len(entries)} entries")
