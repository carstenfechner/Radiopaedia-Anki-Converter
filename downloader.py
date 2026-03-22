#!/usr/bin/env python3
"""
Radiopaedia Case Loader
Downloads all images from every series in a Radiopaedia case viewer.

The page preloads all series images automatically, so no frame-by-frame navigation
is needed. We simply intercept all image network requests during page load and map
them to the correct series/order using the annotated_viewer_json API.

Usage:
    python downloader.py <case_url> [options]

Example:
    python downloader.py https://radiopaedia.org/cases/creutzfeldt-jakob-disease
    python downloader.py https://radiopaedia.org/cases/creutzfeldt-jakob-disease -o ./downloads
    python downloader.py https://radiopaedia.org/cases/creutzfeldt-jakob-disease --headed

Install dependencies:
    pip install -r requirements.txt
    playwright install chromium
"""

import argparse
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import requests
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
from tqdm import tqdm

try:
    from PIL import Image as _PILImage
    _PILLOW_AVAILABLE = True
except ImportError:
    _PILLOW_AVAILABLE = False

# WebP conversion quality (0-100). 80 gives ~90% size reduction vs PNG/JPG.
WEBP_QUALITY = 80

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BASE_URL = "https://radiopaedia.org"
IMAGE_CDN = "prod-images-static.radiopaedia.org/images/"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)
# Suffixes that indicate a thumbnail/variant — we want the original (no suffix)
THUMBNAIL_SUFFIXES = ("_thumb.", "_small.", "_tiny.", "_gallery.", "_medium.")


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class SeriesInfo:
    series_id: int
    name: str               # e.g. "Axial FLAIR"
    frame_ids: list[int]    # frame IDs in correct anatomical order (first → last)
    content_type: str       # "image/jpeg" or "image/png"
    urls: list[str] = field(default_factory=list)  # filled in after interception
    thumbnailed_files: list[dict] = field(default_factory=list)  # from encodings


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def is_original_image_url(url: str) -> bool:
    """Return True if this URL is an original (non-thumbnail) content image."""
    if IMAGE_CDN not in url:
        return False
    url_lower = url.lower()
    if any(s in url_lower for s in THUMBNAIL_SUFFIXES):
        return False
    return True


def frame_id_from_url(url: str) -> Optional[int]:
    """Extract the frame ID from a URL like .../images/2150661/hash.jpg"""
    m = re.search(r"/images/(\d+)/", url)
    return int(m.group(1)) if m else None


def ext_from_content_type(content_type: str) -> str:
    return "png" if "png" in content_type else "jpg"


def safe_name(s: str) -> str:
    """Convert series name to a filesystem-safe string."""
    return re.sub(r"[^\w\s-]", "", s).strip().replace(" ", "_")


def _get_text(page, selector: str) -> str:
    """Extract inner text from the first matching DOM element, or '' on failure."""
    try:
        return page.inner_text(selector, timeout=3000).strip()
    except Exception:
        return ""


def _get_html(page, selector: str) -> str:
    """Extract innerHTML from the first matching DOM element, or '' on failure."""
    try:
        return page.inner_html(selector, timeout=3000).strip()
    except Exception:
        return ""


def url_type(url: str) -> str:
    if "/cases/" in url:
        return "case"
    if "/articles/" in url:
        return "article"
    return "unknown"


def convert_to_webp(src: Path, quality: int = WEBP_QUALITY) -> Path:
    """
    Convert *src* (jpg/png) to WebP at the given quality, delete the original,
    and return the path of the new .webp file.
    Raises RuntimeError if Pillow is not installed.
    """
    if not _PILLOW_AVAILABLE:
        raise RuntimeError(
            "Pillow is not installed — cannot convert to WebP. "
            "Run: pip install pillow"
        )
    dest = src.with_suffix(".webp")
    with _PILImage.open(src) as img:
        img.save(dest, "WEBP", quality=quality, method=4)
    src.unlink()
    return dest


