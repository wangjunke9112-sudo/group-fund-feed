#!/usr/bin/env python3
"""
gz_fund_poc.py -- feasibility proof-of-concept for ONE source:
  Guangzhou municipal portal notice column  (www.gz.gov.cn/xw/tzgg/)
  -> we treat this as the "market/city science bureau" (市科技局) feed.

Goal of this file: prove the OFFLINE half of the chain end to end
    parse list -> parse detail meta -> parse Chinese deadline ->
    is-it-an-opportunity filter -> relevance gate -> four-state tag ->
    dedupe/merge -> derive a seasonal calendar
against the REAL page structure captured on 2026-07-18, with zero network
and zero third-party packages (stdlib only, mirrors the regex style of the
existing aggregate.py).

The NETWORKED half (fetch_live) uses urllib only, so it also needs no pip.
It cannot be exercised in the code sandbox (no network); run it on your own
machine or in GitHub Actions. Everything else is proven by --selftest here.

Usage:
  python3 gz_fund_poc.py --selftest   # offline logic tests on real fixtures
  python3 gz_fund_poc.py --demo       # build notices.json + calendar.json from fixtures
  python3 gz_fund_poc.py --live       # real fetch (needs network; run at home / in CI)
"""

import argparse
import datetime as dt
import html
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "data")

LIST_URL = "https://www.gz.gov.cn/xw/tzgg/"          # the notice column (page 1)

# Pagination, verified 2026-07-18 from the live page's own markup/JS:
#   page 1  -> https://www.gz.gov.cn/xw/tzgg/        (NOT index_1.html - 404)
#   page 2  -> .../index_2.html
#   page N  -> .../index_N.html
#   last    -> .../index_100.html   (site caps pages_limit=100)
# The page's JS declares:  var totalPage = Math.ceil(2380/20); pages_limit=100
# => ~2380 notices exist, at most 2000 reachable by paging. Enough to reach 2024.
def page_url(n):
    """n is 1-indexed. Page 1 is the bare column URL, not index_1.html."""
    return LIST_URL if n <= 1 else LIST_URL + f"index_{n}.html"


_TOTALPAGE_RE = re.compile(r"Math\.ceil\((\d+)\s*/\s*(\d+)\)")
_PAGESLIMIT_RE = re.compile(r"pages_limit\s*=\s*(\d+)")


def detect_totals(html_text):
    """Read the site's own declared totals from its pagination JS.
    Returns (total_items, per_page, pages_limit) with None where unknown."""
    total_items = per_page = limit = None
    m = _TOTALPAGE_RE.search(html_text or "")
    if m:
        total_items, per_page = int(m.group(1)), int(m.group(2))
    m2 = _PAGESLIMIT_RE.search(html_text or "")
    if m2:
        limit = int(m2.group(1))
    return (total_items, per_page, limit)


DEPARTMENT = "市科技局"                                # what we tag this source as
LEVEL = "市"                                          # 国家/省/市/港澳/校内
SOURCE_ID = "gz_kjj"

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 FundFeed/0.1")


# ===========================================================================
# 1. LIST PAGE PARSING
# Real structure (list page): each notice is an <a href=".../content/post_N.html"
# title="..">TITLE</a> followed by a YYYY-MM-DD date. We stay tolerant: pull
# every post_*.html link on the page and the date nearest after it.
# ===========================================================================
# Real markup (verified 2026-07-18 on a live GitHub Actions fetch):
#   <li>
#     <i></i>
#     <a href="https://www.gz.gov.cn/xw/tzgg/content/post_N.html"
#        target="_blank" title="TITLE">
#         <!-- 规章文件 -->            <-- HTML COMMENTS live inside the anchor
#         TITLE</a>
#     <span class="time">2026-07-17</span>
#   </li>
# Lessons encoded below: (a) never assume attribute order, (b) never assume the
# anchor's inner text is comment-free, (c) the date is in a following
# <span class="time">, not bare text.
_ANCHOR_RE = re.compile(
    r'<a\b(?P<attrs>[^>]*href="(?P<url>https?://[^"]*?/content/post_\d+\.html)"[^>]*)>',
    re.I)
