# Agent Memory Demo Lab

The Demo Lab is an optional Streamlit application for inspecting one complete
agent turn without putting UI state or display branches in the core memory
runtime.

## Runtime boundary

Production applications continue to use the standard class:

```python
from mem0 import Memory

memory = Memory(config)
```

The lab uses `DemoMemory`, a `Memory` subclass that freezes retrieval context,
builds the displayed prompt from that exact context, commits through
`Memory.add()`, and exposes a manual `DemoBackgroundWorkerManager`:

```python
from memory_monitor.runtime import DemoMemory

memory = DemoMemory(config)
```

`DemoMemory` does not replace mid-term extraction, long-term extraction,
profile extraction, vector search, or short-term eviction algorithms. Its
worker calls the production stage-level entry points:

```text
process_midterm_job(job_id)
process_longterm_job(job_id)
process_profile_job(job_id)
```

The production `BackgroundWorkerManager` remains unchanged.

## Start the lab

The launcher uses the repository Production configuration from
`mem0.configs.production.load_production_memory_config` and prompts for
`DEEPSEEK_API_KEY` when it is not already in the environment. It uses:
`deepseek-v4-flash` and `BAAI/bge-small-zh-v1.5` (512 dimensions).
It runs in the `MemoryOS` Conda environment, activating it through `conda run`
when necessary. By default the launcher uses the versioned sandbox root
`.memory_monitor_runs/demo-lab-v3`. This intentionally avoids opening the
temporary v2 core databases that predate the `midterm_status` /
`longterm_status` task columns. Set `MEMORY_MONITOR_SIMULATION_ROOT`
explicitly only when you deliberately want another root.

By default the server listens on `0.0.0.0`, while the launcher prints the
browser address `http://localhost:8501`. This makes the plain launcher command
reachable from Windows through WSL's localhost forwarding without presenting
the non-browsable wildcard listening address. Set `MEMORY_MONITOR_ADDRESS` to
override the listening address when needed.

```bash
./memory_monitor/start_demo_lab.sh
```

The API key is exported only to the launcher process and is not written to the
configuration file. To use another Mem0 configuration or change the runtime
location and listening port:

```bash
export MEMORY_MONITOR_MEMORY_CONFIG=/path/to/mem0-config.json
export MEMORY_MONITOR_SIMULATION_ROOT=/path/to/demo-runs
export MEMORY_MONITOR_ADDRESS=127.0.0.1
export MEMORY_MONITOR_PORT=8502
./memory_monitor/start_demo_lab.sh
```

If `MEMORY_MONITOR_MEMORY_CONFIG` points to a missing file, the launcher warns
and falls back to the repository Production configuration. A supplied JSON
file is treated as a partial override of that configuration.

Agentic memory retrieval follows the repository Production configuration.
Set `agentic_retrieval.enabled` in an explicit override to load only short-term context and
the user profile before generation, then let the answer model make at most one
mid-term memory tool call containing one to three complementary queries. The
queries run concurrently and return complete mid-term pages, capped by
`agentic_retrieval.max_total_results`. The flow uses one model call when
context is sufficient and at most two when retrieval is needed. It stays inside
the existing `generate_response` node; only the original user message and final
answer continue to `Memory.add()`.

The Conda environment name can be overridden when needed:

```bash
MEMORY_MONITOR_CONDA_ENV=another-env ./memory_monitor/start_demo_lab.sh
```

The page supports:

- next step;
- run through model generation;
- run the four memory branches;
- run all or continue all remaining work without waiting;
- retry one selected failed step;
- reset an uncommitted turn.

The pipeline controls contain one four-toggle row named “本轮记忆步骤开关”.
Each switch reads the selected turn's persisted `demo_step_runs.is_held`
value. Clearing a pending switch holds that one step as `pending`; it never
turns the step into `skipped` and never counts it as complete. Re-enabling a
held step atomically releases it and immediately submits it when its dependency
is ready. If short-term has not finished yet, the released downstream step
remains pending and the persisted execution target submits it automatically
after `Memory.add()` succeeds.

Queued, running, succeeded, and failed steps have individually disabled
switches, while another held pending step in the same turn remains editable.
This lets an older incomplete turn stay active while a newer turn runs.
Returning to that old turn restores each switch directly from `demo.db`;
releasing it continues only that turn and step without replaying successful
work.

## Pipeline and concurrency

The displayed foreground flow exposes the production query rewrite as a
derived node:

```text
捕获输入 → 问题重写 → 分层检索 → Agentic 检索 → 构建 Prompt → 模型回答
```

“问题重写” is not a persisted or executable Demo step. Its status, original
query, retrieval query, and LLM trace are derived from the existing
`retrieve_context` output, so displaying it does not call `QueryResolver`
again. The persisted pipeline remains a DAG rather than an ordered table:

```text
capture_input → retrieve_context → agentic_retrieval → build_prompt → generate_response
                                                    ├── run_shortterm ─┐
                                                    ├── run_midterm ───┤
                                                    ├── run_longterm ──┤
                                                    └── run_profile ───┤→ complete_turn
```

