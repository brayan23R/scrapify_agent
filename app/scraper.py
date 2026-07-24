from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, Tag
from playwright.async_api import Browser, Page, async_playwright

from .models import ExtractionMode, FieldRule, ScrapeRequest
from .safety import USER_AGENT, robots_allows, validate_public_url


@dataclass
class PageSnapshot:
    url: str
    html: str


def clean_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def absolutize(base_url: str, value: str | None) -> str:
    return urljoin(base_url, value or "") if value else ""


def value_from(node: Tag, rule: FieldRule, base_url: str) -> str:
    if rule.value == "text":
        return clean_text(node.get_text(" ", strip=True))
    if rule.value == "html":
        return str(node)
    raw = node.get(rule.value, "")
    if isinstance(raw, list):
        raw = " ".join(raw)
    raw = str(raw or "").strip()
    if rule.value in {"href", "src"}:
        return absolutize(base_url, raw)
    return raw


def extract_custom(soup: BeautifulSoup, request: ScrapeRequest, base_url: str) -> list[dict[str, Any]]:
    containers: list[Tag | BeautifulSoup]
    if request.item_selector:
        containers = list(soup.select(request.item_selector))
    else:
        containers = [soup]

    rows: list[dict[str, Any]] = []
    for container in containers:
        row: dict[str, Any] = {}
        valid = True
        for rule in request.fields:
            matches = container.select(rule.selector)
            if rule.multiple:
                value: Any = [value_from(node, rule, base_url) for node in matches]
                value = [item for item in value if item not in {"", None}]
            else:
                value = value_from(matches[0], rule, base_url) if matches else ""
            if rule.required and not value:
                valid = False
                break
            row[rule.name] = value
        if valid and any(value not in ("", None, []) for value in row.values()):
            rows.append(row)
    return rows


def extract_links(soup: BeautifulSoup, base_url: str) -> list[dict[str, str]]:
    seen: set[str] = set()
    rows: list[dict[str, str]] = []
    for anchor in soup.select("a[href]"):
        href = absolutize(base_url, anchor.get("href"))
        text = clean_text(anchor.get_text(" ", strip=True))
        if href.startswith(("http://", "https://")) and href not in seen:
            seen.add(href)
            rows.append({"text": text, "url": href})
    return rows


def extract_table(soup: BeautifulSoup) -> list[dict[str, Any]]:
    table = soup.select_one("table")
    if not table:
        return []
    headers = [clean_text(cell.get_text(" ", strip=True)) for cell in table.select("thead th")]
    if not headers:
        first_row = table.select_one("tr")
        headers = [clean_text(cell.get_text(" ", strip=True)) for cell in first_row.select("th,td")] if first_row else []
    rows: list[dict[str, Any]] = []
    all_rows = table.select("tbody tr") or table.select("tr")[1:]
    for tr in all_rows:
        values = [clean_text(cell.get_text(" ", strip=True)) for cell in tr.select("th,td")]
        if not values:
            continue
        if headers and len(headers) == len(values):
            rows.append(dict(zip(headers, values)))
        else:
            rows.append({f"col_{i+1}": value for i, value in enumerate(values)})
    return rows


def first_text(node: Tag, selectors: list[str]) -> str:
    for selector in selectors:
        found = node.select_one(selector)
        if found:
            value = clean_text(found.get_text(" ", strip=True))
            if value:
                return value
    return ""


def first_attr(node: Tag, selectors: list[str], attr: str, base_url: str) -> str:
    for selector in selectors:
        found = node.select_one(selector)
        if found and found.get(attr):
            value = str(found.get(attr))
            return absolutize(base_url, value) if attr in {"href", "src"} else value
    return ""


def infer_repeated_containers(soup: BeautifulSoup, mode: ExtractionMode) -> list[Tag]:
    selectors = {
        ExtractionMode.PRODUCTS: [
            "[itemtype*='Product']",
            ".product",
            ".product-item",
            ".product-card",
            "article[class*='product']",
            "li[class*='product']",
        ],
        ExtractionMode.ARTICLES: [
            "article",
            ".post",
            ".article",
            ".news-item",
            "li[class*='article']",
        ],
    }
    candidates: list[Tag] = []
    for selector in selectors.get(mode, []):
        found = [item for item in soup.select(selector) if isinstance(item, Tag)]
        if len(found) >= 2:
            candidates = found
            break
    return candidates


def extract_products(soup: BeautifulSoup, base_url: str) -> list[dict[str, Any]]:
    containers = infer_repeated_containers(soup, ExtractionMode.PRODUCTS)
    rows: list[dict[str, Any]] = []
    for node in containers:
        name = first_text(node, ["[itemprop='name']", "h2", "h3", ".title", "[class*='name']"])
        price = first_text(node, ["[itemprop='price']", ".price", "[class*='price']"])
        link = first_attr(node, ["a[href]"], "href", base_url)
        image = first_attr(node, ["img[src]", "img[data-src]"], "src", base_url)
        description = first_text(node, ["[itemprop='description']", ".description", "p"])
        if name or price:
            rows.append({"name": name, "price": price, "description": description, "url": link, "image": image})
    return rows


