#!/usr/bin/env bash
# boss-auto-apply 一键环境准备（M1+M2，设计 §14）
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_DIR"
ENV_NAME="boss-auto"
PYTHON_VER="3.12"

echo "==> [1/8] 检查 brew"
command -v brew >/dev/null || { echo "❌ 请先装 brew：https://brew.sh"; exit 1; }

echo "==> [2/8] 检查 node (可选)"
command -v node >/dev/null && node --version || echo "⚠️ node 未装（claude CLI 不依赖 node，可忽略）"

echo "==> [3/8] 检查 Claude CLI（改造1：zcode→claude）"
command -v claude >/dev/null || {
  echo "❌ claude CLI 未装。请装 Claude CLI："
  echo "     npm install -g @anthropic-ai/claude-code"
  exit 1
}
claude --version
echo "    claude CLI OK"

echo "==> [4/8] 装 typst + 思源黑体（brew）"
brew install typst || echo "⚠️ typst 可能已装"
brew install --cask font-noto-sans-cjk-sc || echo "⚠️ 字体可能已装，跳过"

echo "==> [5/8] clone brilliant-CV 模板"
[ -d templates/brilliant-cv ] || git clone --depth 1 https://github.com/yunanwg/brilliant-CV.git templates/brilliant-cv
echo "    template OK"

echo "==> [6/8] 建 conda venv (python=$PYTHON_VER)"
if ! command -v conda >/dev/null; then
  echo "❌ 未找到 conda。请先装 Anaconda/Miniconda，或改用系统 python3 -m venv .venv"
  exit 1
fi
# shellcheck disable=SC1091
source "$(conda info --base)/etc/profile.d/conda.sh" 2>/dev/null || true
if ! conda env list | grep -q "^$ENV_NAME "; then
  conda create -y -n "$ENV_NAME" "python=$PYTHON_VER"
fi
conda activate "$ENV_NAME"
python --version | grep -q "3.12" || { echo "❌ python 版本非 3.12"; exit 1; }
pip install --upgrade pip

echo "==> [7/8] 装 Python 依赖"
if ! pip install -r requirements.txt; then
  echo "⚠️ docling 可能装失败，将降级为 PyMuPDF 兜底解析"
  pip install pymupdf pyyaml jsonschema pydantic pillow loguru rich pytest pytest-mock pytest-cov
fi

echo "==> [8/8] 自检"
python -c "import fitz, yaml, jsonschema, pydantic, loguru, rich, PIL; print('✅ core deps OK')"
command -v typst >/dev/null && typst --version || echo "⚠️ typst 未就绪（F5/F6 受限）"
[ -d templates/brilliant-cv ] && echo "✅ template OK"

# 拷贝示例配置（若不存在）
[ -f config/config.yaml ] || cp config/config.example.yaml config/config.yaml && echo "✅ config.yaml 已生成（按需修改）"

echo ""
echo "============================================================"
echo "✅ 环境就绪。下一步："
echo "   1) 首次登录 Claude CLI（真跑前必做，dry-run 可跳过）："
echo "        claude auth login"
echo "   2) 把简历放到 input/resume.pdf"
echo "   3) 跑预检："
echo "        conda run -n $ENV_NAME python -m boss_auto_apply preflight"
echo "   4) 跑 dry-run（M1+M2 终点）："
echo "        conda run -n $ENV_NAME python -m boss_auto_apply dry-run --non-interactive"
echo "   5) M3+ 真投递前装 boss-cli："
echo "        uv tool install kabi-boss-cli && boss login"
echo "============================================================"
