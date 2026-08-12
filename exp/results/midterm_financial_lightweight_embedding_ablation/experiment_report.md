# 金融领域与轻量通用 Embedding 对照实验

## Retrieval 指标

| Model | Type | Micro R@5 | Gold@5 | Macro R@5 | R@10 | R@20 | MRR | Mean Gold Rank |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| BAAI/bge-small-zh-v1.5 | 基线 | 38.31% | 59/154 | 39.80% | 54.55% | 81.82% | 0.3199 | 11.55 |
| qcsun/financial-embedding | 金融 | 34.42% | 53/154 | 34.78% | 52.60% | 76.62% | 0.3124 | 13.13 |
| BalyasnyAI/multilingual-e5-base | 金融 | 35.71% | 55/154 | 36.34% | 54.55% | 84.42% | 0.2999 | 11.73 |
| Alibaba-NLP/gte-multilingual-base | 通用 | 32.47% | 50/154 | 32.45% | 59.74% | 81.17% | 0.2564 | 12.97 |

## Session R@5

| Model | S001 | S002 | S003 | S004 | S005 |
|---|---:|---:|---:|---:|---:|
| BAAI/bge-small-zh-v1.5 | 47.62% | 34.38% | 30.23% | 40.62% | 46.15% |
| qcsun/financial-embedding | 33.33% | 40.62% | 30.23% | 31.25% | 38.46% |
| BalyasnyAI/multilingual-e5-base | 38.10% | 40.62% | 32.56% | 28.12% | 42.31% |
| Alibaba-NLP/gte-multilingual-base | 28.57% | 34.38% | 27.91% | 40.62% | 30.77% |

## 相对 BGE-small 的 Top5 movement

| Model | Promoted | Demoted | Net | Rescued Query | Hurt Query |
|---|---:|---:|---:|---:|---:|
| qcsun/financial-embedding | 22 | 28 | -6 | 14 | 24 |
| BalyasnyAI/multilingual-e5-base | 16 | 20 | -4 | 11 | 14 |
| Alibaba-NLP/gte-multilingual-base | 16 | 25 | -9 | 13 | 20 |

## Tokenizer 截断审计

| Model | Native / Used Max | Page Max | Page Truncated | Summary Truncated | Keywords Partial / Fully Lost | Query Max | Query Truncated |
|---|---:|---:|---:|---:|---:|---:|---:|
| BAAI/bge-small-zh-v1.5 | 512 / 512 | 695 | 138 | 97 | 40 / 98 | 115 | 0 |
| qcsun/financial-embedding | 8192 / 1024 | 524 | 0 | 0 | 0 / 0 | 83 | 0 |
| BalyasnyAI/multilingual-e5-base | 512 / 512 | 517 | 1 | 0 | 1 / 0 | 86 | 0 |
| Alibaba-NLP/gte-multilingual-base | 8192 / 1024 | 515 | 0 | 0 | 0 / 0 | 83 | 0 |
| google/embeddinggemma-300m | 2048 / NOT_AVAILABLE | NOT_AVAILABLE | NOT_AVAILABLE | NOT_AVAILABLE | NOT_AVAILABLE / NOT_AVAILABLE | NOT_AVAILABLE | NOT_AVAILABLE |

## 结果性质

- BAAI/bge-small-zh-v1.5：当前基线。
- qcsun/financial-embedding：Top5 与宽召回整体退化。
- BalyasnyAI/multilingual-e5-base：仅 R@20 宽召回增强，Top5 前排区分能力下降。
- Alibaba-NLP/gte-multilingual-base：宽召回增强但 Top5 前排区分能力下降。

BGE-small 下被截断 Page 对应的 66 个 eligible Gold：

- qcsun/financial-embedding：promoted 8，demoted 17，net -9。
- BalyasnyAI/multilingual-e5-base：promoted 6，demoted 11，net -5。
- Alibaba-NLP/gte-multilingual-base：promoted 5，demoted 10，net -5。

## 无结果模型

- google/embeddinggemma-300m：`BLOCKED_GATED_REPOSITORY`，失败阶段 `tokenizer_load`；You are trying to access a gated repo.

## 冻结与验证

- BGE-small 精确复现 59/154（38.31%）。
- Page、P2 Query、Gold、visible scope 完全冻结；未调用 LLM，未重新生成 Summary，未重跑完整 Session。
- 详细 promoted/demoted 案例见 `representative_cases.md`。
