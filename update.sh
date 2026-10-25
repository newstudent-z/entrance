#!/usr/bin/env bash
# 服务器上更新错题本：拉最新代码 → 重启服务
# 用法（在服务器 /opt/cuotiben 里）:  bash update.sh
# 前提：/opt/cuotiben 本身是个 git 仓库（首次迁移见 README「从 GitHub 更新」一节）
# 数据（data/、uploads/）在 .gitignore 里，git pull 不会动它们，密码和 API Key 也都在数据库里、不受影响。
set -euo pipefail
cd "$(dirname "$0")"
git pull --ff-only origin main
systemctl restart cuotiben
sleep 1
systemctl is-active cuotiben && echo "✅ 已更新并重启: https://codexme.top/cuoti/"
