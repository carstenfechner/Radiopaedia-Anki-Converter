"""
anki_builder.py
Builds Anki .apkg packages from scraped Radiopaedia data.

Article cards  → deck  Articles::{folder_title}
Case cards     → deck  Quiz::{folder_title}
"""

import hashlib
from pathlib import Path

import genanki

# ---------------------------------------------------------------------------
# Stable ID helpers
# ---------------------------------------------------------------------------

def _stable_id(name: str) -> int:
    """Deterministic integer ID from a string (MD5 truncated to 10 digits)."""
    return int(hashlib.md5(name.encode()).hexdigest(), 16) % (10 ** 10)


def _deck_id(name: str) -> int:
    return _stable_id("deck::" + name)


def _model_id(name: str) -> int:
    return _stable_id("model::" + name)


# ---------------------------------------------------------------------------
# Shared viewer JS (used in article back + case front)
# ---------------------------------------------------------------------------

_VIEWER_JS = """\
<script>
(() => {
    function getLatestElement(selector) {
        const els = document.querySelectorAll(selector);
        return els.length > 0 ? els[els.length - 1] : null;
    }

    const globalExt = "{{File Extension}}".trim() || "jpg";
    const viewer = getLatestElement('#medical-viewer');

    let rawHtml = `{{Cases Data}}`;
    let parsedHtml = rawHtml.replace(/<br\\s*\\/?>/gi, '\\n')
                            .replace(/<\\/div>|<\\/p>|<\\/li>/gi, '\\n')
                            .replace(/<[^>]+>/g, '');
    let tempDiv = document.createElement('div');
    tempDiv.innerHTML = parsedHtml;
    const rawData = (tempDiv.textContent || tempDiv.innerText || "").trim();

    if (!rawData) {
        if (viewer) viewer.style.display = 'none';
        return;
    }

    let casesList = [];
    let caseBlocks = rawData.split(/===/);
    let caseCounter = 1;

    caseBlocks.forEach(block => {
        let lines = block.split('\\n').map(l => l.trim()).filter(l => l !== '');
        if (lines.length >= 2) {
            // Header line: "caseId:ext"  (ext is per-case, fallback to globalExt)
            let headerParts = lines[0].split(':');
            let caseId  = headerParts[0];
            let caseExt = headerParts[1] || globalExt;
            let caseObj = { tabLabel: "Case " + caseCounter, caseId: caseId, ext: caseExt, series: [] };
            for (let i = 1; i < lines.length; i++) {
                let p = lines[i].split('|').map(x => x.trim());
                if (p.length >= 2) {
                    let seriesExt = p[2] || caseExt;  // per-series ext, fallback to case ext
                    caseObj.series.push({ name: p[0], maxSlices: parseInt(p[1]), ext: seriesExt });
                }
            }
            if (caseObj.series.length > 0) {
                casesList.push(caseObj);
                caseCounter++;
            }
        }
    });

    if (casesList.length === 0) return;

    if (viewer) viewer.style.display = 'flex';

    let activeCaseIdx   = 0;
    let activeSeriesIdx = 0;
    let activeSlice     = Math.floor(casesList[0].series[0].maxSlices / 2) || 1;

    function buildImagePath(caseObj, seriesObj, sliceNum) {
        let paddedSlice = String(sliceNum).padStart(3, '0');
        return `${caseObj.caseId}_${seriesObj.name}_${paddedSlice}.${seriesObj.ext}`;
    }

    const casesRibbon = viewer.querySelector('#cases-ribbon');
    const ribbon      = viewer.querySelector('#series-ribbon');
    const mainImg     = viewer.querySelector('#current-slice');
    const sliceCounter = viewer.querySelector('#slice-counter');
    const viewport    = viewer.querySelector('#main-viewport');

    if (casesList.length > 1) {
        casesRibbon.style.display = 'flex';
        casesList.forEach((c, idx) => {
            let tab = document.createElement('div');
            tab.className = 'case-tab';
            if (idx === activeCaseIdx) tab.classList.add('active');
            tab.innerText = c.tabLabel;
            tab.onclick = () => selectCase(idx);
            casesRibbon.appendChild(tab);
        });
    }

    function renderSeriesRibbon() {
        ribbon.innerHTML = '';
        let currentCase = casesList[activeCaseIdx];
        currentCase.series.forEach((s, index) => {
            let thumbDiv = document.createElement('div');
            thumbDiv.className = 'thumbnail-card';
            if (index === activeSeriesIdx) thumbDiv.classList.add('active');

            let previewSlice = Math.floor(s.maxSlices / 2) || 1;
            let img = document.createElement('img');
            img.src = buildImagePath(currentCase, s, previewSlice);

            let stackIcon = document.createElement('div');
            stackIcon.className = 'stack-icon';
            stackIcon.innerText = `☰ ${s.maxSlices}`;

            let label = document.createElement('div');
            label.className = 'thumb-label';
            label.innerText = s.name.replace(/_/g, ' ');

            thumbDiv.appendChild(img);
            thumbDiv.appendChild(stackIcon);
            thumbDiv.appendChild(label);
            thumbDiv.onclick = () => selectSeries(index);
            ribbon.appendChild(thumbDiv);
        });
    }

    function selectCase(idx) {
        viewer.querySelectorAll('.case-tab').forEach((el, i) => {
            el.classList.toggle('active', i === idx);
        });
        activeCaseIdx   = idx;
        activeSeriesIdx = 0;
        activeSlice     = Math.floor(casesList[idx].series[0].maxSlices / 2) || 1;
        renderSeriesRibbon();
        updateViewport();
    }

    function selectSeries(idx) {
        viewer.querySelectorAll('.thumbnail-card').forEach((el, i) => {
            el.classList.toggle('active', i === idx);
        });
        activeSeriesIdx = idx;
        activeSlice     = Math.floor(casesList[activeCaseIdx].series[idx].maxSlices / 2) || 1;
        updateViewport();
    }

    function updateViewport() {
        let curCase   = casesList[activeCaseIdx];
        let curSeries = curCase.series[activeSeriesIdx];
        mainImg.src   = buildImagePath(curCase, curSeries, activeSlice);
        sliceCounter.innerText = `${activeSlice} / ${curSeries.maxSlices}`;
        preloadImages(curCase, curSeries, activeSlice);
    }

    viewport.addEventListener('wheel', (e) => {
        e.preventDefault();
        let max = casesList[activeCaseIdx].series[activeSeriesIdx].maxSlices;
        if (e.deltaY > 0) activeSlice = Math.min(activeSlice + 1, max);
        else              activeSlice = Math.max(activeSlice - 1, 1);
        updateViewport();
    });

    let lastTouchY = null;
    const touchSensitivity = 12;

    viewport.addEventListener('touchstart', (e) => {
        lastTouchY = e.touches[0].clientY;
    }, { passive: false });

    viewport.addEventListener('touchmove', (e) => {
        if (lastTouchY === null) return;
        e.preventDefault();
        let currentTouchY = e.touches[0].clientY;
        let deltaY        = lastTouchY - currentTouchY;
        let max = casesList[activeCaseIdx].series[activeSeriesIdx].maxSlices;
        if (Math.abs(deltaY) > touchSensitivity) {
            if (deltaY > 0) activeSlice = Math.min(activeSlice + 1, max);
            else            activeSlice = Math.max(activeSlice - 1, 1);
            updateViewport();
            lastTouchY = currentTouchY;
        }
    }, { passive: false });

    viewport.addEventListener('touchend', () => { lastTouchY = null; });

    document.addEventListener('keydown', (e) => {
        let currentCase = casesList[activeCaseIdx];
        if (!currentCase) return;
        if (e.key === 'ArrowRight') {
            if (activeSeriesIdx < currentCase.series.length - 1) selectSeries(activeSeriesIdx + 1);
        } else if (e.key === 'ArrowLeft') {
            if (activeSeriesIdx > 0) selectSeries(activeSeriesIdx - 1);
        }
    });

    const preloadedCache = {};
    function preloadImages(cObj, sObj, current) {
        let start = Math.max(1, current - 5);
        let end   = Math.min(sObj.maxSlices, current + 5);
        for (let i = start; i <= end; i++) {
            let path = buildImagePath(cObj, sObj, i);
            if (!preloadedCache[path]) {
                let img = new Image();
                img.src = path;
                preloadedCache[path] = true;
            }
        }
    }

    renderSeriesRibbon();
    updateViewport();
})();
</script>"""

