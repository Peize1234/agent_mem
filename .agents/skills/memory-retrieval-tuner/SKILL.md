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

Research 决策只允许使用当前 run 的 Tune 实验事实，以及从 Baseline 到当前 Stage 的完整当前-run轨迹；跨 run winner、旧数据集指标和历史经验不得作为搜索先验。

运行前先阅读：

- `references/search_strategy.md`
- `search_space.yaml`

## 输入参数

必填：

- `dataset`：Benchmark 数据集路径。

可选：

- `k`：Primary Metric `R@K` 使用的 Recall cutoff，默认：`5`。
- `budget`：`quick | standard | deep`，默认：`standard`。
- `target`：`midterm` 或 `all_memory`。完整评价固定使用 `Turn.required_context`，并联合 ShortTerm、MidTerm 与 Fine-grained cross-session LongTerm。
- `sessions`：可选的 Session 子集。
- `seed`：数据划分/搜索随机种子，默认从 `search_space.yaml` 读取。
- `resume`：已有调参运行目录，用于恢复运行。
- `output_dir`：输出根目录，默认：`exp/results/auto_tuning`。
- `memory_config`：可选的 Repository Production Memory 部分覆盖。显式传入时先与 `load_production_memory_config()` 合并，再通过 `MemoryConfig` 补齐未声明字段；未传入时直接使用仓库级 Production 配置。resume 始终复用该 run 已冻结的 `resolved_memory_config.json`。
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

## Complete Agent Memory contract

评价分母固定来自 `Turn.required_context`（没有该字段的 legacy ID-only workbook 才使用兼容逻辑），不会因 ShortTerm window/capacity 改变。required context 支持 `(A OR B) AND C`：OR group 命中任一成员，AND group 各自必须命中。数字、百分比、日期、金额、实体和单位先做确定性检查，普通文本才进入受控 Semantic Judge；Embedding similarity 不能单独构成 Gold hit。

最终评价覆盖 ShortTerm + MidTerm + Fine-grained cross-session LongTerm union，并报告 fact/requirement Recall@K、macro session recall、MRR、candidate-pool recall、final-context recall、context precision、mean returned pages、每层 contribution、query completion、session stability、runtime、LLM/embedding calls。历史 artifact 中的 `session_longterm` 字段仅作为兼容名称。诊断 artifact 可记录 routed pool、global supplement、threshold 和 final visible hit，但这些 Gold 细节不会进入 Research LLM。

参数由 `scripts/tuner/parameter_schema.py` 分为 query-time/retrieval-only、source-changing、within-session-stateful、cross-session-temporal-stateful 和 production-fixed。Source-changing 参数必须重新执行真实 Add/Mid-term source generation；Evolution/Heat 参数必须真实 replay；Promotion/Cross-session 参数当前仅保留 future schema，不能在本 Benchmark 调优；retrieval-only 才允许复用 source artifact。`longterm_other_session_weight` 从 Production 读取固定默认值 0.7，但因当前没有可靠 Cross-session Gold，明确不进入自动搜索空间。所有 hard constraint 同时由 YAML 与 Python 校验：Mid-term final `max_total_pages` 为 1..5，Fine-grained LongTerm `longterm_top_k` 为 1..30，Mid-term candidate multiplier 为 1..8，Agentic 固定 `max_iterations=2`、`max_tool_calls=1`；`max_tool_result_chars` 从 effective Production config 读取并作为 trace 固定执行条件校验，不参与调参；仅 `max_queries=1..3` 与 `max_total_results=1..5` 可调。生产 `MidTermRetriever` 当前忽略 `candidate_pool_size`，因此它不进入 search space 或 Agentic provenance。

