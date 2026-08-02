# 배포 (GitHub → VM, 웹 「서버배포」 버튼, 코드 전용)

연금컨설팅팀이 웹 헤더의 **🚀 서버배포** 버튼을 누르면, VM이 GitHub `master`의
**최신 코드만** 받아와(`git merge --ff-only`) 서비스를 재시작한다.
커밋에 없는 **데이터 파일(`data/*`)은 손대지 않는다.** 로컬 변경과 충돌하면
병합을 중단하고 아무것도 바꾸지 않는다(운영 데이터 보호 우선). 3분 폴링 같은 자동배포는 없다.

## 흐름
1. (클로드가) 코드 수정 → GitHub `master`에 push
2. 연금컨설팅팀이 웹에서 **🚀 서버배포 → 배포** 클릭
3. VM이 코드 pull + 재시작 → 반영

## 최초 설치 (VM에서 1회)

```bash
cd ~/credit-rating-manager        # 실제 리포 경로로 이동
git pull origin master            # 배포 스크립트 포함 최신 코드 받기
chmod +x deploy/install_auto_deploy.sh
./deploy/install_auto_deploy.sh
```

설치가 하는 일:
1. 무비번 sudo 2줄 허용(`/etc/sudoers.d/credit-deploy`): 웹 앱이 배포 oneshot 트리거 +
   `auto_deploy.sh`가 `credit-rating.service` 재시작
2. `credit-deploy.service`(oneshot) 생성 — 웹 버튼이 이걸 실행
3. (이전 타이머가 있으면 제거)

이후로는 로컬에서 push → 웹 버튼 클릭이면 반영된다.

## 동작 확인 / 로그

```bash
sudo systemctl --no-block start credit-deploy.service   # 버튼과 동일한 수동 실행
tail -f ~/credit-rating-manager/auto_deploy.log         # 배포 이력
```

## 해제

```bash
sudo rm /etc/systemd/system/credit-deploy.service /etc/sudoers.d/credit-deploy
sudo systemctl daemon-reload
```

## 주의
- 운영 데이터(`data/pension_store.json`, `data/rate_history.*`, `data/ratings.json`,
  `data/visit_stats.json` 등)는 커밋하지 않는다. 배포는 코드만 가져오므로 서버 데이터는 유지된다.
- 리포지토리 인증은 VM에 이미 설정된 것(수동 `git pull`이 되던 그 설정)을 그대로 사용한다. 새 키·비밀 등록 불필요.
- 🚀 서버배포 버튼은 연금컨설팅팀에게만 보이며, RM 계정은 서버에서 차단된다.
