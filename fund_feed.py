#!/usr/bin/env python3
"""
fund_feed.py -- LENS 基金雷达 · 多源抓取

在 gz_fund_poc.py 验证成功的基础上扩展为多源框架。

已验证可抓取的源（robots 允许，2026-07-18 实测）：
  gz_portal  广州市政府门户 通知公告      www.gz.gov.cn/xw/tzgg/
  nsfc_zn    NSFC 项目指南                www.nsfc.gov.cn/p1/3381/2824/zntg.html
  nsfc_tzgg  NSFC 通知公告                www.nsfc.gov.cn/p1/2828/2831/tzgg11.html
  most       科技部 国科管平台 通知        service.most.gov.cn/kjjh_tztg_all/

明确不抓取的源（robots 限制 / 登录墙）——改由 data/calendar_seed.json
的 manual_sources 列为人工巡检清单：
  广东省科技厅 gdstc.gd.gov.cn（robots 禁止自动访问）
  阳光政务平台 pro.gdstc.gd.gov.cn（登录墙）
  学校科研院 OA / 通知群（内部渠道）

用法：
  python3 fund_feed.py --selftest          离线自测
  python3 fund_feed.py --probe             探测各源结构（新增源必跑）
  python3 fund_feed.py --live              抓取全部启用的源
  python3 fund_feed.py --live --only nsfc_zn --pages 3
  python3 fund_feed.py --live --pages 45 --until 2024-01-01
"""

import argparse
import datetime as dt
import html
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "data")

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 LENSFundFeed/0.2")

# ===========================================================================
# SOURCE CONFIG
#   detail_re : regex identifying a *detail article* URL on that site
#   page_fmt  : how page N>=2 is addressed; None = only page 1 reachable
#               (NSFC paginates via JS, so history depth there is one page)
# ===========================================================================
SOURCES = [
    {
        "id": "gz_portal", "name": "广州市政府门户·通知公告", "org": "广州市/市科技局",
        "level": "市", "enabled": True,
        "list_url": "https://www.gz.gov.cn/xw/tzgg/",
        "page_fmt": "index_{n}.html",          # page1 = list_url (NOT index_1)
        "detail_re": r"/xw/tzgg/content/post_\d+\.html",
        "prefilter": True,                      # column is all-bureaus; must filter
        "note": "全市各局混发，需标题预筛",
    },
    {
        "id": "nsfc_zn", "name": "NSFC·项目指南", "org": "国家自然科学基金委",
        "level": "国家", "enabled": True,
        "list_url": "https://www.nsfc.gov.cn/p1/3381/2824/zntg.html",
        "page_fmt": None,                       # JS pagination -> page 1 only
        "detail_re": r"/p1/\d+/\d+/\d+\.html",
        "prefilter": False,                     # column is already all guides
        "note": "纯指南栏目；JS分页，仅第1页可达（约25条≈6周，足够周更监控）",
    },
    {
        "id": "nsfc_tzgg", "name": "NSFC·通知公告", "org": "国家自然科学基金委",
        "level": "国家", "enabled": True,
        "list_url": "https://www.nsfc.gov.cn/p1/2828/2831/tzgg11.html",
        "page_fmt": None,
        "detail_re": r"/p1/\d+/\d+/\d+\.html",
        "prefilter": False,
        "note": "国际合作类通知多发于此",
    },
    {
        "id": "most", "name": "科技部·国科管平台", "org": "科学技术部",
        "level": "国家", "enabled": False,   # 列表疑似JS/AJAX渲染，见 note
        "list_url": "https://service.most.gov.cn/kjjh_tztg_all/",
        "page_fmt": None,                       # verify with --probe before trusting
        "detail_re": r"kjjh_tztg_all/\d{8}/\d+\.html",
        "prefilter": False,
        "note": "2026-07-18 probe: 列表页HTML内无通知链接，疑JS/AJAX渲染 → 暂停用，改由 calendar_seed 的手工锚点+人工巡检覆盖。若 --dump most 显示真实通知链接，把 enabled 改回 True 并修正 detail_re 即可",
    },
]


def get_source(sid):
    for s in SOURCES:
        if s["id"] == sid:
            return s
    return None


# ===========================================================================
# TEXT UTILITIES
# ===========================================================================
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)


def clean_text(value):
    if not value:
        return ""
    t = _TAG_RE.sub(" ", str(value))
    t = html.unescape(t)
    return _WS_RE.sub(" ", t).strip()


def strip_body(html_text):
    t = re.sub(r"<script\b.*?</script>", " ", html_text or "", flags=re.I | re.S)
    t = re.sub(r"<style\b.*?</style>", " ", t, flags=re.I | re.S)
    t = _HTML_COMMENT_RE.sub(" ", t)
    t = _TAG_RE.sub(" ", t)
    return _WS_RE.sub(" ", html.unescape(t)).strip()


