import argparse
import base64
import calendar
from datetime import datetime, timezone, timedelta
from email.utils import format_datetime
import hashlib
import html
import json
import logging
import os
from pathlib import Path
import re
from urllib.parse import urlencode, urlsplit, urljoin, parse_qsl, urlunsplit
import xml.etree.ElementTree as ET

from bs4 import BeautifulSoup
import feedparser
import yaml

from .network import Client, safe_url, resolve_public

LOG = logging.getLogger(__name__)
MEDIA = "http://search.yahoo.com/mrss/"
ATOM = "http://www.w3.org/2005/Atom"
ET.register_namespace("media", MEDIA)
ET.register_namespace("atom", ATOM)


def clean(text):
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]", "", str(text))


def load_config(path):
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    locations = data.get("locations") if isinstance(data, dict) else None
    if not isinstance(locations, list) or not locations:
        raise ValueError("locations must be a nonempty list")
    seen = set()
    for loc in locations:
        if not isinstance(loc, dict) or any(not isinstance(loc.get(k), str) or not loc[k].strip() for k in ("name", "slug", "query")):
            raise ValueError("Each location requires name, slug and query")
        if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", loc["slug"]) or loc["slug"] in seen:
            raise ValueError("Invalid or duplicate slug")
        seen.add(loc["slug"])
    return locations


def google_url(query):
    return "https://news.google.com/rss/search?" + urlencode({"q": query, "hl": "ja", "gl": "JP", "ceid": "JP:ja"})


def canonical(url):
    p = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
             if not k.lower().startswith("utm_") and k.lower() not in ("fbclid", "gclid")]
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path, urlencode(sorted(query)), ""))


def deduplicate(items):
    result, urls, titles = [], set(), set()
    for item in sorted(items, key=lambda x: x["published"], reverse=True):
        aliases = {canonical(item["url"]), canonical(item["google_url"])}
        title = (re.sub(r"\s+", "", item["title"]).casefold(), item["source"].casefold())
        if aliases & urls or title in titles:
            continue
        result.append(item)
        urls.update(aliases)
        titles.add(title)
    return result


def extract_image(body, page_url):
    soup = BeautifulSoup(body, "html.parser")
    selectors = ['meta[property="og:image"]', 'meta[property="og:image:url"]',
                 'meta[name="twitter:image"]', 'meta[property="twitter:image"]',
                 'meta[name="twitter:image:src"]', 'meta[itemprop="image"]', 'link[rel="image_src"]']
    for selector in selectors:
        for node in soup.select(selector):
            value = node.get("content") or node.get("href")
            if value:
                try:
                    return safe_url(urljoin(page_url, value.strip()))
                except ValueError:
                    pass
    return None


def resolve_article(client, url):
    if urlsplit(url).hostname != "news.google.com":
        return safe_url(url)
    token = urlsplit(url).path.rstrip("/").split("/")[-1]
    try:
        decoded = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
        match = re.search(rb"https?://[^\s\x00-\x20\x7f-\xff]+", decoded)
        if match:
            return safe_url(match.group().decode())
    except (ValueError, UnicodeError):
        pass
    final, body = client.fetch(url)
    if urlsplit(final).hostname != "news.google.com":
        return safe_url(final)
    soup = BeautifulSoup(body, "html.parser")
    node = soup.select_one("[data-n-a-sg][data-n-a-ts]")
    if node:
        args = ["garturlreq", [["X", "X", ["X", "X"], None, None, 1, 1, "JP:ja", None, 1,
                None, None, None, None, None, 0, 1], "X", "X", 1, [1, 1, 1], 1, 1, None, 0, 0, None, 0],
                token, int(node["data-n-a-ts"]), node["data-n-a-sg"]]
        request = json.dumps([[["Fbv4je", json.dumps(args), None, "generic"]]])
        _, reply = client.fetch("https://news.google.com/_/DotsSplashUi/data/batchexecute", "POST", {"f.req": request})
        for line in reply.decode().splitlines():
            if line.startswith("[["):
                for row in json.loads(line):
                    if len(row) > 2 and row[0] == "wrb.fr" and row[1] == "Fbv4je":
                        payload = json.loads(row[2])
                        if payload[0] == "garturlres":
                            return safe_url(payload[1])
    raise ValueError("Google URL resolution unavailable")


