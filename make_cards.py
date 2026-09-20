#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
make_cards.py — 把内容 JSON 渲染成小红书图文卡片(HTML,截图即用)

    py make_cards.py cards/xxx.json
    py make_cards.py cards/*.json
    py make_cards.py cards/xxx.json --open      # 渲染完顺便用默认浏览器打开

为什么是"JSON + 固定模板",而不是每次让 AI 直接写 HTML:
    模板给你免费的一致性。AI 每次现写 HTML,这批用这个配色、下批换一个,
    字号行距也不一样,连着发几期一看就是散的。
    内容由 AI 产出(它擅长),排版由模板控制(它不该每次重新发明)。
    这跟第四段"信息图用 HTML/CSS 而不是扩散模型"是同一个道理:
    要的是可复现,不是每次都惊喜。

输入 JSON 的结构见 cards/_模板.json。渲染出来的 HTML 用浏览器打开,
每张卡片是一个独立的 1080×1440 区块,右键保存或截图即可。
"""

import argparse
import glob
import html as _html
import io
import json
import os
import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, "cards_html")

CARD_W, CARD_H = 1080, 1440     # 小红书竖版 3:4
COVER_W, COVER_H = 1410, 600    # 公众号首图 2.35:1

# 配色。想换风格改这里就行,所有卡片跟着变 —— 这就是用模板的意义。
# 刻意避开"科技蓝 + 发光"那一套:健康科普要的是可信,不是未来感。
THEME = {
    "bg":       "#F7F5F0",   # 暖白,不刺眼
    "ink":      "#1F2421",   # 近黑的深墨绿,比纯黑柔和
    "ink_soft": "#5A635C",
    "accent":   "#2E6E52",   # 深绿,克制
    "accent_bg": "#E4EDE6",
    "warn_bg":  "#FBF0E4",
    "warn_ink": "#8A5A22",
    "rule":     "#DEDAD2",
}

CSS = """
* { margin:0; padding:0; box-sizing:border-box; }
body {
  background:#8E938F; padding:40px 0;
  font-family: "PingFang SC","Hiragino Sans GB","Microsoft YaHei","Source Han Sans SC",
               "Noto Sans CJK SC",sans-serif;
  -webkit-font-smoothing:antialiased;
}
.hint {
  max-width:%(w)spx; margin:0 auto 28px; padding:18px 24px; border-radius:12px;
  background:#fff; color:#333; font-size:15px; line-height:1.7;
}
.hint b { color:%(accent)s; }
.card {
  width:%(w)spx; height:%(h)spx; margin:0 auto 40px; background:%(bg)s; color:%(ink)s;
  padding:120px 88px 150px; position:relative; overflow:hidden;
  display:flex; flex-direction:column; justify-content:center;
}
/* 内容垂直居中。第一版让内容顶头排,结果每张下面空掉 60%%,像没做完。 */
.card > .kicker:first-of-type { margin-top:0; }
.idx {
  position:absolute; top:52px; right:72px;
  font-size:28px; color:%(ink_soft)s; letter-spacing:.08em;
}
.kicker {
  font-size:30px; color:%(accent)s; letter-spacing:.14em; margin-bottom:34px;
  font-weight:600;
}
.title { font-size:88px; line-height:1.24; font-weight:800; letter-spacing:-.01em; }
.title.sm { font-size:70px; }
.sub {
  font-size:42px; line-height:1.62; color:%(ink_soft)s; margin-top:40px; font-weight:400;
}
.body { font-size:44px; line-height:1.72; margin-top:44px; }
.body p + p { margin-top:30px; }
.em { color:%(accent)s; font-weight:700; }
.pill {
  display:inline-block; background:%(accent_bg)s; color:%(accent)s;
  font-size:30px; font-weight:700; padding:12px 26px; border-radius:999px;
  margin-bottom:34px;
}
.warn {
  background:%(warn_bg)s; color:%(warn_ink)s; border-radius:18px;
  padding:44px 46px; font-size:38px; line-height:1.7; margin-top:48px;
}
.warn b { font-weight:800; }
.foot {
  margin-top:52px; padding-top:44px; border-top:2px solid %(rule)s;
  font-size:34px; line-height:1.7; color:%(ink_soft)s;
}
.foot .lab { color:%(accent)s; font-weight:700; }
.src { font-size:32px; line-height:1.75; color:%(ink_soft)s; margin-top:26px; word-break:break-all; }
.src .t { color:%(ink)s; }
.tags { font-size:32px; line-height:1.85; color:%(accent)s; margin-top:34px; }
.brand {
  position:absolute; left:88px; bottom:60px;
  font-size:26px; color:%(ink_soft)s; letter-spacing:.1em;
}

