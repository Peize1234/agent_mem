# 当前最优 MidTerm 配置上的 Embedding 模型对照

Add Page、P2 Query、Gold、visibility 全部冻结；无 LLM 调用或 Summary regeneration。

| Model | Micro R@5 | Macro R@5 | R@10 | R@20 | MRR | Mean Gold Rank |
|---|---:|---:|---:|---:|---:|---:|
| BAAI/bge-small-zh-v1.5 | 38.31% | 39.80% | 54.55% | 81.82% | 0.3199 | 11.55 |
| aspire/acge_text_embedding | 27.92% | 28.20% | 48.70% | 81.17% | 0.2692 | 13.73 |

## Session R@5

| Model | S001 | S002 | S003 | S004 | S005 |
|---|---:|---:|---:|---:|---:|
| BAAI/bge-small-zh-v1.5 | 47.62% | 34.38% | 30.23% | 40.62% | 46.15% |
| aspire/acge_text_embedding | 23.81% | 31.25% | 25.58% | 21.88% | 38.46% |

## 相对 BGE-small 的 Top5 movement

| Model | Promoted | Demoted | Net | Rescued Query | Hurt Query |
|---|---:|---:|---:|---:|---:|
| aspire/acge_text_embedding | 14 | 30 | -16 | 12 | 23 |

## 模型执行状态

- tencent/Youtu-Embedding：BLOCKED_LOCAL_RESOURCES — The official 8.98 GiB weights exceed the 4 GiB GPU and leave insufficient headroom on the 11 GiB host for a standards-preserving local run. No quantization/offload substitute was used because that would change the requested official encoding standard.
- TencentBAC/Conan-embedding-v2：BLOCKED_OFFICIAL_ARTIFACT_OR_CREDENTIALS — Official Hugging Face repository contains client/example code but no model weights or tokenizer. The official temporary API requires CONAN_AK and CONAN_SK, which are absent.
- Qwen/Qwen3-Embedding-4B：BLOCKED_LOCAL_RESOURCES — The official 7.49 GiB BF16 weights exceed the 4 GiB GPU. CPU encoding was measured at 183-232 seconds per Page (batch_size=1), projecting roughly 17-21 hours for 333 Pages; the run was stopped after two Pages without changing precision or the benchmark.
- aspire/acge_text_embedding：COMPLETED

## Retrieval 表现类型

- aspire/acge_text_embedding：OVERALL_DEGRADED_OR_TIED
