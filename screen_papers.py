#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
screen_papers.py — Newbio Health 内容流水线 · 第二段:筛选

读 fetch_papers 输出的 JSONL,先用规则粗筛,再让本地模型逐条读摘要打分,
最后输出一份可以两分钟扫完的选题清单。

只用标准库。需要一个 OpenAI 兼容的本地接口:
    llama.cpp   llama-server --jinja    → http://localhost:8080/v1   ← 默认
    Ollama      ollama serve            → http://localhost:11434/v1
    LM Studio   开 Local Server         → http://localhost:1234/v1

用法(Windows 用 py,macOS/Linux 用 python3):
    py screen_papers.py --selftest                  # 验证本地模型接口
    py screen_papers.py                             # 处理 out/ 里最新的一份
    py screen_papers.py --input out/xxx.jsonl --top 15
    py screen_papers.py --workers 3
    py screen_papers.py --endpoint http://localhost:11434/v1 --model qwen3:14b

输出:
    screened/YYYY-MM-DD_HHMM.jsonl   全部条目 + 评分
    screened/YYYY-MM-DD_HHMM.md      排序后的选题清单,给人看的
"""

import argparse
import concurrent.futures as cf
import datetime as dt
import glob
import json
import os
import re
import sys
import threading
import urllib.error
import urllib.request

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

# ─────────────────────────────────────────────────────────────
# 配置
# ─────────────────────────────────────────────────────────────

# 默认连 llama.cpp 的 llama-server。启动时必须带 --jinja,否则关思考的参数不生效。
# 想回 Ollama:--endpoint http://localhost:11434/v1 --model qwen3:14b
DEFAULT_ENDPOINT = "http://localhost:8080/v1"
DEFAULT_MODEL = "local"        # llama-server 忽略这个字段;Ollama 必须填真实模型名
TIMEOUT = 180

# 采样参数。这是打分任务,不是写作任务 —— 要的是可复现,不是多样性。
#
# 教训:上一版照搬了 Qwen3.6 官方推荐的通用参数(temperature=0.7,
# presence_penalty=1.5),结果同一份输入跑两次排序就不一样。两个错:
#   · 0.7 对分类/打分太高。你没法信任一个每次都换一批的选题清单。
#   · presence_penalty 惩罚重复 token,而 JSON 的键名天生重复 —— 用在
#     结构化输出上是在轻微破坏格式,纯属有害无益。
# 分数挤成一团的问题真正的根源在总分公式和评分口径,不在温度,见下面 WEIGHTS。
SAMPLING = {
    "temperature": 0.2,
    "top_p": 0.80,
    "top_k": 20,
    "presence_penalty": 0.0,
}

# 关掉思考模式。Qwen3.6 默认是开的,每条摘要都会先吐几百个 <think> token。
# extract_json 能正确剥掉,不影响结果,但 150 条累起来纯属白烧时间。
DISABLE_THINKING = {"chat_template_kwargs": {"enable_thinking": False}}

# 让服务端保证输出是合法 JSON。llama.cpp 会把它转成 GBNF 语法约束,从源头
# 消灭「模型输出无法解析为 JSON」那一类失败,比事后拿正则抠可靠得多。
JSON_MODE = {"response_format": {"type": "json_object"}}

MIN_ABSTRACT_CHARS = 300      # 摘要太短的没法判断,直接丢
NEAR_DUP_PREFIX = 60          # 标题前 N 个字符相同视为重复(同一研究的多个版本)

# ── 总分怎么算 ───────────────────────────────────────────
# 旧公式 rel×2 + ev + nov 有个硬伤:rel3/ev5/nov2 和 rel4/ev2/nov3 都是 13 分,
# 「读者不关心但证据强」和「读者关心但证据弱」打平。对科普账号这是反的 ——
# 一篇读者不关心的文章,证据再硬也不该占选题位。所以:
#   · relevance 权重提到 3,让它真正主导排序
#   · relevance ≤ 2 视为不及格,总分打三折(不判 0,保留可排序性)
#   · 模型自己没写出切入角度 = 它也想不出怎么写,总分减半
# 觉得筛得太狠/太松,先动这四个数,不用改代码逻辑。
# 第二轮教训:光提 relevance 的权重不够。加了「对不上最高给 2」的硬规定之后,
# 模型把 relevance 用成了二元开关 —— 391 条里 rel5 一次没出现过,过闸的全是
# 3 或 4,于是 11 条并列 17 分。刻度上半段是空的,排序就在顶部塌掉了。
# 两个修法一起上:relevance 0-5 每一档都写明锚点(见 PROMPT),
# 外加第四个维度 actionability —— 「读者看完能做什么」。
# 那 11 条并列里,「宠物粮怎么挑怎么存」和「发现一个基因突变」真正的差别
# 在可操作性上,relevance 分不开它们。
# 每改一次评分口径就把这个号往上加。它会写进 .md 表头和 jsonl 每一行,
# 这样任何时候拿出一份旧清单,都能立刻知道它是哪套标准打出来的。
SCORING_VERSION = "v8-feed-bucket"

WEIGHT_RELEVANCE = 3.0
WEIGHT_ACTIONABILITY = 2.0
WEIGHT_EVIDENCE = 2.0         # v3:1.0 → 2.0。宁可少写一篇,不要写一篇站不住的
WEIGHT_NOVELTY = 1.0          # 满分 = 5*3 + 5*2 + 5*2 + 5 = 40
# v4:调回 2。v3 把闸门提到 3 确实压住了并列,但副作用是过闸的 25 条里
# 宠物占 16 条、人体研究只剩 3 条 —— 因为 rel4/5 的锚点("买得到、吃得到")
# 天然偏向宠物商品类论文,人体膳食研究总是差一档。
# 根子在于:让模型拿一把尺子比较「狗粮油脂氧化」和「地中海饮食与认知衰退」,
# 这个要求本身就没定义好。v4 改成闸门放松 + 主题内部排序 + 各取前 N,
# 跨主题的可比性问题就不用解决了 —— 绕过去。
RELEVANCE_GATE = 2            # rel ≤ 这个值算不及格
GATE_FACTOR = 0.3             # 不及格的乘数
NO_HOOK_FACTOR = 0.5          # hook 为空的乘数

# 每期清单里各主题各列几条。这是编辑决定,不是技术决定 —— 想让某个方向
# 多写点就把数字调大。各主题只跟自己人比,不跨主题抢名额。
# 砍掉 lifestyle 和 food_safety 之后,名额还给主线三个方向。
# feed 是单独一档:它不是写稿用的,是看行业动态用的(饲料益生菌是主营业务)。
TOPIC_QUOTA = {
    "gut": 8,
    "supplement": 6,
    "pet": 6,
    "feed": 3,
    "other": 1,
}
# ── 原料雷达 ─────────────────────────────────────────────
# 这是跟"选题清单"完全独立的第二个视角:选题清单服务于写稿,
# 雷达服务于进口生意。同一篇论文在两边的价值经常是相反的 ——
# 某个新藻类提取物的人体试验,读者用不上(选题价值低),
# 但对采购可能极有价值。所以雷达不走 relevance 那道闸。
RADAR_MIN_SOURCING = 3        # 低于这个分不上雷达。3 = 至少有人体数据
RADAR_MAX_INGREDIENTS = 25    # 雷达最多列几个原料

TOPIC_LABELS = {
    "gut": "肠道健康 / 益生菌",
    "supplement": "膳食补充剂 / 功能性原料",
    "pet": "宠物营养",
    "feed": "饲料 / 养殖(行业动态,非选题)",
    "other": "其它",
}

# 某些桶需要一句话说明它是干什么的,渲染在小标题下面。
TOPIC_NOTES = {
    "feed": "这一节不是选题,是行业情报 —— 养殖端的饲料添加剂动态。"
            "评分口径是按科普选题打的,所以这里的分数只能横向比较,别跟上面几节比。",
    "other": "分类不明,或属于这个账号不写的方向(运动、睡眠、食品安全事件)。",
}

HERE = os.path.dirname(os.path.abspath(__file__))
IN_DIR = os.path.join(HERE, "out")
OUT_DIR = os.path.join(HERE, "screened")

# 读者画像写死在这里 —— 这是整个筛选质量的核心,想调方向就改这段。
AUDIENCE = """你在为一个中文健康科普账号(Newbio Health)挑选题。
读者是中国的普通消费者,关心自己和家人的健康、饮食、肠道、益生菌,
以及自家猫狗的营养。他们不是研究人员,看不懂统计学,但不傻,讨厌被吓唬,
喜欢"所以我到底该怎么做"这种能落地的结论。
账号背后是一家做饲料益生菌和宠物营养原料的公司,所以益生菌、肠道菌群、
宠物营养这几个方向价值更高。"""

PROMPT = """{audience}

