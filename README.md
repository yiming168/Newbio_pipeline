# Research-to-Content Pipeline

A local-first pipeline that turns the week's new biomedical literature into two ranked shortlists: **article topics** for a Chinese-language health-science publication, and an **ingredient radar** for sourcing decisions.

Fetches from Europe PMC, scores every abstract with a locally-hosted LLM, and outputs Markdown. No publishing automation — the pipeline ends at a ranked list a human picks from.

```
py fetch_papers.py --days 7     #  ~220 papers  →  out/*.jsonl
py screen_papers.py             #  ~15 min      →  screened/*.md + *_radar.md
```

> **Note on language:** code comments and the LLM prompt are in Chinese. The prompt is
> functional — it defines a Chinese consumer audience and requires Chinese-language output.
> This README is the English entry point.

---

## The problem

Manually scanning new literature for a health-science publication is slow and inconsistent. Broad keyword searches return hundreds of papers a week; almost all are irrelevant, and relevance can't be decided from titles. A separate need — spotting novel functional-food ingredients worth importing — competes for the same reading time but has *opposite* selection criteria.

**Constraint:** all screening runs locally on a single 16GB consumer GPU. At ~4s per abstract, the pipeline can afford to read ~500 papers per run, not 50,000. So the design problem is not "find all health research" — it is *"which 500 are worth reading, and how do you rank them without a human in the loop."*

---

## Architecture

```
┌─────────────────┐   Europe PMC REST      ┌──────────────────┐
│ fetch_papers.py │ ─────────────────────► │  out/*.jsonl     │
│  4 query sets   │   SQLite dedup          │  ~220 / week     │
└─────────────────┘                         └────────┬─────────┘
                                                     │
                            ┌────────────────────────▼─────────────────────┐
                            │ screen_papers.py                             │
                            │  1. rule prefilter (length, near-dup title)  │
                            │  2. LLM scores each abstract → 6 fields      │
                            │  3. two independent rankings                 │
                            └───────┬──────────────────────────┬───────────┘
                                    │                          │
                          ┌─────────▼────────┐      ┌──────────▼──────────┐
                          │ Topic shortlist  │      │ Ingredient radar    │
                          │ bucketed by area │      │ grouped by compound │
                          └──────────────────┘      └─────────────────────┘
```

**Stack:** Python 3.11, standard library only (no pip dependencies for the core pipeline). Local inference via `llama.cpp` / `llama-server` exposing an OpenAI-compatible endpoint; currently Qwen3.6-35B-A3B (MoE, 3B active) at IQ4_NL with expert layers offloaded to CPU.

---

## Design decisions, and the measurements behind them

This section is the interesting part. Every decision below replaced an earlier one that measurement proved wrong.

### 1. Retrieval optimises recall, not precision

An early instinct was to tighten boolean queries until only relevant papers came back. That fails on a real example: *"gut microbiota in mice exposed to antimony"* and *"probiotics improve sleep in adults"* are both genuinely gut-microbiota research. No query syntax separates them — it requires reading the abstract. Queries now cast wide; the LLM does precision.

### 2. One filter clause was silently discarding every preprint

The filter read `(SRC:MED OR SRC:PPR) AND HAS_ABSTRACT:Y AND LANG:eng`. Preprint records generally carry no language metadata, so `LANG:eng` excluded all of them:

| Query | Hits (14 days) |
|---|---|
| Preprints with abstracts | **10,323** |
| …plus `LANG:eng` | **0** |

Rewritten as `((SRC:MED AND LANG:eng) OR SRC:PPR)`. Preprints now supply ~14% of intake, and one placed second in the first shortlist after the fix. The bug was invisible because an `is_preprint` field existed and was always `false` — nothing errored.

**Takeaway:** a field that is always empty looks like a data property, not a bug. Diagnostic scripts in this repo (`diag_preprints.py`) exist to test filter clauses one at a time and show where a count collapses to zero.

### 3. Don't enumerate what you want to discover

The ingredient radar originally searched a hand-written list of compounds (omega-3, lutein, milk thistle…). Two failures: ingredients not on the list could never be found — which is precisely the set worth finding — and precision was poor (90 hits, **2 usable**; `prebiotic` collides with prebiotic *chemistry* in origin-of-life papers, `collagen` collides with biomaterials and oncology).