# ===========================================================================
# GENERIC LIST PARSER
# Lesson from the gz.gov.cn failure: never assume attribute order, never assume
# the anchor's inner text is comment-free, never assume where the date sits.
# So: match any <a> whose href looks like a detail URL, prefer title="", fall
# back to comment-stripped inner text, then hunt for the nearest date after it.
# ===========================================================================
_DATE_PATTERNS = [
    re.compile(r"(20\d{2})-(\d{1,2})-(\d{1,2})"),
    re.compile(r"(20\d{2})/(\d{1,2})/(\d{1,2})"),
    re.compile(r"(20\d{2})年(\d{1,2})月(\d{1,2})日"),
    re.compile(r"(20\d{2})\.(\d{1,2})\.(\d{1,2})"),
]
_TITLE_ATTR_RE = re.compile(r'title="([^"]*)"', re.I)
# Some columns render "<title text> YYYY-MM-DD" inside the anchor; strip it so
# the date does not contaminate the stored title.
_TRAILING_DATE_RE = re.compile(
    r"[\s\u3000]*(?:20\d{2}[-/.]\d{1,2}(?:[-/.]\d{1,2})?|"
    r"20\d{2}年\d{1,2}月(?:\d{1,2}日)?)\s*$")


def _strip_trailing_date(title):
    prev = None
    while prev != title:
        prev = title
        title = _TRAILING_DATE_RE.sub("", title).strip()
    return title


def _find_date(text):
    for pat in _DATE_PATTERNS:
        m = pat.search(text or "")
        if m:
            y, mo, d = m.groups()
            try:
                return dt.date(int(y), int(mo), int(d)).isoformat()
            except ValueError:
                continue
    return ""


def parse_list(html_text, detail_re, base=""):
    """Return [{'title','url','published'}]. Source-agnostic.

    base should be the LIST PAGE URL so that relative hrefs (e.g. MOST uses
    '20260430/5824.html') resolve correctly. Absolute, root-relative and
    directory-relative forms are all handled."""
    from urllib.parse import urljoin
    anchor_re = re.compile(
        r'<a\b(?P<attrs>[^>]*href="(?P<url>[^"]*?' + detail_re + r')"[^>]*)>',
        re.I)
    items, seen = [], set()
    for m in anchor_re.finditer(html_text or ""):
        url = m.group("url")
        if base and not url.lower().startswith(("http://", "https://")):
            url = urljoin(base, url)
        if url in seen:
            continue
        tm = _TITLE_ATTR_RE.search(m.group("attrs"))
        title = clean_text(tm.group(1)) if tm else ""
        if not title:
            close = html_text.find("</a>", m.end())
            if close > 0:
                title = clean_text(_HTML_COMMENT_RE.sub(" ", html_text[m.end():close]))
        title = _strip_trailing_date(title)
        if not title or len(title) < 4:
            continue
        published = _find_date(html_text[m.end():m.end() + 600])
        seen.add(url)
        items.append({"title": title, "url": url, "published": published})
    return items


# ===========================================================================
# DETAIL PARSING
# gz.gov.cn exposes meta-ArticleTitle/ContentSource/PubDate.
# NSFC / MOST do not, so we fall back to <title> and an in-body publish date.
# ===========================================================================
def _meta(html_text, name):
    nm = r"(?:meta-)?" + re.escape(name)
    for pat in (r'<meta[^>]+name=["\']' + nm + r'["\'][^>]+content=["\'](.*?)["\']',
                r'<meta[^>]+content=["\'](.*?)["\'][^>]+name=["\']' + nm + r'["\']'):
        m = re.search(pat, html_text, re.I | re.S)
        if m:
            return clean_text(m.group(1))
    return ""


_PUBDATE_BODY_RE = re.compile(
    r"发布时间[：: ]*\s*(20\d{2})[年\-/.](\d{1,2})[月\-/.](\d{1,2})")
_H1_RE = re.compile(r"<h1[^>]*>(.*?)</h1>", re.I | re.S)
_TITLE_TAG_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)


def parse_detail(html_text, url=""):
    title = _meta(html_text, "ArticleTitle")
    source = _meta(html_text, "ContentSource")
    published = ""
    pub = _meta(html_text, "PubDate")
    if pub:
        published = _find_date(pub)
    body = strip_body(html_text)
    if not published:
        m = _PUBDATE_BODY_RE.search(body)
        if m:
            y, mo, d = m.groups()
            try:
                published = dt.date(int(y), int(mo), int(d)).isoformat()
            except ValueError:
                pass
    if not title:
        m = _H1_RE.search(html_text or "")
        if m:
            title = clean_text(m.group(1))
    if not title:
        m = _TITLE_TAG_RE.search(html_text or "")
        if m:
            title = clean_text(m.group(1))
    return {"title": title, "source": source, "published": published,
            "body": body, "url": url}


