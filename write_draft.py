#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
write_draft.py — Newbio Health 内容流水线 · 第三段:写稿

从 screened/*.jsonl 挑一条选题,生成一份**可以直接粘进 Claude / Codex 网页版**
的提示词包,一次一段按顺序粘,产出三样东西:

    第 1 段  中文公众号稿      面向中国消费者,回答"我该怎么做"
    第 2 段  英文行业资讯稿    给 Newbio 官网,面向同行/客户,回答"这对行业意味着什么"
    第 3 段  首图内容 JSON     CSS 模板渲染成 2.35:1 公众号首图

英文稿不是中文稿的翻译 —— 两个受众问的是不同的问题。
合规要求也更严:**公司官网上的内容在监管眼里是广告,不是媒体**,
所以英文稿绝不能把文献结论跟自家产品挂钩。

默认不调 API。理由很实际:网页版你已经在付费了,还自带出图,而免费 API
的模型高峰期经常 503。加 --api 才会真的调接口(需要 key)。

只用标准库。API key 走环境变量,不许硬编码 —— 这个仓库是公开的。
    setx ANTHROPIC_API_KEY  "sk-ant-..."
    setx GEMINI_API_KEY     "..."        # 免费,aistudio.google.com
    setx DEEPSEEK_API_KEY   "sk-..."
    setx OPENAI_API_KEY     "sk-..."
(设完要新开一个终端窗口)

用法:
    py write_draft.py --list                    # 先看有哪些选题可挑
    py write_draft.py --pick 1                  # 生成提示词包(默认,不花钱)
    py write_draft.py --pick 1 --api            # 直接调 API 出稿(要 key)
    py write_draft.py --pick 2 --topic supplement
    py write_draft.py --pick 1 --provider gemini
    py write_draft.py --pick 1 --provider gemini --model gemini-3-pro
    py write_draft.py --pick 1 --provider deepseek
    py write_draft.py --title "probiotics fail"  # 按标题关键词挑

输出:
    prompts/YYYY-MM-DD_<slug>_提示词.md     ← 默认。整个文件打开,一段一段粘

首图为什么用 CSS 模板而不是生成图片:
  首图上的标题必须一字不错,而扩散模型写不准中文 —— 出来的是"看起来像字"
  的像素。模板还额外给你一致性:每期配色字号都一样,不会飘。

为什么不用本地模型:16GB 显存上限是 14B Q4,写中文科普要在小红书竞争靠的是
文笔和分寸感,达不到。这一段调 API,一天一篇成本几分钱。显卡留给第二段的
批量筛选和第四段的配图 —— 这个分工比全本地或全 API 都合算。
"""

import argparse
import datetime as dt
import email.utils
import glob
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.request

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
IN_DIR = os.path.join(HERE, "screened")
OUT_DIR = os.path.join(HERE, "drafts")
PROMPT_DIR = os.path.join(HERE, "prompts")
TIMEOUT = 300


def _load_dotenv(path=os.path.join(HERE, ".env")):
    """把 .env 里的 KEY=VALUE 塞进 os.environ(已存在的系统变量优先,不覆盖)。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except FileNotFoundError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


_load_dotenv()

# ─────────────────────────────────────────────────────────────
# 三家 API。DeepSeek 和 OpenAI 是同一套协议,Anthropic 不是。
# 先写成可切换的,拿同一个选题各跑一遍比文笔再定。
# ─────────────────────────────────────────────────────────────

PROVIDERS = {
    "claude": {
        "env": "ANTHROPIC_API_KEY",
        "url": "https://api.anthropic.com/v1/messages",
        "model": "claude-sonnet-4-5",
        "style": "anthropic",
    },
    # Gemini 有免费额度,不用绑卡,去 aistudio.google.com 建 key。
    # 注意两件事:
    #   1. 消费级的 Gemini Advanced / AI Pro 订阅**不等于** API 额度 ——
    #      API 的分层只跟结算账号的累计消费挂钩,订阅换不来更高额度。
    #      好在免费层对一天一两篇完全够用。
    #   2. **免费层的数据会被用于改进 Google 产品**(付费层才承诺不用)。
    #      送公开论文摘要没问题,但涉及配方或客户的内容别走这条。
    # 它提供 OpenAI 兼容端点,所以直接复用 openai 那套请求格式。
    "gemini": {
        "env": "GEMINI_API_KEY",
        "url": "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
        "model": "gemini-3.8-flash",
        "style": "openai",
    },
    "deepseek": {
        "env": "DEEPSEEK_API_KEY",
        "url": "https://api.deepseek.com/chat/completions",
        "model": "deepseek-chat",
        "style": "openai",
    },
    "openai": {
        "env": "OPENAI_API_KEY",
        "url": "https://api.openai.com/v1/chat/completions",
        "model": "gpt-4o",
        "style": "openai",
    },
}
DEFAULT_PROVIDER = "claude"
MAX_TOKENS = 4000

# 重试。跟 fetch_papers.py 一套逻辑 —— 那边早就写好了,这边一开始忘了搬,
# 结果 Gemini 一个临时 503(模型过载)就把整个脚本弄死了。
# 同一个坑不该踩第二次。
RETRYABLE = {408, 425, 429, 500, 502, 503, 504}
MAX_RETRIES = 5
MAX_BACKOFF = 60

# 这些错重试没用,直接说清楚是什么问题,别让人对着状态码猜
FATAL_HINTS = {
    400: "请求格式不对 —— 多半是模型名写错了,用 --model 换一个试试",
    401: "认证失败 —— key 不对、过期了,或者复制的时候带了空格",
    403: "没权限 —— key 可能没开通这个模型,或者你所在地区不支持",
    404: "地址或模型不存在 —— 检查 --model 拼写",
    413: "请求太大 —— 摘要过长,调小 build_prompt 里的截断长度",
}


# ─────────────────────────────────────────────────────────────
# 提示词。这一段是整个第三段的产品质量所在,想调文风就改这里。
# ─────────────────────────────────────────────────────────────

COMMON_RULES = """你在为「Newbio Health」写健康科普。这个号背后是一家做饲料益生菌和
宠物营养原料的公司,号是给公司做背书的品牌资产 —— 写砸一篇的代价不是少几个赞,
是专业形象受损。

读者是中国的普通消费者,关心自己和家人的健康、饮食、肠道、益生菌,以及自家猫狗
的营养。他们不是研究人员,看不懂统计学,但不傻,讨厌被吓唬,喜欢"所以我到底
该怎么做"这种能落地的结论。

**六条硬规矩,一条都不能破:**

1. **证据强度必须如实说。** 动物实验就写"在小鼠身上",体外实验就写"在实验室
   条件下",小样本就写"样本不大"。**绝对不许把动物实验写成"研究证明"。**
   **作者自己提出的新概念、新指标、新框架,要写成「有研究者提出」,不能写成
   「科学家发现」** —— 前者是假说,后者是定论,差着十万八千里。
   如实说不会让文章变弱 —— 读者信任的恰恰是这个。

2. **不出现"治疗""治愈""根治""疗效""药效"这类词。** 这是食品和保健品在中国
   的宣传红线。可以说"可能有帮助""与……相关""改善了某个指标"。

3. **不编造摘要里没有的东西。** 没提到具体剂量就不要写剂量,没说多少人参与就
   不要编样本量。宁可含糊也不要编 —— 编出来的数字是最容易被打脸的。

4. **专业名词拿不准中文译名就保留英文原词。** 化合物名、菌株名尤其如此。
   举个真出过的错:luteolin 是木犀草素,不是叶黄素(lutein),两个完全不同的
   东西。不确定就写英文,让读者自己查。

5. **该提醒就医的地方要提醒,但别生硬。** 不要在结尾甩一句"本文不能替代医疗
   建议"了事 —— 写在该写的地方,比如"如果你已经在吃降压药,加这个之前问一下
   医生",这样读者才真的会听。

6. **不做题党。** 标题可以有吸引力,但内容必须兑现标题的承诺。

7. **菌株号要写,但不能写成「这一株更强」。** 这两件事差得很远:
   - **可追溯(对的):**「这项试验用的是 BB-12」「LGG 的那项研究」——
     写菌株号是因为**换一株就没有这项证据了**,证据是跟着菌株走的。
     只写「乳酸菌」对不上任何一项具体证据,等于没说。
   - **优越(错的):**「BB-12 比其他双歧杆菌更有效」——
     绝大多数益生菌研究**根本没做过菌株之间的头对头比较**,
     而「被研究得最多的菌株」通常等于「商业背书最多的菌株」。
     没被比下去不等于更强。

   一个具体的反例,写的时候想想:*Bifidobacterium animalis* subsp. *lactis*
   是基因组**单态**亚种 —— 同亚种两株的全基因组 99.975% 相同,1.94 Mb 里
   只差 47 个 SNP。**几乎没有留下「某一株特别不同」的遗传空间。**
   所以 BB-12 的"特殊"主要来自试验数量,不来自生物学。

   (少数菌株确实有实体依托的差异,比如 LGG 带 spaCBA 菌毛操纵子、编码
   人黏液结合蛋白 —— 但那也只说明它"不一样",不说明它"更有效"。)

   **判断标准很简单:摘要里有没有做菌株间的对照?没有就只能写可追溯,
   不能写优越。**

---

## 这次要写的研究

**标题:** {title}
**期刊:** {journal}　**日期:** {pub_date}{preprint}
**研究类型:** {subject}
**证据强度:** {evidence_block}
**编辑给的切入角度:** {hook}

**摘要原文:**
{abstract}

---
"""

WECHAT_PROMPT = COMMON_RULES + """
## 现在写公众号版

**长度 1200-2000 字。** 公众号读者是坐下来读的,可以铺陈,但别注水。

**结构:**
1. **开头用一个具体的生活场景切入** —— 不要从"最近一项研究发现"开头,
   那是新闻稿的写法。从读者自己的经历切入:货架前的犹豫、吃了三个月没感觉、
   医生说的一句话。
2. **这项研究到底发现了什么** —— 用大白话讲明白,一个专业名词都不要不解释就用。
3. **这个结论有多硬** —— 诚实交代研究类型和局限。这一节是这个号的护城河,
   别人不写,你写。
4. **所以你该怎么做** —— 落到具体行动上。买什么、怎么看配料表、什么时候吃、
   什么情况下别碰。这一节读者截图转发的概率最高。
5. **一句话收尾** —— 不要总结前面说过的话,给一个能带走的判断。

**格式:** 用 Markdown,小标题用 `##`。段落短,三五行一段。
**结尾附上:** 原文标题(英文原名)和链接,格式为 `> 原文:[标题](链接)`。

只输出文章正文,不要有"好的,我来写"之类的话,不要用代码块包裹。
第一行是文章标题,用 `# ` 开头。
"""

XHS_PROMPT = COMMON_RULES + """
## 现在写小红书版

**长度 400-700 字。** 小红书是刷的不是读的,前三行留不住人就没有然后了。

**结构:**
1. **第一行就是钩子。** 一句话,说中读者的具体处境或打破一个常见误解。
2. **3-5 个要点**,每个点用一个 emoji 开头,一两句话说清。
3. **一段"怎么做"** —— 具体到能照做。
4. **一句诚实的边界** —— 比如"这是在小鼠身上做的,人身上还没验证",
   放在结尾反而加分,小红书用户对"过度承诺"很敏感。

**格式:**
- 口语化,像跟朋友说话。可以用"你""咱们"。
- emoji 适度,每个要点一个就够,不要满屏。
- 不用 Markdown 小标题(小红书不渲染),用换行和 emoji 分隔。
- **结尾给 6-8 个话题标签**,用 `#标签` 格式,一行排开。
  标签要混搭:大词(#肠道健康)+ 精准词(#益生菌怎么选)+ 场景词(#养猫日常)。

只输出正文,不要有"好的"之类的话,不要用代码块包裹。
"""

# 证据分只说有多硬,研究类型说硬在哪。两个必须一起给写稿模型。
#
# 踩过的坑:一篇作者自己提出新框架的观点综述拿了 5 分,而旧版只把 5 分翻译成
# "meta 分析或 RCT,证据很强" —— 写稿模型会把尚待验证的假说当成学界定论,
# 教读者去测一个只存在于这一篇论文设想里的指标。
EVIDENCE_NOTE = {
    5: "证据等级最高的那一档",
    4: "人体队列研究",
    3: "人体研究,但样本小或为观察性",
    2: "动物实验",
    1: "体外实验",
    0: "证据很弱",
}

SUBJECT_NOTE = {
    "human": [
        "人体研究。可以说在人身上观察到,但仍要交代样本规模和研究设计。",
    ],
    "animal": [
        "**动物实验。文章里必须写明是在动物身上做的**,绝不能写成「研究证明」。",
        "药物诱导的造模跟人的同名疾病不是一回事,要写就得说清这一层。",
    ],
    "in_vitro": [
        "**体外实验。必须写明是实验室培养皿里的结果**,离「吃下去有没有用」还很远。",
    ],
    "review": [
        "**综述 / 观点文章,不是原始研究。这一条最容易出事,看仔细:**",
        "如果摘要里用的是 we propose / we introduce / we define / we suggest 这类措辞,",
        "说明这是**作者提出的新框架或假说,还没有被验证**。",
        "可以介绍这个思路,但**绝不能当成已有定论来写**,",
        "更不能教读者去用一个只存在于这篇论文里的指标或方法。",
        "对的写法:「有研究者提出了一个新思路,这个想法还需要更多验证」。",
        "错的写法:「科学家发现,你可以这样测一测」。",
        "但如果摘要里有 systematic review 或 meta-analysis,那是归纳已有研究,",
        "证据是硬的,按正常写。",
    ],
}


def evidence_block(rec):
    """把证据分和研究类型拼成一段指令。只给分数不给类型会出事,见上面注释。

    手工选题(manual/*.json)可以带一个 subject_note 字段,直接覆盖这里的
    查表结果 —— 因为手工选题往往不属于 human/animal/in_vitro/review 里的
    任何一类(比如"比较基因组学"),硬套一个反而会给出误导性的提醒。"""
    ev = rec.get("evidence", 0)
    # 手工选题也可以覆盖证据标签 —— 查表出来的"人体队列研究"套在一篇
    # 比较基因组学的题目上就是错的,跟之前 review 拿 5 分那个 bug 同一类:
    # 标签和实际内容对不上,一样会误导写稿模型。
    label = rec.get("evidence_note") or EVIDENCE_NOTE.get(ev, "未知")
    out = "{}/5({})".format(ev, label)
    override = rec.get("subject_note")
    if override:
        lines = override if isinstance(override, list) else [override]
    else:
        lines = SUBJECT_NOTE.get((rec.get("subject") or "").strip().lower())
    if lines:
        out += "\n**研究类型提醒:** " + "\n  ".join(lines)
    return out


# ─────────────────────────────────────────────────────────────
# API 调用
# ─────────────────────────────────────────────────────────────

def retry_after_seconds(headers):
    """429 / 503 常带 Retry-After,读得懂就按它说的等 —— 服务端比我们
    更清楚自己什么时候能缓过来。跟 fetch_papers.py 里是同一套。"""
    raw = (headers or {}).get("Retry-After")
    if not raw:
        return None
    raw = str(raw).strip()
    if raw.isdigit():
        return min(int(raw), MAX_BACKOFF)
    try:
        ts = email.utils.parsedate_to_datetime(raw)
        return max(0.0, min((ts - dt.datetime.now(ts.tzinfo)).total_seconds(), MAX_BACKOFF))
    except Exception:
        return None


def call_api(provider, model, prompt, timeout=TIMEOUT):
    cfg = PROVIDERS[provider]
    key = os.environ.get(cfg["env"], "").strip()
    if not key:
        raise SystemExit(
            f"没找到 {cfg['env']}。设一下再跑(设完新开终端窗口):\n"
            f'    setx {cfg["env"]} "你的key"\n'
            f"或者换一家:--provider {'/'.join(k for k in PROVIDERS if k != provider)}"
        )

    if cfg["style"] == "anthropic":
        body = {"model": model, "max_tokens": MAX_TOKENS,
                "messages": [{"role": "user", "content": prompt}]}
        headers = {"content-type": "application/json", "x-api-key": key,
                   "anthropic-version": "2023-06-01"}
    else:
        body = {"model": model, "max_tokens": MAX_TOKENS,
                "messages": [{"role": "user", "content": prompt}]}
        headers = {"content-type": "application/json",
                   "authorization": f"Bearer {key}"}

    payload = json.dumps(body).encode("utf-8")
    last = None
    for attempt in range(MAX_RETRIES):
        req = urllib.request.Request(cfg["url"], data=payload, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:400]
            if e.code not in RETRYABLE:
                hint = FATAL_HINTS.get(e.code, "重试没用,看下面的原文")
                raise SystemExit(f"API 返回 {e.code} {e.reason} —— {hint}\n{detail}")
            last = f"{e.code} {e.reason}"
            wait = retry_after_seconds(e.headers)
            if wait is None:
                wait = min(2 ** attempt, MAX_BACKOFF)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            last = f"{type(e).__name__}: {e}"
            wait = min(2 ** attempt, MAX_BACKOFF)

        if attempt == MAX_RETRIES - 1:
            raise SystemExit(
                f"连续 {MAX_RETRIES} 次失败:{last}\n"
                f"429 是限流(免费额度用完了,等等再来);503 是模型过载,"
                f"换个模型或换一家试试:--provider deepseek")
        wait += random.uniform(0, 1.0)
        print(f"\n  {last},{wait:.0f}s 后重试(第 {attempt + 1}/{MAX_RETRIES} 次)…",
              end="", flush=True)
        time.sleep(wait)

    if cfg["style"] == "anthropic":
        return "".join(b.get("text", "") for b in data.get("content", []))
    return data["choices"][0]["message"]["content"]


# ─────────────────────────────────────────────────────────────
# 选题
# ─────────────────────────────────────────────────────────────

def newest_input():
    files = [f for f in sorted(glob.glob(os.path.join(IN_DIR, "*.jsonl")))]
    return files[-1] if files else None


def load_shortlist(path):
    """按选题清单的分桶顺序返回候选,跟 .md 里看到的顺序一致。"""
    sys.path.insert(0, HERE)
    import screen_papers as sp
    rows = [json.loads(l) for l in open(path, encoding="utf-8") if l.strip()]
    ok = [r for r in rows if "score_error" not in r]
    out = []
    for key, items, _pool in sp.select_by_topic(ok):
        for r in items:
            out.append((key, r))
    return out


def slugify(title, n=40):
    s = re.sub(r"[^\w\s-]", "", title, flags=re.U).strip()
    s = re.sub(r"[\s_-]+", "-", s)
    return s[:n].strip("-") or "draft"


def build_prompt(template, rec):
    return template.format(
        title=rec.get("title", ""),
        journal=rec.get("journal", "") or "未知",
        pub_date=rec.get("pub_date", ""),
        preprint="　**这是预印本,尚未经过同行评审 —— 文章里要说明这一点**" if rec.get("is_preprint") else "",
        subject=rec.get("subject", "") or "未标注",
        evidence_block=evidence_block(rec),
        hook=rec.get("hook") or "(编辑没给角度,你自己找一个)",
        abstract=(rec.get("abstract") or "")[:6000],
        link=rec.get("link", ""),
    )


# ─────────────────────────────────────────────────────────────
# 第 2 段:英文行业资讯稿(给 Newbio 官网)
#
# 这跟中文稿是两个完全不同的东西,不是翻译:
#   中文 → 公众号,面向中国消费者,回答"我该怎么做"
#   英文 → 公司官网,面向同行/客户/供应商,回答"这对行业意味着什么"
#
# 合规上也不一样,而且更严:**公司官网上的内容在监管眼里是广告,不是媒体。**
# 同一句话发公众号是科普,挂在公司网站上就可能被当成产品宣称。
# 所以英文稿必须是行业观察的口吻,绝不能把文献结论跟自家产品挂钩。
# ─────────────────────────────────────────────────────────────

EN_BRIEF_PROMPT = """## 第 2 段 —— 英文行业资讯稿(等上一段写完再粘)

Now write a **separate English piece** on the same paper — not a translation.

It goes on **Newbio Trading Corp's website**, in an industry-notes section. The readers
are different people with a different question:

| | 中文公众号 | English brief |
|---|---|---|
| Reader | Chinese consumers | ingredient buyers, formulators, industry peers |
| Question | "what should I do?" | "what does this mean for the category?" |
| Register | warm, conversational | plain, professional, unhurried |

**Length: 350-550 words.** These readers skim. Density beats length.

**Structure** (no headings needed unless it genuinely helps):
1. **What was studied, in one or two sentences.** Lead with the finding, not the background.
2. **The design, stated plainly** — n, duration, control, population. Numbers in the body text.
3. **What it does and does not establish.** This is the part that earns trust with this audience;
   they read papers themselves and will notice if you overstate.
4. **Why it matters for the category** — formulation, sourcing, claim substantiation,
   or the gap it leaves open. Keep it observational.

---

### Six hard rules — this piece sits on a company website, so they are stricter than the Chinese one

1. **Never connect the finding to Newbio's own products or capabilities.** No "which is why
   we…", no "this validates…", no closing line about what the company offers.
   **The moment this reads as promotion, it becomes an advertising claim and the
   regulatory standard changes.** Write it as if a trade journal were publishing it.

2. **No health claims, in any grammatical form.** Not "supports immunity", not "may help with".
   Report what the study measured and found: *"reported a reduction in hs-CRP relative to
   control"*. Attribute everything to the study, never to the ingredient in general.

3. **State the evidence grade explicitly and early.** A pilot trial is a pilot trial; an
   in vitro study is an in vitro study; a narrative review is not evidence of effect.
   **Use the study's own hedging language** — if the authors wrote "suggests" or "warrants
   further validation", carry that through rather than flattening it.

4. **No superiority language about any named strain or ingredient** unless the study did a
   head-to-head comparison. Naming a strain is traceability, not a ranking.

5. **Never invent numbers.** If the abstract does not give a dose, sample size or duration,
   omit it. Omissions are fine; fabrications are not.

6. **Keep the Latin binomials correct and italicised on first use**, and keep compound names
   in their published form. (A real error from this project: *luteolin* is 木犀草素, not
   lutein — different compounds entirely. Precision here is what this audience checks.)

**End with the citation** as a single line: title, journal, date, link.

Output the English text only. No preamble, no code fences.
"""


# ─────────────────────────────────────────────────────────────
# 第 3 段:公众号首图。用 CSS 模板渲染,不用生成图片 ——
# 扩散模型写不准中文,而首图上的标题必须一字不错。
# ─────────────────────────────────────────────────────────────

COVER_SPEC_PROMPT = """## 第 3 段 —— 公众号首图(等前两段写完再粘)

最后给我一个首图的内容。**不要画图,也不要写 HTML** —— 本地有 CSS 模板
(`make_cards.py --cover`)负责渲染,你只出内容。

这么做是因为首图上的标题必须一字不错,而图像生成模型写不准中文。

输出这个 JSON,**只输出 JSON,不要用代码块包裹**:

{
  "kicker": "左上角小分类,比如「肠道健康 · 看论文说人话」,10 字以内",
  "title": "首图大标题,**18 字以内**,用 \\n 分成两行最好看",
  "subtitle": "一句副标题,30-45 字,用 **双星号** 标一处重点"
}

**要求:**
- `title` 不必等于文章标题 —— 首图是让人停下来的,可以更短更钩子一点,
  但**必须兑现文章的内容**,不许做题党。
- `subtitle` 是补充信息,不是重复标题。放一个具体的数字或一句反直觉的判断最有效。
- 全程不出现"治疗""治愈""疗效"这类词。

我这边怎么用(你不用管):存成 `covers/<日期>_<短名>.json`,
然后 `py make_cards.py covers/xxx.json --cover`,浏览器打开截图。
"""


def build_prompt_pack(rec):
    """生成一份按顺序粘贴的提示词包。
    分段而不是揉成一个大提示词,是因为网页版对话有上下文:
    第二段可以说"把刚才那篇改写成…",模型知道指的是什么,
    不用把摘要和规矩再抄一遍。"""
    head = build_prompt(WECHAT_PROMPT, rec)
    head = head.replace("## 现在写公众号版", "## 第 1 段 —— 公众号版(先粘这个)")

    parts = [
        f"# 写稿提示词包 · {rec.get('title', '')[:60]}",
        "",
        f"**选题角度:** {rec.get('hook') or '(无)'}",
        f"**证据:** {rec.get('evidence')}/5　{rec.get('subject', '')}"
        + ("　预印本" if rec.get("is_preprint") else ""),
        f"**原文:** [{rec.get('title', '')}]({rec.get('link', '')})",
        "",
        "",
        "> **怎么用:** 打开网页版新建一个对话,把下面每个「第 N 段」依次粘进去,",
        "> 一段出完结果再粘下一段。**不要把整个文件一次性粘进去** —— 分段是为了",
        "> 让模型专注,而且后面几段能引用前面的输出。",
        "",
        "---",
        "",
        head,
        "",
        "---",
        "",
        EN_BRIEF_PROMPT,
        "",
        "---",
        "",
        COVER_SPEC_PROMPT,
        "",
        "---",
        "",
    ]
    parts += [
        "",
        "---",
        "",
        "## 最后自己过一遍",
        "",
        "AI 写完不等于能发。发之前对着这几条扫一眼:",
        "",
        "- **数字和菌株名**跟原文摘要对得上吗?编出来的数字是最容易被打脸的",
        "- **动物实验有没有被写成「研究证明」**?这是这个号最不能犯的错",
        "- 有没有出现**治疗 / 治愈 / 根治 / 疗效**这类词?食品宣传红线",
        "- 专业名词的中文译名拿不准的,**是不是保留了英文原词**?",
        "  (真出过的错:luteolin 是木犀草素,不是叶黄素)",
        "- 配图里**有没有混进文字**?生成的图里只要有字,基本都是错的",
        "",
        f"*选题来源:评分标准 {rec.get('scoring_version', '')}*",
        "",
    ]
    return "\n".join(parts)

def main():
    ap = argparse.ArgumentParser(description="从选题清单生成公众号稿和小红书稿")
    ap.add_argument("--input", help="输入 jsonl,默认取 screened/ 里最新的")
    ap.add_argument("--list", action="store_true", help="列出可选的选题就退出")
    ap.add_argument("--pick", type=int, help="选第几条(配合 --list 看序号)")
    ap.add_argument("--topic", help="限定在某个桶里选,如 gut / supplement / pet")
    ap.add_argument("--title", help="按标题关键词选(不区分大小写)")
    ap.add_argument("--manual", metavar="FILE",
                    help="用手工写的选题(manual/*.json),不从文献清单里挑。"
                         "适合那些不是来自单篇论文、但值得写的题目")
    ap.add_argument("--api", action="store_true",
                    help="不生成提示词包,直接调 API 出稿(需要 key)")
    ap.add_argument("--provider", default=DEFAULT_PROVIDER, choices=list(PROVIDERS))
    ap.add_argument("--model", help="覆盖该 provider 的默认模型")
    ap.add_argument("--only", choices=["wechat", "xhs"], help="只写其中一个版本")
    ap.add_argument("--dry-run", action="store_true",
                    help="配合 --api:只打印提示词不调接口。不加 --api 时无意义")
    args = ap.parse_args()

    # 手工选题:字段名跟评分输出保持一致,所以下游一行都不用改
    if args.manual:
        if not os.path.exists(args.manual):
            sys.exit(f"找不到 {args.manual}。manual/ 目录下有模板可以照抄。")
        with open(args.manual, encoding="utf-8") as f:
            rec = json.load(f)
        missing = [k for k in ("title", "abstract", "hook") if not rec.get(k)]
        if missing:
            sys.exit(f"{args.manual} 缺字段:{', '.join(missing)}")
        return emit(rec, args, src_label=os.path.basename(args.manual))

    src = args.input or newest_input()
    if not src or not os.path.exists(src):
        sys.exit("找不到选题清单。先跑 screen_papers.py,或用 --input 指定。")

    cands = load_shortlist(src)
    if args.topic:
        cands = [(k, r) for k, r in cands if k == args.topic]
    if not cands:
        sys.exit(f"没有候选选题(--topic {args.topic} 可能没这个桶)。")

    if args.list or (args.pick is None and not args.title):
        print(f"来源:{os.path.basename(src)}\n")
        last = None
        for i, (key, r) in enumerate(cands, 1):
            if key != last:
                print(f"── {key}")
                last = key
            print(f"  {i:2}. [{r.get('total')}] {r.get('title','')[:74]}")
            if r.get("hook"):
                print(f"      角度:{r['hook'][:66]}")
        print(f"\n挑一条:py write_draft.py --pick <序号>")
        print(f"先看提示词不花钱:py write_draft.py --pick <序号> --dry-run")
        return

    if args.title:
        hits = [(k, r) for k, r in cands if args.title.lower() in (r.get("title") or "").lower()]
        if not hits:
            sys.exit(f"没有标题包含「{args.title}」的选题。用 --list 看看有哪些。")
        if len(hits) > 1:
            print(f"匹配到 {len(hits)} 条,用第一条。其余:")
            for _k, r in hits[1:4]:
                print(f"   {r.get('title','')[:70]}")
        rec = hits[0][1]
    else:
        if not 1 <= args.pick <= len(cands):
            sys.exit(f"--pick 要在 1 到 {len(cands)} 之间。用 --list 看序号。")
        rec = cands[args.pick - 1][1]

    return emit(rec, args, src_label=os.path.basename(src))


def emit(rec, args, src_label):
    print(f"选题:{rec.get('title','')[:76]}")
    print(f"      证据 {rec.get('evidence')}/5　{rec.get('subject','')}"
          f"{'　预印本' if rec.get('is_preprint') else ''}")
    print(f"角度:{rec.get('hook','(无)')}")

    # ── 默认:生成提示词包,不联网、不花钱 ──
    if not args.api:
        os.makedirs(PROMPT_DIR, exist_ok=True)
        pack = build_prompt_pack(rec)
        path = os.path.join(
            PROMPT_DIR,
            f"{dt.date.today().isoformat()}_{slugify(rec.get('title',''))}"
            f"_提示词.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write(pack)
        print(f"\n写入:{path}")
        print(f"      {len(pack)} 字符,分 {pack.count('## 第')} 段")
        print("\n打开这个文件,把每个「第 N 段」依次粘进 Claude 或 Codex:")
        print("  第 1 段 → 中文公众号稿,存进 drafts/")
        print("  第 2 段 → 英文行业资讯稿(给 Newbio 官网),存进 drafts_en/")
        print("  第 3 段 → 首图内容 JSON,存进 covers/,再跑 make_cards.py --cover")
        print("\n不要一次性全粘 —— 分段是为了让模型专注,后面两段要引用第 1 段的输出。")
        return

    model = args.model or PROVIDERS[args.provider]["model"]
    print(f"模型:{args.provider} / {model}\n")

    jobs = [("wechat", "公众号", WECHAT_PROMPT), ("xhs", "小红书", XHS_PROMPT)]
    if args.only:
        jobs = [j for j in jobs if j[0] == args.only]

    if args.dry_run:
        for _tag, label, tpl in jobs:
            print("=" * 70)
            print(f"【{label}版提示词】\n")
            print(build_prompt(tpl, rec))
        print("=" * 70)
        print("--dry-run:没有调 API,没有花钱。确认没问题就去掉这个参数。")
        return

    os.makedirs(OUT_DIR, exist_ok=True)
    stamp = dt.date.today().isoformat()
    slug = slugify(rec.get("title", ""))
    written = []
    for _tag, label, tpl in jobs:
        print(f"正在写{label}版…", end="", flush=True)
        text = call_api(args.provider, model, build_prompt(tpl, rec))
        path = os.path.join(OUT_DIR, f"{stamp}_{slug}_{label}.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write(text.strip() + "\n\n---\n\n")
            f.write(f"*选题来源:{os.path.basename(src)}　"
                    f"评分标准 {rec.get('scoring_version','')}　"
                    f"生成模型 {args.provider}/{model}*\n")
            f.write(f"*原文:[{rec.get('title','')}]({rec.get('link','')})*\n")
        print(f" 完成 {len(text)} 字")
        written.append(path)

    print()
    for p in written:
        print(f"写入:{p}")
    print("\n稿子是初稿,发之前自己过一遍 —— 尤其是数字和专业名词。")


if __name__ == "__main__":
    main()