_TITLE_ATTR_RE = re.compile(r'title="([^"]*)"', re.I)
_TIME_SPAN_RE = re.compile(
    r'<span[^>]*class="[^"]*time[^"]*"[^>]*>\s*(20\d{2})-(\d{1,2})-(\d{1,2})', re.I)
_DATE_RE = re.compile(r"(20\d{2})-(\d{1,2})-(\d{1,2})")
_COMMENT_RE_HTML = re.compile(r"<!--.*?-->", re.S)


def parse_list(html_text):
    """Return [{'title','url','published'}] from a notice list page's HTML.

    Order-independent on attributes; tolerates HTML comments inside the anchor;
    reads the date from the following <span class="time"> (falling back to any
    nearby YYYY-MM-DD)."""
    items = []
    for m in _ANCHOR_RE.finditer(html_text):
        url = m.group("url")
        # 1) prefer the title="" attribute (authoritative, comment-free)
        tm = _TITLE_ATTR_RE.search(m.group("attrs"))
        title = clean_text(tm.group(1)) if tm else ""
        # 2) fall back to anchor inner text with comments stripped
        if not title:
            close = html_text.find("</a>", m.end())
            if close > 0:
                inner = _COMMENT_RE_HTML.sub(" ", html_text[m.end():close])
                title = clean_text(inner)
        if not title:
            continue
        # 3) date: the <span class="time"> right after the link
        tail = html_text[m.end():m.end() + 600]
        sm = _TIME_SPAN_RE.search(tail) or _DATE_RE.search(tail)
        published = ""
        if sm:
            y, mo, d = sm.group(1), sm.group(2), sm.group(3)
            published = f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"
        items.append({"title": title, "url": url, "published": published})
    # de-dup by url, keep first (newest) occurrence
    seen, out = set(), []
    for it in items:
        if it["url"] in seen:
            continue
        seen.add(it["url"])
        out.append(it)
    return out


# ===========================================================================
# 2. DETAIL PAGE PARSING  (authoritative metadata lives in <meta> tags)
#   meta-ArticleTitle, meta-ContentSource, meta-PubDate, meta-ColumnName
# ===========================================================================
def _meta(html_text, name):
    # tolerate attribute order (name..content OR content..name) AND an optional
    # 'meta-' prefix on the name attribute, which is how this portal labels them.
    nm = r'(?:meta-)?' + re.escape(name)
    pat1 = (r'<meta[^>]+name=["\']' + nm
            + r'["\'][^>]+content=["\'](.*?)["\']')
    pat2 = (r'<meta[^>]+content=["\'](.*?)["\'][^>]+name=["\']'
            + nm + r'["\']')
    for pat in (pat1, pat2):
        m = re.search(pat, html_text, re.I | re.S)
        if m:
            return clean_text(m.group(1))
    return ""


def parse_detail(html_text, url=""):
    """Return a dict with title, source(部门), published, body-ish text."""
    title = _meta(html_text, "ArticleTitle")
    source = _meta(html_text, "ContentSource")
    pub = _meta(html_text, "PubDate")              # e.g. '2024-04-16 13:23:11'
    published = ""
    dm = re.search(r"(20\d{2})-(\d{2})-(\d{2})", pub)
    if dm:
        published = f"{dm.group(1)}-{dm.group(2)}-{dm.group(3)}"
    body = _strip_html_body(html_text)
    return {"title": title, "source": source, "published": published,
            "body": body, "url": url or _meta(html_text, "Url")}


