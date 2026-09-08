/* ============================================================
   원리금보장상품 금리 데이터 파생 로직 (공용 모듈)
   ------------------------------------------------------------
   금리관리(pension.html)와 상품제안관리(proposal.html)는 **같은 규칙**으로
   상품·금리를 만들어내야 한다. 예전에는 두 화면이 서로 다른 경로를 썼다.
     · 금리관리    : raw 재그룹핑 + 정기예금 병합 + 판매중단 상품 제외 + 수기금리(overrides) 덧씌움
     · 상품제안관리: 저장된 rows 를 그대로 사용
                     → 금리관리에서 감춘 상품(실버연금예금·이율변동형 등)이 제안 후보로 뜨고,
                       수기로 고친 금리가 제안 목록에 반영되지 않았다.
   그래서 파생 로직을 이 파일 하나로 모으고, 두 화면 모두 derivePensionRows()를 쓴다.
   (classic script — 전역 함수로 노출. 두 페이지 모두 자기 <script> 앞에서 로드할 것)
   ============================================================ */

const MONTHS = [3,6,12,18,24,30,36,48,60];
const MONTH_LABEL = {3:'3개월',6:'6개월',12:'1년',18:'18개월',24:'2년',30:'30개월',36:'3년',48:'4년',60:'5년'};
const SECTOR_ORDER = ['은행','저축은행','생명보험','손해보험','증권','기타'];

/* ---------- parsing helpers ---------- */
function parseRate(v){
  if(v===null||v===undefined) return null;
  let s=String(v).trim();
  if(s===''||s==='-'||s==='0'||s==='0.0'||s.toUpperCase()==='N/A') return null;
  s=s.split('~')[0].trim();                 // range -> first
  const f=parseFloat(s);
  if(isNaN(f)||f===0) return null;
  return f;
}
function normFamily(prod){
  if(!prod) return '(미지정)';
  let parts=String(prod).split('/');
  if(parts.length>1) parts=parts.slice(1);   // drop org prefix
  let p=parts.join('/');
  p=p.replace(/\(디폴트옵션[^)]*\)/g,'');       // (디폴트옵션용) 등
  p=p.replace(/디폴트옵션(전용|용)?\s*/g,'');
  let segs=p.split('/');
  while(segs.length>1){
    const last=segs[segs.length-1].trim();
    const isMat = /^\d+\s*년(\s*\d+\s*개월)?$/.test(last) ||
                  /^\d+\s*개월$/.test(last) ||
                  /\d+\s*(일|개월|년).*(~|미만|이상|초과)/.test(last) ||
                  /^\d+년\s*~/.test(last);
    if(isMat) segs.pop(); else break;
  }
  p=segs.join('/').replace(/\s+/g,' ').trim();
  return p||String(prod);
}
function parseMaturities(mat){
  if(mat===null||mat===undefined) return [];
  if(typeof mat==='number') { const m=Math.round(mat); return [MONTHS.includes(m)?m:null]; }
  return String(mat).split('/').map(t=>{
    t=t.trim();
    if(/^\d+$/.test(t)){ const m=parseInt(t,10); return MONTHS.includes(m)?m:null; }
    return null;
  });
}
function rateList(v,n){
  if(v===null||v===undefined) return Array(n).fill(null);
  const s=String(v);
  if(s.indexOf('/')>=0){ const t=s.split('/').map(parseRate); while(t.length<n)t.push(null); return t; }
  return Array(n).fill(parseRate(s));
}

