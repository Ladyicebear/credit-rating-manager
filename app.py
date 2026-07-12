import os
import io
import re
import json
import logging
import threading
from collections import Counter
from datetime import datetime
from markupsafe import Markup
from flask import (Flask, render_template, jsonify, request, send_file,
                   redirect, url_for, session)

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)
# 세션 쿠키 서명 키. 배포 시엔 반드시 SECRET_KEY 환경변수로 고정값 지정
# (여러 인스턴스가 같은 키를 써야 로그인 세션이 공유됨).
app.secret_key = os.environ.get('SECRET_KEY', 'dev-secret-change-me')

# ── 접근 제한 (로그인) ─────────────────────────────────────────────────
# 아이디/비밀번호는 환경변수로 주입. 미설정 시 로컬 개발용 기본값(배포 시 반드시 변경).
APP_USER = os.environ.get('APP_USER', 'admin')
APP_PASSWORD = os.environ.get('APP_PASSWORD', 'goun')
if not (os.environ.get('APP_USER') and os.environ.get('APP_PASSWORD')):
    logger.warning('기본 로그인 계정(admin) 사용 중 — 배포 시 APP_USER/APP_PASSWORD 환경변수를 반드시 설정하세요.')

# 로그인 없이 접근 허용할 엔드포인트
_PUBLIC_ENDPOINTS = {'login', 'static'}


@app.before_request
def _require_login():
    if request.endpoint in _PUBLIC_ENDPOINTS:
        return
    if session.get('logged_in'):
        return
    # API(fetch) 요청은 401 JSON, 일반 페이지는 로그인 화면으로 유도
    if request.path.startswith('/api/'):
        return jsonify({'success': False, 'message': '로그인이 필요합니다',
                        'login_required': True}), 401
    return redirect(url_for('login', next=request.path))


@app.route('/login', methods=['GET', 'POST'])
def login():
    error = ''
    if request.method == 'POST':
        u = request.form.get('username', '').strip()
        p = request.form.get('password', '')
        if u == APP_USER and p == APP_PASSWORD:
            session['logged_in'] = True
            session['user'] = u
            nxt = request.args.get('next') or '/'
            if not nxt.startswith('/'):   # 오픈 리다이렉트 방지
                nxt = '/'
            return redirect(nxt)
        error = '아이디 또는 비밀번호가 올바르지 않습니다.'
    if session.get('logged_in'):
        return redirect(url_for('index'))
    return render_template('login.html', error=error)


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

# ── 백그라운드 조회 상태 ───────────────────────────────────────────────
_refresh_lock = threading.Lock()
_refresh_state: dict = {
    'running': False, 'total': 0, 'completed': 0,
    'results': {}, 'summary': '', 'updated_at': '', 'error': '',
}

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, 'data')
RATINGS_FILE = os.path.join(DATA_DIR, 'ratings.json')
INSTITUTIONS_FILE = os.path.join(DATA_DIR, 'institutions.json')
EXPORT_TEMPLATE = os.path.join(BASE_DIR, 'export_template.xlsx')
OVERRIDES_FILE = os.path.join(DATA_DIR, 'overrides.json')
HISTORY_FILE = os.path.join(DATA_DIR, 'rating_history.json')

# 서버 포트: 클라우드(Cloud Run 등)는 환경변수 PORT를 지정함. 없으면 로컬 기본 5000.
PORT = int(os.environ.get('PORT', 5000))

RATING_SCALE = [
    'AAA', 'AA+', 'AA', 'AA-',
    'A+', 'A', 'A-',
    'BBB+', 'BBB', 'BBB-',
    'BB+', 'BB', 'BB-',
    'B+', 'B', 'B-',
    'CCC+', 'CCC', 'CCC-',
    'CC', 'C', 'D',
]

INSURANCE_CATEGORIES = {'손해보험', '생명보험'}
AGENCIES = ['nice', 'kr', 'kis']
AGENCY_LABELS = {'nice': '나이스신용평가', 'kr': '한국기업평가', 'kis': '한국신용평가'}


# ── Rating helpers ────────────────────────────────────────────────────

def get_lowest_rating(ratings: list) -> str:
    valid = [r for r in ratings if r and r.strip() in RATING_SCALE]
    if not valid:
        return ''
    return max(valid, key=lambda r: RATING_SCALE.index(r))


def compare_ratings(old: str, new: str) -> str:
    """등급 변화 방향: 'up' | 'down' | 'same' | ''"""
    if not old or not new:
        return ''
    if old not in RATING_SCALE or new not in RATING_SCALE:
        return ''
    diff = RATING_SCALE.index(new) - RATING_SCALE.index(old)
    if diff < 0:
        return 'up'
    if diff > 0:
        return 'down'
    return 'same'


