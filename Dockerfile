# NICE·KIS 조회는 Playwright(크롬)가 필요 → 크롬이 포함된 공식 이미지 사용
FROM mcr.microsoft.com/playwright/python:v1.49.0-jammy

WORKDIR /app

# 의존성 먼저 설치 (캐시 활용)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 크롬 브라우저 설치 (requirements의 playwright 버전에 맞춰)
RUN playwright install chromium

# 앱 소스 복사
COPY . .

# Cloud Run은 PORT 환경변수(기본 8080)로 들어옴
ENV PORT=8080
EXPOSE 8080

CMD ["python", "app.py"]
