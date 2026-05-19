#!/usr/bin/env python3
from __future__ import annotations

import argparse
import html
import json
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urljoin
from xml.etree import ElementTree

import httpx


DEFAULT_FEEDS = [
    {
        "name": "Fed Monetary Policy",
        "url": "https://www.federalreserve.gov/feeds/press_monetary.xml",
        "weight": 1.0,
    },
    {
        "name": "SEC Press Releases",
        "url": "https://www.sec.gov/news/pressreleases.rss",
        "weight": 1.0,
    },
    {
        "name": "CFTC General Press Releases",
        "url": "https://www.cftc.gov/RSS/RSSGP/rssgp.xml",
        "weight": 1.0,
    },
    {
        "name": "CFTC Enforcement Press Releases",
        "url": "https://www.cftc.gov/RSS/RSSENF/rssenf.xml",
        "weight": 1.1,
    },
    {
        "name": "U.S. Treasury Press Releases",
        "url": "https://home.treasury.gov/news/press-releases",
        "type": "html",
        "include_href": ["/news/press-releases/"],
        "exclude_titles": ["view all", "readouts", "testimonies", "statements", "remarks"],
        "weight": 1.0,
    },
    {
        "name": "Kraken Status",
        "url": "https://status.kraken.com/history.rss",
        "weight": 1.4,
    },
    {
        "name": "CoinDesk",
        "url": "https://www.coindesk.com/arc/outboundfeeds/rss",
        "weight": 0.7,
    },
]

DEFAULT_KEYWORDS = {
    "hack": 35,
    "exploit": 35,
    "breach": 30,
    "outage": 30,
    "degraded": 25,
    "incident": 25,
    "liquidation": 25,
    "bankruptcy": 35,
    "lawsuit": 25,
    "investigation": 25,
    "fraud": 30,
    "scam": 25,
    "sec": 12,
    "cftc": 12,
    "treasury": 10,
    "ofac": 20,
    "fincen": 18,
    "sanctions": 22,
    "money laundering": 20,
    "illicit finance": 18,
    "cybersecurity": 14,
    "stablecoin": 10,
    "fed": 8,
    "fomc": 12,
    "rate hike": 18,
    "inflation": 16,
    "recession": 20,
    "sanction": 20,
    "war": 20,
    "ban": 18,
    "etf outflow": 18,
}

CRYPTO_TERMS = ("bitcoin", "btc", "crypto", "digital asset", "ethereum", "ether", "xrp", "solana", "kraken")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except Exception:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
        except Exception:
            return None


def text_of(node: ElementTree.Element, *names: str) -> str:
    for name in names:
        found = node.find(name)
        if found is not None and found.text:
            return found.text.strip()
    return ""


def strip_namespaces(xml_text: str) -> ElementTree.Element:
    root = ElementTree.fromstring(xml_text.encode("utf-8"))
    for elem in root.iter():
        if "}" in elem.tag:
            elem.tag = elem.tag.split("}", 1)[1]
    return root


def parse_rss(xml_text: str, source: str, limit: int = 12) -> list[dict[str, Any]]:
    root = strip_namespaces(xml_text.lstrip("\ufeff"))
    items: list[dict[str, Any]] = []
    for node in root.findall(".//item")[:limit]:
        title = text_of(node, "title")
        summary = text_of(node, "description", "summary")
        published_raw = text_of(node, "pubDate", "published", "updated")
        published_at = parse_datetime(published_raw)
        items.append(
            {
                "source": source,
                "title": title,
                "summary": summary[:500],
                "link": text_of(node, "link"),
                "published_at": published_at.isoformat() if published_at else None,
            }
        )
    return items


class LinkExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[dict[str, str]] = []
        self._href: str | None = None
        self._chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        attrs_dict = {key.lower(): value or "" for key, value in attrs}
        self._href = attrs_dict.get("href")
        self._chunks = []

    def handle_data(self, data: str) -> None:
        if self._href:
            self._chunks.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() != "a" or not self._href:
            return
        title = html.unescape(" ".join(" ".join(self._chunks).split()))
        if title:
            self.links.append({"href": self._href, "title": title})
        self._href = None
        self._chunks = []