/* ── 특수상품(금리연동형 / 기간식·만기지정·일단위) 판별 & 추출 ── */
function isRateLinkedProd(prod){ return /금리연동형/.test(String(prod||'')); }
function isRangeMat(mat){ return (typeof mat==='string') && /(~|미만|이상|초과)/.test(mat); }
function firstRate(a,b,c){ const v=parseRate(a); if(v!=null) return v; const w=parseRate(b); if(w!=null) return w; return parseRate(c); }
function splitVals(v,n){
  if(v===null||v===undefined) return Array(n).fill(null);
  const s=String(v);
  if(s.indexOf('/')>=0){ const t=s.split('/').map(parseRate); while(t.length<n) t.push(null); return t; }
  return Array(n).fill(parseRate(s));
}
/* raw → { rateLinked:[{sector,org,fam,rate}], period:[{sector,org,fam,mat,db,dc,irp}] } (구간마다 1행) */
function extractSpecial(raw){
  const rateLinked=[], period=[];
  for(const r of (raw||[])){
    if(!r.org) continue;
    const prod=String(r.prod||'');
    if(isRateLinkedProd(prod)){
      rateLinked.push({sector:r.sector, org:String(r.org).trim(), fam:normFamily(prod), rate:firstRate(r.db,r.dc,r.irp)});
    } else if(isRangeMat(r.mat)){
      const mats=String(r.mat).split('/').map(s=>s.trim());
      const dbs=splitVals(r.db,mats.length), dcs=splitVals(r.dc,mats.length), irs=splitVals(r.irp,mats.length);
      for(let i=0;i<mats.length;i++){
        period.push({sector:r.sector, org:String(r.org).trim(), fam:normFamily(prod), mat:mats[i], db:dbs[i], dc:dcs[i], irp:irs[i]});
      }
    }
  }
  return {rateLinked, period};
}