`WithinSessionStatefulReplay` 严格执行 `Search(Qn) -> valid recall/Heat update -> Add(Qn, An)`；搜索时不加入当前 turn，遗忘只使用 production `turn_index`，不使用 `page_sequence`。Fine-grained LongTerm source 在每个完整 QA 后立即生成，不再依赖 ShortTerm eviction；检索保留 Production 的 user hard filter、current/all-session 双路候选和 Session weight。当前 Benchmark 没有可靠 Cross-session Gold，因此不调优跨 Session 权重或 Promotion 参数。结构检查状态必须为 `CROSS_SESSION_TUNING_UNSUPPORTED_NO_GOLD`，不能报告这些参数已 validated/tuned/optimized/selected。

Query、Page、Session merge、Fine-grained LongTerm extraction prompt 是独立 branch。Query baseline 是 Production P0 reference-resolution Prompt，并直接复用 Production message builder/parser；Query candidate 只改变 query representation。所有 Page Prompt candidate 固定复用 Production `previous raw + current raw + following raw` context contract，不能再搜索是否启用上下文。Query Rewrite 每轮最多 3 variants、最多 3 rounds，以 Tune 当前最佳 parent 并始终保留 Production P0；prompt/source identity 变化会使 downstream artifact 失效。Research LLM 只能从 Python 生成的 legal action ID 中选择，永远看不到 Validation、Gold answer、required_context 原文或 future turns。

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
- Gold denominator 固定使用 `Turn.required_context`，与 ShortTerm capacity/window 无关；ShortTerm、MidTerm、Fine-grained LongTerm 的可见 union 共同决定 hit。
- ShortTerm coverage 单独报告。
- 同时报告端到端 union/completion 指标，避免把局部 MidTerm 提升误认为整体 Memory 提升。
- Micro requirement-level R@K 与 Macro/session-level 指标必须分开报告。

## 自主执行规则

除非确实存在缺失依赖、无效数据集、模型/API 不可用，或需要用户批准的破坏性操作，否则应端到端完成整个工作流。

不要每完成一个阶段就停下来询问下一步做什么，应按照下面的搜索策略继续执行。

生产配置仍是公式与默认值的唯一来源。当前允许读取的配置包括 Mid-term 全局补充池倍率、Fine-grained LongTerm over-fetch 倍率、实体匹配阈值，以及固定为 `0.7` 且不自动搜索的 `longterm_other_session_weight`。Benchmark-only 诊断不得回写生产配置或把 Gold 带入生产分支。

如果一个候选会改变最终检索结果、写入内容或 embedding，它必须先由 `mem0/` 的生产配置和生产类实现。Tuner 只通过 `production_overrides` 选择这些能力。Benchmark 评价、诊断记录、实验调度和报告优先使用：

1. 现有 Benchmark / Evaluation 代码；
2. 实验 adapter / 配置覆盖；
3. 本 Skill `scripts/` 下的新 adapter；`exp/benchmark/` 仅作为 legacy 参考，不得成为核心运行依赖。

完整 candidate/threshold/ranking trace 由 Skill 内 `DiagnosticMidTermRetriever` 通过覆盖 production `_on_stage` hook 记录；它不得覆盖或复制 `search()`。Long-term hybrid preset 只映射为 production `semantic_weight` / `bm25_weight` / `entity_weight`，实际打分只调用 `mem0.utils.scoring.score_and_rank`。Source Prompt 必须写入 `MemoryConfig`，由真实 `QueryResolver`、`MidTermUpdater` 和 Fine-grained LongTerm extraction pipeline 执行。Agentic 与普通 Mid-term 合计 `<=5` 仅是 tuner evaluator 的 context constraint，不得据此改写 Production 的 `max_total_results` 或 `MemoryToolExecutor` 截断语义；Production 固定的 `max_tool_result_chars` 必须原样进入 Candidate 和 Agentic trace provenance。

任何会改变 source 的 LLM 请求选项也必须写入 production `midterm.*_request_options` 或 `fine_grained_longterm.extraction_request_options`。Tuner 的 LLM wrapper 只能记录诊断；历史 `benchmark_runtime.deepseek_*_non_thinking` flag 必须先迁移成上述可部署 Production 配置，不能在 wrapper 中私自改请求。

只有当用户明确要求落地所选配置时，才允许修改上述边界之外的生产代码。

