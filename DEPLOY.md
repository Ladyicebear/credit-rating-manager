# 신용등급 관리 시스템 — Google Cloud Run 배포 가이드

이 앱은 이미 컨테이너 배포 준비가 돼 있습니다(`Dockerfile`, `requirements.txt`, `PORT` 환경변수 처리).
아래 순서대로 하면 됩니다. `<...>` 부분만 본인 값으로 바꾸세요.

---

## 0. 사전 준비 (최초 1회)

1. **결제가 등록된 GCP 프로젝트** 준비 (https://console.cloud.google.com → 프로젝트 생성 → 결제 연결)
2. **gcloud CLI 설치**: https://cloud.google.com/sdk/docs/install
3. 로그인 & 프로젝트 지정:
   ```bash
   gcloud auth login
   gcloud config set project <PROJECT_ID>
   ```

## 1. 필요한 서비스(API) 켜기
```bash
gcloud services enable run.googleapis.com cloudbuild.googleapis.com storage.googleapis.com
```

## 2. 데이터 영구 저장용 버킷 만들기 + 초기 데이터 업로드
> Cloud Run은 재배포하면 컨테이너 파일이 초기화됩니다. 그래서 `data/`(조회결과·**변경이력**·기관목록·정정값)를
> GCS 버킷에 두고 앱에 연결합니다. 이 버킷이 앞으로 **데이터의 진짜 원본**이 됩니다.

```bash
# 버킷 이름은 전 세계에서 유일해야 함 (예: goun-credit-rating-data)
gcloud storage buckets create gs://<BUCKET> --location=asia-northeast3

# 로컬 data 폴더의 초기 파일들을 버킷에 업로드 (credit_rating_manager 폴더에서 실행)
gcloud storage cp data/*.json gs://<BUCKET>/
```

## 3. 배포 (소스에서 바로 빌드 + 배포)
`credit_rating_manager` 폴더 안에서 실행하세요.
```bash
gcloud run deploy credit-rating-manager \
  --source . \
  --region asia-northeast3 \
  --execution-environment gen2 \
  --allow-unauthenticated \
  --cpu 2 --memory 4Gi \
  --min-instances 1 --no-cpu-throttling \
  --timeout 3600 \
  --set-env-vars "TZ=Asia/Seoul,APP_USER=<로그인아이디>,APP_PASSWORD=<로그인비밀번호>,SECRET_KEY=<아무-긴-랜덤문자열>" \
  --add-volume "name=data,type=cloud-storage,bucket=<BUCKET>" \
  --add-volume-mount "volume=data,mount-path=/app/data"
```
> **SECRET_KEY**: 로그인 세션 쿠키 서명용. 아무 긴 랜덤 문자열이면 됩니다(예: `openssl rand -hex 32` 결과).
> 여러 인스턴스가 같은 값을 써야 로그인이 유지되므로 **반드시 고정값으로 지정**하세요.
> `APP_USER`/`APP_PASSWORD`를 안 주면 기본 `admin`/`goun`으로 뜨니, 배포 시 꼭 본인 값으로 바꾸세요.
> `APP_USER` 계정이 **연금컨설팅팀(전체관리)** 계정이며, 회원 가입 승인을 담당합니다.

### 회원가입·승인·이메일 매직링크 로그인 환경변수 (선택)

현업 사용자는 **회사 이메일로 가입 신청 → 연금컨설팅팀 승인 → 로그인할 때마다 회사 이메일로 온
매직링크 클릭**으로 접속합니다(비밀번호 없음, 승인된 회원은 조회·다운로드 전용). 이 흐름을 쓰려면
아래 값을 `--set-env-vars`(또는 `--update-env-vars`)에 함께 넣으세요. **미설정 시** 매직링크 이메일이
실제로 발송되지 않고 서버 로그에만 링크가 남습니다(로컬 개발용).

| 환경변수 | 설명 | 예시 |
|---|---|---|
| `SMTP_USER` | 발신 Gmail 주소 | `noreply@miraeasset.com` 또는 Gmail 계정 |
| `SMTP_PASSWORD` | 구글 **앱 비밀번호**(계정 비밀번호 아님) | 2단계 인증 후 발급한 16자리 |
| `SMTP_FROM` | 발신 표시 주소(미설정 시 `SMTP_USER`) | `noreply@miraeasset.com` |
| `SMTP_HOST` / `SMTP_PORT` | 기본 `smtp.gmail.com` / `587`. 다른 서비스(SendGrid 등)로 바꿀 때만 지정 | |
| `APP_BASE_URL` | 매직링크에 넣을 앱 주소(프록시 뒤 배포 시 권장) | `https://credit-rating-manager-xxxx.a.run.app` |
| `ALLOWED_EMAIL_DOMAINS` | 가입 허용 도메인(쉼표 구분). 기본 `miraeasset.com` | `miraeasset.com` |
| `VERIFY_CODE_TTL_MIN` | 로그인/비밀번호변경 인증코드 유효시간(분). 기본 `10` | `10` |
| `EMAIL_LOGIN_SESSION_DAYS` | 이메일 로그인 세션 + 기기 신뢰 유지 기간(일). 기본 `30` | `30` |
| `ADMIN_NOTIFY_EMAIL` | 새 가입 신청 알림을 받을 주소(선택) | 담당자 이메일 |

> **Gmail 앱 비밀번호**: Google 계정 → 보안 → 2단계 인증 켜기 → "앱 비밀번호" 생성 → 그 값을 `SMTP_PASSWORD`로.
> Gmail은 소량(하루 수백 통)에 적합합니다. 도달률/대량 발송이 필요해지면 `SMTP_HOST`/`SMTP_PORT`만 바꿔
> SendGrid·SES 등으로 교체할 수 있습니다.
>
> **승인 관리 화면**: 연금컨설팅팀(`APP_USER`) 계정으로 로그인한 뒤 **`/admin/members`** 로 접속하면
> 가입 대기자를 승인/거절할 수 있습니다.
- 처음이면 "Artifact Registry 저장소를 만들까요?" 등을 물어봅니다 → `Y`.
- 빌드에 몇 분 걸립니다(크롬 포함 이미지).
- 끝나면 `Service URL: https://credit-rating-manager-xxxxxxxx.a.run.app` 이 출력됩니다.

## 4. 접속 & 로그인
- 출력된 URL에 접속하면 **로그인 페이지**가 나옵니다.
- 3번에서 정한 `APP_USER` / `APP_PASSWORD` 를 입력하면 앱으로 들어갑니다. (우상단 🔓 로그아웃 버튼으로 로그아웃)
- `--allow-unauthenticated`는 "구글 IAM 대신 앱 자체 로그인으로 막는다"는 뜻입니다. 접근 제한은 앱의 로그인이 담당합니다.

## 5. 매일 오전 8시 자동조회
- `--min-instances 1 --no-cpu-throttling` 이라 인스턴스가 항상 켜져 있어, 앱 내장 스케줄러가
  **매일 8시(한국시간) 자동 조회**를 실행합니다. **추가 설정 불필요.**

---

## 🔁 코드 수정 후 재배포
```bash
gcloud run deploy credit-rating-manager --source . --region asia-northeast3
```
- 위 옵션들(볼륨·환경변수 등)은 서비스에 저장돼 있어 다시 안 적어도 됩니다.
- **데이터는 GCS 버킷에 있으므로 재배포해도 그대로 유지됩니다.**

## 🔐 아이디/비밀번호 변경
```bash
gcloud run services update credit-rating-manager --region asia-northeast3 \
  --update-env-vars "APP_USER=<새아이디>,APP_PASSWORD=<새비밀번호>"
```
> `--update-env-vars`는 기존 환경변수(SECRET_KEY 등)를 유지하며 지정한 값만 바꿉니다.

---

## 💰 비용 참고 (중요)
- 위 설정은 **인스턴스 1개를 항상 켜두는 방식**(min-instances 1 + CPU 상시할당)이라,
  대략 **월 $50~90 수준**(2vCPU/4GiB 기준)의 고정 비용이 발생합니다.
- 이 앱은 하루 1번 + 가끔 수동조회만 하므로, **평소엔 꺼두고 필요할 때만 켜는(scale-to-zero) 방식**으로
  바꾸면 비용을 크게 줄일 수 있습니다. 다만 그러려면 스케줄러를 Cloud Scheduler로 분리하는 추가 작업이 필요합니다.
  → 원하시면 그 구성으로 만들어 드리겠습니다(코드에 전용 엔드포인트 추가 + Cloud Scheduler 설정).

## ⚠️ 문제 해결
- **메모리 부족(OOM)으로 조회 실패**: 조회 시 크롬이 여러 개 동시에 떠서 무겁습니다.
  `--memory 8Gi` 로 올리거나, 동시 실행 개수를 줄이는 코드 수정을 요청하세요.
- **버킷 접근 권한 오류**: Cloud Run 서비스 계정에 스토리지 권한 부여
  ```bash
  gcloud storage buckets add-iam-policy-binding gs://<BUCKET> \
    --member="serviceAccount:<PROJECT_NUMBER>-compute@developer.gserviceaccount.com" \
    --role="roles/storage.objectAdmin"
  ```
- **로컬에서 먼저 도커로 테스트**(선택):
  ```bash
  docker build -t crm .
  docker run -p 8080:8080 -e APP_USER=goun -e APP_PASSWORD=test crm
  # 브라우저에서 http://localhost:8080 접속
  ```
