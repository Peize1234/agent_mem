# MidTerm 小型 Zero-shot Reranker 对比实验

## 主结果

| 模型 | Micro R@5 | Gold@5 | Macro R@5 | R@10 | R@20 | MRR | Promoted | Demoted | Net |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| C3 BGE-small Dense | 38.31% | 59/154 | 39.80% | 54.55% | 81.82% | 0.3199 | 0 | 0 | +0 |
| BAAI/bge-reranker-v2-m3 | ERROR |  |  |  |  |  |  |  |  |

## Session R@5

- C3 BGE-small Dense / S001：47.62%
- C3 BGE-small Dense / S002：34.38%
- C3 BGE-small Dense / S003：30.23%
- C3 BGE-small Dense / S004：40.62%
- C3 BGE-small Dense / S005：46.15%

## Memory Dependency 子集

- C3 BGE-small Dense：58/149（38.93%）

## 运行状态

- BAAI/bge-reranker-v2-m3：ERROR；new_scores=0；cache_hits=0；peak_cuda_bytes=1155706368.
  - 

## Validation

- query_count: PASS
- page_count: PASS
- eligible_gold: PASS
- c3_baseline: PASS
- p2_query_coverage: PASS
- c3_page_coverage: PASS
- top20_candidate_source: PASS
- visibility: PASS
- future_leakage: PASS
- no_llm: PASS
- no_new_embedding: PASS
- instruction_frozen: PASS
- mem0_unmodified: PASS

## 实验边界

- 仅重排冻结 C3 Dense Top20；Top20 外 Page 顺序保持 C3 不变。
- batch_size=1、max_length=512、FP16；无 CPU offload、量化、BM25、融合或参数搜索。
- Qwen instruction 在推理前冻结；P2 Query、C3 Page、Gold 与 visibility 均来自原 artifact。

详细案例见 `representative_cases.md`。
