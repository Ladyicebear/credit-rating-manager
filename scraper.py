"""
신용평가 3사 스크래핑 모듈

- NICE: Playwright 헤드리스 브라우저 (회사 상세 페이지 파싱)
- KR  : requests POST → 등급공시 JSON API (배치 조회)
- KIS : Playwright 헤드리스 브라우저 (검색 → 등급 파싱)
"""

import re
import logging
import threading
import datetime
import urllib.parse
import requests as _requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from concurrent.futures import TimeoutError as FuturesTimeout
from playwright.sync_api import sync_playwright

logger = logging.getLogger(__name__)

TIMEOUT_MS        = 20_000   # Playwright 타임아웃 (ms) — KIS 등
NICE_TIMEOUT_MS   = 50_000   # NICE 서버 응답이 느려 별도 타임아웃 (17s+)
# 평가사 1곳당 하드 타임아웃(초). Playwright 타임아웃이 안 걸리는 지점에서 멈춰도
# 이 시간이 지나면 포기하고 다음으로 넘어간다. NICE 재시도(검색어 변형)를 고려해 넉넉히.
AGENCY_TIMEOUT_SEC = 180
TIMEOUT_S    = 30       # requests 타임아웃 (s)
INSURANCE_CATEGORIES = {'손해보험', '생명보험'}
SAVING_BANK_CATEGORIES = {'저축은행'}
# NICE 등급 우선순위 (비보험): 기업신용등급(ICR) → 채권(회사채선순위)
# KR·KIS도 동일 규칙 적용
# 저축은행 추가 규칙(2026-06-02 user_instructions [8]):
#   - 우선순위는 다른 비보험사와 동일 (ICR → 회사채선순위)
#   - 등급공시일이 오늘 기준 정확히 2년을 초과하면 미공시 처리

# 양쪽 경계: 한글 뒤에도 매칭되도록 \w 대신 [A-Za-z\d] 사용
# 예) "등급AA+" → 급(한글)은 \w지만 [A-Za-z\d]가 아님 → 정상 매칭
# '0' 접미사는 한국 신용평가 기관이 무수정(기준) 등급을 AA0, A0 등으로 표기할 때 사용
RATING_RE = re.compile(
    r'(?<![A-Za-z\d])(AAA|AA[+\-0]?|A[+\-0]?|BBB[+\-0]?|BB[+\-0]?|B[+\-0]?|CCC[+\-0]?|CC|C|D)(?![A-Za-z\d])'
)


def _norm_rating(raw: str) -> str:
    """AA0 → AA: '0' 접미사를 제거해 표준 등급명으로 정규화"""
    return raw[:-1] if raw.endswith('0') and len(raw) > 1 else raw
DATE_RE = re.compile(r'(\d{4})[.\-](\d{2})[.\-](\d{2})')

_RATING_SCALE = [
    'AAA', 'AA+', 'AA', 'AA-', 'A+', 'A', 'A-',
    'BBB+', 'BBB', 'BBB-', 'BB+', 'BB', 'BB-',
    'B+', 'B', 'B-', 'CCC+', 'CCC', 'CCC-', 'CC', 'C', 'D',
]

_CHROMIUM_ARGS = ['--no-sandbox', '--disable-dev-shm-usage']
_PAGE_HEADERS = {
    'Accept-Language': 'ko-KR,ko;q=0.9,en-US;q=0.8',
    'Accept': 'text/html,application/xhtml+xml,*/*;q=0.8',
}


def _normalize_date(text: str) -> str:
    m = DATE_RE.search(text)
    return f"{m.group(1)}.{m.group(2)}.{m.group(3)}" if m else ''


def _norm_name(name: str) -> str:
    """회사명 정규화: (주)/주식회사 제거 → 특수문자/공백 제거 → 소문자"""
    name = re.sub(r'\(주\)|㈜|주식회사', '', name)
    name = re.sub(r'[() ·　\s]', '', name)
    return name.lower()


# 영문 접두사 → 한글 변환 (KR 회사명이 한글 표기를 쓸 때 매칭)
_EN_TO_KO: list[tuple[str, str]] = [
    ('nh',  '엔에이치'),
    ('kb',  '케이비'),
    ('bnk', '비엔케이'),
    ('db',  '디비'),
    ('sk',  '에스케이'),
    ('ktb', '케이티비'),
    ('kdb', '케이디비'),
    ('sc',  '에스씨'),
    ('sbi', '에스비아이'),
    ('ok',  '오케이'),
    ('mg',  '엠지'),
    ('abl', '에이비엘'),
    ('aia', '에이아이에이'),
    ('im',  '아이엠'),
    ('bnpp', '비엔피파리바'),
]

# 회사명 변경 이력 / 법인 공식명: _norm_name(구명) → 검색 시 사용할 공식명
# 3사(NICE/KR/KIS) 모두에서 `_resolve_company_name()`을 통해 자동 변환
_COMP_ALIASES: dict[str, str] = {
    '대구은행':   '아이엠뱅크',             # DGB대구은행 → iM뱅크 (2023년 사명변경)
    'sc제일은행': '한국스탠다드차타드은행',   # SC제일은행 법인 공식명
    '수협':       '수협은행',               # KR/KIS 공시상 법인명은 '수협은행'
}


def _resolve_company_name(company: str) -> str:
    """별칭 적용: 검색 시 공식 법인명으로 변환
    예) 'SC제일은행' → '한국스탠다드차타드은행', '대구은행' → '아이엠뱅크'
    NICE/KR/KIS 모든 스크래퍼에서 진입 시 호출하여 일관된 검색어 사용
    """
    norm = _norm_name(company)
    alias = _COMP_ALIASES.get(norm)
    return alias if alias else company


