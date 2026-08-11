# MidTerm Add Local Context — Control Experiments

Search 全部固定为上下文引用解析后检索（P2）；没有调用 LLM，也没有修改 Prompt、Gold、visibility 或 Query。

## 1. Production embedding 可复现性问题

| Production vector | Top5 | Macro Top5 | Top10 | Top20 | MRR | Mean Gold Rank |
|---|---:|---:|---:|---:|---:|---:|
| Stored Production embedding | 35.06% | 36.01% | 52.60% | 75.97% | 0.3046 | 14.55 |
| Fresh re-encoded Production embedding | 35.06% | 36.01% | 52.60% | 75.97% | 0.3046 | 14.55 |

- Stored→fresh promoted/demoted/net：0/0/+0。
- 99 个 Query 的 Top5 Page ID 完全一致：True (99/99)。
- Page vector cosine：mean=1.000000000000，min=0.999999999999，p5=1.000000000000，p50=1.000000000000，p95=1.000000000000。

## 2. Raw User / token budget 对上下文 Add 的影响

| Add 方式 | Formatter | Top5 | Macro Top5 | Top10 | Top20 | MRR | Mean Gold Rank |
|---|---|---:|---:|---:|---:|---:|---:|
| 原 Production Add（仅当前 User + Assistant） | Summary + Keywords + User | 35.06% | 36.01% | 52.60% | 75.97% | 0.3046 | 14.55 |
| 原 Production Add（仅当前 User + Assistant） | Summary + Keywords | 35.71% | 36.17% | 54.55% | 75.32% | 0.3003 | 14.79 |
| Production Add + 最近上文 | Summary + Keywords + User | 29.22% | 30.08% | 53.25% | 80.52% | 0.2882 | 13.32 |
| Production Add + 最近上文 | Summary + Keywords | 29.22% | 29.77% | 53.25% | 79.87% | 0.2905 | 13.07 |
| Production Add + 最近上文及下文 | Summary + Keywords + User | 37.01% | 39.20% | 57.79% | 84.42% | 0.3121 | 11.45 |
| Production Add + 最近上文及下文 | Summary + Keywords | 38.31% | 39.80% | 54.55% | 81.82% | 0.3199 | 11.55 |

### Formatter movement

| Comparison | Promoted | Demoted | Net | Rescued Q | Hurt Q |
|---|---:|---:|---:|---:|---:|
| 原 Production Add（仅当前 User + Assistant）：去除 User vs 原始 formatter | 6 | 5 | +1 | 3 | 5 |
| Production Add + 最近上文：去除 User vs 原始 formatter | 4 | 4 | +0 | 3 | 4 |
| Production Add + 最近上文及下文：去除 User vs 原始 formatter | 5 | 3 | +2 | 5 | 3 |
| 去除 User 后：Production Add + 最近上文及下文 vs 原 Production Add | 18 | 14 | +4 | 17 | 11 |

### Token budget

| Add 方式 | Formatter | Truncated | Summary truncated | Keywords fully kept | Keywords partial | Keywords fully lost |
|---|---|---:|---:|---:|---:|---:|
| 原 Production Add（仅当前 User + Assistant） | Summary + Keywords + User | 76 | 13 | 301 | 19 | 13 |
| 原 Production Add（仅当前 User + Assistant） | Summary + Keywords | 32 | 13 | 301 | 19 | 13 |
| Production Add + 最近上文 | Summary + Keywords + User | 213 | 43 | 231 | 58 | 44 |
| Production Add + 最近上文 | Summary + Keywords | 102 | 43 | 231 | 58 | 44 |
| Production Add + 最近上文及下文 | Summary + Keywords + User | 203 | 97 | 195 | 40 | 98 |
| Production Add + 最近上文及下文 | Summary + Keywords | 138 | 97 | 195 | 40 | 98 |

### Query-conditioned separation

| Add 方式 | Formatter | Mean best-Gold minus strongest-NonGold | Median | Positive rate |
|---|---|---:|---:|---:|
| 原 Production Add（仅当前 User + Assistant） | Summary + Keywords + User | -0.048310 | -0.042247 | 13.13% |
| 原 Production Add（仅当前 User + Assistant） | Summary + Keywords | -0.047032 | -0.048726 | 12.12% |
| Production Add + 最近上文 | Summary + Keywords + User | -0.046426 | -0.047238 | 14.14% |
| Production Add + 最近上文 | Summary + Keywords | -0.043052 | -0.044889 | 14.14% |
| Production Add + 最近上文及下文 | Summary + Keywords + User | -0.041144 | -0.042250 | 14.14% |
| Production Add + 最近上文及下文 | Summary + Keywords | -0.037641 | -0.033644 | 15.15% |

### Session Top5

