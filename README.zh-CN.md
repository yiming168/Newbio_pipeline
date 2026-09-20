# Research-to-Content Pipeline(中文版)

> English version: [README.md](README.md)

抓最新科研文献 → 本地模型筛选 → 出**选题清单**(写稿用)和**原料雷达**(进口生意用)
→ 挑一条生成**提示词包**,粘进 Claude/Codex,一次产出三样:
**中文公众号稿** + **英文行业资讯稿**(给 Newbio 官网) + **CSS 首图**。

不自动发布,也不调写稿 API。**流水线停在「需要编辑判断」的地方。**

---

## 目录里每个文件是干什么的

**这个表是权威的。没列进来的文件,就是可以删的。**

**四个阶段**,按顺序跑:

| 文件 | 阶段 | 干什么 |
|---|---|---|
| `fetch_papers.py` | ① 取材 | 按 4 组主题查 Europe PMC,用 SQLite 去重,写出 `out/*.jsonl` |
| `screen_papers.py` | ② 筛选 | 本地模型两遍打分,产出 `screened/` 里的选题清单和原料雷达 |
| `write_draft.py` | ③ 出提示词 | 把选中的一篇变成提示词包,写到 `prompts/`,粘给 Claude/Codex |
| `make_cards.py` | ④ 出首图 | 把封面 JSON 套进固定 CSS 模板,渲染成 `covers/` 里的图 |

**配套的:**

| 文件 | 干什么 | 什么时候用得上 |
|---|---|---|
| `test_pipeline.py` | 284 个离线测试,用假服务器顶替 Europe PMC 和模型 | 改完任何东西都跑一遍。不联网、不用起模型、不用 API key |
| `download_model.py` | 断点续传下载 GGUF 权重(约 18G) | 第一次装,或者换模型 |
| `start_server.bat` | 用适配这块显卡的 MoE / CPU 卸载参数起 `llama-server` | 每次要跑 ② 之前 |
| `diag_europepmc.py` | 直接打 Europe PMC 接口,把原始状态码打出来 | **只在 ① 抓不到东西时用。**Europe PMC 会成片返回 503,这个脚本用来区分「是它挂了」还是「我代码错了」 |
| `seen.sqlite3` | 去重账本,记着抓过的每一个文献 ID | 不要手工改。**删了它,下次会把已经看过的全部重抓一遍** |

**目录全部是产物,没有一个进 git:**

| 目录 | 谁产的 | 里面是什么 |
|---|---|---|
| `out/` | ① | 抓下来的原始记录,一行一条 JSON |
| `screened/` | ② | 选题清单 `.md`、同样内容的 `.jsonl`、原料雷达 `_radar.md`。**`.md` 表头写着它是哪个 `SCORING_VERSION` 打出来的** |
| `prompts/` | ③ | 提示词包,可以直接粘 |
| `covers/` | ④ | 渲染好的 2.35:1 公众号首图 |
| `cards_html/` | ④ | 每张首图背后的中间 HTML。**留着是因为调版式时看它就够了,不用重新跑模型** |
| `drafts/` | 手写 | 定稿的中文公众号文章 |
| `drafts_en/` | 手写 | 定稿的英文行业资讯稿 |
| `manual/` | 手填 | 不走抓取、直接手工录入的选题 —— 值得写但最近没有对应文献的话题。`_模板.json` 是字段模板 |
| `cards/` | **已停产** | 早期小红书 3:4 竖版卡片的内容 JSON。留作模板化排版的参考,不再生成 |

只在本机、永远不提交的:`.env`(API key)、`HANDOFF.md`(踩坑记录,里面有公司和客户名)、`__pycache__/`。

---

## 一、每周怎么跑

前提:`start_server.bat` 已经在跑,且那个窗口开着(llama-server 监听 `localhost:8080`,`screen_papers.py` 默认连它)。