下面是一篇新发表文献的标题和摘要。请判断它值不值得写成一篇科普文章。

标题:{title}
期刊:{journal}
类型:{kind}
摘要:{abstract}

只输出一个 JSON 对象,不要有任何其它文字、不要用代码块包裹:
{{
  "relevance": 0-5,
  "actionability": 0-5,
  "evidence": 0-5,
  "novelty": 0-5,
  "subject": "human" 或 "animal" 或 "in_vitro" 或 "review",
  "topic_fit": "gut" 或 "supplement" 或 "pet" 或 "feed" 或 "other",
  "ingredient": "文中的核心功能性原料,英文原词照抄不要翻译;没有明确单一原料就填空字符串",
  "sourcing": 0-5,
  "hook": "如果要写,一句话中文选题角度;不值得写就填空字符串",
  "reason": "一句话中文理由,20字以内"
}}

评分口径 —— 四个维度都要用满 0 到 5,不要只在两三档之间跳:

relevance(跟读者有多大关系)
  5 = 说的就是读者自己的日常:他们买得到的食物或产品、吃得到的东西、
      自家猫狗天天在经历的事
  4 = 明确相关,但隔了一层 —— 比如结论对,但要读者自己换算到生活里
  3 = 沾边:主题对得上,可研究对象或场景跟读者有距离
      (国外特定人群、住院病人、生产线场景)
  2 = 只有关键词对得上。都叫"肠道菌群",实质是两回事
  1 = 基本无关:罕见病、野生动物、纯工业工艺、纯监测数据
  0 = 完全无关

