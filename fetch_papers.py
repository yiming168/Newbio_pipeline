#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fetch_papers.py — Newbio Health 内容流水线 · 第一段:取材

从 Europe PMC 抓取指定主题的最新文献(含 PubMed 记录与预印本),
去重后输出 JSONL,供下一段(本地模型筛选)使用。

只用标准库,不需要 pip install。

用法(Windows 用 py,macOS/Linux 用 python3):
    py fetch_papers.py --selftest          # 验证网络和接口是否通
    py fetch_papers.py --days 7            # 抓最近 7 天
    py fetch_papers.py --days 30 --topic pet_nutrition
    py fetch_papers.py --days 7 --dry-run  # 只看不写,不记入已读库

输出:
    out/YYYY-MM-DD_HHMM.jsonl   本次新增的文献,一行一条
    seen.sqlite3                已见过的文献 ID,用于跨次去重
"""

import argparse
import datetime as dt
import email.utils
import html
import json
import os
import random
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# Windows 中文系统的控制台默认是 GBK,直接 print 非 ASCII 符号会抛 UnicodeEncodeError。
# 这里把 stdout/stderr 强制成 UTF-8。Python 3.7+ 都支持,失败也不影响主流程。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

# ─────────────────────────────────────────────────────────────
# 配置:改这里
# ─────────────────────────────────────────────────────────────

# 礼貌起见,把联系邮箱放进 User-Agent。接口方遇到异常流量时会先联系你而不是直接封。
# 走环境变量,别硬编码 —— 这个仓库是公开的。
#   Windows 永久设置:  setx EUROPEPMC_CONTACT_EMAIL "you@example.com"
#   设完要新开一个终端窗口才生效。
CONTACT_EMAIL = os.environ.get("EUROPEPMC_CONTACT_EMAIL", "anonymous@example.com")
TOOL_NAME = "newbio-health-pipeline"

# 主题查询式。key 是主题名(会写进输出),value 是 Europe PMC 查询语法。
# 检索词用英文 —— 文献本身是英文的。
TOPICS = {
    # 锚点词限定在标题(TITLE:),否则 Europe PMC 连正文一起搜,常用词会捞进无关文献。
    # 但注意:查询式只负责"召回",精筛交给第二段的模型。这里不要过度收紧。
    # 肠道那条线 —— 第二版。第一版只认标题里的 probiotic / gut microbiota,
    # 结果漏掉一大类:**写菌群不写益生菌、写菌名不写品类**的论文。
    # 实测:451 篇语料里只有 3.3% 来自高影响力期刊,110 篇是预印本 ——
    # 词表太窄,把这个领域里写得最好的那批论文挡在外面了。
    # 这一版把「肠道微生态」的常见英文说法、常见菌属名和作者关键词都加进来,
    # 第一次试跑时裸写 TITLE:microbiota / TITLE:microbiome,结果三条命中里
    # 两条是唾液菌群和阴道菌群 —— 「菌群」不限定部位等于没限定,所以收回成短语。
    "probiotics_gut": (
        '('
        # 品类词
        'TITLE:probiotic* OR TITLE:synbiotic* OR TITLE:postbiotic* OR TITLE:prebiotic* '
        'OR TITLE:fermented '
        # 菌群 / 肠道微生态的各种说法
        'OR TITLE:"gut microbiota" OR TITLE:"gut microbiome" OR TITLE:"gut microbial" '
        'OR TITLE:"intestinal microbiota" OR TITLE:"intestinal microbiome" '
        'OR TITLE:"fecal microbiota" OR TITLE:"faecal microbiota" '
        'OR TITLE:"gut dysbiosis" OR TITLE:"intestinal dysbiosis" '
        'OR TITLE:"gut bacteria" OR TITLE:"gut flora" OR TITLE:"intestinal flora" '
        'OR TITLE:"gut-brain axis" OR TITLE:"gut-liver axis" OR TITLE:"gut barrier" '
        'OR TITLE:"intestinal barrier" OR TITLE:"short-chain fatty acid" '
        'OR TITLE:"short-chain fatty acids" OR TITLE:"fecal microbiota transplantation" '
        # 写菌名不写品类的那一类
        'OR TITLE:Lactobacillus OR TITLE:Lactiplantibacillus OR TITLE:Lacticaseibacillus '
        'OR TITLE:Limosilactobacillus OR TITLE:Bifidobacterium OR TITLE:Akkermansia '
        'OR TITLE:Faecalibacterium OR TITLE:Roseburia OR TITLE:Bacteroides '
        'OR TITLE:Saccharomyces OR TITLE:Enterococcus '
        # 作者关键词:没有标注滞后,能捞到标题里没写清楚的
        'OR KW:probiotics OR KW:prebiotics OR KW:postbiotics OR KW:"gut microbiota" '
        'OR KW:"gut microbiome" OR KW:microbiome OR KW:dysbiosis'
        ') '
        'NOT (TITLE:mice OR TITLE:mouse OR TITLE:murine OR TITLE:rat OR TITLE:rats '
        'OR TITLE:zebrafish OR TITLE:piglet* OR TITLE:broiler* OR TITLE:"in vitro")'
    ),
    # 前沿观察 —— 按期刊抓,**不排除动物实验**。
    # 顶刊的机制论文被上面那条线卡掉有两个原因:标题里常常没有 probiotic,
    # 主体又多是小鼠。这一条两个口子都松开,代价是拿回来的多半是
    # evidence 2 的机制研究 —— 所以它在报告里单独成一节,不跟选题桶抢名额,
    # 也不要指望它的分数好看。看的是「这个领域现在在往哪走」。
    "frontier_microbiome": (
        '('
        'JOURNAL:"Nature" OR JOURNAL:"Science" OR JOURNAL:"Cell" '
        'OR JOURNAL:"Cell host & microbe" OR JOURNAL:"Cell metabolism" '
        'OR JOURNAL:"Cell reports medicine" OR JOURNAL:"Immunity" '
        'OR JOURNAL:"Nature medicine" OR JOURNAL:"Nature microbiology" '
        'OR JOURNAL:"Nature metabolism" OR JOURNAL:"Nature communications" '
        'OR JOURNAL:"Nature aging" OR JOURNAL:"Science translational medicine" '
        'OR JOURNAL:"Gut" OR JOURNAL:"Gastroenterology" OR JOURNAL:"Microbiome" '
        'OR JOURNAL:"Gut microbes" OR JOURNAL:"The ISME journal" '
        'OR JOURNAL:"EMBO molecular medicine" OR JOURNAL:"Cell reports"'
        ') AND ('
        'TITLE:microbiota OR TITLE:microbiome OR TITLE:probiotic* OR TITLE:dysbiosis '
        'OR TITLE:commensal OR TITLE:"gut bacteria" '
        'OR ABSTRACT:"gut microbiota" OR ABSTRACT:"gut microbiome" '
        'OR KW:microbiome OR KW:"gut microbiota"'
        ')'
    ),
    # 宠物营养 —— 第三版。前两版在「太窄」和「太脏」两头来回:
    #   v1 物种词和营养词都限定标题 → 一周只剩 4 条
    #   v2 营养词不限定字段 → 23 条里只有 8 条跟营养有关(35%),
    #      剩下是骨科、肿瘤、MRI、狂犬病。**原因是 Europe PMC 的无字段检索会搜开放
    #      获取文章的全文**:一篇犬淋巴瘤论文只要正文某处提到 "diet",就会被捞进来。
    #      (证据:同一个查询式在本地只用标题+摘要去匹配,23 条里只能复现 16 条。)
    #   v3 营养词全部加字段,但把词表放宽:标题里的营养/配方/口腔护理词,
    #      加上摘要里足够具体的短语("basal diet"、"feeding behaviour" 等),加上作者关键词。
    #      用 9/10–9/19 的语料离线回放:相关 11 条全部命中、无关 15 条全部挡掉、
    #      漏 2 条(一篇宠物饼干会被 probiotics_gut 捞到;一篇犬免疫衰老预印本只有
    #      一个饮食限制分组,本来就是边缘)。
    # 以后改这里:**营养那一半不要再出现无字段的词**,test_pipeline 里有断言盯着。
    "pet_nutrition": (
        '(TITLE:dog OR TITLE:dogs OR TITLE:canine OR TITLE:cat OR TITLE:cats '
        'OR TITLE:feline OR TITLE:kitten OR TITLE:kittens OR TITLE:puppy OR TITLE:puppies '
        'OR TITLE:"companion animal" OR TITLE:"companion animals" '
        'OR TITLE:"pet food" OR TITLE:"pet foods") '
        'AND ('
        # 标题:营养、配方、原料
        'TITLE:nutrition OR TITLE:nutritional OR TITLE:nutrient* OR TITLE:diet OR TITLE:diets '
        'OR TITLE:dietary OR TITLE:food OR TITLE:foods OR TITLE:feeding OR TITLE:kibble '
        'OR TITLE:supplement* OR TITLE:probiotic* OR TITLE:prebiotic* OR TITLE:postbiotic* '
        'OR TITLE:synbiotic* OR TITLE:microbiota OR TITLE:microbiome OR TITLE:digestibility '
        'OR TITLE:palatability OR TITLE:"fatty acid" OR TITLE:"fatty acids" OR TITLE:protein '
        'OR TITLE:fibre OR TITLE:fiber OR TITLE:polysaccharide* OR TITLE:vitamin* '
        'OR TITLE:mineral* OR TITLE:obesity OR TITLE:"body condition" '
        # 标题:口腔护理(洁齿类产品是宠物营养的一个大品类)
        'OR TITLE:dental OR TITLE:plaque OR TITLE:periodontal '
        # 摘要:只收足够具体的短语,单个 "diet" 放进摘要又会把噪音放回来
        'OR ABSTRACT:"dietary supplementation" OR ABSTRACT:"dietary intervention" '
        'OR ABSTRACT:"basal diet" OR ABSTRACT:"pet food" OR ABSTRACT:kibble '
        'OR ABSTRACT:"apparent digestibility" OR ABSTRACT:"feeding behaviour" '
        'OR ABSTRACT:"feeding behavior" OR ABSTRACT:"nutritional management" '
        'OR ABSTRACT:"nutritional requirements" '
        # 作者关键词:没有标注滞后,见 README 设计决策 4
        'OR KW:nutrition OR KW:diet OR KW:"pet food" OR KW:probiotics OR KW:microbiota)'
    ),
    "animal_feed": (
        '(TITLE:"feed additive*" OR TITLE:"animal nutrition" '
        'OR TITLE:"direct-fed microbial*" OR TITLE:broiler* OR TITLE:piglet* '
        'OR TITLE:"laying hen*" OR TITLE:weaning OR TITLE:weaned) '
        'AND (probiotic* OR "gut health" OR microbiota OR performance)'
    ),
    # ── 原料雷达专用 ─────────────────────────────────────
    # 一个原料名都不列。抓的是"某种物质 × 人体研究"这个句式,
    # 具体是南非醉茄还是岩藻多糖,交给第二段的模型从摘要里抽。
    #
    # 上一版是硬列原料名(omega-3、叶黄素、奶蓟草…),问题有两个:
    # 想不到的原料永远抓不到,而想得到的原料多半已经不新了。
    #
    # 两条并集:
    #   标题路线 —— 标题里出现 supplementation / extract / nutraceutical…
    #   关键词路线 —— KW: 是作者投稿时自填的,不经人工标注,没有滞后。
    #     实测它能捞到标题里没写清楚的原料(5-甲基四氢叶酸、藻类活性物)。
    #
    # 注意:这条**故意不排临床人群**,跟下面的 lifestyle_health 相反。
    # 「某原料在慢性肾病患者中的 RCT」对写稿没用,但对做进口是卖点 ——
    # 拿临床证据跟客户谈比拿健康人数据有力。
    #
    # 不要依赖 PUB_TYPE:"Randomized Controlled Trial" —— 它和 MeSH 一样是
    # 人工标注,最新文献还没标上。用标题里的人体线索代替
    # (adults / women / healthy / participants / volunteers)。
    "functional_ingredients": (
        '((TITLE:supplementation OR TITLE:supplement* OR TITLE:extract* '
        'OR TITLE:nutraceutical* OR TITLE:"functional food*" OR TITLE:postbiotic* '
        'OR TITLE:probiotic* OR TITLE:synbiotic* OR TITLE:"bioactive compound*" '
        'OR TITLE:"novel food" OR TITLE:ingestion OR TITLE:"oral administration" '
        'OR TITLE:fortified OR TITLE:fortification) '
        'OR (KW:"dietary supplement" OR KW:"dietary supplements" '
        'OR KW:"functional food" OR KW:"functional foods" OR KW:nutraceutical '
        'OR KW:nutraceuticals OR KW:"bioactive compounds" OR KW:"plant extract" '
        'OR KW:"natural product" OR KW:supplementation OR KW:probiotics '
        'OR KW:prebiotics OR KW:postbiotics)) '
        'AND (TITLE:randomized OR TITLE:randomised OR TITLE:"double-blind" '
        'OR TITLE:placebo OR TITLE:crossover OR TITLE:trial OR TITLE:"meta-analysis" '
        'OR TITLE:"systematic review" OR TITLE:adults OR TITLE:women OR TITLE:men '
        'OR TITLE:participants OR TITLE:volunteers OR TITLE:healthy OR TITLE:human* '
        'OR TITLE:subjects OR TITLE:effect*) '
        'NOT (TITLE:mice OR TITLE:mouse OR TITLE:rat OR TITLE:rats OR TITLE:"in vitro" '
        'OR TITLE:broiler* OR TITLE:piglet* OR TITLE:"laying hen*" OR TITLE:calves)'
    ),

}

# 附加过滤条件,拼在每个查询后面。
#   SRC:MED  = PubMed 收录    SRC:PPR = 预印本    SRC:PMC = PMC 全文
# 如果某个主题返回 0 条,先把这一行改成 "" 试试,多半是过滤太严。
#
# 踩过的坑:上一版写成 (SRC:MED OR SRC:PPR) AND ... AND LANG:eng,
# 结果预印本一条都抓不到。实测同期带摘要的预印本有 10,323 条,加上 LANG:eng
# 之后是 0 —— 预印本记录普遍没有语言元数据,这个条件把它们整片株连了。
# 所以 LANG:eng 只加在 SRC:MED 上,预印本不受它管。
FILTERS = "((SRC:MED AND LANG:eng) OR SRC:PPR) AND HAS_ABSTRACT:Y"

# 某些主题的回溯窗必须比 --days 长。
#
# 实测(2026-10-15,20 本期刊,limit=25):
#   frontier_microbiome   7 天 → 0 条 | 30 天 → 25+ | 60 天 → 25+ | 120 天 → 25+
#   19/20 本期刊单独跑 60 天都有命中(只有 Immunity 是 0,它本来就少发这个方向)
#
# 不是查询式坏了,是**正式期刊记录进 Europe PMC 有滞后**:预印本是即时的,
# 正式文章要等收录。按周跑、窗口跟着往前挪,这批文章就永久漏掉了 ——
# 因为下周的窗口已经不覆盖它们的发表日期。
# 去重库挡着重复,所以窗口开大几乎不要钱:多抓回来的绝大部分会被 is_seen 丢掉。
TOPIC_MIN_DAYS = {
    "frontier_microbiome": 30,
}


BASE_URL = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
PAGE_SIZE = 100          # Europe PMC 单页上限 1000,取 100 更稳
MAX_PAGES = 20           # 单个主题最多翻多少页,防跑飞
REQUEST_GAP = 0.4        # 每次请求间隔(秒),别把人家打疼
TIMEOUT = 30

# 只有这些状态码值得重试。其余 4xx(400 语法错、404 路径错)重试多少次都一样,
# 快速失败反而能让你一眼看出是自己写错了,而不是被伪装成"网络不好"。
RETRYABLE = {408, 425, 429, 500, 502, 503, 504}
MAX_RETRIES = 6          # 1+2+4+8+16+32 ≈ 最多等 63 秒
MAX_BACKOFF = 60         # 单次等待上限,防止 Retry-After 给个离谱的值

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, "out")
DB_PATH = os.path.join(HERE, "seen.sqlite3")


# ─────────────────────────────────────────────────────────────
# 去重库
# ─────────────────────────────────────────────────────────────

def open_db(path=DB_PATH):
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS seen (
            uid        TEXT PRIMARY KEY,
            topic      TEXT,
            title      TEXT,
            pub_date   TEXT,
            first_seen TEXT
        )
    """)
    conn.commit()
    return conn