# 산업 카테고리 일반어: 영문 접두사 제거 후 이 단어만 남으면 alt에 포함하지 않음
# 예) 'SBI저축은행' → 'sbi' 제거 → '저축은행' (단독)이면 다른 *저축은행과 오탐
#     'NH투자증권' → 'nh' 제거 → '투자증권' (단독)이면 다른 *투자증권과 오탐
_INDUSTRY_TERMS: frozenset = frozenset({
    '저축은행', '투자증권', '금융투자', '생명보험', '손해보험',
    '캐피탈', '카드',
})


def _alt_norms(norm: str) -> list[str]:
    """영문 접두사 변환 대안 목록: 한글로 치환 + 영문 접두사 완전 제거"""
    alts = []
    for en, ko in _EN_TO_KO:
        if norm.startswith(en):
            alts.append(ko + norm[len(en):])   # 예: nh투자증권 → 엔에이치투자증권
            stripped = norm[len(en):]
            # 산업 카테고리만 남는 경우 제외 (오탐 방지) — 예: 'sbi저축은행' → '저축은행'
            if len(stripped) >= 3 and stripped not in _INDUSTRY_TERMS:
                alts.append(stripped)           # 예: kb국민은행 → 국민은행
    return alts


def _cell_matches_name(cell_norm: str, alts: list[str]) -> bool:
    """검색결과 행 이름(cell_norm)이 회사 별칭(alts) 중 하나와 안전하게 일치하는지.

    ⚠️ 단순 양방향 부분일치(a in cell or cell in a)는 접두사 제거 별칭이
       다른 브랜드와 오탐을 일으킴.
       예) 'KB라이프생명보험' → 접두사 제거 별칭 '라이프생명보험'이
           '신한라이프생명보험'에 부분 포함되어 신한라이프 AAA를 잘못 반환.
    → 별칭이 셀 이름의 '접두부'일 때만 허용한다. 다른 브랜드 접두사(예: '신한')가
      별칭 앞에 붙어 있으면 거부하여 오탐을 차단한다.
    정상 매칭은 유지:
      · 완전 일치           (cell == alt)
      · NICE가 접두사 생략   (cell in alt, 셀이 더 짧음)
      · 정식 긴 이름의 접두부 (cell.startswith(alt), 예: '삼성화재'→'삼성화재해상보험')
    """
    for a in alts:
        if not a:
            continue
        if cell_norm == a or cell_norm in a or cell_norm.startswith(a):
            return True
    return False


def _search_variants(name: str) -> list[str]:
    """검색어 변형 목록 생성 (NICE/KIS 검색 재시도용)"""
    variants = [name]
    # (주) 접두사 변형: 한국스탠다드차타드은행 → (주)한국스탠다드차타드은행
    # NICE/KR이 법인 공식명 표기를 사용할 때 매칭률 향상
    if not name.startswith('(주)') and '주식회사' not in name and '㈜' not in name:
        joo_variant = f'(주){name}'
        if joo_variant not in variants:
            variants.append(joo_variant)
    norm = _norm_name(name)
    # 영문 접두사 처리: KB국민은행 → 국민은행, (주)국민은행, 케이비국민은행
    for en, ko in _EN_TO_KO:
        if norm.startswith(en):
            stripped_name = name[len(en):]   # 원본 케이스 유지
            stripped_norm = _norm_name(stripped_name)
            # 산업 카테고리만 남으면 NICE/KIS에서도 다른 회사로 오탐 가능 → 변형 추가 안 함
            stripped_is_industry = stripped_norm in _INDUSTRY_TERMS
            if len(stripped_name) >= 3 and stripped_name not in variants and not stripped_is_industry:
                variants.append(stripped_name)
            if not stripped_is_industry:
                joo = f'(주){stripped_name}'
                if joo not in variants:
                    variants.append(joo)
            # 한글 접두사 대체: ABL생명 → 에이비엘생명
            ko_name = ko + stripped_name
            if len(ko_name) >= 3 and ko_name not in variants:
                variants.append(ko_name)
    # 보험사 긴 이름 단축: 삼성화재해상보험 → 삼성화재
    for suffix in ['해상보험', '생명보험', '손해보험']:
        if suffix in name:
            shorter = name[:name.index(suffix)]
            if len(shorter) >= 2 and shorter not in variants:
                variants.append(shorter)
    return variants


# ─── 나이스신용평가 ───────────────────────────────────────────────────

def scrape_nice(company: str, is_insurance: bool) -> tuple[str, str, str]:
    # 등급 우선순위 (비보험): 기업신용등급(ICR) → 채권(회사채선순위)
    # SC제일은행 → 한국스탠다드차타드은행 등 별칭 자동 적용
    company = _resolve_company_name(company)
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, args=_CHROMIUM_ARGS)
            page = browser.new_page()
            page.set_extra_http_headers(_PAGE_HEADERS)
            try:
                table_result = ('', '', '')
                for search_term in _search_variants(company):
                    encoded = urllib.parse.quote(search_term)
                    url = f'https://www.nicerating.com/search/search.do?mainSType=CMP&mainSText={encoded}'
                    page.goto(url, timeout=NICE_TIMEOUT_MS, wait_until='domcontentloaded')
                    page.wait_for_timeout(1000)

                    if 'companyGradeInfo' not in page.url:
                        table_result = _nice_parse_search_table(page, company, is_insurance)

                        # 검색 결과 테이블에서 회사명이 정확히 매칭된 행을 찾았다면
                        # 추가 클릭(첫 번째 '기업상세' 링크)은 다른 회사로 이동할 위험이 있어 즉시 반환
                        # 예) 'NH투자증권' 검색 결과의 첫 행이 BNK투자증권이면
                        #     첫 '기업상세' 클릭 시 BNK 페이지로 이동해 A+를 잘못 반환
                        if table_result and table_result[0]:
                            return table_result

                        # 검색 결과 행 중 회사명이 매칭되는 행의 '기업상세' 링크만 선택
                        detail = _nice_pick_detail_link(page, company)
                        if detail is not None:
                            detail.click()
                            page.wait_for_load_state('domcontentloaded', timeout=NICE_TIMEOUT_MS)
                            page.wait_for_timeout(1000)

                    if 'companyGradeInfo' in page.url:
                        result = _nice_parse_detail(page, is_insurance)
                        if result and result[0]:
                            return result

                    if table_result and table_result[0]:
                        return table_result

                return '', '', ''
            finally:
                page.close()
                browser.close()
    except Exception as e:
        logger.debug('[NICE] %s: %s', company, e)
        return '', '', ''