def parse_news(body, now):
    feed = feedparser.parse(body)
    if not feed.get("version") or (not feed.entries and feed.bozo):
        raise ValueError("Invalid news feed")
    items = []
    for entry in feed.entries:
        try:
            url = safe_url(entry.link)
            date = datetime.fromtimestamp(calendar.timegm(entry.published_parsed), timezone.utc)
            if date > now + timedelta(days=1):
                continue
            source = clean(entry.get("source", {}).get("title", "不明"))
            title = clean(entry.title)
            items.append(dict(id=hashlib.sha256(url.encode()).hexdigest(), google_url=url, url=url,
                title=title, source=source, published=date.isoformat(),
                description=clean(BeautifulSoup(entry.get("summary", ""), "html.parser").get_text(" ", strip=True)), image=None))
        except (AttributeError, ValueError, OverflowError, TypeError):
            LOG.warning("Skipping malformed feed entry")
    return items


def enrich(item, cache, client, now):
    key = item["google_url"]
    record = cache.get(key, {})
    if not record.get("image") and record.get("attempts", 0) < 3 and now.isoformat() >= record.get("next_retry", ""):
        attempts = record.get("attempts", 0) + 1
        record = dict(record, attempts=attempts, next_retry=(now + timedelta(hours=24 * attempts)).isoformat())
        try:
            resolved = record.get("url") or resolve_article(client, key)
            resolve_public(resolved)
            record["url"] = resolved
            final, body = client.fetch(resolved)
            record["url"] = final
            image = extract_image(body, final)
            if image:
                resolve_public(image)
            record["image"] = image
            record["error"] = None if image else "No representative image"
        except Exception as exc:
            record["error"] = type(exc).__name__ + ": " + str(exc)[:160]
            LOG.info("Metadata unavailable: %s", record["error"])
        cache[key] = record
    item["url"] = record.get("url") or item["url"]
    item["image"] = record.get("image") or item.get("image")
    return item


def render_feed(loc, items, base_url):
    rss = ET.Element("rss", {"version": "2.0"})
    channel = ET.SubElement(rss, "channel")
    for tag, text in [("title", "ローカルニュース - " + loc["name"]), ("link", base_url + "/" if base_url else google_url(loc["query"])),
                      ("description", loc["name"] + "の地域ニュース（Google News検索）"), ("language", "ja")]:
        ET.SubElement(channel, tag).text = clean(text)
    if base_url:
        ET.SubElement(channel, f"{{{ATOM}}}link", {"href": base_url + "/feeds/" + loc["slug"] + ".xml", "rel": "self", "type": "application/rss+xml"})
    if items:
        ET.SubElement(channel, "lastBuildDate").text = format_datetime(datetime.fromisoformat(max(i["published"] for i in items)))
    for item in items:
        node = ET.SubElement(channel, "item")
        for tag, text in [("title", item["title"]), ("link", item["url"]), ("author", None),
                          ("pubDate", format_datetime(datetime.fromisoformat(item["published"])) )]:
            if text is not None:
                ET.SubElement(node, tag).text = clean(text)
        ET.SubElement(node, "guid", {"isPermaLink": "false"}).text = item["id"]
        ET.SubElement(node, "source", {"url": google_url(loc["query"])}).text = clean(item["source"])
        description = "<p>" + html.escape(clean(item["description"])) + "</p><p>配信元: " + html.escape(clean(item["source"])) + "</p>"
        if item.get("image"):
            image = item["image"]
            ET.SubElement(node, f"{{{MEDIA}}}thumbnail", {"url": image})
            ET.SubElement(node, f"{{{MEDIA}}}content", {"url": image, "medium": "image"})
            description = '<p><img src="' + html.escape(image, quote=True) + '" alt="" /></p>' + description
        ET.SubElement(node, "description").text = description
    ET.indent(rss)
    return ET.tostring(rss, encoding="utf-8", xml_declaration=True)


def write_changed(path, content):
    path = Path(path)
    data = content if isinstance(content, bytes) else content.encode("utf-8")
    if not path.exists() or path.read_bytes() != data:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_bytes(data)
        temp.replace(path)


