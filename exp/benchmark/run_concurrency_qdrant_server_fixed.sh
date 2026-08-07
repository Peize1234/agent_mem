#!/usr/bin/env bash
set -Eeuo pipefail

# 一键运行 agent_mem 并发测试：
# 1. 检查或启动 Qdrant Server
# 2. 清理本次测试对应的远程 Collection、SQLite 和结果目录
# 3. 临时将 Benchmark 的 Qdrant 配置切换为 Server 模式
# 4. 执行现有 run_concurrency_benchmark.py
#
# 默认运行：
#   ./exp/benchmark/run_concurrency_qdrant_server.sh
#
# 指定配置：
#   ./exp/benchmark/run_concurrency_qdrant_server.sh \
#     exp/benchmark/concurrency_benchmark_real.json
#
# 可选环境变量：
#   PYTHON_BIN=python
#   QDRANT_URL=http://127.0.0.1:6333
#   QDRANT_PORT=6333
#   QDRANT_GRPC_PORT=6334
#   QDRANT_CONTAINER_NAME=agent-mem-benchmark-qdrant
#   QDRANT_IMAGE=qdrant/qdrant:latest
#   STOP_QDRANT_AFTER=0
#
# STOP_QDRANT_AFTER=1 时，仅当容器由本脚本启动，测试结束后才会停止容器。

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/../.." && pwd)"

PYTHON_BIN="${PYTHON_BIN:-python}"
CONFIG_ARG="${1:-${SCRIPT_DIR}/concurrency_benchmark.json}"

QDRANT_PORT="${QDRANT_PORT:-6333}"
QDRANT_GRPC_PORT="${QDRANT_GRPC_PORT:-6334}"
QDRANT_URL="${QDRANT_URL:-http://127.0.0.1:${QDRANT_PORT}}"
QDRANT_CONTAINER_NAME="${QDRANT_CONTAINER_NAME:-agent-mem-benchmark-qdrant}"
QDRANT_IMAGE="${QDRANT_IMAGE:-qdrant/qdrant:latest}"
QDRANT_STORAGE="${QDRANT_STORAGE:-${REPO_ROOT}/exp/runtime/qdrant_server}"
STOP_QDRANT_AFTER="${STOP_QDRANT_AFTER:-0}"

export PYTHONUNBUFFERED=1
export POSTHOG_DISABLED="${POSTHOG_DISABLED:-true}"
export MEM0_TELEMETRY="${MEM0_TELEMETRY:-false}"

STARTED_QDRANT_CONTAINER=0

log() {
  printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"
}

die() {
  printf '错误：%s\n' "$*" >&2
  exit 1
}