# ===========================================================================
# CHINESE DEADLINE EXTRACTION
# ===========================================================================
_ROLLING_RE = re.compile(r"常年申报|长期申报|滚动申报|常年受理|长期有效|随时申报")
_DEADLINE_CUES = [re.compile(p) for p in [
    r"延长至[^。；]{0,12}?(20\d{2})年(\d{1,2})月(\d{1,2})日",
    r"截止[^。；]{0,20}?(20\d{2})年(\d{1,2})月(\d{1,2})日",
    r"(20\d{2})年(\d{1,2})月(\d{1,2})日[^。；]{0,14}?(?:截止|止)",
    r"(20\d{2})年(\d{1,2})月(\d{1,2})日[^。；]{0,6}?前",
    r"至\s*(20\d{2})年(\d{1,2})月(\d{1,2})日\s*\d{1,2}[:：]?\d{0,2}",
    r"申报(?:时间|日期|受理时间)[^。；]{0,30}?(20\d{2})年(\d{1,2})月(\d{1,2})日",
]]


def extract_deadline(body):
    if not body:
        return (None, "unknown")
    if _ROLLING_RE.search(body):
        return (None, "rolling")
    found = []
    for pat in _DEADLINE_CUES:
        for m in pat.finditer(body):
            try:
                found.append(dt.date(*(int(x) for x in m.groups())).isoformat())
            except (ValueError, TypeError):
                continue
    return (max(found), "explicit") if found else (None, "unknown")


# ===========================================================================
# OPPORTUNITY / RELEVANCE / TAGGING
# ===========================================================================
_ADMIN_RE = re.compile(
    r"验收|结题|年报|年度.{0,3}报告|公示|拟立项|立项公告|评审结果|获批|"
    r"领取|免申即享|拨付|信用|抽查|绩效|复核|中期检查|变更|撤销|处罚|"
    r"名单|入库|摸查|统计调查|问卷|征订|讣告|致市民|"
    # 竞赛/展会/解读类不是科研基金申报机会
    r"大赛|竞赛|技能大赛|创业大赛|展示活动|对接会|图文解读|政策解读|"
    r"申请与结题|结题等有关事项")
_OPP_RE = re.compile(
    r"申报指南|申报的通知|征集|组织申报|开始申报|项目申报|受理|指南的通告|"
    r"揭榜|指南的通知|遴选|资助计划|项目指南|申请指南|申报工作|征求意见")


def is_opportunity(title, body=""):
    if _ADMIN_RE.search(title or ""):
        return False
    return bool(_OPP_RE.search((title or "") + " " + (body or "")[:400]))


KEYWORDS = [
    "太阳能", "光伏", "钙钛矿", "叠层", "多结", "光电", "半导体", "薄膜",
    "新能源", "储能", "电池", "材料", "器件", "能源", "碳中和", "氢能",
    "发光", "显示", "量子点", "工程与材料", "化学科学", "国际合作", "交叉",
]
_KW_RE = re.compile("|".join(re.escape(k) for k in KEYWORDS))


# Field terms that are strong on their own (a title containing these is
# almost certainly in scope for a perovskite/PV/energy-materials PI).
_STRONG_KW_RE = re.compile(
    "|".join(["太阳能", "光伏", "钙钛矿", "叠层", "多结", "光电", "半导体",
              "薄膜", "新能源", "储能", "电池", "能源", "碳中和", "氢能",
              "发光", "显示", "量子点", "热电", "工程与材料", "化学科学"]))


def is_relevant(text, title=""):
    """Relevance with a title bias.

    Body-only matches are weak: nearly every government notice contains the
    word 材料 (as in 申报材料) or 创新, which would mark everything relevant.
    A strong field term in the TITLE is the reliable signal.
    """
    hits = sorted(set(m.group(0) for m in _KW_RE.finditer(text or "")))
    strong = bool(_STRONG_KW_RE.search(title or ""))
    # international-cooperation calls are in scope regardless of field wording
    intl = bool(re.search("国际合作|合作研究|合作交流|双边研讨会|学术会议|"
                          "来华交流|外国学者|联合科研资助", title or ""))
    return (strong or intl, hits)