def read_json(path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def generate(root, base_url="", max_enrich=45, client=None, now=None):
    now = now or datetime.now(timezone.utc)
    client = client or Client()
    locations = load_config(root / "locations.yml")
    cache = read_json(root / "data/article_cache.json", {})
    all_items, report, successes = {}, {}, 0
    enrichment_limit = max(max_enrich, len(locations)) if max_enrich > 0 else 0
    budget = enrichment_limit
    for loc in locations:
        path = root / "data" / (loc["slug"] + ".json")
        old = read_json(path, [])
        try:
            _, body = client.fetch(google_url(loc["query"]))
            incoming = parse_news(body, now)
            successes += 1
        except Exception as exc:
            LOG.warning("%s feed failed: %s", loc["slug"], exc)
            incoming = []
        # Keep stable IDs when metadata or destination URLs change.
        prior = {i["google_url"]: i for i in old}
        prior_titles = {(re.sub(r"\s+", "", i["title"]).casefold(), i["source"].casefold()): i for i in old}
        merged = []
        for item in incoming:
            title_key = (re.sub(r"\s+", "", item["title"]).casefold(), item["source"].casefold())
            previous = prior.get(item["google_url"]) or prior_titles.get(title_key)
            if previous:
                item = dict(item, id=previous["id"], url=previous["url"], image=previous.get("image"))
                if item["google_url"] not in cache and previous["google_url"] in cache:
                    cache[item["google_url"]] = dict(cache[previous["google_url"]])
            merged.append(item)
        merged.extend(i for i in old if i["google_url"] not in {n["google_url"] for n in incoming})
        items = deduplicate([i for i in merged if datetime.fromisoformat(i["published"]) >= now - timedelta(days=30)])[:150]
        # Per-location quota prevents a large first region from starving others.
        quota = enrichment_limit // len(locations) + (1 if locations.index(loc) < enrichment_limit % len(locations) else 0)
        for item in items:
            rec = cache.get(item["google_url"], {})
            due = not rec.get("image") and rec.get("attempts", 0) < 3 and now.isoformat() >= rec.get("next_retry", "")
            if due and budget > 0 and quota > 0:
                enrich(item, cache, client, now)
                budget -= 1
                quota -= 1
            elif rec:
                item["url"] = rec.get("url") or item["url"]
                item["image"] = rec.get("image")
        items = deduplicate(items)
        all_items[loc["slug"]] = items
        report[loc["slug"]] = {"fetched": len(incoming), "retained": len(items), "images": sum(bool(i.get("image")) for i in items)}
        write_changed(path, json.dumps(items, ensure_ascii=False, indent=2) + "\n")
        write_changed(root / "feeds" / (loc["slug"] + ".xml"), render_feed(loc, items, base_url.rstrip("/")))
    active_urls = {i["google_url"] for items in all_items.values() for i in items}
    cache = {k: v for k, v in cache.items() if k in active_urls}
    write_changed(root / "data/article_cache.json", json.dumps(cache, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    active = set(all_items)
    for folder, suffix in [("feeds", ".xml"), ("data", ".json")]:
        for path in (root / folder).glob("*" + suffix):
            if path.stem not in active and path.name != "article_cache.json":
                path.unlink()
    links = "\n".join('<li><a href="feeds/' + loc["slug"] + '.xml">ローカルニュース - ' + html.escape(loc["name"]) + '</a><br><code>' + html.escape((base_url.rstrip("/") + "/" if base_url else "") + "feeds/" + loc["slug"] + '.xml') + '</code></li>' for loc in locations)
    write_changed(root / "index.html", '<!doctype html>\n<html lang="ja"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>ローカルニュースRSS</title><style>body{font-family:system-ui;max-width:760px;margin:3rem auto;padding:0 1rem;line-height:1.8}li{margin:1.5rem 0}code{overflow-wrap:anywhere}</style><h1>ローカルニュースRSS</h1><p>リンク先のRSS URLをInoreaderに登録してください。原則毎時17分に更新します。</p><ul>' + links + '</ul><p>Google News検索に基づく地域ニュース。画像・記事の権利は各配信元に帰属します。</p></html>\n')
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not successes:
        raise RuntimeError("All source feeds failed; retained existing articles, deployment aborted")
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--base-url", default=os.environ.get("SITE_URL", ""))
    parser.add_argument("--max-enrich", type=int, default=45)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    generate(args.root, args.base_url, args.max_enrich)


if __name__ == "__main__":
    main()
