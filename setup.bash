#!/usr/bin/env bash
# Ubuntu 下用于配置 ai_tuning_tool 项目的一键安装脚本

set -euo pipefail

REPO_URL="https://github.com/uglycatcat/AI_tuning_tool.git"
REPO_NAME="AI_tuning_tool"
CONDA_ENV="ai_env"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR=""

source_conda() {
  if command -v conda &>/dev/null; then
    # shellcheck disable=SC1091
    source "$(conda info --base)/etc/profile.d/conda.sh"
    return 0
  fi
  if [[ -f "${HOME}/miniconda3/etc/profile.d/conda.sh" ]]; then
    # shellcheck source=/dev/null
    source "${HOME}/miniconda3/etc/profile.d/conda.sh"
    return 0
  fi
  if [[ -f "${HOME}/anaconda3/etc/profile.d/conda.sh" ]]; then
    # shellcheck source=/dev/null
    source "${HOME}/anaconda3/etc/profile.d/conda.sh"
    return 0
  fi
  return 1
}

install_miniconda() {
  echo "未检测到 conda，正在安装 Miniconda..."
  mkdir -p "${HOME}/miniconda3"
  wget https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-x86_64.sh \
    -O "${HOME}/miniconda3/miniconda.sh"
  bash "${HOME}/miniconda3/miniconda.sh" -b -u -p "${HOME}/miniconda3"
  rm -f "${HOME}/miniconda3/miniconda.sh"
  "${HOME}/miniconda3/bin/conda" init --all
  # shellcheck source=/dev/null
  source "${HOME}/miniconda3/etc/profile.d/conda.sh"
}

ensure_conda_env() {
  if conda env list | awk '{print $1}' | grep -qx "${CONDA_ENV}"; then
    echo "conda 环境 ${CONDA_ENV} 已存在，跳过创建。"
    return 0
  fi
  echo "正在根据 environment.yml 创建 conda 环境 ${CONDA_ENV}..."
  conda env create -f environment.yml
}

write_api_key_to_config() {
  local api_key="$1"
  local config_file="$2"
  LLM_API_KEY_VALUE="${api_key}" CONFIG_FILE="${config_file}" conda run -n "${CONDA_ENV}" python - <<'PY'
import json
import os

config_path = os.environ["CONFIG_FILE"]
api_key = os.environ["LLM_API_KEY_VALUE"]

with open(config_path, encoding="utf-8") as f:
    config = json.load(f)

config["LLM_API_KEY"] = api_key

with open(config_path, "w", encoding="utf-8") as f:
    json.dump(config, f, indent=2, ensure_ascii=False)
    f.write("\n")
PY
}

# STEP 1: 确认是否需要 clone 仓库
echo "请确认你运行 setup.bash 时的文件状态，据此我将决定是否 clone 仓库"
echo "1. 你是在 clone 得到的 ai_tuning_tool 仓库中运行 setup.bash（那么我会跳过 clone 的步骤）"
echo "2. 你是在其他地方得到的 setup.bash 文件并运行（那么我会帮你 clone 仓库）"
echo "请输入 1 或 2，（直接回车则默认 2）"
read -r setup_choice

if [[ -z "${setup_choice}" ]]; then
  setup_choice="2"
fi

case "${setup_choice}" in
  1)
    PROJECT_DIR="${SCRIPT_DIR}"
    echo "已在仓库目录中，跳过 clone。"
    ;;
  2)
    CLONE_DIR="$(pwd)/${REPO_NAME}"
    if [[ -d "${CLONE_DIR}/.git" ]]; then
      echo "目录 ${CLONE_DIR} 已存在，直接进入。"
    else
      echo "正在 clone 仓库到 ${CLONE_DIR} ..."
      git clone "${REPO_URL}" "${CLONE_DIR}"
    fi
    PROJECT_DIR="${CLONE_DIR}"
    ;;
  *)
    echo "无效输入：${setup_choice}，请输入 1 或 2。"
    exit 1
    ;;
esac

cd "${PROJECT_DIR}"
echo "项目目录：${PROJECT_DIR}"

# STEP 2: 检查并配置 conda 环境
if ! source_conda; then
  install_miniconda
fi

ensure_conda_env

# STEP 3: 生成 config.json 并写入 API Key
if [[ ! -f config.json ]]; then
  if [[ ! -f config_example.json ]]; then
    echo "错误：未找到 config_example.json。"
    exit 1
  fi
  cp config_example.json config.json
  echo "已从 config_example.json 创建 config.json。"
else
  echo "config.json 已存在，将更新其中的 LLM_API_KEY。"
fi

echo "你使用的 claude key 是（直接回车则默认使用config.json中的值）："
read -r api_key
if [[ -n "${api_key}" ]]; then
  write_api_key_to_config "${api_key}" "${PROJECT_DIR}/config.json"
  echo "已将 API Key 写入 config.json。"
else
  echo "未输入 API Key，保留 config.json 中的现有值。"
fi

echo "请确认 config.json 中的 BASE_URL 等配置是否符合你的网关设置。"

# STEP 4: 启动项目
echo "正在启动项目..."
source_conda
conda activate "${CONDA_ENV}"

if [[ ! -f start.bash ]]; then
  echo "错误：未找到 start.bash。"
  exit 1
fi

# 在当前 shell 中 source，避免子 shell 无法 conda activate
# shellcheck source=/dev/null
source start.bash
