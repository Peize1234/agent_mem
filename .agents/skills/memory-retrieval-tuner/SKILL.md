---
name: memory-retrieval-tuner
description: 自动审查记忆 Benchmark，基于生产一致的 MidTerm 检索链路进行调参，跨 Session 验证泛化能力，并推荐稳定配置。适用于数据集变更或 MidTerm 记忆检索调参场景。R@K 可配置，K 默认值为 5。
---

# 记忆检索自动调参器

## 目标

在数据集发生变化后，自动执行记忆检索调参流程：

1. 调参前先审查 Benchmark；
2. 建立可复现的生产 Baseline；
3. 按 Session 划分 Tune 与 Validation；
4. 优先搜索低成本参数；
5. 根据诊断结果决定是否进入高成本搜索分支；
6. 在 held-out Sessions 上验证最佳候选；
7. 返回稳定、可泛化的推荐配置，而不是只选择样本内得分最高的配置。

**不要**把任何历史实验结果写成普适最优配置。历史结果只能作为搜索先验。

运行前先阅读：

- `references/search_strategy.md`
- `references/experiment_lessons.md`
- `search_space.yaml`

## 输入参数

必填：

- `dataset`：Benchmark 数据集路径。

可选：

- `k`：Primary Metric `R@K` 使用的 Recall cutoff，默认：`5`。
- `budget`：`quick | standard | deep`，默认：`standard`。
- `target`：当前仅支持 `midterm`。ShortTerm、LongTerm 和 union 指标仅用于最终 regression 检查。
- `sessions`：可选的 Session 子集。
- `seed`：数据划分/搜索随机种子，默认从 `search_space.yaml` 读取。
- `resume`：已有调参运行目录，用于恢复运行。
- `output_dir`：输出根目录，默认：`exp/results/auto_tuning`。
- `memory_config`：生成新 source run 时使用的生产兼容 Memory 配置，默认：本 Skill 的 `memory_config.json`；可覆盖为部署侧只读配置。
- `llm_mode`：`real | mock`，默认：`real`；`mock` 仅用于基础设施测试和 smoke test。
- `overrides`：显式指定的搜索空间覆盖项。

调用示例：

```text
$memory-retrieval-tuner dataset=exp/my_dataset.xlsx
```

```text
$memory-retrieval-tuner dataset=exp/my_dataset.xlsx k=10 budget=deep
```

```text
$memory-retrieval-tuner dataset=exp/my_dataset.xlsx k=3 target=midterm sessions=S001-S010
```

用户显式传入的参数始终优先于 `search_space.yaml` 中的默认值。

## 可执行入口

从仓库根目录运行确定性的 orchestrator：

```bash
python .agents/skills/memory-retrieval-tuner/scripts/run_tuner.py \
  dataset=<dataset_path> k=<K> budget=<quick|standard|deep>
```

使用 `resume=<existing_run_dir>` 恢复中断的运行。并发相关参数可通过
`max_parallel_sessions=N`、`max_parallel_candidates=N` 和 `max_parallel_llm_calls=N` 覆盖。

MidTerm 调参 Baseline 绝不能回退为 source-turn BM25 surrogate 或 production trace。它必须来自可重放的 `production_midterm_v1` checkpoint。如果 checkpoint 不存在，tuner 会启动按 Session 隔离的子进程，并运行真实的 `AsyncMemory.add -> MidTermUpdater -> MidTermMemory -> MidTermRetriever` 链路。完整 production trace 会独立发现，只用于最终 ShortTerm/LongTerm/All-memory regression。可通过 `source_run=<path>` 固定已有 source run；不完整的 source run 必须判定为 audit hard failure。

`production_midterm_adapter` 会冻结 Query 时刻的生产 Page / Session payload、dense vector、query vector 和 source-job lineage。它不会使用工作簿中的答案构造 Page Summary。Cheap Candidate 会将这些 artifact 重新加载到 Candidate/Session 隔离的 Qdrant + SQLite runtime 中，并直接调用生产 `MidTermRetriever`。Source worker 的并发同时受到 `max_parallel_sessions` 和 `max_parallel_llm_calls` 限制。

## 指标约定

Primary Objective 是 **held-out Validation Sessions 上 requirement-level Recall@K**。

每个 Query 可以包含一个或多个 Gold requirement。AND 分隔的依赖中，每个必需 group 各自构成一个 requirement；OR group 只构成一个 requirement，只要检索到其中任意 member 即满足。

对于配置的 `k`：

```text
R@K = top K 内满足的 Gold requirement 数量
      ---------------------------------------
             eligible Gold requirement 数量
```

