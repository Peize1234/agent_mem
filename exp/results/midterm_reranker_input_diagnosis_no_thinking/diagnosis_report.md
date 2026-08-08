# S001–S005 Reranker Input 与 Candidate Pool 诊断

> Selection status: **S001-S005_TUNED**。本轮数据不再是 held-out；任何新候选都必须冻结后到 S006+ 验证。

## 1. 基线复现与实验边界

Frozen Quality 精确复现：S001 R@5=47.62%；S002–S005 R@5=33.08%。没有重跑完整 Session、没有生成 Page/长期记忆、没有调用 DeepSeek，也没有修改生产检索代码。

## 2. 真实 tokenizer truncation audit

实际 tokenizer=XLMRobertaTokenizer，configured/model max length=512/512，truncation side=right，pair strategy=longest_first，pair special tokens=4。

| Group | Count | >512 | Truncation rate | Mean pair tokens | KW fully | KW partial | KW absent |
|---|---:|---:|---:|---:|---:|---:|---:|
| ALL_CANDIDATES | 1700 | 6 (0.35%) | 0.35% | 325.7 | 99.65% | 0.24% | 0.12% |
| ALL_GOLD_CANDIDATES | 119 | 0 (0.00%) | 0.00% | 331.8 | 100.00% | 0.00% | 0.00% |
| DEMOTED_OUT_OF_TOP5 | 15 | 0 (0.00%) | 0.00% | 315.5 | 100.00% | 0.00% | 0.00% |
| PROMOTED_INTO_TOP5 | 19 | 0 (0.00%) | 0.00% | 362.7 | 100.00% | 0.00% | 0.00% |
| PRESERVED_HIT | 35 | 0 (0.00%) | 0.00% | 359.5 | 100.00% | 0.00% | 0.00% |
| STILL_MISS | 50 | 0 (0.00%) | 0.00% | 305.5 | 100.00% | 0.00% | 0.00% |

DEMOTED Gold truncation rate=0.00%，non-DEMOTED Gold=0.00%；D1 input fix 相对 D0 净增 -8 Gold。差异和实际增益不足以把 512-token truncation 判定为主要原因。

实际截断后的输入样例保存在 `truncation_debug_cases.jsonl`，包含所有 S005 demoted Gold 和一个 S001 promoted Gold。

## 3. Reranker input / packing screening（固定 RRF60 Top20）

| Input | Micro R@5 | Macro R@5 | MRR | NDCG@5 | Promoted | Demoted | Net | S005 R@5 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| R0_P8_CURRENT | 35.06% | 36.26% | 0.2505 | 0.2265 | 19 | 15 | +4 | 30.77% |
| R1_KEYWORDS_FIRST | 29.87% | 31.23% | 0.2243 | 0.1933 | 16 | 20 | -4 | 34.62% |
| R2_PACK_A | 29.22% | 29.01% | 0.2007 | 0.1746 | 17 | 22 | -5 | 26.92% |
| R3_USER_SUMMARY | 29.22% | 29.67% | 0.2021 | 0.1774 | 18 | 23 | -5 | 26.92% |
| R4_PACK_B | 27.92% | 27.76% | 0.2045 | 0.1784 | 14 | 21 | -7 | 26.92% |
| PACK_C_BALANCED | 29.22% | 29.01% | 0.2007 | 0.1746 | 17 | 22 | -5 | 26.92% |

D1 Best Input Fix：`R1_KEYWORDS_FIRST`，35.06% → 29.87% (-5.19 pp, -8 Gold)。

## 4. Candidate pool

| Candidate | Recall | Hits | Mean K | p95 K | First-stage R@5 |
|---|---:|---:|---:|---:|---:|
| C0_DENSE20 | 74.68% | 115/154 | 17.17 | 20.0 | 32.47% |
| C1_BM25_20 | 75.97% | 117/154 | 17.17 | 20.0 | 21.43% |
| C2_RRF60_20 | 77.27% | 119/154 | 17.17 | 20.0 | 32.47% |
| C3_FIXED_UNION20 | 75.97% | 117/154 | 17.17 | 20.0 | 31.82% |
| C4_UNION15X15 | 79.22% | 122/154 | 18.64 | 25.1 | 32.47% |
| C5_UNION20X20 | 88.96% | 137/154 | 22.54 | 32.0 | 32.47% |

Dense20 ∪ BM2520 coverage=88.96%；RRF20=77.27%。共有 18 个 Gold 在 Dense20/BM2520 至少一路出现、但被 RRF20 压缩丢失。

## 5. Candidate × Top2 Input joint ablation

| Candidate | Input | Candidate R | Final R@5 | Macro R@5 | MRR | NDCG@5 | Promoted | Demoted | Net |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| C2_RRF60_20 | R0_P8_CURRENT | 77.27% | 35.06% | 36.26% | 0.2505 | 0.2265 | 19 | 15 | +4 |
| C3_FIXED_UNION20 | R0_P8_CURRENT | 75.97% | 32.47% | 33.10% | 0.2366 | 0.2089 | 16 | 16 | +0 |
| C4_UNION15X15 | R0_P8_CURRENT | 79.22% | 33.12% | 34.06% | 0.2368 | 0.2135 | 17 | 16 | +1 |
| C5_UNION20X20 | R0_P8_CURRENT | 88.96% | 33.77% | 34.68% | 0.2407 | 0.2166 | 18 | 16 | +2 |
| C2_RRF60_20 | R1_KEYWORDS_FIRST | 77.27% | 29.87% | 31.23% | 0.2243 | 0.1933 | 16 | 20 | -4 |
| C3_FIXED_UNION20 | R1_KEYWORDS_FIRST | 75.97% | 29.22% | 30.30% | 0.2214 | 0.1916 | 15 | 20 | -5 |
| C4_UNION15X15 | R1_KEYWORDS_FIRST | 79.22% | 28.57% | 29.83% | 0.2203 | 0.1892 | 14 | 20 | -6 |
| C5_UNION20X20 | R1_KEYWORDS_FIRST | 88.96% | 29.22% | 30.60% | 0.2192 | 0.1902 | 15 | 20 | -5 |