# --- four-state tagging -----------------------------------------------------
# Profile: holds 海外优青 (so 优青-level individual talent schemes are out);
# new PI (so big-team calls are "join a team", not "lead").
# NOTE 2026-07-18: verified from the MOST call text that 政府间国际科技创新合作
# is a scheme this PI can plausibly LEAD (needs a foreign partner - a strength;
# ≤400万 projects are exempt from cross-scheme 限项 with other 重点研发 specials;
# age requirement 1966-01-01 or later). So it is VIP, not merely "team".
VIP_FUNDS = [
    # 可牵头的常规科研项目
    "面上项目", "基础与应用基础研究", "基础研究计划", "市校院联合",
    "粤港澳", "粤港", "粤澳", "粤穗", "联合研究",
    # 国际合作类（不受海优限制，且国际网络是本人强项）——2026-07-18 依真实
    # 抓取结果补齐：站方实际用词是“研究资助局”而非“研资局”，此前漏标
    "国际合作", "国际科技创新合作", "政府间国际", "战略性科技创新合作",
    "合作研究项目指南", "合作交流", "双边研讨会", "学术会议项目",
    "研资局", "研究资助局", "联合科研资助", "澳门科学技术发展基金",
    "人员交流", "外国学者", "来华交流", "短期讲习班", "海峡两岸",
    "国际合作科学计划", "可持续发展国际合作",
]
TEAM_FUNDS = [
    "重点研发计划", "重点专项", "重大专项", "重点领域研发", "产学研",
    "联合基金", "重大科技", "科技创新2030", "重大项目", "重大研究计划",
    "创新研究群体", "基础科学中心",
]
RED_TALENT = [
    "优秀青年科学基金", "海外优青", "优青", "青年科学基金", "青年拔尖人才",
    "杰出青年", "杰青",
]
_VIP_RE = re.compile("|".join(re.escape(k) for k in VIP_FUNDS))
_TEAM_RE = re.compile("|".join(re.escape(k) for k in TEAM_FUNDS))
_RED_RE = re.compile("|".join(re.escape(k) for k in RED_TALENT))
_TEAMWORD_RE = re.compile(r"团队|群体|集体")


def classify_fund(title, body=""):
    text = (title or "") + " " + (body or "")
    if _RED_RE.search(title or "") and not _TEAMWORD_RE.search(title or ""):
        return ["red"]
    if _VIP_RE.search(text):
        return ["vip"]
    if _TEAM_RE.search(text):
        return ["team"]
    return ["verify"]


# ===========================================================================
# RECORD BUILD / MERGE
# ===========================================================================
def _detail_id(url):
    m = re.search(r"(\d{4,})\.html", url or "")
    return m.group(1) if m else (url or "")


def build_record(detail, src):
    title = detail.get("title", "")
    body = detail.get("body", "")
    deadline, conf = extract_deadline(body)
    relevant, hits = is_relevant(title + " " + body[:800], title=title)
    today = dt.date.today().isoformat()
    if conf == "explicit" and deadline:
        status = "open" if deadline >= today else "closed"
    elif conf == "rolling":
        status = "open"
    else:
        status = "unknown"
    return {
        "id": src["id"] + ":" + _detail_id(detail.get("url", "")),
        "title": title,
        "url": detail.get("url", ""),
        "department": detail.get("source") or src["org"],
        "source_id": src["id"],
        "source_name": src["name"],
        "level": src["level"],
        "published": detail.get("published", ""),
        "deadline": deadline,
        "deadline_confidence": conf,
        "is_opportunity": is_opportunity(title, body),
        "relevant": relevant,
        "keywords": hits,
        "tags": classify_fund(title, body),
        "status": status,
    }


_LEGACY_PREFIX = {"gz_kjj": "gz_portal"}


def _migrate(rec):
    """Backfill fields added after the first release so legacy records from
    gz_fund_poc.py stop showing up as '?' in the per-source summary."""
    if rec.get("source_name") and rec.get("level"):
        return rec
    sid = rec.get("source_id") or (rec.get("id", "").split(":", 1)[0])
    sid = _LEGACY_PREFIX.get(sid, sid)
    src = get_source(sid)
    if src:
        rec.setdefault("source_id", src["id"])
        rec["source_name"] = rec.get("source_name") or src["name"]
        rec["level"] = rec.get("level") or src["level"]
    return rec


def merge(existing, fresh):
    existing = [_migrate(dict(r)) for r in existing]
    by_id = {}
    for r in existing + fresh:
        by_id[r["id"]] = r
    out = list(by_id.values())
    out.sort(key=lambda r: (r.get("published", ""), r.get("id", "")), reverse=True)
    return out


# ===========================================================================
# CALENDAR
# ===========================================================================
def load_seed():
    """Load the manually maintained calendar anchors.

    Previously this swallowed every error and silently returned an empty seed,
    so a missing or malformed file showed up only as a mysteriously empty
    calendar on the website. Now it says exactly what went wrong.
    """
    path = os.path.join(DATA_DIR, "calendar_seed.json")
    if not os.path.exists(path):
        print(f"  ⚠ 未找到 {path}")
        print(f"    → 日历将没有手工锚点（anchors 为空）。")
        print(f"    → 请确认仓库中存在 data/calendar_seed.json（注意在 data/ 目录内）。")
        return {"anchors": [], "manual_sources": []}
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            seed = json.load(fh)
    except json.JSONDecodeError as exc:
        print(f"  ⚠ {path} 不是合法 JSON：{exc}")
        print(f"    → 常见原因：复制粘贴时缺了逗号/引号，或混入了非代码文字。")
        return {"anchors": [], "manual_sources": []}
    except Exception as exc:
        print(f"  ⚠ 读取 {path} 失败：{type(exc).__name__}: {exc}")
        return {"anchors": [], "manual_sources": []}
    n_a = len(seed.get("anchors", []))
    n_m = len(seed.get("manual_sources", []))
    print(f"  ✓ 日历种子：{n_a} 个锚点 · {n_m} 个人工巡检源")
    if n_a == 0:
        print("    ⚠ 文件读到了，但 anchors 是空的——请检查文件内容是否完整。")
    return seed