def find_linked_case_urls(page) -> list[str]:
    """
    Return absolute case URLs from the 'Cases and figures' section of an article page.
    Tries three strategies in order:
      1. The known hashed class _2tl3bx1 (may change between Radiopaedia deploys)
      2. A heading whose text contains 'Cases and figures' → walk up to its container
      3. Any element whose class attribute contains the substring '_2tl3bx1'
    Returns a deduplicated list preserving DOM order, or [] if nothing found.
    """
    raw: list[str] = page.evaluate(r"""() => {
        const base = 'https://radiopaedia.org';

        function absolutify(href) {
            if (!href) return null;
            if (href.startsWith('http')) return href;
            return base + href;
        }

        function caseLinks(root) {
            return Array.from(root.querySelectorAll('a[href*="/cases/"]'))
                .map(a => absolutify(a.getAttribute('href')))
                .filter(Boolean);
        }

        // Strategy 1: exact hashed class
        const byClass = document.querySelector('._2tl3bx1');
        if (byClass) return caseLinks(byClass);

        // Strategy 2: heading text "Cases and figures"
        const headings = Array.from(document.querySelectorAll('h2, h3, h4, h5'));
        const casesHeading = headings.find(h =>
            h.textContent.trim().toLowerCase().includes('cases and figures'));
        if (casesHeading) {
            // Walk up until we find a container that holds case links
            let el = casesHeading.parentElement;
            for (let i = 0; i < 5; i++) {
                if (!el) break;
                const links = caseLinks(el);
                if (links.length > 0) return links;
                el = el.parentElement;
            }
        }

        // Strategy 3: partial class substring match
        const all = Array.from(document.querySelectorAll('[class]'));
        const partial = all.find(el => el.className.includes('_2tl3bx1'));
        if (partial) return caseLinks(partial);

        return [];
    }""")

    # Deduplicate while preserving order; strip query params and URL fragments (#anchor)
    seen: set[str] = set()
    result: list[str] = []
    for u in raw:
        clean = u.split("?")[0].split("#")[0]
        if clean not in seen:
            seen.add(clean)
            result.append(clean)
    return result


# ---------------------------------------------------------------------------
# Article download
# ---------------------------------------------------------------------------

def download_article(url: str, output_dir: Path, headed: bool, delay_ms: int,
                     progress_cb=None, webp: bool = True) -> dict:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headed)
        context = browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1280, "height": 800},
        )
        page = context.new_page()

        print(f"Opening browser{'  (headed)' if headed else ' (headless)'}...")
        print(f"Loading: {url}")
        page.goto(url, wait_until="networkidle", timeout=90000)

        if "/users/sign_in" in page.url:
            browser.close()
            raise RuntimeError("Radiopaedia requires login for this article.")

        time.sleep(delay_ms / 1000.0)

        # Extract rID from the citation block's rid row
        rid = _get_text(page, ".row.rid .col-sm-8")
        if not rid:
            # Fallback: numeric ID from the canonical page URL
            m = re.search(r"/articles/(\d+)", page.url)
            rid = m.group(1) if m else "unknown"
        rid = rid.strip()

        # Extract plain title for <title> tag
        plain_title = _get_text(page, "h1.header-title")

        # Extract HTML fragments
        title_html    = _get_html(page, "h1.header-title")
        body_html     = _get_html(page, ".body.user-generated-content")
        citation_html = _get_html(page, ".citation-info .js-content")

        # Find linked case URLs while the page is still open
        linked_case_urls = find_linked_case_urls(page)

        browser.close()

    print(f"\nArticle: {plain_title}  (rID: {rid})")
    if linked_case_urls:
        print(f"Found {len(linked_case_urls)} linked case(s) in 'Cases and figures'.")
    else:
        print("No linked cases found in 'Cases and figures'.")

    # --- Save article HTML ---
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="source-url" content="{url}">
  <meta name="rid" content="{rid}">
  <title>{plain_title}</title>
  <!-- <link rel="stylesheet" href="style.css"> -->
</head>
<body>
  <header class="article-header">
    <h1 class="header-title header-title-favourite">{title_html}</h1>
  </header>
  <main class="article-body">
    <div class="body user-generated-content">{body_html}</div>
  </main>
  <footer class="article-citation">
    <div class="js-content content">{citation_html}</div>
  </footer>
