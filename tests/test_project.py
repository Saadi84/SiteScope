"""Local integration test without making live website requests."""
import io
import pathlib
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from crawler import Crawler, normalize, export_xlsx, export_csv, is_auxiliary_url, is_pathological_url
from openpyxl import load_workbook
from server import Handler
from urllib.request import urlopen
import json

class Site(BaseHTTPRequestHandler):
    def do_GET(self):
        path = urlsplit(self.path).path
        host = f"http://127.0.0.1:{self.server.server_port}"
        if path == '/robots.txt':
            code, kind, body = 200, 'text/plain', f'User-agent: *\nDisallow: /private\nSitemap: {host}/sitemap_index.xml'.encode()
        elif path == '/sitemap_index.xml':
            code, kind, body = 200, 'application/xml', f'<sitemapindex><sitemap><loc>{host}/pages.xml</loc></sitemap></sitemapindex>'.encode()
        elif path == '/pages.xml':
            code, kind, body = 200, 'application/xml', f'<urlset><url><loc>{host}/ok</loc></url><url><loc>{host}/blocked</loc></url><url><loc>{host}/missing</loc></url><url><loc>{host}/private</loc></url></urlset>'.encode()
        elif path == '/':
            code, kind, body = 403, 'text/html', b'<h1>Forbidden</h1>'
        elif path == '/ok':
            code, kind, body = 200, 'text/html', b'<title>Yes, working</title><a href="/child">child</a><a href="https://example.org/out">external</a>'
        elif path == '/child':
            code, kind, body = 200, 'text/html', b'<title>Child</title>'
        elif path == '/blocked':
            code, kind, body = 401, 'text/html', b'Protected'
        elif path == '/missing':
            code, kind, body = 404, 'text/html', b'Missing'
        elif path == '/private':
            raise AssertionError('Robots private path was requested')
        else:
            code, kind, body = 404, 'text/plain', b'not found'
        self.send_response(code)
        self.send_header('Content-Type', kind)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self,*args):
        pass


def test_crawler_and_excel():
    server = ThreadingHTTPServer(('127.0.0.1', 0), Site)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        port = server.server_port
        rows, summary = Crawler(f'http://127.0.0.1:{port}/', 50, 0,
                                allow_private_for_tests=True).run()
        status = {urlsplit(row['URL']).path: row['Status'] for row in rows}
        assert status['/'] == 'Blocked', status
        assert status['/ok'] == 'Working', status
        assert status['/missing'] == 'Not working', status
        assert status['/blocked'] == 'Blocked', status
        assert status['/private'] == 'Not checked', status
        assert status['/child'] == 'Working', status
        assert len(rows) == 6, status
        assert 'HTTP 403' in summary
        workbook = load_workbook(io.BytesIO(export_xlsx(rows)))
        sheet = workbook["Pages"]
        assert sheet.max_row == 7
        assert sheet['B2'].hyperlink.target.startswith('http://127.0.0.1:')
        assert sheet['E2'].value is None
        assert sheet.row_dimensions[2].height == 21
        assert workbook['Summary']['B2'].value == 6
        import csv
        csvrows = list(csv.reader(io.StringIO(export_csv(rows).decode('utf-8-sig'))))
        assert len(csvrows) == 7 and csvrows[1][1].startswith('http')
        print('INTEGRATION PASS: HTTP 403 blocked, sitemap index discovered, HTML child found, robots respected, 404 reported, and Excel links valid.')
    finally:
        server.shutdown()
        server.server_close()


def test_browser_verification_mock():
    """Mock Chromium response verifies HTTP 403 != broken and rendered links add URLs."""
    server = ThreadingHTTPServer(('127.0.0.1', 0), Site)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        base = f'http://127.0.0.1:{server.server_port}'
        crawler = Crawler(base + '/', 50, 0, allow_private_for_tests=True)
        crawler.sitemap_discovery()
        class BrowserResponse:
            status = 200
        class BrowserPage:
            url = base + '/blocked'
            def goto(self, url, **kwargs):
                self.url = url
                return BrowserResponse()
            def wait_for_timeout(self, ms):
                pass
            def content(self):
                return '<html><title>Browser OK</title><a href="/js-link">JS link</a></html>'
        crawler.browser_page = BrowserPage()
        row = crawler.check_page(base + '/blocked')
        assert row['HTTP'] == 401 and row['Browser HTTP'] == 200, row
        assert row['Status'] == 'Working (browser)', row
        assert row['Title'] == 'Browser OK', row
        assert base + '/js-link' in crawler.seen, crawler.seen
        sheet = load_workbook(io.BytesIO(export_xlsx([row])))["Pages"]
        assert sheet['D2'].value == 401 and sheet['E2'].value == 200
        assert sheet['B2'].hyperlink.target == base + '/blocked'
        print('BROWSER-MOCK PASS: blocked scanner response becomes browser-verified working with separate HTTP codes, rendered link and valid Excel export.')
    finally:
        server.shutdown()
        server.server_close()


