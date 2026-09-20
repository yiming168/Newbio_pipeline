# -*- coding: utf-8 -*-
"""run_all.py — 一条命令跑完全程,全程只用本地模型,不依赖 Claude / Codex。

    py run_all.py

    抓取 → 筛选 → 取总榜前 4 → 每篇出四样(中文稿 / 英文稿 / 小红书导流 / 首图)

和 write_draft.py 的关系:**提示词是同一套,只是换了谁来执行。**
write_draft.py 把提示词打包成 .md 让你粘进网页版;这个脚本把同样的提示词
发给 localhost 上的 llama-server。提示词只维护一份,两条路共用。

为什么后三样要带着「刚写好的文章」一起发:
    第 2/3/4 段的提示词原本是为网页版写的,靠对话上下文引用第 1 段的输出。
    本地这边每次调用都是独立的,所以要手工把上下文喂回去 —— 做法是伪造一段
    对话:[素材] → [助手:文章正文] → [第 N 段提示词]。这样提示词一个字都不用改。

上下文预算(start_server.bat 里 CTX=8192):
    最挤的是英文稿那一步,素材 + 文章 + 提示词 + 输出 ≈ 5000 token,留了余量。
    真要撑爆了,把 CTX 调到 16384(显存够的话),或者用 --only 分次跑。

写作的采样参数跟打分**不一样**,别照搬:
    打分要可复现,temperature=0.2;写作要的是能读,0.2 会写出干巴巴的车轱辘话。
    这里用 Qwen3 官方给非思考模式的推荐档(0.7 / 0.8 / 20),另加一点
    presence_penalty 压重复 —— 长文本本地小模型最常见的翻车就是原地绕圈。
"""

import argparse
import datetime as dt
import glob
import io
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

if hasattr(sys.stdout, "reconfigure"):          # 中文 Windows 控制台默认 GBK
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import screen_papers as sp                      # noqa: E402
import write_draft as wd                        # noqa: E402

DRAFT_DIR = os.path.join(HERE, "drafts")
EN_DIR = os.path.join(HERE, "drafts_en")
COVER_DIR = os.path.join(HERE, "covers")

DEFAULT_TOP = 4
DEFAULT_DAYS = 7

# 写作用的采样参数 —— 见文件头的说明,不要跟 screen_papers.SAMPLING 混用
WRITE_SAMPLING = {"temperature": 0.7, "top_p": 0.80, "top_k": 20,
                  "presence_penalty": 0.5}

MAX_TOKENS = {"wechat": 3500, "en": 1200, "xhs": 700, "cover": 300}

# 本地生成一篇长文要几分钟,不是几秒。超时给足,否则会在快写完时被掐断。
TIMEOUT_WRITE = 1800
TIMEOUT_PING = 10

MIN_ARTICLE_CHARS = 600         # 低于这个多半是模型跑偏了,不要静悄悄存下来
BANNED = ("治愈", "根治", "疗效", "包治")


# ═══════════════════════════════════════════════════════════════
# 和本地模型说话
# ═══════════════════════════════════════════════════════════════