## 自包含与 artifact 复用原则

核心运行不能 import `exp/benchmark/`。通用数据/Gold 解析、production runtime wrapper、检索原语、Branch、模型发现和派生 artifact 构建均位于 `scripts/tuner/`。`exp/results/` 只能作为通过完整 provenance 校验的 frozen source artifact 来源，不能向 Research LLM 提供旧 run 指标、winner 或经验。

新增能力只复用可验证的 artifact contract；不要整份复制单数据集脚本，也不要把旧 winner 固化为默认。frozen artifact 必须通过 dataset、prompt/model、representation、production config 和内容 hash 校验。

主要模块：

- `experiment_branches.py`：Branch Registry 与实验 adapter；
- `staged_search.py`：Tune-only successive filtering；
- `research_evidence.py` / `research_policy.py`：Tune-only 结构化证据与 Python legal action 空间；
- `research_decision.py` / `research_runtime.py`：Research LLM 决策、严格校验、重试、cache 与完整 trace；
- `derived_artifacts.py`：Query 派生 artifact 与 content-addressed cache；Page/Session/field 向量只能由 production source generation 生成；
- `prompt_artifacts.py`：Query Prompt 受控迭代、逐 Query 原子缓存与恢复；
- `source_prompt_variants.py`：Add/Page Summary 受控 Prompt 文本和生成 metadata；Prompt 通过 Production config 执行；
- `diagnostic_midterm_retriever.py`：只覆盖 production diagnostic hook、记录阶段 payload，不实现 retrieval；
- `generated_source_artifacts.py`：Add/Page Prompt 候选按 screening/Tune/Validation Session 延迟物化；
- `encoding_contract.py`：发现并描述模型的 Query/Document encoding contract；实际编码由 production embedder 执行；
- `model_discovery.py`：cache-aware 的 Hugging Face 发现、统一质量排序、下载与 smoke；
- `benchmark_support.py` / `production_runtime.py`：自包含 benchmark schema 与生产 runtime wrapper；
- `production_midterm_adapter.py`：生产 checkpoint 生成和隔离 replay。

Skill 不维护独立的完整 Memory 默认配置。新 run 从仓库唯一入口 `mem0.configs.production.load_production_memory_config()` 取得部署 provider/model overrides，再由 `MemoryConfig` 解析 effective config；显式配置只覆盖其中声明的字段。effective config 会冻结到运行目录，resume 禁止重新读取今天的 Production 默认。`search_space.yaml` 只定义可调参数、搜索范围和 hard constraints，不承担 baseline 默认值。

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
-> complete QA creates Fine-grained LongTerm extraction job
-> background per-QA LongTerm extraction
-> SQLite ShortTerm QA eviction
-> previous Page raw + current QA + following QA fixed context
-> production Page summary prompt
-> production Page embedding/write
-> dense+keyword Session assignment and production Session merge
-> Production P0 ShortTerm query reference resolution
-> resolved retrieval Query embedding
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

所有实验通过 Branch Registry 注册。首阶段运行 `RetrievalControl`；后续诊断可开启 Query Prompt 迭代、派生 Page representation、dense+Qdrant-BM25、multi-vector MaxSim 或 cross-encoder reranking。新 dataset 不要求预先存在 frozen Query/Page artifact：完全匹配 provenance 时复用，否则自动生成。Query/Page representation Branch 必须生成或复用匹配的派生 artifact；Embedding model Branch 必须重新执行真实 Production Add、Page generation 和 Session formation。两者都不能冒充 production baseline。

使用分阶段搜索，不要直接跑完整 Cartesian grid。

每个阶段结束后：

1. 在 Tune Sessions 上评估 Candidate；
2. 剪枝明显劣势 Candidate；
3. 只保留小规模 frontier；
4. 开启新搜索分支前先运行诊断逻辑。

### 5. 诊断与分支选择

根据 R@K 与更深层 Recall 的关系决定下一步应该搜索什么。

示例：

