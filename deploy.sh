#!/usr/bin/env bash
# 错题本一键部署（Ubuntu 22.04/24.04，需 root 或 sudo）
# 用法:  bash deploy.sh [端口,默认18080]
# 登录密码不在脚本里设：首次打开页面时用「设置码」自设（脚本结尾会打印设置码）。
# 也可预设密码:  CUOTI_PASSWORD='你的密码' bash deploy.sh
set -euo pipefail
PORT="${1:-18080}"
APP_DIR=/opt/cuotiben
mkdir -p "$APP_DIR/data" "$APP_DIR/uploads"
cp "$(dirname "$0")/app.py" "$APP_DIR/app.py"
ENV_LINES="Environment=CUOTI_PORT=$PORT
Environment=CUOTI_DB=$APP_DIR/data/cuoti.db
Environment=CUOTI_UPLOAD=$APP_DIR/uploads"
if [ -n "${CUOTI_PASSWORD:-}" ]; then
  ENV_LINES="$ENV_LINES
Environment=CUOTI_PASSWORD=$CUOTI_PASSWORD"
  SETUP_HINT="已预设登录密码，直接用它登录。"
else
  SETUP_CODE="${CUOTI_SETUP_CODE:-$(head -c 12 /dev/urandom | od -An -tx1 | tr -d ' \n' | head -c 12)}"
  ENV_LINES="$ENV_LINES
Environment=CUOTI_SETUP_CODE=$SETUP_CODE"
  SETUP_HINT="首次打开页面时输入设置码 $SETUP_CODE ，再设你自己的登录密码（设置码用后失效）。"
fi
cat > /etc/systemd/system/cuotiben.service <<EOF
[Unit]
Description=Cuotiben (mistake notebook)
After=network.target
[Service]
WorkingDirectory=$APP_DIR
$ENV_LINES
ExecStart=/usr/bin/python3 $APP_DIR/app.py
Restart=always
User=root
[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload && systemctl enable --now cuotiben
sleep 1
systemctl is-active cuotiben && echo "✅ 已启动: http://服务器公网IP:$PORT"
echo "🔑 $SETUP_HINT"
echo "⚠️ 别忘了在云厂商安全组放行 $PORT 端口"
echo "📦 数据: $APP_DIR/data/cuoti.db + $APP_DIR/uploads/，页面里也能一键导出 JSON"