/* ---------- core: rows -> grouped management rows ---------- */
function buildGroups(raw){
  // raw: array of {sector,org,prod,mat,db,dc,irp}
  const groups=new Map();
  for(const r of raw){
    if(!r.org) continue;
    const prod = r.prod ? String(r.prod) : '';
    if(isRateLinkedProd(prod) || isRangeMat(r.mat)) continue;   // 특수상품은 메인 그리드 제외(→ 기타 탭에서 표시)
    const sec = r.sector==='기타' ? '은행' : r.sector;            // 기타 업권 → 은행으로 이동
    const isDefault = prod.indexOf('디폴트옵션')>=0;

    /* ── 정기예금 표기 규칙 ──
       정기예금(비디폴트, 증권/발행어음 제외) 중 '일단위/월단위' 코호트 변형은 화면에서 숨김.
       (국민은행 특례: 상품명 정기예금(일단위)/C 의 금리를 3개월·6개월 양쪽에 반영)
       그 외(기본 org/정기예금, 만기분할, 우체국·카카오 등 서술형)는 '정기예금'으로 통합. */
    let fam, monthsOverride=null, depVariant=false;
    const isDeposit = prod.indexOf('정기예금')>=0 && !isDefault
                      && sec!=='증권' && prod.indexOf('발행어음')<0;
    if(isDeposit){
      const rest = prod.split('/').slice(1).join('/');   // 기관명 제거 후
      if(/일단위|월단위/.test(rest)){
        // 국민은행 특례: 정기예금(일단위)/C → 그 금리를 3개월·6개월에 채움
        if(r.org==='국민은행' && /\(일단위\)\/C$/.test(rest)){ fam='정기예금'; monthsOverride=[3,6]; }
        else continue;   // 그 외 코호트 변형 숨김
      } else {
        fam='정기예금';
        // 기본 정기예금과 같은 fam으로 통합되지만 금리가 다른 변형 상품(예: 정기예금(만기금리형)).
        // 이런 변형은 기본 정기예금이 채운 만기값을 덮어쓰면 안 됨(안 그러면 입력 행 순서에 따라
        // 1년=만기금리형 값이 기본 정기예금 값을 덮어써 오표기됨 — 기업은행 8월 사례).
        if(/\([^)]*\)/.test(rest)) depVariant=true;
      }
    } else {
      fam = normFamily(prod);
    }

    const key=[sec,r.org,fam].join('||');
    let g=groups.get(key);
    if(!g){ g={sector:sec,org:r.org,fam:fam,db:{},dc:{},def:null,srcCount:0,names:[],dbBase:new Set(),dcBase:new Set()}; groups.set(key,g); }
    g.srcCount++; if(prod) g.names.push(prod);
    let months, n, dbR, dcR, irpR;
    if(monthsOverride){
      months=monthsOverride; n=months.length;   // [3,6] → 같은 금리를 두 만기에 채움
      const dbv=parseRate(r.db), dcv=parseRate(r.dc), irpv=parseRate(r.irp);
      dbR=Array(n).fill(dbv); dcR=Array(n).fill(dcv); irpR=Array(n).fill(irpv);
    } else {
      months=parseMaturities(r.mat); n=months.length;
      dbR=rateList(r.db,n); dcR=rateList(r.dc,n); irpR=rateList(r.irp,n);
    }
    for(let i=0;i<n;i++){
      const m=months[i]; if(m===null) continue;
      const dcv = dcR[i]!=null ? dcR[i] : irpR[i];
      if(isDefault){ if(m===36 && dcv!=null){ g.def=dcv; } }
      else {
        // depVariant(정기예금 변형)은 기본 정기예금이 이미 채운 만기만 건너뛰고 빈 만기만 채움.
        // 기본(정기예금)은 항상 기록하고 dbBase/dcBase에 표시 → 이후 변형이 덮어쓰지 못함(행 순서 무관).
        if(dbR[i]!=null){
          if(depVariant){ if(!g.dbBase.has(m)) g.db[m]=dbR[i]; }
          else { g.db[m]=dbR[i]; if(isDeposit) g.dbBase.add(m); }
        }
        if(dcv!=null){
          if(depVariant){ if(!g.dcBase.has(m)) g.dc[m]=dcv; }
          else { g.dc[m]=dcv; if(isDeposit) g.dcBase.add(m); }
        }
      }
    }
    if(isDefault && g.def===null){                      // fallback: any dc/irp value
      for(let i=0;i<n;i++){ const dcv=dcR[i]!=null?dcR[i]:irpR[i]; if(dcv!=null){ g.def=dcv; break; } }
      if(g.def===null){ const v=parseRate(r.dc)??parseRate(r.irp); if(v!=null) g.def=v; }
    }
  }
  const rows=[...groups.values()];
  // 증권사 상품 순서: ELB(0) → DLB(1) → RP·환매조건부(2) → 기타(3). 기관별로 ELB/DLB 먼저, RP 나중
  const prodRank=(fam)=>{ const f=String(fam); if(/ELB/.test(f)) return 0; if(/DLB/.test(f)) return 1; if(/RP|환매조건부/.test(f)) return 2; return 3; };
  rows.sort((a,b)=>{
    const sa=SECTOR_ORDER.indexOf(a.sector), sb=SECTOR_ORDER.indexOf(b.sector);
    if(sa!==sb) return (sa<0?99:sa)-(sb<0?99:sb);
    if(a.org!==b.org) return String(a.org).localeCompare(b.org,'ko');
    const ra=prodRank(a.fam), rb=prodRank(b.fam);
    if(ra!==rb) return ra-rb;                       // 같은 기관 내: ELB/DLB 먼저, RP 다음
    return String(a.fam).localeCompare(b.fam,'ko');
  });
  return rows;
}

/* 정기예금 표시 정규화 — 신규/예전저장분 어디든 조회 시점에 적용.
   ① 정기예금(비증권·비발행어음) 중 일단위/월단위 코호트 행은 제외
   ② 나머지 정기예금 계열(정기예금, 정기예금/3~6, 정기예금/12~18, 우체국퇴직연금정기예금/A 등)은
      기관별로 하나의 '정기예금' 행으로 병합(만기 union). 상품명 뒤 추가정보가 있어도 통합. */