# Only call types specific enough to be MEANINGFUL as a calendar row.
# Deliberately excludes generic words like 项目指南 / 专项项目 / 国际合作:
# nearly every NSFC title contains them, so they produced rows such as
# "专项项目" that told the reader nothing. A row must name a real scheme.
_CALL_TYPES = ["重点领域研发", "产学研合作", "基础研究计划", "市校院联合",
               "政府间国际科技创新合作", "粤港澳", "粤穗", "面上项目",
               "重点研发计划", "联合基金", "双边研讨会", "合作研究项目",
               "外国学者", "来华交流", "高新技术企业", "科技型中小企业"]

# A single occurrence is an event, not a pattern. Require this many before a
# derived row is shown as "seasonality".
_MIN_DERIVED = 2


def derive_calendar(records):
    buckets, orgs = {}, {}
    for r in records:
        if not r.get("is_opportunity"):
            continue
        t = r["title"]
        ct = next((k for k in _CALL_TYPES if k in t), None)
        if not ct:
            continue
        mo = (r.get("published") or "")[5:7]
        if not mo:
            continue
        key = (ct, r.get("level", ""))
        buckets.setdefault(key, {}).setdefault(mo, 0)
        buckets[key][mo] += 1
        orgs.setdefault(key, {})
        dep = r.get("department") or r.get("source_name") or ""
        if dep:
            orgs[key][dep] = orgs[key].get(dep, 0) + 1
    out = []
    for (ct, lvl), months in sorted(buckets.items()):
        total = sum(months.values())
        if total < _MIN_DERIVED:
            continue                      # one-off, not a pattern
        span = sorted(int(m) for m in months)
        # name the issuing body: a row called just "合作研究项目" tells the
        # reader nothing about who runs it.
        dep_counts = orgs.get((ct, lvl), {})
        org = max(dep_counts, key=dep_counts.get) if dep_counts else "来源不明"
        if len(dep_counts) > 1:
            org += f"等{len(dep_counts)}个来源"
        out.append({"name": ct, "org": org, "level": lvl,
                    "months": span, "counts": months, "source": "derived",
                    "confidence": "observed",
                    "window": "历年集中在 " + "、".join(f"{m}月" for m in span),
                    "note": f"由已抓取的 {total} 条同类历史通知统计得出"})
    out.sort(key=lambda r: -sum(r["counts"].values()))
    return out


def build_calendar(records):
    seed = load_seed()
    anchors = []
    for a in seed.get("anchors", []):
        a = dict(a)
        a["source"] = "manual"
        anchors.append(a)
    return {"derived": derive_calendar(records), "anchors": anchors,
            "manual_sources": seed.get("manual_sources", [])}


# ===========================================================================
# NETWORK
# ===========================================================================
def fetch(url, timeout=30):
    import urllib.request
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9",
    })
    return urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", "replace")


def page_url(src, n):
    if n <= 1:
        return src["list_url"]
    if not src.get("page_fmt"):
        return None
    base = src["list_url"].rsplit("/", 1)[0] + "/"
    return base + src["page_fmt"].format(n=n)


_PREFILTER_RE = re.compile(
    r"科技|科学技术|自然科学|基础研究|研发|创新|人才|实验室|重点领域|产学研|专项")


def load_notices():
    try:
        with open(os.path.join(DATA_DIR, "notices.json"), "r", encoding="utf-8") as fh:
            return json.load(fh).get("notices", [])
    except Exception:
        return []


def probe():
    """Check every source: reachable? how many links? sample titles/dates.
    Run this whenever adding a source or when a source goes quiet."""
    print("探测各源结构（新增源必跑）\n" + "=" * 66)
    for src in SOURCES:
        flag = "" if src["enabled"] else "  [已停用]"
        print(f"\n▌{src['name']}  [{src['id']}]{flag}")
        print(f"  {src['list_url']}")
        try:
            h = fetch(src["list_url"])
        except Exception as exc:
            print(f"  ✗ 抓取失败 {type(exc).__name__}: {exc}")
            continue
        base = src["list_url"]
        items = parse_list(h, src["detail_re"], base=base)
        dated = sum(1 for i in items if i["published"])
        print(f"  ✓ {len(h)} 字节 · 解析到 {len(items)} 条 · 其中 {dated} 条带日期")
        if not items:
            print("  ⚠ 0 条：detail_re 可能不匹配，需 dump 原始 HTML 核对")
        for it in items[:3]:
            print(f"     {it['published'] or '(无日期)':<12}{it['title'][:44]}")
        if src.get("page_fmt"):
            u2 = page_url(src, 2)
            try:
                h2 = fetch(u2)
                n2 = len(parse_list(h2, src["detail_re"], base=base))
                print(f"  ✓ 第2页可达 {u2}  {n2} 条")
            except Exception as exc:
                print(f"  ✗ 第2页失败 {u2}  {exc}")
        else:
            print("  · 无翻页（JS分页或未配置），仅第1页")
        time.sleep(1)
    print("\n" + "=" * 66)