```
cd /d %PIPELINE_DIR%

py fetch_papers.py --days 7          :: 抓一周,约 200 条
py screen_papers.py                  :: 打分,约 20 分钟
py write_draft.py --list             :: 看有哪些选题
py write_draft.py --pick 1           :: 出提示词包
```

然后打开 `prompts\` 里那个文件,**一段一段粘进 Claude 或 Codex**:

- **第 1 段** → 中文公众号稿,存进 `drafts\`
- **第 2 段** → **英文行业资讯稿**(给 Newbio 官网),存进 `drafts_en\`
- **第 3 段** → 首图内容 JSON,存成 `covers\<日期>_<短名>.json`

最后渲染首图:

```
py make_cards.py covers/2026-09-17_xxx.json
```

浏览器打开 `cards_html\` 里的 HTML,**右键截图**(或 F12 选中 `.cover`
元素 →「Capture node screenshot」,拿到的是精确的 1410×600,2.35:1)。

### 英文稿不是中文稿的翻译

两个受众问的是不同的问题:

| | 中文公众号 | 英文行业稿 |
|---|---|---|
| 给谁看 | 中国消费者 | 原料采购、配方师、同行 |
| 回答什么 | 「我该怎么做」 | 「这对这个品类意味着什么」 |
| 长度 | 1200-2000 字 | 350-550 words |

**更要紧的是合规。公司官网上的内容在监管眼里是广告,不是媒体** ——
同一句话发公众号是科普,挂在 Newbio 网站上就可能被当成产品宣称。
所以英文稿的规矩更严,提示词里写死了六条,第一条就是
**绝不能把文献结论跟自家产品挂钩**(不许有「这正是我们……」这类收尾)。

产物对照:

| 文件 | 是什么 |
|---|---|
| `screened\*.md` | **选题清单** —— 按主题分桶,每桶前 N 条 |
| `screened\*_radar.md` | **原料雷达** —— 按原料归并,不按论文 |
| `screened\*.jsonl` | 全部条目的完整评分,前两份是从它渲染的 |
| `prompts\*.md` | **提示词包** —— 粘进 Claude/Codex 用 |
| `covers\*.json` | 首图内容(AI 产出,我存下来) |
| `cards_html\*_首图.html` | 首图**成品**,浏览器打开截图即用 |
| `drafts\*.md` | 中文公众号稿 |
| `drafts_en\*.md` | 英文行业资讯稿 |

一周约 160-220 条,打分 20 分钟左右(两次调用)。只要选题清单的话
加 `--no-radar`,约 14 分钟。

### 首次 / 换机器

```
:: 先设三个环境变量(设完要新开终端窗口)
setx EUROPEPMC_CONTACT_EMAIL "you@example.com"
setx LLAMA_DIR               "D:\path\to\llama.cpp"
setx LLAMA_MODELS_DIR        "D:\path\to\llama.cpp\models"
setx PIPELINE_DIR            "G:\path\to\pipeline"

