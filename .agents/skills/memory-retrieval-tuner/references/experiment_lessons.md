# Experiment Lessons

These notes summarize prior experimental evidence and should guide search order.

They are **priors, not rules**. A new dataset may reverse any of them.

## 1. The most important lesson: do not freeze the old winner

A sequence that improved one development benchmark can fail to transfer.

Historically, a pipeline combining:

- bounded Query reference resolution;
- removal of Raw User from Page text;
- context-aware Add/Page generation;

improved an earlier benchmark step by step.

On a later rebalanced benchmark, that same progression reversed and the simpler production/original-query configuration performed substantially better.

Therefore:

> The Skill must freeze the **method of experimentation**, not the previous winning parameter values.

Every transferred component must earn its place again on the new dataset.

## 2. Query rewriting

### Prior observation

Generic rewrite was not reliably helpful.

A bounded reference-resolution prompt that only resolved local expressions such as “上一轮 / 刚才 / 前面 / 接着 / 之前的判断” was more promising on an earlier benchmark.

### Transfer observation

The previously best bounded reference-resolution variant later became negative relative to the original Query.

### Search implication

Order:

1. original Query;
2. bounded reference resolution;
3. generic rewrite only if diagnostics justify it.

Never assume “more explicit Query” means better historical retrieval.

## 3. Page representation

### Prior observation

On an earlier benchmark, `Summary + Keywords` outperformed representations containing Raw User, while `Raw User only` was weak.

### Transfer observation

On the later dataset, removing Raw User became negative.

### Search implication

Always re-test at least:

- production/full representation;
- Summary;
- Summary + Keywords;
- Summary + Keywords + Raw User/full equivalent.

Do not permanently delete Raw User based on old results.

Representation choice is dataset-dependent.

## 4. Context-aware Add / memory-write generation

### Prior observation

Adding previous context plus only the downstream context that truly existed at eviction time strongly improved an earlier benchmark.

### Transfer observation

On the later rebalanced benchmark, Context Add became the largest negative transferred component.

### Search implication

Treat context-aware Add as an **expensive optional branch**, not the default.

Open it mainly when diagnostics show:

- candidate coverage is poor;
- Page summaries omit relation/dependency evidence;
- non-context Page representations cannot recover the needed evidence.

Always compare it against production Add under the same Page representation and Query representation when possible.

## 5. Embedding models

### Prior observation

Several alternative embedding models failed to produce a stable improvement over the existing BGE-small Chinese baseline. Some were close; others regressed.

### Search implication

Do not start by sweeping many embedding models.

First diagnose whether the bottleneck is candidate coverage rather than top-rank ordering.

Use embedding search as a secondary branch and keep the candidate list small.

## 6. BM25 and hybrid retrieval

### Prior observation

BM25 and dense+BM25 fusion did not consistently beat the strongest dense configuration in earlier experiments.

### Search implication

Hybrid search remains useful when the new dataset contains:

- entity-heavy references;
- exact financial terms;
- identifiers;
- numbers;
- lexically distinctive historical facts.

But it should be conditional, not mandatory.

If used, start from dense-heavy fusion and keep the dense baseline in the same comparison.

## 7. Reranking and candidate pools

### Prior observation

Some experiments increased candidate coverage without improving Top-K retrieval. LLM historical-dependency reranking also failed in at least one prior setup.

### Search implication

Before reranking, compare configured R@K with deeper recall.

If:

```text
R@K << R@(4K)
```

then reranking may be justified.

If:

```text
R@(4K) is also poor
```

reranking is unlikely to solve the primary problem; improve candidate generation first.

## 8. Multi-vector / MaxSim / field-aware scoring

### Prior observation

More complex similarity decompositions and MaxSim variants did not yield a stable win in earlier experiments.

### Search implication

Keep these in the advanced branch.

Only open them when:

- single-vector representation appears to collapse distinct evidence;
- field-specific diagnostics show useful complementary signals;
- cheaper representations have plateaued.

Complexity is not itself evidence of better retrieval.

## 9. Candidate coverage versus Top-K ranking

One of the most reusable diagnostic ideas from the prior work is to distinguish:

1. **coverage failure** — Gold never enters the broader candidate set;
2. **ranking failure** — Gold enters a deeper candidate set but misses top K.

This determines the next experiment much better than blindly changing parameters.

Use configured `K`, not literal 5:

- compare `R@K`;
- compare `R@(2K)`;
- compare `R@(4K)`;
- inspect Gold rank transitions.

## 10. ShortTerm must stay separate from MidTerm tuning

Prior benchmarks showed that a large fraction of Gold requirements may already be satisfied by the most recent ShortTerm window.

This can make MidTerm denominators misleading if short-term-satisfied requirements are counted as though MidTerm were responsible for them.

