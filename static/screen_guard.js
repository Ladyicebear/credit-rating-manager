/* 화면 캡처 억제(관리자=연금컨설팅팀 제외) — app.py 의 _inject_screen_guard 가 비관리자 HTML 에만 삽입.
 * 웹페이지는 OS 스크린샷을 완전히 막을 수 없으므로, 아래 조합으로 "어렵게 + 추적 가능하게" 만든다.
 *  1) 창이 포커스를 잃거나 숨겨지면 화면 가림(캡처 도구 Win+Shift+S·캡처 앱 실행 시 대부분 포커스 이동)
 *  2) PrintScreen 키 감지 → 순간 가림 + 클립보드 덮어쓰기
 *  3) 인쇄(Ctrl+P·PDF 저장) 차단, Ctrl+S 저장 차단
 *  4) 사용자 아이디·시각 워터마크 → 휴대폰 촬영 등 막을 수 없는 경우에도 유출자 식별 가능
 * iframe 안에서는 키 감지만 하고, 가림/워터마크는 최상위 창이 담당(iframe 내용까지 함께 가려짐). */
(function () {
  'use strict';
  var me = document.currentScript;
  var label = (me && me.getAttribute('data-user')) || '';
  var isTop = window.top === window;

  function topGuard() {
    try { return window.top.__screenGuard || null; } catch (e) { return null; }
  }

  // ── 키 차단 (모든 frame) ──
  function onKey(e) {
    var k = (e.key || '').toLowerCase();
    if (k === 'printscreen' || e.keyCode === 44) {
      try { navigator.clipboard && navigator.clipboard.writeText(''); } catch (_) {}
      var g = topGuard();
      if (g) g.flash();
      return;
    }
    if ((e.ctrlKey || e.metaKey) && (k === 'p' || k === 's')) {
      e.preventDefault();
      e.stopPropagation();
    }
    // macOS 캡처 단축키(Cmd+Shift+3/4/5) — 감지되면 가림
    if (e.metaKey && e.shiftKey && (k === '3' || k === '4' || k === '5')) {
      var g2 = topGuard();
      if (g2) g2.flash();
    }
  }
  window.addEventListener('keydown', onKey, true);
  window.addEventListener('keyup', onKey, true);

  // ── 인쇄 차단 (모든 frame) ──
  var st = document.createElement('style');
  st.textContent = '@media print{html,body{display:none!important}}';
  (document.head || document.documentElement).appendChild(st);

  if (!isTop) return;

  // ── 가림막 + 워터마크 (최상위 창) ──
  var shield, mark, hidden = false, flashTimer = null;

  function build() {
    shield = document.createElement('div');
    shield.setAttribute('aria-hidden', 'true');
    shield.style.cssText = 'position:fixed;inset:0;z-index:2147483647;background:#111;color:#fff;' +
      'display:none;align-items:center;justify-content:center;font:600 16px/1.6 sans-serif;' +
      'text-align:center;padding:24px';
    shield.textContent = '보안을 위해 화면이 가려졌습니다. 화면을 클릭하면 다시 표시됩니다.';
    document.documentElement.appendChild(shield);

    if (label) {
      var d = new Date(), z = function (n) { return (n < 10 ? '0' : '') + n; };
      var stamp = label + ' · ' + d.getFullYear() + '-' + z(d.getMonth() + 1) + '-' + z(d.getDate()) +
        ' ' + z(d.getHours()) + ':' + z(d.getMinutes());
      var svg = '<svg xmlns="http://www.w3.org/2000/svg" width="320" height="200">' +
        '<text x="10" y="110" transform="rotate(-25 160 100)" fill="rgba(120,120,120,0.13)" ' +
        'font-family="sans-serif" font-size="15">' +
        stamp.replace(/[&<>"]/g, function (c) { return '&#' + c.charCodeAt(0) + ';'; }) +
        '</text></svg>';
      mark = document.createElement('div');
      mark.setAttribute('aria-hidden', 'true');
      mark.style.cssText = 'position:fixed;inset:0;z-index:2147483646;pointer-events:none;' +
        'background-image:url("data:image/svg+xml;charset=utf-8,' + encodeURIComponent(svg) + '")';
      document.documentElement.appendChild(mark);
    }
  }

  function setHidden(h) {
    if (!shield || h === hidden) return;
    hidden = h;
    shield.style.display = h ? 'flex' : 'none';
  }

  // 포커스가 iframe 으로 옮겨간 경우는 document.hasFocus() 가 true 이므로 가리지 않는다.
  function check() {
    if (flashTimer) return;
    setHidden(document.visibilityState === 'hidden' || !document.hasFocus());
  }

  window.__screenGuard = {
    flash: function () {
      setHidden(true);
      clearTimeout(flashTimer);
      flashTimer = setTimeout(function () { flashTimer = null; check(); }, 1500);
    }
  };

  function start() {
    build();
    window.addEventListener('blur', function () { setTimeout(check, 0); });
    window.addEventListener('focus', check);
    document.addEventListener('visibilitychange', check);
    shield.addEventListener('mousedown', function () { window.focus(); setTimeout(check, 0); });
    setInterval(check, 400);
    check();
  }
  if (document.readyState !== 'loading') start();
  else document.addEventListener('DOMContentLoaded', start);
})();
