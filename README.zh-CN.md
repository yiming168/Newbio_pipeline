# Research-to-Content Pipeline(中文版)

> English version: [README.md](README.md)

抓最新科研文献 → 本地模型筛选 → 输出两份东西:**选题清单**(写稿用)和**原料雷达**(进口生意用)。

不自动发布。终点是两份排好的 Markdown,躺在 `screened\` 里等你挑。

---

## 一、每周怎么跑

前提:`start_server.bat` 已经在跑,且那个窗口开着(llama-server 监听 `localhost:8080`,`screen_papers.py` 默认连它)。

```
cd /d %PIPELINE_DIR%

py fetch_papers.py --days 7
py screen_papers.py
```

完事。产出三个文件,时间戳相同:

| 文件 | 是什么 |
|---|---|
| `screened\YYYY-MM-DD_HHMM.md` | **选题清单** —— 按主题分桶,每桶前 N 条 |
| `screened\YYYY-MM-DD_HHMM_radar.md` | **原料雷达** —— 按原料归并,不按论文 |
| `screened\YYYY-MM-DD_HHMM.jsonl` | 全部条目的完整评分,前两份是从它渲染的 |

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

### 四段式,目前做完两段

```
① 取材    fetch_papers.py    Europe PMC → out\*.jsonl
② 筛选    screen_papers.py   本地模型逐条打分 → screened\*.md + *_radar.md
③ 写稿    还没做              调 API 用前沿模型生成公众号稿 + 小红书稿
④ 配图    还没做              HTML/CSS 模板 + Playwright 截图
```

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
py test_pipeline.py                                  153 项检查
py test_pipeline.py -v                               连通过的也列出来

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
