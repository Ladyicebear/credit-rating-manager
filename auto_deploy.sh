#!/usr/bin/env bash
# GitHub master에 새 커밋이 있으면 코드만 당겨오고 서비스를 재시작한다.
# 안전장치:
#  - git merge --ff-only  : 코드만 fast-forward. 데이터 파일(data/*)은 커밋에 없어 손대지 않음.
#    로컬 변경과 충돌하면 병합을 '중단'하고 아무것도 바꾸지 않는다(데이터 보호).
#  - 변경이 없으면 즉시 종료(재시작 안 함).
# systemd 타이머(credit-deploy.timer)가 몇 분마다 이 스크립트를 호출한다.
set -uo pipefail

REPO="$(cd "$(dirname "$0")" && pwd)"
SERVICE="credit-rating.service"
LOG="$REPO/auto_deploy.log"
cd "$REPO" || exit 1

git fetch --quiet origin master || { echo "$(date '+%F %T') fetch 실패"; exit 0; } >>"$LOG" 2>&1

LOCAL="$(git rev-parse HEAD)"
REMOTE="$(git rev-parse origin/master)"
[ "$LOCAL" = "$REMOTE" ] && exit 0   # 변경 없음

echo "$(date '+%F %T') 업데이트 감지 ${LOCAL:0:7} -> ${REMOTE:0:7}" >>"$LOG"
if git merge --ff-only origin/master >>"$LOG" 2>&1; then
  if sudo systemctl restart "$SERVICE" >>"$LOG" 2>&1; then
    echo "$(date '+%F %T') 배포 완료 · 서비스 재시작" >>"$LOG"
  else
    echo "$(date '+%F %T') 코드는 반영됐으나 재시작 실패 — 수동 확인 필요" >>"$LOG"
  fi
else
  echo "$(date '+%F %T') ff-only 병합 실패(로컬 변경 충돌?) — 배포 보류, 수동 확인 필요" >>"$LOG"
fi