def _nice_pick_detail_link(page, company: str):
    """검색결과 테이블에서 회사명이 매칭되는 행의 '기업상세' 링크를 반환.
    매칭되는 행이 없으면 None.

    ⚠️ 첫 번째 '기업상세'를 무조건 클릭하면 'NH투자증권' 검색 시
    BNK투자증권 등 다른 '*투자증권' 첫 행으로 이동해 잘못된 등급을 반환하는 버그가 있었음.
    """
    needle = _norm_name(company)
    alts = [needle] + _alt_norms(needle)

    for tbl in page.locator('table').all():
        rows = tbl.locator('tr').all()
        if len(rows) < 2:
            continue
        for row in rows[1:]:
            cells = row.locator('td').all()
            if not cells:
                continue
            cell0 = cells[0].inner_text().strip()
            if not cell0:
                continue
            cell_norm = _norm_name(cell0)
            if not _cell_matches_name(cell_norm, alts):
                continue
            # 해당 행 내부의 '기업상세' 링크 탐색
            detail = row.locator('a:has-text("기업상세")').first
            if detail.count() > 0:
                return detail
    return None


def _nice_parse_search_table(page, company: str, is_insurance: bool) -> tuple[str, str, str]:
    """NICE 검색결과 테이블에서 직접 등급 추출
    컬럼: 0=기업명, 1=업종, 2=계열, 3=채권, 4=기업어음, 5=전자단기사채,
          6=기업신용평가(ICR), 7=보험금지급능력(IFS), 8=자산유동화, 9=상세
    등급 우선순위: ICR > 회사채(선순위) / 후순위는 반환하지 않음
    """
    needle = _norm_name(company)
    for tbl in page.locator('table').all():
        rows = tbl.locator('tr').all()
        if len(rows) < 2:
            continue
        hdr = ' '.join(c.inner_text().strip() for c in rows[0].locator('th,td').all())
        if '채권' not in hdr:
            continue
        for row in rows[1:]:
            cells = [c.inner_text().strip() for c in row.locator('td').all()]
            if not cells or len(cells) < 4:
                continue
            cell_norm = _norm_name(cells[0])
            if needle not in cell_norm and cell_norm not in needle:
                continue
            n = len(cells)
            def _cell(i): return cells[i] if i < n else ''
            if is_insurance:
                m = RATING_RE.search(_cell(7))
                if m:
                    return _norm_rating(m.group(0)), '', 'IFS'
            else:
                # ICR 우선, 없으면 회사채(선순위) — 후순위는 반환하지 않음
                m = RATING_RE.search(_cell(6))
                if m:
                    return _norm_rating(m.group(0)), '', 'ICR'
                # 채권 컬럼(3) 헤더가 후순위이면 건너뜀
                hdr_cells = [c.inner_text().strip() for c in rows[0].locator('th,td').all()]
                sb_hdr = hdr_cells[3] if len(hdr_cells) > 3 else ''
                if '후순위' not in sb_hdr:
                    m = RATING_RE.search(_cell(3))
                    if m:
                        return _norm_rating(m.group(0)), '', '회사채선순위'
    return '', '', ''


def _nice_parse_detail(page, is_insurance: bool) -> tuple[str, str, str]:
    """NICE 기업상세(companyGradeInfo) 페이지 - DOM 테이블 + 텍스트 파싱
    등급 우선순위: ICR > 회사채(선순위) / 후순위는 반환하지 않음
    """
    # 1차: 상단 기업개요 요약 테이블 파싱 (컬럼: 채권/기업신용등급/보험지급능력)
    result, table_found = _nice_summary_table(page, is_insurance)
    if result[0]:
        return result
    # 요약 테이블을 성공적으로 파싱했으나 등급이 비어 있는 경우:
    # 해당 컬럼에 등급이 없다는 것이 확인된 것이므로 텍스트 폴백 생략
    if table_found:
        return '', '', ''

    # 요약 테이블 자체를 찾지 못한 경우에만 텍스트 키워드 파싱 폴백 사용
    text = page.inner_text('body')
    # '주요 등급내역' 이후는 등급 히스토리 섹션 → 현재 등급이 아닌 과거 이력이 포함됨
    # 해당 섹션을 검색 범위에서 제외하여 히스토리 항목(A+, 2026.04.17 등)을 현재 등급으로
    # 잘못 반환하는 오류 방지
    for _cutoff_kw in ('주요 등급내역', '등급변동 이력', '평정 이력'):
        _cutoff = text.find(_cutoff_kw)
        if _cutoff > 0:
            text = text[:_cutoff]
            break
    if is_insurance:
        for kw, rtype in [('보험지급능력', 'IFS'), ('IFS', 'IFS')]:
            r = _nice_section(text, kw, rtype)
            if r and r[0]:
                return r
    else:
        # ICR 우선, 없으면 회사채(선순위) — 전 기관 동일 규칙
        # 후순위 컨텍스트는 건너뜀 → 후순위만 있으면 미공시 처리
        for kw, rtype in [('기업신용등급', 'ICR'), ('ICR', 'ICR')]:
            r = _nice_section(text, kw, rtype)
            if r and r[0]:
                return r
        for kw, rtype in [('회사채', '회사채선순위')]:
            r = _nice_section(text, kw, rtype, exclude='후순위')
            if r and r[0]:
                return r
    return '', '', ''


