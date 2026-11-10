"""Builds the Morning Edition: a one-page daily newspaper written to site/index.html."""
import html
import json
import random
import re
import shutil
import sys
import webbrowser
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import trafilatura

HERE = Path(__file__).parent
SITE = HERE / "site"
TZ = ZoneInfo("America/Chicago")
UA = {"User-Agent": "Mozilla/5.0 (morning-edition)"}

LAT, LON = 35.2226, -97.4395  # Norman, OK
MAX_AGE = timedelta(hours=24)
MIN_WORDS = 250  # skip pages that didn't extract into a real article

TICKERS = {
    "S&P 500": "^GSPC",
    "Nasdaq": "^IXIC",
    "Dow": "^DJI",
    "Nokia": "NOK",
    "Bitcoin": "BTC-USD",
}

# (section, source kind, url, slots). Order here is the order on the page.
# If a section has nothing new, its slots go to whatever sections still have stories left.
SECTIONS = [
    ("Thunder", "espn", "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/news?team=25&limit=20", 2),
    ("World", "rss", "https://feeds.bbci.co.uk/news/world/rss.xml", 2),
    ("AI & Tech", "rss", "https://hnrss.org/frontpage?points=300", 3),
    ("Markets", "rss", "https://www.cnbc.com/id/100003114/device/rss/rss.html", 1),
    ("NBA", "espn", "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/news?limit=30", 1),
    ("Overwatch", "rss", "https://www.dexerto.com/feed/category/overwatch/", 1),
]
TOTAL_ARTICLES = 10
SKIP_DOMAINS = ("github.com", "youtube.com", "x.com", "twitter.com", ".pdf")


def get(url):
    r = requests.get(url, headers=UA, timeout=20)
    r.raise_for_status()
    return r


def safe(fn, *args, fallback=None):
    # one broken source shouldn't kill the paper
    try:
        return fn(*args)
    except Exception as e:
        print(f"[skip] {fn.__name__}{args[:1]}: {e}", file=sys.stderr)
        return fallback


def recent(when):
    return datetime.now(timezone.utc) - when <= MAX_AGE


# ---------- articles ----------

def rss_candidates(url):
    from xml.etree import ElementTree as ET
    root = ET.fromstring(get(url).content)
    out = []
    for item in root.iter("item"):
        pub = item.findtext("pubDate")
        when = parsedate_to_datetime(pub) if pub else None
        if when and when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        if when and not recent(when):
            continue
        out.append({"title": item.findtext("title", "").strip(), "link": item.findtext("link", "").strip()})
    return out


def rss_body(c):
    link = c["link"]
    if any(d in link for d in SKIP_DOMAINS):
        return None
    page = get(link).text
    text = trafilatura.extract(page, include_comments=False, include_tables=False)
    meta = trafilatura.extract_metadata(page)
    source = (meta.sitename if meta and meta.sitename else re.sub(r"^www\.", "", link.split("/")[2]))
    return {"paras": [p.strip() for p in (text or "").split("\n") if p.strip()], "source": source}


def espn_candidates(url):
    out = []
    for a in get(url).json().get("articles", []):
        if a.get("type") not in ("Story", "HeadlineNews", "Preview", "Recap"):
            continue
        when = datetime.fromisoformat(a["published"].replace("Z", "+00:00"))
        api = a.get("links", {}).get("api", {}).get("self", {}).get("href")
        if api and recent(when) and not re.search(r"(?i)fantasy|\bbets?\b|betting|odds", a["headline"]):
            out.append({"title": a["headline"], "link": api})
    return out


def espn_body(c):
    story = get(c["link"]).json()["headlines"][0].get("story", "")
    story = re.sub(r"(?i)<br\s*/?>|</p>|</h\d>", "\n", story)
    story = html.unescape(re.sub(r"<[^>]+>", "", story))
    return {"paras": [p.strip() for p in story.split("\n") if p.strip()], "source": "ESPN"}