def test_normalization():
    assert normalize('https://example.com/page?utm_source=x&cat=hello#anchor') == 'https://example.com/page?cat=hello'
    assert normalize('https://example.com//about///team/') == 'https://example.com/about/team/'
    assert normalize('javascript:alert(1)') is None
    print('NORMALIZATION PASS: tracking URL removed, duplicate path slashes collapsed, and unsafe URL scheme rejected.')


def test_unlimited_pages_and_ui_rules():
    crawler = Crawler('http://127.0.0.1/', max_pages=None, delay=0,
                      allow_private_for_tests=True)
    for i in range(820):
        assert crawler.add(f'http://127.0.0.1/page-{i}', 'HTML link')
    assert len(crawler.seen) == 821 and not crawler.stats.hit_limit
    limited = Crawler('http://127.0.0.1/', max_pages=3, delay=0,
                      allow_private_for_tests=True)
    for i in range(20): limited.add(f'http://127.0.0.1/page-{i}', 'HTML link')
    assert len(limited.seen) == 3 and limited.stats.hit_limit
    from server import serve, Job
    import inspect
    assert inspect.signature(serve).parameters['port'].default == 8766
    job = Job('https://example.com', None, .5)
    for i in range(250): job.add({'URL':f'https://example.com/{i}', 'Status':'Working'}, 251)
    first = job.snapshot(0)
    assert len(first['rows']) == 200 and first['has_more'] and first['checked'] == 250
    second = job.snapshot(200)
    assert len(second['rows']) == 50 and not second['has_more']
    assert first['counts']['Working'] == 250
    print('UNLIMITED / BATCH PASS: no 500 cap, optional numeric cap, default port 8766, and bounded status responses.')


def test_ui_routes():
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        from urllib.error import HTTPError
        base = f'http://127.0.0.1:{server.server_port}'
        with urlopen(base+'/') as resp:
            assert resp.status == 200 and b'SiteScope' in resp.read()
        with urlopen(base+'/static/app.js') as resp:
            assert resp.status == 200
        from urllib.request import Request
        req = Request(base+'/api/start', data=json.dumps({'url':'http://127.0.0.1/'}).encode(),
                      headers={'Content-Type':'application/json'}, method='POST')
        try:
            urlopen(req)
        except HTTPError as exc:
            assert exc.code == 400
        else:
            raise AssertionError('Private URL was not rejected')
        try:
            urlopen(base+'/api/status/notfound')
        except HTTPError as exc:
            assert exc.code == 404
        else:
            raise AssertionError('Invalid job must return 404')
        print('WEB UI PASS: app and JS load; private URL inputs rejected; invalid jobs return 404.')
    finally:
        server.shutdown()
        server.server_close()