规则：

- `k` 可配置，默认值为 `5`。
- evaluator、报告、文件名或停止逻辑中禁止硬编码 `R@5`。
- 其他 cutoff 尽量从 `k` 推导，例如 `R@(2K)`、`R@(4K)`。
- MidTerm 调参时，Primary denominator 应使用按照 Benchmark contract 实际被路由到 MidTerm / 对 MidTerm eligible 的 Gold requirements。
- ShortTerm coverage 单独报告。
- 同时报告端到端 union/completion 指标，避免把局部 MidTerm 提升误认为整体 Memory 提升。
- Micro requirement-level R@K 与 Macro/session-level 指标必须分开报告。

## 自主执行规则

除非确实存在缺失依赖、无效数据集、模型/API 不可用，或需要用户批准的破坏性操作，否则应端到端完成整个工作流。

不要每完成一个阶段就停下来询问下一步做什么，应按照下面的搜索策略继续执行。

不要为了运行实验而修改 `mem0/` 生产代码。优先使用：

1. 现有 Benchmark / Evaluation 代码；
2. 实验 adapter / 配置覆盖；
3. 本 Skill `scripts/` 下的新 adapter；`exp/benchmark/` 仅作为 legacy 参考，不得成为核心运行依赖。

只有当用户明确要求落地所选配置时，才允许修改生产代码。

## 自包含与历史复用原则

核心运行不能 import `exp/benchmark/`。通用数据/Gold 解析、production runtime wrapper、检索原语、Branch、模型发现和派生 artifact 构建均位于 `scripts/tuner/`。`exp/benchmark/` 只作为寻找历史方法的 legacy 来源，`exp/results/` 只作为 provenance 可验证的 frozen artifact 来源。

新增能力前先搜索历史实现，将可泛化的最小算法或 artifact contract 抽入 Skill；不要整份复制单数据集脚本，也不要把历史 winner 固化为默认。frozen artifact 必须通过 dataset、prompt/model、representation、production config 和内容 hash 校验。

主要模块：

- `experiment_branches.py`：Branch Registry 与实验 adapter；
- `staged_search.py`：Tune-only successive filtering；
- `derived_artifacts.py`：Query/Page/Session/field 向量派生与 content-addressed cache；
- `prompt_artifacts.py`：Query Prompt 受控迭代、逐 Query 原子缓存与恢复；
- `source_prompt_variants.py`：Add/Page Summary 受控 Prompt 和 tuner-only context wrapper；
- `generated_source_artifacts.py`：Add/Page Prompt 候选按 screening/Tune/Validation Session 延迟物化；
- `encoding_contract.py`：模型自己的 Query/Document encoding contract；
- `model_discovery.py`：cache-aware 的 Hugging Face 发现、统一质量排序、下载与 smoke；
- `benchmark_support.py` / `production_runtime.py`：自包含 benchmark schema 与生产 runtime wrapper；
- `production_midterm_adapter.py`：生产 checkpoint 生成和隔离 replay。

默认 `memory_config.json` 也随 Skill 提供，CLI 启动和核心执行不要求 `exp/benchmark/` 存在。它只作为实验 source 配置模板使用；调参不会回写该文件或部署侧配置。

## 工作流程

### 0. 解析配置

加载 `search_space.yaml`，然后应用用户 overrides。

至少解析：

```text
dataset
k
budget
target
seed
sessions
output_dir
```

校验：

- `k >= 1`；
- retrieval candidate pool 至少为 `k`；
- 如果 `R@(2K)` / `R@(4K)` 的报告 cutoff 超过可用候选数量，必须明确标记为 capped；
- 配置必须序列化写入 run metadata；
- `dataset.shortterm_qa_turns` 应与 `memory_config.midterm.short_term_capacity / 2` 一致；实际使用生产配置推导出的 QA-turn window，并显式记录任何 override。

创建：

```text
<output_dir>/<run_id>/
```

### 1. 数据集审查——强制 Gate

任何调参开始前必须先进行 Dataset Audit。

检查：

- Session、Query、Gold requirement 数量；
- 包含 Gold 的 Query 与 independent Query 数量；
- dependency-distance 分布；
- 使用配置的 ShortTerm window 计算 ShortTerm coverage；
- MidTerm/LongTerm eligibility/routing；
- AND/OR Gold 解析；
- 无效依赖或 future dependency；
- cross-session leakage；
- 重复 Query ID；
- 缺失的历史 target；
- 显式历史位置泄漏，例如直接引用历史问题、turn 编号，或模板化的“往前 N 轮”措辞；
- dependency distance 是否异常集中在某一位置；
- 如果可检测，检查明显的 Query/Answer 模板化或重复；
- Benchmark provenance 一致性。

