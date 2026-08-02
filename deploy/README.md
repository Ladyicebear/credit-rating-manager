# 자동 배포 (GitHub → VM, 코드 전용)

VM이 몇 분마다 GitHub `master`를 확인해 **새 커밋이 있으면 코드만** 받아와 서비스를 재시작한다.
`git merge --ff-only`만 쓰므로 커밋에 없는 **데이터 파일(`data/*`)은 절대 건드리지 않는다.**
로컬 변경과 충돌하면 병합을 중단하고 아무것도 바꾸지 않는다(운영 데이터 보호 우선).

## 최초 설치 (VM에서 1회)

```bash
cd ~/credit-rating-manager        # 실제 리포 경로로 이동
git pull origin master            # 자동배포 스크립트 포함해 최신 코드 받기
chmod +x deploy/install_auto_deploy.sh
./deploy/install_auto_deploy.sh          # 기본 3분 주기
# 주기를 바꾸려면:  ./deploy/install_auto_deploy.sh 5min
```

설치가 하는 일:
1. `credit-rating.service` **재시작 명령 하나만** 무비번 sudo 허용(`/etc/sudoers.d/credit-deploy`)
2. `credit-deploy.service` + `credit-deploy.timer` 생성 후 타이머 활성화

이후로는 로컬 PC에서 `git push`만 하면 몇 분 안에 VM에 자동 반영된다.

## 상태 확인

```bash
systemctl list-timers credit-deploy.timer --no-pager   # 다음 실행 시각
tail -f ~/credit-rating-manager/auto_deploy.log        # 배포 이력
sudo systemctl start credit-deploy.service             # 지금 즉시 한 번 실행
```

## 해제

```bash
sudo systemctl disable --now credit-deploy.timer
sudo rm /etc/systemd/system/credit-deploy.{service,timer} /etc/sudoers.d/credit-deploy
sudo systemctl daemon-reload
```

## 주의
- 운영 데이터(`data/pension_store.json`, `data/rate_history.*`, `data/ratings.json`,
  `data/visit_stats.json` 등)는 커밋하지 않는다. 자동배포는 코드만 가져오므로 서버 데이터는 유지된다.
- 리포지토리 인증은 VM에 이미 설정된 것(수동 `git pull`이 되던 그 설정)을 그대로 사용한다. 새 키·비밀 등록 불필요.