def get_rating_type_label(agency_type: str) -> str:
    labels = {
        'ICR': '기업신용등급',
        '회사채선순위': '회사채 선순위',
        'IFS': '보험지급능력',
        '': '-',
    }
    return labels.get(agency_type, agency_type)


# ── Data I/O ──────────────────────────────────────────────────────────

def load_institutions() -> dict:
    with open(INSTITUTIONS_FILE, 'r', encoding='utf-8') as f:
        return json.load(f)


def load_ratings() -> dict:
    if not os.path.exists(RATINGS_FILE):
        return {}
    with open(RATINGS_FILE, 'r', encoding='utf-8') as f:
        return json.load(f)


def save_ratings(data: dict):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(RATINGS_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ── 신용등급 변경이력 ──────────────────────────────────────────────────
# 조회 중 등급 변경이 감지되면 이력을 누적한다.
# 레코드: {name, agency, agency_label, prev, current, direction,
#          type_label, eval_date(변경일=평가일), detected_at(감지일시)}

def load_history() -> list:
    if not os.path.exists(HISTORY_FILE):
        return []
    try:
        with open(HISTORY_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except Exception:
        return []


def append_history(records: list):
    """새 변경 레코드들을 이력 파일에 누적(append). 비어 있으면 아무것도 안 함."""
    if not records:
        return
    os.makedirs(DATA_DIR, exist_ok=True)
    hist = load_history()
    hist.extend(records)
    with open(HISTORY_FILE, 'w', encoding='utf-8') as f:
        json.dump(hist, f, ensure_ascii=False, indent=2)


# ── 사용자 강제 정정(override) ─────────────────────────────────────────
# 재조회(스크래핑)가 사용자가 정정한 값을 다시 덮어쓰지 못하도록, 표시·저장 시점에 강제.
# 형식: { "기관명": { "kr": null } }  → null이면 미공시(빈값) 강제, 문자열이면 그 등급 강제.

def load_overrides() -> dict:
    if not os.path.exists(OVERRIDES_FILE):
        return {}
    try:
        with open(OVERRIDES_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def _apply_overrides(name: str, d: dict, overrides: dict | None = None) -> dict:
    """기관 데이터 dict(d)에 override를 in-place 적용. 정정값이 재조회에도 유지되게 함."""
    ov = (overrides if overrides is not None else load_overrides()).get(name)
    if not ov:
        return d
    for ag, val in ov.items():
        if ag not in AGENCIES:
            continue
        d[ag] = val or ''
        d[f'{ag}_eval_date'] = ''
        d[f'{ag}_type'] = ''
        d[f'{ag}_prev'] = ''
        d[f'{ag}_changed'] = False
    # override가 적용되면 최종등급 변경표시는 무의미 → 초기화
    d['final_prev'] = ''
    d['final_changed'] = False
    return d


# ── Build view data ───────────────────────────────────────────────────

def build_row(name: str, category: str, inst_data: dict, overrides: dict | None = None) -> dict:
    is_insurance = category in INSURANCE_CATEGORIES
    inst_data = _apply_overrides(name, dict(inst_data), overrides)

    agencies_out = {}
    any_changed = False

    for ag in AGENCIES:
        current = inst_data.get(ag, '')
        prev    = inst_data.get(f'{ag}_prev', '')
        changed = inst_data.get(f'{ag}_changed', False)
        direction = compare_ratings(prev, current) if changed and prev else ''
        raw_type = inst_data.get(f'{ag}_type', '')
        if is_insurance:
            raw_type = 'IFS'

        agencies_out[ag] = {
            'rating':    current,
            'eval_date': inst_data.get(f'{ag}_eval_date', ''),
            'prev':      prev,
            'changed':   changed,
            'direction': direction,
            'type':      raw_type,
            'type_label': get_rating_type_label(raw_type),
        }
        if changed and prev and current != prev:
            any_changed = True

    ratings_list = [agencies_out[ag]['rating'] for ag in AGENCIES]
    final = get_lowest_rating(ratings_list)

    final_prev = inst_data.get('final_prev', '')
    final_changed = inst_data.get('final_changed', False)
    final_direction = compare_ratings(final_prev, final) if final_changed and final_prev else ''

    # 최종등급 근거: 어느 평가사의 어떤 유형 등급인지
    if final:
        contrib_ags = [ag for ag in AGENCIES if agencies_out[ag]['rating'] == final]
        contrib_labels = [AGENCY_LABELS[ag] for ag in contrib_ags]
        contrib_types = list(dict.fromkeys(
            agencies_out[ag]['type_label'] for ag in contrib_ags if agencies_out[ag]['type']
        ))
        if len(contrib_ags) == 3:
            basis_agency = '3사 동일'
        elif len(contrib_ags) == 2:
            basis_agency = ' · '.join(contrib_labels)
        else:
            basis_agency = contrib_labels[0]
        basis_type = ' · '.join(contrib_types) if contrib_types else ''
    else:
        basis_agency = ''
        basis_type = ''

    # 등급 상이 여부: 등급이 있는 평가사들 사이에 서로 다른 등급이 존재하는지
    rated = [(ag, agencies_out[ag]['rating']) for ag in AGENCIES if agencies_out[ag]['rating']]
    rating_counts = Counter(r for _, r in rated)
    rating_mismatch = len(rating_counts) > 1
    mismatch_agencies = []
    if rating_mismatch:
        if all(c == 1 for c in rating_counts.values()):
            # 모든 평가사 등급이 제각각 → 전부 상이 처리
            mismatch_agencies = [AGENCY_LABELS[ag] for ag, _ in rated]
        else:
            top_count = max(rating_counts.values())
            majority = {r for r, c in rating_counts.items() if c == top_count}
            mismatch_agencies = [AGENCY_LABELS[ag] for ag, r in rated if r not in majority]

    # 변경사항 목록
    changes = []
    for ag in AGENCIES:
        ag_d = agencies_out[ag]
        if ag_d['changed'] and ag_d['prev'] and ag_d['rating'] and ag_d['rating'] != ag_d['prev']:
            changes.append({
                'agency_label': AGENCY_LABELS[ag],
                'prev': ag_d['prev'],
                'current': ag_d['rating'],
                'direction': ag_d['direction'],
                'type_label': ag_d['type_label'],
                'eval_date': ag_d['eval_date'],
            })

    # 등급구분 대표값
    types_used = [agencies_out[ag]['type'] for ag in AGENCIES if agencies_out[ag]['rating']]
    if is_insurance:
        rep_type = 'IFS'
    elif types_used and all(t == '회사채선순위' for t in types_used):
        rep_type = '회사채선순위'
    elif types_used and all(t == 'ICR' for t in types_used):
        rep_type = 'ICR'
    elif '회사채선순위' in types_used:
        rep_type = 'ICR/회사채혼용'
    else:
        rep_type = ''

    return {
        'name': name,
        'category': category,
        'is_insurance': is_insurance,
        'agencies': agencies_out,
        'final': final,
        'final_prev': final_prev,
        'final_changed': final_changed,
        'final_direction': final_direction,
        'basis_agency': basis_agency,
        'basis_type': basis_type,
        'rating_mismatch': rating_mismatch,
        'mismatch_agencies': mismatch_agencies,
        'changes': changes,
        'any_changed': any_changed,
        'scrape_status': inst_data.get('scrape_status', ''),
        'updated': inst_data.get('updated', ''),
        'rep_type': rep_type,
        'rep_type_label': get_rating_type_label(rep_type),
    }


def build_response_data(institutions: dict, ratings: dict) -> dict:
    category_order = ['증권', '시중은행', '지방은행', '저축은행', '손해보험', '생명보험', '기타']
    overrides = load_overrides()
    result = {}
    for category in category_order:
        items = institutions.get(category, [])
        rows = []
        for inst in items:
            name = inst['name']
            inst_data = ratings.get(name, {})
            rows.append(build_row(name, category, inst_data, overrides))
        result[category] = rows
    return result


# ── Template globals ──────────────────────────────────────────────────

def _rating_css(r: str) -> str:
    if r == 'AAA':        return 'rating-AAA'
    if r.startswith('AA'): return 'rating-AA'
    if r.startswith('A'):  return 'rating-A'
    if r.startswith('BBB'): return 'rating-BBB'
    if r.startswith('BB'): return 'rating-BB'
    if r.startswith('B'):  return 'rating-B'
    if r.startswith('CCC'): return 'rating-CCC'
    if r in ('CC', 'C', 'D'): return 'rating-low'
    return ''


@app.template_global()
def rating_badge(r: str, extra_cls: str = '') -> Markup:
    if not r:
        return Markup('<span class="no-rating">—</span>')
    css = _rating_css(r)
    return Markup(f'<span class="rbadge {css} {extra_cls}">{r}</span>')


@app.template_global()
def change_html(prev: str, current: str, direction: str) -> Markup:
    if not prev or not current or prev == current:
        return Markup('')
    arrow = {'up': '▲', 'down': '▼', 'same': '→'}.get(direction, '→')
    cls   = {'up': 'chg-up', 'down': 'chg-down', 'same': 'chg-same'}.get(direction, '')
    return Markup(
        f'<span class="chg-tag {cls}">'
        f'{prev}&nbsp;{arrow}&nbsp;{current}'
        f'</span>'
    )


@app.template_global()
def rating_select_options() -> Markup:
    opts = '<option value="">— 미공시 —</option>'
    for r in RATING_SCALE:
        opts += f'<option value="{r}">{r}</option>'
    return Markup(opts)


# ── Routes ────────────────────────────────────────────────────────────

@app.route('/pension')
def pension():
    """원리금보장상품 금리관리 화면(통합 탭). 순수 HTML을 그대로 서빙(Jinja 미처리)."""
    return send_file(os.path.join(BASE_DIR, 'pension.html'))


# ── 원리금보장 금리현황 레포트: 양식(pension_report_template.xlsx)에 현재 화면 금리 채워 반환 ──
_PENSION_MONTHS = [3, 6, 12, 18, 24, 30, 36, 48, 60]
_PENSION_ALIAS = {  # 레포트 표기 -> 데이터 표기(예외)
    '기업은행': '중소기업은행', '산업은행': '한국산업은행', 'SBI저축은행': '에스비아이저축은행',
    'NH저축은행': '엔에이치저축은행', 'DB저축은행': '디비저축은행', 'JT친애저축은행': '제이티친애저축은행',
    'BNK투자증권': '비엔케이투자증권', '신한생명': '신한라이프',
}
_PENSION_SECMAP = {'증권': '증권', '은행': '은행', '생보': '생명보험', '손보': '손해보험', '저축은행': '저축은행'}
_ROMAN = [('Ⅲ', '3'), ('Ⅱ', '2'), ('Ⅰ', '1'), ('Ⅳ', '4'), ('Ⅴ', '5'),
          ('ⅲ', '3'), ('ⅱ', '2'), ('ⅰ', '1'), ('III', '3'), ('II', '2'), ('IV', '4'), ('V', '5'), ('I', '1')]


def _pen_core(s):
    s = str(s).lower()
    s = re.sub(r'주식회사|㈜|\(주\)|\s', '', s)
    s = re.sub(r'(생명보험|손해보험|화재보험|연금보험)$', '', s)
    s = re.sub(r'(화재|생명|손보|손해|연금)$', '', s)
    return s


def _pen_inst_key(name, is_report=False):
    if is_report and name in _PENSION_ALIAS:
        name = _PENSION_ALIAS[name]
    return _pen_core(name)


def _pen_secnorm(s):
    return _PENSION_SECMAP.get(str(s).replace('\n', '').strip(), str(s))


def _pen_roman(s):
    s = str(s)
    for k, v in _ROMAN:
        s = s.replace(k, v)
    return s


def _pen_iyul(fam):
    f = _pen_roman(fam)
    bonus = '보너스' in f
    base = re.sub(r'/.*$', '', f)
    m = re.search(r'이율보증형(보험)?\s*([123])', base)
    if m:
        return (bonus, m.group(2))
    if '이율보증형' in base:
        return (bonus, '1')
    return (None, None)


def _pen_match_product(rp, g):
    fam = g['fam']
    text = fam + ' ' + g['names']
    rp = rp.strip()
    if rp == '정기예금':
        return '정기예금' in text
    if rp == 'ELB':
        return ('ELB' in text) and (('ELB' in fam) or ('DLB' not in fam))
    if rp == 'DLB':
        return 'DLB' in text
    if rp == 'ELB/DLB':
        return ('ELB' in text) or ('DLB' in text)
    if rp == '발행어음':
        return '발행어음' in text
    if rp == 'RP':
        return (fam == 'RP') or ('RP' in fam) or ('환매조건부' in text)
    if '보너스이율보증형' in rp:
        b, _ = _pen_iyul(fam)
        return b is True
    m = re.search(r'이율보증형보험\s*([123])?', rp)
    if m:
        want = m.group(1) or '1'
        b, num = _pen_iyul(fam)
        return (b is False) and (num == want)
    return bool(rp) and rp in text


@app.route('/api/pension_report', methods=['POST'])
def pension_report():
    """현재 화면(기준월)의 금리를 레포트 양식에 채워 xlsx로 반환."""
    import io as _io
    import openpyxl
    from openpyxl.cell.cell import MergedCell

    payload = request.get_json(force=True) or {}
    month = payload.get('month', '')
    rows = payload.get('rows', [])

    # 클라이언트 그룹 rows 정규화(db/dc 키 int화)
    G = []
    for r in rows:
        db = {int(k): v for k, v in (r.get('db') or {}).items() if v is not None}
        dc = {int(k): v for k, v in (r.get('dc') or {}).items() if v is not None}
        G.append({'sector': r.get('sector'), 'org': r.get('org') or '', 'fam': r.get('fam') or '',
                  'names': ' '.join(r.get('names') or []), 'db': db, 'dc': dc, 'def': r.get('def')})

    def find_rp(ikey):
        c = [g for g in G if _pen_inst_key(g['org']) == ikey
             and (g['fam'] == 'RP' or 'RP' in g['fam'] or '환매조건부' in (g['fam'] + g['names']) or '발행어음' in (g['fam'] + g['names']))]
        c.sort(key=lambda x: len(x['db']) + len(x['dc']), reverse=True)
        return c[0] if c else None

    wb = openpyxl.load_workbook(os.path.join(BASE_DIR, 'pension_report_template.xlsx'))
    ws = wb.worksheets[0]

    def setcell(rr, cc, v):
        cell = ws.cell(rr, cc)
        if isinstance(cell, MergedCell):   # 병합 비앵커 셀은 건너뜀(디폴트 X 섹션병합 등)
            return
        cell.value = v

    if month and re.match(r'\d{4}-\d{2}', month):
        y, mm = month.split('-')
        setcell(1, 2, f"          퇴직연금 원리금지급형상품 공시금리 현황 [{y}년 {int(mm):02d}월]                ")

    DB_C0, DC_C0, X_C, RP_C = 6, 15, 24, {'db': 26, 'dc': 27, 'irp': 28}
    cur_sec = cur_inst = None
    matched = 0
    for r in range(7, 101):
        b = ws.cell(r, 2).value
        c = ws.cell(r, 3).value
        e = ws.cell(r, 5).value
        if b:
            cur_sec = b
        if c:
            cur_inst = c
        if not e:
            continue
        ikey = _pen_inst_key(cur_inst, is_report=True)
        dsec = _pen_secnorm(cur_sec)
        cand = [g for g in G if _pen_secnorm(g['sector']) == dsec and _pen_inst_key(g['org']) == ikey]
        if not cand:
            cand = [g for g in G if _pen_inst_key(g['org']) == ikey]
        if not cand:
            continue
        pm = [g for g in cand if _pen_match_product(str(e), g)]
        if not pm:
            continue
        pm.sort(key=lambda x: len(x['db']) + len(x['dc']) + (1 if x['def'] else 0), reverse=True)
        mdb, mdc, mdef = {}, {}, None
        for g in pm:
            for m in _PENSION_MONTHS:
                if m in g['db'] and m not in mdb:
                    mdb[m] = g['db'][m]
                if m in g['dc'] and m not in mdc:
                    mdc[m] = g['dc'][m]
            if mdef is None and g['def'] is not None:
                mdef = g['def']
        matched += 1
        for i, m in enumerate(_PENSION_MONTHS):
            if m in mdb:
                setcell(r, DB_C0 + i, round(float(mdb[m]), 3))
            if m in mdc:
                setcell(r, DC_C0 + i, round(float(mdc[m]), 3))
        if mdef is not None:
            setcell(r, X_C, round(float(mdef), 3))
        if c and dsec == '증권':   # RP금리(1년): 기관 첫 행에 기록
            rp = find_rp(ikey)
            if rp:
                if 12 in rp['db']:
                    setcell(r, RP_C['db'], round(float(rp['db'][12]), 3))
                if 12 in rp['dc']:
                    setcell(r, RP_C['dc'], round(float(rp['dc'][12]), 3))
                    setcell(r, RP_C['irp'], round(float(rp['dc'][12]), 3))

    bio = _io.BytesIO()
    wb.save(bio)
    bio.seek(0)
    fname = f"퇴직연금 금리정보 현황_{month}.xlsx" if month else "퇴직연금 금리정보 현황.xlsx"
    logger.info('금리현황 레포트 생성: month=%s, 데이터 %d행, 매칭 %d행', month, len(G), matched)
    return send_file(bio, as_attachment=True, download_name=fname,
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


@app.route('/')
def index():
    institutions = load_institutions()
    ratings = load_ratings()
    data = build_response_data(institutions, ratings)
    meta = ratings.get('_meta', {})

    # 변경 알람 요약 + 변경 감지된 기관 목록(이름·카테고리)
    changed_institutions = [
        {'name': row['name'], 'category': category}
        for category, rows in data.items()
        for row in rows if row['any_changed']
    ]
    total_changed = len(changed_institutions)

    # 변경이력 (최신순)
    history = sorted(
        load_history(),
        key=lambda h: (h.get('detected_at', ''), h.get('eval_date', '')),
        reverse=True,
    )

    return render_template(
        'index.html',
        data=data,
        last_updated=meta.get('updated', '-'),
        scrape_summary=meta.get('scrape_summary', ''),
        total_changed=total_changed,
        changed_institutions=changed_institutions,
        history=history,
        rating_scale=RATING_SCALE,
        agencies=AGENCIES,
        agency_labels=AGENCY_LABELS,
    )


@app.route('/api/export.xlsx')
def api_export_xlsx():
    """조회된 전체 기관 정보를 첨부 양식(export_template.xlsx)에 채워 다운로드.
    컬럼: 업권 | 기관명 | 나이스신용평가 | 한국기업평가 | 한국신용평가 | 최종적용신용등급 | 최종등급 근거
    """
    import openpyxl
    from copy import copy

    institutions = load_institutions()
    ratings = load_ratings()
    data = build_response_data(institutions, ratings)

    wb = openpyxl.load_workbook(EXPORT_TEMPLATE)
    ws = wb.active

    # 1행(헤더)은 유지, 2행 이후를 데이터로 채움.
    # 템플릿 2행에 들어있던 예시 스타일을 복제해 각 데이터 행에 적용.
    sample_styles = [copy(ws.cell(row=2, column=c)._style) for c in range(1, 8)]
    # 기존 데이터 영역(2행~) 비우기
    if ws.max_row >= 2:
        ws.delete_rows(2, ws.max_row - 1)

    r = 2
    for category, rows in data.items():
        for row in rows:
            ags = row['agencies']
            values = [
                category,
                row['name'],
                ags['nice']['rating'],
                ags['kr']['rating'],
                ags['kis']['rating'],
                row['final'],
                row['basis_agency'],
            ]
            for c, v in enumerate(values, start=1):
                cell = ws.cell(row=r, column=c, value=v)
                cell._style = copy(sample_styles[c - 1])
            r += 1

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    fname = f"신용등급_조회결과_{datetime.now().strftime('%Y%m%d')}.xlsx"
    return send_file(
        buf,
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        as_attachment=True,
        download_name=fname,
    )


@app.route('/api/ratings')
def api_ratings():
    institutions = load_institutions()
    ratings = load_ratings()
    data = build_response_data(institutions, ratings)
    meta = ratings.get('_meta', {})
    return jsonify({'data': data, 'last_updated': meta.get('updated', '-')})


def _apply_scrape_results(scrape_result: dict, update_meta: bool = True,
                          alive_override: set | None = None) -> tuple[int, int]:
    """스크래핑 결과를 ratings.json에 반영. (success_count, changed_count) 반환.

    - update_meta: False면 전역 요약/타임스탬프(_meta)를 갱신하지 않음 (단일 기관 재조회용).
    - alive_override: 지정 시 이 평가사 집합을 'alive'로 간주 (단일 기관 재조회는 교차 판단이
      불가하므로, 전체 조회가 해당 기관을 처리하는 것과 동일하게 3사를 alive로 넘겨 사용).
    """
    ratings = load_ratings()
    overrides = load_overrides()
    success_count = 0
    changed_count = 0
    history_records = []  # 이번 실행에서 감지된 등급 변경 이력
    _now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    # 평가사별 '이번 실행 전체 생존' 판단: 어느 한 기관에서라도 값을 반환했으면 정상.
    # 특정 평가사가 모든 기관에서 빈값이면 사이트/API 전면 장애로 보고 stale 값을 지우지 않는다.
    if alive_override is not None:
        agency_alive = {ag: (ag in alive_override) for ag in AGENCIES}
    else:
        agency_alive = {ag: False for ag in AGENCIES}
        for _n, _d in scrape_result.items():
            if _n == '_meta':
                continue
            for ag in AGENCIES:
                if _d.get(ag, ''):
                    agency_alive[ag] = True
    for name, new_data in scrape_result.items():
        if name == '_meta':
            continue
        prev_entry = ratings.get(name, {})
        entry = dict(prev_entry)
        # 신규 기관 첫 스크래핑 시 일부 기관 실패해도 키가 항상 존재하도록 보장
        for _ag in AGENCIES:
            entry.setdefault(_ag, '')
            entry.setdefault(f'{_ag}_eval_date', '')
            entry.setdefault(f'{_ag}_type', '')
            entry.setdefault(f'{_ag}_prev', '')
            entry.setdefault(f'{_ag}_changed', False)
        any_ag_updated = False
        for ag in AGENCIES:
            old_rating = prev_entry.get(ag, '')
            new_rating = new_data.get(ag, '')
            new_date   = new_data.get(f'{ag}_eval_date', '')
            if new_rating:
                # NICE에서 평가일 없는 값은 '검색결과 테이블 폴백'(불안정 출처)에서 나온 것.
                # 이런 값으로는 '변경'을 표시하지 않음 → 과거 오파싱값(예: KB국민은행 AA+)과
                # 비교해 생기는 허위 변경(오탐)을 방지. KR은 항상 날짜 있음, KIS는 영향 없음.
                unreliable_nice = (ag == 'nice' and not new_date)
                if old_rating and old_rating != new_rating and not unreliable_nice:
                    entry[f'{ag}_prev'] = old_rating
                    entry[f'{ag}_changed'] = True
                    changed_count += 1
                    history_records.append({
                        'name': name,
                        'agency': ag,
                        'agency_label': AGENCY_LABELS[ag],
                        'prev': old_rating,
                        'current': new_rating,
                        'direction': compare_ratings(old_rating, new_rating),
                        'type_label': get_rating_type_label(new_data.get(f'{ag}_type', '')),
                        'eval_date': new_date,        # 변경일(평가일)
                        'detected_at': _now,          # 감지일시
                    })
                entry[ag] = new_rating
                entry[f'{ag}_eval_date'] = new_data.get(f'{ag}_eval_date', prev_entry.get(f'{ag}_eval_date', ''))
                entry[f'{ag}_type']      = new_data.get(f'{ag}_type',      prev_entry.get(f'{ag}_type', ''))
                any_ag_updated = True
        # stale 잔존값 정리: 이 기관 조회가 성공(다른 평가사 값 확보)했고, 해당 평가사가
        # 이번 실행에서 다른 기관들엔 정상 응답했는데 이 기관에서만 빈값이면 → 과거 오파싱으로
        # 남은 값(예: KB손해보험 KIS 'AAA')을 제거한다. 평가사 전면 장애(agency_alive=False)나
        # 기관 전체 실패(any_ag_updated=False) 시에는 보존한다.
        if any_ag_updated:
            for ag in AGENCIES:
                if not new_data.get(ag, '') and agency_alive[ag] and entry.get(ag, ''):
                    entry[ag] = ''
                    entry[f'{ag}_eval_date'] = ''
                    entry[f'{ag}_type'] = ''
        old_final = prev_entry.get('final', '')
        new_finals = [entry.get(ag, '') for ag in AGENCIES]
        new_final  = get_lowest_rating(new_finals)
        if old_final and new_final and old_final != new_final:
            entry['final_prev']    = old_final
            entry['final_changed'] = True
        if new_final:
            entry['final'] = new_final
        status = new_data.get('scrape_status', '')
        if any_ag_updated:
            entry['scrape_status'] = status
            entry['updated'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            success_count += 1
        else:
            import re as _re
            base = _re.sub(r'\(재조회실패\)', '', prev_entry.get('scrape_status', '')).strip()
            entry['scrape_status'] = (base or '등급없음') + '(재조회실패)'
        # 사용자 강제 정정(override) 적용 → 재조회 결과가 정정값을 덮어쓰지 못하게 함
        if overrides.get(name):
            _apply_overrides(name, entry, overrides)
            entry['final'] = get_lowest_rating([entry.get(ag, '') for ag in AGENCIES])
        ratings[name] = entry
    if update_meta:
        ratings['_meta'] = {
            'updated': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'scrape_summary': f'조회 성공 {success_count}건 / 등급 변경 감지 {changed_count}건',
        }
    save_ratings(ratings)
    append_history(history_records)
    return success_count, changed_count


@app.route('/api/refresh', methods=['POST'])
def api_refresh():
    global _refresh_state
    with _refresh_lock:
        if _refresh_state['running']:
            return jsonify({'success': False, 'message': '이미 조회 중입니다'}), 409

    institutions = load_institutions()
    total = sum(len(items) for items in institutions.values())

    with _refresh_lock:
        _refresh_state = {
            'running': True, 'total': total, 'completed': 0,
            'results': {}, 'summary': '', 'updated_at': '', 'error': '',
        }

    def _run():
        global _refresh_state
        try:
            from scraper import scrape_all_ratings
            all_results = {}

            def on_progress(name, data):
                with _refresh_lock:
                    _refresh_state['results'][name] = data
                    _refresh_state['completed'] += 1
                all_results[name] = data

            scrape_all_ratings(institutions, progress_callback=on_progress)
            success_count, changed_count = _apply_scrape_results(all_results)
            summary = f'조회 성공 {success_count}건 / 등급 변경 감지 {changed_count}건'
            with _refresh_lock:
                _refresh_state['running']    = False
                _refresh_state['summary']    = summary
                _refresh_state['updated_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        except Exception as e:
            logger.exception('Refresh failed')
            with _refresh_lock:
                _refresh_state['running'] = False
                _refresh_state['error']   = str(e)

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({'success': True, 'total': total})


@app.route('/api/refresh_status')
def api_refresh_status():
    with _refresh_lock:
        return jsonify(dict(_refresh_state))


@app.route('/api/refresh_one/<path:name>', methods=['POST'])
def api_refresh_one(name):
    """기관 1개만 즉시 재조회 (전체 조회 없이). 전역 요약 배너는 갱신하지 않는다."""
    institutions = load_institutions()
    category = next(
        (cat for cat, items in institutions.items()
         if any(i['name'] == name for i in items)),
        None,
    )
    if category is None:
        return jsonify({'success': False, 'message': '기관을 찾을 수 없습니다'}), 404

    from scraper import _scrape_one, SAVING_BANK_CATEGORIES
    is_ins = category in INSURANCE_CATEGORIES
    is_sav = category in SAVING_BANK_CATEGORIES
    try:
        data = _scrape_one(name, is_ins, is_sav)
    except Exception as e:
        logger.exception('단일 재조회 실패 [%s]', name)
        return jsonify({'success': False, 'message': str(e)}), 500

    # 단일 기관이라 평가사 교차 생존 판단이 불가 → 전체 조회와 동일하게 3사를 alive로 취급.
    # 전역 _meta(마지막 업데이트/요약)는 건드리지 않음.
    _apply_scrape_results({name: data}, update_meta=False, alive_override=set(AGENCIES))

    ratings = load_ratings()
    row = build_row(name, category, ratings.get(name, {}), load_overrides())
    return jsonify({'success': True, 'row': row,
                    'scrape_status': ratings.get(name, {}).get('scrape_status', '')})


@app.route('/api/ratings/<path:name>', methods=['PUT'])
def api_update_rating(name):
    data = request.get_json()
    ratings = load_ratings()
    entry = ratings.get(name, {})

    for ag in AGENCIES:
        old_rating = entry.get(ag, '')
        new_rating = data.get(ag, '').strip()
        new_date   = data.get(f'{ag}_eval_date', '').strip()
        new_type   = data.get(f'{ag}_type', '').strip()

        if old_rating and new_rating and old_rating != new_rating:
            entry[f'{ag}_prev']    = old_rating
            entry[f'{ag}_changed'] = True
        entry[ag] = new_rating
        if new_date:
            entry[f'{ag}_eval_date'] = new_date
        if new_type:
            entry[f'{ag}_type'] = new_type

    # 최종 등급 갱신
    old_final = entry.get('final', '')
    new_final = get_lowest_rating([entry.get(ag, '') for ag in AGENCIES])
    if old_final and new_final and old_final != new_final:
        entry['final_prev']    = old_final
        entry['final_changed'] = True
    entry['final']   = new_final
    entry['updated'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    entry['scrape_status'] = '수동입력'

    if '_meta' not in ratings:
        ratings['_meta'] = {}
    ratings['_meta']['updated'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    ratings[name] = entry
    save_ratings(ratings)
    return jsonify({'success': True, 'final': new_final})


@app.route('/api/acknowledge/<path:name>', methods=['POST'])
def api_acknowledge(name):
    """변경 알람 확인 처리 (changed 플래그 초기화)"""
    ratings = load_ratings()
    if name in ratings:
        for ag in AGENCIES:
            ratings[name][f'{ag}_changed'] = False
        ratings[name]['final_changed'] = False
        save_ratings(ratings)
    return jsonify({'success': True})


@app.route('/api/acknowledge_all', methods=['POST'])
def api_acknowledge_all():
    ratings = load_ratings()
    for name, entry in ratings.items():
        if name == '_meta':
            continue
        for ag in AGENCIES:
            entry[f'{ag}_changed'] = False
        entry['final_changed'] = False
    save_ratings(ratings)
    return jsonify({'success': True})


@app.route('/api/institutions', methods=['POST'])
def api_add_institution():
    data = request.get_json()
    category = data.get('category', '').strip()
    name = data.get('name', '').strip()
    if not category or not name:
        return jsonify({'success': False, 'message': '카테고리와 기관명을 입력하세요'}), 400
    institutions = load_institutions()
    if category not in institutions:
        return jsonify({'success': False, 'message': '올바르지 않은 카테고리'}), 400
    if name in [i['name'] for i in institutions[category]]:
        return jsonify({'success': False, 'message': '이미 등록된 기관'}), 400
    institutions[category].append({'name': name})
    with open(INSTITUTIONS_FILE, 'w', encoding='utf-8') as f:
        json.dump(institutions, f, ensure_ascii=False, indent=2)
    return jsonify({'success': True})


@app.route('/api/institutions/<path:name>', methods=['DELETE'])
def api_delete_institution(name):
    institutions = load_institutions()
    found = False
    for cat in institutions:
        before = len(institutions[cat])
        institutions[cat] = [i for i in institutions[cat] if i['name'] != name]
        if len(institutions[cat]) < before:
            found = True
    if not found:
        return jsonify({'success': False, 'message': '기관 없음'}), 404
    with open(INSTITUTIONS_FILE, 'w', encoding='utf-8') as f:
        json.dump(institutions, f, ensure_ascii=False, indent=2)
    ratings = load_ratings()
    ratings.pop(name, None)
    save_ratings(ratings)
    return jsonify({'success': True})


# ── Scheduler ─────────────────────────────────────────────────────────

def scheduled_job():
    logger.info('스케줄 실행: 신용등급 자동 조회 시작')
    with app.app_context():
        try:
            import requests as req
            req.post(f'http://localhost:{PORT}/api/refresh', timeout=300)
        except Exception:
            logger.exception('스케줄 실행 오류')


if __name__ == '__main__':
    import atexit
    from scraper import close_browser
    atexit.register(close_browser)

    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        scheduler = BackgroundScheduler(timezone='Asia/Seoul')
        scheduler.add_job(scheduled_job, 'cron', hour=8, minute=0, id='morning_update')
        scheduler.start()
        logger.info('스케줄러 시작: 매일 오전 8시 자동 조회')
    except ImportError:
        logger.warning('APScheduler 미설치')

    from waitress import serve
    serve(app, host='0.0.0.0', port=PORT)