def _nice_summary_table(page, is_insurance: bool) -> tuple[tuple[str, str, str], bool]:
    """NICE 기업개요 상단 요약 테이블에서 등급 추출
    컬럼 순서: 채권(0) | 기업어음(1) | 전자단기사채(2) | 기업신용등급(3) | 보험지급능력(4)
    각 컬럼은 '등급' + '확정일' 두 td → 실제 td 인덱스 = 컬럼번호 * 2

    반환값: (결과 tuple, 테이블_발견_여부)
    - 테이블_발견_여부=True : 요약테이블을 파싱했음 (등급이 비어있어도 권위 있는 결과)
    - 테이블_발견_여부=False: 요약테이블 자체를 찾지 못함 → 텍스트 폴백 허용
    """
    for tbl in page.locator('table').all():
        rows = tbl.locator('tr').all()
        if len(rows) < 3:
            continue

        # 1행: 컬럼 헤더 (th 또는 td)
        header_texts = [c.inner_text().strip().replace('\n', '').replace(' ', '')
                        for c in rows[0].locator('th,td').all()]
        # '채권'이 헤더 텍스트 중 하나에 포함되는지 확인 (정확 매칭 아닌 부분 포함 검사)
        # 예) '채권', '채권(선순위)', '채권선순위' 모두 매칭
        if not any('채권' in h for h in header_texts):
            continue

        # 실제 td 인덱스 계산 (colspan 무시, 헤더 순서 기준)
        # 데이터 행: tr 중 td가 있는 행 (헤더 제외)
        data_trs = [r for r in rows if r.locator('td').count() >= 4]
        if not data_trs:
            continue
        data_row = data_trs[-1]  # 마지막 데이터 행
        tds = data_row.locator('td').all()
        td_texts = [td.inner_text().strip() for td in tds]

        if is_insurance:
            priorities = [('보험지급능력', 'IFS', 4)]
        else:
            # ICR 우선, 없으면 회사채(선순위) — 전 기관 동일 규칙
            # 채권 컬럼 인덱스는 헤더에서 동적 탐색: '채권' 포함 AND '후순위' 미포함인 컬럼만 사용
            sb_col_idx = None
            for hi, h in enumerate(header_texts):
                if '채권' in h and '후순위' not in h:
                    sb_col_idx = hi
                    break
            priorities = [('기업신용등급', 'ICR', 3)]
            if sb_col_idx is not None:
                priorities.append(('채권', '회사채선순위', sb_col_idx))

        for col_name, rtype, col_idx in priorities:
            ri = col_idx * 2      # 등급 td index
            di = col_idx * 2 + 1  # 확정일 td index
            if ri < len(td_texts):
                m = RATING_RE.search(td_texts[ri])
                if m:
                    date_str = td_texts[di] if di < len(td_texts) else ''
                    d = DATE_RE.search(date_str)
                    return (_norm_rating(m.group(0)), _normalize_date(date_str) if d else '', rtype), True

        # 테이블은 찾았으나 해당 등급 컬럼이 모두 비어 있음 → 권위 있는 '없음' 결과
        return ('', '', ''), True

    return ('', '', ''), False


def _nice_section(text: str, keyword: str, rtype: str = '', exclude: str = ''):
    """NICE 상세 페이지 텍스트에서 keyword 뒤 첫 등급 반환.
    exclude가 지정된 경우, keyword 바로 뒤에 exclude 문자열이 이어지는 위치는 건너뜀.
    예) keyword='회사채', exclude='후순위' → '회사채(후순위)' 섹션을 스킵하고 선순위만 반환.
    """
    start = 0
    while True:
        idx = text.find(keyword, start)
        if idx < 0:
            return None
        # keyword 바로 뒤 짧은 구간에 exclude 문자열이 있으면 해당 위치 건너뜀
        if exclude and exclude in text[idx: idx + len(keyword) + len(exclude) + 2]:
            start = idx + len(keyword)
            continue
        snippet = text[idx: idx + 400]
        m_r = RATING_RE.search(snippet)
        if not m_r:
            return None
        m_d = DATE_RE.search(snippet)
        if not rtype:
            rtype = ('ICR' if keyword in ('ICR', '기업신용등급')
                     else 'IFS' if keyword in ('IFS', '보험지급능력', '보험지급')
                     else '회사채선순위')
        return _norm_rating(m_r.group(0)), _normalize_date(snippet) if m_d else '', rtype


# ─── 한국기업평가 (KR) ───────────────────────────────────────────────
# 등급공시 JSON API 사용 (배치 조회)

_KR_CACHE: dict = {}   # { normalized_name: {'name': str, 'SB': (rating, date)|None, 'ICR': ..., 'IFS': ...} }
_KR_CACHE_LOCK = threading.Lock()
_KR_CACHE_LOADED = False

