"""
anki_builder.py
Builds Anki .apkg packages from scraped Radiopaedia data.

Article cards  → deck  Articles::{folder_title}
Case cards     → deck  Quiz::{folder_title}
"""

import hashlib
import html as _html
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
        // Swap the per-viewer description when switching case tabs.
        // Safe no-op on article cards (no #per-viewer-descriptions there).
        const perViewerDescs = document.getElementById('per-viewer-descriptions');
        const dispDesc = getLatestElement('#display-description');
        if (perViewerDescs && dispDesc) {
            const descDiv = perViewerDescs.querySelector('[data-desc-idx="' + idx + '"]');
            if (descDiv) dispDesc.innerHTML = descDiv.innerHTML;
        }
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
    '<div id="raw-descriptions" style="display:none;">{{Descriptions}}</div>\n\n'
    "{{FrontSide}}\n\n<hr id=\"answer\">\n\n"
    + _VIEWER_HTML + "\n\n"
    + """<div class="case-description" id="viewer-description" style="display:none;">
  <div class="description-label">Description &amp; Findings</div>
  <div id="display-description"></div>
</div>

<div class="radiopaedia-wrapper case-description">
  {{Content}}
</div>

<div class="powered-by">
    Powered by <strong>United Radiology</strong> \U0001f1e8\U0001f1ed
</div>

<script>
(function () {
    function getLatest(sel) {
        var els = document.querySelectorAll(sel);
        return els.length ? els[els.length - 1] : null;
    }
    var dispDesc       = getLatest('#display-description');
    var wrapper        = getLatest('#viewer-description');
    var perViewerDescs = getLatest('#per-viewer-descriptions');
    if (perViewerDescs && dispDesc) {
        var firstDesc = perViewerDescs.querySelector('[data-desc-idx="0"]');
        if (firstDesc && firstDesc.textContent.trim()) {
            dispDesc.innerHTML = firstDesc.innerHTML;
            if (wrapper) wrapper.style.display = 'block';
        }
    }
})();
</script>

"""
    + _VIEWER_JS
)