/* 公众号首图。跟小红书卡片是两套尺寸,别混用 —— 2.35:1 很扁,
   竖版的字号和留白搬过来会溢出。 */
.cover {
  width:%(cw)spx; height:%(ch)spx; margin:0 auto 40px; background:%(bg)s; color:%(ink)s;
  padding:0 96px; position:relative; overflow:hidden;
  display:flex; flex-direction:column; justify-content:center;
}
.cover .kicker { font-size:26px; margin-bottom:22px; letter-spacing:.12em; }
.cover .ctitle { font-size:68px; line-height:1.22; font-weight:800; letter-spacing:-.01em; }
.cover .ctitle.sm { font-size:54px; }
.cover .csub { font-size:29px; line-height:1.6; color:%(ink_soft)s; margin-top:26px; max-width:1000px; }
.cover .cbrand {
  position:absolute; right:96px; bottom:44px;
  font-size:23px; color:%(ink_soft)s; letter-spacing:.12em;
}
.cover .bar {
  position:absolute; left:0; top:0; bottom:0; width:14px; background:%(accent)s;
}
""" % {"w": CARD_W, "h": CARD_H, "cw": COVER_W, "ch": COVER_H, **THEME}


def esc(t):
    return _html.escape(str(t or ""))


def rich(t):
    """内容里用 **粗体** 标重点,渲染成强调色;\n 换行。
    刻意只支持这两种标记 —— 格式多了每张卡片就会长得不一样,
    那就失去用模板的意义了。"""
    out, parts = [], esc(t).split("**")
    for i, p in enumerate(parts):
        out.append(f'<span class="em">{p}</span>' if i % 2 else p)
    return "".join(out).replace("\n\n", "<br><br>").replace("\n", "<br>")


def render_cover(spec):
    """公众号首图。只出一张,2.35:1。
    公众号读者是点进来读文章的,首图的任务是把标题说清楚 ——
    不需要小红书那种一张张翻的结构。"""
    t = spec.get("title") or ""
    long_title = len(t.replace("\n", "")) > 18
    return (f'<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">'
            f'<title>{esc(t)} · 公众号首图</title><style>{CSS}</style></head><body>'
            f'<div class="hint">公众号首图 <b>{COVER_W}×{COVER_H}</b>(2.35:1)。'
            f'右键截图,或 F12 选中 <code>.cover</code> 元素 →「Capture node screenshot」。</div>'
            f'<div class="cover"><div class="bar"></div>'
            f'<div class="kicker">{esc(spec.get("kicker", "Newbio Health"))}</div>'
            f'<div class="ctitle{" sm" if long_title else ""}">{rich(t)}</div>'
            f'<div class="csub">{rich(spec.get("subtitle"))}</div>'
            f'<div class="cbrand">NEWBIO HEALTH</div>'
            f'</div></body></html>')


def render(spec):
    total = 2 + len(spec.get("points", [])) + (1 if spec.get("caveat") else 0)
    n = [0]

    def idx():
        n[0] += 1
        return f'<div class="idx">{n[0]} / {total}</div>'

    cards = []

    # 封面
    cards.append(f"""<div class="card">{idx()}
  <div class="kicker">{esc(spec.get('kicker', 'Newbio Health'))}</div>
  <div class="title{'' if len(spec.get('title','')) <= 16 else ' sm'}">{rich(spec.get('title'))}</div>
  <div class="sub">{rich(spec.get('subtitle'))}</div>
  <div class="brand">NEWBIO HEALTH</div>
</div>""")

    # 要点
    for p in spec.get("points", []):
        cards.append(f"""<div class="card">{idx()}
  <div class="pill">{esc(p.get('tag', ''))}</div>
  <div class="title sm">{rich(p.get('heading'))}</div>
  <div class="body"><p>{rich(p.get('body'))}</p></div>
  <div class="brand">NEWBIO HEALTH</div>