def _strip_html_body(html_text):
    """Very rough main-text extraction: drop scripts/styles/tags, collapse ws.
    Good enough to run the deadline regex over; we are not displaying it."""
    t = re.sub(r"<script\b.*?</script>", " ", html_text, flags=re.I | re.S)
    t = re.sub(r"<style\b.*?</style>", " ", t, flags=re.I | re.S)
    t = re.sub(r"<[^>]+>", " ", t)
    t = html.unescape(t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


# ===========================================================================
# 3. CHINESE DEADLINE EXTRACTION  (the genuinely messy part)
# Returns (deadline_iso_or_None, confidence) where confidence is:
#   'explicit' - a real 截止/前/延长至 date was found
#   'rolling'  - 常年/长期/滚动 申报 (no single deadline)
#   'unknown'  - no parseable deadline; caller should PARK it for review
# ===========================================================================
_ROLLING_RE = re.compile(r"常年申报|长期申报|滚动申报|常年受理|长期有效|随时申报")

# cue patterns; each captures a (year,month,day). Order = specificity.
_DEADLINE_CUES = [
    r"延长至[^。；]{0,12}?(20\d{2})年(\d{1,2})月(\d{1,2})日",
    r"截止[^。；]{0,20}?(20\d{2})年(\d{1,2})月(\d{1,2})日",
    r"(20\d{2})年(\d{1,2})月(\d{1,2})日[^。；]{0,12}?(?:截止|止)",
    r"(20\d{2})年(\d{1,2})月(\d{1,2})日[^。；]{0,4}?前",
    r"申报(?:时间|日期)[^。；]{0,20}?(20\d{2})年(\d{1,2})月(\d{1,2})日",
]
_DEADLINE_CUES = [re.compile(p) for p in _DEADLINE_CUES]


def _iso(y, m, d):
    try:
        return dt.date(int(y), int(m), int(d)).isoformat()
    except ValueError:
        return ""


def extract_deadline(body):
    if not body:
        return (None, "unknown")
    if _ROLLING_RE.search(body):
        return (None, "rolling")
    found = []
    for pat in _DEADLINE_CUES:
        for m in pat.finditer(body):
            iso = _iso(*m.groups())
            if iso:
                found.append(iso)
    if found:
        return (max(found), "explicit")          # latest cued date = the deadline
    return (None, "unknown")


# ===========================================================================
# 4. IS-IT-AN-OPPORTUNITY  +  RELEVANCE GATE
# Most notices in this column are administrative (验收/年报/公示/补助领取).
# We only surface real application opportunities, and only if in-field.
# ===========================================================================
_ADMIN_RE = re.compile(
    r"验收|结题|年报|年度.{0,3}报告|公示|拟立项|立项公告|评审结果|"
    r"领取|免申即享|拨付|信用|抽查|绩效|复核|中期检查|变更|撤销|处罚|"
    r"名单|入库|摸查|统计调查|问卷")
_OPP_RE = re.compile(
    r"申报指南|申报的通知|征集|组织申报|开始申报|项目申报|受理|"
    r"揭榜|指南的通知|拟支持|遴选|资助计划")


def is_opportunity(title, body=""):
    """True = looks like a call for applications; False = administrative."""
    text = (title or "") + " " + (body or "")[:400]
    if _ADMIN_RE.search(title or ""):
        return False
    if _OPP_RE.search(text):
        return True
    # fall-through: unclear -> treat as opportunity=False but caller can PARK
    return False


# field relevance (broad, high-recall; LENS = solar/PV/energy materials)
KEYWORDS = [
    "太阳能", "光伏", "钙钛矿", "叠层", "多结", "光电", "半导体", "薄膜",
    "新能源", "储能", "电池", "材料", "器件", "能源", "碳中和", "氢能",
    "发光", "显示", "OLED", "量子点",
]
_KW_RE = re.compile("|".join(re.escape(k) for k in KEYWORDS), re.I)


def is_relevant(text):
    hits = sorted(set(m.group(0) for m in _KW_RE.finditer(text or "")))
    return (bool(hits), hits)


# ===========================================================================
# 5. FOUR-STATE TAGGING  (profile: holds 海外优青; new PI)
#   vip    - user-curated priority calls the PI can lead
#   team   - big-team calls (likely not PI, but worth joining) -> pinned too
#   red    - talent-individual calls at/below 优青 level (mutually exclusive)
#   conflict - NSFC slot pressure (only meaningful for NSFC source)
#   verify - default when eligibility is unclear
# Only ONE primary tag is returned plus optional 'conflict'. Nothing is hidden.
# ===========================================================================
VIP_FUNDS = [           # things Junke can lead -> gold, pinned
    "面上项目", "基础与应用基础研究", "国际合作", "国际科技合作",
    "粤港澳", "粤港", "粤澳", "联合研究", "市校院联合", "基础研究计划",
]
TEAM_FUNDS = [          # big-team -> blue "团队参与", pinned, prompt to join
    "重点研发计划", "重点专项", "重大专项", "重点领域研发", "产学研",
    "联合基金", "重大科技", "科技创新2030", "重大项目",
]
RED_TALENT = [          # at/below 优青 individual talent -> red, sink (holds 海优)
    "优秀青年科学基金", "海外优青", "优青", "青年科学基金", "青年拔尖人才",
]
_VIP_RE = re.compile("|".join(re.escape(k) for k in VIP_FUNDS))
_TEAM_RE = re.compile("|".join(re.escape(k) for k in TEAM_FUNDS))
_RED_RE = re.compile("|".join(re.escape(k) for k in RED_TALENT))
# a red talent call is only truly red if it's an INDIVIDUAL scheme, not a team one
_TEAMWORD_RE = re.compile(r"团队|群体|集体")


def classify_fund(title, body="", source_level=LEVEL):
    text = (title or "") + " " + (body or "")
    tags = []
    # red: individual talent scheme at/below 优青
    if _RED_RE.search(title or "") and not _TEAMWORD_RE.search(title or ""):
        tags.append("red")
        return tags
    if _VIP_RE.search(text):
        tags.append("vip")
    elif _TEAM_RE.search(text):
        tags.append("team")
    else:
        tags.append("verify")
    # conflict note only applies to NSFC-sourced calls (slot pressure)
    # (this POC source is 市科技局, so it never fires here; shown for completeness)
    return tags


# ===========================================================================
# 6. RECORD BUILD + DEDUP/MERGE  (mirrors aggregate.py structure)
# ===========================================================================
def _post_id(url):
    m = re.search(r"post_(\d+)\.html", url or "")
    return m.group(1) if m else (url or "")


def build_record(detail):
    title = detail["title"]
    body = detail.get("body", "")
    deadline, dl_conf = extract_deadline(body)
    opp = is_opportunity(title, body)
    relevant, hits = is_relevant(title + " " + body[:600])
    tags = classify_fund(title, body, LEVEL)
    # status
    today = dt.date.today().isoformat()
    if dl_conf == "explicit" and deadline:
        status = "open" if deadline >= today else "closed"
    elif dl_conf == "rolling":
        status = "open"
    else:
        status = "unknown"
    return {
        "id": SOURCE_ID + ":" + _post_id(detail.get("url", "")),
        "title": title,
        "url": detail.get("url", ""),
        "department": detail.get("source") or DEPARTMENT,
        "level": LEVEL,
        "published": detail.get("published", ""),
        "deadline": deadline,
        "deadline_confidence": dl_conf,
        "is_opportunity": opp,
        "relevant": relevant,
        "keywords": hits,
        "tags": tags,
        "status": status,
    }


def merge(existing, fresh):
    by_id = {}
    for rec in existing + fresh:
        by_id[rec["id"]] = rec          # last write wins; fresh listed after
    out = list(by_id.values())
    out.sort(key=lambda r: (r.get("published", ""), r.get("id", "")), reverse=True)
    return out


# ===========================================================================
# 7. DERIVE SEASONAL CALENDAR from history (what months each call-type recurs)
# ===========================================================================
def load_seed():
    """Manual calendar anchors + human-inspection checklist for sources we do
    not scrape (e.g. robots-restricted provincial sites)."""
    path = os.path.join(DATA_DIR, "calendar_seed.json")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {"anchors": [], "manual_sources": []}


def build_calendar(records):
    """Combine (a) seasonality DERIVED from scraped history with (b) MANUAL
    anchors for un-scrapable sources. Both are marked with their origin so the
    page can show which is evidence and which is prior knowledge."""
    seed = load_seed()
    derived = []
    for c in derive_calendar(records):
        derived.append({
            "name": c["call_type"],
            "org": DEPARTMENT,
            "level": LEVEL,
            "months": [int(m) for m in c["months"]],
            "counts": c["counts"],
            "source": "derived",
            "confidence": "observed",
            "note": f"由已抓取的 {sum(c['counts'].values())} 条历史通知自动统计",
        })
    anchors = []
    for a in seed.get("anchors", []):
        a = dict(a)
        a["source"] = "manual"
        anchors.append(a)
    return {"derived": derived, "anchors": anchors,
            "manual_sources": seed.get("manual_sources", [])}


def derive_calendar(records):
    """Group opportunity records by a coarse call-type and list the months in
    which they were PUBLISHED across years -> reveals the annual rhythm."""
    def call_type(r):
        t = r["title"]
        for kw in ["重点领域研发", "产学研", "基础研究计划", "国际合作",
                   "粤港澳", "面上项目", "重点研发计划"]:
            if kw in t:
                return kw
        return "其他"
    buckets = {}
    for r in records:
        if not r.get("is_opportunity"):
            continue
        ct = call_type(r)
        mo = (r.get("published", "") or "")[5:7]
        if not mo:
            continue
        buckets.setdefault(ct, {}).setdefault(mo, 0)
        buckets[ct][mo] += 1
    cal = []
    for ct, months in sorted(buckets.items()):
        cal.append({"call_type": ct,
                    "months": sorted(months.keys()),
                    "counts": months})
    return cal


# ---------------------------------------------------------------------------
def clean_text(value):
    if not value:
        return ""
    t = re.sub(r"<[^>]+>", " ", str(value))
    t = html.unescape(t)
    return re.sub(r"\s+", " ", t).strip()


# ===========================================================================
# 8. LIVE FETCH  (urllib only; NOT runnable in the sandbox -- no network)
# ===========================================================================
def load_notices():
    """Load previously stored notices so repeated runs accumulate (resumable
    backfill) instead of overwriting."""
    path = os.path.join(DATA_DIR, "notices.json")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh).get("notices", [])
    except Exception:
        return []