_VIEWER_HTML = """\
<div id="medical-viewer" class="dark-theme" style="display: none; margin-bottom: 25px;">
    <div id="cases-ribbon" style="display: none;"></div>
    <div id="series-ribbon" class="carousel"></div>
    <div id="main-viewport">
        <img id="current-slice" src="" alt="Medical Slice">
        <div id="slice-indicator">Slice: <span id="slice-counter">1</span></div>
    </div>
</div>"""

# ---------------------------------------------------------------------------
# Article card templates
# ---------------------------------------------------------------------------

ARTICLE_FRONT = """\
<div id="raw-html" style="display: none;">
  {{Content}}
</div>

<div class="card-prompt">
  <h2 id="extracted-title"></h2>
</div>

<script>
  var rawHtmlDivs = document.querySelectorAll('#raw-html');
  var rawHtmlDiv  = rawHtmlDivs[rawHtmlDivs.length - 1];
  if (rawHtmlDiv) {
      var titleElement = rawHtmlDiv.querySelector('h1.header-title');
      var titleDisplay = document.querySelectorAll('#extracted-title');
      var activeTitleDisplay = titleDisplay[titleDisplay.length - 1];
      if (titleElement && activeTitleDisplay) {
          var titleText = "";
          for (var i = 0; i < titleElement.childNodes.length; i++) {
              if (titleElement.childNodes[i].nodeType === Node.TEXT_NODE) {
                  titleText += titleElement.childNodes[i].nodeValue;
              }
          }
          activeTitleDisplay.innerText = titleText.trim();
      }
  }
</script>"""

