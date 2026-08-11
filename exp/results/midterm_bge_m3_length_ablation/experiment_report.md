# 当前最优 MidTerm 配置上的 BGE-M3 长度消融

Page、P2 Query、visibility 与 Gold 完全冻结；Page formatter 为 Summary + Keywords。

## Retrieval

| Configuration | Micro R@5 | Macro R@5 | Gold@5 | R@10 | R@20 | MRR | Mean Gold Rank |
|---|---:|---:|---:|---:|---:|---:|---:|
| BAAI/bge-small-zh-v1.5，max_length=512（当前基线） | 38.31% | 39.80% | 59 | 54.55% | 81.82% | 0.3199 | 11.55 |
| BAAI/bge-m3，max_length=512 | 37.01% | 38.26% | 57 | 59.74% | 81.17% | 0.2674 | 12.20 |
| BAAI/bge-m3，max_length=1024 | 37.01% | 38.26% | 57 | 59.74% | 81.17% | 0.2674 | 12.20 |
| BAAI/bge-m3，max_length=2048 | 37.01% | 38.26% | 57 | 59.74% | 81.17% | 0.2674 | 12.20 |

## Session R@5

| Session | Small 512 | M3 512 | M3 1024 | M3 2048 |
|---|---:|---:|---:|---:|
| S001 | 47.62% | 47.62% | 47.62% | 47.62% |
| S002 | 34.38% | 43.75% | 43.75% | 43.75% |
| S003 | 30.23% | 30.23% | 30.23% | 30.23% |
| S004 | 40.62% | 31.25% | 31.25% | 31.25% |
| S005 | 46.15% | 38.46% | 38.46% | 38.46% |

## Gold movement

| Comparison | Promoted | Demoted | Net | Rescued Query | Hurt Query |
|---|---:|---:|---:|---:|---:|
| BGE-M3 512 vs BGE-small 512（模型影响） | 20 | 22 | -2 | 15 | 17 |
| BGE-M3 1024 vs BGE-small 512 | 20 | 22 | -2 | 15 | 17 |
| BGE-M3 2048 vs BGE-small 512 | 20 | 22 | -2 | 15 | 17 |
| BGE-M3 1024 vs BGE-M3 512（长度影响） | 0 | 0 | +0 | 0 | 0 |
| BGE-M3 2048 vs BGE-M3 512（长度影响） | 0 | 0 | +0 | 0 | 0 |
| BGE-M3 2048 vs BGE-M3 1024 | 0 | 0 | +0 | 0 | 0 |

## Tokenizer / truncation

| Configuration | <=512 | 513-1024 | 1025-2048 | >2048 | Page truncated | Summary truncated | Keywords partial | Keywords fully lost |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| BAAI/bge-small-zh-v1.5，max_length=512（当前基线） | 195 | 138 | 0 | 0 | 138 | 97 | 40 | 98 |
| BAAI/bge-m3，max_length=512 | 332 | 1 | 0 | 0 | 1 | 0 | 1 | 0 |
| BAAI/bge-m3，max_length=1024 | 332 | 1 | 0 | 0 | 0 | 0 | 0 | 0 |
| BAAI/bge-m3，max_length=2048 | 332 | 1 | 0 | 0 | 0 | 0 | 0 | 0 |

## 事实结论

- 同为 512：BGE-M3 相对 BGE-small 的 Top5 变化为 -1.30%，Gold net=-2。
- M3 1024 vs 512：Gold net=+0；M3 2048 vs 512：Gold net=+0。
- 按 BGE-small tokenizer 原本 >512 的 Gold：promoted=6，demoted=10，net=-4。
- 最优配置：BAAI/bge-small-zh-v1.5，max_length=512（当前基线），Top5=38.31%。
- BGE-M3 tokenizer 下没有 Page 超过 1024，更没有 Page 超过 2048，因此未运行 8192。