</div>""")

    # 局限(可选)
    if spec.get("caveat"):
        cards.append(f"""<div class="card">{idx()}
  <div class="pill">这篇的局限</div>
  <div class="title sm">{rich(spec.get('caveat_heading', '先说清楚证据有多硬'))}</div>
  <div class="warn">{rich(spec.get('caveat'))}</div>
  <div class="brand">NEWBIO HEALTH</div>
</div>""")

    # 出处 + 标签
    s = spec.get("source", {})
    src_html = (f'<div class="src"><span class="t">{esc(s.get("title"))}</span><br>'
                f'{esc(s.get("journal"))}　{esc(s.get("date"))}<br>{esc(s.get("link"))}</div>')
    tags = " ".join(f"#{esc(t)}" for t in spec.get("tags", []))
    cards.append(f"""<div class="card">{idx()}
  <div class="pill">文献来源</div>
  <div class="title sm">{rich(spec.get('closing', '想自己查证?'))}</div>
  <div class="foot"><span class="lab">原文</span>{src_html}</div>
  <div class="tags">{tags}</div>
  <div class="brand">NEWBIO HEALTH</div>
</div>""")

    hint = (f'<div class="hint">共 <b>{total}</b> 张卡片,每张 <b>{CARD_W}×{CARD_H}</b>(小红书 3:4)。'
            f'<br>截图方法:浏览器里对每张卡片右键 →「截取屏幕截图」;'
            f'或按 F12 打开开发者工具,选中一张 <code>.card</code> 元素后右键 →「Capture node screenshot」,'
            f'这样拿到的就是精确尺寸。</div>')

    return (f'<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">'
            f'<title>{esc(spec.get("title"))} · 小红书卡片</title>'
            f'<style>{CSS}</style></head><body>{hint}{"".join(cards)}</body></html>')


def main():
    ap = argparse.ArgumentParser(description="把内容 JSON 渲染成小红书卡片 HTML")
    ap.add_argument("files", nargs="+", help="卡片 JSON,可以给多个或用通配符")
    ap.add_argument("--out", default=OUT_DIR, help=f"输出目录(默认 {os.path.basename(OUT_DIR)})")
    ap.add_argument("--cards", action="store_true",
                    help=f"出整组小红书卡片({CARD_W}×{CARD_H},3:4)。"
                         f"不加这个参数就只出一张公众号首图({COVER_W}×{COVER_H},2.35:1)")
    ap.add_argument("--cover", action="store_true",
                    help="(已是默认行为,保留兼容)")
    args = ap.parse_args()

    paths = [p for pat in args.files for p in (glob.glob(pat) or [pat])]
    paths = [p for p in paths if not os.path.basename(p).startswith("_")]
    if not paths:
        sys.exit("没找到输入文件。")

    os.makedirs(args.out, exist_ok=True)
    for p in paths:
        if not os.path.exists(p):
            print(f"[跳过] 找不到 {p}")
            continue
        with io.open(p, encoding="utf-8") as f:
            spec = json.load(f)
        # 首图是默认;小红书卡片组是 v2 之前的默认,现在退到 --cards 后面。
        # 改默认的原因:产出格式定为「中文公众号稿 + 英文行业稿 + CSS 首图」,
        # 小红书那条线不再走了,但已经生成的卡片还在,渲染器留着。
        want_cards = args.cards
        need = ("title", "points", "source") if want_cards else ("title",)
        missing = [k for k in need if not spec.get(k)]
        if missing:
            print(f"[跳过] {os.path.basename(p)} 缺字段:{', '.join(missing)}")
            continue
        stem = os.path.splitext(os.path.basename(p))[0]
        if not want_cards:
            out = os.path.join(args.out, stem + "_首图.html")
            io.open(out, "w", encoding="utf-8", newline="\n").write(render_cover(spec))
            print(f"1 张首图　{out}")
        else:
            out = os.path.join(args.out, stem + ".html")
            io.open(out, "w", encoding="utf-8", newline="\n").write(render(spec))
            n = 2 + len(spec["points"]) + (1 if spec.get("caveat") else 0)
            print(f"{n} 张卡片　{out}")


if __name__ == "__main__":
    main()