actionability(读者看完能做什么)
  5 = 能立刻改一个具体行为:换哪种粮、怎么存、认准哪个菌株、几点吃
  4 = 给出了明确方向,细节要读者自己补
  3 = 改变判断标准或选购思路,但没有具体动作
  2 = 只能算"知道了一件事",行为上无从下手
  1 = 连"知道了"都勉强,纯学术增量
  0 = 对读者毫无可操作内容
  注意:这一项跟 relevance 是两回事。一个基因突变的发现可以很相关
  (养猫人确实关心),但可操作性很低 —— 读者没法对基因做任何事。

evidence(证据强度)
  先按研究类型定基准分:meta分析/RCT=5,人体队列=4,人体小样本=3,动物=2,体外=1

  然后只在**结论的措辞超出了设计能支持的范围**时才扣分:
  · 单臂/无对照却宣称"证实有效",扣 2。
  · 几个菌株(或几个样本)的相关性被说成"找到了决定因素"、"揭示了机制",
    却没有敲除/回补一类的验证实验,扣 1-2。
  · 药物诱导的动物造模,结论却直接外推到人的同名疾病,扣 1。
    洛哌丁胺灌出来的小鼠便秘,跟人的功能性便秘不是一回事。

  **关键区分,不要搞错:**
  一篇观察性队列研究,本来就只能得出相关性。它在摘要里老实写
  "associated with"、"may be linked to",那是恰当的谨慎,**不扣分**,
  基准分 4 分照给。只有当设计撑不起结论的口气时才扣 ——
  扣的是"说过头",不是"设计本身是观察性的"。
  同理,样本量小已经体现在基准分里了(人体小样本=3),不要再扣一次。

  扣完最低给 0。宁可少推一篇,不要推一篇站不住的。