ARTICLE_CSS = """\
.card {
    font-family: -apple-system, BlinkMacSystemFont, "Inria Sans", ui-sans-serif, sans-serif;
    background-color: #1a1a1a;
    color: #f2f2f2;
    margin: 0;
    padding: 10px;
    text-align: left;
    -webkit-font-smoothing: antialiased;
}

hr#answer {
    border: 0;
    border-bottom: 1px solid rgba(242,242,242,0.08);
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
    font-weight: 700;
    color: #ffffff;
    max-width: 800px;
    margin: 5px auto 15px auto;
    padding-bottom: 10px;
    border-bottom: 1px solid rgba(242,242,242,0.12);
    display: inline-block;
    letter-spacing: -0.02em;
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
    font-weight: 700;
    color: #ffffff;
    text-align: center;
    margin: 5px auto 15px auto;
    padding-bottom: 10px;
    border-bottom: 1px solid rgba(242,242,242,0.12);
    letter-spacing: -0.02em;
}

.article-body {
    font-size: 15px;
    line-height: 1.65;
    padding: 16px 20px;
    background: #242424;
    border-radius: 12px;
    border-left: 3px solid rgba(242,242,242,0.1);
    color: #e8e8e8;
    margin-bottom: 20px;
}

.section-title {
    font-size: 11px;
    text-transform: uppercase;
    color: rgba(242,242,242,0.4);
    font-weight: 700;
    margin-top: 24px;
    margin-bottom: 8px;
    letter-spacing: 0.8px;
    border-bottom: none;
}

p, ul { margin-top: 0; margin-bottom: 12px; }
li { margin-bottom: 6px; }

a { color: #c8392b; text-decoration: none; border-bottom: 1px dotted rgba(200,57,43,0.5); }
a:hover { color: #e05a4e; border-bottom-color: #e05a4e; }

strong { color: #ffffff; font-weight: 600; }

.case-description { font-size: 15px; line-height: 1.65; max-width: 800px; margin: 0 auto 20px auto; padding: 16px 20px; background: #242424; border-radius: 12px; border-left: 3px solid rgba(242,242,242,0.1); color: #e8e8e8; }
.description-label { font-size: 10px; text-transform: uppercase; color: rgba(242,242,242,0.38); font-weight: 700; margin-bottom: 10px; letter-spacing: 0.8px; }
.viewer-heading { font-size: 11px; text-transform: uppercase; color: rgba(242,242,242,0.45); font-weight: 600; margin-bottom: 6px; letter-spacing: 0.6px; }

.article-citation {
    font-size: 11px;
    color: #8a8a8e;
    border-top: 1px solid rgba(242,242,242,0.08);
    padding-top: 15px;
    margin-top: 20px;
}
.article-citation .row { display: flex; margin-bottom: 4px; }
.article-citation .col-sm-3 { flex: 0 0 20%; color: rgba(242,242,242,0.45); font-weight: 600; }
.article-citation .col-sm-8 { flex: 0 0 80%; }
.article-citation a { color: #8a8a8e; text-decoration: underline; }

/* --- Grid (citation rows on back) --- */
.row { display: flex; flex-wrap: wrap; margin-bottom: 4px; }
.col-sm-3 { flex: 0 0 25%; max-width: 25%; font-weight: 600; color: rgba(242,242,242,0.45); padding-right: 10px; }
.col-sm-8 { flex: 0 0 75%; max-width: 75%; }

/* ======== VIEWER ======== */
#medical-viewer {
    display: flex;
    flex-direction: column;
    background-color: #111;
    border-radius: 12px;
    overflow: hidden;
    max-width: 800px;
    margin: 0 auto;
    box-shadow: 0 0 0 1px rgba(242,242,242,0.08);
}

#series-ribbon {
    display: flex;
    overflow-x: auto;
    gap: 12px;
    padding: 12px 14px;
    background-color: #1e1e1e;
    border-bottom: 1px solid rgba(242,242,242,0.07);
}
#series-ribbon::-webkit-scrollbar { height: 4px; }
#series-ribbon::-webkit-scrollbar-thumb { background: rgba(242,242,242,0.2); border-radius: 2px; }

.thumbnail-card {
    position: relative;
    flex: 0 0 110px;
    cursor: pointer;
    border: 1.5px solid transparent;
    border-radius: 8px;
    transition: border-color 0.15s;
    background: #000;
    overflow: hidden;
}
.thumbnail-card.active { border-color: rgba(242,242,242,0.65); }
.thumbnail-card img { width: 100%; height: 110px; object-fit: cover; display: block; }
.stack-icon { position: absolute; bottom: 28px; right: 4px; background: rgba(0,0,0,0.65); color: rgba(255,255,255,0.8); font-size: 9px; padding: 2px 4px; border-radius: 3px; }
.thumb-label { font-size: 10px; text-align: center; padding: 5px 4px; background: #1e1e1e; color: rgba(242,242,242,0.45); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }

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
#slice-indicator { position: absolute; bottom: 14px; right: 14px; background: rgba(0,0,0,0.55); color: rgba(242,242,242,0.9); padding: 4px 9px; border-radius: 6px; font-size: 13px; font-weight: 600; }

#cases-ribbon { display: flex; overflow-x: auto; gap: 6px; padding: 9px 12px; background-color: #111; border-bottom: 1px solid rgba(242,242,242,0.07); }
#cases-ribbon::-webkit-scrollbar { height: 4px; }
#cases-ribbon::-webkit-scrollbar-thumb { background: rgba(242,242,242,0.2); border-radius: 2px; }

.case-tab { padding: 5px 14px; background-color: rgba(242,242,242,0.05); color: rgba(242,242,242,0.5); border: 1px solid rgba(242,242,242,0.08); border-radius: 20px; font-size: 12px; font-weight: 500; cursor: pointer; white-space: nowrap; transition: all 0.15s ease; }
.case-tab:hover { background-color: rgba(242,242,242,0.09); color: rgba(242,242,242,0.8); }
.case-tab.active { background-color: rgba(242,242,242,0.92); color: #111; border-color: transparent; font-weight: 700; }

.powered-by { font-size: 11px; text-align: center; color: rgba(242,242,242,0.25); margin: 16px auto 8px auto; padding-top: 12px; border-top: 1px solid rgba(242,242,242,0.07); width: fit-content; padding-left: 20px; padding-right: 20px; text-transform: uppercase; letter-spacing: 1px; }"""

# ---------------------------------------------------------------------------
# Case card templates
# ---------------------------------------------------------------------------

CASE_FRONT = _VIEWER_HTML + "\n\n" + _VIEWER_JS