写入：

```text
dataset_audit.json
dataset_audit.md
```

如果 structural correctness 检查失败，停止并返回：

```text
DATASET_AUDIT_FAILED
```

如果仅发现数据质量警告，只要指标仍然可解释，可以继续运行，但必须将本次运行标记为：

```text
DATASET_QUALITY_WARNING
```

绝不能通过调参来“绕过”一个有结构性问题的 Benchmark。

### 2. 建立 Baseline

建立两个完全独立的 Baseline：

- `midterm_baseline`：来自可重放的 `production_midterm_v1` checkpoint；这是 tuning、validation 和 winner selection 唯一允许使用的 Baseline。
- `full_memory_regression_baseline`：可选的完整 production trace，仅在 winner 选出后用于 ShortTerm/LongTerm/All-memory/Union regression。如果不可用，报告 `N/A` 并说明跳过原因。

生产 contract：

```text
AsyncMemory.add
-> SQLite ShortTerm QA eviction
-> production Page summary prompt
-> production Page embedding/write
-> dense+keyword Session assignment and production Session merge
-> unchanged Query embedding
-> dense Session routing
-> dense Page retrieval within routed Sessions
-> global Page-score dedupe/sort/cap
```

Query-time dense+BM25 fusion 是 Benchmark-only Candidate 扩展。它必须保留生产 Session routing，并基于生产生成的 Page payload 工作。绝不能把它标记为 production baseline。

记录：

- Primary R@K；
- Macro/session R@K；
- MRR；
- 条件允许时记录 R@(2K)、R@(4K)；
- Gold rank 的 mean/median；
- ShortTerm coverage；
- target-layer hits；
- Short+Target union；
- All-memory union/completion；
- runtime；
- LLM calls；
- embedding calls；
- cache reuse；
- failed turns。

只有 evaluation contract 与 Baseline 完全一致的 Candidate 才具有可比性。

### 3. 按 Session 切分

禁止把同一 Session 中的 Query 随机拆到 Tune 和 Validation 两侧。

使用 `references/search_strategy.md` 中定义的 split policy。

写入：

```text
split_manifest.json
```

整个 run 期间必须冻结该 split，不允许中途变化。

如果 Session 数量过少，无法形成可靠 holdout，则使用文档规定的 fallback，并在最终推荐中明确降低置信度。

### 4. Cheap Search

优先搜索低成本、可复用 artifact 的维度。

典型维度：

- Query representation；
- Page representation；
- retrieval `top_k` / candidate-pool size；
- similarity threshold；
- session/page limit；
- dense/keyword score weight；
- 在现有 artifact 支持下的低成本 lexical fusion。

所有实验通过 Branch Registry 注册。首阶段运行 `RetrievalControl`；后续诊断可开启 Query Prompt 迭代、派生 Page representation、dense+Qdrant-BM25、field-aware 或 reranking。新 dataset 不要求预先存在 frozen Query/Page artifact：完全匹配 provenance 时复用，否则自动生成。改变 Page/Query/embedding 的 Branch 必须生成或复用派生 artifact，不能冒充 production baseline。

使用分阶段搜索，不要直接跑完整 Cartesian grid。

每个阶段结束后：

1. 在 Tune Sessions 上评估 Candidate；
2. 剪枝明显劣势 Candidate；
3. 只保留小规模 frontier；
4. 开启新搜索分支前先运行诊断逻辑。

### 5. 诊断与分支选择

根据 R@K 与更深层 Recall 的关系决定下一步应该搜索什么。

示例：

- **R@(4K) 高、R@K 低：** Gold 已经进入候选集，但前排排序不足。优先尝试 Query representation、score fusion、reranking、field weighting。
- **R@(4K) 低：** 候选覆盖不足。优先尝试 Page representation、memory-write summary、embedding、candidate generation。
- **Session 方差较大：** 优先选择稳健、可泛化的配置；扩大搜索范围前先检查失败 Session / failure cluster。
- **Tune 提升但 Validation 下降：** 判定为 overfit，不得晋级。
- **低成本 representation 变化已经稳定获胜：** 不要因为还有更多搜索分支就无意义消耗 LLM budget。

具体规则遵循 `references/search_strategy.md`。

### 6. 多阶段 Branch Search

每个 Branch 必须声明名称、诊断 regime、cost level、required artifacts、candidate generation、execution adapter、provenance contract 和资源需求。当前 Registry 包含：