novelty(是不是说了新东西)
  又一篇"益生菌可能有益"的综述=1;推翻常识或给出反直觉结论=5

topic_fit 的边界(容易混,说清楚):
  gut        = 核心是肠道菌群、益生菌本身(人)
  supplement = 有一个明确的、能买到的东西:某个原料、提取物、菌株、营养素(人)
  pet        = 猫、狗等伴侣动物
  feed       = 经济动物的饲料与添加剂:肉鸡、蛋鸡、猪、牛、水产。
               **跟 pet 的分界是养殖端 vs 伴侣动物,不是"动物 vs 人"。**
               一篇仔猪断奶期益生菌的研究是 feed,不是 pet。
  other      = 以上都不是。**纯靠行为达成的(运动、睡眠、冥想、断食)、
               以及食品安全/食物中毒事件,都归 other** —— 这两类这个账号不写,
               归到 other 让它们自然沉底就行,不用为它们纠结分数。

sourcing(作为"可进口到中国销售的功能性原料"的价值)
  **这一项跟 relevance / actionability 完全独立,不要互相看齐。**
  一篇普通读者用不上的论文(某个新藻类提取物的人体试验)对采购可能极有价值;
  一篇"多吃蔬菜有益健康"的高质量综述对采购毫无价值 —— 因为没有可采购的东西。

  先问:文章里有没有一个明确的、能向供应商下单的单一原料?
  没有(谈的是膳食模式、饮食结构、运动、行为、或笼统的"菌群")→ 直接给 0,
  后面不用看了,ingredient 也留空。

  有的话,按**人体证据强度**打分 —— 宁可晚一步,不要拿动物数据去跟客户谈:
  5 = 人体 RCT 或 meta 分析支持,且这个原料在中国市场还不常见
  4 = 人体 RCT 或 meta 分析支持,国内已有但还没做烂
  3 = 有人体数据,但样本小、或只是观察性关联
  2 = 只有动物或体外数据。有意思,但拿这个去谈生意还太早
  1 = 已经是大路货(维生素C、常见乳酸菌、普通蛋白粉),没有信息增量

  ingredient 字段务必**照抄英文原词**(Lactobacillus plantarum P-8、
  fucoidan、urolithin A、ergothioneine…)。这个名字是要拿去搜供应商的,
  翻译成中文就废了。

两条写作纪律:
1. **专业名词拿不准中文译名就直接保留英文原词。** 化合物名、菌株名、酶名尤其如此。
   猜错一个字就是事故 —— 比如 luteolin 是木犀草素,不是叶黄素(lutein),
   两个完全不同的东西。不确定就写 "luteolin",让人去查,不要编。
2. **hook 只在你真的想到一个能写的角度时才填。** 想不出就留空字符串。
   不要为了填满字段而硬凑一个你自己都不信的角度 —— 留空是有用的信号。