Replaced with a *shape*-based query: title or author-keyword signals of a substance (`supplementation`, `extract`, `nutraceutical`) combined with human-study signals (`randomized`, `placebo`, `adults`, `healthy`). The compound name is extracted from the abstract by the LLM, so the pipeline surfaces ingredients nobody thought to search for.

### 4. Curated metadata fields are unusable for recent literature

MeSH terms would solve "don't enumerate" elegantly — `MESH:"Dietary Supplements"` explodes to every specific supplement. Measurement killed it:

| Window | `MESH:"Exercise"` | `TITLE:exercise` |
|---|---|---|
| One year ago | 442 | 646 |
| Last 14 days | 1–3 | 392 |

MeSH indexing is manual and lags by months; preprints never receive it. The same applies to `PUB_TYPE` — an early query depended on `PUB_TYPE:"Randomized Controlled Trial"` and under-returned for exactly this reason. Author-supplied `KW:` keywords carry no such lag and are used instead.

*(MeSH remains the right tool for a retrospective sweep. The finding is recorded rather than discarded.)*

### 5. Scoring must be reproducible, so sampling defaults are wrong for it

Following the model's published sampling recommendation (`temperature=0.7, presence_penalty=1.5`) produced a different ranking on every run. Two errors: 0.7 is too high for a classification task, and `presence_penalty` penalises repeated tokens — but JSON keys repeat by definition, so it actively degrades structured output.

At `temperature=0.2, presence_penalty=0`, two consecutive runs over the same input agreed on **10/10 items across all scoring dimensions** and produced an identical ordering.

### 6. Constrained decoding eliminated a whole failure class

Requesting `response_format: {"type":"json_object"}` makes `llama.cpp` compile a GBNF grammar, so malformed output becomes impossible rather than merely unlikely: **0 parse failures across 391 abstracts**, replacing a "failed to score" section that previously appeared in every report.

Backends vary in what optional fields they accept, so `chat()` degrades gracefully — on HTTP 400 it drops one optional field, remembers the rejection, and retries. The cost is one wasted round-trip on the first item, not on all 391.

### 7. The model compresses any absolute scale — so rank differently instead

Three successive rubric revisions tried to spread the scores. Each moved the peak without widening it:

| Dimension | Share of items in its two most-used levels |
|---|---|
| relevance | 72% |
| actionability | 69% |
| evidence | 76% |
| novelty | 79% |

This is how the model behaves on absolute 0–5 judgements, not a wording problem. Further rubric tuning was abandoned in favour of changing the ranking architecture — a gate plus per-topic buckets — which is what actually produced a usable shortlist.

### 8. Cross-category comparison is an ill-posed request

Asking one model to score *"lipid oxidation in extruded dog food"* and *"Mediterranean diet and cognitive decline"* on a single relevance scale produced a systematically skewed list: **16 pet items vs 3 human-subject items**, because concrete consumer-product papers score higher on "can the reader act on this" than dietary-pattern research.

Papers are now bucketed by topic and ranked only within their bucket, with an editorial quota per bucket. The comparability problem is sidestepped rather than solved.

### 9. Penalise overstated conclusions, not observational designs

A rule intended to penalise correlation-as-causation claims instead condemned an entire legitimate study class — human cohort studies, which can only ever report association. Papers with strong human evidence dropped from 42 to 16.

The rule now targets the mismatch between a study's design and the *strength of its wording*. A cohort study writing "associated with" is being appropriately careful and is not penalised. A useful signal: when authors themselves write *"correlated / suggesting / warranting further validation"*, they are flagging that the claim is unproven.

### 10. Two jobs, two rankings

