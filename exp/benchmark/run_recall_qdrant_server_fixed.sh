#!/usr/bin/env bash
set -Eeuo pipefail

# 一键运行 agent_mem 召回率测试（Qdrant Server 模式）。
#
# 默认运行完整召回测试：
#   ./exp/benchmark/run_recall_qdrant_server.sh
#
# 先运行 smoke test：
#   ./exp/benchmark/run_recall_qdrant_server.sh \
#     exp/benchmark/recall_benchmark_smoke.json
#
# 常用环境变量：
#   PYTHON_BIN=python
#   QDRANT_URL=http://127.0.0.1:6333
#   QDRANT_PORT=6333
#   QDRANT_GRPC_PORT=6334
#   QDRANT_CONTAINER_NAME=agent-mem-benchmark-qdrant
#   QDRANT_IMAGE=qdrant/qdrant:latest
#
# 为降低真实 LLM 召回实验中的连接失败，默认进行以下覆盖：
#   RECALL_SESSION_CONCURRENCY=20
#   MIDTERM_WORKERS=4
#   LONGTERM_WORKERS=4
#   PROFILE_WORKERS=1
#
# 设置为空字符串可保留 JSON 中原有值：
#   RECALL_SESSION_CONCURRENCY= \
#   MIDTERM_WORKERS= \
#   LONGTERM_WORKERS= \
#   PROFILE_WORKERS= \
#   ./exp/benchmark/run_recall_qdrant_server.sh
#
# 其他开关：
#   STOP_QDRANT_AFTER=1       测试结束后停止本脚本启动的 Qdrant
#   STRICT_VALIDATION=1       发现严重异常时以非 0 状态退出（默认开启）
#   KEEP_EXISTING_DATA=1      不清理本次测试数据；通常不建议
#
# 说明：
# - 不修改 benchmark_common.py、benchmark_memory.py 或 mem0 原有代码。
# - 仅在当前 Python 进程内将 Qdrant Local 配置替换为 Qdrant Server。
# - SQLite、结果目录和 Qdrant Collection 仍按 Recall 配置独立管理。

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/../.." && pwd)"

PYTHON_BIN="${PYTHON_BIN:-python}"
CONFIG_ARG="${1:-${SCRIPT_DIR}/recall_benchmark.json}"

QDRANT_PORT="${QDRANT_PORT:-6333}"
QDRANT_GRPC_PORT="${QDRANT_GRPC_PORT:-6334}"
QDRANT_URL="${QDRANT_URL:-http://127.0.0.1:${QDRANT_PORT}}"
QDRANT_CONTAINER_NAME="${QDRANT_CONTAINER_NAME:-agent-mem-benchmark-qdrant}"
QDRANT_IMAGE="${QDRANT_IMAGE:-qdrant/qdrant:latest}"
QDRANT_STORAGE="${QDRANT_STORAGE:-${REPO_ROOT}/exp/runtime/qdrant_server}"

RECALL_SESSION_CONCURRENCY="${RECALL_SESSION_CONCURRENCY-20}"
MIDTERM_WORKERS="${MIDTERM_WORKERS-4}"
LONGTERM_WORKERS="${LONGTERM_WORKERS-4}"
PROFILE_WORKERS="${PROFILE_WORKERS-1}"

STOP_QDRANT_AFTER="${STOP_QDRANT_AFTER:-0}"
STRICT_VALIDATION="${STRICT_VALIDATION:-1}"
KEEP_EXISTING_DATA="${KEEP_EXISTING_DATA:-0}"

export PYTHONUNBUFFERED=1
export POSTHOG_DISABLED="${POSTHOG_DISABLED:-true}"
export MEM0_TELEMETRY="${MEM0_TELEMETRY:-false}"

STARTED_QDRANT_CONTAINER=0
TEMP_CONFIG=""
TEMP_LOG=""

log() {
  printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"
}

die() {
  printf '错误：%s\n' "$*" >&2
  exit 1
}

