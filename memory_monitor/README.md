# Mem0 Memory Monitor

The monitor is an optional Streamlit debug UI for layered memory, persistent
background jobs, trace events, profile diffs, and isolated manual-worker
simulations.

Install and start it from the repository root:

```bash
hatch run monitor:start
```

## Read-only monitoring

Read-only mode is the default. It opens SQLite with `mode=ro` and
`PRAGMA query_only=ON`; retry, execution, deletion, and clearing controls are
disabled.

```bash
export MEMORY_MONITOR_DB_PATH=/path/to/history.db
export MEMORY_MONITOR_MODE=read_only
streamlit run memory_monitor/app.py
```

To expose vector collections, explicitly point the monitor at a Mem0 JSON
configuration. The monitor gives the attached `Memory` instance an in-memory
history database in read-only mode, so opening vector collections does not
migrate the monitored SQLite file.

```bash
export MEMORY_MONITOR_MEMORY_CONFIG=/path/to/mem0-config.json
```

Write controls require both an attached config and explicit opt-in:

```bash
export MEMORY_MONITOR_ALLOW_WRITES=true
```

## Sandbox simulations

```bash
export MEMORY_MONITOR_MODE=sandbox
export MEMORY_MONITOR_SIMULATION_ROOT=.memory_monitor_runs
export MEMORY_MONITOR_MEMORY_CONFIG=/path/to/mem0-config.json
streamlit run memory_monitor/app.py
```

Each simulation uses
`.memory_monitor_runs/<simulation_id>/history.db`, a local Qdrant directory,
and collection names prefixed with `memory_monitor_<simulation_id>`. The
optional Mem0 config supplies the LLM and embedder used by the sandbox; tests
should supply project fakes rather than real external models.

## Manual worker mode

Configure Mem0 as follows:

```python
config.background.enabled = True
config.background.execution_mode = "manual"
```

Calls to `Memory.add()` persist jobs without starting worker threads. Drive
the existing queue logic with:

```python
memory.process_next_migration_job()
memory.process_next_profile_job()
memory.process_migration_job(job_id)
memory.process_profile_job(job_id)
```

Observability is opt-in:

```python
config.observability.enabled = True
config.observability.capture_payloads = True
config.observability.max_payload_length = 20_000
```

Event-write failures are logged and isolated from memory processing.