ARTICLE_BACK = (
    "{{FrontSide}}\n\n<hr id=\"answer\">\n\n"
    + _VIEWER_HTML + "\n\n"
    + """<div class="radiopaedia-wrapper case-description">
  {{Content}}
</div>

<div class="powered-by">
    Powered by <strong>United Radiology</strong> \U0001f1e8\U0001f1ed
</div>

"""
    + _VIEWER_JS
)

ARTICLE_CSS = """\
.card {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    background-color: #000000;
    color: #e0e0e0;
    margin: 0;
    padding: 10px;
    text-align: left;
}

hr#answer {
    border: 0;
    border-bottom: 1px solid #444;
    margin: 20px auto;
    max-width: 800px;
}

.card-prompt {
    text-align: center;
    margin-top: 5vh;
    margin-bottom: 15px;
}
.card-prompt h2 {
    font-size: 26px;
    font-weight: bold;
    color: #ffffff;
    max-width: 800px;
    margin: 5px auto 15px auto;
    padding-bottom: 8px;
    border-bottom: 2px solid #ff9800;
    display: inline-block;
}

.radiopaedia-wrapper {
    max-width: 800px;
    margin: 0 auto;
}

h1 span[data-tippy-content],
.favourite-btn,
.rb-quick-links {
    display: none !important;
}

h1.header-title {
    font-size: 26px;
    font-weight: bold;
    color: #ffffff;
    text-align: center;
    margin: 5px auto 15px auto;
    padding-bottom: 8px;
    border-bottom: 2px solid #ff9800;
}

.article-body {
    font-size: 16px;
    line-height: 1.6;
    padding: 15px 20px;
    background: #1e1e1e;
    border-radius: 8px;
    border-left: 4px solid #ff9800;
    color: #e0e0e0;
    margin-bottom: 20px;
}

.section-title {
    font-size: 12px;
    text-transform: uppercase;
    color: #ff9800;
    font-weight: bold;
    margin-top: 25px;
    margin-bottom: 8px;
    letter-spacing: 1px;
    border-bottom: none;
}

p, ul { margin-top: 0; margin-bottom: 12px; }
li { margin-bottom: 6px; }

a { color: #ff9800; text-decoration: none; border-bottom: 1px dotted #ff9800; }
a:hover { color: #ffffff; border-bottom-style: solid; }

strong { color: #ffffff; font-weight: bold; }

.article-citation {
    font-size: 11px;
    color: #666;
    border-top: 1px solid #333;
    padding-top: 15px;
    margin-top: 20px;
}
.article-citation .row { display: flex; margin-bottom: 4px; }
.article-citation .col-sm-3 { flex: 0 0 20%; color: #888; font-weight: bold; }
.article-citation .col-sm-8 { flex: 0 0 80%; }
.article-citation a { color: #888; text-decoration: underline; }

/* --- Grid (citation rows on back) --- */
.row { display: flex; flex-wrap: wrap; margin-bottom: 4px; }
.col-sm-3 { flex: 0 0 25%; max-width: 25%; font-weight: bold; color: #a6adc8; padding-right: 10px; }
.col-sm-8 { flex: 0 0 75%; max-width: 75%; }

/* ======== VIEWER ======== */
#medical-viewer {
    display: flex;
    flex-direction: column;
    background-color: #121212;
    border-radius: 8px;
    overflow: hidden;
    max-width: 800px;
    margin: 0 auto;
    border: 1px solid #333;
}

#series-ribbon {
    display: flex;
    overflow-x: auto;
    gap: 15px;
    padding: 15px;
    background-color: #1e1e1e;
    border-bottom: 1px solid #333;
}
#series-ribbon::-webkit-scrollbar { height: 8px; }
#series-ribbon::-webkit-scrollbar-thumb { background: #555; border-radius: 4px; }

.thumbnail-card {
    position: relative;
    flex: 0 0 120px;
    cursor: pointer;
    border: 2px solid transparent;
    border-radius: 4px;
    transition: border-color 0.2s;
    background: #000;
}
.thumbnail-card.active { border-color: #ff9800; }
.thumbnail-card img { width: 100%; height: 120px; object-fit: cover; border-radius: 2px 2px 0 0; display: block; }
.stack-icon { position: absolute; bottom: 30px; right: 5px; background: rgba(0,0,0,0.7); color: white; font-size: 10px; padding: 2px 4px; border-radius: 3px; }
.thumb-label { font-size: 11px; text-align: center; padding: 5px; background: #2a2a2a; color: #ccc; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }

#main-viewport {
    position: relative;
    display: flex;
    justify-content: center;
    align-items: center;
    background-color: #000;
    height: 60vh;
    cursor: ns-resize;
}
#current-slice { width: 100%; height: 100%; object-fit: contain; }
#slice-indicator { position: absolute; bottom: 15px; right: 15px; background: rgba(0,0,0,0.6); color: #ff9800; padding: 5px 10px; border-radius: 4px; font-size: 14px; font-weight: bold; }

#cases-ribbon { display: flex; overflow-x: auto; gap: 10px; padding: 10px 15px; background-color: #121212; border-bottom: 1px solid #333; }
#cases-ribbon::-webkit-scrollbar { height: 6px; }
#cases-ribbon::-webkit-scrollbar-thumb { background: #444; border-radius: 3px; }

.case-tab { padding: 6px 16px; background-color: #1e1e1e; color: #a6adc8; border: 1px solid #333; border-radius: 20px; font-size: 13px; font-weight: bold; cursor: pointer; white-space: nowrap; transition: all 0.2s ease; }
.case-tab:hover { background-color: #2a2a2a; color: #e0e0e0; }
.case-tab.active { background-color: rgba(255,152,0,0.15); color: #ff9800; border-color: #ff9800; }

.powered-by { font-size: 12px; text-align: center; color: #aaa; margin: 15px auto 10px auto; padding-top: 10px; border-top: 1px solid #333; width: fit-content; padding-left: 20px; padding-right: 20px; text-transform: uppercase; letter-spacing: 1px; }"""