py download_model.py          # 下模型(18GB,断了重跑会续传)
:: 然后双击 start_server.bat
py screen_papers.py --selftest
py fetch_papers.py --selftest
```

邮箱和路径都走环境变量,没有硬编码 —— 这个仓库是公开的。

---

## 二、原理

### 四段式

```
① 取材    fetch_papers.py    Europe PMC → out\*.jsonl
② 筛选    screen_papers.py   本地模型逐条打分 → screened\*.md + *_radar.md
③ 写稿    write_draft.py     出提示词包(3 段)→ 我粘进 Claude/Codex 写
④ 首图    make_cards.py      首图内容 JSON → 固定模板渲染成 2.35:1 图
```

**第三段为什么不做成脚本。** 写过调 API 的版本,四家可切换,能跑,还是撤了。
原因不是技术:写稿最需要编辑判断和对话上下文,而这恰恰是批处理脚本最不擅长的;
我本来就在用 Claude/Codex,多一层 API 调用没有收益。代码还留在 `--api` 后面,不用。

**但第二段的判断会跟着进第三段。** `evidence: 2` 会变成提示词里的
「这是在动物身上做的,文章必须说明」;一篇摘要里写 we propose 的综述会触发
「不要把假说当定论写」的警告。**便宜的本地模型约束贵的那个。**

### 首图:AI 只出内容,排版归模板

**不让 AI 画图,也不让它写 HTML。** 它出一个内容 JSON(kicker / title / subtitle),
本地 `make_cards.py` 套固定模板渲染。

两个理由:**首图上的标题必须一字不错**,而扩散模型写不准中文,出来的是
「看起来像字」的像素;另外**模板给你免费的一致性** —— AI 每次现写 HTML,
这批配色字号一套、下批又一套,连着发几期一看就是散的。

规格 1410×600(2.35:1)。`**双星号**` 渲染成强调色,`\n` 换行 ——
刻意只支持这两种标记,格式多了每期就会长得不一样。
想换风格改 `make_cards.py` 顶部的 `THEME`,**所有图跟着变**。

小红书那条线不做了。渲染器留着(`make_cards.py --cards`),已生成的卡片还能重渲染。

### ① 取材的原理

从 Europe PMC 的 REST 接口抓,`resultType=core` 让摘要直接跟着 JSON 回来,不用像 PubMed E-utilities 那样再发一次请求解析 XML。只用 Python 标准库。

**四个主题**(查询式在 `fetch_papers.py` 顶部的 `TOPICS`):

| 主题 | 抓什么 | 服务于 |
|---|---|---|
| `probiotics_gut` | 人的肠道菌群、益生菌 | 写稿主线 |
| `pet_nutrition` | 猫狗营养 | 写稿主线 |
| `functional_ingredients` | 「某种物质 × 人体研究」 | **原料雷达** |
| `animal_feed` | 养殖端饲料添加剂 | 行业情报 |

**关键设计:查询式只负责召回,不负责精度。** 精筛是第二段模型的活。别为了提高精度去收紧查询式 —— 试过,失败了。「小鼠暴露于锑后的肠道菌群」和「益生菌改善成人睡眠」都真的是肠道菌群研究,任何检索语法都分不开,必须读懂摘要才行。

**`functional_ingredients` 一个原料名都不列。** 它抓的是句式:标题里有 `supplementation`/`extract`/`nutraceutical` 之类,或者作者关键词里有 `dietary supplement`/`plant extract` 之类,再加上人体线索(`adults`/`women`/`healthy`/`placebo`)。**具体是南非醉茄还是岩藻多糖,由第二段的模型从摘要里抽出来。** 这么设计的理由:你想得到名字的原料多半已经不新了,想不到的才值钱。

跨次去重用 `seen.sqlite3`,记的是 `source:id`。

### ② 筛选的原理

**先规则粗筛**(不花模型算力):摘要短于 300 字符的丢掉,标题规范化后前 60 字符重复的丢掉。

**再让本地模型读摘要,分两次调用**——选题一次,采购一次,刻意分开:

```
第一次(选题,8 个字段)
  relevance      跟读者有多大关系            0-5
  actionability  读者看完能做什么            0-5
  evidence       证据强度                    0-5
  novelty        是不是说了新东西            0-5
  hook / reason  切入角度、值不值得写
  subject / topic_fit

第二次(采购,3 个字段,提示词短一半)
  ingredient     核心原料英文名,不带修饰词(可能为空)
  sourcing       作为可进口原料的价值        0-5
  note           一句话说明
