#!/usr/bin/env bash
# GitHub master에 새 커밋이 있으면 코드만 당겨오고 서비스를 재시작한다.
# 진행 상태를 deploy_status.json에 단계별로 기록 → 웹 배포버튼이 폴링해 성공/실패를 표시한다.
# 안전장치:
#  - data/ 런타임 변경은 병합 전 stash 해서 ff-only 충돌을 원천 차단(데이터 보호).
#  - git merge --ff-only : 코드만 fast-forward(데이터 파일은 커밋에 없어 손대지 않음).
#  - 변경이 없으면 재시작하지 않는다.
# systemd 타이머(credit-deploy.timer) 또는 웹 배포버튼(credit-deploy.service)이 이 스크립트를 호출한다.
set -uo pipefail

REPO="$(cd "$(dirname "$0")" && pwd)"
SERVICE="credit-rating.service"
LOG="$REPO/auto_deploy.log"
STATUS="$REPO/deploy_status.json"
# sudoers NOPASSWD 규칙과 정확히 같은 절대경로로 호출(매칭 보장)
SYSTEMCTL="$(command -v systemctl || echo /usr/bin/systemctl)"
cd "$REPO" || exit 1

now(){ date '+%F %T'; }
# JSON 특수문자 이스케이프(역슬래시·따옴표) — sed 대신 순수 bash 치환(이식성)
esc(){ local s="$1"; s="${s//\\/\\\\}"; s="${s//\"/\\\"}"; printf '%s' "$s"; }
put(){ printf '{"state":"%s","message":"%s","ts":"%s"}\n' "$1" "$(esc "$2")" "$(now)" > "$STATUS"; }

put running "배포 진행 중 — 코드 확인"

git fetch --quiet origin master || { echo "$(now) fetch 실패" >>"$LOG"; put error "GitHub 연결 실패(fetch)"; exit 0; }

LOCAL="$(git rev-parse HEAD)"
REMOTE="$(git rev-parse origin/master)"
if [ "$LOCAL" = "$REMOTE" ]; then
  echo "$(now) 변경 없음" >>"$LOG"
  put no-change "이미 최신 코드입니다 (변경 없음)"
  exit 0
fi

echo "$(now) 업데이트 감지 ${LOCAL:0:7} -> ${REMOTE:0:7}" >>"$LOG"
put running "새 코드 반영 중 ${LOCAL:0:7} → ${REMOTE:0:7}"

# ── 런타임 데이터 보호: data/ 로컬 변경을 잠시 치워 ff-only 충돌 방지 ──
STASHED=0
if [ -n "$(git status --porcelain -- data/)" ]; then
  git stash push -q -- data/ >>"$LOG" 2>&1 && STASHED=1
fi

if git merge --ff-only origin/master >>"$LOG" 2>&1; then
  MERGE_OK=1
else
  MERGE_OK=0
fi

# ── data/ 원복(충돌 시 VM의 라이브 데이터 우선 보존) ──
if [ "$STASHED" = 1 ]; then
  git stash pop -q >>"$LOG" 2>&1 || {
    echo "$(now) stash pop 충돌 — VM 데이터 우선 보존" >>"$LOG"
    git checkout --theirs -- data/ >>"$LOG" 2>&1
    git reset -q -- data/ >>"$LOG" 2>&1
    git stash drop -q >>"$LOG" 2>&1
  }
fi

if [ "$MERGE_OK" != 1 ]; then
  echo "$(now) ff-only 병합 실패 — 배포 보류, 수동 확인 필요" >>"$LOG"
  put merge-failed "병합 실패(로컬 변경/이력 충돌) — 배포 보류, 수동 확인 필요"
  exit 0
fi

put restarting "코드 반영 완료 — 서비스 재시작 중"
# (이 스크립트는 credit-deploy.service에서 실행되므로 credit-rating 재시작에도 살아남아 아래 최종상태를 기록한다)
if sudo "$SYSTEMCTL" restart "$SERVICE" >>"$LOG" 2>&1; then
  echo "$(now) 배포 완료 · 서비스 재시작" >>"$LOG"
  put success "배포 완료 (${REMOTE:0:7})"
else
  echo "$(now) 코드는 반영됐으나 재시작 실패 — 수동 확인 필요" >>"$LOG"
  put restart-failed "코드는 반영됐으나 재시작 실패 — 수동 확인 필요"
fi