def gather_articles():
    pools = []
    for name, kind, url, slots in SECTIONS:
        cands = safe(rss_candidates if kind == "rss" else espn_candidates, url, fallback=[])
        pools.append({"name": name, "kind": kind, "slots": slots, "cands": cands, "got": []})

    seen = set()

    def take_one(pool):
        while pool["cands"]:
            c = pool["cands"].pop(0)
            key = c["title"].lower()
            if key in seen or not c["link"]:
                continue
            body = safe(rss_body if pool["kind"] == "rss" else espn_body, c)
            if body:
                # drop the headline repeated as a paragraph and leftover bylines/labels
                paras = [p for p in body["paras"] if p.lower() != key and len(p.split()) >= 3]
                # bbc tacks "related stories" on the end, each marked "- Published"
                cut = next((n for n, p in enumerate(paras) if n >= 3 and p.startswith("- Published")), None)
                body["paras"] = [p for p in paras[:cut] if not p.startswith("- Published")]
            if body and sum(len(p.split()) for p in body["paras"]) >= MIN_WORDS:
                seen.add(key)
                pool["got"].append({**c, **body, "section": pool["name"]})
                return True
        return False

    # first pass: each section fills its own slots
    for pool in pools:
        for _ in range(pool["slots"]):
            if not take_one(pool):
                break
    # second pass: hand empty slots to sections that still have stories
    count = sum(len(p["got"]) for p in pools)
    while count < TOTAL_ARTICLES:
        progressed = False
        for pool in pools:
            if count < TOTAL_ARTICLES and take_one(pool):
                count += 1
                progressed = True
        if not progressed:
            break
    return [a for p in pools for a in p["got"]]


# ---------- boxes ----------

def weather():
    pts = get(f"https://api.weather.gov/points/{LAT},{LON}").json()["properties"]
    periods = get(pts["forecast"]).json()["properties"]["periods"]
    return periods[:6]


def markets():
    rows = []
    for label, sym in TICKERS.items():
        res = safe(lambda s: get(f"https://query1.finance.yahoo.com/v8/finance/chart/{s}?range=5d&interval=1d").json(), sym)
        try:
            closes = [c for c in res["chart"]["result"][0]["indicators"]["quote"][0]["close"] if c is not None]
            last, prev = closes[-1], closes[-2]
            rows.append((label, last, (last - prev) / prev * 100))
        except Exception:
            continue
    return rows


def on_this_day():
    t = datetime.now(TZ)
    url = f"https://api.wikimedia.org/feed/v1/wikipedia/en/onthisday/selected/{t.month:02d}/{t.day:02d}"
    events = get(url).json().get("selected", [])
    random.Random(t.date().toordinal()).shuffle(events)
    return sorted(events[:3], key=lambda e: e["year"])