- `RetrievalControl`；
- `QueryRepresentation`；
- `PageRepresentation`；
- `HybridRetrieval`；
- `Reranking`；
- `Embedding`；
- `FieldAwareMultiVector`；
- `MemoryWriteAddPrompt`。

搜索循环必须是：生成 Candidate → Tune → frontier/prune → 重新诊断 → 选择下一 Branch。Validation 仅在循环停止后运行。停止原因必须来自 budget、`min_improvement_pp`/`patience_stages`、frontier convergence、数据质量或资源约束，不得固定写成 validation selected。按 `search.branch_coverage` 审计当前诊断下的 relevant、attempted、exhausted、remaining 和 blocked Branch；patience 只在 relevant Branch 已覆盖且没有可 refine 的工作时硬停止，否则记录 `PATIENCE_SOFT_EXHAUSTED` 并继续由低成本向高成本推进。

Branch Candidate 默认从当前 Tune frontier anchor 继承已经验证的维度；只有显式 ablation 才可设置 `ablation_from_baseline=true`。Branch 以 `(name, generation_round, effective config hash)` 跟踪，同一 Branch 可以 coarse → refine 后再次进入；`max_rounds`、全局 `max_stages`、candidate hash 去重和 patience 共同防止循环。

`QueryRepresentation` 最多运行三轮，每轮以当前最佳 Query Prompt 为 parent 生成最多三个受控方向，并始终保留 production/original 结果作为全局参照。Rewrite history 必须由每份 production manifest 验证的 `memory_config.midterm.short_term_capacity / 2` 推导，只包含当前 Query 之前仍在 production ShortTerm 的 QA；缺失、非正偶数或 manifest/config 不一致时失败，不能使用默认窗口。artifact identity 必须记录 message/QA window、history policy 和 production config hash。失败模式只来自 Tune requirement rows；Validation 指标、Gold、未来轮次和已离开 ShortTerm 的历史不得进入 Prompt。任一轮低于 `min_improvement_pp`、全部变体无提升、original 仍优或 frontier 不再保留该方向时提前停止。

高成本 Embedding/Reranker Candidate 先在 Tune Session 子集 screening，明显低于当前 frontier anchor 者不进入完整 Tune。

Budget 约束实验层级：`quick` 至多 medium、禁止下载/LLM generation；`standard` 至多 high、模型仅使用本地 cache；`deep` 才允许 expensive Branch、联网模型发现/下载和有明确 provenance 的新生成。各 profile 还分别约束 stage、Branch、Candidate 和 validation frontier 数量。

如果诊断需要的 Branch 未注册，不要直接长期跳过：先用 `rg` 搜索 `exp/benchmark` 的历史实现；抽取通用算法/artifact contract 到 Skill adapter，补最小单测，注册后从当前 run 的 frozen stage 恢复。只有缺少模型、API、资源或必要 provenance 时才记录 `UNAVAILABLE`。

### 7. 模型自动发现

Embedding/Reranker Branch 使用 `model_discovery.py`。`standard` 只扫描并使用本地 cache，禁止联网；`deep` 将本地 cache 与在线发现结果按 `model_id + immutable revision` 合并、去重，再用同一质量规则排序和截取候选，最后才根据 cache 状态决定复用或下载。cache 命中只降低成本，不能增加模型的实验优先级。

筛选优先读取 model card/config/model-index 中的 MTEB/C-MTEB/FinMTEB、multilingual retrieval、retrieval/reranking、语言、architecture、License 与参数量。可可靠解析的实际 metric 写入结构化 `benchmark_scores`；原始分数只在相同 benchmark、task、dataset、metric 内归一化和比较，禁止跨 Benchmark 直接相加，也禁止因为 model card 报告条目更多而奖励质量分。名称、tags 和 downloads 只作为 metadata 缺失时的弱证据。下载复用 HF cache；相同 model ID 与 revision 已存在时不再次调用下载，并记录 source、License、选择原因和资源状态。gated、下载失败或资源不足必须记录 `UNAVAILABLE`，不能中止整次搜索。

每个 embedding 必须先解析并记录 encoding contract，包括 Query/Document prefix 或 instruction、`prompt_name`、normalization 和 pooling。无法从模型自己的 config/model card 或已知官方 family contract 可靠确定时，标记 `UNAVAILABLE`；禁止裸 `SentenceTransformer.encode(text)` 后把结果当作模型能力。

### 8. 高成本 LLM Search

只对 frontier Candidate，且诊断明确表明 representation generation 是瓶颈时运行。

潜在搜索分支：

