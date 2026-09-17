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

## 🔐 2차 보안 — RM 개별 계정(회원가입 → 승인 → 이메일 본인인증)

RM 영업사원이 **회사 이메일로 직접 회원가입** → **연금컨설팅팀이 승인** → 승인된 사원이
로그인하면 **회사 이메일로 6자리 인증코드**가 발송되고, 코드를 확인해야 로그인됩니다.
한 번 인증한 브라우저는 **30일간 코드 없이** 로그인합니다(신뢰기기).

> 기존 연금컨설팅팀 계정(`APP_USER`/`APP_PASSWORD`)과 환경변수 RM 공용계정은 그대로 동작합니다.
> 개별 RM 계정은 그 위에 추가되는 방식이라 기존 로그인은 영향받지 않습니다.

### 필요한 환경변수
```bash
gcloud run services update credit-rating-manager --region asia-northeast3 \
  --update-env-vars "\
RM_EMAIL_DOMAINS=company.co.kr,\
RM_TEAMS=강남WM센터,서초WM센터,여의도WM센터,판교WM센터,분당WM센터,\
SMTP_HOST=smtp.gmail.com,\
SMTP_PORT=587,\
SMTP_USER=noreply@company.co.kr,\
SMTP_PASS=<Gmail-앱-비밀번호-16자리>,\
SMTP_FROM=noreply@company.co.kr,\
SMTP_FROM_NAME=스마트펜션,\
RM_ADMIN_NOTIFY_EMAIL=pension-team@company.co.kr"
```
| 변수 | 설명 |
|------|------|
| `RM_EMAIL_DOMAINS` | 가입 허용 회사 이메일 도메인(쉼표구분). 예: `company.co.kr`. **미설정 시 도메인 제한 없음**(외부 메일도 가입 가능하니 반드시 설정 권장). |
| `RM_TEAMS` | 회원가입 시 선택하는 소속팀 목록(쉼표구분). 미설정 시 기본 목록 사용. |
| `RM_DEVICE_TRUST_DAYS` | 신뢰기기 유지일. 기본 `30`. |
| `RM_ADMIN_NOTIFY_EMAIL` | 새 가입 신청 시 알림 메일을 받을 연금컨설팅팀 주소(선택). |
| `SMTP_HOST/PORT/USER/PASS/FROM/FROM_NAME` | 인증·안내 메일 발송 계정. 업체 비종속(표준 SMTP). |

> **SMTP를 설정하지 않으면** 인증코드가 실제로 발송되지 않고 서버 로그에만 남습니다(개발용).
> 운영에서는 반드시 SMTP를 설정하세요. Cloud Run은 25번 포트를 막지만 **587(STARTTLS)·465(SSL)는 열려 있어** Gmail/Workspace·SendGrid·네이버웍스 등 어떤 SMTP든 사용할 수 있습니다.

### Gmail / Google Workspace로 발송하기 (가장 간단)
1. 발송용 구글 계정에서 **2단계 인증**을 켠다.
2. Google 계정 → 보안 → **앱 비밀번호** 발급(16자리). → `SMTP_PASS`에 넣는다.
3. `SMTP_HOST=smtp.gmail.com`, `SMTP_PORT=587`, `SMTP_USER`=그 계정 메일, `SMTP_FROM`=동일(또는 Workspace 별칭).
   - Workspace라면 관리콘솔에서 SMTP/IMAP 허용 정책을 확인하세요.

### 운영 흐름
- RM: 로그인 화면 → **회원가입 신청**(이름·소속팀·회사이메일·비밀번호) → 승인 대기.
- 연금컨설팅팀: 메인 상단 **가입승인** 탭(데스크톱, 연금컨설팅팀 전용)에서 승인/반려/삭제.
- 승인된 RM: 로그인 → 회사 이메일로 온 6자리 코드 입력 → 완료(‘이 기기 30일 기억’ 선택 가능).

> 회원 데이터는 `data/members.json`에 저장되어 **GCS 버킷에 영구 보존**됩니다(재배포해도 유지).
> 비밀번호는 해시로만 저장하며 평문·인증코드는 저장하지 않습니다(코드는 해시+10분 만료).

---

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