Content selection and ingredient sourcing value the same paper differently — a Phase I trial of a novel algal extract is worthless as an article (readers can't buy or use it) and valuable as sourcing intelligence. A single rubric served neither. The radar bypasses the content relevance gate entirely and ranks on evidence strength, grouped by compound rather than by paper, because three papers on one ingredient in one week is a stronger signal than any single score.

### 11. Scoring outputs are versioned

Every rubric change increments `SCORING_VERSION`, which is written into the report header and every JSONL row. Reports produced weeks apart under different rubrics are otherwise indistinguishable and silently incomparable.

---

## Scoring model

Each abstract yields six judgements:

| Field | Meaning |
|---|---|
| `relevance` | how closely it touches the reader's daily life |
| `actionability` | what the reader can actually do after reading |
| `evidence` | study-design strength, minus deductions for overstated conclusions |
| `novelty` | whether it says something new |
| `ingredient` | the core compound, verbatim in English (often empty) |
| `sourcing` | value as an importable functional ingredient |

```
score = relevance×3 + actionability×2 + evidence×2 + novelty×1     (max 40)
        × 0.3   if relevance ≤ 2      (audience doesn't care)
        × 0.5   if no angle proposed  (model couldn't find a story)
```

Discounted items are kept and labelled with the reason and pre-discount score, so the discount itself can be audited.

---

## Setup

```bash
# 1. Contact email for the Europe PMC User-Agent (API etiquette)
setx EUROPEPMC_CONTACT_EMAIL "you@example.com"
setx LLAMA_MODELS_DIR        "D:\path\to\llama.cpp\models"
setx LLAMA_DIR               "D:\path\to\llama.cpp"
#    open a NEW terminal after setx

# 2. Download the model (~18GB, resumable)
py download_model.py

# 3. Start the inference server (keep the window open)
start_server.bat

# 4. Verify both ends
py fetch_papers.py --selftest
py screen_papers.py --selftest
```

Requires Python 3.11+ and a `llama.cpp` build with CUDA. `start_server.bat` must pass `--jinja`, otherwise the flag that disables the model's thinking mode is ignored.

## Usage

```bash
py fetch_papers.py --days 7                       # weekly run
py fetch_papers.py --days 7 --dry-run --show 25   # inspect recall without writing
py fetch_papers.py --topic probiotics_gut         # single topic

py screen_papers.py                               # score everything
py screen_papers.py --limit 10                    # time a small batch first
py screen_papers.py --workers 3                   # concurrency (1 if VRAM-tight)
py screen_papers.py --endpoint http://localhost:11434/v1 --model qwen3:14b   # Ollama
```

Tests — fully offline, no network and no model required (both are replaced by local mock
servers), so they run anywhere. Assertions are **derived from the module constants** rather
than hardcoded, so re-tuning the rubric doesn't turn the suite red:

```bash
py test_pipeline.py        # 135 checks
py test_pipeline.py -v     # list passing checks too
```

Diagnostics, written while debugging the issues above and kept for reuse:

```bash
py diag_europepmc.py                    # isolate who is returning 503: headers, proxy, or ISP
py diag_preprints.py                    # add filter clauses one at a time, find where a count hits 0
py diag_strategies.py --only S7 S8      # compare candidate retrieval strategies side by side
```

## Configuration

| Goal | Where |
|---|---|
| Change what "relevant" means | `AUDIENCE` in `screen_papers.py` — the highest-leverage knob in the project |
| Items per topic in the report | `TOPIC_QUOTA` |
| Shortlist too short / too long | `RELEVANCE_GATE` |
| Re-weight the scoring dimensions | `WEIGHT_*` |
| Radar sensitivity to early signals | `RADAR_MIN_SOURCING` (3 → 2 admits animal-only evidence) |
| Add or edit a retrieval topic | `TOPICS` in `fetch_papers.py` |
| VRAM headroom | `NCPUMOE` in `start_server.bat` (higher = less VRAM, slower) |

---

## Scope and status

**Done:** retrieval, screening, topic shortlist, ingredient radar.
**Not built:** draft generation, image generation.

**Deliberately out of scope: publishing automation.** Drafts are posted by hand. Integrating the WeChat and Xiaohongshu APIs would add platform-compliance and account risk for no time saved on the part that is actually slow — finding and evaluating source material.

Draft generation is planned as a frontier-model API call rather than local inference: a 14B model at 4-bit is adequate for scoring English abstracts but not for competitive Chinese prose. The GPU is better spent on batch screening and image rendering.
