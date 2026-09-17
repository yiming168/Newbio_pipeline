#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
diag_europepmc.py — 排查 Europe PMC 返回 503 到底是谁在拦

依次换不同的请求头/URL 发同一条最小查询,把每次的状态码、响应头里
能指认拦截者的字段(Server / Retry-After / CF-Ray / X-Cache 等)和响应体
开头打出来。只用标准库。

    py diag_europepmc.py
"""

import json
import os
import socket
import ssl
import sys
import urllib.error
import urllib.request

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

BASE = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
Q_MIN = BASE + "?query=probiotic&format=json&pageSize=1"
Q_CORE = BASE + "?query=probiotic&format=json&resultType=core&pageSize=1"

BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")
_EMAIL = os.environ.get("EUROPEPMC_CONTACT_EMAIL", "anonymous@example.com")
SCRIPT_UA = f"newbio-health-pipeline/1.0 (mailto:{_EMAIL})"

TELLTALE = ["server", "retry-after", "cf-ray", "cf-cache-status", "x-cache",
            "x-served-by", "via", "x-ratelimit-remaining", "content-type"]

CASES = [
    ("A  脚本当前的头(UA=newbio-health-pipeline)", Q_MIN,
     {"User-Agent": SCRIPT_UA, "Accept": "application/json"}),
    ("B  Python 默认头(完全不设 UA)", Q_MIN, None),
    ("C  浏览器 UA", Q_MIN,
     {"User-Agent": BROWSER_UA, "Accept": "application/json"}),
    ("D  浏览器 UA + 完整浏览器头", Q_MIN,
     {"User-Agent": BROWSER_UA,
      "Accept": "application/json,text/plain,*/*",
      "Accept-Language": "en-US,en;q=0.9",
      "Accept-Encoding": "identity",
      "Connection": "close"}),
    ("E  脚本头 + resultType=core(实际用的那条)", Q_CORE,
     {"User-Agent": SCRIPT_UA, "Accept": "application/json"}),
    ("F  站点根目录(看整个 ebi.ac.uk 是不是都 503)", "https://www.ebi.ac.uk/",
     {"User-Agent": BROWSER_UA}),
]


def dns_check():
    print("── DNS ───────────────────────────────────────────")
    for host in ("www.ebi.ac.uk", "europepmc.org"):
        try:
            infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
            ips = sorted({i[4][0] for i in infos})
            print(f"  {host:16} → {', '.join(ips)}")
        except Exception as e:
            print(f"  {host:16} → 解析失败 {type(e).__name__}: {e}")
    print()


def show_headers(hdrs):
    for k in TELLTALE:
        v = hdrs.get(k)
        if v:
            print(f"       {k}: {v}")


def run(name, url, headers):
    print(f"── {name}")
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read(600)
            print(f"     [{resp.status}] 成功")
            show_headers(resp.headers)
            if url != "https://www.ebi.ac.uk/":
                try:
                    print(f"       hitCount = {json.loads(body.decode()).get('hitCount')}")
                except Exception:
                    print(f"       响应体开头:{body[:160].decode('utf-8', 'replace')}")
            return True
    except urllib.error.HTTPError as e:
        body = e.read(600).decode("utf-8", "replace").replace("\n", " ")
        print(f"     [{e.code}] {e.reason}")
        show_headers(e.headers)
        print(f"       响应体开头:{body[:300]}")
    except (urllib.error.URLError, ssl.SSLError, TimeoutError, OSError) as e:
        print(f"     [连不上] {type(e).__name__}: {e}")
    return False


def main():
    print(f"Python {sys.version.split()[0]}   OpenSSL {ssl.OPENSSL_VERSION}\n")
    dns_check()
    results = []
    for name, url, headers in CASES:
        results.append((name, run(name, url, headers)))
        print()

    print("── 结论 ──────────────────────────────────────────")
    ok = [n for n, r in results if r]
    bad = [n for n, r in results if not r]
    if not ok:
        print("  全部失败 → 不是请求头的问题,是你到 ebi.ac.uk 这条网络路径")
        print("    先在浏览器里打开这个地址看看:")
        print(f"    {Q_MIN}")
        print("    浏览器能打开而脚本不行 → 大概率是本机代理/VPN/安全软件只放行浏览器")
        print("    浏览器也打不开 → ISP 或 DNS 层面,换手机热点测一下就能确认")
    elif bad:
        print("  有成功有失败 → 是请求头的问题,能过的是:")
        for n in ok:
            print(f"    {n}")
        print("  把 fetch_papers.py 里的 User-Agent 换成能过的那组即可。")
    else:
        print("  全部成功 → 刚才那波 503 是临时的,直接重跑 fetch_papers.py")
        print("  但重试策略还是该加固(503 值得等更久),跟我说一声我改。")


if __name__ == "__main__":
    main()