# ---------------------------------------------------------------------------
# Case card templates
# ---------------------------------------------------------------------------

CASE_FRONT = _VIEWER_HTML + "\n\n" + _VIEWER_JS

CASE_BACK = """\
<div id="raw-descriptions" style="display:none;">{{Descriptions}}</div>
<div id="raw-citations"    style="display:none;">{{Citations}}</div>

{{FrontSide}}

<hr id="answer">

<div class="case-description">
    <div class="description-label">Description &amp; Findings</div>
    <div id="display-description"></div>
</div>

<div class="case-reference" id="display-citation"></div>

<div class="powered-by">
    Powered by <strong>United Radiology</strong> \U0001f1e8\U0001f1ed
</div>

<script>
(function () {
    function getLatest(sel) {
        var els = document.querySelectorAll(sel);
        return els.length ? els[els.length - 1] : null;
    }
    var rawDesc   = getLatest('#raw-descriptions');
    var rawCit    = getLatest('#raw-citations');
    var dispDesc  = getLatest('#display-description');
    var dispCit   = getLatest('#display-citation');
    if (rawDesc  && dispDesc) dispDesc.innerHTML = rawDesc.innerHTML;
    if (rawCit   && dispCit)  dispCit.innerHTML  = rawCit.innerHTML;
})();
</script>"""