- **R@(4K) 高、R@K 低：** Gold 已经进入候选集，但前排排序不足。优先尝试 Query representation、score fusion、cross-encoder reranking、multi-vector MaxSim。
- **R@(4K) 低：** 候选覆盖不足。优先尝试 Page representation、memory-write summary、embedding、candidate generation。
- **Session 方差较大：** 优先选择稳健、可泛化的配置；扩大搜索范围前先检查失败 Session / failure cluster。
- **Tune 提升但 Validation 下降：** 判定为 overfit，不得晋级。
- **低成本 representation 变化已经稳定获胜：** 不要因为还有更多搜索分支就无意义消耗 LLM budget。

具体规则遵循 `references/search_strategy.md`。

### 6. 多阶段 Branch Search

每个 Branch 必须声明名称、诊断 regime、cost level、required artifacts、candidate generation、execution adapter、provenance contract 和资源需求。当前 Registry 包含：

- `RetrievalControl`；
- `AgenticRetrieval`（仅消费 dataset、当前 Anchor retrieval/source/query/LongTerm semantic parent identity、固定 `max_tool_result_chars` 以及精确参数组合全部匹配的 `production_agentic_trace`；缺失或任一 mismatch 时明确 `UNAVAILABLE`）；
- `QueryRewritePrompt`（当前 Query Prompt 搜索的唯一可达 Branch；旧 `QueryRepresentation` 仅保留兼容 adapter，不进入默认 coverage）；
- `PageRepresentation`；
- `HybridRetrieval`；
- `Reranking`；
- `Embedding`；
- `FieldAwareMultiVector`；
- `FineGrainedLongtermRetrieval`（旧名 `SessionLongtermRetrieval` 仅保留兼容 alias）；
- `MidtermPageSummaryPrompt`、`MidtermSessionMergePrompt`、`FineGrainedLongtermExtractionPrompt`；
- `MidtermSourceConfig`；
- `MidtermEvolution`；
- `Promotion`（仅保留为 future infrastructure，当前默认 branch coverage 禁用）。

搜索循环必须是：生成 Candidate → Tune → frontier/prune → 重新诊断 → 选择下一 Branch。Validation 仅在循环停止后运行。停止原因必须来自 budget、`min_improvement_pp`/`patience_stages`、frontier convergence、数据质量或资源约束，不得固定写成 validation selected。按 `search.branch_coverage` 审计当前诊断下的 relevant、attempted、exhausted、remaining 和 blocked Branch。纯 deterministic 模式继续把 relevant coverage 用作 patience 的完整性 gate；Research 模式只强制 required coverage，并在 patience 生效时让模型在 Python 提供的继续/stop legal actions 中作结果导向选择。

Branch Candidate 默认从当前 Tune frontier anchor 继承已经验证的维度；只有显式 ablation 才可设置 `ablation_from_baseline=true`。Branch 以 `(name, generation_round, effective config hash)` 跟踪，同一 Branch 可以 coarse → refine 后再次进入；`max_rounds`、全局 `max_stages`、candidate hash 去重和 patience 共同防止循环。

Stage 1 始终由 Python deterministic policy 执行 `RetrievalControl`，不调用 Research LLM。从 Stage 2 开始，每个下一 Stage 选择边界只调用一次 Research Decision（单次 Decision 最多三次 retry）：Python 先完成 diagnose、coverage、原 `registry.select()` deterministic plan 以及带唯一 `action_id` 的 legal action space，Research LLM 只能选择这些 ID，随后 Python 再校验 budget、round、resource、required action 和 schema。三次 API/JSON/schema/action 校验均失败时，必须原样执行本轮预先计算的 `registry.select()` 结果，禁止另写替代策略。

