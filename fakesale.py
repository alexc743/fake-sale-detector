#!/usr/bin/env python3
"""
Fake Sale Detector — is that "WAS £80, NOW £40" real?

Paste a UK product URL. It uses TinyFish to:
  1. FETCH the live page            -> product name + today's price
  2. FETCH the Wayback Machine      -> list of archived copies of that exact page
  3. FETCH old snapshots            -> what the price REALLY was 1-12 months ago
  4. SEARCH the product name        -> same product at other retailers
  5. AGENT on the best competitor   -> reads their live price (JS-heavy pages)
  6. Verdict + HTML report card     -> report.html

Usage:
  export TINYFISH_API_KEY=...
  python fakesale.py "https://www.argos.co.uk/product/3284627" --was 449.95
"""
import argparse
import datetime as dt
import html
import json
import os
import re
import statistics
import sys
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse, quote

import requests

API_KEY = os.environ.get("TINYFISH_API_KEY", "")
HEADERS = {"X-API-Key": API_KEY, "Content-Type": "application/json"}
SEARCH_URL = "https://api.search.tinyfish.ai"
FETCH_URL = "https://api.fetch.tinyfish.ai"
AGENT_URL = "https://agent.tinyfish.ai/v1/automation/run"

DEBUG = os.environ.get("FAKESALE_DEBUG") == "1"
DEBUG_LOG = []

PRICE_HEADING =re.compile(r"#+\s*£\s?([\d,]+(?:\.\d{2})?)")
PRICE_ANY = re.compile(r"£\s?([\d,]+(?:\.\d{2})?)")


def log(msg):
    print(f"  → {msg}", flush=True)


# ---------------------------------------------------------------- TinyFish calls
def tf_fetch(urls, ttl=None, exclude=None):
    body = {"urls": urls, "format": "markdown"}
    if ttl is not None:
        body["ttl"] = ttl
    if exclude:
        body["exclude_selectors"] = exclude
    r = requests.post(FETCH_URL, headers=HEADERS, json=body, timeout=150)
    r.raise_for_status()
    data = r.json()
    if DEBUG:
        DEBUG_LOG.append({"request": body, "response": data})
    for e in data.get("errors") or []:
        log(f"Fetch error: {str(e)[:160]}")
    results = data.get("results") or []

    # Return results in the SAME ORDER as `urls` (the API may normalise URLs,
    # so match loosely on url/final_url, then fall back to position).
    def norm(u):
        return re.sub(r"^https?://(www\.)?|/$", "", (u or "").lower()).replace("%2f", "/")

    out = []
    for i, u in enumerate(urls):
        hit = next((res for res in results
                    if norm(u) in (norm(res.get("url")), norm(res.get("final_url")))), None)
        if hit is None and len(results) == len(urls):
            hit = results[i]
        out.append(hit or {})
    return out


def tf_search(query, exclude_domain=None):
    params = {"query": query, "location": "GB", "language": "en"}
    if exclude_domain:
        params["exclude_domains"] = exclude_domain
    r = requests.get(SEARCH_URL, headers=HEADERS, params=params, timeout=30)
    r.raise_for_status()
    return r.json().get("results", [])


def tf_agent(url, goal):
    body = {
        "url": url,
        "goal": goal,
        "browser_profile": "stealth",
        "proxy_config": {"enabled": True, "country_code": "GB"},
    }
    r = requests.post(AGENT_URL, headers=HEADERS, json=body, timeout=180)
    r.raise_for_status()
    data = r.json()
    res = data.get("result") or data.get("result_json") or {}
    if isinstance(res, str):
        try:
            res = json.loads(res)
        except json.JSONDecodeError:
            res = {"raw": res}
    return res


# ---------------------------------------------------------------- helpers
NOISE = [
    "nav", "header", "footer", "#megaMenu", "[class*='MegaMenu']",
    "[class*='navigation']", "[data-test*='review']",
]


def parse_price(text):
    """Main product price = first price shown as a heading, else first £ amount."""
    if not text:
        return None
    m = PRICE_HEADING.search(text) or PRICE_ANY.search(text)
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


def parse_name(res):
    text = (res or {}).get("text") or ""
    m = re.search(r"^#\s+(.+)$", text, re.M)
    if m:
        return m.group(1).strip()
    title = (res or {}).get("title") or ""
    return re.sub(r"^Buy\s+|\s*\|.*$", "", title).strip() or "Unknown product"


def strip_url(u):
    p = urlparse(u)
    return f"{p.netloc}{p.path}".removeprefix("www.")