```

**为什么非拆不可:** 合并成一次调用时,模型是顺序生成的,先出的判断会条件化
后出的。字段顺序只能决定牺牲哪一边——采购在前,一篇优质综述被判"无商业转化
价值"、hook 留空,触发减半规则,**桶内第一名从 30.0 腰斩到 15.0**;选题在前,
采购判断被挤掉,**雷达从 9 个原料掉到 6 个**。拆开之后两边都不再打折,
雷达反而涨到 16 个。代价是多花三成时间(`--no-radar` 可以跳过第二次)。

**总分 = 相关×3 + 可操作×2 + 证据×2 + 新意×1**(满分 40)

然后两道折扣:
- `relevance ≤ 2` → 总分打三折(读者不关心的,证据再强也不该占选题位)
- `hook` 为空 → 再减半(模型自己都想不出怎么写)

打折的条目不会消失,`.md` 里会标 `⚠ 已打折(相关性只有 2,原始 13.0)`,你能判断折扣合不合理。

**最后按主题分桶,各取前 N。** 配额在 `TOPIC_QUOTA`:

```python
{"gut": 8, "supplement": 6, "pet": 6, "feed": 3, "other": 1}
```

分桶是为了解决一个根本问题:**让模型拿一把尺子比较「狗粮油脂氧化」和「地中海饮食与认知衰退」,这个要求本身就没定义好。** 它们不在一个可比空间里。分桶之后各主题只跟自己人比,跨主题的可比性问题就绕过去了。

### 原料雷达的原理

**跟选题清单是两个独立视角,同一篇论文在两边的价值经常相反。** 一篇「某新藻类提取物的 I 期人体试验」,读者用不上(选题价值低),但对采购可能极有价值。

所以雷达**不走 relevance 那道闸**,只看 `sourcing`:

```
先问:有没有一个明确的、能向供应商下单的单一原料?
没有(膳食模式、行为、笼统的"菌群")→ 0 分,不上榜
有的话按人体证据强度:
  5 = 人体 RCT/meta 支持,国内还不常见
  4 = 人体 RCT/meta 支持,国内已有但没做烂
  3 = 有人体数据,但样本小或只是观察性
  2 = 只有动物或体外数据 —— 拿这个去谈生意还太早
  1 = 大路货(维生素C、常见乳酸菌)
```

门槛 `RADAR_MIN_SOURCING = 3`,**动物数据进不来**。

另外有一条**代码强制的约束**:采购分不得超出证据分能支撑的档位
(`SOURCING_CAP_BY_EVIDENCE`)。拆成两次调用之后,采购那次看不到证据分,
会自己重新判一遍、判得不一致——实测出现过「采购 4 / 证据 1」。
**跨调用的一致性靠提示词叮嘱是不可靠的,锁在代码里才行。**
被压过档的会在雷达表里标 `↓`。

**雷达按原料归并,不按论文。** 同一个原料这期出现三篇,比任何单篇分数都更说明问题 —— 三组人同时在研究它,说明在升温。原料名保留英文原词(那是你要拿去搜供应商的)。

**雷达的价值是累积的。** 单期 20-30 个原料看不出什么,十期之后按频次排出来的那张表才是做进口该看的东西。

**国内法规准入(新食品原料目录、保健食品原料目录)雷达不作判断,自己核。**

---

## 三、想调什么改哪里

| 想干什么 | 改哪 |
|---|---|
| 换筛选方向、调读者画像 | `screen_papers.py` 的 `AUDIENCE` —— **这是整个筛选质量的核心旋钮,比任何参数都重要** |
| 各主题出几条 | `TOPIC_QUOTA` |
| 清单太短 / 太长 | `RELEVANCE_GATE`(2→1 放松,2→3 收紧) |
| 更看重证据 / 更看重可操作 | `WEIGHT_*` 四个权重 |
| 雷达想看早期信号 | `RADAR_MIN_SOURCING` 3→2(会放进只有动物数据的) |
| 首图配色 / 字号 | `make_cards.py` 顶部的 `THEME` 和 `CSS`,改一处所有图跟着变 |
| 英文稿的口径 / 合规 | `write_draft.py` 里的 `EN_BRIEF_PROMPT` |
| 首图要哪些字段 | `write_draft.py` 里的 `COVER_SPEC_PROMPT` |
| 采购分与证据分的绑定松紧 | `SOURCING_CAP_BY_EVIDENCE` |
| 输出被截断 | `MAX_TOKENS_SCORE` / `MAX_TOKENS_RADAR` |
| 加 / 改抓取主题 | `fetch_papers.py` 的 `TOPICS` |
| 换模型 | `start_server.bat` 里的 `MODEL`(或环境变量 `LLAMA_MODELS_DIR`);或 `--endpoint` + `--model` 切回 Ollama |
| 显存不够 / 有富余 | `start_server.bat` 里的 `NCPUMOE`(大=省显存但慢) |

**每次改评分口径,把 `SCORING_VERSION` 往上加一版。** 它会写进 `.md` 表头和 `jsonl` 每一行 —— 过两周翻出一份旧清单,你能立刻知道它是哪套标准打出来的。

---

## 四、常用命令

```
:: 抓取
py fetch_papers.py --days 7                          日常
py fetch_papers.py --days 7 --dry-run --show 25      只看不写,调查询式用
py fetch_papers.py --topic probiotics_gut --dry-run   单个主题
py fetch_papers.py --selftest                        验接口通不通

