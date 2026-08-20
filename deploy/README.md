# 배포 (GitHub → VM 자동배포, 코드 전용)

GitHub `master`에 **push되면 VM이 자동으로** 최신 **코드만** 받아와(`git merge --ff-only`)
서비스를 재시작한다. `credit-deploy.timer`가 1분마다 새 커밋을 확인한다.
커밋에 없는 **데이터 파일(`data/*`)은 손대지 않으며**, 병합 전 `data/`를 자동 stash 해
운영 데이터를 보호한다. 웹 헤더의 **🚀 서버배포** 버튼은 "기다리지 않고 즉시 배포"용으로 계속 동작한다.

## 흐름
1. (클로드가) 코드 수정 → GitHub `master`에 push
2. VM 타이머가 감지(최대 1분) → 코드 pull + 재시작 → 자동 반영
   - 또는 웹에서 **🚀 서버배포** 클릭 시 즉시 반영
3. 배포 진행상태(성공/실패/변경없음)는 배포 버튼 팝업에 실시간 표시(`deploy_status.json` 폴링)

## 최초 설치 / 자동배포 켜기 (VM에서 1회)

```bash
cd ~/credit-rating-manager        # 실제 리포 경로로 이동
git pull origin master            # 배포 스크립트 포함 최신 코드 받기
chmod +x deploy/install_auto_deploy.sh
./deploy/install_auto_deploy.sh
```

설치가 하는 일:
1. 무비번 sudo 2줄 허용(`/etc/sudoers.d/credit-deploy`): 배포 oneshot 트리거 +
   `auto_deploy.sh`가 `credit-rating.service` 재시작
2. `credit-deploy.service`(oneshot) 생성 — 새 커밋이 있으면 pull+재시작
3. `credit-deploy.timer` 생성·활성화 — 1분마다 위 서비스 실행(자동배포)

폴링 주기를 바꾸려면 `install_auto_deploy.sh`의 `POLL="1min"` 을 `2min`·`5min` 등으로 바꿔 다시 실행.

## 동작 확인 / 로그

```bash
systemctl status credit-deploy.timer --no-pager          # 타이머 활성 여부
systemctl list-timers credit-deploy.timer --no-pager     # 다음 실행 예정
sudo systemctl --no-block start credit-deploy.service    # 버튼과 동일한 즉시 실행
tail -f ~/credit-rating-manager/auto_deploy.log          # 배포 이력
```

## 자동배포 끄기 / 해제

```bash
sudo systemctl disable --now credit-deploy.timer         # 자동배포만 끄기(버튼은 유지)
# 완전 제거:
sudo rm -f /etc/systemd/system/credit-deploy.timer /etc/systemd/system/credit-deploy.service /etc/sudoers.d/credit-deploy
sudo systemctl daemon-reload
```

## 주의
- 운영 데이터(`data/pension_store.json`, `data/rate_history.*`, `data/ratings.json`,
  `data/visit_stats.json` 등)는 커밋하지 않는다. 배포는 코드만 가져오고, 병합 전 `data/`를
  자동 stash 하므로 서버 데이터는 유지된다.
- 리포지토리 인증은 VM에 이미 설정된 것(수동 `git pull`이 되던 그 설정)을 그대로 사용한다.
- 🚀 서버배포 버튼은 연금컨설팅팀에게만 보이며, RM 계정은 서버에서 차단된다.