# Titles worth opening a detail page for. Kept broad on purpose: cheap to widen,
# expensive to miss. Everything else is skipped WITHOUT a detail fetch.
_TITLE_PREFILTER_RE = re.compile(
    r"科技|科学技术|自然科学|基础研究|研发|创新|人才|实验室|重点领域|产学研|专项")


def fetch_live(pages=1, until=None, sleep=1.0, verbose=True):
    """Walk the notice column from page 1 outward.

    pages : how many list pages to walk this run
    until : ISO date string; stop once list items are older than this
            (e.g. '2024-01-01' for the 2-year backfill)
    Resumable: already-stored notice ids are skipped without a detail fetch,
    so you can run --pages 10 repeatedly and keep going deeper each time.
    """
    import time
    import urllib.request

    known = {r["id"] for r in load_notices()}
    records = []
    stop = False

    for n in range(1, pages + 1):
        url = page_url(n)
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9",
            })
            listing = urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "replace")
        except Exception as exc:
            print(f"[list] {url}  ERROR {type(exc).__name__}: {exc}")
            break

        items = parse_list(listing)
        if n == 1:
            tot, per, lim = detect_totals(listing)
            if tot:
                print(f"[site] 共 {tot} 条 · 每页 {per} 条 · 最多可翻 {lim} 页 "
                      f"(≈{(lim or 0) * (per or 0)} 条可达)")
        print(f"[list] p{n} {url}  {len(items)} links")
        if not items:
            break

        for it in items:
            # date cutoff: list is newest-first, so once we pass it we can stop
            if until and it.get("published") and it["published"] < until:
                print(f"[stop] 已回溯到 {it['published']} < {until}，停止")
                stop = True
                break

            nid = SOURCE_ID + ":" + _post_id(it["url"])
            if nid in known:
                continue                        # already have it; no detail fetch
            if not _TITLE_PREFILTER_RE.search(it["title"]):
                continue                        # not science/tech related

            try:
                req = urllib.request.Request(it["url"], headers={"User-Agent": USER_AGENT})
                page_html = urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "replace")
            except Exception as exc:
                print(f"   [detail] {it['url']}  ERROR {exc}")
                continue

            detail = parse_detail(page_html, url=it["url"])
            if not detail.get("published"):
                detail["published"] = it.get("published", "")
            if not detail.get("title"):
                detail["title"] = it["title"]
            rec = build_record(detail)
            records.append(rec)
            known.add(nid)
            if verbose:
                tag = rec["tags"][0]
                print(f"   + [{tag}] {rec['published']} {rec['title'][:38]}")
            time.sleep(sleep)

        if stop:
            break
        time.sleep(sleep)

    return records


