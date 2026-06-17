import os
import json
import logging
import threading
from datetime import datetime
from markupsafe import Markup
from flask import Flask, render_template, jsonify, request

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)

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


# ── Build view data ───────────────────────────────────────────────────

def build_row(name: str, category: str, inst_data: dict) -> dict:
    is_insurance = category in INSURANCE_CATEGORIES

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
        'changes': changes,
        'any_changed': any_changed,
        'scrape_status': inst_data.get('scrape_status', ''),
        'updated': inst_data.get('updated', ''),
        'rep_type': rep_type,
        'rep_type_label': get_rating_type_label(rep_type),
    }


def build_response_data(institutions: dict, ratings: dict) -> dict:
    category_order = ['증권사', '시중은행', '지방은행', '저축은행', '손해보험', '생명보험', '기타']
    result = {}
    for category in category_order:
        items = institutions.get(category, [])
        rows = []
        for inst in items:
            name = inst['name']
            inst_data = ratings.get(name, {})
            rows.append(build_row(name, category, inst_data))
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

@app.route('/')
def index():
    institutions = load_institutions()
    ratings = load_ratings()
    data = build_response_data(institutions, ratings)
    meta = ratings.get('_meta', {})

    # 변경 알람 요약
    total_changed = sum(
        1 for rows in data.values()
        for row in rows if row['any_changed']
    )

    return render_template(
        'index.html',
        data=data,
        last_updated=meta.get('updated', '-'),
        scrape_summary=meta.get('scrape_summary', ''),
        total_changed=total_changed,
        rating_scale=RATING_SCALE,
        agencies=AGENCIES,
        agency_labels=AGENCY_LABELS,
    )


@app.route('/api/ratings')
def api_ratings():
    institutions = load_institutions()
    ratings = load_ratings()
    data = build_response_data(institutions, ratings)
    meta = ratings.get('_meta', {})
    return jsonify({'data': data, 'last_updated': meta.get('updated', '-')})


def _apply_scrape_results(scrape_result: dict) -> tuple[int, int]:
    """스크래핑 결과를 ratings.json에 반영. (success_count, changed_count) 반환"""
    ratings = load_ratings()
    success_count = 0
    changed_count = 0
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
            if new_rating:
                if old_rating and old_rating != new_rating:
                    entry[f'{ag}_prev'] = old_rating
                    entry[f'{ag}_changed'] = True
                    changed_count += 1
                entry[ag] = new_rating
                entry[f'{ag}_eval_date'] = new_data.get(f'{ag}_eval_date', prev_entry.get(f'{ag}_eval_date', ''))
                entry[f'{ag}_type']      = new_data.get(f'{ag}_type',      prev_entry.get(f'{ag}_type', ''))
                any_ag_updated = True
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
        ratings[name] = entry
    ratings['_meta'] = {
        'updated': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'scrape_summary': f'조회 성공 {success_count}건 / 등급 변경 감지 {changed_count}건',
    }
    save_ratings(ratings)
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
            req.post('http://localhost:5000/api/refresh', timeout=300)
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
    serve(app, host='0.0.0.0', port=5000)