_KR_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'Accept-Language': 'ko-KR,ko;q=0.9',
    'X-Requested-With': 'XMLHttpRequest',
    'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
    'Referer': 'https://www.korearatings.com/',
}

_STRIP_RE = re.compile(r'[↑↓▲▼\s]')


def _load_kr_cache():
    """KR 등급공시 API 일괄 조회 → 캐시 적재"""
    global _KR_CACHE_LOADED
    today = datetime.date.today().strftime('%Y-%m-%d')
    two_years_ago = (datetime.date.today() - datetime.timedelta(days=730)).strftime('%Y-%m-%d')

    post_data = {
        'MENU_ID': '360', 'CONTENTS_NO': '1', 'SITE_NO': '2',
        'COMP_CD': '', 'STDT': two_years_ago, 'ENDT': today,
        'checkAll': 'Y',
        'SVCTY_CD': ['01', '07', '02', '03', '10', '11', '05', '09', '04'],
    }

    try:
        r = _requests.post(
            'https://www.korearatings.com/ajaxf/frDisclosureSvc/getRatingDisclosureList.do',
            headers=_KR_HEADERS, data=post_data, timeout=60,
        )
        r.raise_for_status()
        data = r.json().get('data', {})

        tmp: dict = {}

        def _store(comp_nm: str, rtype: str, grd_raw: str, eval_dt: str):
            grd = _STRIP_RE.sub('', grd_raw)
            m = RATING_RE.search(grd)
            if not m:
                return
            rating = _norm_rating(m.group(0))
            norm = _norm_name(comp_nm)
            if not norm:
                return
            if norm not in tmp:
                tmp[norm] = {'name': comp_nm, 'SB': None, 'ICR': None, 'IFS': None}
            cur = tmp[norm][rtype]
            if cur is None or eval_dt > cur[1]:
                tmp[norm][rtype] = (rating, eval_dt)
            elif eval_dt == cur[1]:
                # 같은 날짜에 여러 채권 항목 존재 시 더 높은 등급(선순위) 유지
                try:
                    if _RATING_SCALE.index(rating) < _RATING_SCALE.index(cur[0]):
                        tmp[norm][rtype] = (rating, eval_dt)
                except ValueError:
                    pass

        # data32 / data31: 회사채 (SB) — GRD_NM 필드
        # ① 후순위채(FB(Sub), 후순위 포함 명칭) 제외
        # ② 담보부증권(HB), 해외채 등 비일반채 제외 → 선순위 회사채(FB)·보증채(GB)만 반영
        #    HB(채권담보부증권)는 발행사 신용이 아닌 담보 풀 신용을 반영하므로 SB 등급 산출 제외
        for key in ('data32', 'data31'):
            for item in data.get(key, {}).get('Data', []):
                comp_nm      = item.get('COMP_NM', '') or ''
                grd_nm       = item.get('GRD_NM', '') or ''
                eval_dt      = item.get('EVAL_DT', '') or ''
                bond_kind_en = item.get('BOND_KIND_ENNM', '') or ''
                bond_kind_kr = item.get('BOND_KIND_KRNM', '') or ''
                if 'Sub' in bond_kind_en or '후순위' in bond_kind_kr:
                    continue
                # FB(일반 회사채)·GB(보증사채) 이외 채권 종류는 제외
                if bond_kind_en and bond_kind_en not in ('FB', 'GB'):
                    continue
                _store(comp_nm, 'SB', grd_nm, eval_dt)

        # data34: ICR (기업신용등급) — CUR_GRD_NM_ORG 필드
        for item in data.get('data34', {}).get('Data', []):
            comp_nm = item.get('COMP_NM', '') or ''
            grd     = (item.get('CUR_GRD_NM_ORG') or item.get('GRD') or item.get('GRD_NM') or '').strip()
            eval_dt = item.get('EVAL_DT', '') or ''
            _store(comp_nm, 'ICR', grd, eval_dt)

        # data35: IFS (보험금지급능력) — CUR_GRD_NM_ORG 필드
        for item in data.get('data35', {}).get('Data', []):
            comp_nm = item.get('COMP_NM', '') or ''
            grd     = (item.get('CUR_GRD_NM_ORG') or item.get('GRD') or item.get('GRD_NM') or '').strip()
            eval_dt = item.get('EVAL_DT', '') or ''
            _store(comp_nm, 'IFS', grd, eval_dt)

        with _KR_CACHE_LOCK:
            _KR_CACHE.clear()
            _KR_CACHE.update(tmp)
            _KR_CACHE_LOADED = True
        logger.info('[KR] 캐시 적재 완료: %d개 회사', len(tmp))

    except Exception as e:
        logger.warning('[KR] 캐시 적재 실패: %s', e)
        with _KR_CACHE_LOCK:
            _KR_CACHE_LOADED = True  # 실패해도 flag 설정 (무한 재시도 방지)


def _kr_lookup(norm: str) -> dict | None:
    """정규화된 이름으로 KR 캐시 검색 (정확 → 영문↔한글 변환 → 부분)"""
    entry = _KR_CACHE.get(norm)
    if entry:
        return entry

    alts = _alt_norms(norm)

    # 영문 접두사 → 한글 변환 시도 (정확 매칭)
    for alt in alts:
        entry = _KR_CACHE.get(alt)
        if entry:
            return entry

    # 부분 매칭: norm 및 alt_norms 모두 시도 (역방향 제외 → 오탐 방지)
    # 여러 매칭 시 가장 짧은 키(가장 구체적) 선택
    candidates = [norm] + alts
    for candidate in candidates:
        if len(candidate) < 4:
            continue
        best_key = None
        best_len = float('inf')
        with _KR_CACHE_LOCK:
            for key in _KR_CACHE:
                if candidate in key and len(key) < best_len:
                    best_key = key
                    best_len = len(key)
        if best_key:
            return _KR_CACHE.get(best_key)

    return None