# ---------------------------------------------------------------- pipeline
def live_price(url):
    log("Fetch: reading the live product page")
    res = tf_fetch([url], ttl=0, exclude=NOISE)[0]
    if parse_price(res.get("text")) is None:
        res = tf_fetch([url])[0] or res  # retry: no selectors, cache allowed
    name, price = parse_name(res), parse_price(res.get("text"))
    if price is None:
        log("Fetch couldn't read the price — sending the Agent in")
        a = tf_agent(url, "Read this product page. Return JSON with product_name "
                          "and current_price (number, GBP). Do not add to basket.")
        name = a.get("product_name", name)
        price = a.get("current_price")
    return name, price


def price_history(url, max_points=6):
    log("Fetch: asking the Wayback Machine for archived copies")
    since = (dt.date.today() - dt.timedelta(days=550)).strftime("%Y%m%d")
    target = strip_url(url)
    cdx = (f"https://web.archive.org/cdx/search/cdx?url={target}&output=json"
           f"&from={since}&filter=statuscode:200&collapse=timestamp:6")
    rows = []
    for attempt in range(2):  # archive.org's CDX server is flaky — retry once
        raw = (tf_fetch([cdx])[0].get("text") or "").replace("\\_", "_")
        # rows look like ["urlkey","20250813022257","https://...","text/html","200",...]
        rows = re.findall(r'"(\d{14})"\s*,\s*"(https?://[^"]+)"', raw)
        if rows:
            break

    if not rows:  # fallback: Wayback "closest snapshot" API, probed every ~month
        log("CDX list empty — probing Wayback 'closest snapshot' month by month")
        today = dt.date.today()
        probes = [f"https://archive.org/wayback/available?url={target}&timestamp="
                  f"{(today - dt.timedelta(days=d)).strftime('%Y%m%d')}"
                  for d in range(30, 541, 30)]
        found = []
        for i in range(0, len(probes), 10):  # Fetch takes max 10 URLs per call
            found += tf_fetch(probes[i:i + 10])
        for res in found:
            m = re.search(r"web\.archive\.org/web/(\d{14})/(https?://[^\"\s]+)",
                          (res.get("text") or "").replace("\\_", "_"))
            if m and m.groups() not in rows:
                rows.append(m.groups())
        rows.sort()

    if not rows:
        log("No archived copies found")
        return []
    rows = rows[-max_points:]  # most recent N snapshots
    snap_urls = [f"https://web.archive.org/web/{ts}/{orig}" for ts, orig in rows]
    log(f"Fetch: reading {len(snap_urls)} old snapshots in parallel")
    got = tf_fetch(snap_urls, exclude=NOISE)
    history = []
    for (ts, _), su, res in zip(rows, snap_urls, got):
        p = parse_price(res.get("text"))
        if p:
            history.append({"date": dt.datetime.strptime(ts[:8], "%Y%m%d").date().isoformat(),
                            "price": p, "source": su})
    log(f"Got {len(history)} historical prices")
    return history


def competitors(name, own_url):
    log(f"Search: looking for '{name}' at other UK retailers")
    own = urlparse(own_url).netloc.removeprefix("www.")
    results = tf_search(f"{name} price UK", exclude_domain=own)
    skip = ("youtube.", "reddit.", "wikipedia.", "support.", "camelcamelcamel")
    offers = []
    for r in results:
        u = r.get("url", "")
        if any(s in u for s in skip):
            continue
        p = parse_price(r.get("snippet", ""))
        offers.append({"site": r.get("site_name") or urlparse(u).netloc,
                       "url": u, "price": p, "via": "Search"})
    offers = offers[:4]
    # Agent verifies the top candidate's live price (snippets can be stale)
    if offers:
        top = offers[0]
        log(f"Agent: checking the live price on {top['site']}")
        try:
            a = tf_agent(top["url"], f"Find the current selling price in GBP of '{name}' "
                                     "on this page. Return JSON with price (number) and "
                                     "in_stock (boolean). Do not add to basket.")
            if a.get("price"):
                top["price"], top["via"] = float(a["price"]), "Agent"
                top["in_stock"] = a.get("in_stock")
        except Exception as e:  # keep the snippet price if the agent fails
            log(f"Agent skipped ({e.__class__.__name__})")
    return [o for o in offers if o["price"]]


def sane_offers(offers, now):
    """Drop snippet prices that can't be the same product (finance per month,
    accessories, bundles): keep only 40%–300% of the price we're checking."""
    return [o for o in offers if o.get("price") and 0.4 * now <= o["price"] <= 3 * now]