# ===========================================================================
# REAL FIXTURES captured 2026-07-18 (used by --selftest and --demo, no network)
# ===========================================================================
# real <meta> block from a real 科技局 detail page (trimmed to the meta lines)
FIX_DETAIL = '''<html><head>
<meta name="meta-ArticleTitle" content="广州市科学技术局关于发布重点研发计划2024年度重点领域研发专题产学研合作项目认定立项方向申报指南的通知">
<meta name="meta-ColumnName" content="通知公告">
<meta name="meta-ContentSource" content="市科技局">
<meta name="meta-PubDate" content="2024-04-16 13:23:11">
<meta name="meta-Url" content="https://www.gz.gov.cn/xw/tzgg/content/post_9600238.html">
</head><body>
<p>各有关单位：为促进产学研深度合作加快科技成果转化，现发布2024年度重点领域研发专题产学研合作项目认定立项方向申报指南。</p>
<p>六、申报时间 本项目常年申报。申报单位网上申报开始时间为2024年5月10日9:00。</p>
</body></html>'''

# EXACT real list markup, captured 2026-07-18 from a live GitHub Actions fetch
# (HTTP 200, 74601 bytes). Note the comments inside <a> and the <span class="time">.
FIX_LIST = '''
    <div class="main_border">
      <ul class="news_list">
                <li>
            <i></i>
            <a href="https://www.gz.gov.cn/xw/tzgg/content/post_10905933.html" target="_blank" title="致市民的一封信">
              <!-- 规章文件 -->
                        <!-- 规范性文件 -->

                        <!-- 其他文件 -->

                致市民的一封信</a>
            <span class="time">2026-07-17</span>
          </li>
                <li>
            <i></i>
            <a href="https://www.gz.gov.cn/xw/tzgg/content/post_10849284.html" target="_blank" title="广州市科学技术局 广州市财政局 国家税务总局广州市税务局关于组织开展广州市2026年高新技术企业认定工作的通知">
              <!-- 规章文件 -->
                广州市科学技术局 广州市财政局 国家税务总局广州市税务局关于组织开展广州市2026年高新技术企业认定工作的通知</a>
            <span class="time">2026-06-10</span>
          </li>
                <li>
            <i></i>
            <a href="https://www.gz.gov.cn/xw/tzgg/content/post_9600238.html" target="_blank" title="广州市科学技术局关于发布重点研发计划2024年度重点领域研发专题产学研合作项目认定立项方向申报指南的通知">
              <!-- 其他文件 -->
                广州市科学技术局关于发布重点研发计划2024年度重点领域研发专题产学研合作项目认定立项方向申报指南的通知</a>
            <span class="time">2024-04-16</span>
          </li>
      </ul>
    </div>'''