D2 Best Candidate + Input：`C2_RRF60_20 + R0_P8_CURRENT`，35.06% → 35.06% (+0.00 pp, +0 Gold)。Candidate coverage 提高并不自动转化为 final Top5；具体 hard-negative effect 见上表。

## 6. max_length / model

bge-reranker-base 的 1024 状态：**UNSUPPORTED**。原因：tokenizer.model_max_length=512; model.max_position_embeddings=514. The architecture supports the configured 512-token pair, not a 1024-token sequence.

新 reranker：UNAVAILABLE_NOT_CACHED。BAAI/bge-reranker-v2-m3 is not present in the existing HuggingFace cache. Network/model download was intentionally not introduced into this isolated diagnosis.

## 7. S005

Current Frozen Quality：R@5=30.77%、promoted=3、demoted=7。D1：R@5=34.62%、demoted=7；D2：R@5=30.77%、demoted=7。相对当前 7 个 demotion，D2 恢复 0 个，并新增 demotion 0 个。

## 8. Warm E2E latency

| Scheme | Candidate mean/p95 | Packing mean/p95 | Reranker mean/p95 | Total mean/p95 |
|---|---:|---:|---:|---:|
| D0_CURRENT_FROZEN_QUALITY | 234.08/445.42 ms | 0.16/0.41 ms | 10103.99/17298.01 ms | 10338.32/17652.55 ms |
| D1_BEST_INPUT_FIX | 249.75/476.32 ms | 0.10/0.15 ms | 10118.25/17789.32 ms | 10368.12/18039.16 ms |
| D2_BEST_CANDIDATE_INPUT | 234.08/445.42 ms | 0.16/0.41 ms | 10103.99/17298.01 ms | 10338.32/17652.55 ms |

## 9. 结论与下一阶段

最终诊断状态：**GENERIC_RERANKER_REMAINS_MAIN_BOTTLENECK**。

Input packing 和 candidate expansion 均未形成稳定的多 Session 净提升；停止继续手调 S001-S005。本轮没有形成值得冻结的新候选，因此暂不消耗 S006+；若继续研发，先在 development 数据上评估一个可用的新 reranker 或构造训练数据，形成冻结候选后再进入 S006+ held-out。本轮不训练。

## 10. 直接问题回答

1. 当前实际 configured max_length=512；tokenizer model_max_length=512。
2. truncation=True 对 pair 解析为 longest_first，truncation_side=right、padding_side=right。
3. P8 candidate >512：6/1700（0.35%）。
4. Gold candidate >512：0/119（0.00%）。
5. DEMOTED Gold truncation=0.00%，non-DEMOTED Gold=0.00%；DEMOTED Gold truncation rate=0.00%，non-DEMOTED Gold=0.00%；D1 input fix 相对 D0 净增 -8 Gold。差异和实际增益不足以把 512-token truncation 判定为主要原因。
6. 所有 P8 candidate keywords：fully=99.65%、partial=0.24%、not visible=0.12%。
7. Keywords 前置 R1 R@5=29.87%，当前 R0=35.06%。
8. 包含 User Input 的最佳方案 R3_USER_SUMMARY R@5=29.22%。
9. Token-budgeted packing 最佳为 R3_USER_SUMMARY；相对 Frozen Quality：35.06% → 29.22% (-5.84 pp, -9 Gold)。
10. 512→1024：未运行，状态 UNSUPPORTED；模型架构 max_position_embeddings=514。
11. S005 demotion：7 → 7；恢复 0，新增 0。
12. Dense20∪BM2520 theoretical/actual exact coverage=88.96%。
13. RRF20 丢失 Dense20/BM2520 已找到 Gold=18。
14. Fixed Union20 candidate recall=75.97%；RRF20=77.27%。
15. Expanded Union 最佳 joint 为 C2_RRF60_20，mean candidate count=17.17；是否值得由 final R@5/latency 共同判断。
16. Candidate Recall=77.27%，Final R@5=35.06%；提高覆盖并未被假定等于提高 Top5。
17. 当前主要瓶颈：GENERIC_RERANKER_REMAINS_MAIN_BOTTLENECK。
18. generic bge-reranker-base：当前没有稳定证据支持继续作为默认 reranker。
19. bge-reranker-v2-m3：executed=False，status=UNAVAILABLE_NOT_CACHED。
20. 新模型对比：BAAI/bge-reranker-v2-m3 is not present in the existing HuggingFace cache. Network/model download was intentionally not introduced into this isolated diagnosis.
21. 仍无证据要求修改 Page Prompt；所有实验只重组既有字段。
22. 仍无必要 Query Rewrite；Query 始终固定 Q0。
23. Fine-tuning：只建议作为未来方向评估；本轮仅统计数据，未训练。
24. 本轮最佳 S001-S005 tuned 方案：C2_RRF60_20 + R0_P8_CURRENT + BAAI/bge-reranker-base@512 → Top5。
25. 相对 Frozen Quality：35.06% → 35.06% (+0.00 pp, +0 Gold)。
26. 是否冻结进入 S006+：NOT_GENERATED_NO_MATERIAL_STABLE_CANDIDATE。本任务未运行 S006+。
