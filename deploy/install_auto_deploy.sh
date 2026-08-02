#!/usr/bin/env bash
# VM에서 1회만 실행 → GitHub 자동 pull 배포(코드 전용)를 systemd 타이머로 설치한다.
# 사용법:  ./deploy/install_auto_deploy.sh [주기]
#   예)   ./deploy/install_auto_deploy.sh 3min   (기본 3분)
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
USER_NAME="$(id -un)"
SERVICE="credit-rating.service"
INTERVAL="${1:-3min}"
SYSTEMCTL="$(command -v systemctl)"

echo "리포: $REPO"
echo "실행계정: $USER_NAME"
echo "확인주기: $INTERVAL"

# 1) 서비스 재시작만 무비번 sudo 허용(딱 이 명령 하나만)
echo "$USER_NAME ALL=(root) NOPASSWD: $SYSTEMCTL restart $SERVICE" | sudo tee /etc/sudoers.d/credit-deploy >/dev/null
sudo chmod 440 /etc/sudoers.d/credit-deploy
sudo visudo -cf /etc/sudoers.d/credit-deploy >/dev/null && echo "sudoers 등록 OK"

# 2) auto_deploy.sh 실행권한
chmod +x "$REPO/auto_deploy.sh"

# 3) systemd 유닛(서비스+타이머) 생성
sudo tee /etc/systemd/system/credit-deploy.service >/dev/null <<UNIT
[Unit]
Description=Auto-deploy credit-rating from GitHub (code only)
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=$USER_NAME
WorkingDirectory=$REPO
ExecStart=$REPO/auto_deploy.sh
UNIT

sudo tee /etc/systemd/system/credit-deploy.timer >/dev/null <<UNIT
[Unit]
Description=Check GitHub for credit-rating updates every $INTERVAL

[Timer]
OnBootSec=2min
OnUnitActiveSec=$INTERVAL
Persistent=true

[Install]
WantedBy=timers.target
UNIT

# 4) 활성화
sudo systemctl daemon-reload
sudo systemctl enable --now credit-deploy.timer

echo
echo "설치 완료. 상태 확인:"
echo "  systemctl status credit-deploy.timer --no-pager"
echo "  systemctl list-timers credit-deploy.timer --no-pager"
echo "배포 로그:  tail -f $REPO/auto_deploy.log"
echo "지금 한 번 즉시 실행:  sudo systemctl start credit-deploy.service"
