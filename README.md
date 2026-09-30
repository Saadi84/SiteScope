# SiteScope — Website Page Discovery & Verification Tool

SiteScope is a local same-site crawler built to discover publicly reachable internal pages, verify whether they are available, reduce noisy archive/navigation results, export clean Excel or CSV reports, and build redirect plans for confirmed broken URLs. It runs on **http://127.0.0.1:8766** so another local app can keep port 8765.

## Run on Windows

Extract the ZIP, open the folder containing `server.py` and `requirements.txt` in VS Code, then run:

```powershell
python -m pip install -r requirements.txt
python -m playwright install chromium
python server.py
```

Then open **http://127.0.0.1:8766**. After dependencies are installed, `START.bat` can also start SiteScope.

## What SiteScope does

- Discovers same-site pages from XML sitemaps, robots.txt sitemap pointers, recursive HTML links, and optional Chromium-rendered links.
- Verifies scanner HTTP and browser HTTP separately. Scanner 403 + browser 200 is reported as **Working (browser)**. Browser-confirmed 404/410 is reported as **Not working**.
- By default hides common archive/navigation URLs such as author, category, tag, search, feed, and pagination routes from final results while still crawling them to discover older real pages. Nested CMS routes such as `/blog/category/...` are handled too. Enable **Include archive/navigation URLs in results** to show them.
- Has no preset page cap. The optional page limit is blank by default; use **Stop scan** for long runs.
- Filters obvious malformed crawler loops, including very long URLs and repeated path-segment loops, before they enter the crawl queue.
- Cleans final results by merging redirect/canonical aliases and common trailing-slash duplicates such as `/contact` and `/contact/`.
- If the requests scanner has a network error, browser-assisted mode can independently verify the page in Chromium instead of immediately treating it as unavailable.
- The dashboard separates **Discovered candidates**, **Unique results**, and **Duplicates merged**, so the result count is easy to understand.
- Exports clickable Excel reports and UTF-8 CSV files.
- Includes a **Redirect Assistant** for confirmed `Not working` pages. It suggests relevant working destinations from the same scan, but the user always chooses the final destination and 301/302 type.
- Redirect plans can be exported as CSV or copied as Apache `.htaccess` / Nginx rules. This works locally and requires no Google Cloud or hosted service.

## Redirect Assistant

After a scan, any confirmed `Not working` URL appears in Redirect Assistant. SiteScope ranks working pages from the same website using URL/title similarity and shows them as suggestions. The user can also enter a same-site destination manually, choose **301 Permanent** or **302 Temporary**, and add the rule to the plan.

SiteScope intentionally does **not** make an unauthenticated change to a live website. Applying a redirect directly inside WordPress, Shopify, a hosting panel, Apache, Nginx, or another CMS requires credentials or a platform-specific integration. The generated redirect plan is ready to hand to the website owner/developer, or to apply through that platform.

## Important boundaries

SiteScope is designed for public website discovery, not guaranteed server inventory. Login-only, unlinked, restricted, robots-disallowed, or some JavaScript-only pages can still be missing. It does not bypass login, CAPTCHA, bot protection, or access controls. A discovered page count is therefore the number SiteScope found, not proof of the website's absolute total.

Use a considerate request delay, especially on large sites. The crawler stops further requests on HTTP 429 rate limiting.

## Tests

Run:

```powershell
python -m pytest -q
```

The included tests use local servers and browser mocks. They verify crawling, sitemap discovery, robots handling, nested archive filtering, browser verification, network-error recovery, duplicate cleanup, malformed-loop filtering, exports, UI routes, dashboard metrics, and the default port.

## Final hardening

This build adds generic URL cleanup for wider real-world use: confirmed redirect/canonical aliases are merged, common trailing-slash variants are collapsed in final reporting, tracking parameters are removed, query parameters are normalized, repeated path loops are rejected, nested archive routes are filtered, and infrastructure URLs such as Cloudflare `/cdn-cgi/` are excluded. Browser-confirmed 404/410 responses are reported as **Not working**.


## Publish edition

This edition uses the selected **Teal Green `#3D5B59` + Salmon Pink `#FCB5AC`** visual identity throughout the interface, with small interaction/hover improvements for a cleaner showcase experience.


## Smart entry / regional site detection

Some websites first show a country, language, or regional storefront selector instead of the real content site. SiteScope includes a **Detect country / language site versions** preflight helper. It inspects only the entered public landing page, suggests detected same-family site versions (for example a regional subdomain), and lets the user explicitly choose which version to scan. SiteScope never chooses a country/language automatically.

This is useful for sites such as a global landing page that routes visitors to separate regional storefronts. If no separate version is detected, scan the entered URL normally.

## Key product features

- Internal page discovery from sitemaps, HTML links, and optional Chromium rendering
- Browser-assisted verification for scanner-blocked or transient HTTP failures
- URL cleanup, redirect/canonical alias merging, archive/navigation filtering
- Smart country/language/regional entry detection with user-controlled selection
- Redirect Assistant for confirmed broken pages (301/302 plan generation; no live-site edits without credentials)
- Excel and CSV export
- Optional unlimited crawl size; user can stop at any time