### Search implication

Always report:

- total Gold requirements;
- ShortTerm-satisfied requirements;
- outside-ShortTerm / routed MidTerm requirements;
- target-layer R@K on its eligible denominator;
- Short+Mid union;
- All-memory union/completion.

Do not claim an end-to-end gain solely from a local MidTerm percentage.

## 11. OR-group semantics

Some older datasets contained OR Gold groups.

An OR group is one requirement: retrieving any accepted member satisfies it.

### Search implication

The Gold parser must preserve AND/OR semantics before tuning.

Do not change the denominator when changing K or retrieval configuration.

## 12. Benchmark construction can dominate the apparent optimum

A later rebalanced dataset contained many highly explicit long-history questions and a strong concentration at a fixed dependency distance.

That structure can make simple Original Query / lexical similarity look unusually strong and can punish contextual transformations that were helpful on more implicit references.

### Search implication

Dataset audit is not optional.

Before interpreting a large reversal as a retrieval breakthrough, check:

- whether the query explicitly leaks the historical target;
- dependency-distance concentration;
- template patterns;
- whether long-range turns are genuinely natural.

If quality is questionable, mark the best configuration as benchmark-sensitive.

## 13. Frozen artifact reuse is good methodology

When two Page-generation methods have already produced valid artifacts on the same dataset with validated prompt provenance, reusing them is preferable to regenerating them during an ablation.

Benefits:

- avoids LLM stochasticity;
- saves cost;
- makes comparisons cleaner;
- preserves reproducibility.

### Search implication

“No new LLM calls” does **not** mean a summary was never generated by an LLM. It may mean the run reused a previously generated frozen artifact.

Record both:

- original generation provenance;
- current-run reuse/call counts.

## 14. Session isolation matters

A prior concurrent benchmark run using shared local vector-store state across Sessions produced incomplete/invalid execution.

Per-Session isolation fixed the issue.

### Search implication

Parallelism must not allow Session collections/runtime state to interfere.

Validate expected turn count and failed-turn count after every source run.

An incomplete fast run is not a valid candidate.

## 15. Generalization beats incremental in-sample gains

Repeatedly optimizing on the same small Session set can overfit the benchmark even without training a model.

### Search implication

Use Session-disjoint validation.

A candidate that improves tune R@K but regresses on validation should be labeled `OVERFIT`, even if it is the highest tune result.

Prefer a stable near-tied candidate over a fragile tiny gain.

## 16. Search in layers of cost

Prior work included many expensive experiments that were informative but did not improve the final metric.

The reusable lesson is to order experiments by cost:

1. reuse existing rankings/artifacts;
2. production-checkpoint replay with frozen Query/Page/Session vectors;
3. numeric retrieval parameters;
4. additional embeddings;
5. reranking;
6. new LLM-generated Page/Query artifacts.

This makes dataset transfer much faster.

## 17. Never optimize only one number

Primary selection should be validation requirement-level R@K, but final interpretation should include:

- Macro/session R@K;
- R@(2K), R@(4K);
- MRR;
- Gold-rank distribution;
- per-Session variance;
- Short+target union;
- All-memory union/completion;
- runtime and model-call cost.

A +0.2 percentage-point R@K gain with worse stability and much higher cost may not be the better configuration.

## 18. What should be reused from the repository

Prefer adapting existing benchmark infrastructure rather than rebuilding it.

Likely reusable categories include:

- benchmark common utilities;
- memory/runtime construction;
- isolated Session runner;
- Gold parser;
- MidTerm retrieval evaluator;
- Page representation diagnostics;
- Page recall diagnostics;
- reference-resolution ablations;
- embedding ablations;
- BM25/hybrid ablations;
- reranking experiments;
- frozen results under `exp/results/`.

Verify current local paths and APIs before reuse.

The reusable forms of these capabilities now live behind the Skill's Branch Registry and self-contained adapters. Treat the old `exp/benchmark` scripts as evidence and extraction sources, not runtime imports. New datasets should consume production checkpoints and content-addressed derivatives rather than the single-dataset snapshot formats directly.

Model names in historical experiments are also priors. Embedding and reranker Branches should discover resource-compatible current candidates from the local Hugging Face cache and, under the deep budget, metadata search. Record unavailable/gated models instead of substituting a different model silently.

## 19. What should not be hard-coded into the Skill

Do not hard-code:

- C3 as the best method;
- Original Query as the best method;
- one Page representation;
- one embedding model;
- one Add prompt;
- R@5;
- a specific dataset;
- a specific Session split;
- a fixed dependency distance;
- a fixed “improvement path”.

Hard-code only the experimental discipline:

> audit -> baseline -> split -> cheap search -> diagnose -> conditional expensive search -> held-out validation -> stable selection.
