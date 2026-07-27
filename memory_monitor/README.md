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

`DemoMemory` does not replace migration, long-term extraction, profile
extraction, vector search, or short-term eviction algorithms. Its worker calls
the production worker's complete migration/profile job paths.

## Start the lab

The launcher uses the included DeepSeek + local HuggingFace configuration from
`demo_config.json` and prompts for `DEEPSEEK_API_KEY` when it is not already in
the environment. It matches the financial trace test defaults:
`deepseek-v4-flash` and `BAAI/bge-small-zh-v1.5` (512 dimensions).
It runs in the `MemoryOS` Conda environment, activating it through `conda run`
when necessary.

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
- run all remaining steps;
- retry a failed step;
- skip optional migration/profile steps;
- reset an uncommitted turn.

## Storage isolation

Every simulation owns:

```text
.memory_monitor_runs/<simulation_id>/
├── history.db
├── qdrant/
└── demo.db
```

`history.db` and `qdrant/` contain the normal core memory state. `demo.db`
contains only `demo_sessions`, `demo_turns`, `demo_step_runs`, and
`demo_snapshots`. The complete original transcript is read from `demo_turns`;
it is never reconstructed from the evictable short-term memory table.

The page talks to `DemoPipelineService`, `DemoRepository`, and
`MemoryStateService`. It does not modify task rows with SQL, call core private
methods, or monkey patch the worker.

Each committed turn uses `demo-turn:<simulation_id>:<turn_id>` as a persisted
`Memory.add()` idempotency key. The core SQLite transaction stores that
operation together with its short-term messages and migration/profile jobs, so
reopening the sandbox or retrying a failed Demo step reuses the original
result.

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