def chat(messages, endpoint, model, max_tokens, json_mode=False,
         timeout=TIMEOUT_WRITE):
    """多轮对话版。screen_papers.chat 是单轮 + 强制 JSON,这里两个都不合用。"""
    body = {
        "model": model,
        "messages": messages,
        "stream": False,
        "max_tokens": max_tokens,
        **WRITE_SAMPLING,
    }
    body.update(sp.DISABLE_THINKING)
    if json_mode:
        body.update(sp.JSON_MODE)
        body["presence_penalty"] = 0.0      # JSON 的键名天生重复,罚它会出坏 JSON

    req = urllib.request.Request(
        endpoint.rstrip("/") + "/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer local"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data["choices"][0]["message"]["content"]


def preflight(endpoint, model):
    """本地服务没起来是最常见的失败。先花两秒问一句,别等抓完筛完才发现。"""
    try:
        chat([{"role": "user", "content": "回一个字:好"}],
             endpoint, model, max_tokens=8, timeout=TIMEOUT_PING)
        return True
    except Exception as e:
        print(f"\n连不上本地模型({endpoint}):{e}", file=sys.stderr)
        print("\n  1. start_server.bat 那个窗口还开着吗?", file=sys.stderr)
        print("  2. 起服务时带 --jinja 了吗?没带的话关思考的参数不生效。",
              file=sys.stderr)
        print("  3. 端口改过的话用 --endpoint 指过来。", file=sys.stderr)
        return False


# ═══════════════════════════════════════════════════════════════
# 上下文:把「刚写好的文章」喂回给模型
# ═══════════════════════════════════════════════════════════════

def material(rec):
    """一段紧凑的素材,替代完整的第 1 段提示词。
    后三步只需要模型知道「这篇稿子是根据什么写的」,不需要再看一遍写作要求。"""
    return "\n".join([
        "这是我们刚才那篇稿子的原始素材:",
        f"- 论文:{rec.get('title', '')}",
        f"- 期刊:{rec.get('journal') or '未知'}　{rec.get('pub_date', '')}",
        f"- 研究类型:{rec.get('subject') or '未标注'}",
        f"- 证据强度:{wd.evidence_block(rec)}",
        f"- 原文链接:{rec.get('link', '')}",
        "",
        "摘要:",
        (rec.get("abstract") or "")[:3000],
    ])


def followup(rec, article, prompt, with_rules=False):
    """伪造一段对话,让为网页版写的提示词在本地也能原样用。"""
    stub = material(rec)
    if with_rules:
        stub = wd.COMMON_RULES + "\n\n" + stub
    return [
        {"role": "user", "content": stub + "\n\n请据此写一篇中文公众号稿。"},
        {"role": "assistant", "content": article},
        {"role": "user", "content": prompt},
    ]


# ═══════════════════════════════════════════════════════════════
# 四样产出
# ═══════════════════════════════════════════════════════════════

def clean_output(text):
    """模型爱用 ``` 把正文包起来,也爱先说一句「好的,我来写」。"""
    t = (text or "").strip()
    t = re.sub(r"^```[a-zA-Z]*\s*\n", "", t)
    t = re.sub(r"\n```\s*$", "", t)
    return t.strip()


def gen_article(rec, endpoint, model):
    text = clean_output(chat(
        [{"role": "user", "content": wd.build_prompt(wd.WECHAT_PROMPT, rec)}],
        endpoint, model, MAX_TOKENS["wechat"]))
    return text


def gen_en(rec, article, endpoint, model):
    return clean_output(chat(
        followup(rec, article, wd.EN_BRIEF_PROMPT),
        endpoint, model, MAX_TOKENS["en"]))


def gen_xhs(rec, article, endpoint, model):
    return clean_output(chat(
        followup(rec, article, wd.XHS_LEAD_PROMPT, with_rules=True),
        endpoint, model, MAX_TOKENS["xhs"]))


def gen_cover(rec, article, endpoint, model):
    raw = chat(followup(rec, article, wd.COVER_SPEC_PROMPT),
               endpoint, model, MAX_TOKENS["cover"], json_mode=True)
    spec = sp.extract_json(raw)
    if not isinstance(spec, dict) or not spec.get("title"):
        return None
    return {k: spec.get(k, "") for k in ("kicker", "title", "subtitle")}


# ═══════════════════════════════════════════════════════════════
# 落盘
# ═══════════════════════════════════════════════════════════════

def article_slug(article, rec, n=24):
    """优先用文章自己的标题做文件名,跟手工存的那批保持一致的中文命名。"""
    first = (article.splitlines() or [""])[0].strip()
    title = first.lstrip("# ").strip() if first.startswith("#") else ""
    return wd.slugify(title or rec.get("title", ""), n)


def save(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"                      # 先写 .tmp 再覆盖 —— 见 HANDOFF 第 18 条
    with io.open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text.rstrip() + "\n")
    os.replace(tmp, path)
    return path


def warn_article(article):
    """不拦,只提醒 —— 本地小模型写长文最容易在这两处翻车。"""
    bad = [w for w in BANNED if w in article]
    notes = []
    if bad:
        notes.append("出现宣传红线词:" + "、".join(bad))
    if len(article) < MIN_ARTICLE_CHARS:
        notes.append(f"只有 {len(article)} 字,疑似没写完")
    if "研究证明" in article or "科学证实" in article:
        notes.append("出现「研究证明 / 科学证实」,核对一下是不是动物实验")
    return notes


# ═══════════════════════════════════════════════════════════════
# 选题
# ═══════════════════════════════════════════════════════════════

def pick_top(path, n=DEFAULT_TOP, skip=("other",)):
    """跨桶按总分取前 n。桶是给人看清单用的,自动写稿只认分数。"""
    rows = [json.loads(l) for l in io.open(path, encoding="utf-8") if l.strip()]
    ok = [r for r in rows if "score_error" not in r]
    picked = []
    for key, items, _pool in sp.select_by_topic(ok):
        if key in skip:
            continue
        picked.extend(items)
    picked.sort(key=lambda r: (-(r.get("total") or 0), r.get("title", "")))
    seen, out = set(), []
    for r in picked:
        uid = r.get("uid") or r.get("title")
        if uid in seen:
            continue
        seen.add(uid)
        out.append(r)
        if len(out) >= n:
            break
    return out


# ═══════════════════════════════════════════════════════════════

def run_stage(label, cmd):
    print(f"\n{'='*60}\n{label}\n{'='*60}")
    r = subprocess.run([sys.executable] + cmd, cwd=HERE)
    if r.returncode != 0:
        print(f"\n{label} 失败(退出码 {r.returncode}),停在这里。", file=sys.stderr)
        sys.exit(r.returncode)


def main():
    ap = argparse.ArgumentParser(
        description="一条命令跑完全程,全程本地模型,不依赖 Claude / Codex")
    ap.add_argument("--days", type=int, default=DEFAULT_DAYS, help="抓最近几天")
    ap.add_argument("--top", type=int, default=DEFAULT_TOP, help="写前几篇")
    ap.add_argument("--skip-fetch", action="store_true", help="不抓,用已有的")
    ap.add_argument("--skip-screen", action="store_true", help="不筛,用已有的清单")
    ap.add_argument("--endpoint", default=sp.DEFAULT_ENDPOINT)
    ap.add_argument("--model", default=sp.DEFAULT_MODEL)
    ap.add_argument("--only", choices=["wechat", "en", "xhs", "cover"],
                    action="append", help="只出其中某几样(可重复)")
    ap.add_argument("--dry-run", action="store_true",
                    help="只列出会写哪几篇,不调模型")
    args = ap.parse_args()

    want = set(args.only or ["wechat", "en", "xhs", "cover"])
    if want - {"wechat"} and "wechat" not in want:
        want.add("wechat")                   # 后三样都要先有正文

    # 筛选本身就要本地模型,所以 --dry-run 也得先探一下 —— 除非连筛选都跳过。
    # 早期版本只在要写稿时才探,结果 --dry-run 抓完 200 条才在筛选那步炸。
    needs_model = (not args.skip_screen) or (not args.dry_run)
    if needs_model and not preflight(args.endpoint, args.model):
        return 1

    if not args.skip_fetch:
        run_stage("① 抓取", ["fetch_papers.py", "--days", str(args.days)])
    if not args.skip_screen:
        run_stage("② 筛选", ["screen_papers.py",
                            "--endpoint", args.endpoint, "--model", args.model])

    path = wd.newest_input()
    if not path:
        print("screened/ 里没有清单,先跑一次筛选。", file=sys.stderr)
        return 1

    picks = pick_top(path, args.top)
    print(f"\n{'='*60}\n③ 写稿 —— 本地模型\n{'='*60}")
    print(f"清单:{os.path.basename(path)}　取总榜前 {len(picks)} 篇\n")
    for i, r in enumerate(picks, 1):
        print(f"  {i}. [{r.get('total')}] {r.get('title','')[:66]}")
    if args.dry_run:
        print("\n--dry-run:没有调模型。")
        return 0

    stamp = dt.date.today().isoformat()
    covers, failures = [], []
    for i, rec in enumerate(picks, 1):
        title = rec.get("title", "")[:60]
        print(f"\n── {i}/{len(picks)} {title}")
        t0 = time.time()
        try:
            article = gen_article(rec, args.endpoint, args.model)
        except Exception as e:
            print(f"   中文稿失败:{e}", file=sys.stderr)
            failures.append((title, str(e)))
            continue
        slug = article_slug(article, rec)
        print(f"   中文稿 {len(article)} 字　{time.time()-t0:.0f}s"
              f" → {save(os.path.join(DRAFT_DIR, f'{stamp}_{slug}_公众号.md'), article)}")
        for note in warn_article(article):
            print(f"   ⚠ {note}")

        steps = [
            ("en", "英文稿", lambda: gen_en(rec, article, args.endpoint, args.model),
             os.path.join(EN_DIR, f"{stamp}_{slug}_EN.md")),
            ("xhs", "导流笔记", lambda: gen_xhs(rec, article, args.endpoint, args.model),
             os.path.join(DRAFT_DIR, f"{stamp}_{slug}_小红书导流.md")),
        ]
        for tag, label, fn, out in steps:
            if tag not in want:
                continue
            try:
                text = fn()
                print(f"   {label} {len(text)} 字 → {save(out, text)}")
            except Exception as e:
                print(f"   {label}失败:{e}", file=sys.stderr)
                failures.append((f"{title} / {label}", str(e)))

        if "cover" in want:
            try:
                spec = gen_cover(rec, article, args.endpoint, args.model)
                if spec:
                    p = save(os.path.join(COVER_DIR, f"{stamp}_{slug}.json"),
                             json.dumps(spec, ensure_ascii=False, indent=2))
                    covers.append(p)
                    print(f"   首图内容 → {p}")
                else:
                    print("   首图内容:模型没给出可用的 JSON,跳过", file=sys.stderr)
            except Exception as e:
                print(f"   首图失败:{e}", file=sys.stderr)
                failures.append((f"{title} / 首图", str(e)))

    if covers:
        run_stage("④ 渲染首图", ["make_cards.py"] + covers)

    print(f"\n{'='*60}\n跑完了\n{'='*60}")
    print(f"中文稿 → drafts\\　英文稿 → drafts_en\\　首图 → cards_html\\")
    if failures:
        print(f"\n有 {len(failures)} 处没成:")
        for what, why in failures:
            print(f"  · {what}:{why}")
    print("\n本地模型写的稿子发之前一定自己过一遍 —— 数字、菌株名、"
          "动物实验有没有被写成「研究证明」。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