cleanup() {
  [[ -n "${TEMP_CONFIG}" && -f "${TEMP_CONFIG}" ]] && rm -f "${TEMP_CONFIG}" || true
  [[ -n "${TEMP_LOG}" && -f "${TEMP_LOG}" ]] && rm -f "${TEMP_LOG}" || true

  if [[ "${STOP_QDRANT_AFTER}" == "1" && "${STARTED_QDRANT_CONTAINER}" == "1" ]]; then
    log "停止本脚本启动的 Qdrant 容器：${QDRANT_CONTAINER_NAME}"
    docker rm -f "${QDRANT_CONTAINER_NAME}" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

if [[ ! -f "${REPO_ROOT}/pyproject.toml" || ! -d "${REPO_ROOT}/mem0" ]]; then
  die "无法识别仓库根目录：${REPO_ROOT}"
fi

command -v "${PYTHON_BIN}" >/dev/null 2>&1 \
  || die "找不到 Python：${PYTHON_BIN}"

if [[ "${CONFIG_ARG}" = /* ]]; then
  CONFIG_PATH="${CONFIG_ARG}"
else
  CONFIG_PATH="${REPO_ROOT}/${CONFIG_ARG}"
fi
CONFIG_PATH="$(cd -- "$(dirname -- "${CONFIG_PATH}")" && pwd)/$(basename -- "${CONFIG_PATH}")"

[[ -f "${CONFIG_PATH}" ]] || die "召回测试配置不存在：${CONFIG_PATH}"
[[ -f "${SCRIPT_DIR}/run_recall_benchmark.py" ]] \
  || die "缺少 ${SCRIPT_DIR}/run_recall_benchmark.py"
[[ -f "${SCRIPT_DIR}/benchmark_common.py" ]] \
  || die "缺少 ${SCRIPT_DIR}/benchmark_common.py"
[[ -f "${SCRIPT_DIR}/benchmark_memory.py" ]] \
  || die "缺少 ${SCRIPT_DIR}/benchmark_memory.py"

qdrant_ready() {
  "${PYTHON_BIN}" - "${QDRANT_URL}" >/dev/null 2>&1 <<'PY'
import json
import sys
import urllib.request

base = sys.argv[1].rstrip("/")
try:
    with urllib.request.urlopen(f"{base}/collections", timeout=2.0) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if response.status == 200 and isinstance(payload, dict):
        raise SystemExit(0)
except Exception:
    pass
raise SystemExit(1)
PY
}

start_qdrant() {
  if qdrant_ready; then
    log "检测到可用的 Qdrant Server：${QDRANT_URL}"
    return
  fi

  command -v docker >/dev/null 2>&1 \
    || die "Qdrant Server 未运行，并且系统中找不到 docker"

  docker info >/dev/null 2>&1 \
    || die "Docker daemon 不可用，或当前用户没有 Docker 权限"

  mkdir -p "${QDRANT_STORAGE}"

  if docker ps --format '{{.Names}}' | grep -Fxq "${QDRANT_CONTAINER_NAME}"; then
    log "Qdrant 容器已运行，等待服务就绪：${QDRANT_CONTAINER_NAME}"
  else
    if docker ps -a --format '{{.Names}}' | grep -Fxq "${QDRANT_CONTAINER_NAME}"; then
      log "删除旧的已停止 Qdrant 容器：${QDRANT_CONTAINER_NAME}"
      docker rm -f "${QDRANT_CONTAINER_NAME}" >/dev/null
    fi

    log "启动 Qdrant Server：${QDRANT_CONTAINER_NAME}"
    docker run -d \
      --name "${QDRANT_CONTAINER_NAME}" \
      --restart unless-stopped \
      -p "${QDRANT_PORT}:6333" \
      -p "${QDRANT_GRPC_PORT}:6334" \
      -v "${QDRANT_STORAGE}:/qdrant/storage" \
      "${QDRANT_IMAGE}" >/dev/null
    STARTED_QDRANT_CONTAINER=1
  fi

  for _ in $(seq 1 60); do
    if qdrant_ready; then
      log "Qdrant Server 已就绪：${QDRANT_URL}"
      return
    fi
    sleep 1
  done

  docker logs --tail 100 "${QDRANT_CONTAINER_NAME}" >&2 || true
  die "Qdrant Server 在 60 秒内未就绪"
}

create_effective_config() {
  TEMP_CONFIG="$(mktemp --suffix=.recall-benchmark.json)"

  "${PYTHON_BIN}" - \
    "${CONFIG_PATH}" \
    "${TEMP_CONFIG}" \
    "${RECALL_SESSION_CONCURRENCY}" <<'PY'
import json
import sys
from pathlib import Path

source = Path(sys.argv[1])
target = Path(sys.argv[2])
session_concurrency = sys.argv[3].strip()

config = json.loads(source.read_text(encoding="utf-8"))

if session_concurrency:
    value = int(session_concurrency)
    if value <= 0:
        raise ValueError("RECALL_SESSION_CONCURRENCY 必须大于 0")
    config.setdefault("execution", {})["session_concurrency"] = value

target.write_text(
    json.dumps(config, ensure_ascii=False, indent=2),
    encoding="utf-8",
)
PY
}

print_test_config() {
  "${PYTHON_BIN}" - \
    "${TEMP_CONFIG}" \
    "${CONFIG_PATH}" \
    "${REPO_ROOT}" \
    "${QDRANT_URL}" \
    "${MIDTERM_WORKERS}" \
    "${LONGTERM_WORKERS}" \
    "${PROFILE_WORKERS}" <<'PY'
import json
import os
import sys
from pathlib import Path

effective_path = Path(sys.argv[1])
source_path = Path(sys.argv[2])
repo_root = Path(sys.argv[3])
qdrant_url = sys.argv[4]
midterm_workers = sys.argv[5].strip()
longterm_workers = sys.argv[6].strip()
profile_workers = sys.argv[7].strip()

config = json.loads(effective_path.read_text(encoding="utf-8"))
benchmark = config.get("benchmark") or {}
dataset = config.get("dataset") or {}
storage = config.get("storage") or {}
execution = config.get("execution") or {}
memory = config.get("memory") or {}
retrieval = config.get("retrieval") or {}
evaluation = config.get("evaluation") or {}

print("========== 召回测试配置 ==========")
print(f"原始配置       : {source_path}")
print(f"run_name       : {benchmark.get('run_name')}")
print(f"数据集         : {dataset.get('path')}")
print(f"最大 Session   : {dataset.get('max_sessions')}")
print(f"每 Session 轮数: {dataset.get('max_turns_per_session')}")
print(f"Session 并发   : {execution.get('session_concurrency')}")
print(f"等待策略       : {execution.get('wait_strategy', 'before_evaluation')}")
print(f"内部 LLM       : {memory.get('llm_mode', 'real')}")
print(f"infer          : {memory.get('infer', True)}")
print(f"profile        : {memory.get('profile_enabled', False)}")
print(f"Top-K          : {retrieval.get('top_k', 20)}")
print(f"阈值           : {retrieval.get('threshold', 0.1)}")
print(f"只评长距离     : {evaluation.get('evaluate_only_long_range', True)}")
print(f"Collection     : {storage.get('collection_name')}")
print(f"Qdrant Server  : {qdrant_url}")
print(f"中期 Worker    : {midterm_workers or '保留 memory_config.json 原值'}")
print(f"长期 Worker    : {longterm_workers or '保留 memory_config.json 原值'}")
print(f"画像 Worker    : {profile_workers or '保留 memory_config.json 原值'}")
print(f"结果目录       : {benchmark.get('output_dir')}")
print("==================================")

if str(memory.get("llm_mode", "real")).lower() == "real":
    if not os.environ.get("DEEPSEEK_API_KEY"):
        print(
            "错误：当前配置使用 real LLM，但未设置 DEEPSEEK_API_KEY",
            file=sys.stderr,
        )
        raise SystemExit(2)
PY
}

reset_test_state() {
  if [[ "${KEEP_EXISTING_DATA}" == "1" ]]; then
    log "KEEP_EXISTING_DATA=1：保留当前 Recall 数据和 Collection"
    return
  fi

  "${PYTHON_BIN}" - \
    "${TEMP_CONFIG}" \
    "${REPO_ROOT}" \
    "${QDRANT_URL}" <<'PY'
import json
import os
import shutil
import sys
import urllib.parse
import urllib.request
from pathlib import Path

config_path = Path(sys.argv[1])
repo_root = Path(sys.argv[2])
qdrant_url = sys.argv[3].rstrip("/")
config = json.loads(config_path.read_text(encoding="utf-8"))

benchmark = config.get("benchmark") or {}
storage = config.get("storage") or {}

def resolve(value):
    path = Path(os.path.expandvars(os.path.expanduser(str(value))))
    return path if path.is_absolute() else (repo_root / path).resolve()

reset_storage = bool(benchmark.get("reset_storage", True))
if not reset_storage:
    print("reset_storage=false：保留原有 SQLite、结果目录和 Collection")
    raise SystemExit(0)

output_dir = resolve(
    benchmark.get("output_dir", "exp/results/recall_benchmark")
)
runtime_dir = resolve(
    storage.get("runtime_dir", "exp/runtime/recall_benchmark")
)
collection_prefix = str(
    storage.get("collection_name", "recall_benchmark")
).strip()

shutil.rmtree(output_dir, ignore_errors=True)
shutil.rmtree(runtime_dir, ignore_errors=True)
runtime_dir.mkdir(parents=True, exist_ok=True)

try:
    with urllib.request.urlopen(
        f"{qdrant_url}/collections",
        timeout=10.0,
    ) as response:
        data = json.loads(response.read().decode("utf-8"))
except Exception as exc:
    raise RuntimeError(f"读取 Qdrant Collection 失败：{exc}") from exc

collections = (
    data.get("result", {}).get("collections", [])
    if isinstance(data, dict)
    else []
)
names = [
    str(item.get("name"))
    for item in collections
    if isinstance(item, dict) and item.get("name")
]

targets = sorted(
    name
    for name in names
    if name == collection_prefix or name.startswith(f"{collection_prefix}_")
)

for name in targets:
    encoded_name = urllib.parse.quote(name, safe="")
    request = urllib.request.Request(
        f"{qdrant_url}/collections/{encoded_name}?timeout=60",
        method="DELETE",
    )
    try:
        with urllib.request.urlopen(request, timeout=70.0) as response:
            response.read()
        print(f"已删除 Qdrant Collection：{name}")
    except Exception as exc:
        raise RuntimeError(f"删除 Collection {name} 失败：{exc}") from exc

print(f"已清理 SQLite 运行目录：{runtime_dir}")
print(f"已清理测试结果目录：{output_dir}")
PY
}

run_recall_with_remote_qdrant() {
  "${PYTHON_BIN}" - \
    "${SCRIPT_DIR}" \
    "${TEMP_CONFIG}" \
    "${QDRANT_URL}" \
    "${MIDTERM_WORKERS}" \
    "${LONGTERM_WORKERS}" \
    "${PROFILE_WORKERS}" <<'PY'
import sys
from pathlib import Path

script_dir = Path(sys.argv[1]).resolve()
config_path = Path(sys.argv[2]).resolve()
qdrant_url = sys.argv[3].rstrip("/")
midterm_workers = sys.argv[4].strip()
longterm_workers = sys.argv[5].strip()
profile_workers = sys.argv[6].strip()

sys.path.insert(0, str(script_dir))

import benchmark_common  # noqa: E402

original_prepare_runtime_config = benchmark_common.prepare_runtime_config

def prepare_runtime_config_for_server(
    base_config,
    *,
    runtime_dir,
    collection_name,
    reset_storage,
    agentic_retrieval_enabled,
    profile_enabled,
    profile_update_on_add,
):
    config = original_prepare_runtime_config(
        base_config,
        runtime_dir=runtime_dir,
        collection_name=collection_name,
        reset_storage=reset_storage,
        agentic_retrieval_enabled=agentic_retrieval_enabled,
        profile_enabled=profile_enabled,
        profile_update_on_add=profile_update_on_add,
    )

    vector_store = config.setdefault("vector_store", {})
    vector_config = vector_store.setdefault("config", {})

    # 当前仓库的 QdrantConfig 不接受 prefer_grpc，并且 url 模式的
    # 校验规则要求 api_key。这里解析 URL 后使用明确支持的 host/port/https。
    from urllib.parse import urlparse

    parsed_qdrant = urlparse(qdrant_url)
    if not parsed_qdrant.hostname:
        raise ValueError(f"无效的 Qdrant URL：{qdrant_url}")

    vector_config.pop("url", None)
    vector_config.pop("client", None)
    vector_config.pop("api_key", None)
    vector_config["path"] = None
    vector_config["host"] = parsed_qdrant.hostname
    vector_config["port"] = parsed_qdrant.port or (443 if parsed_qdrant.scheme == "https" else 6333)
    vector_config["https"] = parsed_qdrant.scheme == "https"
    vector_config["collection_name"] = collection_name

    background = config.setdefault("background", {})
    if midterm_workers:
        background["midterm_worker_count"] = int(midterm_workers)
    if longterm_workers:
        background["longterm_worker_count"] = int(longterm_workers)
    if profile_workers:
        background["profile_worker_count"] = int(profile_workers)

    return config

benchmark_common.prepare_runtime_config = prepare_runtime_config_for_server

import run_recall_benchmark as runner  # noqa: E402

# run_recall_benchmark 在导入时把函数绑定到模块变量，需要同步替换。
runner.prepare_runtime_config = prepare_runtime_config_for_server

sys.argv = [
    str(script_dir / "run_recall_benchmark.py"),
    "--config",
    str(config_path),
]
runner.main()
PY
}

resolve_output_and_runtime() {
  "${PYTHON_BIN}" - "${TEMP_CONFIG}" "${REPO_ROOT}" <<'PY'
import json
import os
import sys
from pathlib import Path

config = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
repo_root = Path(sys.argv[2])

def resolve(value):
    path = Path(os.path.expandvars(os.path.expanduser(str(value))))
    return path if path.is_absolute() else (repo_root / path).resolve()

benchmark = config.get("benchmark") or {}
storage = config.get("storage") or {}

print(resolve(benchmark.get("output_dir", "exp/results/recall_benchmark")))
print(resolve(storage.get("runtime_dir", "exp/runtime/recall_benchmark")))
PY
}

validate_and_print_results() {
  local output_dir="$1"
  local runtime_dir="$2"
  local console_log="$3"

  "${PYTHON_BIN}" - \
    "${output_dir}" \
    "${runtime_dir}" \
    "${console_log}" \
    "${STRICT_VALIDATION}" <<'PY'
import csv
import json
import re
import sqlite3
import sys
from collections import Counter
from pathlib import Path

output_dir = Path(sys.argv[1])
runtime_dir = Path(sys.argv[2])
console_log = Path(sys.argv[3])
strict = sys.argv[4] == "1"

summary_path = output_dir / "recall_summary.json"
db_path = runtime_dir / "history.db"

if not summary_path.exists():
    print(f"错误：未找到召回汇总文件：{summary_path}", file=sys.stderr)
    raise SystemExit(2)

summary = json.loads(summary_path.read_text(encoding="utf-8"))
log_text = console_log.read_text(encoding="utf-8", errors="replace")

def format_ratio(value):
    if value is None:
        return "-"
    return f"{float(value):.4f}"

critical_patterns = {
    "qdrant_index_error": r"index \d+ is out of bounds for axis 0 with size \d+",
    "qdrant_shape_error": r"operands could not be broadcast together with shapes",
    "degradation_failed": r"Background stage degradation failed",
    "degraded_storage": r"Background stage used degraded storage",
    "cleanup_failed": r"Failed to clean up discarded stage outputs",
    "llm_empty_response": r"response parsing failed: response is empty",
}
critical_counts = {
    name: len(re.findall(pattern, log_text, flags=re.IGNORECASE))
    for name, pattern in critical_patterns.items()
}

transient_counts = {
    "migration_stage_failed": len(
        re.findall(r"Background migration stage failed", log_text)
    ),
    "llm_extraction_failed": len(
        re.findall(r"LLM extraction failed", log_text)
    ),
    "deepseek_retry": len(
        re.findall(r"Retrying request to /chat/completions", log_text)
    ),
}

job_statuses = {}
stage_statuses = {}
invalid_final_jobs = 0

if db_path.exists():
    connection = sqlite3.connect(str(db_path))
    connection.row_factory = sqlite3.Row
    try:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if "memory_migration_jobs" in tables:
            columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(memory_migration_jobs)"
                )
            }
            for column in ("status", "midterm_status", "longterm_status"):
                if column not in columns:
                    continue
                counts = {
                    str(row[0]): int(row[1])
                    for row in connection.execute(
                        f"""
                        SELECT {column}, COUNT(*)
                        FROM memory_migration_jobs
                        GROUP BY {column}
                        """
                    )
                }
                if column == "status":
                    job_statuses = counts
                else:
                    stage_statuses[column] = counts

            # Recall 准确率实验只接受完整 succeeded。
            if "status" in columns:
                invalid_final_jobs = int(
                    connection.execute(
                        """
                        SELECT COUNT(*)
                        FROM memory_migration_jobs
                        WHERE COALESCE(status, '') <> 'succeeded'
                        """
                    ).fetchone()[0]
                )
    finally:
        connection.close()

cross_session_leaks = 0
future_turn_leaks = 0
turn_csv = output_dir / "recall_turn_results.csv"
if turn_csv.exists():
    with turn_csv.open(encoding="utf-8", newline="") as file:
        for row in csv.DictReader(file):
            try:
                cross_session_leaks += int(float(row.get("cross_session_leak_count") or 0))
                future_turn_leaks += int(float(row.get("future_turn_leak_count") or 0))
            except ValueError:
                pass

failed_turns = int(summary.get("failed_turns") or 0)
critical_total = sum(critical_counts.values())

run_valid = (
    failed_turns == 0
    and critical_total == 0
    and invalid_final_jobs == 0
    and cross_session_leaks == 0
    and future_turn_leaks == 0
)

print("")
print("========== 召回测试结果 ==========")
print(f"结果目录             : {output_dir}")
print(f"总轮数               : {summary.get('total_turns')}")
print(f"失败轮数             : {failed_turns}")
print(f"评测轮数             : {summary.get('evaluated_turns')}")
print(f"Short Recall         : {format_ratio((summary.get('short') or {}).get('recall'))}")
print(f"Mid Recall           : {format_ratio((summary.get('mid') or {}).get('recall'))}")
print(f"Long Recall          : {format_ratio((summary.get('long') or {}).get('recall'))}")
print(f"Mid+Long Recall      : {format_ratio((summary.get('mid_long') or {}).get('recall'))}")
print(f"All Recall           : {format_ratio((summary.get('all') or {}).get('recall'))}")
print(f"All Full Recall      : {format_ratio((summary.get('all') or {}).get('full_dependency_recall'))}")
print(f"跨 Session 泄漏      : {cross_session_leaks}")
print(f"未来轮次泄漏         : {future_turn_leaks}")
print(f"迁移任务最终状态     : {job_statuses or '未读取到'}")
print(f"中长期阶段状态       : {stage_statuses or '未读取到'}")
print(f"最终非 succeeded 任务: {invalid_final_jobs}")
print(f"严重日志计数         : {critical_counts}")
print(f"重试/阶段失败计数    : {transient_counts}")
print(f"本次结果是否有效     : {run_valid}")
print("==================================")

validation = {
    "run_valid": run_valid,
    "failed_turns": failed_turns,
    "cross_session_leaks": cross_session_leaks,
    "future_turn_leaks": future_turn_leaks,
    "migration_job_statuses": job_statuses,
    "stage_statuses": stage_statuses,
    "invalid_final_jobs": invalid_final_jobs,
    "critical_log_counts": critical_counts,
    "transient_log_counts": transient_counts,
}
(output_dir / "recall_validation.json").write_text(
    json.dumps(validation, ensure_ascii=False, indent=2),
    encoding="utf-8",
)

if strict and not run_valid:
    print(
        "错误：本次召回测试存在失败、降级、Qdrant 异常、泄漏或未成功任务，"
        "结果已标记为无效。",
        file=sys.stderr,
    )
    raise SystemExit(3)
PY
}

log "仓库根目录：${REPO_ROOT}"
log "Python：$("${PYTHON_BIN}" --version 2>&1)"
log "Qdrant URL：${QDRANT_URL}"

create_effective_config
print_test_config
start_qdrant
reset_test_state

mapfile -t RESOLVED_PATHS < <(resolve_output_and_runtime)
OUTPUT_DIR="${RESOLVED_PATHS[0]}"
RUNTIME_DIR="${RESOLVED_PATHS[1]}"
mkdir -p "${OUTPUT_DIR}"
TEMP_LOG="$(mktemp --suffix=.recall-console.log)"

log "开始执行召回率测试"

set +e
run_recall_with_remote_qdrant 2>&1 | tee "${TEMP_LOG}"
RUN_STATUS="${PIPESTATUS[0]}"
set -e

mkdir -p "${OUTPUT_DIR}"
cp "${TEMP_LOG}" "${OUTPUT_DIR}/recall_console.log"

if [[ "${RUN_STATUS}" -ne 0 ]]; then
  die "召回率 Python 脚本执行失败，退出码=${RUN_STATUS}。日志：${OUTPUT_DIR}/recall_console.log"
fi

log "召回率测试执行完成"
validate_and_print_results "${OUTPUT_DIR}" "${RUNTIME_DIR}" "${TEMP_LOG}"

log "结果文件：${OUTPUT_DIR}/recall_summary.json"
log "有效性检查：${OUTPUT_DIR}/recall_validation.json"
log "完整控制台日志：${OUTPUT_DIR}/recall_console.log"