Coverage 分为 `required`、`selectable` 和 `expensive_gated`。`RetrievalControl` 是 required；其他研究 Branch 默认 selectable；Add/Page Prompt 等高成本 Branch 由 Python 的 budget / max cost / remaining expensive candidates / max rounds / resource / exhausted / blocked 等硬条件决定是否可执行。required coverage 未完成时不能越过 required；一旦 Python 判断 expensive Branch 当前可执行，即使仍有 selectable Branch remaining，该 action 也可以进入 `legal_actions`，由 Research LLM 判断现在是否值得跑。Research LLM 必须为所有未选择的 Branch action 给出临时 `DEPRIORITIZED` reason，但不能写入 `EXHAUSTED`/`BLOCKED`；Evidence、anchor 或合法动作变化后，该 Branch 可重新进入。真正的 exhausted/blocked 仍只由 Python 的无候选、max rounds、budget 或 resource 规则产生。Research 正常启用时不以“所有 selectable Branch 都跑过”为目标，可以在 required coverage 完成且 patience/frontier convergence 已触发时选择 Python 提供的 stop action。stop_reason 必须区分 `frontier_converged` 与 `global_patience`；两者同时触发时优先记录 `frontier_converged`。

Research Prompt 只能包含 Tune Sessions 的聚合实验史、Candidate config diff/指标、frontier、failure distribution、diagnosis、deterministic plan、Branch 状态与硬约束。不得传入 held-out Validation、Gold dependency、答案或 future turns。每个 attempt 必须先向 `research_trace.jsonl` 写 REQUEST，再写 RESPONSE；非法响应、exception、retry、cache hit 和 deterministic fallback 均完整记录，敏感配置必须 redact。Decision cache identity 至少覆盖 dataset、Tune scope、anchor/evidence/legal-action hash、Branch registry/config 状态、Prompt、模型配置和 schema。

`QueryRewritePrompt` 最多运行三轮，每轮以当前最佳 Query Prompt 为 parent 生成最多三个受控方向，并始终保留 Production P0 结果作为全局参照。旧 `QueryRepresentation` 类只作为兼容 adapter，默认 `branch_coverage` 不会同时调两套重复的 Prompt 逻辑。Rewrite history 必须由每份 production manifest 验证的 `memory_config.midterm.short_term_capacity / 2` 推导，只包含当前 Query 之前仍在 production ShortTerm 的 QA；缺失、非正偶数或 manifest/config 不一致时失败，不能使用默认窗口。artifact identity 必须记录 P0 Prompt hash、message/QA window、history policy 和 production config hash。失败模式只来自 Tune requirement rows；Validation 指标、Gold、未来轮次和已离开 ShortTerm 的历史不得进入 Prompt。任一轮低于 `min_improvement_pp`、全部变体无提升、P0 仍优或 frontier 不再保留该方向时提前停止。

高成本 Embedding/Reranker Candidate 先在 Tune Session 子集 screening，明显低于当前 frontier anchor 者不进入完整 Tune。

Budget 约束实验层级：`quick` 至多 medium、禁止下载/LLM generation；`standard` 至多 high、模型仅使用本地 cache；`deep` 才允许 expensive Branch、联网模型发现/下载和有明确 provenance 的新生成。各 profile 还分别约束 stage、Branch、Candidate 和 validation frontier 数量。

如果诊断需要的 Branch 未注册，不要直接长期跳过：补齐当前 production adapter 的最小 artifact contract、单测和注册，再从当前 run 的 frozen stage 恢复。只有缺少模型、API、资源或必要 provenance 时才记录 `UNAVAILABLE`。

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

在 `budget=deep` 的 coverage 诊断下，三个 source prompt branch 分别通过隔离的真实 `AsyncMemory.add → per-QA Fine-grained LongTerm job / MidTermUpdater → Page/Session embedding` 链路生成候选。Page Prompt candidate 全部使用相同的 Production previous/current/following raw contract；只有 Prompt 文本变化。候选创建时只冻结生成规范；screening 只物化 screening Sessions，只有晋级候选才继续补全 Tune，Validation 在搜索停止后才物化 held-out Sessions。Production Add/Summary 由当前 anchor/baseline 作为参照，不重复生成。每个 branch 的 source representation、artifact hash 和下游失效范围独立记录。

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
research_trace.jsonl
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
- [ ] `final_report.md` 明确区分当前-run实证结果、生产默认与未验证项。