# real body sentences (deadline forms) observed on this portal
FIX_BODIES = {
    "rolling": "六、申报时间 本项目常年申报。申报单位网上申报开始时间为2024年5月10日9:00。",
    "explicit_range": "请获得资助的单位于2026年3月2日至2026年3月15日期间（2026年3月15日下午18时截止）登录广州科技GI。",
    "explicit_extend": "现将行业赛报名截止时间延长至2025年10月20日17时。请各意向参赛企业知悉。",
    "explicit_before": "请各单位在2025年4月20日前完成创新平台2024年度情况报告。",
    "none": "我局现组织开展2025年广州市科技计划项目验收工作，有关事项通知如下。",
}


def _demo_records():
    """Build records from the fixtures the way --live would, but offline."""
    recs = []
    # the rich detail fixture (full record via meta + body)
    recs.append(build_record(parse_detail(FIX_DETAIL)))
    # extra synthetic-but-real-shaped rows to populate the calendar/statuses,
    # using real titles + representative bodies seen on the portal
    extra = [
        ("广州市科学技术局关于组织开展广州市2026年高新技术企业认定工作的通知",
         "2026-06-10", FIX_BODIES["explicit_range"],
         "https://www.gz.gov.cn/xw/tzgg/content/post_10849284.html"),
        ("广州市科学技术局关于开展2025年市科技计划项目验收工作的通知",
         "2025-04-23", FIX_BODIES["none"],
         "https://www.gz.gov.cn/xw/tzgg/content/post_10230914.html"),
        ("广州市基础研究计划2025年度市校院联合资助项目申报指南",
         "2025-03-05", FIX_BODIES["explicit_before"],
         "https://www.gz.gov.cn/xw/tzgg/content/post_demo1.html"),
        ("广州市重点领域研发计划2025年度新能源与新材料专题申报指南",
         "2025-02-20", FIX_BODIES["explicit_range"],
         "https://www.gz.gov.cn/xw/tzgg/content/post_demo2.html"),
    ]
    for title, pub, body, url in extra:
        recs.append(build_record({"title": title, "source": "市科技局",
                                   "published": pub, "body": body, "url": url}))
    return recs