def scrape_kr(company: str, is_insurance: bool) -> tuple[str, str, str]:
    global _KR_CACHE_LOADED
    if not _KR_CACHE_LOADED:
        _load_kr_cache()

    # SC제일은행 → 한국스탠다드차타드은행 등 별칭 자동 적용
    company = _resolve_company_name(company)
    norm  = _norm_name(company)
    entry = _kr_lookup(norm)

    if entry is None:
        return '', '', ''

    if is_insurance:
        ifs = entry.get('IFS')
        if ifs:
            return ifs[0], ifs[1], 'IFS'
        return '', '', ''

    icr = entry.get('ICR')
    if icr:
        return icr[0], icr[1], 'ICR'
    sb = entry.get('SB')
    if sb:
        return sb[0], sb[1], '회사채선순위'
    return '', '', ''


# ─── 한국신용평가 (KIS) ──────────────────────────────────────────────

_KIS_OVERVIEW_BASE = 'https://www.kisrating.com/ratingsSearch/corp_overview.do'
_KIS_SEARCH_URL    = 'https://www.kisrating.com/ratingsSearch/corp_search.do'


def _kis_presearch_in_browser(page, search_term: str) -> dict | None:
    """Playwright 컨텍스트 안에서 fetch로 presearch 호출 (Cloudflare 세션 공유)"""
    try:
        return page.evaluate(
            """async (kw) => {
                const resp = await fetch('/ratingsSearch/corp_presearch.json', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/x-www-form-urlencoded'},
                    body: 'searchType=1&searchKeyword=' + encodeURIComponent(kw)
                });
                return await resp.json();
            }""",
            search_term,
        )
    except Exception as e:
        logger.debug('[KIS presearch] %s: %s', search_term, e)
        return None


def scrape_kis(company: str, is_insurance: bool) -> tuple[str, str, str]:
    # SC제일은행 → 한국스탠다드차타드은행 등 별칭 자동 적용
    company = _resolve_company_name(company)
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, args=_CHROMIUM_ARGS)
            page = browser.new_page()
            page.set_extra_http_headers(_PAGE_HEADERS)
            try:
                # 검색 페이지 먼저 로드 → Cloudflare 세션 쿠키 획득
                page.goto(_KIS_SEARCH_URL, timeout=TIMEOUT_MS)
                page.wait_for_load_state('networkidle', timeout=TIMEOUT_MS)

                needle = _norm_name(company)
                for search_term in _search_variants(company):
                    presearch = _kis_presearch_in_browser(page, search_term)

                    if presearch and presearch.get('message') == 'success':
                        if presearch.get('isRedirect'):
                            # 단일 결과: kiscd로 overview 직접 이동
                            data_list = presearch.get('dataList1') or []
                            if not data_list and presearch.get('kiscd'):
                                data_list = [{'kiscd': presearch['kiscd'],
                                              'upcheOpt': presearch.get('upcheOpt', '')}]

                            alts = [needle] + _alt_norms(needle)
                            candidates = [
                                item['kiscd'] for item in data_list
                                if any(a in _norm_name(item.get('upcheOpt', ''))
                                       or _norm_name(item.get('upcheOpt', '')) in a
                                       for a in alts if a)
                            ]
                            if not candidates:
                                candidates = [item['kiscd'] for item in data_list]

                            for kiscd in candidates:
                                url = f'{_KIS_OVERVIEW_BASE}?kiscd={kiscd}'
                                page.goto(url, timeout=NICE_TIMEOUT_MS, wait_until='domcontentloaded')
                                page.wait_for_timeout(1000)
                                result = _kis_overview(page, is_insurance)
                                if result[0]:
                                    return result
                                page.goto(_KIS_SEARCH_URL, timeout=TIMEOUT_MS)
                                page.wait_for_load_state('networkidle', timeout=TIMEOUT_MS)
                        else:
                            # 복수 결과: 폼 action을 corp_search.do로 설정 후 submit
                            with page.expect_navigation(timeout=TIMEOUT_MS):
                                page.evaluate(
                                    """(kw) => {
                                        document.querySelector('#searchKeyword').value = kw;
                                        var frm = document.querySelector('#frm');
                                        frm.action = '/ratingsSearch/corp_search.do';
                                        frm.submit();
                                    }""",
                                    search_term,
                                )
                            page.wait_for_load_state('networkidle', timeout=TIMEOUT_MS)
                            result = _kis_table(page, company, is_insurance)
                            if result[0]:
                                return result
                            page.goto(_KIS_SEARCH_URL, timeout=TIMEOUT_MS)
                            page.wait_for_load_state('networkidle', timeout=TIMEOUT_MS)
                    else:
                        # presearch 실패 → 기존 방식 fallback
                        page.locator('#searchKeyword').fill(search_term)
                        page.locator('#btnSearch').click()
                        try:
                            page.wait_for_url('**/corp_overview.do**', timeout=8000)
                        except Exception:
                            pass
                        page.wait_for_timeout(3000)
                        if 'corp_overview.do' in page.url:
                            result = _kis_overview(page, is_insurance)
                            if result[0]:
                                return result
                        else:
                            result = _kis_table(page, company, is_insurance)
                            if result[0]:
                                return result
                        page.goto(_KIS_SEARCH_URL, timeout=TIMEOUT_MS)
                        page.wait_for_load_state('networkidle', timeout=TIMEOUT_MS)

                return '', '', ''
            finally:
                page.close()
                browser.close()
    except Exception as e:
        logger.debug('[KIS] %s: %s', company, e)
        return '', '', ''