CASE_CSS = """\
.card {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    background-color: #000000;
    color: #e0e0e0;
    margin: 0;
    padding: 10px;
    text-align: left;
}

hr#answer { border: 0; border-bottom: 1px solid #444; margin: 20px auto; max-width: 800px; }

.case-title { font-size: 26px; font-weight: bold; color: #ffffff; text-align: center; max-width: 800px; margin: 5px auto 15px auto; padding-bottom: 8px; border-bottom: 2px solid #ff9800; }

.case-description { font-size: 16px; line-height: 1.6; text-align: left; max-width: 800px; margin: 0 auto 10px auto; padding: 15px 20px; background: #1e1e1e; border-radius: 8px; border-left: 4px solid #ff9800; color: #e0e0e0; }

.description-label { font-size: 11px; text-transform: uppercase; color: #ff9800; font-weight: bold; margin-bottom: 8px; letter-spacing: 1px; }

.case-reference, .article-citation { font-size: 11px; max-width: 800px; margin: 20px auto 5px auto; color: #666; border-top: 1px solid #333; padding-top: 15px; line-height: 1.5; }
.case-reference .row, .article-citation .row { display: flex; flex-wrap: wrap; margin-bottom: 4px; }
.case-reference .col-sm-3, .article-citation .col-sm-3 { flex: 0 0 25%; max-width: 25%; color: #888; font-weight: bold; padding-right: 10px; }
.case-reference .col-sm-8, .article-citation .col-sm-8 { flex: 0 0 75%; max-width: 75%; }
.case-reference a, .article-citation a { color: #888; text-decoration: underline; }

.powered-by { font-size: 12px; text-align: center; color: #aaa; margin: 15px auto 10px auto; padding-top: 10px; border-top: 1px solid #333; width: fit-content; padding-left: 20px; padding-right: 20px; text-transform: uppercase; letter-spacing: 1px; }

.case-description h1 span[data-tippy-content], .case-description .favourite-btn, .case-description .rb-quick-links { display: none !important; }
.case-description h1.header-title { display: none; }
.case-description .section-title { font-size: 12px; text-transform: uppercase; color: #ff9800; font-weight: bold; margin-top: 25px; margin-bottom: 8px; letter-spacing: 1px; border-bottom: none; }
.case-description p, .case-description ul { margin-top: 0; margin-bottom: 12px; }
.case-description li { margin-bottom: 6px; }
.case-description a { color: #ff9800; text-decoration: none; border-bottom: 1px dotted #ff9800; }
.case-description a:hover { color: #ffffff; border-bottom-style: solid; }
.case-description strong { color: #ffffff; font-weight: bold; }

/* ======== VIEWER ======== */
#medical-viewer { display: flex; flex-direction: column; background-color: #121212; border-radius: 8px; overflow: hidden; max-width: 800px; margin: 0 auto; border: 1px solid #333; }

#cases-ribbon { display: flex; overflow-x: auto; gap: 10px; padding: 10px 15px; background-color: #121212; border-bottom: 1px solid #333; }
#cases-ribbon::-webkit-scrollbar { height: 6px; }
#cases-ribbon::-webkit-scrollbar-thumb { background: #444; border-radius: 3px; }

.case-tab { padding: 6px 16px; background-color: #1e1e1e; color: #a6adc8; border: 1px solid #333; border-radius: 20px; font-size: 13px; font-weight: bold; cursor: pointer; white-space: nowrap; transition: all 0.2s ease; }
.case-tab:hover { background-color: #2a2a2a; color: #e0e0e0; }
.case-tab.active { background-color: rgba(255,152,0,0.15); color: #ff9800; border-color: #ff9800; }

#series-ribbon { display: flex; overflow-x: auto; gap: 15px; padding: 15px; background-color: #1e1e1e; border-bottom: 1px solid #333; }
#series-ribbon::-webkit-scrollbar { height: 8px; }
#series-ribbon::-webkit-scrollbar-thumb { background: #555; border-radius: 4px; }

.thumbnail-card { position: relative; flex: 0 0 120px; cursor: pointer; border: 2px solid transparent; border-radius: 4px; transition: border-color 0.2s; background: #000; }
.thumbnail-card.active { border-color: #ff9800; }
.thumbnail-card img { width: 100%; height: 120px; object-fit: cover; border-radius: 2px 2px 0 0; display: block; }
.stack-icon { position: absolute; bottom: 30px; right: 5px; background: rgba(0,0,0,0.7); color: white; font-size: 10px; padding: 2px 4px; border-radius: 3px; }
.thumb-label { font-size: 11px; text-align: center; padding: 5px; background: #2a2a2a; color: #ccc; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }

#main-viewport { position: relative; display: flex; justify-content: center; align-items: center; background-color: #000; height: 60vh; cursor: ns-resize; }
#current-slice { width: 100%; height: 100%; object-fit: contain; }
#slice-indicator { position: absolute; bottom: 15px; right: 15px; background: rgba(0,0,0,0.6); color: #ff9800; padding: 5px 10px; border-radius: 4px; font-size: 14px; font-weight: bold; }"""

