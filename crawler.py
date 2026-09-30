"""Polite, same-site crawler for publicly accessible web pages."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from io import BytesIO
import gzip
import ipaddress
import re
import socket
import threading
import time
from typing import Callable
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit, urldefrag, quote, unquote
from urllib.robotparser import RobotFileParser
import xml.etree.ElementTree as ET

import requests
from bs4 import BeautifulSoup
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill

USER_AGENT = "SiteScope/1.0 (local public website audit)"
MAX_BODY = 2_500_000
MAX_SITEMAP = 6_000_000
SKIP = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif", ".svg", ".ico", ".pdf",
        ".zip", ".rar", ".7z", ".mp4", ".mp3", ".mov", ".avi", ".css", ".js", ".json",
        ".xml", ".gz", ".woff", ".woff2", ".ttf", ".eot", ".doc", ".docx", ".xls",
        ".xlsx", ".ppt", ".pptx", ".webm", ".m4a", ".webmanifest", ".txt")
TRACKING = {"fbclid", "gclid", "dclid", "msclkid", "_ga", "_gl", "mc_cid", "mc_eid"}


def public_host(host: str) -> bool:
    try:
        addresses = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
        return bool(addresses) and all(ipaddress.ip_address(a[4][0]).is_global for a in addresses)
    except (ValueError, OSError):
        return False


def normalize(url: str, base: str | None = None) -> str | None:
    try:
        url = urljoin(base, url) if base else url
        url, _ = urldefrag(url.strip())
        p = urlsplit(url)
        if p.scheme.lower() not in ("http", "https") or not p.hostname or p.username or p.password:
            return None
        host = p.hostname.lower().rstrip(".")
        port = p.port
        netloc = host if port in (None, 80 if p.scheme == "http" else 443) else f"{host}:{port}"
        if ":" in host and not host.startswith("["):
            netloc = f"[{host}]" + (f":{port}" if port not in (None, 80 if p.scheme == "http" else 443) else "")
        raw_path = unquote(p.path or "/")
        # Collapse duplicate slashes in the path so /about/ and //about/ are the same page.
        raw_path = re.sub(r"/{2,}", "/", raw_path)
        path = quote(raw_path, safe="/%:@!$&'()*+,;=-._~")
        query = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
                 if k.lower() not in TRACKING and not k.lower().startswith("utm_")]
        # Query-string order does not normally identify a different page. Sorting prevents
        # duplicates such as ?a=1&b=2 versus ?b=2&a=1 while preserving repeated values.
        query.sort(key=lambda item: (item[0], item[1]))
        return urlunsplit((p.scheme.lower(), netloc, path, urlencode(query, doseq=True), ""))
    except (ValueError, UnicodeError):
        return None



def is_pathological_url(url: str) -> bool:
    """Reject obvious crawler-generated URL loops without blocking normal deep paths.

    A repeated relative-link bug can create URLs where the same long path segment is
    appended over and over. SiteScope keeps normal deep URLs, but ignores clear loops
    and excessively long URLs that are unlikely to be useful crawl targets.
    """
    try:
        p = urlsplit(url)
        if len(url) > 4096 or len(p.path) > 3072:
            return True
        parts = [unquote(part).strip().lower() for part in p.path.split("/") if part]
        run_value = None
        run_length = 0
        for part in parts:
            if part == run_value and len(part) >= 12:
                run_length += 1
            else:
                run_value = part
                run_length = 1
            if run_length >= 4:
                return True
        return False
    except (ValueError, UnicodeError):
        return True

def result_identity(url: str) -> str:
    """Return a stable identity key for display/export deduplication.

    SiteScope treats the common trailing-slash variants /page and /page/ as one
    result while preserving query strings. This is a reporting heuristic only;
    crawling still visits discovered URLs independently when allowed.
    """
    normalized = normalize(url) or url
    p = urlsplit(normalized)
    path = p.path or "/"
    if path != "/":
        path = path.rstrip("/") or "/"
    return urlunsplit((p.scheme, p.netloc, path, p.query, ""))


def allowed_site(url: str, root: str) -> bool:
    host = urlsplit(url).hostname
    return bool(host and host.removeprefix("www.") == root.removeprefix("www."))


def is_page(url: str) -> bool:
    path = re.sub(r"/+", "/", urlsplit(url).path or "/").lower()
    # Cloudflare and similar infrastructure endpoints are not website content pages.
    if path == "/cdn-cgi" or path.startswith("/cdn-cgi/"):
        return False
    return not path.endswith(SKIP)


def is_auxiliary_url(url: str) -> bool:
    """Return True for archive/navigation URLs that are useful for discovery but noisy as page results.

    These routes are still crawled so they can reveal older content; by default they are simply
    omitted from the visible/exported page list. The user can opt in to include them.
    """
    p = urlsplit(url)
    path = re.sub(r"/+", "/", p.path or "/").lower()
    parts = [part for part in path.split("/") if part]
    query = dict(parse_qsl(p.query, keep_blank_values=True))

    # Common CMS archive/taxonomy/search routes. These markers can be nested,
    # e.g. /blog/category/news/ or /articles/tag/seo/.
    archive_markers = {"author", "category", "tag", "search"}
    if any(part in archive_markers for part in parts):
        return True
    # WordPress/CMS paginated listing routes such as /blog/page/2/.
    if len(parts) >= 2 and parts[-2] == "page" and parts[-1].isdigit():
        return True
    # Feed endpoints are navigation/alternate representations, not normal content pages.
    if parts and parts[-1] in {"feed", "rss", "rss2"}:
        return True
    # Common query-based search/pagination variants.
    lowered = {str(k).lower(): str(v).lower() for k, v in query.items()}
    if "s" in lowered or "search" in lowered:
        return True
    if any(k in lowered and lowered[k].isdigit() for k in ("paged", "page")):
        return True
    return False


def origin(url: str) -> str:
    p = urlsplit(url)
    return f"{p.scheme}://{p.netloc}"


@dataclass
class CrawlStats:
    sitemap_checked: int = 0
    sitemap_pages: int = 0
    html_pages: int = 0
    root_status: int | None = None
    hit_limit: bool = False
    denied_by_robots: bool = False
    browser_working: int = 0
    browser_unavailable: str = ""
    result_pages_discovered: int = 0
    auxiliary_discovered: int = 0
    auxiliary_traversed: int = 0
    aliases_merged: int = 0


class Crawler:
    def __init__(self, start: str, max_pages: int | None = None, delay: float = 0.5,
                 on_result: Callable[[dict, int], None] | None = None,
                 on_message: Callable[[str], None] | None = None,
                 stop: threading.Event | None = None, allow_private_for_tests: bool = False,
                 browser_mode: bool = False, include_archives: bool = False):
        self.start = normalize(start if "://" in start else "https://" + start)
        if not self.start:
            raise ValueError("Website ka valid URL enter karein, e.g. https://example.com")
        self.root = urlsplit(self.start).hostname
        if not (allow_private_for_tests or public_host(self.root)):
            raise ValueError("Website public domain hona chahiye. Local/private addresses allowed nahi hain.")
        self.allow_private_for_tests = allow_private_for_tests
        self.browser_mode = browser_mode
        self.include_archives = include_archives
        self.browser = None
        self.browser_manager = None
        self.browser_page = None
        if max_pages is not None and max_pages < 1:
            raise ValueError("Optional page limit must be at least 1")
        self.max_pages = max_pages
        self.delay = delay
        self.on_result = on_result or (lambda row, count: None)
        self.on_message = on_message or (lambda message: None)
        self.stop = stop or threading.Event()
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.headers.update({"User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.6"})
        self.queue: deque[str] = deque()
        self.seen: set[str] = set()
        self.sources: dict[str, set[str]] = {}
        self.rows: list[dict] = []
        # Final/canonical URLs already emitted. This is intentionally separate from
        # ``seen`` because multiple discovered aliases can redirect to the same page.
        self.reported_urls: set[str] = set()  # result_identity keys
        self.robots: dict[str, RobotFileParser] = {}
        self.stats = CrawlStats()
        self.next_request = 0.0
        self.add(self.start, "Input")

    def message(self, message: str):
        self.on_message(message)

    def add(self, url: str, source: str) -> bool:
        target = normalize(url)
        if not target or not allowed_site(target, self.root) or not is_page(target) or is_pathological_url(target):
            return False
        if target in self.seen:
            self.sources[target].add(source)
            return False
        if self.max_pages is not None and len(self.seen) >= self.max_pages:
            self.stats.hit_limit = True
            return False
        self.seen.add(target)
        self.sources[target] = {source}
        self.queue.append(target)
        if self.include_archives or not is_auxiliary_url(target):
            self.stats.result_pages_discovered += 1
        else:
            self.stats.auxiliary_discovered += 1
        if source == "Sitemap":
            self.stats.sitemap_pages += 1
        elif source == "HTML link":
            self.stats.html_pages += 1
        return True

    def ensure_safe(self, url: str) -> bool:
        p = urlsplit(url)
        return (p.scheme in ("http", "https") and allowed_site(url, self.root)
                and (self.allow_private_for_tests or public_host(p.hostname)))

    def wait(self):
        remaining = self.next_request - time.monotonic()
        if remaining > 0:
            self.stop.wait(remaining)
        self.next_request = time.monotonic() + self.delay

    def fetch(self, url: str, *, robots_check: bool = True):
        """Follow only validated same-site redirects; never bypass robots restrictions."""
        current = url
        for _ in range(8):
            if self.stop.is_set():
                return None, current, "Stopped"
            if not self.ensure_safe(current):
                return None, current, "External/private redirect not followed"
            if robots_check and not self.can_fetch(current):
                return None, current, "Disallowed by robots.txt"
            self.wait()
            if self.stop.is_set():
                return None, current, "Stopped"
            try:
                response = self.session.get(current, timeout=(4, 8), allow_redirects=False, stream=True)
            except requests.RequestException as exc:
                return None, current, f"Network error: {type(exc).__name__}"
            if response.status_code in (301, 302, 303, 307, 308) and response.headers.get("Location"):
                target = normalize(response.headers["Location"], current)
                response.close()
                if not target or target == current:
                    return None, current, "Invalid redirect"
                current = target
                continue
            return response, current, ""
        return None, current, "Too many redirects"

    def can_fetch(self, url: str) -> bool:
        site_origin = origin(url)
        if site_origin not in self.robots:
            rp = RobotFileParser()
            robots_url = site_origin + "/robots.txt"
            rp.set_url(robots_url)
            try:
                if not self.ensure_safe(robots_url):
                    rp.disallow_all = True
                else:
                    self.wait()
                    resp = self.session.get(robots_url, timeout=(4, 6), allow_redirects=False,
                                            stream=True)
                    if resp.status_code in (401, 403):
                        rp.disallow_all = True
                    elif resp.status_code == 200:
                        data = read_bounded(resp, 512_000)
                        rp.parse(data.decode("utf-8", errors="replace").splitlines())
                        for line in data.decode("utf-8", errors="replace").splitlines():
                            if line.lstrip().lower().startswith("sitemap:"):
                                sm = normalize(line.split(":", 1)[1].strip(), site_origin)
                                if sm and allowed_site(sm, self.root):
                                    self.sitemap_candidates.add(sm)
                    elif resp.status_code in (404, 410):
                        rp.parse([])
                    else:
                        rp.disallow_all = True
                        self.message("robots.txt unavailable (temporary response); crawl paused for this host.")
                    resp.close()
            except (requests.RequestException, ValueError):
                rp.disallow_all = True
                self.message("robots.txt unavailable; this host will not be crawled until permissions can be verified.")
            self.robots[site_origin] = rp
        return self.robots[site_origin].can_fetch(USER_AGENT, url)

    def parse_sitemap(self, url: str, queue: deque[str]) -> None:
        resp, last, err = self.fetch(url, robots_check=False)
        if err or resp is None:
            return
        try:
            if resp.status_code != 200:
                return
            raw = read_bounded(resp, MAX_SITEMAP)
            if urlsplit(last).path.endswith(".gz") or raw[:2] == b"\x1f\x8b":
                try:
                    with gzip.GzipFile(fileobj=BytesIO(raw)) as compressed:
                        raw = compressed.read(MAX_SITEMAP + 1)
                except (OSError, EOFError):
                    return
            if len(raw) > MAX_SITEMAP:
                return
            root = ET.fromstring(raw)
            kind = root.tag.rsplit("}", 1)[-1].lower()
            if kind == "sitemapindex":
                for node in root.iter():
                    if node.tag.rsplit("}", 1)[-1] == "loc" and node.text:
                        sm = normalize(node.text.strip(), last)
                        if sm and allowed_site(sm, self.root):
                            queue.append(sm)
            elif kind == "urlset":
                for node in root.iter():
                    if node.tag.rsplit("}", 1)[-1] == "loc" and node.text:
                        self.add(node.text.strip(), "Sitemap")
        except (ET.ParseError, UnicodeError, ValueError):
            return
        finally:
            resp.close()

    def sitemap_discovery(self):
        """Read explicit robots sitemaps + common sitemap files before visiting pages."""
        self.sitemap_candidates = set()
        self.message("Checking robots.txt and XML sitemaps...")
        self.can_fetch(self.start)
        root = origin(self.start)
        for path in ("/sitemap.xml", "/sitemap_index.xml", "/sitemap-index.xml",
                     "/wp-sitemap.xml", "/sitemap.xml.gz", "/page-sitemap.xml",
                     "/post-sitemap.xml"):
            self.sitemap_candidates.add(root + path)
        candidates = deque(sorted(self.sitemap_candidates))
        done = set()
        while candidates and not self.stop.is_set():
            sm = candidates.popleft()
            if sm in done or not self.ensure_safe(sm):
                continue
            done.add(sm)
            self.parse_sitemap(sm, candidates)
            self.stats.sitemap_checked += 1
            if self.max_pages is not None and len(self.seen) >= self.max_pages:
                self.stats.hit_limit = True
                break
        extra = (f"; {self.stats.auxiliary_discovered} archive/navigation URL(s) will be traversed but hidden"
                 if self.stats.auxiliary_discovered and not self.include_archives else "")
        self.message(f"Found {self.stats.result_pages_discovered} result page(s) so far{extra}. Checking URLs now...")

    def prepare_browser(self):
        """Ordinary Chromium rendering, not stealth or a CAPTCHA bypass."""
        if not self.browser_mode:
            return
        try:
            from playwright.sync_api import sync_playwright
            self.browser_manager = sync_playwright().start()
            self.browser = self.browser_manager.chromium.launch(headless=True)
            context = self.browser.new_context(ignore_https_errors=False, service_workers="block")
            self.browser_page = context.new_page()
            self.message("Browser mode ready: checking pages in Chromium as well as HTTP...")
        except Exception as exc:
            self.stats.browser_unavailable = ("Chromium could not start: " + str(exc).splitlines()[0][:200] +
                ". Run: python -m playwright install chromium")
            self.message(self.stats.browser_unavailable + "; continuing with HTTP-only scan.")
            self.close_browser()

    def close_browser(self):
        if self.browser is not None:
            try:
                self.browser.close()
            except Exception:
                pass
            self.browser = None
        if self.browser_manager is not None:
            try:
                self.browser_manager.stop()
            except Exception:
                pass
            self.browser_manager = None
        self.browser_page = None

    def extract_links(self, html: bytes | str, base_url: str, source: str) -> str:
        soup = BeautifulSoup(html, "html.parser")
        for element in soup.select("a[href], area[href], link[rel='canonical'][href]"):
            href = element.get("href")
            if href:
                self.add(normalize(href, base_url) or "", source)
        return soup.title.get_text(" ", strip=True)[:200] if soup.title else ""

    def inspect_in_browser(self, url: str, row: dict, parse_links: bool):
        """Use real browser outcome separately from the HTTP client's response."""
        if self.browser_page is None or self.stop.is_set() or not self.can_fetch(url):
            return
        # A normal page load, no anti-bot evasion, no CAPTCHA solving or login bypass.
        self.wait()
        if self.stop.is_set():
            return
        try:
            response = self.browser_page.goto(url, wait_until="domcontentloaded", timeout=18000)
            if response is None:
                row["Notes"] = (row["Notes"] + " Browser returned no navigation response.").strip()
                return
            code = response.status
            row["Browser HTTP"] = code
            final_url = normalize(self.browser_page.url)
            if not final_url or not allowed_site(final_url, self.root):
                row["Notes"] = (row["Notes"] + " Browser redirected outside this site; not verified.").strip()
                return
            original_url = row.get("URL", url)
            if final_url != original_url:
                row["Notes"] = (row["Notes"] + f" Browser final URL: {final_url}.").strip()
                # Use the confirmed same-site browser destination as the result URL.
                # This collapses aliases such as /contact-us and /contact-us/.
                row["URL"] = final_url
            if 200 <= code < 300:
                html = self.browser_page.content()
                low = html.lower()[:200000]
                # A 200 response displaying a bot challenge is not proof that the page works.
                challenge = any(marker in low for marker in (
                    "<title>just a moment", "cf-challenge", "<title>attention required!",
                    "<title>access denied", "captcha-delivery.com/captcha/"))
                if challenge:
                    row["Status"] = "Blocked"
                    row["Notes"] = (row["Notes"] +
                        " Browser displayed an access challenge despite HTTP 2xx; manually verify.").strip()
                    return
                if row["Status"] != "Working":
                    row["Status"] = "Working (browser)"
                    self.stats.browser_working += 1
                    row["Notes"] = (row["Notes"] +
                        " HTTP scanner and browser returned different results; verified in Chromium.").strip()
                if parse_links:
                    # Give common client-side routes a short chance to appear after DOMContentLoaded.
                    try:
                        self.browser_page.wait_for_timeout(450)
                    except Exception:
                        pass
                    html = self.browser_page.content()
                    title = self.extract_links(html, final_url, "Browser link")
                    row["Title"] = title or row["Title"]
            elif code in (401, 403, 429):
                row["Status"] = "Blocked"
                row["Notes"] = (row["Notes"] +
                    f" Chromium also received HTTP {code}; manually verify in your own browser.").strip()
            elif code in (404, 410):
                row["Status"] = "Not working"
                row["Notes"] = (row["Notes"] +
                    f" Chromium confirmed HTTP {code}; page is not available.").strip()
            elif 500 <= code:
                row["Status"] = "Server error"
                row["Notes"] = (row["Notes"] + f" Chromium received HTTP {code}.").strip()
            elif 400 <= code:
                row["Status"] = "Not working"
                row["Notes"] = (row["Notes"] + f" Chromium received HTTP {code}.").strip()
            else:
                row["Notes"] = (row["Notes"] + f" Chromium received HTTP {code}.").strip()
        except Exception as exc:
            # Browser failures do not turn a known HTTP status into a broken page.
            row["Notes"] = (row["Notes"] +
                f" Browser verification inconclusive: {type(exc).__name__}.").strip()

    def check_page(self, url: str) -> dict:
        row = {"URL": url, "Status": "Not checked", "HTTP": "", "Browser HTTP": "",
               "Title": "", "Source": ", ".join(sorted(self.sources.get(url, {"Input"}))), "Notes": ""}
        resp, final_url, err = self.fetch(url)
        if err or resp is None:
            row["Notes"] = err
            if err.startswith("Network") or err == "Too many redirects":
                row["Status"] = "Error"
                # A transient requests-layer failure is not proof the page is down.
                # When browser assistance is enabled, independently verify it in Chromium.
                if self.browser_page is not None:
                    self.inspect_in_browser(url, row, parse_links=True)
                elif self.stats.browser_unavailable:
                    row["Notes"] = (row["Notes"] + " " + self.stats.browser_unavailable).strip()
            elif "robots.txt" in err:
                row["Status"] = "Not checked"
                self.stats.denied_by_robots = True
            # Never use browser mode on pages robots.txt disallows.
            return row
        try:
            code = resp.status_code
            row["HTTP"] = code
            if url == self.start:
                self.stats.root_status = code
            if code in (401, 403):
                row["Status"] = "Blocked"
                row["Notes"] = "HTTP scanner was denied; this does NOT mean the page is broken."
            elif code == 429:
                row["Status"] = "Blocked"
                row["Notes"] = "Rate limit 429; scan stops to avoid further requests."
            elif 200 <= code < 300:
                row["Status"] = "Working"
            elif code in (404, 410):
                row["Status"] = "Not working"
            elif 500 <= code:
                row["Status"] = "Server error"
            elif 400 <= code:
                row["Status"] = "Not working"
            else:
                row["Status"] = "Not checked"
                row["Notes"] = "Unexpected HTTP response"
            if final_url != url:
                row["Notes"] = (row["Notes"] + f" Scanner redirected to {final_url}.").strip()
                # A followed same-site redirect is concrete canonicalization evidence.
                # Report the destination rather than keeping a duplicate alias row.
                row["URL"] = final_url
            content_type = resp.headers.get("Content-Type", "").lower()
            if row["Status"] == "Working" and ("html" in content_type or not content_type):
                body = read_bounded(resp, MAX_BODY)
                if body:
                    row["Title"] = self.extract_links(body, final_url, "HTML link")
            elif row["Status"] == "Working":
                row["Notes"] = (row["Notes"] + " Non-HTML resource.").strip()
        except (requests.RequestException, ValueError) as exc:
            row["Status"] = "Error"
            row["Notes"] = f"Read failed: {type(exc).__name__}"
        finally:
            resp.close()
        # The browser independently checks a blocked page and also discovers JS-built links.
        if self.browser_page is not None and row["Status"] in ("Working", "Blocked") and row["HTTP"] != 429:
            self.inspect_in_browser(url, row, parse_links=True)
        elif row["Status"] == "Blocked" and self.stats.browser_unavailable:
            row["Notes"] += " " + self.stats.browser_unavailable
        return row

    def run(self) -> tuple[list[dict], str]:
        try:
            self.prepare_browser()
            self.sitemap_discovery()
            while self.queue and not self.stop.is_set():
                url = self.queue.popleft()
                row = self.check_page(url)
                result_url = normalize(row.get("URL", url)) or url
                row["URL"] = result_url
                reportable = self.include_archives or not is_auxiliary_url(result_url)
                if reportable:
                    # Deduplicate only after redirects/browser navigation have resolved.
                    # This handles trailing-slash, http→https, www/non-www and other
                    # same-site aliases without guessing that distinct URLs are equal.
                    identity = result_identity(result_url)
                    if identity not in self.reported_urls:
                        self.reported_urls.add(identity)
                        self.rows.append(row)
                        self.on_result(row, self.stats.result_pages_discovered)
                    else:
                        self.stats.aliases_merged += 1
                else:
                    self.stats.auxiliary_traversed += 1
                if row["HTTP"] == 429 or row["Browser HTTP"] == 429:
                    self.message("429 rate limit detected. Scan stopped to avoid extra requests.")
                    break
            if self.stop.is_set():
                return self.rows, "Scan stopped. Export includes result pages checked so far."
            msg = (f"Scan finished. {self.stats.result_pages_discovered} candidate page(s) discovered; "
                   f"{len(self.rows)} unique result(s) remain after cleanup. Count is NOT the site's known total.")
            if self.stats.aliases_merged:
                msg += f" {self.stats.aliases_merged} redirect/canonical duplicate URL(s) were merged."
            if self.stats.auxiliary_traversed and not self.include_archives:
                msg += (f" {self.stats.auxiliary_traversed} archive/navigation URL(s) were crawled for discovery "
                        "but omitted from results (author/category/tag/search/pagination/feed).")
            if self.stats.root_status in (401, 403):
                msg += (" Homepage returned HTTP " + str(self.stats.root_status) +
                    " to the scanner. Blocked pages are NOT confirmed broken; inaccessible HTML limits discovery.")
            if self.stats.browser_working:
                msg += f" {self.stats.browser_working} scanner-unverified page(s) loaded successfully in Chromium."
            if self.stats.browser_unavailable:
                msg += " " + self.stats.browser_unavailable
            if self.stats.denied_by_robots:
                msg += " Some pages were not crawled because of robots.txt."
            if self.stats.hit_limit:
                msg += " Optional page limit reached; clear it for unlimited discovery."
            msg += " Login-only, unlinked, restricted and some JS-only pages may still be missing."
            return self.rows, msg
        finally:
            self.close_browser()


