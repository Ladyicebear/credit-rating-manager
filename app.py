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


# ── 과거 금리 추이(내부 보관 데이터) ──
#   data/rate_history.xlsx : 원본 엑셀 바이트 그대로 보관(다운로드용)
#   data/rate_history.json : 구조화 테이블(향후 개발에서 재사용)
_RATE_HISTORY_XLSX = os.path.join(BASE_DIR, 'data', 'rate_history.xlsx')
_RATE_HISTORY_JSON = os.path.join(BASE_DIR, 'data', 'rate_history.json')


@app.route('/download/rate_history')
def download_rate_history():
    """'과거 금리 추이' 버튼 → 내부 보관된 원본 엑셀을 그대로 다운로드."""
    if not os.path.exists(_RATE_HISTORY_XLSX):
        return jsonify({'error': 'rate_history.xlsx not found'}), 404
    return send_file(_RATE_HISTORY_XLSX, as_attachment=True,
                     download_name='과거 금리 추이.xlsx',
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


@app.route('/api/rate_history')
def api_rate_history():
    """과거 금리 추이 구조화 테이블(JSON). 향후 개발에서 재사용."""
    if not os.path.exists(_RATE_HISTORY_JSON):
        return jsonify({'error': 'rate_history.json not found'}), 404
    with open(_RATE_HISTORY_JSON, encoding='utf-8') as f:
        return app.response_class(f.read(), mimetype='application/json')


@app.route('/download/rate_compare')
def download_rate_compare():
    """'금리비교하기' 버튼 → 과거 금리 추이 데이터로 5개 꺾은선 그래프가 든 엑셀 생성.
       완성본 Sheet2 사용(Sheet1은 #DIV/0! 다수로 값 누락). 컬럼: 1 DATE,
       2 증권평균, 3 은행평균, 4 생보평균, 5 손보평균, 6 저축평균, 7 원리금보장DB, 8 기준금리."""
    import openpyxl
    from collections import defaultdict
    from openpyxl.chart import LineChart, Reference
    from openpyxl.chart.axis import ChartLines
    from openpyxl.chart.text import RichText
    from openpyxl.chart.marker import Marker
    from openpyxl.chart.shapes import GraphicalProperties
    from openpyxl.drawing.line import LineProperties
    from openpyxl.drawing.text import (Paragraph, ParagraphProperties,
                                       CharacterProperties, RichTextProperties)
    if not os.path.exists(_RATE_HISTORY_JSON):
        return jsonify({'error': 'rate_history.json not found'}), 404
    with open(_RATE_HISTORY_JSON, encoding='utf-8') as f:
        hist = json.load(f)
    sh = hist['sheets']['Sheet2']
    headers = sh['headers']
    rows = sh['rows']

    NCOL = 8  # DATE ~ 기준금리 (Sheet2 인덱스 0~7)

    def num(v):
        if isinstance(v, bool):
            return None
        if isinstance(v, (int, float)):
            return round(v, 2)  # 금리 소수점 둘째자리
        try:
            return round(float(v), 2)
        except (TypeError, ValueError):
            return None  # '#DIV/0!' · 빈값 등은 공백 처리

    wb = openpyxl.Workbook()
    wsd = wb.active
    wsd.title = '데이터'
    wsd.append(headers[:NCOL])
    for r in rows:
        wsd.append([r[0]] + [num(r[i]) for i in range(1, NCOL)])
    last = wsd.max_row  # 헤더 포함 마지막 행
    cats = Reference(wsd, min_col=1, min_row=2, max_row=last)

    def x_label_style():
        """X축 날짜 라벨: 세로(-90°) 회전 + 작은 글꼴 → 겹침 없이 식별 가능."""
        return RichText(
            bodyPr=RichTextProperties(rot=-5400000, vert='horz'),
            p=[Paragraph(pPr=ParagraphProperties(defRPr=CharacterProperties(sz=700)))])

    # 다중 라인 색상 순서: 주황 → 네이비 → 하늘색 → 짙은 검정 (그 뒤 회색·진주황)
    LINE_COLORS = ['F68121', '123E7C', '5AB0E0', '1A1A1A', '9AA3AE', 'C77B2E']

    def style_series(ch, marker_size):
        """색상순서 라인. marker_size>0이면 원형 마커(흰 채움·색 테두리), 0이면 마커 없음(깔끔한 꺾은선)."""
        for i, s in enumerate(ch.series):
            col = LINE_COLORS[i % len(LINE_COLORS)]
            gp = GraphicalProperties()
            gp.line = LineProperties(solidFill=col, w=25400)   # 2pt
            s.graphicalProperties = gp
            if marker_size and marker_size > 0:
                mk = Marker(symbol='circle', size=marker_size)
                mgp = GraphicalProperties(solidFill='FFFFFF')
                mgp.line = LineProperties(solidFill=col)
                mk.graphicalProperties = mgp
                s.marker = mk
            else:
                s.marker = Marker(symbol='none')   # ①~⑤: 동그라미 제거
            s.smooth = False

    def make_chart(ws, cats_ref, last_row, title, cols, legend, marker_size, xrot):
        ch = LineChart()
        if title:
            ch.title = title
        ch.type = 'line'
        ch.style = 2
        ch.height = 13
        ch.width = 30
        ch.y_axis.delete = False
        ch.x_axis.delete = False
        ch.y_axis.numFmt = '0.00'
        ch.y_axis.majorGridlines = ChartLines()
        if xrot:
            ch.x_axis.txPr = x_label_style()    # 반월 시계열: 날짜 라벨 세로 회전
        ch.x_axis.tickLblPos = 'low'
        for c in cols:
            ref = Reference(ws, min_col=c, min_row=1, max_row=last_row)
            ch.add_data(ref, titles_from_data=True)
        ch.set_categories(cats_ref)
        style_series(ch, marker_size)
        if legend:
            ch.legend.position = legend
        else:
            ch.legend = None
        return ch

    # 반월 시계열 5종 (마커 작게, 상단범례/단일=제목)
    specs = [
        (None, '①업권평균비교', [2, 3, 4, 5, 6, 7], 't'),
        ('증권사 ELB', '②증권ELB', [2], None),
        ('원리금보장상품 금리 평균 (1년, DB)', '③원리금보장평균', [7], None),
        (None, '④증권vs기준금리', [2, 8], 't'),
        (None, '⑤업권별사업자금리', [2, 3, 4], 't'),
    ]
    for title, sheet_name, cols, legend in specs:
        wsc = wb.create_sheet(title=sheet_name)
        wsc.add_chart(make_chart(wsd, cats, last, title, cols, legend, 0, True), 'B2')

    # ⑥ 원리금보장 평균금리 vs 소비자물가상승률 (연도별 2015~, 첨부 그래프 형식)
    ann = defaultdict(list)
    for r in rows:
        y = str(r[0])[:4]
        v = num(r[6])   # 원리금보장상품금리 평균(1년, DB)
        if v is not None and y.isdigit():
            ann[y].append(v)
    rate_annual = {y: round(sum(vs) / len(vs), 2) for y, vs in ann.items()}
    cpi_annual = {}
    cd = _cpi_history_load()
    if cd:
        cpi_annual.update(cd.get('annual', {}))
        monthly = cd.get('monthly', {})
        if monthly:
            cy = sorted(set(k[:4] for k in monthly))[-1]
            vals = [v for k, v in monthly.items() if k[:4] == cy]
            cpi_annual[cy] = round(sum(vals) / len(vals), 2)
    wsd2 = wb.create_sheet(title='데이터_연도별')
    wsd2.append(['연도', '원리금보장상품 평균금리', '소비자물가상승률'])
    for y in range(2015, 2027):
        ys = str(y)
        wsd2.append([ys, rate_annual.get(ys), cpi_annual.get(ys)])
    last2 = wsd2.max_row
    cats2 = Reference(wsd2, min_col=1, min_row=2, max_row=last2)
    wsc6 = wb.create_sheet(title='⑥원리금보장vs물가상승률')
    #  범례는 상단('t') — 하단이면 연도 라벨과 겹침(참조 이미지=상단)
    wsc6.add_chart(make_chart(wsd2, cats2, last2, None, [2, 3], 't', 7, False), 'B2')

    bio = io.BytesIO()
    wb.save(bio)
    bio.seek(0)
    return send_file(bio, as_attachment=True, download_name='금리 비교.xlsx',
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


# ── 소비자물가지수(e-나라지표) 최신월 상승률 ──
_CPI_CACHE = {'ts': 0, 'data': None}
_CPI_URL = ('https://www.index.go.kr/unity/potal/eNara/sub/showStblGams3.do'
            '?stts_cd=106001&idx_cd=1060&freq=M&period=N')

# ── 과거 물가상승률(내부 보관: 연도별 + 올해 월별 누적) ──
_CPI_HISTORY_JSON = os.path.join(BASE_DIR, 'data', 'cpi_history.json')


def _cpi_history_load():
    if not os.path.exists(_CPI_HISTORY_JSON):
        return None
    with open(_CPI_HISTORY_JSON, encoding='utf-8') as f:
        return json.load(f)


def _cpi_history_save(d):
    with open(_CPI_HISTORY_JSON, 'w', encoding='utf-8') as f:
        json.dump(d, f, ensure_ascii=False, indent=1)


def _cpi_upsert_month(year, month, rate):
    """최신월 물가상승률을 과거 물가상승률 테이블(월별)에 누적 저장(매월 자동)."""
    try:
        d = _cpi_history_load()
        if not d:
            return
        key = '%s-%02d' % (str(year), int(month))
        d.setdefault('monthly', {})
        if d['monthly'].get(key) != rate:
            d['monthly'][key] = rate
            _cpi_history_save(d)
    except Exception:  # noqa
        logger.exception('물가상승률 월별 누적 저장 실패')


def _cpi_graph_points(d):
    """연도별(과거) + 올해(1월~최신월 평균) 그래프 포인트."""
    annual = d.get('annual', {})
    monthly = d.get('monthly', {})
    pts = [{'x': y, 'y': annual[y]} for y in sorted(annual)]
    if monthly:
        cy = sorted(set(k[:4] for k in monthly))[-1]     # 올해(월별 최신 연도)
        vals = [v for k, v in monthly.items() if k[:4] == cy]
        pts.append({'x': cy, 'y': round(sum(vals) / len(vals), 2),
                    'avg': True, 'months': len(vals)})
    return pts


@app.route('/api/cpi')
def cpi_rate():
    """지표누리 e-나라지표 소비자물가지수에서 최신월 소비자물가 상승률(전년동월비)과 전월대비 변동을 반환."""
    import time
    now = time.time()
    if _CPI_CACHE['data'] and now - _CPI_CACHE['ts'] < 6 * 3600:
        return jsonify(_CPI_CACHE['data'])
    try:
        import requests
        from bs4 import BeautifulSoup
        html = requests.get(_CPI_URL, timeout=12, headers={'User-Agent': 'Mozilla/5.0'}).text
        soup = BeautifulSoup(html, 'lxml')
        target = next((t for t in soup.find_all('table') if '소비자물가' in t.get_text()), None)
        heads = [c.get_text(strip=True) for c in target.select('thead th, thead td')]
        row = None
        for tr in target.select('tbody tr'):
            cells = [c.get_text(strip=True) for c in tr.find_all(['th', 'td'])]
            if cells and cells[0].replace(' ', '') == '소비자물가':
                row = cells
                break
        months = [(i, h) for i, h in enumerate(heads) if re.match(r'^\d{6}월$', h)]
        li = months[-1][0]
        rate = float(row[li])
        pi = months[-2][0] if len(months) >= 2 else li - 1
        prev = float(row[pi])
        data = {'ok': True, 'year': months[-1][1][:4], 'month': months[-1][1][4:6],
                'rate': rate, 'prev': prev, 'diff': round(rate - prev, 2),
                'source': 'e-나라지표 소비자물가지수'}
        _cpi_upsert_month(data['year'], data['month'], data['rate'])   # 매월 자동 누적
        _CPI_CACHE['ts'] = now
        _CPI_CACHE['data'] = data
        return jsonify(data)
    except Exception as e:  # noqa
        logger.exception('소비자물가지수 조회 실패')
        if _CPI_CACHE['data']:
            return jsonify(_CPI_CACHE['data'])
        return jsonify({'ok': False, 'message': str(e)})


@app.route('/api/cpi_history')
def api_cpi_history():
    """과거 물가상승률 그래프 데이터(연도별 + 올해 월평균)."""
    d = _cpi_history_load()
    if not d:
        return jsonify({'error': 'cpi_history not found'}), 404
    return jsonify({'label': d.get('label', '소비자물가상승률(%)'),
                    'points': _cpi_graph_points(d),
                    'annual': d.get('annual', {}), 'monthly': d.get('monthly', {})})


@app.route('/download/cpi_history')
def download_cpi_history():
    """'과거 물가상승률 다운로드' → 누적된 과거 물가상승률 엑셀 생성(연도별 + 올해 월별)."""
    import openpyxl
    d = _cpi_history_load()
    if not d:
        return jsonify({'error': 'cpi_history not found'}), 404
    annual = d.get('annual', {})
    monthly = d.get('monthly', {})
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = '소비자물가상승률'
    hdr = ['']
    vals = [d.get('label', '소비자물가상승률(%)')]
    for y in sorted(annual):
        hdr.append(y)
        vals.append(annual[y])
    for k in sorted(monthly):
        y, m = k.split('-')
        hdr.append('%s년 %d월' % (y, int(m)))
        vals.append(monthly[k])
    ws.append(hdr)
    ws.append(vals)
    bio = io.BytesIO()
    wb.save(bio)
    bio.seek(0)
    return send_file(bio, as_attachment=True, download_name='과거 물가상승률.xlsx',
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


@app.route('/api/pension_export', methods=['POST'])
def pension_export():
    """당월/전체 금리표(화면 형식) 엑셀 생성. 월별 1시트 + 금리연동형/기타 섹션 포함.
       payload: {filename, months:[{month:'YYYY-MM', rows:[{sector,org,fam,db,dc,def}], special:{rateLinked,period}}]}"""
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter
    payload = request.get_json(force=True, silent=True) or {}
    months = payload.get('months', [])
    if not months:
        return jsonify({'error': 'no data'}), 400
    MO = _PENSION_MONTHS                      # [3,6,12,18,24,30,36,48,60]
    MLABEL = {3: '3개월', 6: '6개월', 12: '1년', 18: '18개월', 24: '2년',
              30: '30개월', 36: '3년', 48: '4년', 60: '5년'}
    NC = 3 + len(MO) * 2 + 1                  # 22열 (업권·기관·상품 + DB9 + DC9 + 디폴트)

    thin = Side(style='thin', color='D0D5DD')
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    hdr_fill = PatternFill('solid', fgColor='F1F3F5')
    center = Alignment(horizontal='center', vertical='center', wrap_text=True)
    left = Alignment(horizontal='left', vertical='center')

    def num(v):
        try:
            if v is None or v == '' or v == '-':
                return None
            return round(float(v), 2)
        except (TypeError, ValueError):
            return None

    def style_row(ws, r, ncol, leftcols):
        for cc in range(1, ncol + 1):
            c = ws.cell(row=r, column=cc)
            c.border = border
            c.font = Font(size=9)
            c.alignment = left if cc in leftcols else center

    def section_header(ws, r, headers):
        for j, h in enumerate(headers):
            c = ws.cell(row=r, column=1 + j, value=h)
            c.fill = hdr_fill
            c.font = Font(bold=True, size=9, color='374151')
            c.alignment = center
            c.border = border

    wb = openpyxl.Workbook()
    wb.remove(wb.active)

    for entry in months:
        m = str(entry.get('month', ''))
        rows = entry.get('rows', []) or []
        special = entry.get('special', {}) or {}
        try:
            y, mo = m.split('-')
            label = '%s년 %d월' % (y, int(mo))
        except Exception:
            label = m or '금리현황'
        ws = wb.create_sheet(title=(label or '금리현황')[:31])

        # 제목
        tc = ws.cell(row=1, column=1, value='■ %s 퇴직연금 원리금보장상품 금리 현황' % label)
        tc.font = Font(bold=True, size=13)
        ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=NC)

        # 헤더(3~4행): 값 기입 → 스타일 → 병합
        hr1, hr2 = 3, 4
        ws.cell(row=hr1, column=1, value='업권')
        ws.cell(row=hr1, column=2, value='상품제공기관')
        ws.cell(row=hr1, column=3, value='상품구분')
        ws.cell(row=hr1, column=4, value='DB')
        ws.cell(row=hr1, column=4 + len(MO), value='DC / IRP')
        ws.cell(row=hr1, column=NC, value='디폴트\n옵션용 3년')
        for i, mm in enumerate(MO):
            ws.cell(row=hr2, column=4 + i, value=MLABEL[mm])
            ws.cell(row=hr2, column=4 + len(MO) + i, value=MLABEL[mm])
        for rr in (hr1, hr2):
            for cc in range(1, NC + 1):
                c = ws.cell(row=rr, column=cc)
                c.fill = hdr_fill
                c.font = Font(bold=True, size=9, color='374151')
                c.alignment = center
                c.border = border
        ws.merge_cells(start_row=hr1, start_column=1, end_row=hr2, end_column=1)
        ws.merge_cells(start_row=hr1, start_column=2, end_row=hr2, end_column=2)
        ws.merge_cells(start_row=hr1, start_column=3, end_row=hr2, end_column=3)
        ws.merge_cells(start_row=hr1, start_column=4, end_row=hr1, end_column=3 + len(MO))
        ws.merge_cells(start_row=hr1, start_column=4 + len(MO), end_row=hr1, end_column=3 + len(MO) * 2)
        ws.merge_cells(start_row=hr1, start_column=NC, end_row=hr2, end_column=NC)

        # 데이터
        dr = hr2 + 1
        for r in rows:
            db = r.get('db', {}) or {}
            dc = r.get('dc', {}) or {}
            ws.cell(row=dr, column=1, value=r.get('sector', ''))
            ws.cell(row=dr, column=2, value=r.get('org', ''))
            ws.cell(row=dr, column=3, value=r.get('fam', ''))
            for i, mm in enumerate(MO):
                ws.cell(row=dr, column=4 + i, value=num(db.get(str(mm), db.get(mm))))
                ws.cell(row=dr, column=4 + len(MO) + i, value=num(dc.get(str(mm), dc.get(mm))))
            ws.cell(row=dr, column=NC, value=num(r.get('def')))
            style_row(ws, dr, NC, (2, 3))
            for cc in range(4, NC + 1):
                ws.cell(row=dr, column=cc).number_format = '0.00'
            dr += 1

        # 금리연동형
        rl = special.get('rateLinked', []) or []
        if rl:
            dr += 1
            ws.cell(row=dr, column=1, value='· 금리연동형').font = Font(bold=True, size=10)
            dr += 1
            section_header(ws, dr, ['업권', '상품제공기관', '상품구분', '금리'])
            dr += 1
            for r in rl:
                ws.cell(row=dr, column=1, value=r.get('sector', ''))
                ws.cell(row=dr, column=2, value=r.get('org', ''))
                ws.cell(row=dr, column=3, value=r.get('fam', ''))
                ws.cell(row=dr, column=4, value=num(r.get('rate')))
                style_row(ws, dr, 4, (2, 3))
                ws.cell(row=dr, column=4).number_format = '0.00'
                dr += 1

        # 기타(만기지정식·일단위지정)
        pd = special.get('period', []) or []
        if pd:
            dr += 1
            ws.cell(row=dr, column=1, value='· 기타 (만기지정식·일단위지정)').font = Font(bold=True, size=10)
            dr += 1
            section_header(ws, dr, ['업권', '상품제공기관', '상품구분', '만기(개월)', 'DB', 'DC', 'IRP'])
            dr += 1
            for r in pd:
                ws.cell(row=dr, column=1, value=r.get('sector', ''))
                ws.cell(row=dr, column=2, value=r.get('org', ''))
                ws.cell(row=dr, column=3, value=r.get('fam', ''))
                ws.cell(row=dr, column=4, value=r.get('mat', ''))
                for k, key in enumerate(('db', 'dc', 'irp')):
                    ws.cell(row=dr, column=5 + k, value=num(r.get(key)))
                style_row(ws, dr, 7, (2, 3))
                for cc in range(5, 8):
                    ws.cell(row=dr, column=cc).number_format = '0.00'
                dr += 1

        # 열 너비
        ws.column_dimensions['A'].width = 10
        ws.column_dimensions['B'].width = 22
        ws.column_dimensions['C'].width = 22
        for i in range(len(MO) * 2):
            ws.column_dimensions[get_column_letter(4 + i)].width = 8
        ws.column_dimensions[get_column_letter(NC)].width = 10

    if not wb.sheetnames:
        wb.create_sheet(title='금리현황')
    bio = io.BytesIO()
    wb.save(bio)
    bio.seek(0)
    fname = payload.get('filename') or '퇴직연금 금리현황.xlsx'
    return send_file(bio, as_attachment=True, download_name=fname,
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


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
    for r in range(7, ws.max_row + 1):   # 양식 행 추가/변경에도 대응(상품구분 없는 행은 스킵)
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