:: 筛选
py screen_papers.py                                  日常
py screen_papers.py --limit 10                       先打 10 条计时
py screen_papers.py --top 3                          每个主题只列 3 条
py screen_papers.py --workers 3                      并发(显存紧就设 1)
py screen_papers.py --no-radar                       跳过采购那次调用,快三成
py screen_papers.py --selftest                       验模型接口
py screen_papers.py --endpoint http://localhost:11434/v1 --model qwen3:14b   切 Ollama

:: 测试(完全离线,不联网、不需要模型,mock server 顶替两头)
py test_pipeline.py                                  284 项检查
py test_pipeline.py -v                               连通过的也列出来

:: 写稿与配图
py write_draft.py --list                             看有哪些选题
py write_draft.py --pick 1                           出提示词包(ChatGPT 版)
py write_draft.py --manual manual/xxx.json           手工选题
py make_cards.py covers/xxx.json                     渲染公众号首图
py make_cards.py cards/xxx.json --cards              (旧)小红书卡片组

:: 出问题时
py diag_europepmc.py                                 Europe PMC 报错时,查是谁在拦
py diag_preprints.py                                 预印本抓不到时,逐条加过滤条件看在哪归零
py diag_strategies.py --only S7 S8 --show 10         试新的召回策略
```

---

## 五、故障排查

**`py screen_papers.py --selftest` 连不上** —— llama-server 窗口还开着吗?启动时带 `--jinja` 了吗?没带的话关思考的参数不生效。

**Europe PMC 报 503** —— 跑 `py diag_europepmc.py`。它用六组不同的请求头试同一条查询,并打印响应体开头(503 页面通常写着是谁挡的)。六组全绿就是服务端临时抽风,重跑即可 —— 现在重试策略会读 `Retry-After` 头,最多扛 63 秒。

**`llama-server.exe` 说 `unknown argument --n-cpu-moe`** —— 构建太旧。把 `start_server.bat` 里那行换成 `-ot "\.ffn_.*_exps\.=CPU"`,或者更新 llama.cpp。

**有条目报「输出被截断」** —— 调大 `MAX_TOKENS_SCORE`(或雷达那边的
`MAX_TOKENS_RADAR`)。中文 token 密度高,长 hook 容易顶到上限。
解析失败现在会区分「被截断」和「答非所问」,两者处理方式不同。

**打分结果每次不一样** —— `SAMPLING` 里的 `temperature` 被调高了。打分任务要的是可复现,0.2 是对的档位;`presence_penalty` 必须是 0(它惩罚重复 token,而 JSON 的键名天生重复)。

**清单里全是宠物 / 全是人体** —— 不是权重问题,是 `TOPIC_QUOTA` 配额。各桶独立,互不抢名额。

**某个主题突然 0 条** —— 先把 `fetch_papers.py` 里的 `FILTERS` 临时改成 `""` 试试,多半是过滤条件出了问题(有过先例:`LANG:eng` 曾经把预印本全挡了)。