function isDepositFam(r){
  const f=String(r.fam);
  return f.indexOf('정기예금')>=0 && r.sector!=='증권' && f.indexOf('발행어음')<0;
}
/* 화면에서 제외할 상품(판매중단 등) */
function isExcludedProduct(r){
  const t=String(r.fam)+' '+((r.names||[]).join(' '));
  if(/실버연금예금/.test(t)) return true;   // NH실버연금예금(판매중단) 제외
  // 생보·손보: 이율변동형·금리혼합형보험 제외
  if((r.sector==='생명보험'||r.sector==='손해보험') && /이율변동형|금리혼합형/.test(t)) return true;
  return false;
}
function normalizeDepositRows(rows){
  const out=[]; const merged=new Map();
  for(const r of rows){
    if(!isDepositFam(r)){ out.push(r); continue; }
    if(/일단위|월단위/.test(String(r.fam))) continue;   // 코호트 숨김
    const key=r.sector+'||'+r.org;
    let m=merged.get(key);
    if(!m){ m={sector:r.sector,org:r.org,fam:'정기예금',db:{},dc:{},def:null,srcCount:0,names:[]}; merged.set(key,m); out.push(m); }
    m.srcCount++; if(r.names) m.names.push(...r.names);
    for(const k in r.db){ if(m.db[k]==null && r.db[k]!=null) m.db[k]=r.db[k]; }
    for(const k in r.dc){ if(m.dc[k]==null && r.dc[k]!=null) m.dc[k]=r.dc[k]; }
    if(m.def==null && r.def!=null) m.def=r.def;
  }
  return out;
}

/* ========== 금리 수기 입력·수정(override 층) ==========
   업로드 원본(raw)은 조회마다 재그룹핑되므로, 수기값은 rec.overrides에 따로 저장해
   화면 생성 후 덧씌운다. rowKey = sector||org||fam (한 화면행 식별).
   저장은 기존 save()가 localStorage + 서버(pension_store)로 동기화(RM은 서버서 차단). */
function penRowKey(r){ return [r.sector,r.org,r.fam].join('||'); }

function applyOverrides(rows, ov){
  rows.forEach(r=>{ r.manual={db:{},dc:{},def:false}; });   // 수기 표시 초기화
  if(!ov) return;
  rows.forEach(r=>{
    const o=ov[penRowKey(r)]; if(!o) return;
    if(o.db) for(const k in o.db){ const v=o.db[k]; if(v!=null){ r.db[+k]=v; r.manual.db[+k]=true; } }
    if(o.dc) for(const k in o.dc){ const v=o.dc[k]; if(v!=null){ r.dc[+k]=v; r.manual.dc[+k]=true; } }
    if(o.def!=null){ r.def=o.def; r.manual.def=true; }
  });
}

/* 저장 레코드(rec) → 화면/제안에 쓰는 최종 상품 행 목록.
   금리관리·상품제안관리는 반드시 이 함수로 행을 만든다(두 화면 결과 일치 보장).
   raw(업로드 원본)가 있으면 최신 규칙으로 재그룹핑하고, 없으면 저장된 rows를 쓴다. */
function derivePensionRows(rec){
  if(!rec) return [];
  const base = rec.raw ? buildGroups(rec.raw) : (rec.rows || []);
  const rows = normalizeDepositRows(base).filter(r=>!isExcludedProduct(r));
  applyOverrides(rows, rec.overrides);
  return rows;
}

/* 파생 행 → 만기별 1행으로 평탄화한 DB제도 상품 목록(상품제안관리 좌측 목록의 원천).
   [{sector,org,fam,months,period,rate}] · 업권→기관→만기 순 정렬. */
function flattenDbRows(rows){
  const out=[];
  for(const r of (rows||[])){
    const db=r.db||{};
    for(const m of MONTHS){
      const v=db[m];
      if(v==null) continue;
      out.push({sector:r.sector, org:r.org, fam:r.fam, months:m, period:MONTH_LABEL[m], rate:v});
    }
  }
  out.sort((a,b)=>{
    const s=SECTOR_ORDER.indexOf(a.sector)-SECTOR_ORDER.indexOf(b.sector);
    if(s!==0) return s;
    const o=String(a.org).localeCompare(b.org,'ko'); if(o!==0) return o;
    return a.months-b.months;
  });
  return out;
}