CASE_BACK = """\
<div id="raw-descriptions" style="display:none;">{{Descriptions}}</div>
<div id="raw-citations"    style="display:none;">{{Citations}}</div>

{{FrontSide}}

<hr id="answer">

<div class="case-title" id="display-title"></div>

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
    var rawTitle  = getLatest('#raw-title');
    var dispTitle = getLatest('#display-title');
    var rawDesc   = getLatest('#raw-descriptions');
    var rawCit    = getLatest('#raw-citations');
    var dispDesc  = getLatest('#display-description');
    var dispCit   = getLatest('#display-citation');
    if (rawTitle  && dispTitle) dispTitle.innerText = rawTitle.innerText;
    // Show the first viewer's description on load.
    // Falls back to the flat rawDesc for legacy single-viewer cards.
    var perViewerDescs = getLatest('#per-viewer-descriptions');
    if (perViewerDescs && dispDesc) {
        var firstDesc = perViewerDescs.querySelector('[data-desc-idx="0"]');
        if (firstDesc) dispDesc.innerHTML = firstDesc.innerHTML;
    } else if (rawDesc && dispDesc) {
        dispDesc.innerHTML = rawDesc.innerHTML;
    }
    if (rawCit   && dispCit)  dispCit.innerHTML  = rawCit.innerHTML;
})();
</script>"""