def dump_links(sid, limit=40):
    """Show the real hrefs on a source's list page. Use when --probe reports
    0 links: it reveals the actual URL shape so detail_re can be corrected."""
    src = get_source(sid)
    if not src:
        print("未知源 id:", sid, "| 可用:", ", ".join(s["id"] for s in SOURCES))
        return
    print(f"▌{src['name']} 列表页真实链接样本\n  {src['list_url']}\n" + "=" * 66)
    try:
        h = fetch(src["list_url"])
    except Exception as exc:
        print("抓取失败:", exc)
        return
    print(f"页面 {len(h)} 字节")
    hrefs = re.findall(r'<a\b[^>]*href="([^"]+)"', h, re.I)
    print(f"页面共 {len(hrefs)} 个链接。前 {limit} 个非导航链接：\n")
    shown = 0
    for hf in hrefs:
        if hf.startswith(("#", "javascript:", "mailto:")):
            continue
        print("   ", hf)
        shown += 1
        if shown >= limit:
            break
    print(f"\n当前 detail_re = {src['detail_re']}")
    hit = [x for x in hrefs if re.search(src["detail_re"], x)]
    print(f"其中匹配 detail_re 的: {len(hit)} 个")
    for x in hit[:5]:
        print("    ✓", x)


def harvest(src, pages=1, until=None, sleep=1.0):
    known = {r["id"] for r in load_notices()}
    base = src["list_url"]
    records, stop = [], False
    for n in range(1, pages + 1):
        url = page_url(src, n)
        if not url:
            break
        try:
            listing = fetch(url)
        except Exception as exc:
            print(f"  [list] p{n} ERROR {type(exc).__name__}: {exc}")
            break
        items = parse_list(listing, src["detail_re"], base=base)
        print(f"  [list] p{n} {len(items)} links")
        if not items:
            break
        for it in items:
            if until and it.get("published") and it["published"] < until:
                print(f"  [stop] 回溯到 {it['published']} < {until}")
                stop = True
                break
            nid = src["id"] + ":" + _detail_id(it["url"])
            if nid in known:
                continue
            if src.get("prefilter") and not _PREFILTER_RE.search(it["title"]):
                continue
            try:
                page_html = fetch(it["url"])
            except Exception as exc:
                print(f"     [detail] ERROR {exc}")
                continue
            d = parse_detail(page_html, url=it["url"])
            d["published"] = d.get("published") or it.get("published", "")
            d["title"] = d.get("title") or it["title"]
            rec = build_record(d, src)
            records.append(rec)
            known.add(nid)
            print(f"     + [{rec['tags'][0]}] {rec['published']} {rec['title'][:36]}")
            time.sleep(sleep)
        if stop:
            break
        time.sleep(sleep)
    return records


def run_live(only=None, pages=1, until=None):
    fresh = []
    for src in SOURCES:
        if not src["enabled"]:
            continue
        if only and src["id"] != only:
            continue
        print(f"\n▌{src['name']} [{src['id']}]")
        fresh.extend(harvest(src, pages=pages, until=until))
    recs = merge(load_notices(), fresh)
    gen = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    _write("notices.json", {"generated": gen, "count": len(recs), "notices": recs})
    _write("calendar.json", dict(generated=gen, **build_calendar(recs)))
    opp = sum(1 for r in recs if r.get("is_opportunity"))
    oldest = min((r["published"] for r in recs if r.get("published")), default="—")
    by_src = {}
    for r in recs:
        by_src[r.get("source_name", "?")] = by_src.get(r.get("source_name", "?"), 0) + 1
    print("\n" + "-" * 60)
    print(f"本次新增 {len(fresh)} · 库内 {len(recs)}（申报机会 {opp}）· 回溯至 {oldest}")
    for k, v in sorted(by_src.items(), key=lambda x: -x[1]):
        print(f"   {k}: {v}")