def test_archive_navigation_filter():
    assert is_auxiliary_url("https://example.com/category/news/")
    assert is_auxiliary_url("https://example.com/author/admin/")
    assert is_auxiliary_url("https://example.com/blog/page/2/")
    assert is_auxiliary_url("https://example.com/tag/seo/")
    assert is_auxiliary_url("https://example.com/?s=term")
    assert not is_auxiliary_url("https://example.com/blog/article-name/")
    assert not is_auxiliary_url("https://example.com/services/page-design/")

    class ArchiveSite(BaseHTTPRequestHandler):
        def do_GET(self):
            path = urlsplit(self.path).path
            if path == "/robots.txt":
                body = b"User-agent: *\nAllow: /"
                code, kind = 200, "text/plain"
            elif path == "/":
                body = (b'<title>Home</title><a href="/category/news/">cat</a>'
                        b'<a href="/author/admin/">author</a><a href="/blog/page/2/">older</a>'
                        b'<a href="/regular/">regular</a>')
                code, kind = 200, "text/html"
            elif path == "/category/news/":
                body = b'<title>Category</title><a href="/deep-category/">deep</a>'
                code, kind = 200, "text/html"
            elif path == "/author/admin/":
                body = b'<title>Author</title><a href="/deep-author/">deep</a>'
                code, kind = 200, "text/html"
            elif path == "/blog/page/2/":
                body = b'<title>Page 2</title><a href="/deep-page/">deep</a>'
                code, kind = 200, "text/html"
            elif path in ("/regular/", "/deep-category/", "/deep-author/", "/deep-page/"):
                body = b"<title>Content</title>"
                code, kind = 200, "text/html"
            else:
                body = b"not found"
                code, kind = 404, "text/plain"
            self.send_response(code)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), ArchiveSite)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        rows, summary = Crawler(base + "/", None, 0, allow_private_for_tests=True).run()
        paths = {urlsplit(r["URL"]).path for r in rows}
        assert "/category/news/" not in paths
        assert "/author/admin/" not in paths
        assert "/blog/page/2/" not in paths
        assert {"/", "/regular/", "/deep-category/", "/deep-author/", "/deep-page/"} <= paths, paths
        assert "3 archive/navigation URL(s)" in summary, summary

        rows_all, _ = Crawler(base + "/", None, 0, allow_private_for_tests=True,
                              include_archives=True).run()
        all_paths = {urlsplit(r["URL"]).path for r in rows_all}
        assert {"/category/news/", "/author/admin/", "/blog/page/2/"} <= all_paths, all_paths
        print("ARCHIVE FILTER PASS: archive/pagination routes are traversed for discovery but hidden by default, with opt-in inclusion.")
    finally:
        server.shutdown()
        server.server_close()



def test_pathological_url_filter():
    assert not is_pathological_url("https://example.com/services/shopify-development/company/florida/")
    loop = "https://example.com/my-account/lost-password/" + "/".join(["shopify-development-company-in-florida"] * 4)
    assert is_pathological_url(loop)
    crawler = Crawler("http://127.0.0.1/", None, 0, allow_private_for_tests=True)
    before = len(crawler.seen)
    assert not crawler.add(loop.replace("https://example.com", "http://127.0.0.1"), "HTML link")
    assert len(crawler.seen) == before
    print("PATH LOOP PASS: obvious repeated path loops are rejected without blocking normal deep URLs.")


def test_browser_404_overrides_scanner_block():
    crawler = Crawler("http://127.0.0.1/", None, 0, allow_private_for_tests=True)
    class BrowserResponse:
        status = 404
    class BrowserPage:
        url = "http://127.0.0.1/missing"
        def goto(self, url, **kwargs):
            self.url = url
            return BrowserResponse()
    crawler.browser_page = BrowserPage()
    crawler.can_fetch = lambda url: True
    crawler.wait = lambda: None
    row = {"URL": "http://127.0.0.1/missing", "Status": "Blocked", "HTTP": 403, "Browser HTTP": "", "Title": "", "Source": "HTML link", "Notes": "HTTP scanner was denied; this does NOT mean the page is broken."}
    crawler.inspect_in_browser(row["URL"], row, parse_links=False)
    assert row["Browser HTTP"] == 404
    assert row["Status"] == "Not working", row
    print("BROWSER 404 PASS: Chromium-confirmed 404 overrides a scanner 403 and becomes Not working.")


def test_infrastructure_url_filter():
    from crawler import is_page
    assert not is_page("https://example.com/cdn-cgi/l/email-protection")
    assert not is_page("https://example.com//cdn-cgi/challenge-platform/x")
    assert is_page("https://example.com/contact-us/")
    crawler = Crawler("http://127.0.0.1/", None, 0, allow_private_for_tests=True)
    before = len(crawler.seen)
    assert not crawler.add("http://127.0.0.1/cdn-cgi/l/email-protection", "HTML link")
    assert len(crawler.seen) == before
    print("INFRASTRUCTURE FILTER PASS: Cloudflare /cdn-cgi/ technical URLs are excluded from crawl results.")

if __name__ == "__main__":
    test_crawler_and_excel()
    test_browser_verification_mock()
    test_normalization()
    test_unlimited_pages_and_ui_rules()
    test_ui_routes()
    test_archive_navigation_filter()
    test_pathological_url_filter()
    test_browser_404_overrides_scanner_block()
    test_infrastructure_url_filter()