</body>
</html>
"""
    dest = output_dir / f"{rid}.html"
    dest.write_text(html, encoding="utf-8")
    print(f"Saved article HTML: {dest.resolve()}")

    # --- Download each linked case and collect full run() dicts ---
    linked_cases: list[dict] = []   # list of run() return dicts

    if linked_case_urls:
        print()
        for i, case_url in enumerate(linked_case_urls, start=1):
            slug = case_url.rstrip("/").split("/")[-1]
            print(f"\n[Case {i}/{len(linked_case_urls)}] {slug}")
            if progress_cb:
                progress_cb({"type": "progress",
                             "message": f"  Linked case {i}/{len(linked_case_urls)}: {slug}"})
            case_results = run(
                case_url=case_url,
                output_dir=output_dir,
                delay_ms=delay_ms,
                headed=headed,
                filter_series=None,
                progress_cb=progress_cb,
                webp=webp,
            )
            linked_cases.extend(case_results)

    # --- Write linked-cases index file ---
    index_file = output_dir / f"{rid}_linked_cases.txt"
    lines = [
        f"Article:  {plain_title}",
        f"rID:      {rid}",
        f"URL:      {url}",
        "",
    ]
    if linked_cases:
        lines.append(f"Linked cases ({len(linked_cases)} total — Cases and figures section):")
        for i, c in enumerate(linked_cases, start=1):
            lines.append(
                f"  {i:>2}. rID: {c['rid']:<8}  study: {c['case_id']}"
            )
    else:
        lines.append("No linked cases found in 'Cases and figures' section.")
    lines.append("")

    index_file.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nLinked-cases index saved: {index_file.resolve()}")

    # Build Anki-ready content_html (body content only, no full HTML doc wrapper)
    content_html = (
        f'<h1 class="header-title header-title-favourite">{title_html}</h1>\n'
        f'<div class="body user-generated-content">{body_html}</div>\n'
        f'<div class="article-citation">'
        f'<div class="js-content content">{citation_html}</div>'
        f'</div>'
    )

    return {
        "rid":          rid,
        "plain_title":  plain_title,
        "content_html": content_html,
        "linked_cases": linked_cases,
    }


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------

def run(case_url: str, output_dir: Path, delay_ms: int, headed: bool,
        filter_series: Optional[list[str]],
        progress_cb=None,
        webp: bool = True) -> list[dict]:
    """
    Download images from all viewers on a Radiopaedia case page.
    Returns a list of dicts — one per viewer, ordered top-to-bottom as on the page.
    Each dict has the same structure as before, but case_id is the study_id (unique
    per viewer) so that filenames never clash when one case page has multiple viewers.
    """
    case_slug = case_url.rstrip("/").split("/")[-1].split("?")[0]

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headed)
        context = browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1280, "height": 800},
        )
        page = context.new_page()

        captured: dict[int, str] = {}          # frame_id → url (all viewers share this)
        all_viewer_jsons: dict[int, dict] = {} # study_id → full json payload

        def on_response(response):
            url = response.url
            if "annotated_viewer_json" in url and response.status == 200:
                try:
                    data = response.json()
                    study = data.get("study", data)
                    sid = study.get("id")
                    if sid:
                        all_viewer_jsons[sid] = data
                except Exception:
                    pass
            elif is_original_image_url(url) and response.status == 200:
                fid = frame_id_from_url(url)
                if fid is not None:
                    captured[fid] = url

        page.on("response", on_response)

        print(f"Opening browser{'  (headed)' if headed else ' (headless)'}...")
        print(f"Loading: {case_url}")
        page.goto(case_url, wait_until="networkidle", timeout=90000)

        if "/users/sign_in" in page.url:
            browser.close()
            raise RuntimeError("Radiopaedia requires login for this case.")

        time.sleep(delay_ms / 1000.0)

        if not all_viewer_jsons:
            browser.close()
            raise RuntimeError(
                "Could not find the viewer metadata API response. "
                "Make sure this URL is a Radiopaedia case with an image viewer."
            )

        # Order viewers by vertical position on page (topmost = first).
        # Viewer containers use id="study-{study_id}".
        dom_order: list[int] = page.evaluate("""
            () => Array.from(document.querySelectorAll("[id^='study-']"))
                       .map(el => ({
                           sid: parseInt(el.id.replace('study-', '')),
                           top: el.getBoundingClientRect().top + window.scrollY
                       }))
                       .filter(v => !isNaN(v.sid))
                       .sort((a, b) => a.top - b.top)
                       .map(v => v.sid)
        """)
        ordered_viewer_data: list[dict] = []
        seen: set[int] = set()
        for sid in dom_order:
            if sid in all_viewer_jsons and sid not in seen:
                ordered_viewer_data.append(all_viewer_jsons[sid])
                seen.add(sid)
        # Fallback: any viewer not found via DOM selector
        for sid, data in all_viewer_jsons.items():
            if sid not in seen:
                ordered_viewer_data.append(data)

        n = len(ordered_viewer_data)
        if n > 1:
            print(f"  Found {n} viewers on page — will create {n} sub-cases (top→bottom)")

        # Extract page-level metadata (shared across all viewers)
        page_title         = _get_text(page, "h1.header-title")
        page_findings      = _get_text(page, ".study-findings, .body.sub-section")
        page_findings_html = _get_html(page, ".study-findings, .body.sub-section")
        page_citation      = _get_text(page, ".citation-info .js-content")
        page_citation_html = _get_html(page, ".citation-info .js-content")

        browser.close()

    # --- Process each viewer ---
    CDN_BASE = f"https://{IMAGE_CDN}"
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Referer": BASE_URL, "Accept": "*/*"})

    results: list[dict] = []

    for viewer_data in ordered_viewer_data:
        study    = viewer_data.get("study", viewer_data)
        study_id = study.get("id", "?")
        rid      = str(study.get("case_id", study_id))  # rID

        raw_series = study.get("series", [])
        if not raw_series:
            continue

        series_list: list[SeriesInfo] = []
        for s in raw_series:
            perspective = (s.get("perspective") or "").strip()
            specifics   = (s.get("specifics") or "").strip()
            name = f"{perspective} {specifics}".strip() or f"Series_{s.get('series_id', '?')}"
            frames = s.get("frames", [])
            frame_ids = [f["id"] for f in frames]
            thumbnailed_files = (s.get("encodings") or {}).get("thumbnailed_files") or []
            series_list.append(SeriesInfo(
                series_id=s["series_id"],
                name=name,
                frame_ids=frame_ids,
                content_type=s.get("content_type", "image/jpeg"),
                thumbnailed_files=thumbnailed_files,
            ))

        label = f" (viewer {len(results)+1}/{n})" if n > 1 else ""
        print(f"\nCase: {case_slug}  (rID: {rid}, study ID: {study_id}){label}")
        print(f"Found {len(series_list)} series:")
        for s in series_list:
            print(f"  {s.name}: {len(s.frame_ids)} frames")
        print(f"\nIntercepted {len(captured)} unique image URLs during page load.")

        # Assign URLs — prefer captured, fallback to thumbnailed_files
        for series in series_list:
            ordered_urls = []
            missing = []
            for i, fid in enumerate(series.frame_ids):
                url = captured.get(fid)
                if url:
                    ordered_urls.append(url)
                else:
                    tf = series.thumbnailed_files[i] if i < len(series.thumbnailed_files) else None
                    original_fn = tf.get("original") if tf else None
                    if original_fn:
                        ordered_urls.append(f"{CDN_BASE}{fid}/{original_fn}")
                    else:
                        missing.append(fid)
                        ordered_urls.append(None)
            series.urls = ordered_urls
            if missing:
                print(f"  Warning: {series.name} — {len(missing)} frame(s) not captured "
                      f"(IDs: {missing[:5]}{'...' if len(missing) > 5 else ''})")

        # Use study_id as directory/file prefix to avoid cross-viewer name clashes
        case_dir = output_dir / f"{study_id}_{case_slug}"
        case_dir.mkdir(parents=True, exist_ok=True)

        # Determine effective extension per series (webp if conversion is on)
        effective_ext: dict[int, str] = {}  # series_id → final ext

        total_ok = total_skip = 0
        for series in series_list:
            if filter_series and series.name not in filter_series:
                continue
            if not any(u for u in series.urls):
                print(f"\nSkipping '{series.name}': no URLs captured.")
                continue

            raw_ext = ext_from_content_type(series.content_type)
            final_ext = "webp" if (webp and _PILLOW_AVAILABLE) else raw_ext
            effective_ext[series.series_id] = final_ext
            sname = safe_name(series.name)
            print(f"\nDownloading: {series.name} ({sum(1 for u in series.urls if u)} images)"
                  + (f"  [→ WebP q={WEBP_QUALITY}]" if final_ext == "webp" else ""))

            with tqdm(total=len(series.urls), desc=f"  {series.name}", unit="img") as pbar:
                for idx, url in enumerate(series.urls, start=1):
                    # Files named {study_id}_{safe_series}_{idx:03d}.{final_ext}
                    filename = f"{study_id}_{sname}_{idx:03d}.{final_ext}"
                    dest = case_dir / filename

                    if dest.exists():
                        total_ok += 1; pbar.update(1); continue
                    if url is None:
                        total_skip += 1; pbar.update(1); continue

                    success = False
                    for attempt in range(3):
                        try:
                            resp = session.get(url, timeout=30, stream=True)
                            resp.raise_for_status()
                            if final_ext == "webp":
                                # Write original first, then convert
                                tmp_dest = dest.with_suffix("." + raw_ext)
                                tmp_dest.write_bytes(resp.content)
                                try:
                                    convert_to_webp(tmp_dest)
                                except Exception as conv_err:
                                    # Conversion failed — keep original format
                                    print(f"\n  WebP conversion failed for {filename}: {conv_err}")
                                    tmp_dest.rename(dest.with_suffix("." + raw_ext))
                                    effective_ext[series.series_id] = raw_ext
                            else:
                                dest.write_bytes(resp.content)
                            success = True; break
                        except Exception as e:
                            if attempt < 2:
                                time.sleep(2 ** attempt)
                            else:
                                print(f"\n  Failed: {filename}: {e}")
                    total_ok += 1 if success else 0
                    total_skip += 0 if success else 1
                    pbar.update(1)

        print(f"\nDone. {total_ok} downloaded"
              + (f", {total_skip} skipped" if total_skip else "")
              + f"\nSaved to: {case_dir.resolve()}")

        series_dicts = []
        for s in series_list:
            raw_ext = ext_from_content_type(s.content_type)
            final_ext = effective_ext.get(s.series_id,
                            "webp" if (webp and _PILLOW_AVAILABLE) else raw_ext)
            series_dicts.append({
                "name":       s.name,
                "safe_name":  safe_name(s.name),
                "max_slices": len(s.urls),
                "ext":        final_ext,
            })
        # dominant ext: prefer webp, then png, else jpg
        exts = {sd["ext"] for sd in series_dicts}
        if "webp" in exts:
            dominant_ext = "webp"
        elif "png" in exts:
            dominant_ext = "png"
        else:
            dominant_ext = "jpg"

        results.append({
            "case_id":        str(study_id),   # unique per viewer — used for filenames
            "rid":            rid,              # rID of the case page (for display/GUID)
            "series":         series_dicts,
            "output_dir":     case_dir,
            "file_extension": dominant_ext,
            "findings_html":  page_findings_html,
            "citation_html":  page_citation_html,
        })

    # Write one metadata file in the first viewer's directory
    if results:
        info_file = results[0]["output_dir"] / f"{results[0]['rid']}_info.txt"
        if not info_file.exists():
            lines = [
                f"Title:    {page_title}",
                f"URL:      {case_url}",
                f"Case ID:  {results[0]['rid']}",
                "",
                "=== Findings ===",
                page_findings,
                "",
                "=== Citation ===",
                page_citation,
            ]
            info_file.write_text("\n".join(lines), encoding="utf-8")
            print(f"\nMetadata saved: {info_file.name}")

    return results


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Download all images from a Radiopaedia case viewer.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python downloader.py https://radiopaedia.org/cases/creutzfeldt-jakob-disease
  python downloader.py https://radiopaedia.org/cases/creutzfeldt-jakob-disease -o ./downloads
  python downloader.py https://radiopaedia.org/cases/creutzfeldt-jakob-disease --headed
  python downloader.py https://radiopaedia.org/cases/creutzfeldt-jakob-disease --series "Axial FLAIR"
        """,
    )
    parser.add_argument("url", help="Radiopaedia case URL")
    parser.add_argument("-o", "--output", default=".", help="Output directory (default: current)")
    parser.add_argument("--headed", action="store_true", help="Show browser window (for debugging)")
    parser.add_argument("--delay", type=int, default=500, metavar="MS",
                        help="Extra wait after page load in ms (default: 500)")
    parser.add_argument("--series", action="append", metavar="NAME", dest="series",
                        help="Download only this series (repeatable)")
    parser.add_argument("--no-webp", action="store_true",
                        help="Keep original JPG/PNG instead of converting to WebP")
    args = parser.parse_args()

    output_dir = Path(args.output).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    use_webp = not args.no_webp
    if use_webp and not _PILLOW_AVAILABLE:
        print("Warning: Pillow not installed — WebP conversion disabled. "
              "Run: pip install pillow", file=sys.stderr)
        use_webp = False

    t = url_type(args.url)
    try:
        if t == "case":
            run(
                case_url=args.url,
                output_dir=output_dir,
                delay_ms=args.delay,
                headed=args.headed,
                filter_series=args.series,
                webp=use_webp,
            )
        elif t == "article":
            download_article(
                url=args.url,
                output_dir=output_dir,
                headed=args.headed,
                delay_ms=args.delay,
                webp=use_webp,
            )
        else:
            sys.exit("Error: URL must be a radiopaedia.org/cases/... or /articles/... URL")
    except RuntimeError as e:
        sys.exit(f"Error: {e}")


if __name__ == "__main__":
    main()
