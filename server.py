"""Local background-crawler UI using Python's built-in HTTP server (no Streamlit)."""
from __future__ import annotations

from datetime import datetime, timezone
import io
import json
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import requests
from bs4 import BeautifulSoup
from crawler import Crawler, export_xlsx, export_csv, normalize, public_host

HERE = Path(__file__).parent
JOBS: dict[str, "Job"] = {}
JOBS_LOCK = threading.Lock()


def _entry_family(host: str) -> str:
    host = (host or "").lower().rstrip(".")
    return host[4:] if host.startswith("www.") else host


def _same_entry_family(candidate_host: str, input_host: str) -> bool:
    """Allow the entered host plus its subdomains (e.g. www.example.com -> pk.example.com)."""
    candidate_host = (candidate_host or "").lower().rstrip(".")
    input_host = (input_host or "").lower().rstrip(".")
    base = _entry_family(input_host)
    return candidate_host in {input_host, base, "www." + base} or candidate_host.endswith("." + base)


def _entry_label(text: str, url: str) -> str:
    clean = " ".join((text or "").split()).strip()[:80]
    if clean:
        return clean
    host = urlsplit(url).hostname or url
    return host


def detect_entry_options(raw_url: str) -> dict:
    """Inspect only the supplied public landing page and suggest user-selectable site versions.

    This is intentionally a preflight helper, not an automatic cross-domain crawler. It does not
    choose a country/language on the user's behalf.
    """
    start = normalize(raw_url if "://" in raw_url else "https://" + raw_url)
    if not start:
        raise ValueError("Enter a valid public website URL.")
    input_host = urlsplit(start).hostname or ""
    if not public_host(input_host):
        raise ValueError("Website must be a public domain.")

    links = []
    page_title = ""
    browser_note = ""
    country_words = {
        "pakistan","global","international","united kingdom","uk","united states","usa","us",
        "uae","united arab emirates","canada","australia","india","saudi","ksa","qatar",
        "english","arabic","français","french","deutsch","german","español","spanish"
    }
    # Browser first, because country/language gates are commonly rendered with JavaScript.
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            context = browser.new_context(ignore_https_errors=False, service_workers="block")
            page = context.new_page()
            page.goto(start, wait_until="domcontentloaded", timeout=18000)
            try:
                page.wait_for_timeout(450)
            except Exception:
                pass
            page_title = (page.title() or "")[:160]
            for item in page.locator("a[href]").all():
                try:
                    href = item.get_attribute("href")
                    text = item.inner_text(timeout=500)
                except Exception:
                    continue
                target = normalize(href or "", page.url)
                if target:
                    links.append((text, target))

            # Some entry gates use JavaScript buttons instead of normal links. Probe only
            # obvious country/language labels in isolated pages and record the resulting URL.
            labels_to_probe = []
            try:
                for el in page.locator("a,button,[role='button']").all():
                    try:
                        txt = " ".join((el.inner_text(timeout=350) or "").split()).strip()[:80]
                    except Exception:
                        continue
                    low = txt.lower()
                    if txt and any(word == low or word in low for word in country_words):
                        if txt not in labels_to_probe:
                            labels_to_probe.append(txt)
                    if len(labels_to_probe) >= 10:
                        break
            except Exception:
                labels_to_probe = []
            for label in labels_to_probe:
                probe = context.new_page()
                try:
                    probe.goto(start, wait_until="domcontentloaded", timeout=12000)
                    locator = probe.get_by_text(label, exact=True).first
                    if locator.count() == 0:
                        locator = probe.get_by_text(label, exact=False).first
                    locator.click(timeout=2500)
                    try:
                        probe.wait_for_load_state("domcontentloaded", timeout=7000)
                    except Exception:
                        probe.wait_for_timeout(1200)
                    target = normalize(probe.url)
                    if target and target.rstrip("/") != start.rstrip("/"):
                        links.append((label, target))
                except Exception:
                    pass
                finally:
                    probe.close()
            browser.close()
    except Exception as exc:
        browser_note = f"Chromium preflight unavailable ({type(exc).__name__}); used HTML fallback."
        try:
            sess = requests.Session(); sess.trust_env = False
            resp = sess.get(start, timeout=(4, 10), headers={"User-Agent":"SiteScope/1.0 (entry preflight)"})
            page_title = BeautifulSoup(resp.text[:2_000_000], "html.parser").title.get_text(" ", strip=True)[:160] if resp.text else ""
            soup = BeautifulSoup(resp.text[:2_000_000], "html.parser")
            for a in soup.select("a[href]"):
                target = normalize(a.get("href") or "", resp.url)
                if target:
                    links.append((a.get_text(" ", strip=True), target))
            resp.close(); sess.close()
        except Exception:
            pass

    # Keep only the entered host/family. Cross-subdomain candidates are strong signals of
    # regional storefronts; shallow country/language-looking links on the same host are also useful.
    seen = set(); options = []
    start_host = (urlsplit(start).hostname or "").lower()
    for text, target in links:
        p = urlsplit(target); host = (p.hostname or "").lower()
        if not host or not _same_entry_family(host, start_host):
            continue
        shallow = len([x for x in p.path.split("/") if x]) <= 2
        label = _entry_label(text, target)
        lower_label = label.lower()
        cross_subdomain = host not in {start_host, _entry_family(start_host), "www." + _entry_family(start_host)}
        looks_regional = any(word == lower_label or word in lower_label for word in country_words)
        if not (cross_subdomain or (looks_regional and shallow)):
            continue
        key = target.rstrip("/")
        if key in seen:
            continue
        seen.add(key)
        options.append({"label": label, "url": target, "host": host})
        if len(options) >= 12:
            break

    # Include the originally entered URL as a neutral choice.
    result = [{"label": "Current / default site", "url": start, "host": input_host}]
    for item in options:
        if item["url"].rstrip("/") != start.rstrip("/"):
            result.append(item)
    return {
        "start_url": start, "page_title": page_title, "options": result,
        "detected": len(result) > 1, "note": browser_note,
        "message": ("Site versions/entry options detected. Choose the version you want to scan."
                    if len(result) > 1 else
                    "No separate country/language site version was detected. You can scan the entered URL normally.")
    }