def test_redirect_alias_deduplication():
    class RedirectSite(BaseHTTPRequestHandler):
        def do_GET(self):
            path = urlsplit(self.path).path
            if path == '/robots.txt':
                body, code, kind = b'User-agent: *\nAllow: /', 200, 'text/plain'
            elif path == '/':
                body = b'<a href="/contact">A</a><a href="/contact/">B</a>'
                code, kind = 200, 'text/html'
            elif path == '/contact':
                self.send_response(301)
                self.send_header('Location', '/contact/')
                self.end_headers()
                return
            elif path == '/contact/':
                body, code, kind = b'<title>Contact</title>', 200, 'text/html'
            else:
                body, code, kind = b'no', 404, 'text/plain'
            self.send_response(code)
            self.send_header('Content-Type', kind)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers(); self.wfile.write(body)
        def log_message(self, *args): pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), RedirectSite)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        base = f'http://127.0.0.1:{server.server_port}'
        rows, summary = Crawler(base + '/', None, 0, allow_private_for_tests=True).run()
        urls = [r['URL'] for r in rows]
        assert urls.count(base + '/contact/') == 1, urls
        assert base + '/contact' not in urls, urls
        assert 'redirect/canonical duplicate URL(s) were merged' in summary, summary
        print('REDIRECT DEDUPE PASS: slash aliases collapse to the confirmed redirect destination.')
    finally:
        server.shutdown(); server.server_close()


def test_query_order_normalization():
    a = normalize('https://example.com/page?b=2&a=1&utm_source=x')
    b = normalize('https://example.com/page?a=1&b=2')
    assert a == b == 'https://example.com/page?a=1&b=2'
    print('QUERY NORMALIZATION PASS: tracking params removed and equivalent query ordering deduplicated.')


def test_nested_archive_routes_are_auxiliary():
    assert is_auxiliary_url("https://example.com/blog/category/daily-strength/")
    assert is_auxiliary_url("https://example.com/articles/tag/seo/")
    assert is_auxiliary_url("https://example.com/news/author/editor/")
    assert not is_auxiliary_url("https://example.com/blog/how-to-build-strength/")


def test_unverified_trailing_slash_variants_merge_in_results():
    from crawler import result_identity
    assert result_identity("https://example.com/registration-page-memberpress") == result_identity(
        "https://example.com/registration-page-memberpress/"
    )
    assert result_identity("https://example.com/?a=1") != result_identity("https://example.com/?a=2")


def test_job_snapshot_exposes_clear_discovery_metrics():
    from server import Job
    job = Job("https://example.com", None, .5)
    job.add({"URL":"https://example.com/a", "Status":"Working"}, 3)
    class Stats:
        aliases_merged = 2
        auxiliary_traversed = 4
        result_pages_discovered = 3
    class DummyCrawler:
        stats = Stats()
    job.crawler = DummyCrawler()
    snap = job.snapshot(0)
    assert snap["discovered"] == 3
    assert snap["unique_results"] == 1
    assert snap["aliases_merged"] == 2
    assert snap["auxiliary_omitted"] == 4


def test_network_error_can_be_recovered_by_browser():
    crawler = Crawler("http://127.0.0.1/", None, 0, allow_private_for_tests=True, browser_mode=True)
    class BrowserResponse:
        status = 200
    class BrowserPage:
        url = "http://127.0.0.1/network-page"
        def goto(self, url, **kwargs):
            self.url = url
            return BrowserResponse()
        def content(self):
            return "<html><title>Recovered</title></html>"
        def wait_for_timeout(self, value):
            pass
    crawler.browser_page = BrowserPage()
    crawler.can_fetch = lambda url: True
    crawler.wait = lambda: None
    crawler.fetch = lambda url: (None, url, "Network error: ConnectionError")
    row = crawler.check_page("http://127.0.0.1/network-page")
    assert row["Status"] == "Working (browser)", row
    assert row["Browser HTTP"] == 200
    assert row["Title"] == "Recovered"

def test_entry_family_detection_rules():
    from server import _same_entry_family
    assert _same_entry_family('pk.khaadi.com', 'www.khaadi.com')
    assert _same_entry_family('uk.example.co.uk', 'www.example.co.uk')
    assert _same_entry_family('www.example.com', 'www.example.com')
    assert not _same_entry_family('facebook.com', 'www.khaadi.com')
    assert not _same_entry_family('evil-khaadi.com', 'www.khaadi.com')


def test_ui_exposes_entry_detection_and_redirect_feature():
    html = (pathlib.Path(__file__).resolve().parents[1] / 'static' / 'index.html').read_text(encoding='utf-8')
    assert 'Detect country / language site versions' in html
    assert 'Redirect Assistant' in html
    assert 'Website Page Discovery, Verification &amp; Redirect Planning Tool' in html