def is_seen(conn, uid):
    return conn.execute("SELECT 1 FROM seen WHERE uid = ?", (uid,)).fetchone() is not None


def mark_seen(conn, rec):
    conn.execute(
        "INSERT OR IGNORE INTO seen (uid, topic, title, pub_date, first_seen) VALUES (?,?,?,?,?)",
        (rec["uid"], rec["topic"], rec["title"], rec["pub_date"],
         dt.datetime.now().isoformat(timespec="seconds")),
    )


# ─────────────────────────────────────────────────────────────
# HTTP
# ─────────────────────────────────────────────────────────────

def retry_after_seconds(headers):
    """503/429 常带 Retry-After,值可能是秒数,也可能是 HTTP 日期。
    读得懂就按它说的等 —— 服务端比我们更清楚自己什么时候能缓过来。"""
    raw = (headers or {}).get("Retry-After")
    if not raw:
        return None
    raw = str(raw).strip()
    if raw.isdigit():
        return min(int(raw), MAX_BACKOFF)
    try:
        ts = email.utils.parsedate_to_datetime(raw)
        delta = (ts - dt.datetime.now(ts.tzinfo)).total_seconds()
        return max(0.0, min(delta, MAX_BACKOFF))
    except Exception:
        return None


def http_get_json(params, retries=MAX_RETRIES):
    url = BASE_URL + "?" + urllib.parse.urlencode(params)
    ua = f"{TOOL_NAME}/1.0 (mailto:{CONTACT_EMAIL})"
    last_err = None

    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8"))

        except urllib.error.HTTPError as e:
            if e.code not in RETRYABLE:
                raise RuntimeError(
                    f"接口返回 {e.code} {e.reason} —— 这不是网络问题,重试没用。"
                    f"多半是查询语法写错了。\nURL: {url}"
                ) from e
            last_err = e
            wait = retry_after_seconds(e.headers)
            if wait is None:
                wait = min(2 ** attempt, MAX_BACKOFF)

        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            last_err = e
            wait = min(2 ** attempt, MAX_BACKOFF)

        if attempt == retries - 1:
            break
        # 抖动:四个主题要是同时撞上 503,别让它们踩着同一个节拍一起重试
        wait += random.uniform(0, 1.0)
        print(f"    请求失败({last_err}),{wait:.1f}s 后重试"
              f"(第 {attempt + 1}/{retries} 次)…", file=sys.stderr)
        time.sleep(wait)

    raise RuntimeError(f"连续 {retries} 次请求失败:{last_err}\nURL: {url}")


