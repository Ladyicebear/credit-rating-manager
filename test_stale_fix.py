# -*- coding: utf-8 -*-
"""_apply_scrape_results stale 정리 로직 검증 (실제 파일 미변경)"""
import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
import app

captured = {}
BASE = {
    'KB손해보험': {'nice': '', 'nice_eval_date': '', 'nice_type': '',
                'kr': 'AA+', 'kr_eval_date': '2026.05.14', 'kr_type': 'IFS',
                'kis': 'AAA', 'kis_eval_date': '', 'kis_type': 'IFS',
                'final': 'AA+', 'scrape_status': '조회성공'},
    '한화손해보험': {'nice': 'AA', 'kr': 'AA', 'kis': 'AA', 'final': 'AA',
                'scrape_status': '조회성공'},
}

def run(label, base, scrape_result):
    app.load_ratings  = lambda: {k: dict(v) for k, v in base.items()}
    app.load_overrides = lambda: {}
    app.save_ratings  = lambda d: captured.update({'r': d})
    app._apply_scrape_results(scrape_result)
    kb = captured['r']['KB손해보험']
    print(f"[{label}] KB손해보험 kis={kb['kis']!r} kis_date={kb['kis_eval_date']!r} "
          f"final={kb['final']!r} status={kb['scrape_status']!r}")
    return kb

# 1) 버그 케이스: KR 성공, KIS는 한화에는 값 주는데 KB손해보험만 빈값 → AAA 제거 기대
print("=== 1. 정상 실행: KIS 살아있음, KB손해보험만 KIS 빈값 (기대: AAA 제거) ===")
kb = run('fix', BASE, {
    'KB손해보험':  {'kr': 'AA+', 'kr_eval_date': '2026.05.14', 'kr_type': 'IFS',
                 'kis': '', 'nice': '', 'scrape_status': '조회성공'},
    '한화손해보험': {'nice': 'AA', 'kr': 'AA', 'kis': 'AA', 'scrape_status': '조회성공'},
})
assert kb['kis'] == '', "AAA가 제거되어야 함"
assert kb['final'] == 'AA+', "final은 AA+ 유지"

# 2) KIS 전면 장애: 모든 기관 KIS 빈값 → AAA 보존 기대
print("\n=== 2. KIS 전면 장애: 모든 기관 KIS 빈값 (기대: AAA 보존) ===")
kb = run('kis-down', BASE, {
    'KB손해보험':  {'kr': 'AA+', 'kr_eval_date': '2026.05.14', 'kr_type': 'IFS',
                 'kis': '', 'nice': '', 'scrape_status': '조회성공'},
    '한화손해보험': {'nice': 'AA', 'kr': 'AA', 'kis': '', 'scrape_status': '조회성공'},
})
assert kb['kis'] == 'AAA', "KIS 전면 장애 시 stale 보존해야 함"

# 3) 기관 전체 실패: KB손해보험 3사 모두 빈값 → 보존 + 재조회실패 기대
print("\n=== 3. 기관 전체 실패: KB손해보험 3사 모두 빈값 (기대: 보존 + 재조회실패) ===")
kb = run('inst-fail', BASE, {
    'KB손해보험':  {'kr': '', 'kis': '', 'nice': '', 'scrape_status': '등급없음'},
    '한화손해보험': {'nice': 'AA', 'kr': 'AA', 'kis': 'AA', 'scrape_status': '조회성공'},
})
assert kb['kis'] == 'AAA', "기관 전체 실패 시 보존"
assert '재조회실패' in kb['scrape_status']

print("\n모든 검증 통과 ✅")