def extract_articles(soup: BeautifulSoup, base_url: str) -> list[dict[str, Any]]:
    containers = infer_repeated_containers(soup, ExtractionMode.ARTICLES)
    rows: list[dict[str, Any]] = []
    for node in containers:
        title = first_text(node, ["h1", "h2", "h3", ".title", "[itemprop='headline']"])
        link = first_attr(node, ["h1 a[href]", "h2 a[href]", "h3 a[href]", "a[href]"], "href", base_url)
        date = first_text(node, ["time", "[itemprop='datePublished']", ".date", "[class*='date']"])
        summary = first_text(node, ["[itemprop='description']", ".excerpt", ".summary", "p"])
        if title:
            rows.append({"title": title, "date": date, "summary": summary, "url": link})
    return rows


def extract_auto(soup: BeautifulSoup, base_url: str) -> list[dict[str, Any]]:
    # Intenta primero patrones repetidos comunes.
    products = extract_products(soup, base_url)
    if len(products) >= 2:
        return products
    articles = extract_articles(soup, base_url)
    if len(articles) >= 2:
        return articles

    title = clean_text(soup.title.get_text(" ", strip=True)) if soup.title else ""
    description_node = soup.select_one("meta[name='description'], meta[property='og:description']")
    description = str(description_node.get("content", "")).strip() if description_node else ""
    headings = [clean_text(h.get_text(" ", strip=True)) for h in soup.select("h1,h2,h3")][:50]
    text = clean_text((soup.select_one("main") or soup.select_one("article") or soup.body or soup).get_text(" ", strip=True))
    return [{
        "title": title,
        "description": description,
        "headings": headings,
        "text": text[:20000],
        "url": base_url,
    }]


def extract_page(html: str, request: ScrapeRequest, base_url: str) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "lxml")
    if request.mode == ExtractionMode.CUSTOM:
        return extract_custom(soup, request, base_url)
    if request.mode == ExtractionMode.LINKS:
        return extract_links(soup, base_url)
    if request.mode == ExtractionMode.TABLE:
        return extract_table(soup)
    if request.mode == ExtractionMode.PRODUCTS:
        return extract_products(soup, base_url)
    if request.mode == ExtractionMode.ARTICLES:
        return extract_articles(soup, base_url)
    return extract_auto(soup, base_url)


async def snapshot_page(page: Page, url: str, request: ScrapeRequest) -> PageSnapshot:
    await page.goto(url, wait_until="domcontentloaded", timeout=45000)
    if request.wait_for_selector:
        await page.wait_for_selector(request.wait_for_selector, timeout=15000)
    if request.delay_ms:
        await page.wait_for_timeout(request.delay_ms)
    return PageSnapshot(url=page.url, html=await page.content())


def next_url_from_html(html: str, current_url: str, selector: str | None) -> str | None:
    soup = BeautifulSoup(html, "lxml")
    selectors = [selector] if selector else [
        "a[rel='next']",
        ".pagination a.next",
        "a.next",
        "a[aria-label*='Next' i]",
        "a[aria-label*='Siguiente' i]",
    ]
    for css in selectors:
        if not css:
            continue
        node = soup.select_one(css)
        if node and node.get("href"):
            return absolutize(current_url, str(node.get("href")))
    return None


async def run_scraper(request: ScrapeRequest) -> tuple[list[dict[str, Any]], int, list[str]]:
    start_url = str(request.url)
    validate_public_url(start_url)
    if request.respect_robots_txt and not await robots_allows(start_url):
        raise PermissionError("robots.txt no permite acceder a esta URL con este agente.")

    origin_host = urlparse(start_url).hostname
    current_url: str | None = start_url
    visited: set[str] = set()
    all_rows: list[dict[str, Any]] = []
    errors: list[str] = []

    async with async_playwright() as playwright:
        browser: Browser = await playwright.chromium.launch(headless=True)
        context = await browser.new_context(user_agent=USER_AGENT, viewport={"width": 1440, "height": 1000})
        page = await context.new_page()
        try:
            for _ in range(request.max_pages):
                if not current_url or current_url in visited:
                    break
                validate_public_url(current_url)
                if request.same_domain_only and urlparse(current_url).hostname != origin_host:
                    errors.append(f"Se omitió una URL externa: {current_url}")
                    break
                if request.respect_robots_txt and not await robots_allows(current_url):
                    errors.append(f"robots.txt bloqueó: {current_url}")
                    break

                visited.add(current_url)
                try:
                    snapshot = await snapshot_page(page, current_url, request)
                    rows = extract_page(snapshot.html, request, snapshot.url)
                    for row in rows:
                        row.setdefault("_source_url", snapshot.url)
                    all_rows.extend(rows)
                    if len(all_rows) >= request.max_items:
                        all_rows = all_rows[: request.max_items]
                        break
                    current_url = next_url_from_html(snapshot.html, snapshot.url, request.next_page_selector)
                    if current_url:
                        await asyncio.sleep(max(request.delay_ms, 500) / 1000)
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"Error en {current_url}: {type(exc).__name__}: {exc}")
                    break
        finally:
            await context.close()
            await browser.close()

    return all_rows, len(visited), errors