- Add/memory-write prompt variant；
- context-aware Add variant；
- Page-summary prompt variant；
- bounded Query reference resolution；
- Query rewrite variant。

要求：

- 固定 prompt 文本并记录 hash；
- 记录 LLM model/config；
- 记录精确调用次数；
- 缓存生成的 artifact；
- 不要为了让 ablation 看起来“新鲜”而重新生成已有且有效的 frozen artifact；
- 两个 prompt variant 如果 provenance 无法区分，则禁止直接比较。

在 `budget=deep` 的 coverage 诊断下，`MemoryWriteAddPrompt` 对当前 dataset 通过隔离的真实 `AsyncMemory.add → MidTermUpdater → Page/Session embedding` 链路生成 conservative Add、context-aware Add、evidence-focused summary 和诊断驱动 summary。候选创建时只冻结生成规范；screening 只物化 screening Sessions，只有晋级候选才继续补全 Tune，Validation 在搜索停止后才物化 held-out Sessions。Production Add/Summary 由当前 anchor/baseline 作为参照，不重复生成。Context wrapper 仅改变摘要模型当时可见的输入，不改变原始 Page dialogue、source-turn lineage 或生产代码。

### 9. Validation

只有 Tune frontier 中保留下来的 Candidate 才进入 held-out Validation。

Primary Ranking Criterion 是 Validation requirement-level R@K。

Tie-break 顺序：

1. Validation R@K；
2. Macro/session stability；
3. MRR；
4. R@(2K) / R@(4K)；
5. 更低成本和更低复杂度。

当差异落在 `selection.tie_tolerance_pp` 内时，应视为实际接近，除非重复实验或 Session-level 证据明显支持其中某个 Candidate。

如果 Candidate 在 Tune 上明显提升，但 Validation 下降，标记为：

```text
OVERFIT
```

不得选择该 Candidate。

### 10. 最终推荐

推荐一个配置，并最多附带两个备选：

- `best_stable`：默认推荐；
- `best_accuracy`：只有在准确率确实明显更高但成本也更高时给出；
- `best_low_cost`：只有在成本明显更低且准确率接近时给出。

使用“best”时必须明确它是 tune-best 还是 validation-best。

写入：

```text
leaderboard.csv
search_trace.jsonl
best_config.json
best_config_diff.json
requirement_results.jsonl
session_metrics.csv
final_report.md
run_metadata.json
```

## Candidate 晋级规则

Candidate 满足以下任一条件时可以晋级：

- Primary R@K 有具有实际意义的提升；或
- R@K 基本持平，但 stability/MRR/cost 更好；或
- 具有足够诊断价值，值得触发下游搜索分支。

不能因为 Candidate 只提升了某一个 Session，却明显损害其他 Session，就让它晋级。

当改进已经根据 `search_space.yaml` 中的 stopping rules 进入平台期后，不要继续无边界扩大搜索空间。

## 报告要求

`final_report.md` 必须包含：

- dataset 与 provenance；
- 配置的 `K`；
- ShortTerm window；
- Tune/Validation split；
- Baseline metrics；
- 实际执行过的 search stage；
- 被跳过的 branch 及原因；
- Tune leaderboard；
- Validation leaderboard；
- per-Session metrics；
- best stable configuration；
- 相对 Baseline 的 diff；
- percentage point 的绝对提升；
- R@(2K)/R@(4K) 上下文；
- MRR 和 rank diagnostics；
- all-memory union/completion impact；
- cost：runtime、LLM calls、embedding calls；
- overfit Candidate；
- data-quality warning；
- stop reason；
- confidence/limitations。

当 `k=10` 时，所有 Primary label 和报告文本必须写 `R@10`，不能写 `R@5`。

## 最终检查

完成前确认：

- [ ] Dataset Audit 已通过，或所有 warning 均已明确记录。
- [ ] `k` 来自用户 override 或默认值 `5`。
- [ ] evaluator 中没有硬编码 `5`。
- [ ] Tune 和 Validation 在 Session 维度完全不重叠。
- [ ] Baseline 与 Candidate 使用相同的 Gold/routing/evaluation contract。
- [ ] Candidate artifact 属于当前 dataset/config，或其 provenance 已验证。
- [ ] failed-turn count 为 0，或者这些 failure 已明确判定为使结果无效。
- [ ] 不存在 future/cross-session memory leakage。
- [ ] 最佳配置由 held-out Validation 选择，而不是仅根据 Tune score。
- [ ] Search 已因为一个可记录的 reason 停止。
- [ ] `best_config.json` 可复现。
- [ ] `final_report.md` 明确区分实证结果与历史先验。