# ===========================================================================
def selftest():
    print("Running offline self-test on REAL captured structure...")

    # --- pagination (this is what broke: index_1.html does NOT exist) ---
    assert page_url(1) == "https://www.gz.gov.cn/xw/tzgg/", page_url(1)
    assert page_url(2) == "https://www.gz.gov.cn/xw/tzgg/index_2.html", page_url(2)
    assert page_url(100) == "https://www.gz.gov.cn/xw/tzgg/index_100.html"
    assert "index_1.html" not in page_url(1)

    # --- totals read from the page's own JS (real snippet) ---
    real_js = ("// 总页数 var totalPage = Math.ceil(2380/20); var pages_limit=100; "
               "if(totalPage>pages_limit){ totalPage=pages_limit }")
    assert detect_totals(real_js) == (2380, 20, 100), detect_totals(real_js)
    assert detect_totals("no js here") == (None, None, None)

    # --- list parsing (against EXACT real markup: comments inside <a>,
    #     target attr between href and title, date in <span class="time">) ---
    items = parse_list(FIX_LIST)
    assert len(items) == 3, items
    assert items[0]["url"].endswith("post_10905933.html"), items[0]
    assert items[0]["title"] == "致市民的一封信", items[0]
    assert items[0]["published"] == "2026-07-17", items[0]
    assert items[1]["published"] == "2026-06-10", items[1]
    assert "高新技术企业认定" in items[1]["title"], items[1]
    assert items[2]["published"] == "2024-04-16", items[2]
    # comments inside the anchor must not leak into the title
    assert all("规章文件" not in it["title"] for it in items), items
    assert all("<!--" not in it["title"] for it in items), items

    # --- detail meta parsing (authoritative) ---
    d = parse_detail(FIX_DETAIL)
    assert d["source"] == "市科技局", d
    assert d["published"] == "2024-04-16", d
    assert "产学研合作项目认定立项方向申报指南" in d["title"], d

    # --- Chinese deadline extraction ---
    assert extract_deadline(FIX_BODIES["rolling"]) == (None, "rolling")
    assert extract_deadline(FIX_BODIES["explicit_range"]) == ("2026-03-15", "explicit")
    assert extract_deadline(FIX_BODIES["explicit_extend"]) == ("2025-10-20", "explicit")
    assert extract_deadline(FIX_BODIES["explicit_before"]) == ("2025-04-20", "explicit")
    assert extract_deadline(FIX_BODIES["none"]) == (None, "unknown")

    # --- opportunity vs administrative ---
    assert is_opportunity("广州市重点领域研发计划2025年度新能源专题申报指南") is True
    assert is_opportunity("广州市科技计划项目验收工作的通知") is False
    assert is_opportunity("关于领取2025年度补助资金的通知") is False

    # --- relevance gate ---
    assert is_relevant("新能源与新材料专题")[0] is True
    assert is_relevant("市属学校校服款式")[0] is False

    # --- four-state tagging ---
    assert classify_fund("广州市重点领域研发计划新能源专题申报指南") == ["team"]
    assert classify_fund("广州市基础研究计划市校院联合资助项目申报指南") == ["vip"]
    assert classify_fund("国家自然科学基金青年科学基金项目") == ["red"]
    assert classify_fund("粤港澳研究团队项目申报指南") == ["vip"]   # 团队 but a 粤港澳 VIP scheme
    assert classify_fund("某某一般性通知") == ["verify"]

    # --- full record from real detail fixture ---
    rec = build_record(parse_detail(FIX_DETAIL))
    assert rec["department"] == "市科技局"
    assert rec["deadline_confidence"] == "rolling" and rec["status"] == "open"
    assert rec["is_opportunity"] is True
    assert rec["tags"] == ["team"], rec["tags"]        # 重点领域研发/产学研 -> 团队参与
    assert rec["relevant"] is False   # this particular产学研 call has no field kw in title/body

    # --- dedupe/merge ---
    a = build_record({"title": "T", "source": "市科技局", "published": "2025-01-01",
                      "body": "常年申报", "url": ".../post_1.html"})
    b = dict(a); b = build_record({"title": "T2", "source": "市科技局",
                                   "published": "2025-01-01", "body": "常年申报",
                                   "url": ".../post_1.html"})
    merged = merge([a], [b])
    assert len(merged) == 1 and merged[0]["title"] == "T2", merged

    # --- calendar derivation ---
    cal = derive_calendar(_demo_records())
    types = {c["call_type"] for c in cal}
    assert "重点领域研发" in types and "基础研究计划" in types, types

    print("All offline self-tests passed  ✓")
    print("(list parse, meta parse, 4 deadline forms, opportunity filter,")
    print(" relevance gate, 4-state tagging, dedupe, calendar — on real structure)")