def philosophy():
    # rotate authors daily, then walk through each author's passages
    passages = json.loads((HERE / "passages.json").read_text(encoding="utf-8"))
    by_author = {}
    for p in passages:
        by_author.setdefault(p["author"], []).append(p)
    authors = list(by_author)
    n = datetime.now(TZ).date().toordinal()
    pool = by_author[authors[n % len(authors)]]
    return pool[(n // len(authors)) % len(pool)]


# ---------- page ----------

def e(s):
    return html.escape(str(s))


def read_time(paras):
    return max(1, round(sum(len(p.split()) for p in paras) / 230))


def render_weather(periods):
    if not periods:
        return ""
    now = periods[0]
    rest = "".join(
        f"<li><b>{e(p['name'])}</b> {e(p['temperature'])}° · {e(p['shortForecast'])}</li>" for p in periods[1:]
    )
    return f"""<section class="box">
<h3>Weather · Norman</h3>
<div class="temp">{e(now['temperature'])}°</div>
<p class="fc">{e(now['name'])}: {e(now['detailedForecast'])}</p>
<ul class="mini">{rest}</ul></section>"""


def render_markets(rows):
    if not rows:
        return ""
    trs = "".join(
        f"<tr><td>{e(l)}</td><td>{v:,.2f}</td><td class='{'up' if ch >= 0 else 'dn'}'>{'▲' if ch >= 0 else '▼'} {abs(ch):.2f}%</td></tr>"
        for l, v, ch in rows
    )
    return f'<section class="box"><h3>Markets · last close</h3><table class="mk">{trs}</table></section>'


def render_otd(events):
    if not events:
        return ""
    lis = "".join(f"<li><b>{e(ev['year'])}</b> {e(ev['text'])}</li>" for ev in events)
    return f'<section class="box"><h3>On This Day</h3><ul class="otd">{lis}</ul></section>'


def render_philosophy(p):
    if not p:
        return ""
    paras = "".join(f"<p>{e(x)}</p>" for x in p["paras"])
    sub = f"{e(p['work'])}" + (f" · {e(p['title'])}" if p["title"] else "")
    return f"""<article class="reflection" id="reflection">
<div class="kicker">The Daily Reflection · {read_time(p['paras'])} min read</div>
<h2>{e(p['author'])}</h2><div class="byline">{sub}</div>
<div class="body">{paras}</div></article>"""


def render_article(a, i):
    paras = "".join(f"<p>{e(x)}</p>" for x in a["paras"])
    cls = "story lead" if i == 0 else "story"
    return f"""<article class="{cls}" id="a{i}">
<div class="kicker">{e(a['section'])} · {e(a['source'])} · {read_time(a['paras'])} min read</div>
<h2>{e(a['title'])}</h2>
<div class="body">{paras}</div>
<a class="top" href="#contents">↑ contents</a></article>"""


# ---------- audio ----------

VOICE = "en-US-AndrewNeural"
BYTES_PER_SEC = 48000 / 8  # edge-tts default output is 48 kbps constant-bitrate mp3


def make_audio(segments):
    # one mp3 for the whole edition so it keeps playing with the screen locked;
    # chapters mark where each piece starts so next/prev can jump between them
    import asyncio
    import edge_tts

    async def record(n, text, gate):
        async with gate:
            for attempt in range(3):
                try:
                    buf = bytearray()
                    async for chunk in edge_tts.Communicate(text, VOICE).stream():
                        if chunk["type"] == "audio":
                            buf += chunk["data"]
                    return bytes(buf)
                except Exception as e:
                    print(f"[retry] audio segment {n} attempt {attempt + 1}: {e}", file=sys.stderr)
                    await asyncio.sleep(3)
            return b""

    def pieces(text, limit=1200):
        # short requests fail less often, and a failed piece costs a paragraph, not an article
        out, cur = [], ""
        for para in text.split("\n"):
            if cur and len(cur) + len(para) > limit:
                out.append(cur)
                cur = ""
            cur = f"{cur}\n{para}" if cur else para
        return out + [cur] if cur else out

    async def record_all():
        gate = asyncio.Semaphore(8)
        jobs = [[record(n, p, gate) for p in pieces(s[2])] for n, s in enumerate(segments)]
        done = await asyncio.gather(*(asyncio.gather(*j) for j in jobs))
        return [b"".join(parts) for parts in done]

    clips = asyncio.run(record_all())
    audio, chapters = bytearray(), []
    for (sid, title, _), clip in zip(segments, clips):
        if not clip:
            continue  # a piece that wouldn't record is skipped, the rest still plays
        chapters.append({"id": sid, "title": title, "start": round(len(audio) / BYTES_PER_SEC, 2)})
        audio += clip
    (SITE / "edition.mp3").write_bytes(audio)
    return chapters


PLAYER_JS = """
const A=document.getElementById('aud'),CH=JSON.parse(document.getElementById('chapters').textContent),
 T=document.getElementById('ptitle'),P=document.getElementById('pplay'),BAR=document.getElementById('pbar'),
 KEY='pos-'+document.body.dataset.ed;
let cur=-1;
const idx=t=>{let i=0;CH.forEach((c,n)=>{if(t>=c.start)i=n});return i};
function go(i,play=true){i=Math.max(0,Math.min(CH.length-1,i));
 const seek=()=>{A.currentTime=CH[i].start;if(play)A.play()};
 A.readyState>=1?seek():A.addEventListener('loadedmetadata',seek,{once:true});A.load&&A.readyState<1&&A.load()}
function sync(){const i=idx(A.currentTime);
 if(i!==cur){cur=i;T.textContent=CH[i].title;
  if('mediaSession'in navigator)navigator.mediaSession.metadata=new MediaMetadata({title:CH[i].title,
   artist:'The Morning Edition',artwork:[{src:'icon.png',sizes:'512x512',type:'image/png'}]})}
 if(A.duration)BAR.style.width=(A.currentTime/A.duration*100)+'%';
 try{localStorage.setItem(KEY,A.currentTime)}catch(e){}}
A.addEventListener('timeupdate',sync);
A.addEventListener('play',()=>P.textContent='❚❚');A.addEventListener('pause',()=>P.textContent='▶');
P.onclick=()=>{if(A.paused){if(A.currentTime===0){let s=0;try{s=+localStorage.getItem(KEY)||0}catch(e){}
 if(s){A.currentTime=s}}A.play()}else A.pause()};
document.getElementById('pprev').onclick=()=>{const i=idx(A.currentTime);
 go(A.currentTime-CH[i].start>3?i:i-1)};
document.getElementById('pnext').onclick=()=>go(idx(A.currentTime)+1);
document.querySelectorAll('[data-ch]').forEach(b=>b.onclick=()=>go(+b.dataset.ch));
if('mediaSession'in navigator){const m=navigator.mediaSession;
 m.setActionHandler('play',()=>A.play());m.setActionHandler('pause',()=>A.pause());
 m.setActionHandler('previoustrack',()=>document.getElementById('pprev').click());
 m.setActionHandler('nexttrack',()=>go(idx(A.currentTime)+1));}
"""


def render_player(chapters, now):
    if not chapters:
        return ""
    return f"""<div class="player"><div class="pbar"><i id="pbar"></i></div><div class="prow">
<button id="pprev" aria-label="Previous">⏮</button><button id="pplay" class="pp" aria-label="Play">▶</button>
<button id="pnext" aria-label="Next">⏭</button><span id="ptitle">Listen to today's edition</span></div></div>
<audio id="aud" preload="none" src="edition.mp3?v={now:%Y%m%d}"></audio>
<script type="application/json" id="chapters">{json.dumps(chapters)}</script>
<script>{PLAYER_JS}</script>"""


CSS = """
:root{--paper:#f5f0e4;--ink:#1a1a1a;--rule:#1a1a1a;--muted:#5b554a;--up:#1d5c2e;--dn:#8a1c1c}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%;touch-action:pan-x pan-y}
body{margin:0;background:var(--paper);color:var(--ink);font:17px/1.55 'Libre Caslon Text',Georgia,serif;
 padding:env(safe-area-inset-top) 16px 40px}
.wrap{max-width:860px;margin:0 auto}
header.mast{text-align:center;border-bottom:4px double var(--rule);padding:18px 0 6px}
.mast h1{font:900 clamp(34px,10vw,64px)/1 'UnifrakturMaguntia','Old English Text MT',serif;margin:6px 0}
.dateline{display:flex;justify-content:space-between;font:12px/1.4 'Libre Caslon Text',serif;
 text-transform:uppercase;letter-spacing:.08em;border-top:1px solid var(--rule);padding-top:6px;margin-top:6px}
.boxes{display:grid;grid-template-columns:1fr;gap:0;border-bottom:1px solid var(--rule)}
@media(min-width:700px){.boxes{grid-template-columns:repeat(3,1fr)}.box+.box{border-left:1px solid var(--rule)}
 .story .body,.reflection .body{column-count:2;column-gap:28px;column-rule:1px solid #c9c1ad}}
.box{padding:12px 10px}
.box h3,.contents h3{font:700 12px/1 'Libre Caslon Text',serif;text-transform:uppercase;letter-spacing:.12em;
 margin:0 0 8px;border-bottom:1px solid var(--rule);padding-bottom:5px}
.temp{font:700 44px/1 'Playfair Display',serif}
.fc{font-size:14px;margin:6px 0}
.mini,.otd{list-style:none;padding:0;margin:0;font-size:13.5px}
.mini li,.otd li{padding:3px 0;border-top:1px dotted #b8ae98}
.mk{width:100%;border-collapse:collapse;font-size:14px;font-variant-numeric:tabular-nums}
.mk td{padding:4px 0;border-top:1px dotted #b8ae98}.mk td:nth-child(n+2){text-align:right}
.up{color:var(--up)}.dn{color:var(--dn)}
.contents{padding:12px 0;border-bottom:3px double var(--rule)}
.contents ol{margin:0;padding-left:22px;font-size:15px}.contents li{padding:2px 0}
.contents a{color:inherit;text-decoration:none}.contents .sec{font-size:11px;text-transform:uppercase;
 letter-spacing:.08em;color:var(--muted)}
article{padding:18px 0;border-bottom:1px solid var(--rule)}
.kicker{font:11px/1.3 'Libre Caslon Text',serif;text-transform:uppercase;letter-spacing:.12em;color:var(--muted)}
article h2{font:700 26px/1.15 'Playfair Display',serif;margin:6px 0 10px}
.lead h2{font-size:clamp(30px,7vw,44px)}
.body p{margin:0 0 .8em;text-align:justify;hyphens:auto;-webkit-hyphens:auto}
.lead .body>p:first-child::first-letter,.reflection .body>p:first-child::first-letter{float:left;
 font:700 3.4em/.8 'Playfair Display',serif;padding:6px 6px 0 0}
.reflection{background:#ece5d3;padding:18px 14px;margin:18px 0;border:1px solid var(--rule)}
.reflection h2{margin-bottom:2px}.byline{font-style:italic;color:var(--muted);margin-bottom:12px}
.top{display:inline-block;margin-top:6px;font-size:12px;color:var(--muted);text-decoration:none}
footer{text-align:center;font-size:12px;color:var(--muted);padding:20px 0 90px}
.listen{font:700 11px/1 'Libre Caslon Text',serif;text-transform:uppercase;letter-spacing:.1em;
 background:none;border:1px solid var(--ink);color:var(--ink);padding:6px 10px;margin:0 0 12px;cursor:pointer}
.player{position:fixed;left:0;right:0;bottom:0;background:var(--ink);color:var(--paper);
 padding:0 16px calc(10px + env(safe-area-inset-bottom))}
.pbar{height:3px;background:#444;margin:0 -16px 8px}.pbar i{display:block;height:100%;width:0;background:var(--paper)}
.prow{display:flex;align-items:center;gap:10px;max-width:860px;margin:0 auto}
.prow button{background:none;border:0;color:var(--paper);font-size:20px;padding:6px;cursor:pointer}
.prow .pp{width:44px;height:44px;border:1px solid var(--paper);border-radius:50%;font-size:16px}
#ptitle{font-size:14px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;flex:1}
"""


def build_page():
    now = datetime.now(TZ)
    articles = gather_articles()
    wx = safe(weather, fallback=[])
    phil = safe(philosophy)
    boxes = (
        render_weather(wx)
        + render_markets(safe(markets, fallback=[]))
        + render_otd(safe(on_this_day, fallback=[]))
    )
    reflection = render_philosophy(phil)

    # listening order matches reading order: intro, lead, reflection, the rest
    intro = f"Good morning. This is the Morning Edition for {now:%A}, {now:%B} {now.day}."
    if wx:
        intro += f" Weather in Norman. {wx[0]['name']}: {wx[0]['detailedForecast']}"
    segs = [("top", "Today's weather", intro)]
    for i, a in enumerate(articles):
        segs.append((f"a{i}", a["title"], f"{a['section']}. {a['title']}.\n" + "\n".join(a["paras"])))
        if i == 0 and phil:
            segs.append(("reflection", f"Reflection · {phil['author']}",
                         f"The daily reflection. From {phil['author']}, {phil['work']}.\n" + "\n".join(phil["paras"])))
    chapters = safe(make_audio, segs, fallback=[]) if "--no-audio" not in sys.argv else []
    player = render_player(chapters, now)
    ch_index = {c["id"]: n for n, c in enumerate(chapters)}

    def listen(sid):
        return f'<button class="listen" data-ch="{ch_index[sid]}">▶ Listen</button>' if sid in ch_index else ""

    reflection = reflection.replace('<div class="body">', listen("reflection") + '<div class="body">', 1)
    contents = "".join(
        f"<li><a href='#a{i}'><span class='sec'>{e(a['section'])}</span> {e(a['title'])}</a></li>"
        for i, a in enumerate(articles)
    ) + "<li><a href='#reflection'><span class='sec'>Reflection</span> The Daily Reflection</a></li>"
    stories = [render_article(a, i).replace('<div class="body">', listen(f"a{i}") + '<div class="body">', 1)
               for i, a in enumerate(articles)]
    # reflection sits right after the lead story
    body = "".join(stories[:1]) + reflection + "".join(stories[1:])
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no, viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="default">
<meta name="apple-mobile-web-app-title" content="Morning">
<meta name="theme-color" content="#f5f0e4">
<link rel="apple-touch-icon" href="icon.png"><link rel="manifest" href="manifest.json">
<title>The Morning Edition</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Libre+Caslon+Text:ital,wght@0,400;0,700;1,400&family=Playfair+Display:wght@700;900&family=UnifrakturMaguntia&display=swap" rel="stylesheet">
<style>{CSS}</style>
<script>document.addEventListener("gesturestart",function(e){{e.preventDefault()}})</script></head><body data-ed="{now:%Y%m%d}"><div class="wrap">
<header class="mast"><h1>The Morning Edition</h1>
<div class="dateline"><span>{now:%A}</span><span>{now:%B} {now.day}, {now.year}</span><span>Norman, Okla.</span></div></header>
<div class="boxes">{boxes}</div>
<nav class="contents" id="contents"><h3>In Today's Edition</h3><ol>{contents}</ol></nav>
{body}
<footer>Printed {now:%-I:%M %p} · {len(articles)} stories</footer>
</div>{player}</body></html>"""


if __name__ == "__main__":
    SITE.mkdir(exist_ok=True)
    for f in ("icon.png", "manifest.json"):
        shutil.copy(HERE / "static" / f, SITE / f)
    out = SITE / "index.html"
    out.write_text(build_page(), encoding="utf-8")
    print(f"wrote {out}")
    if "--no-open" not in sys.argv:
        webbrowser.open(out.as_uri())
