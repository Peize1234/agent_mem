# Add 阶段最近局部上下文实验报告

## Retrieval

| Add 方式 | Top5 | Macro Top5 | Top10 | Top20 | MRR | Mean Gold Rank |
|---|---:|---:|---:|---:|---:|---:|
| 原 Production Add（仅当前 User + Assistant） | 35.06% | 36.01% | 52.60% | 75.97% | 0.3046 | 14.55 |
| Production Add + 最近上文 | 29.22% | 30.08% | 53.25% | 80.52% | 0.2882 | 13.32 |
| Production Add + 最近上文及下文 | 37.01% | 39.20% | 57.79% | 84.42% | 0.3121 | 11.45 |

## Session-level Top5

| Session | 原 Production Add | Production Add + 最近上文 | Production Add + 最近上文及下文 |
|---|---:|---:|---:|
| S001 | 33.33% | 28.57% | 52.38% |
| S002 | 46.88% | 31.25% | 31.25% |
| S003 | 25.58% | 23.26% | 25.58% |
| S004 | 28.12% | 25.00% | 40.62% |
| S005 | 46.15% | 42.31% | 46.15% |

## 相对 Production 的 Top5 movement

| Add 方式 | Promoted | Demoted | Net | Rescued Queries | Hurt Queries |
|---|---:|---:|---:|---:|---:|
| Production Add + 最近上文 | 11 | 20 | -9 | 8 | 15 |
| Production Add + 最近上文及下文 | 16 | 13 | +3 | 14 | 12 |

## Pairwise Top5 movement

| Comparison | Promoted | Demoted | Net | Rescued Queries | Hurt Queries | Mean rank improvement |
|---|---:|---:|---:|---:|---:|---:|
| Production Add + 最近上文 vs 原 Production Add（仅当前 User + Assistant） | 11 | 20 | -9 | 8 | 15 | +1.23 |
| Production Add + 最近上文及下文 vs 原 Production Add（仅当前 User + Assistant） | 16 | 13 | +3 | 14 | 12 | +3.10 |
| Production Add + 最近上文及下文 vs Production Add + 最近上文 | 22 | 10 | +12 | 18 | 9 | +1.87 |

## Representation / truncation / separation

| Add 方式 | Mean summary chars | Mean keywords | Truncated Pages | User fully truncated | Separation mean |
|---|---:|---:|---:|---:|---:|
| 原 Production Add（仅当前 User + Assistant） | 411.85 | 7.99 | 76 | 33 | -0.048310 |
| Production Add + 最近上文 | 537.59 | 7.95 | 213 | 106 | -0.046426 |
| Production Add + 最近上文及下文 | 559.95 | 7.63 | 203 | 139 | -0.041144 |

## Deterministic context-source audit

该审计只比较当前 QA、实际输入上下文和输出中的 canonical anchors；不使用 Gold 或检索结果。

| Add 方式 | Added beyond current QA | From previous | Following-only | Unsupported | Pages with following-only |
|---|---:|---:|---:|---:|---:|
| Production Add + 最近上文 | 489 | 367 | 0 | 122 | 0 |
| Production Add + 最近上文及下文 | 627 | 525 | 14 | 88 | 14 |

## 事实诊断

- 只加上文：Top5 相对 Production 变化 -5.84%，Top20 变化 +4.55%；Top5 promoted/demoted/net = 11/20/-9。
- 加上文及下文：Top5 相对 Production 变化 +1.95%，Top20 变化 +8.44%；Top5 promoted/demoted/net = 16/13/+3。
- 前后文配置相对只加上文：promoted/demoted/net = 22/10/+12；这是完整配置差异，其中也包括按各自 Prompt 顺序生成的上文 Page 差异。
- 前后文配置在 S001、S004 分别较 Production 提升 +19.05%、+12.50%，但 S002 下降 -15.62%。
- Summary 变长后，截断 Page 从 76 增至 213 / 203；User 完全被截断从 33 增至 106 / 139。
- S004-Q056 的 Gold S004-Q050 为下文配置特有救回：Production #39、只加上文 #19、前后文 #4；前后文摘要保留了综合结论、证据分级、反证检验和后续用途。
- S005-Q042 的 Gold S005-Q036 是下降例：Production #3、只加上文 #25、前后文 #17；两个上下文版 Summary 都超过 512-token 内容预算，Keywords 与 User 均完全未进入 embedding。
- context-source audit 是字符串/canonical 匹配，不等同于语义事实审计；其中“Unsupported”包括同义改写和数字格式变化，只能作为人工复查候选，不能直接判定为幻觉。

## 结论边界

- 加入上文的 RESCUED Query：8。
- 加入上文及下文的 RESCUED Query：14。
- 是否改善总体检索，以表中 Top5、promotion/demotion、separation 与代表案例的事实结果为准。
- 本轮没有修改 Search-P2，没有生成下一版 Prompt。