def _write(name, obj):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(os.path.join(DATA_DIR, name), "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=1)


# ===========================================================================
# SELFTEST -- fixtures are REAL markup/text captured from each site
# ===========================================================================
FIX_GZ = '''<ul class="news_list"><li><i></i>
<a href="https://www.gz.gov.cn/xw/tzgg/content/post_10905933.html" target="_blank" title="致市民的一封信">
  <!-- 规章文件 -->
  致市民的一封信</a><span class="time">2026-07-17</span></li>
<li><i></i><a href="https://www.gz.gov.cn/xw/tzgg/content/post_9600238.html" target="_blank" title="广州市科学技术局关于发布重点研发计划2024年度重点领域研发专题产学研合作项目认定立项方向申报指南的通知">
  <!-- 其他文件 -->
  广州市科学技术局关于发布…</a><span class="time">2024-04-16</span></li></ul>'''

FIX_NSFC = '''<ul>
<li><a href="https://www.nsfc.gov.cn/p1/3381/2824/138937.html">关于发布2026年度国家自然科学基金指南引导类原创探索计划项目——“变革性手性化学…</a><span>2026-07-15</span></li>
<li><a href="https://www.nsfc.gov.cn/p1/3381/2824/137822.html">2026年度国家自然科学基金委员会与韩国国家研究基金会合作研究项目指南</a><span>2026-06-29</span></li>
<li><a href="https://www.nsfc.gov.cn/p1/3381/2824/99667.html">关于2026年度国家自然科学基金项目申请与结题等有关事项的通告</a><span>2026-01-14</span></li></ul>'''

FIX_MOST_DETAIL = '''<html><head><title>国家科技管理信息系统公共服务平台</title></head><body>
<h1>科技部国际合作司关于发布国家重点研发计划“政府间国际科技创新合作”重点专项 2026年度第二批联合研发项目申报指南的通知</h1>
<p>发布时间：2026年04月30日 来源：科学技术部</p>
<p>项目申报单位网上填报申报书的受理时间为：2026年5月7日8:00至2026年6月25日16:00。</p>
</body></html>'''


def selftest():
    print("离线自测（全部基于各站真实结构）...")
    gz = get_source("gz_portal")

    # --- generic list parser on gz markup (comments inside <a>, span.time) ---
    items = parse_list(FIX_GZ, gz["detail_re"])
    assert len(items) == 2, items
    assert items[0]["title"] == "致市民的一封信", items[0]
    assert items[0]["published"] == "2026-07-17", items[0]
    assert items[1]["published"] == "2024-04-16", items[1]
    assert all("规章文件" not in i["title"] for i in items)

    # --- same parser on NSFC markup (no title attr, date in sibling span) ---
    nz = get_source("nsfc_zn")
    ni = parse_list(FIX_NSFC, nz["detail_re"])
    assert len(ni) == 3, ni
    assert ni[0]["published"] == "2026-07-15", ni[0]
    assert "韩国" in ni[1]["title"], ni[1]

    # --- trailing date must not contaminate the title (NSFC 通知公告 does this) ---
    _t1 = "可持续发展国际合作科学计划2026年度项目指南（第二批） 2026-06-05"
    assert _strip_trailing_date(_t1) == "可持续发展国际合作科学计划2026年度项目指南（第二批）"
    assert _strip_trailing_date("某通知 2026-06") == "某通知"
    assert _strip_trailing_date("2026年度项目指南") == "2026年度项目指南"   # don't over-strip

    # --- relative hrefs must resolve (MOST lists them relative to the column) ---
    most = get_source("most")
    rel = ('<ul><li><a href="kjjh_tztg_all/20260430/5824.html">科技部…“政府间国际科技创新合作”'
           '重点专项2026年度第二批联合研发项目申报指南的通知</a>'
           '<span>2026-04-30</span></li></ul>')
    # base is the column URL; a column-relative href must resolve against the site root
    ri = parse_list(rel, most["detail_re"], base="https://service.most.gov.cn/")
    assert len(ri) == 1, ri
    assert ri[0]["url"] == "https://service.most.gov.cn/kjjh_tztg_all/20260430/5824.html", ri[0]["url"]
    assert ri[0]["published"] == "2026-04-30", ri[0]
    # absolute form must still work
    ab = '<a href="https://service.most.gov.cn/kjjh_tztg_all/20260430/5824.html">政府间国际科技创新合作专项指南</a><span>2026-04-30</span>'
    ai = parse_list(ab, most["detail_re"], base=most["list_url"])
    assert len(ai) == 1 and ai[0]["url"].startswith("https://service.most.gov.cn/"), ai

    # --- pagination ---
    assert page_url(gz, 1) == "https://www.gz.gov.cn/xw/tzgg/"
    assert page_url(gz, 2) == "https://www.gz.gov.cn/xw/tzgg/index_2.html"
    assert page_url(nz, 2) is None          # NSFC paginates via JS

    # --- MOST detail: no meta tags, must fall back to h1 + body publish date ---
    d = parse_detail(FIX_MOST_DETAIL, url="https://service.most.gov.cn/kjjh_tztg_all/20260430/5824.html")
    assert d["published"] == "2026-04-30", d["published"]
    assert "政府间国际科技创新合作" in d["title"], d["title"]
    dl, conf = extract_deadline(d["body"])
    assert (dl, conf) == ("2026-06-25", "explicit"), (dl, conf)

    # --- deadline forms ---
    assert extract_deadline("本项目常年申报。") == (None, "rolling")
    assert extract_deadline("2026年3月15日下午18时截止") == ("2026-03-15", "explicit")
    assert extract_deadline("报名截止时间延长至2025年10月20日17时") == ("2025-10-20", "explicit")
    assert extract_deadline("请于2025年4月20日前完成") == ("2025-04-20", "explicit")
    assert extract_deadline("我局现组织开展验收工作") == (None, "unknown")

    # --- opportunity filter ---
    assert is_opportunity("广州市重点领域研发计划2025年度新能源专题申报指南")
    assert not is_opportunity("广州市科技计划项目验收工作的通知")
    assert not is_opportunity("《2026年度国家自然科学基金项目指南》征订通知")
    assert not is_opportunity("致市民的一封信")

    # --- four-state tagging ---
    assert classify_fund("国家自然科学基金青年科学基金项目") == ["red"]
    assert classify_fund("2026年度优秀青年科学基金项目指南") == ["red"]
    # verified: 政府间国际合作 is leadable by this PI -> vip, not team
    _t2 = "“政府间国际科技创新合作”重点专项2026年度第二批联合研发项目申报指南"
    assert classify_fund(_t2) == ["vip"], classify_fund(_t2)
    assert classify_fund("2026年度NSFC与香港研资局联合科研资助基金合作研究项目指南") == ["vip"]
    assert classify_fund("广东省重点领域研发计划新能源专题申报指南") == ["team"]
    assert classify_fund("国家重点研发计划“合成生物学”重点专项申报指南") == ["team"]
    assert classify_fund("某某一般性通知") == ["verify"]

    # --- regression: real titles from the 2026-07-18 harvest that were初版误判 ---
    # 站方用词是“研究资助局”，早期只写了“研资局”，导致 JRS 未置顶
    assert classify_fund("2026年度国家自然科学基金委员会与香港研究资助局联合科研资助基金合作研究项目指南") == ["vip"]
    assert classify_fund("2026年度国家自然科学基金外国学者研究基金项目指南") == ["vip"]
    assert classify_fund("2026年度国家自然科学基金欧洲青年科研人员来华交流项目指南") == ["vip"]
    assert classify_fund("中德科学中心2026年度中德学生与青年学者短期讲习班") == ["vip"]
    # 竞赛/解读/结题通告不是申报机会
    assert not is_opportunity("广州市科学技术局关于举办2026年广州科技创新创业大赛全球赛的通知")
    assert not is_opportunity("广州市卫生健康委员会关于举办粤港澳大湾区托育人才技能大赛的通知")
    assert not is_opportunity("关于2026年度国家自然科学基金项目申请与结题等有关事项的通告")
    assert not is_opportunity("【图文解读】《广州市科技保险补助资金管理办法》解读材料")
    # 相关性以标题为准：正文出现“申报材料”不应使整条变相关
    assert is_relevant("申报材料 创新", title="广州市住房和城乡建设局关于商品住房补贴的通知")[0] is False
    assert is_relevant("", title="广州市重点领域研发计划新能源与新材料专题申报指南")[0] is True
    assert is_relevant("", title="NSFC与香港研究资助局合作研究项目指南")[0] is True

    # --- legacy record migration (gz_kjj -> gz_portal) ---
    old = {"id": "gz_kjj:123", "title": "旧记录", "published": "2025-01-07"}
    mig = merge([old], [])[0]
    assert mig["source_name"] == "广州市政府门户·通知公告", mig
    assert mig["level"] == "市", mig

    # --- record build + merge ---
    rec = build_record(parse_detail(FIX_MOST_DETAIL, url=".../5824.html"), get_source("most"))
    assert rec["tags"] == ["vip"] and rec["is_opportunity"], rec
    assert rec["level"] == "国家" and rec["deadline"] == "2026-06-25"
    merged = merge([rec], [dict(rec, title="更新版")])
    assert len(merged) == 1 and merged[0]["title"] == "更新版"

    # --- calendar ---
    cal = build_calendar([rec])
    assert isinstance(cal["derived"], list) and "anchors" in cal

    print("全部离线自测通过 ✓")
    print("  通用列表解析（gz 与 NSFC 两种不同标记）、翻页、MOST 无 meta 回退、")
    print("  5 种截止写法、事务过滤、四态标签、去重、日历")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--probe", action="store_true", help="探测各源结构")
    ap.add_argument("--dump", default=None, metavar="SOURCE_ID",
                    help="打印某个源列表页的真实链接样本（--probe 报0条时用）")
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--only", default=None, help="只跑某个源 id")
    ap.add_argument("--pages", type=int, default=1)
    ap.add_argument("--until", default=None, help="回溯截止日期 YYYY-MM-DD")
    a = ap.parse_args()
    if a.selftest:
        selftest()
    elif a.dump:
        dump_links(a.dump)
    elif a.probe:
        probe()
    elif a.live:
        run_live(only=a.only, pages=a.pages, until=a.until)
    else:
        ap.print_help()
        sys.exit(1)