class Job:
    def __init__(self, url: str, max_pages: int | None, delay: float, browser_mode: bool = False,
                 include_archives: bool = False):
        self.id = uuid.uuid4().hex
        self.url, self.max_pages, self.delay, self.browser_mode = url, max_pages, delay, browser_mode
        self.include_archives = include_archives
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.rows: list[dict] = []
        self.discovered = 0
        self.counts = {name: 0 for name in ("Working", "Working (browser)", "Not working", "Blocked", "Server error", "Error", "Not checked")}
        self.state = "running"
        self.message = "Starting scan..."
        self.crawler = None

    def add(self, row: dict, discovered: int):
        with self.lock:
            self.rows.append(row)
            self.counts[row["Status"]] = self.counts.get(row["Status"], 0) + 1
            self.discovered = discovered

    def note(self, message: str):
        with self.lock:
            self.message = message

    def execute(self):
        try:
            crawler = Crawler(self.url, self.max_pages, self.delay, self.add, self.note, self.stop,
                              browser_mode=self.browser_mode, include_archives=self.include_archives)
            self.crawler = crawler
            _rows, summary = crawler.run()
            crawler.session.close()
            with self.lock:
                self.message = summary
                self.state = "stopped" if self.stop.is_set() else "finished"
        except Exception as exc:
            with self.lock:
                self.state = "error"
                self.message = f"Scan failed: {type(exc).__name__}: {exc}"

    def snapshot(self, after: int = 0):
        with self.lock:
            batch = self.rows[after:after + 200]
            aliases = self.crawler.stats.aliases_merged if self.crawler is not None else 0
            auxiliary = self.crawler.stats.auxiliary_traversed if self.crawler is not None else 0
            discovered = self.crawler.stats.result_pages_discovered if self.crawler is not None else self.discovered
            return {"job_id": self.id, "state": self.state, "message": self.message,
                    "discovered": discovered, "unique_results": len(self.rows),
                    "checked": len(self.rows), "aliases_merged": aliases,
                    "auxiliary_omitted": auxiliary,
                    "counts": dict(self.counts), "rows": batch,
                    "has_more": after + len(batch) < len(self.rows), "max_pages": self.max_pages}