def _kis_overview(page, is_insurance: bool) -> tuple[str, str, str]:
    """corp_overview.do 텍스트에서 등급 추출
    KIS 상세 페이지 구조:
      회사채(선순위)\n등급AA\nOutlook/Watchlist...\n평가일 YYYY.MM.DD
      Issuer Rating\n등급AAA\nOutlook/Watchlist...\n평가일 YYYY.MM.DD
      보험금지급능력\n등급AAA\nOutlook...
    """
    text = page.inner_text('body')

    if is_insurance:
        # 보험금지급능력 섹션 (등급 바로 뒤에 위치)
        for sec in ['보험금지급능력', 'IFS', '보험금지급']:
            r = _kis_snippet(text, sec, 'IFS')
            if r[0]:
                return r
    else:
        # KIS 비보험 우선순위: Issuer Rating(ICR) 1순위 → 회사채(선순위) 2순위 (2026-06-01 지시)
        # 전 기관 동일 규칙 (user_instructions.md [1], [7])
        # Issuer Rating: 첫 번째는 네비게이션, 두 번째가 실제 등급 섹션
        idx = text.find('Issuer Rating')
        if idx >= 0:
            idx2 = text.find('Issuer Rating', idx + 1)
            search_from = idx2 if idx2 >= 0 else idx
            snippet = text[search_from: search_from + 200]
            m_r = RATING_RE.search(snippet)
            if m_r:
                m_d = DATE_RE.search(snippet)
                return _norm_rating(m_r.group(0)), _normalize_date(snippet) if m_d else '', 'ICR'
        # 일반 ICR 텍스트
        for sec in ['ICR']:
            r = _kis_snippet(text, sec, 'ICR')
            if r[0]:
                return r
        # 2순위: 회사채(선순위)
        for sec in ['회사채(선순위)']:
            r = _kis_snippet(text, sec, '회사채선순위')
            if r[0]:
                return r

    return '', '', ''


def _kis_snippet(text: str, keyword: str, rtype: str) -> tuple[str, str, str]:
    """keyword 이후 200자 안에서 등급 찾기. 내비게이션 오탐 방지를 위해 모든 occurrence 시도."""
    idx = 0
    while True:
        idx = text.find(keyword, idx)
        if idx < 0:
            return '', '', ''
        snippet = text[idx: idx + 200]
        m_r = RATING_RE.search(snippet)
        if m_r:
            m_d = DATE_RE.search(snippet)
            return _norm_rating(m_r.group(0)), _normalize_date(snippet) if m_d else '', rtype
        idx += len(keyword)


def _kis_table(page, company: str, is_insurance: bool) -> tuple[str, str, str]:
    """corp_search.do 결과 테이블에서 등급 추출.
    헤더 텍스트로 컬럼 인덱스를 동적으로 찾아 사용한다.
    """
    needle = _norm_name(company)

    for tbl in page.locator('table').all():
        rows = tbl.locator('tr').all()
        if len(rows) < 2:
            continue

        hdr_cells = [c.inner_text().strip().replace('\n', '').replace(' ', '')
                     for c in rows[0].locator('th, td').all()]
        if '회사채' not in ' '.join(hdr_cells):
            continue

        # 헤더에서 컬럼 인덱스 탐색
        def _col(keyword: str) -> int:
            for i, h in enumerate(hdr_cells):
                if keyword in h:
                    return i
            return -1

        col_sb  = _col('회사채(선순위)')   # SB
        col_icr = _col('IssuerRating')       # ICR
        col_ifs = _col('보험금지급')          # IFS

        for row in rows[1:]:
            cells = [c.inner_text().strip() for c in row.locator('td').all()]
            if not cells or not cells[0].strip():
                continue
            cell_norm = _norm_name(cells[0])
            alts = [needle] + _alt_norms(needle)
            if not any(a in cell_norm or cell_norm in a for a in alts if a):
                continue

            n = len(cells)
            def _cell(i): return cells[i] if 0 <= i < n else ''

            if is_insurance:
                if col_ifs >= 0:
                    m = RATING_RE.search(_cell(col_ifs))
                    if m:
                        return _norm_rating(m.group(0)), '', 'IFS'
                continue  # 보험사는 IFS 없으면 해당 행 무시
            if col_icr >= 0:
                m = RATING_RE.search(_cell(col_icr))
                if m:
                    return _norm_rating(m.group(0)), '', 'ICR'
            if col_sb >= 0:
                m = RATING_RE.search(_cell(col_sb))
                if m:
                    return _norm_rating(m.group(0)), '', '회사채선순위'

    return '', '', ''


# ─── 2년 룰 ────────────────────────────────────────────────────────
# user_instructions.md [8](2026-06-02) 저축은행 → [10](2026-07-10) 전 기관으로 확장:
# 등급공시일이 오늘 기준 2년을 초과하면 미공시 처리 (전 카테고리 공통)

