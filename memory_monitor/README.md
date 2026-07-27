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

Provide a normal Mem0 JSON configuration containing the LLM and embedder
settings used by the sandbox:

```bash
export MEMORY_MONITOR_MEMORY_CONFIG=/path/to/mem0-config.json
export MEMORY_MONITOR_SIMULATION_ROOT=.memory_monitor_runs
hatch run monitor:start
```

Alternatively:

```bash
pip install -e ".[monitor]"
streamlit run memory_monitor/app.py
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

## Tests

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
pytest -p pytest_asyncio.plugin -q tests/memory_monitor

PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
pytest -p pytest_asyncio.plugin -q \
  tests/memory/test_background_worker.py \
  tests/memory/test_retrieve_context.py \
  tests/memory/test_layered_add_flow.py \
  tests/memory/test_midterm_memory.py \
  tests/memory/test_user_profile.py \
  tests/memory/test_storage.py
```