def parse_html_links(
    html_text: str,
    source: str,
    base_url: str,
    include_href: list[str] | None = None,
    exclude_titles: list[str] | None = None,
    limit: int = 12,
) -> list[dict[str, Any]]:
    parser = LinkExtractor()
    parser.feed(html_text)
    include_href = include_href or []
    exclude_titles = [value.lower() for value in (exclude_titles or [])]
    seen: set[str] = set()
    items: list[dict[str, Any]] = []
    for link in parser.links:
        href = link["href"]
        title = link["title"]
        title_l = title.lower()
        if include_href and not any(pattern in href for pattern in include_href):
            continue
        if any(pattern in title_l for pattern in exclude_titles):
            continue
        full_url = urljoin(base_url, href)
        if full_url in seen:
            continue
        seen.add(full_url)
        items.append(
            {
                "source": source,
                "title": title,
                "summary": "",
                "link": full_url,
                "published_at": None,
            }
        )
        if len(items) >= limit:
            break
    return items


def keyword_matches(text: str, keyword: str) -> bool:
    keyword = keyword.lower()
    if " " in keyword:
        return keyword in text
    return re.search(rf"\b{re.escape(keyword)}\b", text) is not None


def score_item(item: dict[str, Any], keywords: dict[str, int], source_weight: float, now: datetime) -> dict[str, Any]:
    text = f"{item.get('title', '')} {item.get('summary', '')}".lower()
    hits: list[str] = []
    score = 0
    for keyword, weight in keywords.items():
        if keyword_matches(text, keyword):
            score += int(weight)
            hits.append(keyword)

    if any(term in text for term in CRYPTO_TERMS) and any(k in hits for k in ("sec", "cftc", "lawsuit", "investigation", "fraud")):
        score += 12
        hits.append("crypto_regulatory_context")

    published_at = parse_datetime(str(item.get("published_at") or ""))
    if published_at:
        age_hours = max((now - published_at).total_seconds() / 3600, 0)
        if age_hours <= 6:
            score = int(score * 1.25)
        elif age_hours > 72:
            score = int(score * 0.35)
        item["age_hours"] = round(age_hours, 2)

    weighted = int(score * DecimalCompat(source_weight))
    return {"score": weighted, "hits": hits}


def DecimalCompat(value: float | int | str) -> float:
    try:
        return float(value)
    except Exception:
        return 1.0


def load_cache(path: Path) -> dict[str, Any] | None:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return None


def write_cache(path: Path, snapshot: dict[str, Any]) -> None:
    path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def snapshot_age_minutes(snapshot: dict[str, Any], now: datetime) -> float | None:
    fetched_at = parse_datetime(str(snapshot.get("fetched_at") or ""))
    if not fetched_at:
        return None
    return max((now - fetched_at).total_seconds() / 60, 0)


def risk_level(score: int) -> str:
    if score >= 75:
        return "extreme"
    if score >= 55:
        return "high"
    if score >= 30:
        return "medium"
    return "low"