/* ============================================================
   이달의 제안상품 자동 승계
   ------------------------------------------------------------
   기준월 금리를 새로 업로드하면(예: 9월) 상품제안관리의 '이달의 제안상품'은 빈 상태였고,
   연금컨설팅팀이 매달 처음부터 다시 골라 [등록]을 눌러야 모바일 제안카드에 떴다.
   → 새 기준월이 들어오면 **직전에 제안목록이 있던 달**의 상품들을 그대로 이어받고,
     금리만 새 기준월 값으로 갱신해 자동 등록한다. 없어진 상품은 빼고 알려준다.
   ============================================================ */

/* 제안상품 키. app.py 주석 · proposal.html rowKey()와 같은 규칙: sector|org|fam|months */
function proposalRowKey(r){ return [r.sector,r.org,r.fam,r.months].join('|'); }

/* 예전에 등록된 항목의 상품명이 지금 규칙과 달라도 이어받도록 하는 보조 키.
   정기예금 계열은 기관별 1행('정기예금')으로 병합되므로, 변형 이름으로 등록된
   과거 항목(예: '정기예금(개월)DB', '퇴직연금 정기예금')도 같은 상품으로 본다. */
function proposalMatchKeys(r){
  const keys=[proposalRowKey(r)];
  if(isDepositFam(r)) keys.push([r.sector,r.org,'정기예금',r.months].join('|'));
  return keys;
}

/* 해당 기준월의 DB제도 상품을 키로 찾을 수 있는 색인. {키 -> 행} */
function dbRowIndex(rows){
  const idx=new Map();
  for(const r of rows) for(const k of proposalMatchKeys(r)) if(!idx.has(k)) idx.set(k,r);
  return idx;
}

/* 등록된 제안항목들을 해당 기준월의 현재 금리로 다시 맞춘다.
   반환 {items, dropped, updated, changed}
     items   : 살아있는 상품(금리 갱신본)
     dropped : 그 기준월에 없는 상품(제외됨)
     updated : 금리·상품명이 실제로 달라진 건수
     changed : 위 둘 중 하나라도 있어 저장본과 달라졌는지 */
function reprice(items, dbRows){
  const idx=dbRowIndex(dbRows||[]);
  const out=[], dropped=[], seen=new Set(); let updated=0, dedup=0;
  for(const it of (items||[])){
    let hit=null;
    for(const k of proposalMatchKeys(it)){ hit=idx.get(k); if(hit) break; }
    if(!hit){ dropped.push(it); continue; }
    // 상품명이 병합되면서(예: 정기예금 변형 → '정기예금') 같은 상품이 둘로 등록돼 있을 수 있다.
    const key=proposalRowKey(hit);
    if(seen.has(key)){ dedup++; continue; }
    seen.add(key);
    if(hit.rate!==it.rate || hit.fam!==it.fam) updated++;
    out.push({sector:hit.sector, org:hit.org, fam:hit.fam, months:hit.months,
              period:hit.period, rate:hit.rate});
  }
  return {items:out, dropped, updated, changed: !!(dropped.length || updated || dedup)};
}

/* 새 기준월(month)의 제안목록을 직전 달에서 승계해 만든다.
   반환: {from, items, dropped} · 승계할 이전 달이 없으면 null. */
function carryOverProposals(store, proposals, month){
  const rec = store && store[month];
  if(!rec) return null;
  const prev = Object.keys(proposals||{})
      .filter(m => m < month && Array.isArray(proposals[m]) && proposals[m].length)
      .sort();
  if(!prev.length) return null;
  const from = prev[prev.length-1];
  const r = reprice(proposals[from], flattenDbRows(derivePensionRows(rec)));
  return {from, items:r.items, dropped:r.dropped};
}