# ---------------------------------------------------------------------------
# Note models
# ---------------------------------------------------------------------------

ARTICLE_MODEL = genanki.Model(
    model_id=_model_id("Radiopaedia Article"),
    name="Radiopaedia Article",
    fields=[
        {"name": "Content"},
        {"name": "Cases Data"},
        {"name": "File Extension"},
    ],
    templates=[{
        "name":  "Article Card",
        "qfmt":  ARTICLE_FRONT,
        "afmt":  ARTICLE_BACK,
    }],
    css=ARTICLE_CSS,
)

CASE_MODEL = genanki.Model(
    model_id=_model_id("Radiopaedia Case"),
    name="Radiopaedia Case",
    fields=[
        {"name": "Cases Data"},
        {"name": "File Extension"},
        {"name": "Descriptions"},
        {"name": "Citations"},
    ],
    templates=[{
        "name":  "Case Card",
        "qfmt":  CASE_FRONT,
        "afmt":  CASE_BACK,
    }],
    css=CASE_CSS,
)

# ---------------------------------------------------------------------------
# Cases Data field builder
# ---------------------------------------------------------------------------

def _hidden_media_html(cases: list[dict]) -> str:
    """
    Return a hidden <div> containing <img> tags for every image in the given cases.
    Appending this to any rendered note field ensures Anki imports the media files
    when the .apkg is imported (Anki only imports files referenced in note fields).
    """
    img_refs = []
    for c in cases:
        case_ext = c.get("file_extension", "jpg")
        cid = c["case_id"]
        for s in c["series"]:
            series_ext = s.get("ext", case_ext)
            for idx in range(1, s["max_slices"] + 1):
                img_refs.append(f'<img src="{cid}_{s["safe_name"]}_{idx:03d}.{series_ext}">')
    if not img_refs:
        return ""
    return '<div style="display:none">' + "".join(img_refs) + "</div>"


def _build_cases_data(cases: list[dict]) -> str:
    """
    Build the {{Cases Data}} field value from a list of case dicts.
    Header line format: "{case_id}:{file_extension}"  (e.g. "48734:png")
    Series lines:       "{safe_name}|{max_slices}"  or  "{safe_name}|{max_slices}|{ext}"
                        (per-series ext only written when it differs from case ext)
    Single case → no === separator.  Multiple → one === between each pair.
    The viewer JS strips all HTML before parsing, so the appended hidden media
    refs do not affect it.
    """
    blocks = []
    for c in cases:
        case_ext = c.get("file_extension", "jpg")
        cid = c["case_id"]
        lines = [f"{cid}:{case_ext}"]
        for s in c["series"]:
            series_ext = s.get("ext", case_ext)
            # Write per-series ext only when it differs from the case-level ext
            if series_ext != case_ext:
                lines.append(f"{s['safe_name']}|{s['max_slices']}|{series_ext}")
            else:
                lines.append(f"{s['safe_name']}|{s['max_slices']}")
        blocks.append("\n".join(lines))
    return "\n===\n".join(blocks)