`complete_turn` is derived from the four branch rows and is never persisted as
an executable step. In the normal Demo flow it becomes successful only when
all four memory steps are `succeeded`; a held pending step keeps the turn
active. The `skipped` terminal remains only for system-level compatibility,
never for the user-facing blocker switches. There is no commit node and no
refresh node. The real backend dependency remains accurate:
`run_shortterm` calls `Memory.add()` once, and mid-term, long-term, and profile
wait for the job IDs created by that transaction. Their parallel visual nodes
show “waiting for task creation” until then.

The graph remains one compact left-to-right row. The four memory nodes form one
vertical column between the model node and the centered completion node. It
fits the normal desktop detail pane without a horizontal scrollbar; only a
very narrow pane scrolls horizontally instead of changing to a four-column or
2×2 layout. A fixed-width inline SVG arrow connects model generation to the
fork, and the fork and merge arms align with the four node centers.

Every open sandbox owns one `DemoBackgroundCoordinator`:

- one bounded foreground executor chains capture, retrieval, prompt building,
  and model generation; different turns can use different workers;
- four separate `ThreadPoolExecutor(max_workers=1)` queues run short-term,
  mid-term, long-term, and profile work;
- each memory type is FIFO across turns, while different types can overlap;
- queueing first persists `queued`; the executor changes it to `running` only
  after its worker starts;
- leases are heartbeated and every completion is fenced by an execution token.

All buttons only persist an execution target and submit eligible, unheld work.
They do not wait for `Future.result()`. A callback schedules the next foreground
step or the newly eligible memory branches after a predecessor succeeds.
“下一步” uses four clicks for the four foreground nodes; its fifth click stores
the whole memory-batch intent. Short-term is submitted once, then its successful
completion immediately fans out every unheld mid-term, long-term, and profile
branch to their independent executors. Held branches do not prevent the others
from running, and the execution target stays persisted until the held work is
released and all target steps finish.

The Streamlit page keeps the selected-turn gates, action buttons, navigation,
DAG, snapshots, jobs, and trace inside one right-side `st.fragment`. That
fragment polls while a turn or backend job is unfinished, then unregisters its
timer after completion; widget interactions still rerender it immediately. A
separate left-side fragment reloads the original transcript from `demo.db` every
400 ms, so a generated assistant reply appears without waiting for the memory
stage. The chat input remains outside both fragments. The database panel reads
the selected partition directly from the isolated backend on each render, while
persisted step snapshots remain the source only for per-step diffs. The keyed
700 px native chat scroll container uses both simulation and session IDs,
`autoscroll=False`, contained overscroll, and a stable scrollbar gutter. This
requires Streamlit 1.56 or newer.

## Storage isolation

Every simulation owns:

```text
.memory_monitor_runs/demo-lab-v3/<simulation_id>/
├── history.db
├── qdrant/
└── demo.db
```

`history.db` and `qdrant/` contain the normal core memory state. `demo.db`
contains only `demo_sessions`, `demo_turns`, `demo_step_runs`, and
`demo_snapshots`. The blocker for each memory node is stored on its step row as
`is_held`; execution intent and the background-submission timestamp are stored
on `demo_turns`. Legacy `run_*` columns remain for compatibility, but the
current blocker controls do not write them. The complete original transcript
is read from `demo_turns`; it is never reconstructed from the evictable
short-term memory table.

Database initialization is idempotent. Existing Demo databases gain the
configuration, scheduling, completion, `queued_at`, and `is_held` columns with
safe defaults, and missing current steps are inserted. On unfinished turns,
legacy rows skipped specifically as `Disabled by turn configuration` are
restored to `pending` and converted into holds from their old `run_*` value;
completed history and system-level skips are untouched. The launcher
deliberately uses a new v3 root rather than attempting to mutate temporary v2
core memory tables.

The page talks to `DemoPipelineService`, `DemoRepository`, and
`MemoryStateService`. It does not modify task rows with SQL, call core private
methods, or monkey patch the worker.

Each committed turn uses `demo-turn:<simulation_id>:<turn_id>` as a persisted
`Memory.add()` idempotency key. The core SQLite transaction stores that
operation together with its short-term messages and migration/profile jobs, so
reopening the sandbox or retrying a failed Demo step reuses the original
result.

On application restart, persisted execution targets are resumed after expired
leases are recovered. Expired queued work becomes pending; an expired running
step becomes a retryable failure. Active turns are queried from `demo.db`, not
Session State. Unfinished turns are clickable active cards; completed turns
move to the history-only selector and remain in the transcript, steps,
snapshots, and databases.

Deleting a sandbox first closes its coordinator to new submissions, waits for
running and queued futures, then closes `DemoMemory` (including worker
heartbeats and database/vector resources) before removing the directory.

## Tests

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
pytest -p pytest_asyncio.plugin -q tests/memory_monitor

PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
pytest -p pytest_asyncio.plugin -q \
  tests/memory/test_idempotency.py \
  tests/memory/test_background_worker.py \
  tests/memory/test_retrieve_context.py \
  tests/memory/test_layered_add_flow.py \
  tests/memory/test_midterm_memory.py \
  tests/memory/test_user_profile.py \
  tests/memory/test_storage.py
```