def read_bounded(resp, size: int) -> bytes:
    chunks = []
    total = 0
    for chunk in resp.iter_content(chunk_size=32768):
        if chunk:
            total += len(chunk)
            if total > size:
                raise ValueError("Response exceeds download size limit")
            chunks.append(chunk)
    return b"".join(chunks)


EXPORT_HEADERS = ["#", "Page URL", "Status", "Scanner HTTP", "Browser HTTP", "Page title", "Found via", "Notes"]


def spreadsheet_safe(value):
    """Treat untrusted site text as plain text in Excel and CSV."""
    if not isinstance(value, str):
        return value
    return "'" + value if value.lstrip()[:1] in ("=", "+", "-", "@") or value[:1] in ("\t", "\r", "\n") else value


def export_values(i: int, row: dict) -> list:
    return [i, spreadsheet_safe(row.get("URL", "")), spreadsheet_safe(row.get("Status", "")),
            row.get("HTTP", ""), row.get("Browser HTTP", ""),
            spreadsheet_safe(row.get("Title", "")), spreadsheet_safe(row.get("Source", "")),
            spreadsheet_safe(row.get("Notes", ""))]


def export_csv(rows: list[dict]) -> bytes:
    import csv
    from io import StringIO
    out = StringIO(newline="")
    writer = csv.writer(out)
    writer.writerow(EXPORT_HEADERS)
    for i, row in enumerate(rows, 1):
        writer.writerow(export_values(i, row))
    return ("\ufeff" + out.getvalue()).encode("utf-8")  # Excel-friendly UTF-8 BOM


