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

The launcher uses the included DeepSeek + local HuggingFace configuration from
`demo_config.json` and prompts for `DEEPSEEK_API_KEY` when it is not already in
the environment. It matches the financial trace test defaults:
`deepseek-v4-flash` and `BAAI/bge-small-zh-v1.5` (512 dimensions).
It runs in the `MemoryOS` Conda environment, activating it through `conda run`
when necessary. By default the launcher uses the versioned sandbox root
`.memory_monitor_runs/demo-lab-v2`, so databases created by the earlier
single-migration pipeline are left untouched and are not offered in the new
lab.

```bash
conda activate MemoryOS
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
and falls back to the included configuration.

The Conda environment name can be overridden when needed:

```bash
MEMORY_MONITOR_CONDA_ENV=another-env ./memory_monitor/start_demo_lab.sh
```

The page supports:

- next step;
- run through model generation;
- run through core commit;
- submit all enabled background branches without waiting;
- retry one selected failed step;
- reset an uncommitted turn.

Each turn persists three independent switches in `demo.db`: `run_midterm`,
`run_longterm`, and `run_profile`. They default to enabled for new and legacy
turns. A disabled branch is not submitted, remains `pending` in both the Demo
step and core job, is labeled “本轮未启用,” and is excluded from effective
progress. It is never represented as `skipped`.

## Pipeline and concurrency

The displayed pipeline is a DAG rather than an ordered table:

```text
capture_input
    ↓
retrieve_context
    ↓
build_prompt
    ↓
generate_response
    ↓
commit_turn
    ├── run_midterm ─┐
    ├── run_longterm ┼── refresh_state
    └── run_profile ─┘
```

`refresh_state` waits only for the background branches enabled on that turn.
Dependencies are explicit in `STEP_DEPENDENCIES`; enum order is used only for
stable display and storage positions.

Every open sandbox owns one `DemoBackgroundCoordinator`. It has three separate
`ThreadPoolExecutor(max_workers=1)` instances, one each for mid-term,
long-term, and profile work. Tasks of the same type retain submission order,
while the three types can run concurrently. A background click claims the
persisted Demo step, submits all enabled branches, and returns immediately.
The core repository still owns the real task lease, heartbeat, retry,
same-scope ordering, migration finalization, and cleanup semantics.

The Streamlit page refreshes the live DAG, snapshots, jobs, and trace in a
one-second `st.fragment` only while a background Demo step is running. The
conversation and controls are outside that fragment, so viewing a historical
turn or typing the next turn does not change selection or configuration.

## Storage isolation

Every simulation owns:

```text
.memory_monitor_runs/demo-lab-v2/<simulation_id>/
├── history.db
├── qdrant/
└── demo.db
```

`history.db` and `qdrant/` contain the normal core memory state. `demo.db`
contains only `demo_sessions`, `demo_turns`, `demo_step_runs`, and
`demo_snapshots`. Per-turn switches and the background-submission timestamp
are columns on `demo_turns`. The complete original transcript is read from
`demo_turns`; it is never reconstructed from the evictable short-term memory
table.

Database initialization is idempotent. Existing databases gain the three
configuration columns with enabled defaults. Legacy `run_migration` rows are
copied once into `run_midterm` and `run_longterm`; the old row is retained for
forensics but omitted from the current DAG.

The page talks to `DemoPipelineService`, `DemoRepository`, and
`MemoryStateService`. It does not modify task rows with SQL, call core private
methods, or monkey patch the worker.

Each committed turn uses `demo-turn:<simulation_id>:<turn_id>` as a persisted
`Memory.add()` idempotency key. The core SQLite transaction stores that
operation together with its short-term messages and migration/profile jobs, so
reopening the sandbox or retrying a failed Demo step reuses the original
result.

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