CASE_CSS = """\
.card {
    font-family: -apple-system, BlinkMacSystemFont, "Inria Sans", ui-sans-serif, sans-serif;
    background-color: #1a1a1a;
    color: #f2f2f2;
    margin: 0;
    padding: 10px;
    text-align: left;
    -webkit-font-smoothing: antialiased;
}

hr#answer { border: 0; border-bottom: 1px solid rgba(242,242,242,0.08); margin: 20px auto; max-width: 800px; }

.case-title { font-size: 26px; font-weight: 700; color: #ffffff; text-align: center; max-width: 800px; margin: 5px auto 15px auto; padding-bottom: 10px; border-bottom: 1px solid rgba(242,242,242,0.12); letter-spacing: -0.02em; }

.case-description { font-size: 15px; line-height: 1.65; text-align: left; max-width: 800px; margin: 0 auto 10px auto; padding: 16px 20px; background: #242424; border-radius: 12px; border-left: 3px solid rgba(242,242,242,0.1); color: #e8e8e8; }

.description-label { font-size: 10px; text-transform: uppercase; color: rgba(242,242,242,0.38); font-weight: 700; margin-bottom: 10px; letter-spacing: 0.8px; }
.viewer-heading { font-size: 11px; text-transform: uppercase; color: rgba(242,242,242,0.45); font-weight: 600; margin-bottom: 6px; letter-spacing: 0.6px; }

.case-reference, .article-citation { font-size: 11px; max-width: 800px; margin: 20px auto 5px auto; color: #8a8a8e; border-top: 1px solid rgba(242,242,242,0.08); padding-top: 15px; line-height: 1.5; }
.case-reference .row, .article-citation .row { display: flex; flex-wrap: wrap; margin-bottom: 4px; }
.case-reference .col-sm-3, .article-citation .col-sm-3 { flex: 0 0 25%; max-width: 25%; color: rgba(242,242,242,0.45); font-weight: 600; padding-right: 10px; }
.case-reference .col-sm-8, .article-citation .col-sm-8 { flex: 0 0 75%; max-width: 75%; }
.case-reference a, .article-citation a { color: #8a8a8e; text-decoration: underline; }

.powered-by { font-size: 11px; text-align: center; color: rgba(242,242,242,0.25); margin: 16px auto 8px auto; padding-top: 12px; border-top: 1px solid rgba(242,242,242,0.07); width: fit-content; padding-left: 20px; padding-right: 20px; text-transform: uppercase; letter-spacing: 1px; }

.case-description h1 span[data-tippy-content], .case-description .favourite-btn, .case-description .rb-quick-links { display: none !important; }
.case-description h1.header-title { display: none; }
.case-description .section-title { font-size: 11px; text-transform: uppercase; color: rgba(242,242,242,0.4); font-weight: 700; margin-top: 24px; margin-bottom: 8px; letter-spacing: 0.8px; border-bottom: none; }
.case-description p, .case-description ul { margin-top: 0; margin-bottom: 12px; }
.case-description li { margin-bottom: 6px; }
.case-description a { color: #c8392b; text-decoration: none; border-bottom: 1px dotted rgba(200,57,43,0.5); }
.case-description a:hover { color: #e05a4e; }
.case-description strong { color: #ffffff; font-weight: 600; }

/* ======== VIEWER ======== */
#medical-viewer { display: flex; flex-direction: column; background-color: #111; border-radius: 12px; overflow: hidden; max-width: 800px; margin: 0 auto; box-shadow: 0 0 0 1px rgba(242,242,242,0.08); }

#cases-ribbon { display: flex; overflow-x: auto; gap: 6px; padding: 9px 12px; background-color: #111; border-bottom: 1px solid rgba(242,242,242,0.07); }
#cases-ribbon::-webkit-scrollbar { height: 4px; }
#cases-ribbon::-webkit-scrollbar-thumb { background: rgba(242,242,242,0.2); border-radius: 2px; }

.case-tab { padding: 5px 14px; background-color: rgba(242,242,242,0.05); color: rgba(242,242,242,0.5); border: 1px solid rgba(242,242,242,0.08); border-radius: 20px; font-size: 12px; font-weight: 500; cursor: pointer; white-space: nowrap; transition: all 0.15s ease; }
.case-tab:hover { background-color: rgba(242,242,242,0.09); color: rgba(242,242,242,0.8); }
.case-tab.active { background-color: rgba(242,242,242,0.92); color: #111; border-color: transparent; font-weight: 700; }

#series-ribbon { display: flex; overflow-x: auto; gap: 12px; padding: 12px 14px; background-color: #1e1e1e; border-bottom: 1px solid rgba(242,242,242,0.07); }
#series-ribbon::-webkit-scrollbar { height: 4px; }
#series-ribbon::-webkit-scrollbar-thumb { background: rgba(242,242,242,0.2); border-radius: 2px; }

.thumbnail-card { position: relative; flex: 0 0 110px; cursor: pointer; border: 1.5px solid transparent; border-radius: 8px; transition: border-color 0.15s; background: #000; overflow: hidden; }
.thumbnail-card.active { border-color: rgba(242,242,242,0.65); }
.thumbnail-card img { width: 100%; height: 110px; object-fit: cover; display: block; }
.stack-icon { position: absolute; bottom: 28px; right: 4px; background: rgba(0,0,0,0.65); color: rgba(255,255,255,0.8); font-size: 9px; padding: 2px 4px; border-radius: 3px; }
.thumb-label { font-size: 10px; text-align: center; padding: 5px 4px; background: #1e1e1e; color: rgba(242,242,242,0.45); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }

#main-viewport { position: relative; display: flex; justify-content: center; align-items: center; background-color: #000; height: 60vh; cursor: ns-resize; }
#current-slice { width: 100%; height: 100%; object-fit: contain; }
#slice-indicator { position: absolute; bottom: 14px; right: 14px; background: rgba(0,0,0,0.55); color: rgba(242,242,242,0.9); padding: 4px 9px; border-radius: 6px; font-size: 13px; font-weight: 600; }"""

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
        {"name": "Descriptions"},
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

        # Build per-linked-case descriptions for the article viewer.
        # linked is a flat list of viewer dicts (one per viewer across all linked cases).
        if linked:
            desc_divs = []
            for i, case in enumerate(linked):
                heading = case.get("study_heading", "")
                fhtml   = case.get("findings_html", "")
                heading_html = (
                    f'<div class="viewer-heading">{_html.escape(heading)}</div>'
                    if heading else ""
                )
                desc_divs.append(
                    f'<div data-desc-idx="{i}" style="display:none">'
                    f'{heading_html}{fhtml}</div>'
                )
            descriptions = (
                f'<div id="per-viewer-descriptions">{"".join(desc_divs)}</div>'
            )
        else:
            descriptions = ""

        note = genanki.Note(
            model=ARTICLE_MODEL,
            fields=[content, cd_str, file_ext, descriptions],
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
        # Build per-viewer description divs so the card can swap descriptions
        # when the user switches between case tabs in the viewer.
        title = _html.escape(group[0].get("title", ""))
        desc_divs = []
        for i, case in enumerate(group):
            heading  = case.get("study_heading", "")
            fhtml    = case.get("findings_html", "")
            heading_html = (
                f'<div class="viewer-heading">{_html.escape(heading)}</div>'
                if heading else ""
            )
            desc_divs.append(
                f'<div data-desc-idx="{i}" style="display:none">'
                f'{heading_html}{fhtml}</div>'
            )
        findings = (
            f'<div id="raw-title" style="display:none">{title}</div>'
            + f'<div id="per-viewer-descriptions">{"".join(desc_divs)}</div>'
            + hidden
        )
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