# ─────────────────────────────────────────────────────────────
# 解析
# ─────────────────────────────────────────────────────────────

# Europe PMC 的标题/摘要里会夹 HTML,而且有时是被转义过的,例如
#   &lt;i&gt;Enterocloster citroniae&lt;/i&gt;
# 不清干净的话,模型读到的是标签,你看到的也是一串 &lt;i&gt;。
_TAG_RE = re.compile(r"<[^>]{1,80}>")


def clean_text(value):
    """反转义 → 去标签 → 合并空白。反转义最多做两轮,够处理双重转义,
    又不会把标题里正常的 & 之类越解越离谱。"""
    text = str(value or "")
    for _ in range(2):
        if not any(tok in text for tok in ("&lt;", "&gt;", "&amp;", "&quot;", "&#")):
            break
        text = html.unescape(text)
    text = _TAG_RE.sub("", text)
    return re.sub(r"\s+", " ", text).strip()


def normalize(raw, topic):
    """把 Europe PMC 的一条结果压成我们自己的扁平结构。字段缺失一律给空值,不让下游炸掉。"""
    journal = ""
    ji = raw.get("journalInfo") or {}
    j = ji.get("journal") or {}
    journal = j.get("title") or j.get("medlineAbbreviation") or ""

    source = raw.get("source", "")
    ext_id = raw.get("id", "")
    uid = f"{source}:{ext_id}" if source and ext_id else (raw.get("doi") or raw.get("pmid") or ext_id)

    doi = raw.get("doi", "")
    if raw.get("pmid"):
        link = f"https://pubmed.ncbi.nlm.nih.gov/{raw['pmid']}/"
    elif doi:
        link = f"https://doi.org/{doi}"
    else:
        link = f"https://europepmc.org/article/{source}/{ext_id}" if source and ext_id else ""

    return {
        "uid": uid,
        "topic": topic,
        "title": clean_text(raw.get("title")).rstrip("."),
        "abstract": clean_text(raw.get("abstractText")),
        "authors": (raw.get("authorString") or "").strip(),
        "journal": journal,
        "pub_date": raw.get("firstPublicationDate", "") or ji.get("printPublicationDate", ""),
        "doi": doi,
        "pmid": raw.get("pmid", ""),
        "pmcid": raw.get("pmcid", ""),
        "source": source,
        "is_preprint": source == "PPR",
        "is_open_access": raw.get("isOpenAccess") == "Y",
        "cited_by": raw.get("citedByCount", 0),
        "link": link,
    }


