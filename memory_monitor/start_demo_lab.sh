#!/usr/bin/env bash

set -Eeuo pipefail

export MEM0_TELEMETRY="${MEM0_TELEMETRY:-false}"
export PYTHONNOUSERSITE="${PYTHONNOUSERSITE:-1}"

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repository_root="$(cd -- "${script_dir}/.." && pwd)"
cd "${repository_root}"

default_simulation_root="${repository_root}/.memory_monitor_runs/demo-lab-v3"
simulation_root="${MEMORY_MONITOR_SIMULATION_ROOT:-${default_simulation_root}}"
server_address="${MEMORY_MONITOR_ADDRESS:-0.0.0.0}"
server_port="${MEMORY_MONITOR_PORT:-8501}"
conda_environment="${MEMORY_MONITOR_CONDA_ENV:-MemoryOS}"

browser_address="${server_address}"
if [[ "${browser_address}" == "0.0.0.0" ]]; then
    browser_address="localhost"
fi

if [[ "${CONDA_DEFAULT_ENV:-}" == "${conda_environment}" ]]; then
    python_command=(python)
elif command -v conda >/dev/null 2>&1; then
    python_command=(conda run --no-capture-output -n "${conda_environment}" python)
else
    echo "错误：当前未激活 ${conda_environment}，并且找不到 Conda。" >&2
    echo "请先运行：conda activate ${conda_environment}" >&2
    exit 1
fi

echo "正在检查 Conda 环境 ${conda_environment}..."
set +e
dependency_report="$("${python_command[@]}" -c '
import importlib
import site
from pathlib import Path

modules = (
    "openai",
    "qdrant_client",
    "sentence_transformers",
    "streamlit",
    "pyarrow",
    "pandas",
    "requests",
    "urllib3",
    "posthog",
)
user_site = Path(site.getusersitepackages()).expanduser().resolve()
invalid = []
missing = []
for name in modules:
    try:
        module = importlib.import_module(name)
    except Exception as exc:
        missing.append(f"{name}: {type(exc).__name__}: {exc}")
        continue
    module_path = Path(module.__file__).expanduser().resolve()
    print(f"{name}: {module_path}")
    if module_path.is_relative_to(user_site) or "/.local/lib/" in module_path.as_posix():
        invalid.append(f"{name}: {module_path}")
if missing:
    raise SystemExit(
        "Conda 环境缺少 Demo Lab 依赖（已禁用用户级 site-packages）：\n"
        + "\n".join(missing)
        + "\n请在目标 Conda 环境中补齐依赖后重试。"
    )
if invalid:
    raise SystemExit(
        "检测到用户级依赖，拒绝混用 Conda 和 ~/.local：\n" + "\n".join(invalid)
    )
')"
dependency_status=$?
set -e
if ((dependency_status != 0)); then
    echo "错误：Conda 环境 ${conda_environment} 依赖检查失败。" >&2
    echo "${dependency_report}" >&2
    exit 1
fi
echo "${dependency_report}"

if [[ ! "${server_port}" =~ ^[0-9]+$ ]] || ((server_port < 1 || server_port > 65535)); then
    echo "错误：MEMORY_MONITOR_PORT 必须是 1 到 65535 之间的端口号。" >&2
    exit 1
fi

mkdir -p "${simulation_root}"
export MEMORY_MONITOR_SIMULATION_ROOT="${simulation_root}"

using_default_config=false
if [[ -z "${MEMORY_MONITOR_MEMORY_CONFIG:-}" ]]; then
    using_default_config=true
elif [[ ! -f "${MEMORY_MONITOR_MEMORY_CONFIG}" ]]; then
    echo "警告：找不到自定义配置文件：${MEMORY_MONITOR_MEMORY_CONFIG}" >&2
    echo "将改用仓库 Production 配置。" >&2
    using_default_config=true
    unset MEMORY_MONITOR_MEMORY_CONFIG
fi
if [[ -n "${MEMORY_MONITOR_MEMORY_CONFIG:-}" ]]; then
    export MEMORY_MONITOR_MEMORY_CONFIG
fi

if [[ "${using_default_config}" == true && -z "${DEEPSEEK_API_KEY:-}" ]]; then
    if [[ -t 0 ]]; then
        read -r -s -p "请输入 DEEPSEEK_API_KEY（仅用于本次进程，不会写入文件）：" DEEPSEEK_API_KEY
        echo
        if [[ -z "${DEEPSEEK_API_KEY}" ]]; then
            echo "错误：DEEPSEEK_API_KEY 不能为空。" >&2
            exit 1
        fi
        export DEEPSEEK_API_KEY
    else
        echo "错误：默认配置使用 DeepSeek，请先设置 DEEPSEEK_API_KEY。" >&2
        exit 1
    fi
fi

export STREAMLIT_SERVER_ADDRESS="${server_address}"
export STREAMLIT_SERVER_PORT="${server_port}"
export STREAMLIT_SERVER_HEADLESS=true
export STREAMLIT_BROWSER_GATHER_USAGE_STATS=false

echo "正在启动 Agent Memory Demo Lab..."
echo "访问地址：http://${browser_address}:${server_port}"
echo "Conda 环境：${conda_environment}"
echo "沙盒目录：${MEMORY_MONITOR_SIMULATION_ROOT}"
echo "配置来源：${MEMORY_MONITOR_MEMORY_CONFIG:-mem0.configs.production.load_production_memory_config}"
echo "按 Ctrl+C 停止服务。"

exec "${python_command[@]}" -m streamlit run memory_monitor/app.py "$@"
