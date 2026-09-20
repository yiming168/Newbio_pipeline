#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_pipeline.py — 离线测试。不联网,不需要本地模型,不装任何东西。

    py test_pipeline.py
    py test_pipeline.py -v      # 连通过的也列出来

网络那两头(Europe PMC、llama-server)用本地 mock server 顶替,
所以这份测试在任何机器上、任何时候都能跑。

**断言一律从模块常量推导,不写死数字。** 之前吃过亏:测试里硬编码了
当时的权重和版本号,改完口径之后测试全红,但代码其实是对的 ——
测试过期比没有测试更糟,它会让你不敢相信真正的失败。
"""

import json
import os
import re
import sys
import tempfile
import threading
import urllib.error
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

import fetch_papers as fp
import screen_papers as sp
import write_draft as wd

VERBOSE = "-v" in sys.argv
PASS = FAIL = 0
_section = [""]


def section(name):
    _section[0] = name
    if VERBOSE:
        print(f"\n── {name}")


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        if VERBOSE:
            print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  [{_section[0]}] {name}" + (f"\n        {detail}" if detail else ""))


# ═══════════════════════════════════════════════════════════════
# 假的 Europe PMC
# ═══════════════════════════════════════════════════════════════

class FakeEuropePMC(BaseHTTPRequestHandler):
    plan = {"fail_times": 0, "code": 503, "retry_after": None, "pages": 1}
    calls = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        FakeEuropePMC.calls.append(self.path)
        p = FakeEuropePMC.plan
        if p["fail_times"] > 0:
            p["fail_times"] -= 1
            self.send_response(p["code"])
            if p["retry_after"]:
                self.send_header("Retry-After", str(p["retry_after"]))
            self.end_headers()
            self.wfile.write(b'{"error":"x"}')
            return
        page = len([c for c in FakeEuropePMC.calls if "cursorMark" in c])
        results = [{"source": "MED", "id": f"{page}-{i}", "title": f"Paper {page}-{i}",
                    "abstractText": "a" * 400, "pmid": f"{page}{i}"} for i in range(2)]
        body = json.dumps({"hitCount": 4, "resultList": {"result": results},
                           "nextCursorMark": f"cur{page}" if page < p["pages"] else "END"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


# ═══════════════════════════════════════════════════════════════
# 假的 llama-server
# ═══════════════════════════════════════════════════════════════

class FakeLLM(BaseHTTPRequestHandler):
    reply = {"v": "{}"}
    reject = set()       # "json" / "think" / "boom"
    seen = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeLLM.seen.append(body)
        bad = (("response_format" in body and "json" in FakeLLM.reject)
               or ("chat_template_kwargs" in body and "think" in FakeLLM.reject))
        if bad:
            self.send_response(400); self.end_headers(); self.wfile.write(b'{}'); return
        if "boom" in FakeLLM.reject:
            self.send_response(500); self.end_headers(); self.wfile.write(b'{}'); return
        out = json.dumps({"choices": [{"message": {"content": FakeLLM.reply["v"]}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


class FakeWriterAPI(BaseHTTPRequestHandler):
    """同时假扮 Anthropic 和 OpenAI 两种协议 —— 靠认证头区分。"""
    seen = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        h = {k.lower(): v for k, v in self.headers.items()}
        FakeWriterAPI.seen.append({"headers": h, "body": body})
        if "x-api-key" in h:
            out = json.dumps({"content": [{"type": "text", "text": "# 稿子标题\n正文"}]})
        else:
            out = json.dumps({"choices": [{"message": {"content": "# 稿子标题\n正文"}}]})
        out = out.encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


def serve(handler, port):
    srv = HTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


# ═══════════════════════════════════════════════════════════════
# 取材
# ═══════════════════════════════════════════════════════════════

def test_normalize():
    section("fetch · 字段规范化")
    r = fp.normalize({}, "t")
    check("空记录不抛异常", r["title"] == "" and r["uid"] == "")

    r = fp.normalize({"source": "MED", "id": "123", "title": "A study.",
                      "abstractText": " abs ", "pmid": "999", "doi": "10.1/x",
                      "citedByCount": 3, "isOpenAccess": "Y",
                      "journalInfo": {"journal": {"title": "Gut"}},
                      "firstPublicationDate": "2026-09-01"}, "gut")
    check("uid / 去尾点 / 期刊 / OA / 引用数", r["uid"] == "MED:123" and r["title"] == "A study"
          and r["journal"] == "Gut" and r["is_open_access"] and r["cited_by"] == 3, r)
    check("有 pmid 时链到 PubMed", r["link"].endswith("/999/"), r["link"])

    r = fp.normalize({"source": "PPR", "id": "P1", "title": "Pre", "doi": "10.1101/y"}, "t")
    check("预印本标记 + doi 链接", r["is_preprint"] and r["link"] == "https://doi.org/10.1101/y")
    r = fp.normalize({"source": "PPR", "id": "P7", "title": "No ids"}, "t")
    check("无 pmid/doi 回落 europepmc", r["link"] == "https://europepmc.org/article/PPR/P7", r["link"])


def test_clean_text():
    section("fetch · HTML 清洗")
    cases = [
        ("&lt;i&gt;Enterocloster&lt;/i&gt; species", "Enterocloster species", "双重转义标签"),
        ("<i>Lactobacillus</i> <sub>2</sub>", "Lactobacillus 2", "裸标签"),
        ("Diet &amp; the gut", "Diet & the gut", "HTML 实体"),
        ("Smith & Jones study", "Smith & Jones study", "正常的 & 不被越解越乱"),
        ("  Too    many\n\nspaces  ", "Too many spaces", "空白折叠"),
        (None, "", "空值"),
    ]
    for raw, want, label in cases:
        check(f"clean_text · {label}", fp.clean_text(raw) == want, repr(fp.clean_text(raw)))


def test_filters_and_query():
    section("fetch · 查询式")
    check("LANG:eng 只约束 SRC:MED(否则预印本全被株连)",
          "(SRC:MED AND LANG:eng) OR SRC:PPR" in fp.FILTERS, fp.FILTERS)
    q = fp.build_query("TITLE:probiotic*", "2026-09-01", "2026-09-08")
    check("build_query 带日期区间", "FIRST_PDATE:[2026-09-01 TO 2026-09-08]" in q)
    check("build_query 带 FILTERS", "HAS_ABSTRACT:Y" in q)
    for name, topic_q in fp.TOPICS.items():
        f = " ".join(topic_q.split())
        check(f"{name} 括号/引号配平", f.count("(") == f.count(")") and f.count('"') % 2 == 0,
              f'{f.count("(")} 开 / {f.count(")")} 闭')


def test_dedup():
    section("fetch · 跨次去重")
    db = tempfile.mktemp(suffix=".sqlite3")
    conn = fp.open_db(db)
    rec = {"uid": "MED:1", "topic": "t", "title": "T", "pub_date": "2026-09-01"}
    check("首次未见过", not fp.is_seen(conn, "MED:1"))
    fp.mark_seen(conn, rec); conn.commit()
    check("标记后判重生效", fp.is_seen(conn, "MED:1"))
    fp.mark_seen(conn, rec); conn.commit()
    check("重复标记不抛异常", True)
    conn.close(); os.unlink(db)


def test_retry():
    section("fetch · 重试与降级")
    srv = serve(FakeEuropePMC, 8811)
    old_base, old_backoff, old_gap = fp.BASE_URL, fp.MAX_BACKOFF, fp.REQUEST_GAP
    fp.BASE_URL, fp.MAX_BACKOFF, fp.REQUEST_GAP = "http://127.0.0.1:8811/s", 1, 0

    def reset(**kw):
        FakeEuropePMC.calls.clear()
        FakeEuropePMC.plan.update({"fail_times": 0, "code": 503, "retry_after": None, "pages": 1})
        FakeEuropePMC.plan.update(kw)

    reset(fail_times=3)
    d = fp.http_get_json({"query": "x"})
    check("503 连挂 3 次后仍成功(重试上限 %d)" % fp.MAX_RETRIES,
          d.get("hitCount") == 4 and len(FakeEuropePMC.calls) == 4, len(FakeEuropePMC.calls))

    for code in (400, 404):
        reset(fail_times=99, code=code)
        try:
            fp.http_get_json({"query": "x"})
            check(f"{code} 应当抛出", False)
        except RuntimeError as e:
            check(f"{code} 立刻失败不重试", len(FakeEuropePMC.calls) == 1 and "不是网络问题" in str(e),
                  f"{len(FakeEuropePMC.calls)} 次请求")

    for code in sorted(fp.RETRYABLE):
        reset(fail_times=1, code=code)
        try:
            fp.http_get_json({"query": "x"})
            check(f"{code} 属于可重试", len(FakeEuropePMC.calls) == 2, len(FakeEuropePMC.calls))
        except RuntimeError:
            check(f"{code} 属于可重试", False, "被当成不可重试了")

    reset(fail_times=99)
    try:
        fp.http_get_json({"query": "x"}, retries=2)
        check("重试用尽应抛出", False)
    except RuntimeError as e:
        check("重试用尽抛 RuntimeError", "连续 2 次" in str(e))

    # 注意:这个测试里 MAX_BACKOFF 被临时压小了,所以期望值必须从常量推导。
    # (第一版这里写死了 7,结果 MAX_BACKOFF=1 时误报失败 —— 正是本文件开头
    #  警告的那种测试过期。)
    check("Retry-After 数字被采纳并受上限约束",
          fp.retry_after_seconds({"Retry-After": "7"}) == min(7, fp.MAX_BACKOFF),
          f"MAX_BACKOFF={fp.MAX_BACKOFF}")
    check("Retry-After 超上限被截断",
          fp.retry_after_seconds({"Retry-After": "99999"}) == fp.MAX_BACKOFF)
    check("Retry-After 缺失 / 垃圾值 / None 都返回 None",
          fp.retry_after_seconds({}) is None
          and fp.retry_after_seconds({"Retry-After": "soon"}) is None
          and fp.retry_after_seconds(None) is None)
    check("Retry-After 过去的日期归零",
          fp.retry_after_seconds({"Retry-After": "Wed, 21 Oct 2020 07:28:00 GMT"}) == 0.0)

    # 翻页中途失败,已取到的页要保住
    reset(pages=3)
    real, state = fp.http_get_json, {"n": 0}

    def flaky(params, retries=fp.MAX_RETRIES):
        state["n"] += 1
        if state["n"] == 3:
            raise RuntimeError("模拟第三页失败")
        return real(params, retries=retries)

    fp.http_get_json = flaky
    got = fp.search_topic("t", "TITLE:x", "2026-09-01", "2026-09-16")
    check("翻页中途失败保留前两页", len(got) == 4, len(got))

    fp.http_get_json = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("第一页就挂"))
    try:
        fp.search_topic("t", "TITLE:x", "2026-09-01", "2026-09-16")
        check("第一页失败应抛出交给 main 跳过", False)
    except RuntimeError:
        check("第一页失败抛出交给 main 跳过", True)

    fp.http_get_json = real
    fp.BASE_URL, fp.MAX_BACKOFF, fp.REQUEST_GAP = old_base, old_backoff, old_gap
    srv.shutdown()


def test_contact_env():
    section("fetch · 联系邮箱不硬编码")
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "fetch_papers.py"), encoding="utf-8").read()
    check("CONTACT_EMAIL 读环境变量", "os.environ.get(\"EUROPEPMC_CONTACT_EMAIL\"" in src)
    check("源码里没有真实邮箱", "@gmail.com" not in src and "@qq.com" not in src)


# ═══════════════════════════════════════════════════════════════
# 筛选
# ═══════════════════════════════════════════════════════════════

def test_extract_json():
    section("screen · 模型输出解析")
    good = '{"relevance":4,"evidence":3,"novelty":2,"hook":"h"}'
    cases = [
        (good, True, "干净 JSON"),
        ("```json\n" + good + "\n```", True, "代码块包裹"),
        ("好的,我的判断如下:\n" + good, True, "前面带废话"),
        ("<think>想想\n{\"fake\":1}\n</think>\n" + good, True, "带 think 块"),
        ('{"hook":"用 {剂量} 说事","relevance":5}', True, "字符串里有花括号"),
        ("这篇文章讲的是益生菌,建议写。", False, "答非所问"),
        ('{"relevance":4,"evidence":', False, "截断的 JSON"),
        ("", False, "空字符串"),
    ]
    for text, should, label in cases:
        check(f"extract_json · {label}", (sp.extract_json(text) is not None) == should)

    check("clamp 越界/脏值",
          (sp.clamp(9), sp.clamp(-3), sp.clamp("4"), sp.clamp("4.6"),
           sp.clamp(None), sp.clamp("abc")) == (5, 0, 4, 5, 0, 0))


def test_prefilter():
    section("screen · 规则粗筛")
    short = "a" * (sp.MIN_ABSTRACT_CHARS - 1)
    okabs = "a" * sp.MIN_ABSTRACT_CHARS
    kept, dropped = sp.prefilter([{"title": "X", "abstract": short},
                                  {"title": "Y", "abstract": okabs}])
    check(f"摘要 < {sp.MIN_ABSTRACT_CHARS} 字符被丢弃", len(kept) == 1 and len(dropped) == 1)

    same = "Probiotic supplementation and sleep quality in adults"
    kept, dropped = sp.prefilter([{"title": same, "abstract": okabs},
                                  {"title": same, "abstract": okabs}])
    check("同名记录(MED+PPR)归并", len(kept) == 1, [d[1] for d in dropped])

    long_a = "Effects of multi-strain probiotic supplementation on gut microbiota in healthy adults: a randomized trial"
    long_b = "Effects of multi-strain probiotic supplementation on gut microbiota in healthy adults: study protocol"
    kept, _ = sp.prefilter([{"title": long_a, "abstract": okabs},
                            {"title": long_b, "abstract": okabs}])
    check(f"长标题前 {sp.NEAR_DUP_PREFIX} 字符相同视为重复", len(kept) == 1)

    kept, _ = sp.prefilter([{"title": "", "abstract": okabs}, {"title": "", "abstract": okabs}])
    check("多条空标题不互相误杀", len(kept) == 2)


def test_chat_degradation():
    section("screen · 可选字段优雅降级")
    srv = serve(FakeLLM, 8812)
    ep = "http://127.0.0.1:8812/v1"
    FakeLLM.reply["v"] = '{"relevance":4,"evidence":3,"novelty":2,"hook":"h"}'

    import importlib
    for label, reject, want_calls in [("全支持", set(), 1),
                                      ("拒 json", {"json"}, 2),
                                      ("两个都拒", {"json", "think"}, 3)]:
        importlib.reload(sp)          # 清掉 _UNSUPPORTED 记忆
        FakeLLM.seen.clear(); FakeLLM.reject = set(reject)
        sp.chat(ep, "local", "给我 JSON")
        check(f"{label}:{want_calls} 次往返", len(FakeLLM.seen) == want_calls, len(FakeLLM.seen))
        body = FakeLLM.seen[-1]
        for k, v in sp.SAMPLING.items():
            check(f"{label}:采样参数 {k} 始终带上", body.get(k) == v, body.get(k))
        check(f"{label}:max_tokens 始终带上", body.get("max_tokens") == sp.MAX_TOKENS_SCORE,
              body.get("max_tokens"))

    importlib.reload(sp)
    FakeLLM.seen.clear(); FakeLLM.reject = {"json"}
    sp.chat(ep, "local", "x"); FakeLLM.seen.clear(); sp.chat(ep, "local", "y")
    check("记住了不支持,后续不再重试", len(FakeLLM.seen) == 1, len(FakeLLM.seen))

    importlib.reload(sp)
    FakeLLM.seen.clear(); FakeLLM.reject = {"boom"}
    try:
        sp.chat(ep, "local", "x")
        check("非 400 错误应抛出", False)
    except urllib.error.HTTPError as e:
        check("非 400 错误不被降级逻辑吞掉", e.code == 500 and len(FakeLLM.seen) == 1)

    rec = {"title": "T", "abstract": "a" * 500, "journal": "J", "is_preprint": False}
    out = sp.score_one(rec, ep, "local")
    check("服务端 500 时记 score_error 而不是崩", "score_error" in out)
    FakeLLM.reject = set()
    srv.shutdown()


def test_sampling_is_deterministic():
    section("screen · 采样参数适合打分任务")
    check("temperature 足够低(打分要可复现)", sp.SAMPLING["temperature"] <= 0.3,
          sp.SAMPLING["temperature"])
    check("presence_penalty 为 0(JSON 键名天生重复,惩罚它有害)",
          sp.SAMPLING["presence_penalty"] == 0, sp.SAMPLING["presence_penalty"])


def test_scoring():
    section("screen · 总分与折扣")
    srv = serve(FakeLLM, 8813)
    ep = "http://127.0.0.1:8813/v1"
    FakeLLM.reject = set()
    rec = {"title": "T", "abstract": "a" * 500, "journal": "J", "is_preprint": False,
           "link": "http://x", "pub_date": "2026-09-10"}

    def score(rel, act, ev, nov, hook="角度", **extra):
        d = {"relevance": rel, "actionability": act, "evidence": ev, "novelty": nov,
             "subject": "human", "topic_fit": "gut", "hook": hook, "reason": "r",
             "ingredient": "", "sourcing": 0}
        d.update(extra)
        FakeLLM.reply["v"] = json.dumps(d)
        return sp.score_one(rec, ep, "local")

    W = (sp.WEIGHT_RELEVANCE, sp.WEIGHT_ACTIONABILITY, sp.WEIGHT_EVIDENCE, sp.WEIGHT_NOVELTY)
    full = 5 * sum(W)
    o = score(5, 5, 5, 5)
    check(f"满分 = 5×(权重和) = {full:g}", o["total"] == full, o["total"])

    gate_ok = sp.RELEVANCE_GATE + 1
    o = score(gate_ok, 2, 2, 2)
    expect = gate_ok * W[0] + 2 * W[1] + 2 * W[2] + 2 * W[3]
    check("刚好过闸不打折", o["total"] == round(expect, 1) and not o["penalties"], o["total"])

    o = score(sp.RELEVANCE_GATE, 5, 5, 5)
    raw = sp.RELEVANCE_GATE * W[0] + 5 * W[1] + 5 * W[2] + 5 * W[3]
    check(f"rel ≤ {sp.RELEVANCE_GATE} 打 {sp.GATE_FACTOR} 折",
          o["total"] == round(raw * sp.GATE_FACTOR, 1) and o["raw_total"] == round(raw, 1), o["total"])

    o = score(gate_ok, 3, 3, 3, hook="")
    raw = gate_ok * W[0] + 3 * W[1] + 3 * W[2] + 3 * W[3]
    check(f"hook 为空乘 {sp.NO_HOOK_FACTOR}", o["total"] == round(raw * sp.NO_HOOK_FACTOR, 1), o["total"])

    o = score(sp.RELEVANCE_GATE, 3, 3, 3, hook="")
    check("两项折扣叠加,penalties 记两条", len(o["penalties"]) == 2, o["penalties"])

    FakeLLM.reply["v"] = '{"relevance":4,"evidence":2,"novelty":3,"hook":"h","reason":"r"}'
    o = sp.score_one(rec, ep, "local")
    check("模型漏字段时按 0 计,不崩", o["actionability"] == 0)

    check("输出带版本戳", o.get("scoring_version") == sp.SCORING_VERSION)
    check("内容打分不再产出 ingredient/sourcing(已拆成第二次调用)",
          "ingredient" not in o and "sourcing" not in o, sorted(o))

    # ── 第二次调用:采购视角 ──
    def radar(**kw):
        d = {"ingredient": "fucoidan", "sourcing": 4, "note": "n"}
        d.update(kw)
        FakeLLM.reply["v"] = json.dumps(d)
        return sp.source_one(rec, ep, "local")

    o = radar()
    check("source_one 抽出原料与采购分", o["ingredient"] == "fucoidan" and o["sourcing"] == 4)
    o = radar(ingredient="", sourcing=5)
    check("没抽出原料名 → 采购分强制归零", o["sourcing"] == 0, o["sourcing"])

    FakeLLM.reply["v"] = "这篇讲的是益生菌。"
    o = sp.source_one(rec, ep, "local")
    check("采购判断解析失败时不崩,给出 sourcing_error",
          o["sourcing"] == 0 and "sourcing_error" in o, o)
    srv.shutdown()


def test_truncation_detection():
    section("screen · 截断识别")
    cases = [
        ('{"relevance":4,"hook":"很长的中文角度', True, "字符串中途被砍"),
        ('{"relevance":4,"evidence":', True, "键值对中途被砍"),
        ('{"a":1,"b":{"c":2}', True, "嵌套未闭合"),
        ('{"relevance":4,"hook":"ok"}', False, "完整 JSON"),
        ("这篇文章讲的是益生菌,建议写。", False, "答非所问(不是截断)"),
        ("", False, "空字符串"),
        ('<think>想<\/think>{"a":1}', False, "think 块不干扰判断"),
    ]
    for text, want, label in cases:
        check(f"looks_truncated · {label}", sp.looks_truncated(text) == want,
              sp.looks_truncated(text))
    check("max_tokens 已设,且雷达那次更小",
          sp.MAX_TOKENS_SCORE > 0 and 0 < sp.MAX_TOKENS_RADAR < sp.MAX_TOKENS_SCORE,
          (sp.MAX_TOKENS_SCORE, sp.MAX_TOKENS_RADAR))


def test_topic_buckets():
    section("screen · 主题分桶")
    check("配额与标签的键一致", set(sp.TOPIC_QUOTA) == set(sp.TOPIC_LABELS),
          set(sp.TOPIC_QUOTA) ^ set(sp.TOPIC_LABELS))
    check("说明文字只挂在已知桶上", set(sp.TOPIC_NOTES) <= set(sp.TOPIC_LABELS))

    pool = 12
    base = {"abstract": "a" * 400, "journal": "J", "is_preprint": False, "link": "http://x",
            "pub_date": "2026-09-10", "relevance": 4, "actionability": 3, "evidence": 4,
            "novelty": 3, "reason": "r", "hook": "h", "penalties": [], "ingredient": "",
            "sourcing": 0, "subject": "human"}
    rows = [{**base, "title": f"{t}-{i}", "topic_fit": t, "total": 30 - i}
            for t in sp.TOPIC_QUOTA for i in range(pool)]

    for key, items, got_pool in sp.select_by_topic(rows):
        check(f"{key} 取 {sp.TOPIC_QUOTA[key]} 条", len(items) == sp.TOPIC_QUOTA[key], len(items))
        check(f"{key} 池子计数正确", got_pool == pool, got_pool)
        check(f"{key} 桶内按总分降序", [r["total"] for r in items] == sorted(
            (r["total"] for r in items), reverse=True))

    unknown = [{**base, "title": "weird", "topic_fit": "no_such_bucket", "total": 99}]
    buckets = dict((k, v) for k, v, _ in sp.select_by_topic(unknown))
    check("未知 topic_fit 落到 other,不丢失", len(buckets["other"]) == 1)

    md = tempfile.mktemp(suffix=".md")
    sp.write_markdown(md, rows, None, "t.jsonl", 0)
    body = open(md, encoding="utf-8").read()
    check("表头写明评分版本", sp.SCORING_VERSION in body)
    for key, label in sp.TOPIC_LABELS.items():
        check(f"渲染出 {key} 小节", f"## {label}" in body)
    for key, note in sp.TOPIC_NOTES.items():
        check(f"{key} 的说明文字已渲染", note[:12] in body)
    os.unlink(md)


def test_radar():
    section("screen · 原料雷达")
    base = {"abstract": "a", "journal": "J", "is_preprint": False, "link": "http://x",
            "pub_date": "2026-09-10", "relevance": 3, "actionability": 2, "novelty": 3,
            "reason": "理由", "total": 20}
    def mk(ing, srcg, ev, title):
        return {**base, "title": title, "ingredient": ing, "sourcing": srcg, "evidence": ev}

    hi = sp.RADAR_MIN_SOURCING + 1
    lo = sp.RADAR_MIN_SOURCING - 1
    rows = [mk("fucoidan", hi, 5, "Fucoidan RCT"),
            mk("Fucoidan", hi, 4, "fucoidan cohort"),     # 大小写不同
            mk("fucoidan.", sp.RADAR_MIN_SOURCING, 3, "fucoidan pilot"),   # 尾点
            mk("urolithin A", hi, 5, "Urolithin A RCT"),
            mk("astaxanthin", lo, 2, "Astaxanthin in mice"),   # 低于门槛
            mk("", 0, 5, "Mediterranean diet review")]         # 无原料

    path = tempfile.mktemp(suffix=".md")
    n = sp.write_radar(path, rows, "t.jsonl")
    body = open(path, encoding="utf-8").read()
    check("上榜原料 2 个", n == 2, n)
    check("大小写 + 尾标点归并成一个", sum(1 for l in body.split("\n")
                                        if l.lower().startswith("## fucoidan")) == 1)
    check("fucoidan 计 3 篇", "| 3 |" in body)
    check(f"采购分 < {sp.RADAR_MIN_SOURCING} 的不上榜", "astaxanthin" not in body.lower())
    check("无原料的不上榜", "Mediterranean" not in body)
    check("提醒法规需自行核实", "自己核" in body)
    os.unlink(path)

    empty = tempfile.mktemp(suffix=".md")
    check("没有合格原料时不崩", sp.write_radar(empty, [mk("", 0, 5, "x")], "t.jsonl") == 0)
    check("并给出调参提示", "RADAR_MIN_SOURCING" in open(empty, encoding="utf-8").read())
    os.unlink(empty)


def test_writer_providers():
    section("write · 三家 API 适配")
    srv = serve(FakeWriterAPI, 8821)
    for name, cfg in wd.PROVIDERS.items():
        check(f"{name} 配置齐全", all(k in cfg for k in ("env", "url", "model", "style")))
        check(f"{name} key 走环境变量", cfg["env"].endswith("_API_KEY"), cfg["env"])
    check("默认 provider 有效", wd.DEFAULT_PROVIDER in wd.PROVIDERS)
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "write_draft.py"), encoding="utf-8").read()
    check("源码里没有硬编码的 key", not re.search(r"sk-[A-Za-z0-9]{8}", src))

    for name in wd.PROVIDERS:
        FakeWriterAPI.seen.clear()
        real = wd.PROVIDERS[name]
        wd.PROVIDERS[name] = {**real, "url": "http://127.0.0.1:8821/v1"}
        os.environ[real["env"]] = "test-key-123"
        text = wd.call_api(name, real["model"], "写点东西")
        h = FakeWriterAPI.seen[0]["headers"]
        if real["style"] == "anthropic":
            check(f"{name} 用 x-api-key 认证", h.get("x-api-key") == "test-key-123")
            check(f"{name} 带 anthropic-version", "anthropic-version" in h)
        else:
            check(f"{name} 用 Bearer 认证", h.get("authorization") == "Bearer test-key-123")
        check(f"{name} 能解出正文", text == "# 稿子标题\n正文", text[:30])
        check(f"{name} 带 max_tokens",
              FakeWriterAPI.seen[0]["body"].get("max_tokens") == wd.MAX_TOKENS)
        wd.PROVIDERS[name] = real

    os.environ.pop("DEEPSEEK_API_KEY", None)
    try:
        wd.call_api("deepseek", "x", "y")
        check("缺 key 应当退出", False)
    except SystemExit as e:
        check("缺 key 给 setx 提示而不是抛异常",
              "setx" in str(e) and "DEEPSEEK_API_KEY" in str(e))
    srv.shutdown()


def test_writer_retry():
    section("write · 重试与错误提示")
    plan = {"fail": 0, "code": 503, "retry_after": None}
    calls = []

    class Flaky(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            calls.append(1)
            if plan["fail"] > 0:
                plan["fail"] -= 1
                self.send_response(plan["code"])
                if plan["retry_after"]:
                    self.send_header("Retry-After", str(plan["retry_after"]))
                self.end_headers()
                self.wfile.write(b'{"error":{"message":"busy"}}')
                return
            out = json.dumps({"choices": [{"message": {"content": "正文"}}]}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

    srv = serve(Flaky, 8822)
    real = wd.PROVIDERS["openai"]
    wd.PROVIDERS["openai"] = {**real, "url": "http://127.0.0.1:8822/v1"}
    os.environ["OPENAI_API_KEY"] = "k"
    old_backoff = wd.MAX_BACKOFF
    wd.MAX_BACKOFF = 1

    def reset(**kw):
        calls.clear()
        plan.update({"fail": 0, "code": 503, "retry_after": None})
        plan.update(kw)

    reset(fail=2)
    check("503 连挂两次后仍成功", wd.call_api("openai", "m", "p") == "正文" and len(calls) == 3,
          len(calls))

    reset(fail=1, code=429)
    wd.call_api("openai", "m", "p")
    check("429(限流)会重试", len(calls) == 2, len(calls))

    for code in sorted(wd.RETRYABLE):
        reset(fail=1, code=code)
        try:
            wd.call_api("openai", "m", "p")
            check(f"{code} 属于可重试", len(calls) == 2, len(calls))
        except SystemExit:
            check(f"{code} 属于可重试", False, "被当成致命错误")

    for code, frag in ((401, "认证失败"), (400, "模型名"), (404, "拼写"), (403, "没权限")):
        reset(fail=99, code=code)
        try:
            wd.call_api("openai", "m", "p")
            check(f"{code} 应当立刻失败", False)
        except SystemExit as e:
            check(f"{code} 不重试且给出可读提示",
                  len(calls) == 1 and frag in str(e), f"{len(calls)} 次 / {str(e)[:40]}")

    reset(fail=99, code=503)
    try:
        wd.call_api("openai", "m", "p")
        check("重试用尽应当退出", False)
    except SystemExit as e:
        check("重试用尽给出换家建议",
              "--provider" in str(e) and str(wd.MAX_RETRIES) in str(e), str(e)[:60])

    check("Retry-After 数字受上限约束",
          wd.retry_after_seconds({"Retry-After": "7"}) == min(7, wd.MAX_BACKOFF))
    check("Retry-After 缺失/垃圾值返回 None",
          wd.retry_after_seconds({}) is None
          and wd.retry_after_seconds({"Retry-After": "soon"}) is None)

    wd.MAX_BACKOFF = old_backoff
    wd.PROVIDERS["openai"] = real
    srv.shutdown()


def test_writer_prompts():
    section("write · 提示词组装")
    rec = {"title": "Effects of X on Y", "journal": "Gut", "pub_date": "2026-09-10",
           "subject": "animal", "evidence": 2, "hook": "一个角度",
           "abstract": "ABSTRACT-BODY", "link": "http://x", "is_preprint": True}
    for tpl, label in ((wd.WECHAT_PROMPT, "公众号"), (wd.XHS_PROMPT, "小红书")):
        p = wd.build_prompt(tpl, rec)
        check(f"{label}:标题与摘要已注入", "Effects of X on Y" in p and "ABSTRACT-BODY" in p)
        check(f"{label}:占位符全部替换", not re.findall(r"\{(\w+)\}", p),
              re.findall(r"\{(\w+)\}", p))
        check(f"{label}:证据档位说明与分数对应",
              wd.EVIDENCE_NOTE[rec["evidence"]][:6] in p)
        check(f"{label}:预印本被标出", "尚未经过同行评审" in p)
        for rule, word in (("如实说证据强度", "如实说"), ("宣传红线", "治愈"),
                           ("不编造", "不编造"), ("术语翻译", "luteolin"),
                           ("菌株可追溯而非优越", "没被比下去不等于更强"),
                           ("单态亚种反例", "99.975%")):
            check(f"{label}:硬规矩「{rule}」在", word in p)

    check("公众号版比小红书版长", "1200-2000" in wd.WECHAT_PROMPT and "400-700" in wd.XHS_PROMPT)
    check("小红书版要求话题标签", "话题标签" in wd.XHS_PROMPT)
    check("公众号版要求附原文链接", "原文:" in wd.WECHAT_PROMPT)

    r2 = {**rec, "hook": "", "is_preprint": False}
    p = wd.build_prompt(wd.XHS_PROMPT, r2)
    check("没有 hook 时给兜底说明", "你自己找一个" in p)
    check("非预印本不出现预印本警告", "尚未经过同行评审" not in p)

    for ev in range(6):
        p = wd.build_prompt(wd.XHS_PROMPT, {**rec, "evidence": ev})
        check(f"证据 {ev} 有对应的档位说明", wd.EVIDENCE_NOTE[ev][:6] in p)

    check("slug 去特殊字符", wd.slugify("Effects of <i>X</i>: a trial!") == "Effects-of-iXi-a-trial",
          wd.slugify("Effects of <i>X</i>: a trial!"))
    check("slug 兜底不为空", wd.slugify("!!!") == "draft")
    check("slug 限长", len(wd.slugify("a" * 200)) <= 40)


def test_prompt_pack():
    section("write · 提示词包")
    rec = {"title": "Effects of X on Y", "journal": "Gut", "pub_date": "2026-09-10",
           "subject": "review", "evidence": 5, "hook": "一个角度",
           "abstract": "We propose a new framework.", "link": "http://x",
           "is_preprint": False, "scoring_version": "vTest"}
    pack = wd.build_prompt_pack(rec)

    check("分 3 段", pack.count("## 第") == 3, pack.count("## 第"))
    check("开头交代了用法", "一段出完结果再粘下一段" in pack)
    check("警告别一次全粘", "不要把整个文件一次性粘进去" in pack)
    check("带原文链接", "http://x" in pack)
    check("带评分版本", "vTest" in pack)
    check("摘要已注入", "We propose a new framework." in pack)
    check("review 类型警告还在", "we propose" in pack)
    check("结尾有自检清单", "最后自己过一遍" in pack)
    check("占位符全部替换", not re.findall(r"\{\w+\}", pack), re.findall(r"\{\w+\}", pack))

    check("第 1 段是中文公众号稿", "公众号版" in pack and "1200-2000 字" in pack)
    check("第 2 段是英文行业稿", "英文行业资讯稿" in pack and "350-550 words" in pack)
    check("第 3 段是首图内容", "公众号首图" in pack and '"kicker"' in pack)
    check("不再产出小红书卡片组", "小红书" not in pack.split("## 第 3 段")[0])

    # 英文稿的合规约束 —— 这几条是它跟中文稿最大的区别
    en = pack.split("## 第 2 段")[1].split("## 第 3 段")[0]
    check("英文稿:禁止跟自家产品挂钩", "Never connect the finding to Newbio" in en)
    check("英文稿:点明官网内容=广告标准", "advertising claim" in en)
    check("英文稿:禁止健康宣称", "No health claims" in en)
    check("英文稿:要求明示证据等级", "State the evidence grade" in en)
    check("英文稿:沿用作者的 hedging 措辞",
          "own hedging language" in en and "warrants" in en)
    check("英文稿:菌株是可追溯不是排名", "traceability, not a ranking" in en)
    check("英文稿:不许编数字", "Never invent numbers" in en)
    check("英文稿:保留 luteolin 那次教训", "luteolin" in en)
    check("英文稿:明说不是翻译", "not a translation" in en)
    check("英文稿:要求附引用", "End with the citation" in en)

    cov = pack.split("## 第 3 段")[1]
    check("首图:要 JSON 不要画图", "不要画图" in cov and "不要写 HTML" in cov)
    check("首图:解释了为什么不用生成图", "写不准中文" in cov)
    check("首图:限制标题长度", "18 字以内" in cov)
    check("首图:禁止做题党", "不许做题党" in cov)

    r2 = {**rec, "subject": "animal", "evidence": 2}
    check("动物实验的类型警告会跟着进提示词包",
          "必须写明是在动物身上做的" in wd.build_prompt_pack(r2))

    r3 = {**rec, "subject": "comparative_genomics",
          "subject_note": ["自定义提醒第一行", "第二行"]}
    blk = wd.evidence_block(r3)
    check("subject_note 覆盖生效", "自定义提醒第一行" in blk and "第二行" in blk, blk)
    check("覆盖后不再套用 review 的警告", "we propose" not in blk)
    check("没有 subject_note 时仍走查表",
          "we propose" in wd.evidence_block({**rec, "subject": "review"}))
    r5 = {**rec, "evidence": 4, "evidence_note": "比较基因组学,不是临床试验"}
    check("evidence_note 覆盖证据标签",
          "比较基因组学,不是临床试验" in wd.evidence_block(r5)
          and "人体队列研究" not in wd.evidence_block(r5))
    check("没有 evidence_note 时仍走查表",
          "人体队列研究" in wd.evidence_block({**rec, "evidence": 4}))

    import glob as _glob
    for f in _glob.glob(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                     "manual", "*.json")):
        with open(f, encoding="utf-8") as fh:
            m = json.load(fh)
        name = os.path.basename(f)
        check(f"{name} 必填字段齐全",
              all(m.get(k) for k in ("title", "abstract", "hook")))
        if not name.startswith("_"):
            pk = wd.build_prompt_pack(m)
            check(f"{name} 能生成完整提示词包",
                  pk.count("## 第") == 3 and not re.findall(r"\{\w+\}", pk))


def test_cards():
    section("cards · 小红书卡片渲染")
    import make_cards as mc
    spec = {
        "kicker": "分类", "title": "标题第一行\n第二行",
        "subtitle": "副标题带 **重点**",
        "points": [{"tag": "标签A", "heading": "要点一", "body": "正文 **强调** 内容"},
                   {"tag": "标签B", "heading": "要点二", "body": "第二条"}],
        "caveat": "局限说明\n\n第二段",
        "source": {"title": "Paper Title", "journal": "J", "date": "2026-01-01",
                   "link": "pubmed.ncbi.nlm.nih.gov/1/"},
        "tags": ["标签1", "标签2"],
    }
    h = mc.render(spec)
    n_cards = h.count('class="card"')
    check("卡片数 = 封面 + 要点 + 局限 + 出处", n_cards == 1 + 2 + 1 + 1, n_cards)
    check("尺寸是小红书 3:4", f"width:{mc.CARD_W}px" in h and f"height:{mc.CARD_H}px" in h)
    check("3:4 比例正确", abs(mc.CARD_H / mc.CARD_W - 4 / 3) < 0.01)
    check("** 渲染成强调", '<span class="em">重点</span>' in h)
    check("\\n 渲染成换行", "标题第一行<br>第二行" in h)
    check("\\n\\n 渲染成空行", "局限说明<br><br>第二段" in h)
    check("文献出处进了卡片", "Paper Title" in h and "pubmed.ncbi.nlm.nih.gov/1/" in h)
    check("标签进了卡片", "#标签1" in h and "#标签2" in h)
    check("页码是 N / 总数", "1 / 5" in h and "5 / 5" in h)
    check("CSS 内联,不引外部资源",
          "<style>" in h and "http" not in h.split("<body>")[0])
    check("用系统中文字体栈,不引网络字体", "PingFang SC" in h and "fonts.googleapis" not in h)
    check("HTML 转义防止内容破坏结构",
          "&lt;script&gt;" in mc.render({**spec, "title": "<script>x</script>"}))
    check("没有 caveat 时少一张卡",
          mc.render({k: v for k, v in spec.items() if k != "caveat"}).count('class="card"') == 4)
    check("给了截图方法的提示", "截图方法" in h)

    cov = mc.render_cover(spec)
    check("首图比例 2.35:1", abs(mc.COVER_W / mc.COVER_H - 2.35) < 0.02,
          f"{mc.COVER_W}x{mc.COVER_H}")
    check("首图只有一张", cov.count('class="cover"') == 1)
    check("首图不含卡片结构", 'class="card"' not in cov)
    check("首图渲染标题与副标题", "标题第一行<br>第二行" in cov and "副标题带" in cov)
    check("首图的 ** 也渲染成强调", '<span class="em">重点</span>' in cov)
    check("首图尺寸写进 CSS", f"width:{mc.COVER_W}px" in cov)
    check("首图和卡片是两套尺寸", mc.COVER_W != mc.CARD_W and mc.COVER_H != mc.CARD_H)


def test_prompt_contract():
    section("screen · 提示词契约")
    for f in ("relevance", "actionability", "evidence", "novelty",
              "hook", "reason", "topic_fit", "subject"):
        check(f"内容 PROMPT 声明了 {f} 字段", f'"{f}"' in sp.PROMPT)
    for f in ("ingredient", "sourcing", "note"):
        check(f"RADAR_PROMPT 声明了 {f} 字段", f'"{f}"' in sp.RADAR_PROMPT)
    check("内容提示词不再要求输出 sourcing 字段",
          '"sourcing": 0-5' not in sp.PROMPT)
    check("雷达提示词不含读者画像(它只干一件事)",
          sp.AUDIENCE[:20] not in sp.RADAR_PROMPT)
    check("雷达提示词要求原料名不带修饰词", "不带用途和修饰" in sp.RADAR_PROMPT)
    check("两次调用互不干涉的说明还在",
          "没有任何关系" in sp.RADAR_PROMPT and "跟这次无关" in sp.PROMPT)
    for key in sp.TOPIC_QUOTA:
        check(f"PROMPT 提到分类 {key}", f'"{key}"' in sp.PROMPT)
    check("术语翻译纪律仍在(luteolin 事故)", "luteolin" in sp.PROMPT)
    check("hook 留空是有效信号", "留空" in sp.PROMPT)
    check("relevance 六档锚点齐全", all(f"  {i} = " in sp.PROMPT for i in range(6)))
    check("证据减分只针对说过头,不针对观察性设计",
          "associated with" in sp.PROMPT and "不扣分" in sp.PROMPT)


# ═══════════════════════════════════════════════════════════════

def main():
    for t in (test_normalize, test_clean_text, test_filters_and_query, test_dedup,
              test_retry, test_contact_env, test_extract_json, test_prefilter,
              test_chat_degradation, test_sampling_is_deterministic, test_scoring,
              test_truncation_detection,
              test_topic_buckets, test_radar, test_prompt_contract,
              test_writer_providers, test_writer_retry, test_writer_prompts,
              test_prompt_pack, test_cards):
        t()
    total = PASS + FAIL
    print(f"\n{'='*56}")
    print(f"评分口径 {sp.SCORING_VERSION}　抓取主题 {len(fp.TOPICS)} 个")
    print(f"{PASS}/{total} 通过" + (f"，{FAIL} 失败" if FAIL else "，全部通过"))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