def export_xlsx(rows: list[dict]) -> bytes:
    """Readable rows; long Notes remain visible via selection, never grow row height."""
    from io import BytesIO
    from openpyxl.utils import get_column_letter

    book = Workbook(write_only=False)
    overview = book.active
    overview.title = "Summary"
    overview.append(["SITESCOPE", "Website Page Discovery & Verification Tool"])
    overview.append(["Total checked", len(rows)])
    for status in ("Working", "Working (browser)", "Not working", "Blocked", "Server error", "Error", "Not checked"):
        overview.append([status, sum(r.get("Status") == status for r in rows)])
    overview.append(["Note", "HTTP 403 alone is not proof that a page is broken. Scanner and browser results can differ."])
    overview.append(["Discovery", "Only public, discoverable URLs are included; this is not a guaranteed full site count."])
    overview.column_dimensions["A"].width = 28
    overview.column_dimensions["B"].width = 94
    overview.freeze_panes = "A2"
    overview["A1"].font = Font(bold=True, color="FFFFFF", size=14)
    overview["B1"].font = Font(bold=True, color="FFFFFF")
    for c in overview[1]:
        c.fill = PatternFill("solid", fgColor="172B4D")
    overview.row_dimensions[1].height = 30
    for i in range(2, overview.max_row + 1):
        overview.row_dimensions[i].height = 22
        overview.cell(i, 1).font = Font(bold=True, color="23415E")

    # Excel allows 1,048,576 rows per worksheet; split automatically when needed.
    capacity = 1_048_575
    for offset in range(0, len(rows), capacity):
        part = rows[offset:offset + capacity]
        ws = book.create_sheet("Pages" if offset == 0 else f"Pages {offset // capacity + 1}")
        ws.append(EXPORT_HEADERS)
        ws.row_dimensions[1].height = 29
        for cell in ws[1]:
            cell.fill = PatternFill("solid", fgColor="172B4D")
            cell.font = Font(bold=True, color="FFFFFF", size=10)
            cell.alignment = Alignment(vertical="center", wrap_text=False)
        for i, row in enumerate(part, offset + 1):
            ws.append(export_values(i, row))
            excel_row = i - offset + 1
            ws.row_dimensions[excel_row].height = 21  # NO giant wrapped rows
            url_cell = ws.cell(excel_row, 2)
            url_cell.hyperlink = row["URL"]
            url_cell.font = Font(color="0563C1", underline="single", size=10)
            status_cell = ws.cell(excel_row, 3)
            status = row.get("Status", "")
            color = "166534" if status.startswith("Working") else "9A3412" if status == "Blocked" else "9F1239" if status == "Not working" else "475569"
            status_cell.font = Font(bold=True, color=color, size=10)
            if excel_row % 2 == 0:
                for cell in ws[excel_row]:
                    cell.fill = PatternFill("solid", fgColor="F3F7FC")
            for cell in ws[excel_row]:
                cell.alignment = Alignment(vertical="center", wrap_text=False)
        for col, width in {"A": 7, "B": 63, "C": 23, "D": 16, "E": 16, "F": 49, "G": 30, "H": 64}.items():
            ws.column_dimensions[col].width = width
        ws.freeze_panes = "C2"
        ws.auto_filter.ref = f"A1:H{len(part) + 1}"
    if not rows:
        book.create_sheet("Pages").append(EXPORT_HEADERS)
    data = BytesIO()
    book.save(data)
    return data.getvalue()