def build_snapshot(config: dict[str, Any], cache_path: Path, now: datetime | None = None) -> dict[str, Any]:
    now = now or utc_now()
    enabled = bool(config.get("enabled", False))
    if not enabled:
        return {
            "status": "disabled",
            "fetched_at": now.isoformat(),
            "risk_score": 0,
            "risk_level": "low",
            "items": [],
            "errors": [],
        }

    fetch_interval = int(config.get("fetch_interval_minutes", 60))
    cached = load_cache(cache_path)
    age = snapshot_age_minutes(cached, now) if cached else None
    if cached and age is not None and age < fetch_interval:
        cached["status"] = "cached"
        cached["age_minutes"] = round(age, 2)
        return cached

    feeds = config.get("feeds") or DEFAULT_FEEDS
    keywords = {**DEFAULT_KEYWORDS, **dict(config.get("keywords", {}))}
    items: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    headers = {"User-Agent": "Mozilla/5.0 KrakenAutoTrader/1.0 research-risk-overlay"}
    with httpx.Client(timeout=12, follow_redirects=True, headers=headers) as client:
        for feed in feeds:
            name = str(feed.get("name") or feed.get("url") or "feed")
            url = str(feed.get("url") or "")
            if not url:
                continue
            try:
                response = client.get(url)
                response.raise_for_status()
                source_type = str(feed.get("type", "rss")).lower()
                if source_type == "html":
                    parsed = parse_html_links(
                        response.text,
                        name,
                        url,
                        include_href=list(feed.get("include_href", [])),
                        exclude_titles=list(feed.get("exclude_titles", [])),
                        limit=int(config.get("items_per_feed", 12)),
                    )
                else:
                    parsed = parse_rss(response.text, name, limit=int(config.get("items_per_feed", 12)))
                weight = DecimalCompat(feed.get("weight", 1.0))
                for item in parsed:
                    risk = score_item(item, keywords, weight, now)
                    item.update(risk)
                items.extend(parsed)
            except Exception as exc:
                errors.append({"source": name, "error": f"{type(exc).__name__}: {exc}"})

    items = sorted(items, key=lambda item: int(item.get("score", 0)), reverse=True)[: int(config.get("max_items", 24))]
    top_score = max([int(item.get("score", 0)) for item in items] or [0])
    breadth_score = sum(1 for item in items if int(item.get("score", 0)) >= 20) * 5
    risk_score = min(100, top_score + breadth_score)
    snapshot = {
        "status": "fresh" if not errors else "partial",
        "fetched_at": now.isoformat(),
        "risk_score": risk_score,
        "risk_level": risk_level(risk_score),
        "block_buys": risk_score >= int(config.get("block_buy_risk_score", 70)),
        "reduce_size": risk_score >= int(config.get("reduce_size_risk_score", 40)),
        "items": items,
        "errors": errors,
    }
    write_cache(cache_path, snapshot)
    return snapshot


def compact_snapshot(snapshot: dict[str, Any], top_n: int = 5) -> dict[str, Any]:
    return {
        "status": snapshot.get("status"),
        "fetched_at": snapshot.get("fetched_at"),
        "age_minutes": snapshot.get("age_minutes"),
        "risk_score": snapshot.get("risk_score", 0),
        "risk_level": snapshot.get("risk_level", "low"),
        "block_buys": bool(snapshot.get("block_buys", False)),
        "reduce_size": bool(snapshot.get("reduce_size", False)),
        "top_items": [
            {
                "source": item.get("source"),
                "title": item.get("title"),
                "score": item.get("score", 0),
                "hits": item.get("hits", []),
                "published_at": item.get("published_at"),
            }
            for item in list(snapshot.get("items", []))[:top_n]
        ],
        "errors": snapshot.get("errors", []),
    }


def format_snapshot(snapshot: dict[str, Any]) -> str:
    compact = compact_snapshot(snapshot)
    lines = [
        f"研究风险: {compact['risk_level']} score={compact['risk_score']} status={compact['status']}",
        f"阻止买入: {compact['block_buys']} / 缩小仓位: {compact['reduce_size']}",
    ]
    for item in compact["top_items"][:5]:
        lines.append(f"- [{item.get('source')}] {item.get('title')} score={item.get('score')}")
    if compact["errors"]:
        lines.append(f"源错误: {len(compact['errors'])}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch public macro/news research risk snapshot")
    parser.add_argument("command", choices=["fetch", "show"])
    parser.add_argument("--config", type=Path, default=Path("config.json"))
    args = parser.parse_args()

    raw = json.loads(args.config.read_text(encoding="utf-8"))
    root = args.config.parent
    cache_path = root / raw.get("paths", {}).get("research_cache", "research_snapshot.json")
    if args.command == "fetch":
        snapshot = build_snapshot(raw.get("research", {}), cache_path)
    else:
        snapshot = load_cache(cache_path) or build_snapshot(raw.get("research", {}), cache_path)
    print(format_snapshot(snapshot))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
