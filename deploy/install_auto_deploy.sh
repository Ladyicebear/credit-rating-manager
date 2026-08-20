#!/usr/bin/env bash
# VM에서 1회만 실행 → GitHub master에 푸시되면 자동으로 코드 배포(+재시작)되도록 설정한다.
#   - credit-deploy.timer  : 1분마다 credit-deploy.service 실행(폴링).
#   - credit-deploy.service(oneshot): auto_deploy.sh가 새 커밋이 있으면 git pull(코드만)+재시작.
#     (auto_deploy.sh는 data/ 자동 stash로 데이터 보호 + 진행상태를 deploy_status.json에 기록)
#   - 웹 「서버배포」 버튼도 그대로 동작(즉시 배포 트리거).
# 사용법:  ./deploy/install_auto_deploy.sh
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
USER_NAME="$(id -un)"
SERVICE="credit-rating.service"
SYSTEMCTL="$(command -v systemctl)"
POLL="1min"   # 폴링 주기(푸시 후 최대 이 시간 내 자동 배포). 원하면 2min·5min 등으로 조정.

echo "리포: $REPO"
echo "실행계정: $USER_NAME"
echo "폴링주기: $POLL"

# 1) 무비번 sudo 허용 — 딱 두 명령만:
#    · 웹 앱/타이머가 배포 oneshot을 트리거
#    · auto_deploy.sh가 앱 서비스 재시작
sudo tee /etc/sudoers.d/credit-deploy >/dev/null <<SUDO
$USER_NAME ALL=(root) NOPASSWD: $SYSTEMCTL --no-block start credit-deploy.service
$USER_NAME ALL=(root) NOPASSWD: $SYSTEMCTL restart $SERVICE
SUDO
sudo chmod 440 /etc/sudoers.d/credit-deploy
sudo visudo -cf /etc/sudoers.d/credit-deploy >/dev/null && echo "sudoers 등록 OK"

# 2) auto_deploy.sh 실행권한
chmod +x "$REPO/auto_deploy.sh"

# 3) 배포 oneshot 서비스
sudo tee /etc/systemd/system/credit-deploy.service >/dev/null <<UNIT
[Unit]
Description=Deploy credit-rating from GitHub (code only)
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=$USER_NAME
WorkingDirectory=$REPO
ExecStart=$REPO/auto_deploy.sh
UNIT

# 4) 폴링 타이머(자동 배포) — master에 푸시되면 최대 $POLL 내 자동 반영
sudo tee /etc/systemd/system/credit-deploy.timer >/dev/null <<UNIT
[Unit]
Description=Auto-deploy credit-rating: poll GitHub every $POLL

[Timer]
OnBootSec=$POLL
OnUnitActiveSec=$POLL
Unit=credit-deploy.service
Persistent=true

[Install]
WantedBy=timers.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable --now credit-deploy.timer

echo
echo "설치 완료 — 이제 GitHub master에 푸시되면 최대 $POLL 내 자동 배포됩니다."
echo "타이머 상태:      systemctl status credit-deploy.timer --no-pager"
echo "다음 실행 예정:   systemctl list-timers credit-deploy.timer --no-pager"
echo "즉시 1회 배포:    sudo systemctl --no-block start credit-deploy.service   (또는 웹 「서버배포」 버튼)"
echo "배포 로그:        tail -f $REPO/auto_deploy.log"
echo "자동배포 끄기:    sudo systemctl disable --now credit-deploy.timer"