def verdict(now, was_claim, history, offers):
    offers = sane_offers(offers, now)
    past = [h["price"] for h in history]
    notes = []
    if not past:
        return "⚪", "Not enough history", ["No archived prices found for this page."]
    lo, hi, med = min(past), max(past), statistics.median(past)
    cheapest = min(offers, key=lambda o: o["price"]) if offers else None

    if was_claim and was_claim > hi * 1.05:
        code, title = "🔴", "Fake sale"
        notes.append(f"Claims it was £{was_claim:.2f}, but the highest price we can find "
                     f"in the archive is £{hi:.2f}.")
    elif now >= med * 0.95:
        code, title = "🟡", "Not really a deal"
        notes.append(f"£{now:.2f} is about what it usually costs (typical £{med:.2f}).")
    else:
        code, title = "🟢", "Genuine price drop"
        notes.append(f"£{now:.2f} is {100 * (1 - now / med):.0f}% below its typical "
                     f"archived price of £{med:.2f}.")
    if now <= lo:
        notes.append("This is the lowest price we've seen in the archive.")
    hs = sorted(history, key=lambda x: x["date"])
    for a, b in zip(hs, hs[1:]):
        if b["price"] > a["price"] * 1.15:
            notes.append(f"⚠️ Price hike spotted: £{a['price']:.2f} → £{b['price']:.2f} "
                         f"around {b['date'][:7]} — a classic move to inflate the 'was' price "
                         "before a sale. Judge discounts against the older price.")
    if cheapest and cheapest["price"] < now * 0.98:
        notes.append(f"Heads up: {cheapest['site']} has it for £{cheapest['price']:.2f}.")
        if code == "🟢":
            code, title = "🟡", "Real drop, but cheaper elsewhere"
    elif cheapest and cheapest["price"] < now:
        notes.append(f"About the same price elsewhere "
                     f"(cheapest: {cheapest['site']} £{cheapest['price']:.2f}).")
    elif cheapest:
        notes.append(f"Cheaper than other retailers we checked "
                     f"(next best: {cheapest['site']} £{cheapest['price']:.2f}).")
    return code, title, notes


# ---------------------------------------------------------------- report
def render(data, path="report.html"):
    h = data["history"] + [{"date": dt.date.today().isoformat(), "price": data["now"], "source": data["url"]}]
    h.sort(key=lambda x: x["date"])
    ps = [x["price"] for x in h] + ([data["was"]] if data.get("was") else [])
    top, W, H, pad = max(ps) * 1.1, 640, 220, 40
    def X(i): return pad + i * (W - 2 * pad) / max(1, len(h) - 1)
    def Y(p): return H - pad - p / top * (H - 2 * pad)
    pts = " ".join(f"{X(i):.0f},{Y(x['price']):.0f}" for i, x in enumerate(h))
    dots = "".join(
        f'<circle cx="{X(i):.0f}" cy="{Y(x["price"]):.0f}" r="5" class="{"now" if i == len(h) - 1 else "dot"}"/>'
        f'<text x="{X(i):.0f}" y="{Y(x["price"]) - 10:.0f}" text-anchor="middle">£{x["price"]:.0f}</text>'
        f'<text x="{X(i):.0f}" y="{H - 12}" text-anchor="middle" class="ax">{x["date"][:7]}</text>'
        for i, x in enumerate(h))
    was_line = (f'<line x1="{pad}" x2="{W - pad}" y1="{Y(data["was"]):.0f}" y2="{Y(data["was"]):.0f}" class="was"/>'
                f'<text x="{W - pad}" y="{Y(data["was"]) - 6:.0f}" text-anchor="end" class="wast">claimed "was" £{data["was"]:.0f}</text>'
                if data.get("was") else "")
    rows = "".join(f'<tr><td>{html.escape(o["site"])}</td><td>£{o["price"]:.2f}</td>'
                   f'<td>{o["via"]}</td><td><a href="{html.escape(o["url"])}">link</a></td></tr>'
                   for o in data["offers"])
    hist_rows = "".join(f'<tr><td>{x["date"]}</td><td>£{x["price"]:.2f}</td>'
                        f'<td><a href="{html.escape(x["source"])}">snapshot</a></td></tr>' for x in data["history"])
    notes = "".join(f"<li>{html.escape(n)}</li>" for n in data["notes"])
    page = f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Fake Sale Detector</title><style>