| Session | Production Full | Production No User | Previous Full | Previous No User | Both Full | Both No User |
|---|---:|---:|---:|---:|---:|---:|
| S001 | 33.33% | 33.33% | 28.57% | 28.57% | 52.38% | 47.62% |
| S002 | 46.88% | 43.75% | 31.25% | 28.12% | 31.25% | 34.38% |
| S003 | 25.58% | 30.23% | 23.26% | 25.58% | 25.58% | 30.23% |
| S004 | 28.12% | 31.25% | 25.00% | 28.12% | 40.62% | 40.62% |
| S005 | 46.15% | 42.31% | 42.31% | 38.46% | 46.15% | 46.15% |

S002 的前后文方案从 Full 的 31.25% 升至 No User 的 34.38%，但仍低于 No User Production 的 43.75%，因此只属于部分恢复。

### Selected rank controls

下面列出 S005-Q042 及自动选择的 no-User RESCUED/HURT Query；完整 score 在 CSV 中。

| Query | Gold source | Production Full→No User | Previous Full→No User | Both Full→No User |
|---|---|---:|---:|---:|
| S001-Q042 | S001-Q036 | #6→#6 | #9→#11 | #5→#7 |
| S002-Q013 | S002-Q001 | #9→#9 | #9→#9 | #9→#9 |
| S002-Q013 | S002-Q006 | #5→#5 | #7→#8 | #6→#5 |
| S002-Q026 | S002-Q014 | #6→#5 | #3→#2 | #9→#8 |
| S002-Q026 | S002-Q019 | #9→#6 | #13→#12 | #12→#10 |
| S002-Q033 | S002-Q023 | #4→#7 | #2→#7 | #1→#2 |
| S002-Q033 | S002-Q029 | #11→#18 | #15→#15 | #13→#11 |
| S003-Q035 | S003-Q029 | #7→#4 | #14→#14 | #19→#17 |
| S003-Q039 | S003-Q027 | #6→#5 | #18→#15 | #3→#2 |
| S003-Q039 | S003-Q032 | #23→#22 | #29→#30 | #27→#29 |
| S003-Q091 | S003-Q079 | #27→#42 | #27→#20 | #14→#22 |
| S003-Q091 | S003-Q084 | #4→#7 | #9→#4 | #6→#3 |
| S003-Q091 | S003-Q085 | #8→#6 | #8→#3 | #16→#23 |
| S004-Q028 | S004-Q022 | #6→#7 | #6→#4 | #5→#8 |
| S004-Q033 | S004-Q023 | #11→#10 | #4→#4 | #10→#5 |
| S004-Q033 | S004-Q029 | #19→#13 | #20→#21 | #21→#19 |
| S004-Q055 | S004-Q045 | #49→#48 | #28→#29 | #32→#25 |
| S004-Q055 | S004-Q051 | #43→#46 | #4→#6 | #27→#33 |
| S004-Q066 | S004-Q056 | #12→#20 | #6→#5 | #20→#15 |
| S004-Q066 | S004-Q062 | #42→#49 | #10→#10 | #16→#22 |
| S005-Q042 | S005-Q036 | #3→#1 | #25→#25 | #17→#18 |
| S005-Q052 | S005-Q040 | #5→#9 | #2→#3 | #2→#1 |
| S005-Q052 | S005-Q045 | #29→#30 | #42→#41 | #30→#26 |
| S005-Q055 | S005-Q045 | #14→#12 | #45→#43 | #23→#24 |
| S005-Q055 | S005-Q051 | #3→#7 | #4→#7 | #3→#7 |

## 控制结论

- Fresh Production Top5 与 stored 的差值：+0.00%。
- 去除 User 后，前后文方案相对去除 User 的 Production：Top5 +2.60%，Top10 +0.00%，Top20 +6.49%。
- 前后文方案自身去除 User 后：Top5 +1.30%，Top10 -3.25%，Top20 -2.60%；并非所有 cutoff 同时改善。
- User 位于 formatter 末尾，因此删除 User 没有改变前后文方案的 Summary 截断数 (97→97)，也没有改变 Keywords partial/full-loss 数 (40/98→40/98)。
- S005-Q042 没有被上下文方案救回：原 Production Add（仅当前 User + Assistant） #3→#1；Production Add + 最近上文 #25→#25；Production Add + 最近上文及下文 #17→#18。Production 的改善来自其他候选 Page 移除 User 后的相对排序变化；该 Gold 在原 formatter 中本就未编码到 User。
- 因此 Raw User 确实对 Top5 有小幅干扰，但当前结果不支持把后续重点完全归因于删除 User；上下文 Summary 自身超过 token budget 的问题仍然存在。
- 本控制实验没有修改或生成任何 Add/Search Prompt，也没有生成下一版 Prompt。
