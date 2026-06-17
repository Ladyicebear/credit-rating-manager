@echo off
chcp 65001 >nul
echo =========================================
echo  신용등급 관리 시스템 시작
echo =========================================
echo.

:: Python 설치 확인
python --version >nul 2>&1
if errorlevel 1 (
    echo [오류] Python이 설치되어 있지 않습니다.
    echo  https://www.python.org 에서 Python 3.10 이상을 설치하세요.
    pause
    exit /b 1
)

:: 가상환경 생성 (없을 경우)
if not exist "venv\" (
    echo [1/3] 가상환경 생성 중...
    python -m venv venv
)

:: 가상환경 활성화
call venv\Scripts\activate.bat

:: 패키지 설치
echo [2/3] 필요 패키지 설치 중...
pip install -r requirements.txt -q

:: 서버 실행
echo [3/3] 서버 시작 중...
echo.
echo  브라우저에서 http://localhost:5000 으로 접속하세요.
echo  종료하려면 이 창에서 Ctrl+C 를 누르세요.
echo.
start "" http://localhost:5000
python app.py

pause
