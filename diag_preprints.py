#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
diag_preprints.py — 查清楚预印本为什么一条都没抓到

上一次 --days 14 抓了 339 条,is_preprint 全是 False。但 Europe PMC 同期
光是带摘要的预印本就有一万多条,说明是被我们自己的过滤条件挡掉了。

这个脚本把 FILTERS 里的条件一条条加上去,看 hitCount 在哪一步掉到 0。

    py diag_preprints.py
    py diag_preprints.py --days 14
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


def hits(query):
    url = BASE + "?" + urllib.parse.urlencode(
        {"query": query, "format": "json", "resultType": "idlist", "pageSize": 1})
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode()).get("hitCount")
    except Exception as e:
        return f"错误 {type(e).__name__}"


def ladder(title, base_query, date_clause):
    print(f"\n── {title}")
    steps = [
        ("裸查询",                          base_query),
        ("+ 日期",                          f"({base_query}) AND {date_clause}"),
        ("+ 日期 + SRC:PPR",                f"({base_query}) AND {date_clause} AND (SRC:PPR)"),
        ("+ 日期 + SRC:PPR + HAS_ABSTRACT", f"({base_query}) AND {date_clause} AND (SRC:PPR) AND HAS_ABSTRACT:Y"),
        ("+ 日期 + SRC:PPR + HAS_ABSTRACT + LANG:eng",
                                            f"({base_query}) AND {date_clause} AND (SRC:PPR) AND HAS_ABSTRACT:Y AND LANG:eng"),
    ]
    prev = None
    for label, q in steps:
        n = hits(q)
        drop = ""
        if isinstance(n, int) and isinstance(prev, int) and prev > 0:
            if n == 0:
                drop = "   ← 就是这一条把预印本全挡了"
            elif n < prev * 0.5:
                drop = f"   ← 砍掉了 {(1 - n / prev) * 100:.0f}%"
        print(f"   {str(n):>8}  {label}{drop}")
        prev = n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=14)
    args = ap.parse_args()

    today = dt.date.today()
    date_clause = (f"(FIRST_PDATE:[{(today - dt.timedelta(days=args.days)).isoformat()}"
                   f" TO {today.isoformat()}])")
    print(f"日期区间:{date_clause}")

    # 不带任何主题词,单看预印本总量 —— 排除"就是没有相关预印本"这种可能
    ladder("所有预印本(不限主题)", "*:*", date_clause)

    # 我们实际用的主题查询
    sys.path.insert(0, ".")
    import fetch_papers as fp
    ladder("probiotics_gut(实际查询式)", fp.TOPICS["probiotics_gut"], date_clause)

    print("\n── 参考:同一主题的正式发表(SRC:MED)")
    q = (f"({fp.TOPICS['probiotics_gut']}) AND {date_clause} AND (SRC:MED) "
         f"AND HAS_ABSTRACT:Y AND LANG:eng")
    print(f"   {hits(q):>8}  完整过滤")

    print("\n看哪一行掉到 0,就是哪个条件的问题。把整个输出贴给我。")


if __name__ == "__main__":
    main()
