#!/usr/bin/env python3
"""비밀번호 재설정 (서버에서 직접 실행).

화면의 '비밀번호 변경'은 현재 비밀번호를 알아야 하므로, 분실 시에는 이 스크립트를 쓴다.
서버 셸 접근 권한 자체가 본인 확인 역할을 하므로 웹으로는 열지 않는다.

    python reset_password.py           # 새 비밀번호 입력받아 설정
    python reset_password.py --clear   # 저장된 비밀번호 삭제 → APP_PASSWORD 환경변수로 복귀
    python reset_password.py --show    # 현재 어떤 기준으로 로그인되는지만 확인

비밀번호는 화면에 표시되지 않고 셸 기록에도 남지 않는다(입력값을 인자로 받지 않음).
저장되는 것은 해시뿐이며 평문은 어디에도 기록하지 않는다.
"""
import os
import sys
import json
import getpass
from datetime import datetime

from werkzeug.security import generate_password_hash

# app.py와 같은 위치·형식을 사용한다(app.py의 AUTH_FILE / MIN_PASSWORD_LEN와 맞출 것).
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
AUTH_FILE = os.path.join(BASE_DIR, 'data', 'auth.json')
MIN_PASSWORD_LEN = 8


def _load() -> dict:
    if not os.path.exists(AUTH_FILE):
        return {}
    try:
        with open(AUTH_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        print(f'경고: auth.json을 읽지 못했습니다({e}). 새로 만듭니다.')
        return {}


def show():
    app_user = os.environ.get('APP_USER', 'admin')
    auth = _load()
    print(f'아이디(APP_USER)   : {app_user}')
    if auth.get('password_hash'):
        print(f'비밀번호 기준       : 저장된 비밀번호(auth.json)')
        print(f'마지막 변경        : {auth.get("updated", "-")}')
    else:
        env_set = bool(os.environ.get('APP_PASSWORD'))
        src = 'APP_PASSWORD 환경변수' if env_set else "기본값 'goun'"
        print(f'비밀번호 기준       : {src}  (auth.json 없음)')
    print(f'auth.json 경로     : {AUTH_FILE}')


def clear():
    if not os.path.exists(AUTH_FILE):
        print('저장된 비밀번호가 없습니다. 이미 환경변수 기준입니다.')
        return
    backup = AUTH_FILE + '.bak'
    os.replace(AUTH_FILE, backup)
    print(f'저장된 비밀번호를 삭제했습니다(백업: {backup}).')
    print('이제 APP_PASSWORD 환경변수(미설정 시 기본 goun)로 로그인됩니다.')
    print('서비스를 재시작할 필요는 없습니다.')


def reset():
    print('새 비밀번호를 입력하세요. 입력값은 화면에 표시되지 않습니다.')
    pw1 = getpass.getpass('새 비밀번호        : ')
    if len(pw1) < MIN_PASSWORD_LEN:
        sys.exit(f'중단: 비밀번호는 {MIN_PASSWORD_LEN}자 이상이어야 합니다.')
    pw2 = getpass.getpass('새 비밀번호 확인   : ')
    if pw1 != pw2:
        sys.exit('중단: 두 입력이 일치하지 않습니다.')

    auth = _load()
    auth['password_hash'] = generate_password_hash(pw1)
    auth['updated'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    os.makedirs(os.path.dirname(AUTH_FILE), exist_ok=True)
    with open(AUTH_FILE, 'w', encoding='utf-8') as f:
        json.dump(auth, f, ensure_ascii=False, indent=2)
    try:
        os.chmod(AUTH_FILE, 0o600)      # 같은 서버의 다른 사용자에게 노출되지 않도록
    except OSError:
        pass

    print('\n비밀번호를 변경했습니다.')
    print(f'아이디: {os.environ.get("APP_USER", "admin")}')
    print('바로 로그인할 수 있습니다(서비스 재시작 불필요).')


if __name__ == '__main__':
    arg = sys.argv[1] if len(sys.argv) > 1 else ''
    if arg == '--clear':
        clear()
    elif arg == '--show':
        show()
    elif arg in ('', '--reset'):
        reset()
    else:
        sys.exit(__doc__)