:root{{--bg:#f6f4ef;--card:#fff;--ink:#1c1b19;--mute:#6b675f;--line:#e4e0d6;--acc:#e8572a}}
@media (prefers-color-scheme:dark){{:root{{--bg:#151412;--card:#1f1e1b;--ink:#f1eee7;--mute:#a19c91;--line:#34322d}}}}
body{{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,-apple-system,Segoe UI,sans-serif}}
.w{{max-width:720px;margin:32px auto;padding:0 16px}} .card{{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:22px;margin-bottom:16px}}
.k{{font-size:12px;letter-spacing:.08em;text-transform:uppercase;color:var(--mute)}} h1{{font-size:22px;margin:4px 0 0}}
.v{{font-size:34px;font-weight:800;margin:6px 0}} .big{{font-size:28px;font-weight:700}} .row{{display:flex;gap:28px;flex-wrap:wrap;margin-top:10px}}
svg{{width:100%;height:auto}} polyline{{fill:none;stroke:var(--ink);stroke-width:2}} .dot{{fill:var(--ink)}} .now{{fill:var(--acc)}}
svg text{{font-size:12px;fill:var(--ink)}} .ax{{fill:var(--mute)}} .was{{stroke:var(--acc);stroke-dasharray:6 5}} .wast{{fill:var(--acc)}}
table{{width:100%;border-collapse:collapse}} td,th{{text-align:left;padding:6px 4px;border-bottom:1px solid var(--line)}} a{{color:var(--acc)}}
footer{{color:var(--mute);font-size:12px;text-align:center;margin:20px 0}}</style></head><body><div class="w">
<div class="card"><div class="k">Fake Sale Detector · {html.escape(urlparse(data["url"]).netloc)}</div>
<h1>{html.escape(data["name"])}</h1><div class="v">{data["code"]} {html.escape(data["title"])}</div>
<div class="row"><div><div class="k">Now</div><div class="big">£{data["now"]:.2f}</div></div>
{f'<div><div class="k">Claimed was</div><div class="big"><s>£{data["was"]:.2f}</s></div></div>' if data.get("was") else ""}
{f'<div><div class="k">Typical (archive)</div><div class="big">£{statistics.median([x["price"] for x in data["history"]]):.2f}</div></div>' if data["history"] else ""}</div>
<ul>{notes}</ul></div>
<div class="card"><div class="k">Price history (Wayback Machine via TinyFish Fetch)</div>
<svg viewBox="0 0 {W} {H}">{was_line}<polyline points="{pts}"/>{dots}</svg>
<table><tr><th>Date</th><th>Price</th><th>Source</th></tr>{hist_rows}</table></div>
<div class="card"><div class="k">Other retailers right now (TinyFish Search + Agent)</div>
<table><tr><th>Retailer</th><th>Price</th><th>Read by</th><th></th></tr>{rows or '<tr><td colspan=4>None found</td></tr>'}</table></div>
<footer>Checked {data["checked"]} · built with TinyFish Search, Fetch, Agent &amp; Monitor</footer></div></body></html>"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(page)
    return path


def main():
    ap = argparse.ArgumentParser(description="Is that sale real?")
    ap.add_argument("url", nargs="?")
    ap.add_argument("--was", type=float, help="the 'was' price the shop is claiming")
    ap.add_argument("--from-json", help="re-render a saved result.json")
    ap.add_argument("--no-open", action="store_true")
    a = ap.parse_args()

    if a.from_json:
        data = json.load(open(a.from_json, encoding="utf-8"))
        data["offers"] = sane_offers(data["offers"], data["now"])
        data["code"], data["title"], data["notes"] = verdict(
            data["now"], data.get("was"), data["history"], data["offers"])
    else:
        if not a.url:
            ap.error("give a product URL")
        if not API_KEY:
            sys.exit("Set TINYFISH_API_KEY first (https://agent.tinyfish.ai/api-keys)")
        print(f"\n🕵️  Fake Sale Detector — {a.url}\n")
        with ThreadPoolExecutor() as ex:
            f_live = ex.submit(live_price, a.url)
            f_hist = ex.submit(price_history, a.url)
            name, now = f_live.result()
            history = f_hist.result()
        if now is None:
            sys.exit("Couldn't read a price from that page.")
        offers = sane_offers(competitors(name, a.url), now)
        code, title, notes = verdict(now, a.was, history, offers)
        data = dict(url=a.url, name=name, now=now, was=a.was, history=history, offers=offers,
                    code=code, title=title, notes=notes,
                    checked=dt.datetime.now().strftime("%d %b %Y %H:%M"))
        json.dump(data, open("result.json", "w"), indent=2)
        if DEBUG:
            json.dump(DEBUG_LOG, open("debug.json", "w"), indent=2)
            log("Wrote raw TinyFish responses to debug.json")

    print(f"\n{data['code']}  {data['title']}  —  {data['name']}  now £{data['now']:.2f}")
    for n in data["notes"]:
        print(f"   • {n}")
    path = render(data)
    print(f"\nReport: {os.path.abspath(path)}\n")
    if not a.no_open:
        webbrowser.open("file://" + os.path.abspath(path))


if __name__ == "__main__":
    main()
