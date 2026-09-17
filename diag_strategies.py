#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
diag_strategies.py — 对比几种"不列举原料名"的召回策略

    py diag_strategies.py                 # 全部策略,每个看 6 条标题
    py diag_strategies.py --show 10
    py diag_strategies.py --only S7 S8    # 只跑指定的
    py diag_strategies.py --days 7

看三件事:
  1. hitCount 在不在 200-800 这个可处理区间
  2. 抽样标题里有多少是"普通人能用上的"
  3. MeSH 到底能不能用(脚本末尾有语法探针)
"""

import argparse
import datetime as dt
import json
import os
import sys
import urllib.parse
import urllib.request

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

BASE = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
_EMAIL = os.environ.get("EUROPEPMC_CONTACT_EMAIL", "anonymous@example.com")
UA = f"newbio-health-pipeline/1.0 (mailto:{_EMAIL})"

FILTERS = "((SRC:MED AND LANG:eng) OR SRC:PPR) AND HAS_ABSTRACT:Y"

# ─────────────────────────────────────────────────────────────
# 候选策略。每条都不点具体原料名。
# 实测记录(2026-09-17,14 天窗口):
#   S1   3 条   MeSH 概念            —— 作废,见末尾语法探针
#   S2  54 条   研究类型 × 健康词     —— 量少,且主体是临床人群
#   S3 274 条   干预 × 结局          —— 量合适,精度一般
#   S4  65 条   S2 ∪ S3(窄版)
#   S5   1 条   MeSH × 研究类型      —— 作废
#   S6 217 条   运动/冥想/睡眠        —— 量合适,但临床人群占大头
#   S7  45 条   物质 × 人体试验       —— 精度极高但量少,被 PUB_TYPE 滞后拖累
#   S8 206 条   生活方式(排临床)     —— 采用
#
# MeSH 探针结论(2026-09-17):
#   MESH:"Exercise" 语法正确(一年前窗口 442 条,对照 TITLE 646 条),
#   引号必须带。但最近 14 天只有 1-3 条 —— PubMed 人工标注要几个月才跟上。
#   所以 MeSH 不能用于"最新文献"流水线,但适合做回溯扫描。
#   同理 PUB_TYPE 也是人工标注,新文献同样没有 —— 不要依赖它做时效性检索。
#   KW:(作者投稿时自填的关键词)不经人工标注,理论上无滞后,见 S10。
# ─────────────────────────────────────────────────────────────

STRATEGIES = {
"S1": ("MeSH 概念(靠标注展开,不列举)", '''
(MESH:"Life Style" OR MESH:"Exercise" OR MESH:"Meditation" OR MESH:"Mindfulness"
 OR MESH:"Diet" OR MESH:"Dietary Supplements" OR MESH:"Sleep" OR MESH:"Health Behavior"
 OR MESH:"Probiotics" OR MESH:"Gastrointestinal Microbiome")
'''),

"S2": ("研究类型 + 宽泛健康词(高证据优先)", '''
(PUB_TYPE:"Randomized Controlled Trial" OR PUB_TYPE:"Meta-Analysis"
 OR PUB_TYPE:"Systematic Review")
AND (TITLE:diet OR TITLE:dietary OR TITLE:nutrition OR TITLE:supplement*
 OR TITLE:exercise OR TITLE:"physical activity" OR TITLE:training
 OR TITLE:sleep OR TITLE:meditation OR TITLE:mindfulness OR TITLE:lifestyle
 OR TITLE:microbiome OR TITLE:microbiota OR TITLE:probiotic*)
'''),

"S3": ("干预 × 结局(点读者关心的结果,不点手段)", '''
(TITLE:intervention OR TITLE:supplementation OR TITLE:intake OR TITLE:adherence
 OR TITLE:lifestyle OR TITLE:habit* OR TITLE:behaviour* OR TITLE:behavior*
 OR TITLE:training OR TITLE:exercise OR TITLE:diet)
AND (TITLE:sleep OR TITLE:mood OR TITLE:depression OR TITLE:anxiety OR TITLE:stress
 OR TITLE:cognition OR TITLE:cognitive OR TITLE:memory OR TITLE:weight OR TITLE:obesity
 OR TITLE:inflammation OR TITLE:longevity OR TITLE:ageing OR TITLE:aging
 OR TITLE:"blood pressure" OR TITLE:glucose OR TITLE:cholesterol OR TITLE:immunity
 OR TITLE:fatigue OR TITLE:"quality of life" OR TITLE:gut)
'''),

"S6": ("纯运动 / 冥想 / 睡眠", '''
(TITLE:exercise OR TITLE:"physical activity" OR TITLE:"resistance training"
 OR TITLE:"aerobic training" OR TITLE:walking OR TITLE:yoga OR TITLE:meditation
 OR TITLE:mindfulness OR TITLE:"breathing exercise*" OR TITLE:sleep
 OR TITLE:"sedentary behaviour" OR TITLE:"sedentary behavior")
AND (TITLE:health OR TITLE:outcome* OR TITLE:risk OR TITLE:trial OR TITLE:intervention
 OR TITLE:effect* OR TITLE:improve* OR TITLE:association)
'''),

# ── 新增:针对两个目的各配一条 ──────────────────────────
"S7": ("原料雷达专用:某种物质 × 人体试验(原料名交给模型抽)", '''
(TITLE:supplementation OR TITLE:supplement* OR TITLE:extract* OR TITLE:nutraceutical*
 OR TITLE:"functional food*" OR TITLE:postbiotic* OR TITLE:"bioactive compound*"
 OR TITLE:"novel food" OR TITLE:isolate OR TITLE:concentrate)
AND (TITLE:randomized OR TITLE:randomised OR TITLE:"double-blind" OR TITLE:placebo
 OR TITLE:crossover OR TITLE:trial OR TITLE:"meta-analysis" OR TITLE:"systematic review"
 OR PUB_TYPE:"Randomized Controlled Trial" OR PUB_TYPE:"Meta-Analysis")
NOT (TITLE:mice OR TITLE:mouse OR TITLE:rat OR TITLE:rats OR TITLE:"in vitro"
 OR TITLE:broiler* OR TITLE:piglet* OR TITLE:"laying hen*")
'''),

"S8": ("生活方式,排掉临床治疗语境(只留普通人群)", '''
(TITLE:exercise OR TITLE:"physical activity" OR TITLE:"resistance training"
 OR TITLE:walking OR TITLE:yoga OR TITLE:meditation OR TITLE:mindfulness
 OR TITLE:sleep OR TITLE:sedentary OR TITLE:"intermittent fasting"
 OR TITLE:"time-restricted" OR TITLE:lifestyle OR TITLE:circadian)
AND (TITLE:health OR TITLE:risk OR TITLE:outcome* OR TITLE:effect* OR TITLE:improve*
 OR TITLE:association OR TITLE:mortality OR TITLE:cognition OR TITLE:mood
 OR TITLE:metabolic OR TITLE:microbiome OR TITLE:microbiota OR TITLE:weight)
NOT (TITLE:patients OR TITLE:chemotherapy OR TITLE:rehabilitation OR TITLE:survivor*
 OR TITLE:stroke OR TITLE:"cerebral palsy" OR TITLE:preterm OR TITLE:dialysis
 OR TITLE:"intensive care" OR TITLE:surgery OR TITLE:"multiple sclerosis"
 OR TITLE:parkinson* OR TITLE:schizophrenia OR TITLE:"spinal cord")
'''),

# ── S7 放宽:去掉对 PUB_TYPE 的依赖(它跟 MeSH 一样是人工标注,同样滞后),
#    改用标题里的"人体研究"线索。很多原料试验的标题是
#    "Effects of X on Y in healthy adults" —— 没有 randomized 这个词,
#    但有 adults / women / healthy / participants。
"S9": ("S7 放宽:物质 × 人体线索(不靠 PUB_TYPE)", '''
(TITLE:supplementation OR TITLE:supplement* OR TITLE:extract* OR TITLE:nutraceutical*
 OR TITLE:"functional food*" OR TITLE:postbiotic* OR TITLE:probiotic* OR TITLE:synbiotic*
 OR TITLE:"bioactive compound*" OR TITLE:"novel food" OR TITLE:ingestion
 OR TITLE:"oral administration" OR TITLE:fortified OR TITLE:fortification)
AND (TITLE:randomized OR TITLE:randomised OR TITLE:"double-blind" OR TITLE:placebo
 OR TITLE:crossover OR TITLE:trial OR TITLE:"meta-analysis" OR TITLE:"systematic review"
 OR TITLE:adults OR TITLE:women OR TITLE:men OR TITLE:participants OR TITLE:volunteers
 OR TITLE:healthy OR TITLE:human* OR TITLE:subjects)
NOT (TITLE:mice OR TITLE:mouse OR TITLE:rat OR TITLE:rats OR TITLE:"in vitro"
 OR TITLE:broiler* OR TITLE:piglet* OR TITLE:"laying hen*" OR TITLE:calves
 OR TITLE:preterm OR TITLE:neonat*)
'''),

# ── S10:作者关键词路线。KW 是投稿时作者自己填的,不经人工标注 ——
#    理论上没有 MeSH/PUB_TYPE 那种滞后。这条要是成立,就是"不列举原料名"
#    的最佳答案:作者自己会把原料名写进关键词。
"S10": ("作者关键词 KW:(无标注滞后?)", '''
(KW:"dietary supplement" OR KW:"dietary supplements" OR KW:"functional food"
 OR KW:"functional foods" OR KW:nutraceutical OR KW:nutraceuticals
 OR KW:"bioactive compounds" OR KW:"plant extract" OR KW:"natural product"
 OR KW:supplementation OR KW:probiotics OR KW:prebiotics OR KW:postbiotics)
AND (TITLE:randomized OR TITLE:randomised OR TITLE:placebo OR TITLE:trial
 OR TITLE:adults OR TITLE:women OR TITLE:men OR TITLE:participants OR TITLE:healthy
 OR TITLE:human* OR TITLE:supplementation OR TITLE:effect*)
NOT (TITLE:mice OR TITLE:mouse OR TITLE:rat OR TITLE:rats OR TITLE:"in vitro")
'''),
}


def flat(q):
    return " ".join(q.split())


def query(q, page_size=1, result_type="idlist"):
    url = BASE + "?" + urllib.parse.urlencode(
        {"query": q, "format": "json", "resultType": result_type, "pageSize": page_size})
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        return {"__error__": f"{type(e).__name__}: {e}"}


def run(key, label, raw_q, date_clause, show):
    q = f"({flat(raw_q)}) AND {date_clause} AND {FILTERS}"
    print(f"\n{'='*78}\n{key}　{label}")
    d = query(q, page_size=show, result_type="core")
    if "__error__" in d:
        print(f"  [查询出错] {d['__error__']}")
        return
    n = d.get("hitCount")
    if n is None:
        print(f"  [异常] 没拿到 hitCount:{str(d)[:200]}")
        return
    verdict = ("量偏少" if n < 150 else "量合适" if n <= 900 else "量太大,打分跑不完")
    print(f"  命中 {n} 条　{verdict}\n")
    for r in ((d.get("resultList") or {}).get("result") or [])[:show]:
        src = ("预印本" if r.get("source") == "PPR"
               else (r.get("journalInfo") or {}).get("journal", {}).get("title", ""))
        print(f"   · {(r.get('title') or '').replace(chr(10), ' ')[:104]}")
        print(f"     {src}")


def mesh_syntax_probe():
    """上一轮 MeSH 在一年前的窗口也只有 3% 覆盖 —— 不像标注滞后,
    更像字段名写错了。用一年前的窗口(标注早该完成)把几种写法试清楚。"""
    print(f"\n{'='*78}\nMeSH 语法探针(一年前的窗口,排除标注滞后干扰)")
    today = dt.date.today()
    dc = (f"(FIRST_PDATE:[{(today - dt.timedelta(days=395)).isoformat()}"
          f" TO {(today - dt.timedelta(days=365)).isoformat()}])")
    for q, label in [
        ('MESH:"Exercise"',         'MESH: 带引号'),
        ('MESH:Exercise',           'MESH: 不带引号'),
        ('MESH_TERM:"Exercise"',    'MESH_TERM:'),
        ('MESH_HEADING:"Exercise"', 'MESH_HEADING:'),
        ('KW:"Exercise"',           'KW: 关键词'),
        ('TITLE:exercise',          '(对照)标题词'),
    ]:
        d = query(f"({q}) AND {dc} AND {FILTERS}")
        print(f"  {label:22} {str(d.get('hitCount', d.get('__error__', '?'))):>8}   {q}")
    print("\n  某一行跟对照组同数量级 → 那是正确写法")
    print("  全都很小 → 这个接口不支持 MeSH 检索,这条路作废")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--show", type=int, default=6)
    ap.add_argument("--only", nargs="*", help="只跑指定策略,如 --only S7 S8")
    ap.add_argument("--no-probe", action="store_true", help="跳过 MeSH 语法探针")
    args = ap.parse_args()

    today = dt.date.today()
    date_clause = (f"(FIRST_PDATE:[{(today - dt.timedelta(days=args.days)).isoformat()}"
                   f" TO {today.isoformat()}])")
    print(f"区间 {date_clause}　附加过滤 {FILTERS}")
    print("参照:现有五个主题合计约 480 条 / 14 天")

    for k in (args.only if args.only else list(STRATEGIES)):
        if k not in STRATEGIES:
            print(f"\n未知策略 {k},可用:{', '.join(STRATEGIES)}")
            continue
        run(k, *STRATEGIES[k], date_clause, args.show)

    if not args.no_probe:
        mesh_syntax_probe()


if __name__ == "__main__":
    main()