"""


# ─────────────────────────────────────────────────────────────
# 规则粗筛
# ─────────────────────────────────────────────────────────────

def prefilter(records):
    """不花模型算力就能排除的:没摘要、摘要太短、标题近重复。"""
    kept, dropped, seen_prefix = [], [], set()
    for r in records:
        abstract = (r.get("abstract") or "").strip()
        if len(abstract) < MIN_ABSTRACT_CHARS:
            dropped.append((r, f"摘要过短({len(abstract)}字符)"))
            continue
        key = re.sub(r"\W+", "", (r.get("title") or "").lower())[:NEAR_DUP_PREFIX]
        if key and key in seen_prefix:
            dropped.append((r, "标题近重复"))
            continue
        seen_prefix.add(key)
        kept.append(r)
    return kept, dropped


# ─────────────────────────────────────────────────────────────
# 模型调用
# ─────────────────────────────────────────────────────────────

# 不同后端对可选字段的支持不一样(llama-server 两个都认;Ollama 认 JSON 模式、
# 不认 chat_template_kwargs;老版本可能两个都不认)。这里记下服务端拒收过哪个,
# 后面的条目就不再发它 —— 只在第一条上浪费一次往返,不是 150 次。
_UNSUPPORTED = set()
_UNSUPPORTED_LOCK = threading.Lock()

OPTIONAL_FIELDS = [("think", DISABLE_THINKING), ("json", JSON_MODE)]


def chat(endpoint, model, prompt, timeout=TIMEOUT):
    while True:
        with _UNSUPPORTED_LOCK:
            extras = [(k, v) for k, v in OPTIONAL_FIELDS if k not in _UNSUPPORTED]

        body = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
            **SAMPLING,
        }
        for _key, payload in extras:
            body.update(payload)

        req = urllib.request.Request(
            endpoint.rstrip("/") + "/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": "Bearer local"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            return data["choices"][0]["message"]["content"]
        except urllib.error.HTTPError as e:
            # 400 多半是服务端不认某个可选字段。从后往前摘掉一个再试,
            # 而不是一次全摘 —— 能留住的就留住。
            if e.code == 400 and extras:
                dropped = extras[-1][0]
                with _UNSUPPORTED_LOCK:
                    _UNSUPPORTED.add(dropped)
                hint = {"think": "思考模式改用启动参数关(llama-server 查 --reasoning / --chat-template-kwargs)",
                        "json": "退回靠 extract_json 解析,打分失败率会上去一点"}[dropped]
                print(f"\n  [降级] 服务端拒收 {dropped} 字段 —— {hint}", file=sys.stderr)
                continue
            raise


def extract_json(text):
    """模型经常把 JSON 包在 ```json 里,或者前面加一句废话,甚至先输出 <think>。
    这里尽量把第一个完整的对象抠出来。"""
    if not text:
        return None
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    text = re.sub(r"^\s*```(?:json)?|```\s*$", "", text.strip(), flags=re.M)
    start = text.find("{")
    if start < 0:
        return None
    depth, in_str, esc = 0, False, False
    for i in range(start, len(text)):
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        else:
            if c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start:i + 1])
                    except json.JSONDecodeError:
                        return None
    return None


def clamp(v, lo=0, hi=5):
    try:
        return max(lo, min(hi, int(round(float(v)))))
    except (TypeError, ValueError):
        return 0


def score_one(rec, endpoint, model):
    kind = "预印本" if rec.get("is_preprint") else "正式发表"
    prompt = PROMPT.format(
        audience=AUDIENCE,
        title=rec.get("title", ""),
        journal=rec.get("journal", "") or "未知",
        kind=kind,
        abstract=(rec.get("abstract") or "")[:4000],
    )
    try:
        raw = chat(endpoint, model, prompt)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, KeyError) as e:
        return {**rec, "score_error": f"{type(e).__name__}: {e}"}

    parsed = extract_json(raw)
    if not parsed:
        return {**rec, "score_error": "模型输出无法解析为 JSON", "raw_head": (raw or "")[:200]}

    rel = clamp(parsed.get("relevance"))
    act = clamp(parsed.get("actionability"))
    ev = clamp(parsed.get("evidence"))
    nov = clamp(parsed.get("novelty"))
    hook = str(parsed.get("hook", "")).strip()[:200]
    ingredient = str(parsed.get("ingredient", "")).strip()[:80]
    sourcing = clamp(parsed.get("sourcing"))
    # 没抽出原料名就不该有采购分 —— 雷达要的是能下单的东西
    if not ingredient:
        sourcing = 0

    raw = (rel * WEIGHT_RELEVANCE + act * WEIGHT_ACTIONABILITY
           + ev * WEIGHT_EVIDENCE + nov * WEIGHT_NOVELTY)
    total = raw
    penalties = []
    if rel <= RELEVANCE_GATE:
        total *= GATE_FACTOR
        penalties.append(f"相关性只有 {rel}")
    if not hook:
        total *= NO_HOOK_FACTOR
        penalties.append("模型没给出切入角度")

    return {
        **rec,
        "scoring_version": SCORING_VERSION,
        "relevance": rel, "actionability": act, "evidence": ev, "novelty": nov,
        "ingredient": ingredient, "sourcing": sourcing,
        "total": round(total, 1),
        "raw_total": round(raw, 1),        # 没打折之前的分,方便你判断折扣是否合理
        "penalties": penalties,
        "subject": str(parsed.get("subject", ""))[:20],
        "topic_fit": str(parsed.get("topic_fit", ""))[:20],
        "hook": hook,
        "reason": str(parsed.get("reason", ""))[:200],
    }


# ─────────────────────────────────────────────────────────────
# 输出
# ─────────────────────────────────────────────────────────────

def select_by_topic(ok, per_topic=None):
    """按 topic_fit 分桶,每桶内部排序后各取前 N。
    跨主题不再互相抢名额 —— 这是 v4 的核心改动。
    返回 [(topic_key, [条目…]), …],顺序按 TOPIC_QUOTA 的书写顺序。"""
    buckets = {k: [] for k in TOPIC_QUOTA}
    for r in ok:
        key = r.get("topic_fit") if r.get("topic_fit") in buckets else "other"
        buckets[key].append(r)

    out = []
    for key, quota in TOPIC_QUOTA.items():
        items = sorted(buckets[key], key=lambda r: (-r.get("total", 0), r.get("pub_date", "")))
        n = quota if per_topic is None else per_topic
        out.append((key, items[:n], len(items)))
    return out


def render_entry(idx, r):
    flag = " · 预印本" if r.get("is_preprint") else ""
    score_line = (f"**总分 {r.get('total')}**"
                  f"　相关 {r.get('relevance')} / 可操作 {r.get('actionability')}"
                  f" / 证据 {r.get('evidence')} / 新意 {r.get('novelty')}"
                  f"　| {r.get('subject','')}")
    if r.get("penalties"):
        score_line += f"　⚠ 已打折({'、'.join(r['penalties'])},原始 {r.get('raw_total')})"
    lines = [
        f"### {idx}. {r.get('title','')}",
        "",
        score_line,
        "",
        f"*{r.get('journal','')}　{r.get('pub_date','')}{flag}*",
        "",
    ]
    if r.get("hook"):
        lines += [f"> **切入角度:** {r['hook']}", ""]
    if r.get("reason"):
        lines += [f"判断:{r['reason']}", ""]
    lines += [f"[原文]({r.get('link','')})", ""]
    return lines


def write_radar(path, scored, src_name):
    """原料雷达:按原料归并,不按论文。同一个原料这期出现几篇,
    是比任何单篇分数都重要的信号 —— 一个原料同时被三组人研究,
    说明它在升温。"""
    ok = [r for r in scored if "score_error" not in r and r.get("sourcing", 0) >= RADAR_MIN_SOURCING]

    groups = {}
    for r in ok:
        key = r["ingredient"].lower().strip(" .,;:")
        groups.setdefault(key, []).append(r)

    # 排序:先看最高采购分,再看这期出现了几篇,最后看证据
    ranked = sorted(
        groups.items(),
        key=lambda kv: (-max(r["sourcing"] for r in kv[1]),
                        -len(kv[1]),
                        -max(r["evidence"] for r in kv[1])),
    )[:RADAR_MAX_INGREDIENTS]

    lines = [
        f"# 原料雷达 · {dt.date.today().isoformat()}",
        "",
        f"来源:`{src_name}`　评分标准:`{SCORING_VERSION}`",
        "",
        f"从 {len(scored)} 篇里筛出有明确可采购原料、且采购分 ≥ {RADAR_MIN_SOURCING} "
        f"(即至少有人体数据)的 {len(ok)} 篇,归并成 {len(groups)} 个原料。",
        "",
        "> 这份表跟「选题清单」是两个独立视角,一篇论文可能只上其中一边。",
        "> **国内新食品原料 / 保健食品原料目录的准入状态,这里不作判断,需要自己核。**",
        "",
        "---",
        "",
        "| 原料 | 采购分 | 本期篇数 | 最强证据 | 代表研究 |",
        "|---|---|---|---|---|",
    ]
    for key, items in ranked:
        items = sorted(items, key=lambda r: (-r["sourcing"], -r["evidence"]))
        best = items[0]
        name = best["ingredient"]
        title = (best.get("title") or "")[:70].replace("|", "/")
        link = best.get("link", "")
        lines.append(
            f"| **{name}** | {max(r['sourcing'] for r in items)} | {len(items)} | "
            f"ev{max(r['evidence'] for r in items)} | [{title}]({link}) |"
        )

    lines += ["", "---", ""]
    for key, items in ranked:
        items = sorted(items, key=lambda r: (-r["sourcing"], -r["evidence"]))
        lines += [f"## {items[0]['ingredient']}", ""]
        for r in items:
            flag = " · 预印本" if r.get("is_preprint") else ""
            lines += [
                f"- **采购 {r['sourcing']} / 证据 {r['evidence']}**　{r.get('journal','')}"
                f"　{r.get('pub_date','')}{flag}",
                f"  [{r.get('title','')}]({r.get('link','')})",
                f"  {r.get('reason','')}",
                "",
            ]

    if not ranked:
        lines += ["", f"*本期没有采购分 ≥ {RADAR_MIN_SOURCING} 的原料。"
                      f"把脚本里的 RADAR_MIN_SOURCING 调到 2 可以看到只有动物数据的那些。*", ""]

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return len(ranked)


def write_markdown(path, scored, top, src_name, dropped_n):
    ok = [r for r in scored if "score_error" not in r]
    bad = [r for r in scored if "score_error" in r]

    selected = select_by_topic(ok, per_topic=top)
    shown = sum(len(items) for _, items, _ in selected)

    lines = [
        f"# 选题清单 · {dt.date.today().isoformat()}",
        "",
        f"来源:`{src_name}`　评分 {len(ok)} 条　粗筛丢弃 {dropped_n} 条"
        + (f"　打分失败 {len(bad)} 条" if bad else ""),
        "",
        f"评分标准:`{SCORING_VERSION}`　"
        f"总分 = 相关×{WEIGHT_RELEVANCE:g} + 可操作×{WEIGHT_ACTIONABILITY:g}"
        f" + 证据×{WEIGHT_EVIDENCE:g} + 新意×{WEIGHT_NOVELTY:g}",
        "",
        f"按主题分桶,各取前 N(配额见脚本里的 `TOPIC_QUOTA`)。共列出 {shown} 条。",
        "",
    ]

    for key, items, pool in selected:
        if not items:
            continue
        lines += ["---", "",
                  f"## {TOPIC_LABELS.get(key, key)}　<sub>{len(items)} / {pool} 条</sub>",
                  ""]
        if TOPIC_NOTES.get(key):
            lines += [f"*{TOPIC_NOTES[key]}*", ""]
        for i, r in enumerate(items, 1):
            lines += render_entry(i, r)

    if bad:
        lines += ["---", "", "## 打分失败", ""]
        for r in bad[:10]:
            lines.append(f"- {r.get('title','')[:70]} — {r.get('score_error','')}")

    lines += ["", f"*全部 {len(ok)} 条的完整评分见同名 .jsonl*", ""]

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


# ─────────────────────────────────────────────────────────────
# 主流程
# ─────────────────────────────────────────────────────────────

def newest_input():
    files = sorted(glob.glob(os.path.join(IN_DIR, "*.jsonl")))
    return files[-1] if files else None


def selftest(endpoint, model):
    print(f"自检:{endpoint}　模型 {model}")
    try:
        out = chat(endpoint, model,
                   '只输出这个 JSON,不要别的:{"relevance":4,"actionability":3,'
                   '"evidence":3,"novelty":2,'
                   '"subject":"human","topic_fit":"gut","hook":"测试","reason":"测试"}',
                   timeout=120)
    except Exception as e:
        print(f"  [失败] 连不上或调用出错:{type(e).__name__}: {e}")
        print("  检查 llama-server:进程在不在、启动时带没带 --jinja、端口对不对")
        print("  检查 Ollama:  ollama serve 起了没、模型 pull 了没、--model 填的是真实模型名")
        return 1
    parsed = extract_json(out)
    if not parsed:
        print(f"  [失败] 模型有响应但输出解析不了。原始开头:{out[:200]}")
        return 1
    print(f"  [成功] 模型返回并解析正常 → {json.dumps(parsed, ensure_ascii=False)}")
    return 0


def main():
    ap = argparse.ArgumentParser(description="用本地模型给文献打分,挑出值得写的选题")
    ap.add_argument("--input", help="输入 JSONL,默认取 out/ 里最新的一份")
    ap.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--top", type=int, default=None,
                    help="每个主题最多列几条。不传就用脚本里的 TOPIC_QUOTA 配额")
    ap.add_argument("--limit", type=int, help="只处理前 N 条(调试用)")
    ap.add_argument("--workers", type=int, default=2, help="并发数,默认 2。显存吃紧就设 1")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        sys.exit(selftest(args.endpoint, args.model))

    src = args.input or newest_input()
    if not src or not os.path.exists(src):
        sys.exit(f"找不到输入文件。先跑 fetch_papers 生成 out/*.jsonl,或用 --input 指定。")

    with open(src, encoding="utf-8") as f:
        records = [json.loads(line) for line in f if line.strip()]
    print(f"读入 {len(records)} 条:{os.path.basename(src)}")

    kept, dropped = prefilter(records)
    print(f"规则粗筛后剩 {len(kept)} 条(丢弃 {len(dropped)} 条)")
    if args.limit:
        kept = kept[:args.limit]
        print(f"--limit 生效,只处理前 {len(kept)} 条")
    if not kept:
        sys.exit("没有可评分的条目。")

    print(f"开始打分:{args.model} @ {args.endpoint}，并发 {args.workers}")
    print(f"评分标准 {SCORING_VERSION}:相关×{WEIGHT_RELEVANCE:g} + "
          f"可操作×{WEIGHT_ACTIONABILITY:g} + 证据×{WEIGHT_EVIDENCE:g} + "
          f"新意×{WEIGHT_NOVELTY:g}")
    scored = []
    lock = threading.Lock()
    done = [0]

    def work(rec):
        out = score_one(rec, args.endpoint, args.model)
        with lock:
            done[0] += 1
            tag = "!" if "score_error" in out else str(out.get("total", ""))
            print(f"\r  {done[0]}/{len(kept)}  最近一条得分 {tag}   ", end="", flush=True)
        return out

    with cf.ThreadPoolExecutor(max_workers=max(1, args.workers)) as ex:
        scored = list(ex.map(work, kept))
    print()

    errs = sum(1 for r in scored if "score_error" in r)
    if errs:
        print(f"  其中 {errs} 条打分失败(详情见输出文件)")

    os.makedirs(OUT_DIR, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y-%m-%d_%H%M")
    jsonl_path = os.path.join(OUT_DIR, stamp + ".jsonl")
    md_path = os.path.join(OUT_DIR, stamp + ".md")
    radar_path = os.path.join(OUT_DIR, stamp + "_radar.md")

    with open(jsonl_path, "w", encoding="utf-8") as f:
        for r in sorted(scored, key=lambda x: -x.get("total", 0)):
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    write_markdown(md_path, scored, args.top, os.path.basename(src), len(dropped))
    n_ing = write_radar(radar_path, scored, os.path.basename(src))

    print(f"\n写入:{md_path}        选题清单")
    print(f"      {radar_path}  原料雷达({n_ing} 个原料)")
    print(f"      {jsonl_path}")

    ok = sorted([r for r in scored if "score_error" not in r], key=lambda x: -x.get("total", 0))
    if ok:
        print(f"\n最高分 3 条:")
        for r in ok[:3]:
            print(f"  [{r['total']}] {r.get('title','')[:64]}")
            if r.get("hook"):
                print(f"        角度:{r['hook'][:60]}")


if __name__ == "__main__":
    main()