def build_query(topic_query, date_from, date_to):
    parts = [f"({topic_query})", f"(FIRST_PDATE:[{date_from} TO {date_to}])"]
    if FILTERS.strip():
        parts.append(FILTERS)
    return " AND ".join(parts)


def search_topic(topic, topic_query, date_from, date_to, limit=None, retries=MAX_RETRIES):
    """翻页取完一个主题,返回 normalize 之后的列表。"""
    query = build_query(topic_query, date_from, date_to)
    cursor = "*"
    collected = []
    for page in range(MAX_PAGES):
        params = {
            "query": query,
            "format": "json",
            "resultType": "core",     # core 才带 abstract
            "pageSize": PAGE_SIZE,
            "cursorMark": cursor,
        }
        try:
            data = http_get_json(params, retries=retries)
        except RuntimeError as e:
            # 翻到第三页才挂,不该把前两页也扔了。有多少留多少。
            if collected:
                print(f"  [部分失败] 第 {page + 1} 页取不到,保留已取到的 {len(collected)} 条",
                      file=sys.stderr)
                print(f"    {e}", file=sys.stderr)
                return collected
            raise
        result_list = (data.get("resultList") or {}).get("result") or []
        for raw in result_list:
            rec = normalize(raw, topic)
            if rec["uid"]:
                collected.append(rec)
            if limit and len(collected) >= limit:
                return collected

        next_cursor = data.get("nextCursorMark")
        if not result_list or not next_cursor or next_cursor == cursor:
            break
        cursor = next_cursor
        time.sleep(REQUEST_GAP)
    return collected