class Handler(BaseHTTPRequestHandler):
    def respond(self, code: int, data: bytes, content_type: str, *, filename: str | None = None):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if filename:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def json(self, code: int, payload: dict):
        self.respond(code, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def do_GET(self):
        target = urlsplit(self.path)
        path = target.path
        if path in ("/", "/static/app.js", "/static/style.css"):
            name = "index.html" if path == "/" else path.rsplit("/", 1)[-1]
            content_type = "text/html; charset=utf-8" if name.endswith(".html") else ("text/css; charset=utf-8" if name.endswith(".css") else "text/javascript; charset=utf-8")
            self.respond(200, (HERE / "static" / name).read_bytes(), content_type)
            return
        if path.startswith("/api/status/"):
            job = JOBS.get(path.rsplit("/", 1)[-1])
            if not job:
                self.json(404, {"error": "Scan not found"})
                return
            try:
                after = max(0, int(parse_qs(target.query).get("after", ["0"])[0]))
            except ValueError:
                after = 0
            self.json(200, job.snapshot(after))
            return
        if path.startswith("/api/download/") or path.startswith("/api/csv/"):
            csv_mode = path.startswith("/api/csv/")
            job = JOBS.get(path.rsplit("/", 1)[-1])
            if not job:
                self.json(404, {"error": "Scan not found"})
                return
            with job.lock:
                rows = list(job.rows)
            if not rows:
                self.json(400, {"error": "No pages to export yet"})
                return
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            if csv_mode:
                data = export_csv(rows)
                self.respond(200, data, "text/csv; charset=utf-8", filename=f"sitescope_pages_{stamp}.csv")
            else:
                data = export_xlsx(rows)
                self.respond(200, data, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                             filename=f"sitescope_pages_{stamp}.xlsx")
            return
        self.json(404, {"error": "Not found"})

    def do_POST(self):
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 8192:
                self.json(400, {"error": "Invalid input length"})
                return
            data = json.loads(self.rfile.read(size))
            if not isinstance(data, dict):
                raise ValueError()
        except (ValueError, json.JSONDecodeError):
            self.json(400, {"error": "Invalid JSON request"})
            return
        if self.path == "/api/detect-entry":
            url = str(data.get("url", "")).strip()
            if len(url) > 2048 or not url:
                self.json(400, {"error": "Valid website URL daalein."})
                return
            try:
                self.json(200, detect_entry_options(url))
            except ValueError as exc:
                self.json(400, {"error": str(exc)})
            except Exception as exc:
                self.json(500, {"error": f"Entry detection failed: {type(exc).__name__}"})
            return
        if self.path == "/api/start":
            url = str(data.get("url", "")).strip()
            if len(url) > 2048 or not url:
                self.json(400, {"error": "Valid website URL daalein."})
                return
            try:
                raw_limit = data.get("max_pages")
                max_pages = None if raw_limit in (None, "") else int(raw_limit)
                delay = float(data.get("delay", 0.5))
            except (TypeError, ValueError):
                self.json(400, {"error": "Page limit aur delay numbers hone chahiye."})
                return
            if (max_pages is not None and max_pages < 1) or not 0.5 <= delay <= 10:
                self.json(400, {"error": "Page limit khaali rakhein ya positive integer daalein; delay 0.5–10 seconds."})
                return
            try:
                check = Crawler(url, max_pages, delay)
                check.session.close()
            except ValueError as exc:
                self.json(400, {"error": str(exc)})
                return
            with JOBS_LOCK:
                if any(j.state == "running" for j in JOBS.values()):
                    self.json(409, {"error": "Ek scan already chal raha hai. Pehle Stop dabayein."})
                    return
                for key in list(JOBS)[:-4]:
                    if JOBS[key].state != "running":
                        JOBS.pop(key, None)
                job = Job(url, max_pages, delay, browser_mode=data.get("browser_mode") is True,
                          include_archives=data.get("include_archives") is True)
                JOBS[job.id] = job
            threading.Thread(target=job.execute, daemon=True).start()
            self.json(201, {"job_id": job.id})
            return
        if self.path.startswith("/api/stop/"):
            job = JOBS.get(self.path.rsplit("/", 1)[-1])
            if not job:
                self.json(404, {"error": "Scan not found"})
                return
            job.stop.set()
            job.note("Stopping after the current request...")
            self.json(200, {"ok": True})
            return
        self.json(404, {"error": "Not found"})

    def log_message(self, format, *args):
        if not self.path.startswith("/api/status/"):
            super().log_message(format, *args)


def serve(port: int = 8766, open_browser: bool = True):
    import webbrowser
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{server.server_port}/"
    print(f"SiteScope running: {url}", flush=True)
    if open_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")
    finally:
        server.server_close()


if __name__ == "__main__":
    serve()