def _apply_2y_filter(r: str, d: str, t: str) -> tuple[str, str, str]:
    """등급 결과에 2년 룰 적용 (전 기관 공통).
    - 등급이 비어있거나 평정일이 없으면 그대로 반환 (필터링 안 함)
    - 평정일이 오늘 기준 정확히 2년 전 동일일자 미만이면 ('', '', '') 반환
    예) 오늘이 2026-07-10 → 2024-07-10 미만 평정일은 미공시
    """
    if not r or not d:
        return r, d, t
    try:
        parts = d.replace('-', '.').split('.')
        if len(parts) != 3:
            return r, d, t
        eval_date = datetime.date(int(parts[0]), int(parts[1]), int(parts[2]))
        today = datetime.date.today()
        try:
            cutoff = today.replace(year=today.year - 2)
        except ValueError:
            # 윤년 2/29 → 2/28로 보정
            cutoff = today.replace(year=today.year - 2, day=28)
        if eval_date < cutoff:
            return '', '', ''
    except (ValueError, AttributeError):
        pass
    return r, d, t


# ─── 기관 1개 병렬 조회 ──────────────────────────────────────────────

def _scrape_one(name: str, is_insurance: bool, is_savings: bool = False) -> dict:
    """3사를 동시에 조회.

    각 평가사에 하드 타임아웃을 건다. Playwright 내부 타임아웃이 걸리지 않는 지점
    (브라우저 실행·페이지 종료 등)에서 멈추면 .result()가 무한 대기해 전체 조회가
    영영 끝나지 않았다(2026-07-21 손해보험 첫 기관에서 멈춘 사례).
    멈춘 평가사는 빈값으로 넘기고 다음 기관으로 진행한다. 빈값이 기존 등급을
    지우지는 않는다(app.py MISS_LIMIT: 연속 2회 빈값부터 삭제).
    """
    pool = ThreadPoolExecutor(max_workers=3)
    try:
        futures = {
            'nice': pool.submit(scrape_nice, name, is_insurance),
            'kr':   pool.submit(scrape_kr,   name, is_insurance),
            'kis':  pool.submit(scrape_kis,  name, is_insurance),
        }
        out = {}
        for ag, fut in futures.items():
            try:
                out[ag] = fut.result(timeout=AGENCY_TIMEOUT_SEC)
            except FuturesTimeout:
                logger.warning('조회 시간초과 [%s] %s — %d초 초과, 빈값 처리',
                               name, ag.upper(), AGENCY_TIMEOUT_SEC)
                out[ag] = ('', '', '')
            except Exception as e:
                logger.warning('조회 오류 [%s] %s: %s', name, ag.upper(), e)
                out[ag] = ('', '', '')
        nice_r, nice_d, nice_t = out['nice']
        kr_r,   kr_d,   kr_t   = out['kr']
        kis_r,  kis_d,  kis_t  = out['kis']
    finally:
        # 멈춘 스레드가 남아 있어도 기다리지 않는다(wait=True면 여기서 다시 무한 대기).
        pool.shutdown(wait=False)

    # 2년 룰(전 기관): 평정일이 오늘 기준 2년 초과면 미공시 처리
    # (2026-07-10 사용자 지시 [10] — 기존 저축은행 한정에서 전 카테고리로 확장)
    nice_r, nice_d, nice_t = _apply_2y_filter(nice_r, nice_d, nice_t)
    kr_r,   kr_d,   kr_t   = _apply_2y_filter(kr_r,   kr_d,   kr_t)
    kis_r,  kis_d,  kis_t  = _apply_2y_filter(kis_r,  kis_d,  kis_t)

    found = any([nice_r, kr_r, kis_r])
    logger.info(
        '  %s → NICE:%s(%s) KR:%s(%s) KIS:%s(%s)',
        name,
        nice_r or '-', nice_t or '-',
        kr_r   or '-', kr_t   or '-',
        kis_r  or '-', kis_t  or '-',
    )
    return {
        'nice': nice_r, 'nice_eval_date': nice_d, 'nice_type': nice_t,
        'kr':   kr_r,   'kr_eval_date':   kr_d,   'kr_type':   kr_t,
        'kis':  kis_r,  'kis_eval_date':  kis_d,  'kis_type':  kis_t,
        'scrape_status': '조회성공' if found else '등급없음',
    }


# ─── 전체 기관 일괄 조회 ─────────────────────────────────────────────

def scrape_all_ratings(institutions: dict, progress_callback=None) -> dict:
    """전체 기관을 최대 4개씩 병렬로 조회 (Playwright 부하 제한)"""
    global _KR_CACHE_LOADED
    if not _KR_CACHE_LOADED:
        logger.info('[KR] 등급공시 배치 조회 시작...')
        _load_kr_cache()

    tasks: list[tuple[str, bool, bool]] = []
    for category, items in institutions.items():
        is_ins = category in INSURANCE_CATEGORIES
        is_sav = category in SAVING_BANK_CATEGORIES
        for inst in items:
            tasks.append((inst['name'], is_ins, is_sav))

    result: dict = {}
    logger.info('총 %d개 기관 조회 시작', len(tasks))

    with ThreadPoolExecutor(max_workers=4) as pool:
        future_map = {
            pool.submit(_scrape_one, name, is_ins, is_sav): name
            for name, is_ins, is_sav in tasks
        }
        for future in as_completed(future_map):
            name = future_map[future]
            try:
                result[name] = future.result()
            except Exception as e:
                logger.warning('조회 실패 [%s]: %s', name, e)
                result[name] = {
                    'nice': '', 'nice_eval_date': '', 'nice_type': '',
                    'kr':   '', 'kr_eval_date':   '', 'kr_type':   '',
                    'kis':  '', 'kis_eval_date':  '', 'kis_type':  '',
                    'scrape_status': '조회오류',
                }
            if progress_callback:
                try:
                    progress_callback(name, result[name])
                except Exception:
                    pass

    logger.info('조회 완료: %d건', len(result))
    return result


def close_browser():
    """각 호출이 자체 playwright 컨텍스트를 관리하므로 별도 정리 불필요"""
    pass
