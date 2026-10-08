/* 미사용 자동 로그아웃 — app.py 의 _inject_screen_guard 가 로그인 사용자 HTML 에 삽입.
 * 클릭·키 입력·스크롤·터치가 있으면 서버에 /api/heartbeat 를 보내(최대 1분에 1회) 세션을 연장하고,
 * 마지막 활동 후 data-idle-min 분이 지나면 /logout 으로 이동한다.
 * 마지막 활동 시각은 localStorage 로 같은 브라우저의 모든 탭·iframe 이 공유한다. */
(function () {
  'use strict';
  var me = document.currentScript;
  var idleMs = (parseInt(me && me.getAttribute('data-idle-min'), 10) || 30) * 60000;
  var KEY = 'lastActivityAt';
  var localLast = Date.now(), lastBeat = Date.now(), done = false;

  function getLast() {
    var v = 0;
    try { v = parseInt(localStorage.getItem(KEY), 10) || 0; } catch (e) {}
    return Math.max(v, localLast);
  }

  function activity() {
    var now = Date.now();
    localLast = now;
    try { localStorage.setItem(KEY, String(now)); } catch (e) {}
    if (now - lastBeat > 60000) {
      lastBeat = now;
      try {
        fetch('/api/heartbeat', { method: 'POST', credentials: 'same-origin' })
          .then(function (r) { if (r.status === 401) logout(); })
          .catch(function () {});
      } catch (e) {}
    }
  }

  function logout() {
    if (done) return;
    done = true;
    try { window.top.location.href = '/logout?expired=1'; }
    catch (e) { location.href = '/logout?expired=1'; }
  }

  ['mousedown', 'keydown', 'wheel', 'touchstart', 'scroll'].forEach(function (ev) {
    window.addEventListener(ev, activity, { capture: true, passive: true });
  });
  activity();

  // 만료 판단은 최상위 창에서만(iframe 은 활동 기록만 담당).
  if (window.top === window) {
    setInterval(function () { if (Date.now() - getLast() > idleMs) logout(); }, 15000);
  }
})();