cleanup() {
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

[[ -f "${CONFIG_PATH}" ]] || die "并发测试配置不存在：${CONFIG_PATH}"
[[ -f "${SCRIPT_DIR}/run_concurrency_benchmark.py" ]] \
  || die "缺少 ${SCRIPT_DIR}/run_concurrency_benchmark.py"
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

print_test_config() {
  "${PYTHON_BIN}" - "${CONFIG_PATH}" "${REPO_ROOT}" <<'PY'
import json
import os
import sys
from pathlib import Path

config_path = Path(sys.argv[1])
repo_root = Path(sys.argv[2])
config = json.loads(config_path.read_text(encoding="utf-8"))

benchmark = config.get("benchmark") or {}
storage = config.get("storage") or {}
memory = config.get("memory") or {}
load = config.get("load") or {}
prefill = config.get("prefill") or {}

print("========== 并发测试配置 ==========")
print(f"配置文件       : {config_path}")
print(f"run_name       : {benchmark.get('run_name')}")
print(f"workload       : {load.get('workload', 'mixed')}")
print(f"LLM 模式       : {memory.get('llm_mode', 'mock')}")
print(f"目标 RPS       : {load.get('target_rps', 50)}")
print(f"最大 in-flight : {load.get('max_in_flight', 100)}")
print(f"正式请求上限   : {load.get('max_requests')}")
print(f"流量模型       : {load.get('traffic_model', 'uniform')}")
print(f"预填充轮数     : {prefill.get('turns_per_session', 0)}")
print(f"Collection     : {storage.get('collection_name')}")
print(f"结果目录       : {benchmark.get('output_dir')}")
print("==================================")

if str(memory.get("llm_mode", "mock")).lower() == "real":
    if not os.environ.get("DEEPSEEK_API_KEY"):
        print(
            "错误：当前配置使用 real LLM，但未设置 DEEPSEEK_API_KEY",
            file=sys.stderr,
        )
        raise SystemExit(2)
PY
}

reset_test_state() {
  "${PYTHON_BIN}" - "${CONFIG_PATH}" "${REPO_ROOT}" "${QDRANT_URL}" <<'PY'
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
collection_prefix = str(
    storage.get("collection_name", "concurrency_benchmark")
).strip()

if not reset_storage:
    print("reset_storage=false：保留原有数据库、结果和 Collection")
    raise SystemExit(0)

output_dir = resolve(
    benchmark.get("output_dir", "exp/results/concurrency_benchmark")
)
runtime_dir = resolve(
    storage.get("runtime_dir", "exp/runtime/concurrency_benchmark")
)

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

run_benchmark_with_remote_qdrant() {
  # 不修改 benchmark_common.py。
  # 在当前 Python 进程中先替换 prepare_runtime_config，
  # 再导入并运行现有并发测试脚本。
  "${PYTHON_BIN}" - "${SCRIPT_DIR}" "${CONFIG_PATH}" "${QDRANT_URL}" <<'PY'
import sys
from pathlib import Path

script_dir = Path(sys.argv[1]).resolve()
config_path = Path(sys.argv[2]).resolve()
qdrant_url = sys.argv[3].rstrip("/")

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

    return config

benchmark_common.prepare_runtime_config = prepare_runtime_config_for_server

import run_concurrency_benchmark as runner  # noqa: E402

# 防止模块导入方式变化时仍引用旧函数。
runner.prepare_runtime_config = prepare_runtime_config_for_server

sys.argv = [
    str(script_dir / "run_concurrency_benchmark.py"),
    "--config",
    str(config_path),
]
runner.main()
PY
}

print_result_summary() {
  "${PYTHON_BIN}" - "${CONFIG_PATH}" "${REPO_ROOT}" <<'PY'
import json
import os
import sys
from pathlib import Path

config_path = Path(sys.argv[1])
repo_root = Path(sys.argv[2])
config = json.loads(config_path.read_text(encoding="utf-8"))
benchmark = config.get("benchmark") or {}

output_dir = Path(
    os.path.expandvars(
        os.path.expanduser(
            str(
                benchmark.get(
                    "output_dir",
                    "exp/results/concurrency_benchmark",
                )
            )
        )
    )
)
if not output_dir.is_absolute():
    output_dir = (repo_root / output_dir).resolve()

summary_path = output_dir / "concurrency_summary.json"
if not summary_path.exists():
    print(f"未找到汇总文件：{summary_path}", file=sys.stderr)
    raise SystemExit(1)

summary = json.loads(summary_path.read_text(encoding="utf-8"))

def metric(name, percentile):
    value = (summary.get(name) or {}).get(percentile)
    return "-" if value is None else f"{value:.2f} ms"

print("")
print("========== 并发测试结果 ==========")
print(f"结果目录             : {output_dir}")
print(f"目标 RPS             : {summary.get('target_rps')}")
print(f"实际调度 RPS         : {summary.get('actual_schedule_rps')}")
print(f"实测请求数           : {summary.get('measured_requests')}")
print(f"成功请求数           : {summary.get('successful_measured_requests')}")
print(f"失败请求数           : {summary.get('failed_measured_requests')}")
print(f"错误率               : {summary.get('error_rate')}")
print(f"最大 observed in-flight: {summary.get('max_in_flight_observed')}")
print(f"Build p50 / p95 / p99: {metric('build_total_ms', 'p50')} / "
      f"{metric('build_total_ms', 'p95')} / {metric('build_total_ms', 'p99')}")
print(f"Add p50 / p95 / p99  : {metric('add_submit_ms', 'p50')} / "
      f"{metric('add_submit_ms', 'p95')} / {metric('add_submit_ms', 'p99')}")
print(f"后台是否排空         : {summary.get('background_flushed')}")
print(f"后台排空耗时         : {summary.get('background_drain_seconds')} s")
print("==================================")
PY
}

log "仓库根目录：${REPO_ROOT}"
log "Python：$("${PYTHON_BIN}" --version 2>&1)"
log "Qdrant URL：${QDRANT_URL}"

print_test_config
start_qdrant
reset_test_state

log "开始执行并发测试"
run_benchmark_with_remote_qdrant

log "并发测试执行完成"
print_result_summary
