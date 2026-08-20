# -*- coding: utf-8 -*-
"""큰 ZIP을 HTTP 업로드(용량 제한) 없이 서버에서 직접 분류한다.

사용법 (VM):
  1) 브라우저 SSH의 '파일 업로드' 버튼으로 zip을 ~/credit-rating-manager 에 올린다
  2) cd ~/credit-rating-manager
     python3 classify_zip.py "퇴직연금 원리금보장상품 약관 및 상품설명서_2026년 8월.zip"

  → 웹 「약관·상품설명서」 탭과 동일한 규칙으로 기관별 분류·저장됩니다.
"""
import sys

if len(sys.argv) < 2:
    print('사용법: python3 classify_zip.py <약관및상품설명서.zip>')
    sys.exit(1)

import app  # 웹앱과 동일한 분류 로직 재사용(서버는 실행하지 않음)

with open(sys.argv[1], 'rb') as f:
    data = f.read()

matched, unmatched = app.classify_zip(data)

print('== 등록 %d건 · 미매칭 %d건 ==' % (len(matched), len(unmatched)))
for m in matched:
    print('  OK   ' + m)
if unmatched:
    print('-- 미매칭(기관/종류 인식 실패) --')
    for u in unmatched:
        print('  SKIP ' + u)
