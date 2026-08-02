#!/usr/bin/env bash
# VM에서 1회만 실행 → 웹 「서버배포」 버튼으로 배포(코드 전용)가 되도록 설정한다.
#   - credit-deploy.service(oneshot): 실행되면 auto_deploy.sh가 git pull(코드만)+재시작.
#   - Flask 앱(웹 버튼)이 이 oneshot을 트리거만 하고, oneshot이 서비스를 재시작한다.
#   - 3분 폴링 타이머는 만들지 않는다(원할 때만 버튼으로 배포).
# 사용법:  ./deploy/install_auto_deploy.sh
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
USER_NAME="$(id -un)"
SERVICE="credit-rating.service"
SYSTEMCTL="$(command -v systemctl)"

echo "리포: $REPO"
echo "실행계정: $USER_NAME"

# 1) 무비번 sudo 허용 — 딱 두 명령만:
#    · 웹 앱이 배포 oneshot을 트리거(--no-block start)
#    · auto_deploy.sh가 앱 서비스 재시작
sudo tee /etc/sudoers.d/credit-deploy >/dev/null <<SUDO
$USER_NAME ALL=(root) NOPASSWD: $SYSTEMCTL --no-block start credit-deploy.service
$USER_NAME ALL=(root) NOPASSWD: $SYSTEMCTL restart $SERVICE
SUDO
sudo chmod 440 /etc/sudoers.d/credit-deploy
sudo visudo -cf /etc/sudoers.d/credit-deploy >/dev/null && echo "sudoers 등록 OK"

# 2) auto_deploy.sh 실행권한
chmod +x "$REPO/auto_deploy.sh"

# 3) 배포 oneshot 서비스(타이머 없음 — 웹 버튼으로만 실행)
sudo tee /etc/systemd/system/credit-deploy.service >/dev/null <<UNIT
[Unit]
Description=Deploy credit-rating from GitHub (code only, on-demand)
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=$USER_NAME
WorkingDirectory=$REPO
ExecStart=$REPO/auto_deploy.sh
UNIT

# (이전에 타이머를 설치했다면 제거 — 폴링 안 함)
sudo systemctl disable --now credit-deploy.timer 2>/dev/null || true
sudo rm -f /etc/systemd/system/credit-deploy.timer

sudo systemctl daemon-reload

echo
echo "설치 완료 — 이제 웹 헤더의 「서버배포」 버튼으로 배포됩니다."
echo "테스트(수동 1회 실행):  sudo systemctl --no-block start credit-deploy.service"
echo "배포 로그:  tail -f $REPO/auto_deploy.log"