def _collect_media(case: dict) -> list[str]:
    """Return absolute path strings for all downloaded images in a case."""
    case_dir = case["output_dir"]
    result   = []
    for s in case["series"]:
        ext    = s["ext"]
        sname  = s["safe_name"]
        cid    = case["case_id"]
        for idx in range(1, s["max_slices"] + 1):
            p = case_dir / f"{cid}_{sname}_{idx:03d}.{ext}"
            if p.exists():
                result.append(str(p.resolve()))
    return result


def _dominant_ext(cases: list[dict]) -> str:
    for c in cases:
        for s in c["series"]:
            if s.get("ext") == "png":
                return "png"
    return "jpg"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_package(
    folder_title: str,
    article_data_list: list[dict],
    case_data_list: list[dict],
    output_dir: Path,
) -> Path:
    """
    Build and write an .apkg file. Returns its Path.

    article_data_list: list of download_article() return dicts
    case_data_list:    list of run() return dicts (standalone cases only)
    """
    article_deck = genanki.Deck(_deck_id(f"Articles::{folder_title}"),
                                f"Articles::{folder_title}")
    case_deck    = genanki.Deck(_deck_id(f"Quiz::{folder_title}"),
                                f"Quiz::{folder_title}")
    all_media: list[str] = []

    # --- Article notes ---
    for art in article_data_list:
        linked   = art.get("linked_cases", [])
        cd_str   = _build_cases_data(linked) if linked else ""
        file_ext = _dominant_ext(linked) if linked else "jpg"
        # Append hidden img refs to Content (a rendered field) so Anki imports
        # all media files — Anki only imports files referenced in note fields.
        hidden   = _hidden_media_html(linked)
        content  = art["content_html"] + hidden

        note = genanki.Note(
            model=ARTICLE_MODEL,
            fields=[content, cd_str, file_ext],
            guid=genanki.guid_for(art["rid"]),
        )
        article_deck.add_note(note)
        for case in linked:
            all_media.extend(_collect_media(case))

    # --- Standalone case notes ---
    # case_data_list is a list of groups; each group (list of sub-case dicts)
    # comes from one case URL and becomes ONE Anki card.
    for group in case_data_list:
        if not group:
            continue
        cd_str   = _build_cases_data(group)
        file_ext = _dominant_ext(group)
        hidden   = _hidden_media_html(group)
        # Findings and citation from the first sub-case (shared page metadata)
        findings = group[0].get("findings_html", "") + hidden
        citation = group[0].get("citation_html", "")
        # GUID based on the rID of the first viewer (stable across re-imports)
        guid = genanki.guid_for(group[0].get("rid", group[0]["case_id"]))

        note = genanki.Note(
            model=CASE_MODEL,
            fields=[cd_str, file_ext, findings, citation],
            guid=guid,
        )
        case_deck.add_note(note)
        for case in group:
            all_media.extend(_collect_media(case))

    # Deduplicate media by absolute path
    all_media = list(dict.fromkeys(all_media))

    print(f"[anki_builder] media files to embed: {len(all_media)}")
    missing = [p for p in all_media if not Path(p).exists()]
    if missing:
        print(f"[anki_builder] WARNING — {len(missing)} files NOT found on disk:")
        for m in missing[:5]:
            print(f"  {m}")
    else:
        if all_media:
            print(f"[anki_builder] All files confirmed on disk. First: {all_media[0]}")

    apkg_path = output_dir / f"{folder_title}.apkg"
    pkg = genanki.Package([article_deck, case_deck])
    pkg.media_files = all_media
    pkg.write_to_file(str(apkg_path))
    print(f"[anki_builder] Package written: {apkg_path}  ({apkg_path.stat().st_size:,} bytes)")
    return apkg_path