# ─────────────────────────────────────────────────────────────
# 主流程
# ─────────────────────────────────────────────────────────────

def selftest():
    print("自检:向 Europe PMC 发一条最小查询…")
    data = http_get_json({
        "query": "probiotic", "format": "json", "resultType": "core", "pageSize": 1,
    })
    hits = data.get("hitCount")
    results = (data.get("resultList") or {}).get("result") or []
    print(f"  hitCount = {hits}")
    if not results:
        print("  [失败] 没拿到结果。接口通了但返回为空,检查查询语法。")
        return 1
    r = normalize(results[0], "selftest")
    print("  [成功] 接口正常")
    print(f"    标题:{r['title'][:70]}")
    print(f"    期刊:{r['journal']}   日期:{r['pub_date']}")
    print(f"    摘要:{'有,' + str(len(r['abstract'])) + ' 字符' if r['abstract'] else '无'}")
    print(f"    链接:{r['link']}")
    return 0


def main():
    ap = argparse.ArgumentParser(description="从 Europe PMC 抓取指定主题的最新文献")
    ap.add_argument("--days", type=int, default=7, help="回溯天数(默认 7)")
    ap.add_argument("--topic", action="append", help="只跑指定主题,可重复。默认跑全部")
    ap.add_argument("--limit", type=int, help="每个主题最多取多少条(调试用)")
    ap.add_argument("--retries", type=int, default=MAX_RETRIES,
                    help=f"单次请求最多重试几轮(默认 {MAX_RETRIES})。赶上接口抽风就调大")
    ap.add_argument("--dry-run", action="store_true", help="只打印,不写文件、不记入已读库")
    ap.add_argument("--show", type=int, default=10,
                    help="--dry-run 时列出多少条标题(默认 10)。调查询式的时候调大")
    ap.add_argument("--query", metavar="Q",
                    help="直接跑一条临时查询式(只配合 --dry-run 用)。"
                         "调查询式时用它一条条试,不要反复改脚本")
    ap.add_argument("--selftest", action="store_true", help="只验证接口连通性")
    args = ap.parse_args()

    if args.selftest:
        sys.exit(selftest())

    today = dt.date.today()
    date_to = today.isoformat()
    date_from = (today - dt.timedelta(days=args.days)).isoformat()

    topics = TOPICS
    if args.query:
        # 临时查询式。Europe PMC 的字段行为(尤其 JOURNAL:)在本地测不了,
        # 只能这样一条条打上去问它。
        if not args.dry_run:
            sys.exit("--query 只能配合 --dry-run 使用,免得把试出来的垃圾写进去重库")
        topics = {"(临时查询式)": args.query}
    elif args.topic:
        unknown = [t for t in args.topic if t not in TOPICS]
        if unknown:
            sys.exit(f"未知主题:{', '.join(unknown)}\n可用:{', '.join(TOPICS)}")
        topics = {t: TOPICS[t] for t in args.topic}

    print(f"检索区间:{date_from} → {date_to}   主题:{', '.join(topics)}")

    conn = None if args.dry_run else open_db()
    fresh, dup_count, total_hits = [], 0, 0

    for name, q in topics.items():
        days = max(args.days, TOPIC_MIN_DAYS.get(name, 0))
        t_from = (today - dt.timedelta(days=days)).isoformat()
        print(f"\n[{name}]" + (f"  回溯 {days} 天(这条线的下限)" if days != args.days else ""))
        try:
            found = search_topic(name, q, t_from, date_to,
                                 limit=args.limit, retries=args.retries)
        except RuntimeError as e:
            print(f"  [失败] {e}", file=sys.stderr)
            continue

        total_hits += len(found)
        if not found:
            print("  命中 0 条 —— 若持续为 0,把脚本里的 FILTERS 改成空字符串再试")
            continue

        new_here = 0
        for rec in found:
            if conn is not None and is_seen(conn, rec["uid"]):
                dup_count += 1
                continue
            fresh.append(rec)
            new_here += 1
            if conn is not None:
                mark_seen(conn, rec)

        print(f"  命中 {len(found)} 条,其中新增 {new_here} 条")
        time.sleep(REQUEST_GAP)

    fresh.sort(key=lambda r: r["pub_date"], reverse=True)

    print(f"\n合计命中 {total_hits} 条,去重后新增 {len(fresh)} 条,重复 {dup_count} 条")

    if args.dry_run:
        for r in fresh[:args.show]:
            flag = " [预印本]" if r["is_preprint"] else ""
            print(f"\n  {r['pub_date']}  {r['journal']}{flag}\n  {r['title']}\n  {r['link']}")
        if len(fresh) > args.show:
            print(f"\n  …另有 {len(fresh) - args.show} 条未显示(要看更多用 --show N)")
        return

    if not fresh:
        print("没有新增,不写文件。")
        if conn:
            conn.commit()
            conn.close()
        return

    os.makedirs(OUT_DIR, exist_ok=True)
    out_path = os.path.join(OUT_DIR, dt.datetime.now().strftime("%Y-%m-%d_%H%M") + ".jsonl")
    with open(out_path, "w", encoding="utf-8") as f:
        for rec in fresh:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    conn.commit()
    conn.close()
    print(f"已写入:{out_path}")


if __name__ == "__main__":
    main()