def _write(name, obj):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(os.path.join(DATA_DIR, name), "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=1)


def demo():
    recs = merge([], _demo_records())
    gen = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    _write("notices.json", {"generated": gen, "source": DEPARTMENT,
                            "count": len(recs), "notices": recs})
    _write("calendar.json", dict(generated=gen, **build_calendar(recs)))
    print(f"Wrote {DATA_DIR}/notices.json ({len(recs)} notices) + calendar.json\n")
    # console preview
    order = {"vip": 0, "team": 1, "verify": 2, "red": 3}
    recs_sorted = sorted(recs, key=lambda r: (order.get(r["tags"][0], 9),
                                              r.get("deadline") or "9999"))
    badge = {"vip": "⭐VIP", "team": "🟦团队", "verify": "⚪待核实", "red": "🔴不可报"}
    print(f"{'tag':<8}{'opp':<5}{'rel':<5}{'deadline':<12}{'conf':<10}title")
    print("-" * 96)
    for r in recs_sorted:
        print(f"{badge.get(r['tags'][0],''):<7} "
              f"{'Y' if r['is_opportunity'] else '-':<4} "
              f"{'Y' if r['relevant'] else '-':<4} "
              f"{(r['deadline'] or '—'):<12}{r['deadline_confidence']:<10}"
              f"{r['title'][:40]}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--pages", type=int, default=1,
                    help="本次抓取翻多少页（第1页=最新）")
    ap.add_argument("--until", default=None,
                    help="回溯截止日期，如 2024-01-01；遇到更早的条目即停止")
    args = ap.parse_args()
    if args.selftest:
        selftest()
    elif args.demo:
        demo()
    elif args.live:
        fresh = fetch_live(pages=args.pages, until=args.until)
        recs = merge(load_notices(), fresh)
        gen = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        _write("notices.json", {"generated": gen, "source": DEPARTMENT,
                                "count": len(recs), "notices": recs})
        _write("calendar.json", dict(generated=gen, **build_calendar(recs)))
        opp = sum(1 for r in recs if r.get("is_opportunity"))
        oldest = min((r.get("published", "") for r in recs if r.get("published")), default="—")
        print("-" * 60)
        print(f"本次新增 {len(fresh)} 条 · 库内合计 {len(recs)} 条 "
              f"（其中申报机会 {opp} 条）· 最早回溯至 {oldest}")
        print(f"Wrote {DATA_DIR}/notices.json + calendar.json")
    else:
        ap.print_help()
        sys.exit(1)
