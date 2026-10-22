/* 求职工作台 · 前端逻辑
   拆成独立文件而不是内联在 index.html —— 内联单文件已到 794 行，
   再加台账就没人能维护了。 */
'use strict';

const $  = s => document.querySelector(s);
const $$ = s => [...document.querySelectorAll(s)];
const J  = async (u, o) => (await fetch(u, o)).json();
const post = (u, b) => J(u, {method:'POST', headers:{'Content-Type':'application/json'},
                              body: JSON.stringify(b || {})});

const SAL_LABEL = {verified:"已核验", heuristic:"待确认", unresolved:"未解析", na:"不标注"};
const VCLS = {"强烈推荐":"strong","可以投":"go","有风险":"risk","不建议":"no",
              "不要投":"no","无法评估":"na","边缘":"risk"};
const SCLS = {"强烈推荐":"good","可以投":"good","有风险":"mid","不建议":"bad",
              "不要投":"bad","无法评估":"mid","边缘":"mid"};
const LVL  = {hard:["sk-hard","硬性"], normal:["sk-norm","一般"], nice:["sk-nice","加分"]};
const ST_LABEL = {saved:"待投递", applied:"已投递", assessment:"笔试中",
                  interview:"面试中", offer:"已获 Offer", rejected:"已拒绝",
                  withdrawn:"已撤回"};

const state = { jobs:[], reports:{}, salary:{}, statusFilter:'',
                summary:{}, reminders:[], collect:null, showAll:false,
                llmReady:false, sem:{}, trust:{} };

function esc(s){
  return String(s ?? '').replace(/[&<>"']/g,
    c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
function toast(m){
  const t = $('#toast'); t.textContent = m; t.classList.add('on');
  clearTimeout(t._t); t._t = setTimeout(()=>t.classList.remove('on'), 2600);
}
function daysSince(iso){
  if(!iso) return null;
  const d = new Date(iso); if(isNaN(d)) return null;
  return Math.floor((Date.now()-d.getTime())/86400000);
}

/* ── Tab 切换 ───────────────────────────────────────────────
   用 location.hash 驱动，不只是内存里的一个变量：
     · 刷新后停在原来的 tab
     · 链接可以直接分享到某个 tab
     · 浏览器前进/后退能切换
   实测踩过：用 onclick 内存切换时，自动化环境点不动，
   改 hash 后既能正常工作也能通过直接访问 URL 验证。 */
const TABS = ['today','track','resume','parttime','insight','settings'];

function showTab(name, pushHash = true){
  if(!TABS.includes(name)) name = 'today';
  $$('.tab').forEach(t => t.classList.toggle('on', t.dataset.tab === name));
  $$('.pane').forEach(p => p.classList.toggle('on', p.id === 'pane-' + name));
  if(pushHash && location.hash.slice(1) !== name) location.hash = name;
  if(name === 'track') loadTracking();
  if(name === 'resume') loadResumes();
  if(name === 'insight') loadInsight();
  if(name === 'settings') loadDataStats();
  if(name === 'parttime') initPartTime();
}
$$('.tab').forEach(t => {
  t.onclick = e => { e.preventDefault(); showTab(t.dataset.tab); };
});
window.addEventListener('hashchange', () => showTab(location.hash.slice(1), false));

/* ── 抽屉 ───────────────────────────────────────────────── */
function openDrawer(kind){
  const d = $('#drawer' + kind[0].toUpperCase() + kind.slice(1));
  if(d) d.classList.add('on');
  if(kind === 'import') setTimeout(()=>$('#importText').focus(), 50);
}
function closeDrawer(kind){
  const d = $('#drawer' + kind[0].toUpperCase() + kind.slice(1));
  if(d) d.classList.remove('on');
}

/* ═══════════════════════════════════════════════════════════
   今天 —— 设计理念：回答「现在该干什么」，而不是罗列岗位
   ═══════════════════════════════════════════════════════════ */

/* 根据真实状态算出今天最重要的事。
   优先级刻意这样排：先处理已经投出去的，再看新岗位，
   最后才是采集 —— 因为「投了没回音」比「有没有新岗位」更值钱。 */
function computeFocus(summary, reminders, collect){
  const urgent = (reminders || []).filter(r => r.level === 'urgent');
  const soon   = (reminders || []).filter(r => r.level === 'soon');
  const todayNew = summary.today_new || 0;

  if(urgent.length){
    return {
      label: `今天有 ${urgent.length} 件事不能再拖`,
      title: urgent[0].title.replace(/^「(.+?)」/, '「$1」'),
      sub: urgent[0].action,
      cta: '去处理',
      act: 'track',
      n: urgent.length,
    };
  }
  if(soon.length){
    return {
      label: '有几件该做的跟进',
      title: soon[0].title,
      sub: soon[0].action,
      cta: '去跟进',
      act: 'track',
      n: soon.length,
    };
  }
  if(todayNew > 0){
    return {
      label: `${todayNew} 个新岗位`,
      title: todayNew >= 10 ? '今天新岗位不少，建议先看匹配度最高的几个'
                           : '今天有新的岗位进来了',
      sub: '已按「你能不能匹配」排好序，不用自己从一堆里挑',
      cta: '看岗位',
      act: 'today',
      n: todayNew,
    };
  }
  if(!summary.total){
    return {
      label: '还没有数据',
      title: '先导入几个岗位，或者配置每日采集',
      sub: '导入：把你从 BOSS 看到的岗位复制粘贴进来即可。采集：需要你自己的 Chrome 登录态。',
      cta: '导入岗位',
      act: 'import',
    };
  }
  const c = collect || {};
  if(c.status === 'never'){
    return {
      label: '数据停在 ' + (summary.total || 0) + ' 个',
      title: '配好每日采集，岗位才会自己变多',
      sub: '平台不提供「按发布时间回溯」，历史只能靠每天积累。',
      cta: '去配置',
      act: 'settings',
    };
  }
  if(c.status === 'fail'){
    return {
      label: '采集出问题了',
      title: c.text,
      sub: '可能是浏览器没开、登录态过期，或平台改版了。',
      cta: '去排查',
      act: 'settings',
    };
  }
  return {
    label: '暂时没有要处理的事',
    title: '今天没新岗位，投递也没有要跟进的',
    sub: '想扩大范围可以在设置里换关键词或城市。',
    cta: '看看全部岗位',
    act: 'today',
  };
}

function onFocusClick(){
  const a = state.focus && state.focus.act;
  if(a === 'import') return openDrawer('import');
  if(a === 'settings') return showTab('settings');
  if(a === 'track') return showTab('track');
  toggleAll();
}

function renderFocus(){
  const f = computeFocus(state.summary || {}, state.reminders || [], state.collect);
  state.focus = f;
  $('#focusLabel').textContent = f.label;
  $('#focusTitle').textContent = f.title;
  $('#focusSub').textContent = f.sub || '';
  $('#focusBtn').textContent = f.cta || '开始处理';
}

async function loadToday(){
  const [s, r, c] = await Promise.all([
    J('/api/summary'),
    J('/api/tracking/reminders'),
    J('/api/collect/health'),
  ]);
  state.summary = s; state.reminders = r.reminders || []; state.collect = c.health;

  $('#mNew').textContent = s.today_new || 0;
  $('#mFollow').textContent = state.reminders.length || 0;

  try{
    const t = await J('/api/tracking/summary');
    const ts = t.summary || {};
    $('#mOpen').textContent = ts.open || 0;
  }catch(e){ $('#mOpen').textContent = '—'; }

  renderFocus();
  renderReminders();
  renderCollect(c.health);

  // 顶栏提示
  const urgent = state.reminders.filter(x => x.level === 'urgent').length;
  const badge = $('#tabBadge');
  badge.hidden = urgent === 0; badge.textContent = urgent;
}

function renderReminders(){
  const box = $('#remindBox');
  const list = $('#remindList');
  const items = state.reminders || [];
  const urgent = items.filter(x => x.level === 'urgent').length;

  if(!items.length){ box.hidden = true; return; }
  box.hidden = false;
  box.classList.toggle('hot', urgent > 0);
  $('#todoSub').textContent = urgent
    ? `${urgent} 件紧急 · ${items.length - urgent} 件提醒`
    : `${items.length} 件提醒`;

  list.innerHTML = items.slice(0, 6).map(x => `
    <li onclick="openApp(${x.application_id})">
      <span class="lv lv-${x.level}">${x.level === 'urgent' ? '急' : '提'}</span>
      <span class="bd">
        <b>${esc(x.title)}</b>
        <small>${esc(x.detail)}　→　${esc(x.action)}</small>
      </span>
      <span class="go">处理 ›</span>
    </li>`).join('');
}

function renderCollect(h){
  const bar = $('#collectBar');
  const dot = $('#collectDot'), pill = $('#collectPill').querySelector('.dot');
  let ok = true;
  if(!h){ ok = false; }
  else if(h.status === 'never'){ ok = false; }
  else if(h.status === 'fail'){ ok = false; }

  bar.classList.toggle('fail', !ok);
  dot.classList.toggle('bad', !ok);
  pill.classList.toggle('bad', !ok);

  if(h && h.status === 'never'){
    $('#collectTitle').textContent = '每天自动收集还没开启';
    $('#collectSub').textContent = '开启后岗位会自己越攒越多 · 点右边按钮，三步搞定，不用敲命令';
  } else if(h && h.status === 'fail'){
    $('#collectTitle').textContent = '上次采集失败';
    $('#collectSub').textContent = h.text + (h.at ? `（${String(h.at).slice(0,16)}）` : '');
  } else if(h){
    $('#collectTitle').textContent = '每日采集正常';
    $('#collectSub').textContent = h.text + (h.at ? `　最近一次 ${String(h.at).slice(5,16)}` : '');
  }

  $('#collectPill').innerHTML = `<span class="dot${ok?'':' bad'}"></span> ${
    !h ? '采集未知' : h.status === 'never' ? '未启用采集'
      : h.status === 'fail' ? '采集失败' : '采集正常'}`;
}


function qs(){
  const p = new URLSearchParams();
  $$('[data-k]').forEach(el => {
    const k = el.dataset.k;
    if(el.type === 'checkbox'){ if(el.checked) p.set(k,'1'); }
    else if(el.value.trim()) p.set(k, el.value.trim());
  });
  return p.toString();
}

function toggleAll(){
  const w = $('#filtersWrap');
  const open = w.hidden;
  w.hidden = !open;
  if(open) w.scrollIntoView({behavior:'smooth', block:'start'});
}

/* 默认只展示前 N 个 —— 设计理念：帮用户挑好，不是让他自己挑 */
const PICK_LIMIT = 5;

async function load(){
  const r = await J('/api/jobs?' + qs());
  state.jobs = r.kept || [];

  // 先按匹配度排序（无法评估的排后面），再限量
  const rep = m => state.reports[m] || {match_score:0, verdict:''};
  const ordered = [...state.jobs].sort((a,b) => {
    const va = rep(a.id).verdict === '无法评估' ? 1 : 0;
    const vb = rep(b.id).verdict === '无法评估' ? 1 : 0;
    if(va !== vb) return va - vb;
    return rep(b.id).match_score - rep(a.id).match_score;
  });
  const shown = state.showAll ? ordered : ordered.slice(0, PICK_LIMIT);

  $('#cards').innerHTML = shown.length
    ? shown.map(cardHtml).join('')
    : `<div class="empty"><div class="big">🗂</div>
       目前没有符合条件的岗位。<br>放宽筛选条件，或先导入一批试试。</div>`;

  $('#picksSub').textContent = state.jobs.length > PICK_LIMIT && !state.showAll
    ? `已按匹配度排序，先给你看 ${shown.length} 个（共 ${r.kept_count} 个）`
    : `已按匹配度排序 · 共 ${r.kept_count} 个`;
  $('#filterStat').textContent = r.dropped_count ? `另有 ${r.dropped_count} 个被硬性条件筛掉` : '';

  const more = document.querySelector('.more-row .btn');
  if(more) more.textContent = state.showAll ? '收起' :
    (state.jobs.length > PICK_LIMIT ? `看全部 ${r.kept_count} 个符合条件的岗位` : '没有更多了');

  const d = r.dropped_sample || [];
  $('#droppedBox').innerHTML = (state.showAll && d.length) ? `
    <details class="dropped">
      <summary>被筛掉的 ${r.dropped_count} 个岗位 —— 点开看原因</summary>
      <ul>${d.map(x=>`<li><b>${esc(x.position)||'（无岗位名）'}</b>
        ${esc(x.company)?`@ ${esc(x.company)}`:''} —— ${esc((x.reasons||[]).join('；'))}</li>`).join('')}</ul>
    </details>` : '';

  await loadAnalysis();
}

/* 岗位可信度体检 —— 纯规则，跟匹配度分开算、分开显示。
   ⚠ 它**不扣匹配度的分**：一个 90% 匹配的岗位如果同时是条幽灵岗，
   两条信息必须同时看见，不能让高匹配度把风险盖住。 */
async function loadTrust(){
  const ids = state.jobs.map(j => j.id);
  if(!ids.length){ state.trust = {}; renderCards(); return; }
  const r = await post('/api/trust/batch', {ids});
  state.trust = (r && r.reports) || {};
  renderCards();
}

function trustBadge(j){
  const t = state.trust[j.id];
  if(!t) return '';
  const bad = (t.signals||[]).filter(s => s.level === 'bad');
  const out = [];

  // 最显眼的一档：岗位可能不活跃 / 有明确的法条或合同风险
  if(t.ghost_risk === 'suspect'){
    out.push(`<span class="trust-badge tb-bad" onclick="event.stopPropagation();openTrust(${j.id})">⚠ 这岗位可能不太活跃</span>`);
  }
  if(bad.length){
    out.push(`<span class="trust-badge tb-bad" onclick="event.stopPropagation();openTrust(${j.id})">${esc(bad[0].title)}</span>`);
  } else if(t.worst_level === 'warn'){
    out.push(`<span class="trust-badge tb-warn" onclick="event.stopPropagation();openTrust(${j.id})">有条件要问清楚</span>`);
  }

  // 实测踩过的坑：一开始只在"有问题"时才给徽章，结果正常岗位一个都不显示 ——
  // 功能等于不存在，而「这个薪资能不能信」恰恰是每条岗位都该知道的事。
  // 体检类的东西必须常驻，问题才升级颜色。
  const PAY_CLS = {高:'ok', 中:'mid', 低:'low', 未知:'unk'};
  const lvl = PAY_CLS[t.pay_trust] || 'unk';
  out.push(`<span class="trust-badge tb-${lvl} pay" onclick="event.stopPropagation();openTrust(${j.id})">`
         + `薪资可信度 ${esc(t.pay_trust)} · 体检 ›</span>`);
  return out.join('');
}

function openTrust(jobId){
  const t = state.trust[jobId];
  if(!t) return;
  const SIG_ICON = {bad:'✗', warn:'!', info:'i'};
  $('#trustBody').innerHTML = `
    <div class="why">
      ${t.ghost_risk === 'suspect' ? `
        <div class="ghost-box">
          <b>⚠ 这条岗位可能不太活跃</b>
          <div>${esc(t.ghost_reason)}</div>
        </div>` : ''}
      <div class="trust-head">
        <div class="k">公司类型</div><div class="v">${esc(t.company_type_label)}</div>
      </div>
      <div class="trust-head">
        <div class="k">这个薪资能不能信</div>
        <div class="v">${esc(t.pay_trust)}</div>
        <div class="d">${esc(t.pay_trust_reason)}</div>
      </div>
      ${(t.signals||[]).length ? `<h4>值得留意的地方（${t.signals.length}）</h4>
        ${t.signals.map(s=>`
          <div class="tsig ts-${s.level}">
            <div class="t">${SIG_ICON[s.level]||'i'} ${esc(s.title)}</div>
            <div class="d">${esc(s.detail)}</div>
            ${s.evidence?`<div class="e">原文：${esc(s.evidence)}</div>`:''}
          </div>`).join('')}`
        : '<div class="emptyhint">这个岗位没发现明显问题</div>'}
      <div class="dim" style="margin-top:14px;font-size:12px">
        体检结果<b>不影响</b>上面的匹配度 —— 两件事要分开看：
        匹配度高只说明你符合要求，不代表这个岗位真的在招人。
      </div>
    </div>`;
  openDrawer('trust');
}

async function loadAnalysis(){
  const a = await J('/api/analyze?' + qs());
  state.reports = {}; state.salary = {};
  if(!a.ok){
    $('#cmpBox').style.display = 'none';
    renderCards(); await loadTrust(); return;
  }
  state.reports = a.reports || {};
  state.salary = a.salary || {};
  renderCards();

  const sk = (a.candidate||{}).skill_count || 0;
  if(sk && a.candidate.is_fresh_grad)
    $('#filterNote').innerHTML = `你是 <b>应届/在校</b>，学历 <b>${esc(a.candidate.education||'未识别')}</b>。经验要求与你冲突的岗位会被降级`;

  const rows = a.compare || [];
  if(rows.length >= 2 && state.showAll){
    $('#cmpBox').style.display = '';
    $('#cmpExplain').innerHTML = (a.explain||[]).map(l=>`<div class="sug">${esc(l)}</div>`).join('');
    $('#cmpTable').innerHTML =
      `<thead><tr><th>岗位</th><th>薪资</th><th>匹配</th><th>结论</th><th>优势</th><th>风险</th></tr></thead>
       <tbody>${rows.map((r,i)=>`<tr class="${i===0?'win':''}">
         <td><b>${esc(r.position)}</b><br><span class="dim">${esc(r.company)}</span></td>
         <td class="num">${esc(r.salary_text)}</td>
         <td><span class="num" style="color:${i===0?'var(--ok-600)':'var(--ink-600)'};font-weight:700">${Math.round(r.match_score)}%</span></td>
         <td><span class="verdict v-${VCLS[r.verdict]||'na'}">${esc(r.verdict)}</span></td>
         <td>${(r.pros||[]).map(p=>`<div style="color:var(--ok-600)">✓ ${esc(p)}</div>`).join('')||'—'}</td>
         <td>${(r.cons||[]).map(p=>`<div style="color:var(--bad-600)">✗ ${esc(p)}</div>`).join('')||'—'}</td>
       </tr>`).join('')}</tbody>`;
  } else $('#cmpBox').style.display = 'none';

  renderFocus();
  await loadTrust();
}

/* 卡片渲染收敛到一处：初次加载与分析返回都走它，避免限量逻辑不一致 */
function renderCards(){
  const rep = m => state.reports[m] || {match_score:0, verdict:''};
  const ordered = [...state.jobs].sort((a,b) => {
    const va = rep(a.id).verdict === '无法评估' ? 1 : 0;
    const vb = rep(b.id).verdict === '无法评估' ? 1 : 0;
    if(va !== vb) return va - vb;
    return rep(b.id).match_score - rep(a.id).match_score;
  });
  const shown = state.showAll ? ordered : ordered.slice(0, PICK_LIMIT);
  $('#cards').innerHTML = shown.length
    ? shown.map(cardHtml).join('')
    : `<div class="empty"><div class="big">🗂</div>
       目前没有符合条件的岗位。<br>放宽筛选条件，或先导入一批试试。</div>`;
}

function cardHtml(j, idx){
  const st = j.salary_state || 'unresolved';
  const sal = j.salary_text || '';
  const rep = state.reports[j.id];
  const applied = j.status === 'applied' || j.status === 'interview';
  const cls = [];
  if(j.is_outsourcing_risk === true) cls.push('is-outsourcing');
  if(applied) cls.push('is-applied');
  if(j.status === 'ignored' || j.status === 'rejected') cls.push('is-ignored');

  const tags = [j.city, j.district, j.experience, j.education,
                j.employment_type, j.campus_batch].filter(Boolean);
  const flags = [];
  if(j.is_outsourcing_risk === true) flags.push(`<span class="flag flag-risk">⚠ 疑似外包/派遣</span>`);
  const d = daysSince(j.first_seen_at);
  if(d !== null && d <= 1) flags.push(`<span class="flag flag-fresh">🆕 今天新挂</span>`);
  else if(d !== null && d >= 7) flags.push(`<span class="flag flag-aged">已挂 ${d} 天</span>`);
  if(j.missing && j.missing.length)
    flags.push(`<span class="flag flag-incomplete">缺 ${j.missing.join('、')}</span>`);
  if(applied) flags.push(`<span class="flag flag-fresh">✓ 已投递</span>`);

  const acts = applied
    ? `<button class="act on" disabled>已投递</button>
       <button class="act" onclick="event.stopPropagation();openAppByJob(${j.id})">跟进</button>`
    : `<button class="act primary" onclick="event.stopPropagation();applyJob(${j.id})">投递</button>
       <button class="act" onclick="event.stopPropagation();setJobStatus(${j.id},'interested')">感兴趣</button>
       <button class="act" onclick="event.stopPropagation();setJobStatus(${j.id},'ignored')">先不看</button>`;

  return `
  <article class="card ${cls.join(' ')}">
    <div class="card-top">
      <div class="card-main">
        <div class="pos">${esc(j.position) || '<未取到岗位名>'}</div>
        <div class="co">
          <span>${esc(j.company) || '<未取到公司名>'}</span>
          ${j.company_size?`<span class="sep">·</span><span>${esc(j.company_size)}</span>`:''}
        </div>
        ${tags.length?`<div class="tags">${tags.map(t=>
          `<span class="tag ${t===j.city?'city':''}">${esc(t)}</span>`).join('')}</div>`:''}
        ${flags.length?`<div class="flags">${flags.join('')}
          ${rep?`<span class="verdict v-${VCLS[rep.verdict]||'na'}">${esc(rep.verdict)}</span>`:''}
        </div>`:''}
        ${trustBadge(j)?`<div class="flags">${trustBadge(j)}</div>`:''}
        ${j.outsourcing_evidence && j.outsourcing_evidence.length
          ? `<div class="evid">判据：${esc(j.outsourcing_evidence[0])}</div>`:''}
        ${rep?`<div class="why-mini">${esc(rep.summary)}</div>`:''}
      </div>
      <div class="salary">
        <div class="v ${sal?'':'empty'} num">${esc(sal) || '薪资未取到'}</div>
        <div class="st st-${st}">● ${SAL_LABEL[st]||st}${j.salary_period?' · '+esc(j.salary_period):''}</div>
      </div>
      ${rep?`<div class="score s-${SCLS[rep.verdict]||'mid'}">
        <div class="n num">${Math.round(rep.match_score)}<small style="font-size:12px">%</small></div>
        <div class="l">匹配度</div></div>`:''}
    </div>
    ${rep?`<div class="why-panel" id="wp-${j.id}" style="display:none">${whyHtml(rep,j)}</div>
      <button class="toggle" onclick="toggle('wp-${j.id}')">▾ 为什么给你这个分数</button>`:''}
    ${state.llmReady ? (state.sem[j.id]
      ? `<div class="why-panel" id="sp-${j.id}" style="display:none">${semHtml(state.sem[j.id])}</div>
         <button class="toggle" onclick="toggle('sp-${j.id}')">▾ 模型补充的判断</button>`
      : `<button class="act" onclick="event.stopPropagation();deepAnalyze(${j.id},this)">让模型再读一遍</button>
         <button class="act" onclick="event.stopPropagation();tailorResume(${j.id},this)">对齐这份简历</button>`) : ''}
    <div class="card-foot">
      ${j.first_seen_at?`<span class="dim">首次发现 ${String(j.first_seen_at).slice(0,10)}</span>`:''}
      <div class="acts">${acts}</div>
    </div>
  </article>`;
}

/* 语义层：模型只做加法，不得推翻规则引擎的 match_score / verdict。
   置信度不足的条目会被标成「推测」，不能和规则结论混在一起显示。 */
async function deepAnalyze(jobId, btn){
  const old = btn.textContent;
  btn.disabled = true; btn.textContent = '模型在读…（约 10 秒）';
  const r = await post('/api/llm/analyze/' + jobId);
  btn.disabled = false;
  if(!r.ok){
    btn.textContent = old;
    toast(r.error || '模型没读成功，先到设置里检查模型接入');
    return;
  }
  state.sem[jobId] = r;
  toast(r.from_cache ? '这次用的是之前的结果，没再调模型'
                     : '模型读完了');
  renderCards();
}

function semHtml(s){
  const line = (label, arr, cls) => (arr && arr.length)
    ? `<h4>${label}（${arr.length}）</h4><ul style="margin:0;padding-left:18px">${
        arr.map(x=>`<li style="color:var(--${cls})">${esc(x)}</li>`).join('')}</ul>`
    : '';
  return `<div class="why">
    <div class="dim" style="margin-bottom:10px">
      下面是模型在规则之外补充的看法，<b>不改变</b>上面的匹配度和结论。
      模型自己也可能读错，带「推测」二字的要自己去核实。
    </div>
    ${(s.semantic_matches||[]).length ? `<h4>✓ 规则没认出来、但模型认为你具备（${s.semantic_matches.length}）</h4>
      <div>${s.semantic_matches.map(m=>`
        <div class="sug">· ${esc(m.skill)}
          ${m.confidence==='high'?'':'<span class="dim">（推测）</span>'}</div>
        <div class="evid">JD：${esc((m.evidence_jd||'').slice(0,70))}</div>
        <div class="evid">简历：${esc((m.evidence_resume||'').slice(0,70))}</div>`).join('')}</div>` : ''}
    ${line('✗ 模型认为你还缺的（推测，需自行核实）', s.missing, 'bad-600')}
    ${line('⚠ 模型标出的风险点（推测，需自行核实）', s.red_flags, 'warn-700')}
    ${(s.resume_advice||[]).length ? `<h4>📝 简历可以怎么改</h4>
      ${s.resume_advice.map(x=>`<div class="sug">${esc(x)}</div>`).join('')}` : ''}
    ${(s.soft_fit_score!=null) ? `<h4>软性素质评分 ${s.soft_fit_score}（模型主观判断，仅供参考）</h4>` : ''}
  </div>`;
}

/* 简历定制 / ATS 对齐。
   它只会做两件事：告诉你哪些关键词已经命中，以及哪些你确实没有。
   **不会替你编任何没做过的事** —— 程序逐字校验过改写不得引入新技术名词。 */
async function tailorResume(jobId, btn){
  const old = btn.textContent;
  btn.disabled = true; btn.textContent = '正在对齐…';
  const r = await post('/api/materials/tailor/' + jobId);
  btn.disabled = false; btn.textContent = old;
  if(!r.ok){ toast(r.error || '没对齐成，先检查简历和模型设置'); return; }
  openTailorSheet(r);
  toast(r.from_cache ? '这次直接用了之前的结果，没再调模型' : '对齐好了');
}

function openTailorSheet(r){
  const pct = Math.round((r.ats_ratio||0)*100);
  const ul = (t,arr,cls) => (arr&&arr.length)
    ? `<h4>${t}</h4><ul class="prep-list ${cls}">${arr.map(x=>`<li>${esc(x)}</li>`).join('')}</ul>` : '';

  $('#tailorBody').innerHTML = `
    <div class="why">
      <div class="tailor-score">
        <div class="n">${pct}<small>%</small></div>
        <div class="l">这个词，HR 的系统能搜到你</div>
      </div>
      <div class="dim" style="margin:10px 0 14px">
        下面的改写建议只会调整<b>说法和顺序</b>，不会替你新增任何没做过的经历。
        被挡掉的建议说明它想往简历里塞新东西 —— 那种建议本来就不该听。
      </div>
      ${ul(`✓ 你已经有的（${(r.ats_hit||[]).length}）`, r.ats_hit, 'ok')}
      ${ul(`✗ 岗位要、你没有的（${(r.ats_missing||[]).length}）`, r.ats_missing, 'bad')}
      ${ul('⛔ 这些真的别硬写', r.cannot_fake, 'bad')}
      ${(r.rewrite||[]).length ? `<h4>✎ 可以这样改（${r.rewrite.length}）</h4>
        ${r.rewrite.map(x=>`
          <div class="rw">
            <div class="from">原：${esc(x.original)}</div>
            <div class="to">改：${esc(x.suggested)}</div>
            ${x.reason?`<div class="dim">为什么要改：${esc(x.reason)}</div>`:''}
          </div>`).join('')}` : ''}
    </div>`;
  openDrawer('tailor');
}

/* ═══════════════════════════════════════════════════════════
   兼职 —— 判据跟全职完全是两套
   ═══════════════════════════════════════════════════════════
   全职比「月薪 + 岗位要求」；兼职比的是三件完全不同的事：

     1. 时薪 —— 日薪 200 元/天 每天 8 小时 = 25/小时，
        月薪 5000 每天 8 小时 = 28.7/小时。数字大 10 倍，时薪反而低。
     2. 能不能居家 —— 对学生常常比时薪更决定可行性（通勤 1.5 小时
        做时薪 25 的家教，实际算下来不如时薪 18 的线上客服）。
     3. 干什么活 —— 「运营助理」可能是做数据也可能是拉人头，
        不分类就没法比。

   ⚠ 一条不能妥协的：工时靠猜出来的时薪**不参与排名、不参与硬筛选**。
   口径不明的数字比大小，得出的名次是随意的。 */

const PT = { meta:null, ids:[], cats:[], init:false };

async function initPartTime(){
  if(PT.init) return;
  const r = await J('/api/parttime/meta');
  if(!r.ok) return;
  PT.meta = r;

  // 结算周期下拉
  const sel = $('#ptSettle');
  (r.settlements || []).forEach(s => {
    const o = document.createElement('option'); o.value = s; o.textContent = s;
    sel.appendChild(o);
  });

  // 工作内容分类多选
  $('#ptCats').innerHTML = (r.categories || []).map(c =>
    `<button class="chip" data-cat="${c.key}" onclick="this.classList.toggle('on')"
      title="${esc(c.caution)}">${esc(c.label)}</button>`).join('');

  // 候选：全库岗位里兼职那部分
  const jobs = await J('/api/jobs');
  PT.ids = (jobs.kept || []).map(j => j.id);

  PT.init = true;
  screenPartTime();
}

function ptFilterObj(){
  return {
    hourly_min: $('#ptHourly').value.trim(),
    remote: $('#ptRemote').value,
    settlement: $('#ptSettle').value,
    exclude_scams: $('#ptNoScam').checked,
    exclude_fake_free: $('#ptNoFake').checked,
    keyword: $('#ptKw').value.trim(),
    categories: $$('#ptCats .chip.on').map(c => c.dataset.cat),
  };
}

async function screenPartTime(){
  $('#ptStat').textContent = '筛选中…';
  const r = await post('/api/parttime/screen',
    { ids: PT.ids, filter: ptFilterObj() });
  $('#ptStat').textContent = '';
  if(!r.ok){ $('#ptEmpty').hidden = false;
    $('#ptEmpty').textContent = r.error || '筛选失败'; return; }

  if(r.empty){
    $('#ptEmpty').hidden = false; $('#ptEmpty').textContent = r.reason;
    $('#ptResult').hidden = true; return;
  }
  $('#ptEmpty').hidden = true;
  $('#ptResult').hidden = false;

  const s = r.stats || {};
  $('#ptSub').textContent =
    `${r.kept.length} 个符合${r.pending.length ? ` · ${r.pending.length} 个算不出时薪没法判` : ''}`
    + `（兼职共 ${s.total || 0} 个）`;

  // 结论
  $('#ptExplain').innerHTML = esc((r.compare && r.compare.explain) || '');

  // 对比表
  const rows = (r.compare && r.compare.rows) || [];
  $('#ptCmp').innerHTML = rows.length ? `
    <table class="cmp">
      <thead><tr><th>时薪</th><th>岗位</th><th>能不能居家</th>
        <th>干什么活</th><th>结算</th><th>要注意</th></tr></thead>
      <tbody>${rows.map(x=>`
        <tr>
          <td class="num"><b>${esc(x.hourly_display)}</b><br>
            <span class="dim">${esc(x.basis)}</span></td>
          <td><b>${esc(x.position)}</b><br><span class="dim">${esc(x.company)}</span></td>
          <td>${esc(x.remote)}${x.fake_free
            ? '<br><span class="dim" style="color:var(--bad-600)">假居家</span>' : ''}</td>
          <td>${esc(x.category)}</td>
          <td>${esc(x.settlement || '—')}</td>
          <td>${x.scam_level==='scam'
              ? '<span class="flag flag-incomplete">⛔ 别接</span>'
              : (x.category_caution
                  ? `<span class="dim">${esc(x.category_caution)}</span>` : '—')}</td>
        </tr>`).join('')}</tbody>
    </table>
    ${(r.compare.assumed_only||[]).length ? `<p class="note">
      另有 ${r.compare.assumed_only.length} 个因为没写工时、算不出时薪，
      没有参与排名 —— <b>补上"每天几小时"就能一起比了</b>。</p>` : ''}
    ${r.compare.note ? `<p class="note">${esc(r.compare.note)}</p>` : ''}`
    : `<div class="emptyhint">没有能按时薪比较的岗位</div>`;

  // 算不出时薪的
  const pb = $('#ptPendingBox');
  pb.hidden = !r.pending.length;
  $('#ptPending').innerHTML = r.pending.map(ptCard).join('');

  // 符合条件��
  const kb = $('#ptKeptBox');
  kb.hidden = !r.kept.length;
  $('#ptKeptSub').textContent = r.kept.length + ' 个';
  $('#ptKept').innerHTML = r.kept.map(ptCard).join('');
}

function ptCard(x){
  const scam = x.signals.filter(g => g.level === 'scam');
  const warn = x.signals.filter(g => g.level === 'warn');
  return `<article class="pt-item ${scam.length ? 'is-scam' : ''}">
    <div class="pt-main">
      <div class="pt-h">
        <span class="pt-pay num">${esc((x.hourly && x.hourly.display) || '时薪算不出来')}</span>
        <b>${esc(x.position)}</b>
        <span class="dim">${esc(x.company)}</span>
      </div>
      <div class="pt-tags">
        <span class="tag city">${esc((x.remote && x.remote.label) || '')}</span>
        <span class="tag">${esc((x.category && x.category.label) || '')}</span>
        ${x.settlement ? `<span class="tag">${esc(x.settlement)}</span>` : ''}
        ${(x.remote && x.remote.fake_free)
          ? '<span class="flag flag-incomplete">假居家：要随时在线</span>' : ''}
      </div>
      ${(x.hourly && x.hourly.basis) ? `<div class="evid">怎么算的：${esc(x.hourly.basis)}</div>` : ''}
      ${scam.map(g => `<div class="pt-warn">⛔ ${esc(g.title)}</div>`).join('')}
      ${warn.map(g => `<div class="pt-note">⚠ ${esc(g.title)}</div>`).join('')}
      ${(x.category && x.category.caution) ? `<div class="pt-note">${esc(x.category.caution)}</div>` : ''}
      ${(x.notes||[]).length ? `<div class="pt-note">${esc(x.notes.join('；'))}</div>` : ''}
    </div>
  </article>`;
}

async function copyPtReport(){
  const r = await post('/api/parttime/report', { ids: PT.ids });
  if(!r.ok){ toast(r.error || '导出失败'); return; }
  const t = r.text || '';
  try { await navigator.clipboard.writeText(t); toast('结论已复制，粘贴到备忘录里就能带走'); }
  catch(e){ toast('复制失败，浏览器不让访问剪贴板'); }
}

function whyHtml(rep, job){  const sk = arr => arr.map(m=>{
    const [c,l] = LVL[m.level] || LVL.normal;
    return `<span class="skw ${c}" title="${esc((m.jd_evidence||'').slice(0,50))}">
      ${m.hit?'✓':'✗'} ${esc(m.skill)}<small style="opacity:.7">${l}</small></span>`;
  }).join('');
  const sal = state.salary[String(job.id)];
  const salHtml = (sal && (sal.vs_expect || sal.vs_batch)) ? `
    <h4>💰 薪资怎么看</h4>
    ${sal.vs_expect?`<div class="sug">${esc(sal.vs_expect)}</div>`:''}
    ${sal.vs_batch?`<div class="sug">${esc(sal.vs_batch)}</div>`:''}` : '';
  return `<div class="why">
    ${rep.covered && rep.covered.length
      ? `<h4>✓ 你已经具备（${rep.covered.length}）</h4><div class="sk">${sk(rep.covered)}</div>`:''}
    ${(rep.missing_hard||[]).length || (rep.missing_normal||[]).length
      ? `<h4>✗ 你还缺（硬性 ${(rep.missing_hard||[]).length} · 一般 ${(rep.missing_normal||[]).length}）</h4>
         <div class="sk">${sk([...(rep.missing_hard||[]),...(rep.missing_normal||[])])}</div>`:''}
    ${(rep.blockers||[]).length
      ? `<h4>⛔ 投之前必须知道</h4>${rep.blockers.map(s=>`<div class="block">${esc(s)}</div>`).join('')}`:''}
    ${(rep.suggestions||[]).length
      ? `<h4>📌 你该做什么</h4>${rep.suggestions.map(s=>`<div class="sug">${esc(s)}</div>`).join('')}`:''}
    ${salHtml}
    ${(rep.notes||[]).length
      ? `<h4>⚠️ 说明</h4>${rep.notes.map(s=>`<div class="sug">${esc(s)}</div>`).join('')}`:''}
  </div>`;
}

function toggle(id){
  const el = document.getElementById(id);
  const on = el.style.display === 'none';
  el.style.display = on ? 'block' : 'none';
  el.previousElementSibling.textContent = on ? '▴ 收起' : '▾ 为什么给你这个分数';
}

/* ── 岗位操作 ───────────────────────────────────────────── */
async function applyJob(id){
  const rep = state.reports[id];
  const r = await post('/api/tracking/applications', {
    job_id: id, match_score: rep ? rep.match_score : null,
    match_verdict: rep ? rep.verdict : ''
  });
  if(!r.ok){ toast(r.error || '投递记录失败'); return; }
  toast(`已记入台账：${r.application.position}`);
  load(); loadToday();
}
async function openAppByJob(jobId){
  const r = await J('/api/tracking/applications');
  const hit = (r.applications||[]).find(a => a.job_id === jobId);
  if(hit) openApp(hit.id);
  else { await applyJob(jobId); toast('已建记录，点「投递」标签查看'); showTab('track'); }
}
async function setJobStatus(id, st){
  await post(`/api/jobs/${id}/status`, {status: st});
  toast({interested:'已标记感兴趣', ignored:'已隐藏'}[st] || '已更新');
  load(); loadToday();
}
function resetFilters(){
  $$('[data-k]').forEach(el => {
    if(el.type === 'checkbox') el.checked = false; else el.value = '';
  });
  load(); loadToday();
}

/* ═══════════════════════════════════════════════════════════
   投递台账
   ═══════════════════════════════════════════════════════════ */
$$('#statusChips .chip').forEach(c => c.onclick = () => {
  $$('#statusChips .chip').forEach(x => x.classList.remove('on'));
  c.classList.add('on');
  state.statusFilter = c.dataset.st;
  loadTracking();
});

async function loadTracking(){
  const q = state.statusFilter ? '?status=' + state.statusFilter : '';
  const r = await J('/api/tracking/applications' + q);
  const apps = r.applications || [], s = r.summary || {};

  // 统计卡：点哪个 tab 都能下钻
  $('#tstats').innerHTML = [
    ['总投递', s.total || 0], ['进行中', s.open || 0],
    ['跟进次数', s.followups || 0], ['已获 Offer', s.offer || 0],
    ['回复率', s.reply_rate === null || s.reply_rate === undefined
       ? '—' : s.reply_rate + '%']
  ].map(([k,v]) => `<div class="tstat"><div class="k">${k}</div>
      <div class="v num">${v}</div></div>`).join('');

  // 首页那三个数字由 loadToday() 维护（#mNew/#mFollow/#mOpen）。
  // 这里不要再写 #sOpen/#sReply —— 那两个 id 在首页重做时已经移除了，
  // 实测踩过：写不存在的元素会抛 TypeError，把整个列表渲染中断，
  // 表现为「统计有数但列表空白」。
  if(!apps.length){
    $('#apps').innerHTML = `<div class="empty"><div class="big">📮</div>
      ${state.statusFilter ? '这个状态下没有记录' : '还没有投递记录'}<br>
      <span class="dim">在「今日」里点岗位卡的「投递」按钮，就会记到这里</span></div>`;
    return;
  }
  $('#apps').innerHTML = apps.map(appCard).join('');
}

function appCard(a){
  const d = a.days_since_applied || 0;
  // 边框颜色直接表达紧迫度，不用另加标签
  let cls = '', daysCls = 'd-ok', daysTxt = `投了 ${d} 天`;
  if(a.status === 'applied' && d >= 7){ cls = 'stale'; daysCls = 'd-late'; }
  else if(a.status === 'applied' && d >= 3){ cls = 'due'; daysCls = 'd-due'; }
  else if(a.status === 'applied'){ daysTxt = `投了 ${d} 天，可以跟一下`; daysCls = 'd-due'; }

  const meta = [];
  if(a.salary_text) meta.push(`<span>薪资 <b>${esc(a.salary_text)}</b></span>`);
  if(a.match_score !== null && a.match_score !== undefined)
    meta.push(`<span>投递时匹配度 <b>${Math.round(a.match_score)}%</b></span>`);
  if(a.resume_name) meta.push(`<span>简历 <b>${esc(a.resume_name)}</b></span>`);
  if(a.followup_count) meta.push(`<span>跟进 <b>${a.followup_count}</b> 次</span>`);

  // 面试准备 —— 只在"已进面试"的状态下出现。
  // 放在卡片上而不是详情里，是因为准备是面试**前**的事，
  // 面试前用户多半还停留在台账列表页，不会点进详情。
  const prepBtn = (a.status === 'interview' && state.llmReady)
    ? `<button class="act primary" onclick="event.stopPropagation();prepInterview(${a.job_id},this)">准备这场面试</button>`
    : '';

  return `<article class="app ${cls}" onclick="openApp(${a.id})" style="cursor:pointer">
    <div class="app-top">
      <div class="app-main">
        <div class="app-title">${esc(a.position)}</div>
        <div class="app-sub">${esc(a.company || '未知公司')}${a.city?' · '+esc(a.city):''}</div>
        <div class="app-meta">${meta.join('')}</div>
      </div>
      <span class="app-days ${daysCls}">${esc(daysTxt)}</span>
      <span class="st st-${a.status}">${esc(a.status_label)}</span>
    </div>
    ${prepBtn ? `<div class="card-foot">${prepBtn}</div>` : ''}
  </article>`;
}

/* 面试准备：预测问题 + 回答要点 + 缺口 + 反问清单
   ⚠ 回答要点的每一条都来自简历原文摘录，程序逐字比对过 ——
   这里不会、也不允许出现"你可以说自己会某技术"这种话。 */
async function prepInterview(jobId, btn){
  const old = btn.textContent;
  btn.disabled = true; btn.textContent = '正在准备…';
  const r = await post('/api/materials/interview/' + jobId);
  btn.disabled = false; btn.textContent = old;
  if(!r.ok){ toast(r.error || '没准备成，先检查简历和模型设置'); return; }
  openPrepSheet(r);
  toast(r.from_cache ? '这次直接用了之前的结果，没再调模型' : '准备材料好了');
}

function openPrepSheet(r){
  const q = (r.questions||[]).map((x,i)=>`
    <div class="prep-q">
      <h4>${i+1}. ${esc(x.question)}</h4>
      ${x.why?`<div class="dim">考察点：${esc(x.why)}</div>`:''}
      <ul class="prep-points">${(x.points||[]).map(p=>`<li>${esc(p)}</li>`).join('')}</ul>
      ${x.evidence_resume?`<div class="evid">简历依据：${esc(x.evidence_resume)}</div>`:''}
      ${x.fallback?`<div class="dim">答不上来时：${esc(x.fallback)}</div>`:''}
    </div>`).join('');
  const ul = (t,arr,cls) => (arr&&arr.length)
    ? `<h4>${t}</h4><ul class="prep-list ${cls}">${arr.map(x=>`<li>${esc(x)}</li>`).join('')}</ul>` : '';

  $('#prepBody').innerHTML = `
    <div class="why">
      <div class="dim" style="margin-bottom:10px">
        下面的回答要点，每一条都能在你的简历原文里找到出处。
        面试官顺着追问你答不上来时，<b>说实话比硬撑安全得多</b>。
      </div>
      ${q || '<div class="emptyhint">这次没能从你的简历里找到可展开的问题 —— 简历写得太简了，面试官可问的也就那些</div>'}
      ${ul('你可能答不上来的（岗位要，但你没做过）', r.gaps, 'bad')}
      ${ul('该反问面试官', r.ask_back, 'ok')}
      ${ul('面试前准备好', r.checklist)}
    </div>`;
  openDrawer('prep');
}

async function openApp(id){
  const r = await J(`/api/tracking/applications/${id}`);
  if(!r.ok){ toast(r.error); return; }
  const a = r.application, fus = r.followups || [];

  const tl = fus.length ? `<div class="tl">${fus.map(f=>`
    <div class="tl-item ${f.reply?'reply':''}">
      <div class="when">${esc(f.at)}　${esc({email:'邮件',phone:'电话',platform:'平台内聊',other:'其他'}[f.channel]||f.channel)}</div>
      <div class="what">${f.reply?'对方回复：':'已发送：'}</div>
      <div class="said">${esc(f.reply || f.content)}</div>
    </div>`).join('')}</div>` : '<div class="emptyhint" style="margin-top:12px">还没有跟进记录</div>';

  const acts = Object.entries(ST_LABEL).filter(([k]) => k !== a.status)
    .map(([k,v]) => `<button class="act" onclick="setAppStatus(${a.id},'${k}')">${v}</button>`)
    .join('');

  $('#appDetail').innerHTML = `
    <div style="display:flex;align-items:flex-start;gap:12px">
      <div style="flex:1">
        <h3>${esc(a.position)}</h3>
        <p class="dim" style="margin-top:4px">
          ${esc(a.company||'未知公司')}${a.city?' · '+esc(a.city):''}
          ${a.salary_text?' · '+esc(a.salary_text):''}
        </p>
      </div>
      <span class="st st-${a.status}">${esc(a.status_label)}</span>
    </div>
    <div class="app-meta" style="margin-top:14px">
      <span>投递 <b>${esc(a.applied_at)}</b>（${a.days_since_applied} 天前）</span>
      ${a.match_score!=null?`<span>投递时匹配度 <b>${Math.round(a.match_score)}%</b></span>`:''}
      ${a.resume_name?`<span>简历 <b>${esc(a.resume_name)}</b></span>`:''}
      <span>跟进 <b>${a.followup_count}</b> 次</span>
    </div>

    ${a.reply?`<div class="evid" style="margin-top:12px">最近回复：${esc(a.reply)}</div>`:''}

    <h4 style="margin-top:20px;font-size:13px;color:var(--ink-600)">跟进时间线</h4>
    ${tl}

    <div class="draftbox" id="draftBox"></div>
    <div style="display:flex;gap:8px;margin-top:16px;flex-wrap:wrap">
      <button class="btn" onclick="genDraft(${a.id})">生成跟进话术</button>
      <button class="btn ghost" onclick="showFollowForm(${a.id})">手动记一次跟进</button>
    </div>

    <h4 style="margin-top:20px;font-size:13px;color:var(--ink-600)">改状态</h4>
    <div style="display:flex;gap:6px;flex-wrap:wrap;margin-top:8px">
      ${acts}
      <button class="btn ghost sm" onclick="delApp(${a.id})">删除记录</button>
    </div>`;
  openDrawer('app');
}

async function setAppStatus(id, st){
  const r = await post(`/api/tracking/applications/${id}/status`, {status: st});
  if(!r.ok){ toast(r.error || '状态未更新'); return; }
  toast('状态已更新');
  loadTracking(); loadToday();
  openApp(id);
}

async function delApp(id){
  if(!confirm('删除这条投递记录？跟进记录也会一起删掉。')) return;
  await fetch(`/api/tracking/applications/${id}`, {method:'DELETE'});
  closeDrawer('app');
  toast('已删除');
  loadTracking(); loadToday();
}

function showFollowForm(id){
  const box = $('#draftBox');
  box.innerHTML = `
    <div class="lb">记录一次跟进</div>
    <textarea id="fContent" style="width:100%;min-height:80px;padding:10px;border-radius:8px;
      border:1px solid var(--brand-200);background:var(--surface);outline:none"
      placeholder="我说了什么"></textarea>
    <input id="fReply" placeholder="对方回了什么（没回就留空）"
      style="width:100%;margin-top:8px;padding:9px 11px;border-radius:8px;
      border:1px solid var(--brand-200);background:var(--surface);outline:none">
    <div class="row-end">
      <button class="btn sm" onclick="saveFollow(${id})">保存</button>
      <button class="btn ghost sm" onclick="genDraft(${id})">让 AI 帮我写</button>
    </div>`;
}

async function saveFollow(id){
  const content = $('#fContent').value.trim();
  if(!content){ toast('写点内容'); return; }
  await post(`/api/tracking/applications/${id}/followups`,
    {content, reply: $('#fReply').value.trim()});
  toast('已记录');
  openApp(id); loadTracking(); loadToday();
}

async function genDraft(id){
  const box = $('#draftBox');
  box.innerHTML = `<div class="lb">正在生成…</div>`;
  const r = await post(`/api/tracking/followup-draft/${id}`);
  if(!r.ok){ box.innerHTML = `<div class="lb">生成失败</div><div class="dim">${esc(r.error)}</div>`; return; }
  const d = r.draft || {};
  const variants = Object.entries(d.variants || {})
    .map(([k,v]) => `<div class="variant"><b>${esc(k)}</b>：${esc(v)}</div>`).join('');
  box.innerHTML = `
    <div class="lb">跟进话术草稿${d.source === 'template' ? '（模板）' : ''}</div>
    <div class="msg">${esc(d.message || '（生成失败）')}</div>
    ${variants ? `<div class="variants">${variants}</div>` : ''}
    <div class="warn">⚠ ${r.error ? esc(r.error) + '；' : ''}
      这是草稿，<b>请自己改过再发</b>。本工具不会替你发送任何消息。</div>
    <div class="row-end">
      <button class="btn sm" onclick="copyText(this)">复制</button>
    </div>`;
}

function copyText(btn){
  const msg = btn.closest('.draftbox').querySelector('.msg').textContent;
  navigator.clipboard?.writeText(msg).then(()=>toast('已复制'), ()=>toast('复制失败，请手动选中'));
}

/* ═══════════════════════════════════════════════════════════
   简历
   ═══════════════════════════════════════════════════════════ */
async function loadResumes(){
  const r = await J('/api/resumes');
  const rs = r.resumes || [];
  if(!rs.length){
    $('#rlist').innerHTML = `<div class="emptyhint">还没有简历版本。
      在上面粘一份，投递时会自动记下用的是哪一版。</div>`;
    return;
  }
  $('#rlist').innerHTML = rs.map(x => `
    <article class="rcard">
      <div class="rm">
        <div class="rn">${esc(x.name)}
          ${x.is_default?'<span class="tag city" style="margin-left:6px">默认</span>':''}</div>
        <div class="rt">${esc(x.tag||'未标方向')} · ${x.length} 字</div>
        <div class="rh">hash ${esc(x.hash)}</div>
      </div>
      <div style="display:flex;gap:6px">
        ${x.is_default?'':`<button class="btn ghost sm" onclick="setDefaultResume(${x.id})">设为默认</button>`}
        <button class="btn ghost sm" onclick="viewResume(${x.id})">查看</button>
        <button class="btn ghost sm" onclick="delResume(${x.id})">删除</button>
      </div>
    </article>`).join('');
}

async function addResume(){
  const content = $('#rContent').value.trim();
  if(!content){ toast('先粘贴简历内容'); return; }
  const r = await post('/api/resumes', {
    name: $('#rName').value.trim(), tag: $('#rTag').value.trim(),
    content, is_default: $('#rDefault').checked });
  if(!r.ok){ toast(r.error); return; }
  $('#rName').value = ''; $('#rTag').value = ''; $('#rContent').value = ''; $('#rDefault').checked = false;
  toast('已保存');
  loadResumes(); loadDataStats();
}
async function setDefaultResume(id){
  await post(`/api/resumes/${id}/default`);
  toast('已设为默认');
  loadResumes();
}
async function delResume(id){
  if(!confirm('删除这份简历？已投递的记录里还留着引用，不影响。')) return;
  await fetch(`/api/resumes/${id}`, {method:'DELETE'});
  toast('已删除'); loadResumes(); loadDataStats();
}
async function viewResume(id){
  const r = await J('/api/resumes');
  const x = (r.resumes||[]).find(v => v.id === id);
  if(!x) return;
  $('#resumeDetail').innerHTML = `
    <h3>${esc(x.name)}</h3>
    <p class="dim" style="margin-top:4px">${esc(x.tag||'')} · ${x.length} 字 · hash ${esc(x.hash)}</p>
    <textarea style="width:100%;min-height:420px;margin-top:16px;padding:14px;border-radius:10px;
      border:1px solid var(--line);background:var(--surface-2);outline:none;line-height:1.8"
      readonly>${esc(x.content)}</textarea>`;
  openDrawer('resume');
}

/* ═══════════════════════════════════════════════════════════
   复盘
   ═══════════════════════════════════════════════════════════ */
async function loadInsight(){
  const box = $('#insight');
  try{
    await _loadInsight(box);
  }catch(e){
    // 实测踩过：这里抛错时页面整块空白，console 里还抓不到
    // （promise rejection 不一定进 console），用户只看到"页面坏了"。
    // 所以渲染失败必须显示出来，而不是静默留白。
    box.innerHTML = `<div class="emptyhint" style="text-align:left">
      <b style="color:var(--bad-600)">复盘数据加载失败</b><br>
      <span class="dim">${esc(e && e.message || e)}</span></div>`;
    console.error('[求职工作台] loadInsight failed:', e);
  }
}

async function _loadInsight(box){
  const r = await J('/api/tracking/summary');
  const s = r.summary || {};
  if(!s.total){
    box.innerHTML = `<div class="emptyhint">
      还没有投递数据。去「今日」投几个，这里就会告诉你<br>
      投出去的岗位里哪些真的有回音、匹配度与回复率的关系。</div>`;
    return;
  }

  // 匹配度 vs 结果：这是最有价值的洞察
  const apps = (await J('/api/tracking/applications')).applications || [];
  const buckets = {'0-30': {n:0, ok:0}, '31-60': {n:0, ok:0}, '61-80': {n:0, ok:0}, '81-100': {n:0, ok:0}};
  apps.filter(a => a.match_score != null).forEach(a => {
    const m = a.match_score;
    const k = m <= 30 ? '0-30' : m <= 60 ? '31-60' : m <= 80 ? '61-80' : '81-100';
    buckets[k].n++;
    if(['offer','interview','assessment'].includes(a.status)) buckets[k].ok++;
  });

  const bars = Object.entries(buckets).filter(([,v]) => v.n > 0).map(([k,v]) => {
    const pct = Math.round(v.ok / v.n * 100);
    return `<div class="barrow">
      <div class="bl"><span>匹配度 ${k}%</span>
        <span class="num">${v.ok}/${v.n} 有回应 · ${pct}%</span></div>
      <div class="bt"><i style="width:${pct}%"></i></div>
    </div>`;
  }).join('');

  const decided = s.decided || 0;
  const advice = decided < 5
    ? '样本还太少（' + decided + ' 个），再多投一些这个结论才有意义。'
    : (s.reply_rate >= 40
        ? '回复率不错，说明方向对。可以适当投更多同类岗位。'
        : '回复率偏低。优先投匹配度 60% 以上的岗位，低匹配的先别浪费时间。');

  box.innerHTML = `
    <div class="grid3">
      <div class="bigstat"><div class="v num">${s.total||0}</div><div class="k">累计投递</div></div>
      <div class="bigstat"><div class="v num" style="color:${(s.reply_rate||0)>=40?'var(--ok-600)':'var(--warn-700)'}">${s.reply_rate==null?'—':s.reply_rate+'%'}</div><div class="k">回复率（拿到 offer / 已出结果）</div></div>
      <div class="bigstat"><div class="v num">${s.followups||0}</div><div class="k">跟进次数</div></div>
    </div>
    <div class="section">
      <div class="sec-hd"><h3>匹配度与回应的关系</h3>
        <span class="sub">这条最能指导你接下来往哪投</span></div>
      ${bars ? `<div class="bars">${bars}</div>` : '<div class="emptyhint">投的时候带上匹配度快照才有数据</div>'}
      <p class="note">${esc(advice)}</p>
    </div>
    <div class="section">
      <div class="sec-hd"><h3>状态分布</h3></div>
      <div class="bars">${Object.entries(s.by_status||{}).map(([k,v])=>`
        <div class="barrow">
          <div class="bl"><span class="st st-${k}" style="font-size:11px">${ST_LABEL[k]||k}</span></div>
          <div class="bt"><i style="width:${Math.round(v/(s.total||1)*100)}%"></i></div>
          <div class="bl" style="margin-top:4px"><span class="dim num">${v} 个</span></div>
        </div>`).join('')}</div>
    </div>`;
}

/* ═══════════════════════════════════════════════════════════
   导入 / 设置
   ═══════════════════════════════════════════════════════════ */
async function doImport(){
  const content = $('#importText').value.trim();
  if(!content){ toast('先粘贴内容'); return; }
  const res = $('#importRes'); res.textContent = '解析中…';
  const r = await post('/api/jobs/import', {mode:'text', content});
  if(!r.ok){ res.innerHTML = `<span style="color:var(--bad-600)">${esc(r.error)}</span>`; return; }
  res.innerHTML = `新增 <b>${r.inserted}</b> · 更新 ${r.updated} · 完整率 ${Math.round(r.completeness*100)}%
    ${r.healthy?'':`<span style="color:var(--bad-600)"> · ⚠ ${esc((r.warnings||[]).join('；'))}</span>`}`;
  toast(`已入库 ${r.inserted} 个岗位`);
  setTimeout(()=>{ closeDrawer('import'); $('#importText').value=''; }, 1200);
  load(); loadToday();
}

/* ═══════════════════════════════════════════════════════════
   每日采集 —— 新手向导
   设计前提：使用者可能完全不懂软件，所以每步只说一件事、一个大按钮、
   状态随时能查。**任何时候都不让用户去敲命令行。**
   ═══════════════════════════════════════════════════════════ */

const WIZ = { step: 1, state: {} };

function wizGo(step, done = []){
  WIZ.step = step;
  [1,2,3].forEach(n => {
    const pane = $('#wz' + n);
    if(pane) pane.hidden = (n !== step);
    const el = document.querySelector(`.wiz-step[data-s="${n}"]`);
    if(el){
      el.classList.toggle('done', done.includes(n));
      el.classList.toggle('now', n === step);
    }
  });
  const now = document.querySelector('.wiz-step.now');
  if(now) now.scrollIntoView({block:'nearest', behavior:'smooth'});
}

function wizState(msg, kind){
  const el = $('#wz' + WIZ.step + 'state');
  if(!el) return;
  const color = kind === 'err' ? 'var(--bad-600)'
              : kind === 'ok'  ? 'var(--ok-600)' : 'var(--ink-600)';
  el.innerHTML = msg ? `<span style="color:${color}">${esc(msg)}</span>` : '';
}

/* 清掉三步各自的提示行。实测踩过：上次停在第 3 步，这次直接跳到第 2 步，
   第 1 步底下还挂着上一次的「正在查看状态…」，用户以为程序卡住了。 */
function wizReset(){
  WIZ.step = 1;
  [1,2,3].forEach(n => { const el = $('#wz' + n + 'state'); if(el) el.innerHTML = ''; });
}

/* 打开向导：先问后端真实状态，从当前应该做的那一步开始。
   实测踩过：每次都从头显示第 1 步，用户明明登录过了还被要求再登录一次。 */
async function openCollectWizard(){
  openDrawer('collect');
  wizReset();
  WIZ.step = 1;
  wizState('正在查看状态…');
  const r = await post('/api/collect/status');
  WIZ.state = r;
  applyWizardState(r);
}

function applyWizardState(r){
  if(!r) return;
  if(!r.installed){
    wizGo(1);
    $('#wz1desc').innerHTML =
      `<b style="color:var(--bad-600)">没找到 Chrome 或 Edge。</b><br>
       先去官网装一个：<a href="https://www.google.com/chrome/" target="_blank"
       style="color:var(--brand-600)">google.com/chrome</a><br>
       装好后回来点下面的按钮。`;
    $('.wiz-step[data-s="2"]').style.display = 'none';
    $('.wiz-step[data-s="3"]').style.display = 'none';
    return;
  }
  $('.wiz-step[data-s="2"]').style.display = '';
  $('.wiz-step[data-s="3"]').style.display = '';

  // next_step 由后端决定，前端不自己猜状态
  const s = r.next_step;
  if(s === 'launch'){
    wizGo(1);
    wizState('');
  } else if(s === 'login'){
    wizGo(2, [1]);
    wizState(`${r.browser_name}已经打开了，请在那个窗口里扫码登录`);
  } else if(s === 'confirm'){
    wizGo(2, [1]);
    wizState('浏览器已经开着。如果你确定登录过了，点上面的按钮让程序确认一下');
  } else if(s === 'ready'){
    wizGo(3, [1,2]);
    wizState('✅ 已经连上了，可以开始采集', 'ok');
  } else if(s === 'install'){
    wizGo(1);
    wizState('没找到浏览器，需要先安装', 'err');
  }
}

async function launchBrowser(){
  const btn = $('#wz1 .btn');
  const old = btn.textContent;
  btn.disabled = true; btn.textContent = '正在启动…';
  wizState('正在打开浏览器窗口，请稍等…');

  const r = await post('/api/collect/launch-browser');
  btn.disabled = false; btn.textContent = old;

  WIZ.state = r;
  if(r.ok){
    wizState('浏览器已启动。如果没看到窗口，看一下任务栏。', 'ok');
    // 给浏览器一点时间把登录页加载出来
    setTimeout(async () => {
      const s2 = await post('/api/collect/status');
      WIZ.state = s2;
      applyWizardState(s2);
    }, 2500);
  } else {
    wizState(r.message || '启动失败，请稍后再试一次', 'err');
  }
}

async function checkLogin(){
  const btn = $('#wz2 .btn');
  btn.disabled = true; btn.textContent = '正在检查…';
  wizState('正在确认登录状态…');

  const r = await post('/api/collect/check');
  btn.disabled = false; btn.textContent = '我登录好了';
  WIZ.state = r;

  if(r.ok){
    wizGo(3, [1,2]);
    wizState('✅ 登录成功，填个关键词就可以开始了', 'ok');
    const kw = $('#cKw'); if(kw && kw.value) $('#dcKw').value = kw.value;
    const city = $('#cCity'); if(city && city.value) $('#dcCity').value = city.value;
  } else if(r.logged_in === false){
    wizState('还是没检测到登录。请确认你在浏览器窗口里真的扫码登录成功了', 'err');
  } else {
    wizState('判断不出来。请在浏览器里打开 zhipin.com 首页，确认已登录后再点一次', 'err');
  }
}

async function runCollect(fromWizard){
  const kw  = $('#dcKw').value.trim();
  const city= $('#dcCity').value.trim();
  const pg  = parseInt($('#dcPages').value || 3, 10);
  const el  = $('#collectOut');

  el.innerHTML = '<div style="color:var(--ink-600);font-size:13.5px">'
    + '采集中…（保守模式要几分钟，页面别关）</div>';

  const r = await post('/api/collect/run',
    {keyword: kw || 'Java', city, pages: pg});

  if(!r.ok && r.show_wizard){
    // 中途状态掉了（比如浏览器被关了）—— 直接把人带回该做的那一步
    el.innerHTML = '';
    applyWizardState(await post('/api/collect/status'));
    return;
  }
  if(!r.ok && !r.headline){
    el.innerHTML = `<div style="color:var(--bad-600)">✗ ${esc(r.error)}</div>`;
    return;
  }

  const head = r.headline || (r.ok ? '完成' : '失败');
  const color = r.ok ? 'var(--ok-600)' : 'var(--bad-600)';
  el.innerHTML = `
    <div style="font-size:14.5px;font-weight:600;color:${color}">${esc(head)}</div>
    <div class="dim" style="margin-top:6px">
      页数 ${r.pages_done}/${r.pages_total}${r.skipped?`（跳过 ${r.skipped}）`:''}
      　完整率 ${Math.round((r.completeness||0)*100)}%　耗时 ${Math.round(r.elapsed||0)}s
    </div>
    ${(r.warnings||[]).map(w=>`<div class="dim" style="margin-top:4px">⚠ ${esc(w)}</div>`).join('')}
    ${(r.errors||[]).map(x=>`<div style="color:var(--bad-600);font-size:12.5px;margin-top:4px">✗ ${esc(x)}</div>`).join('')}
    ${r.ok ? '<div style="margin-top:12px"><button class="btn sm" onclick="closeDrawer(\'collect\');load();loadToday()">去看看岗位</button></div>' : ''}`;
  if(r.ok){ load(); loadToday(); loadDataStats(); }
}

function openCollectSettings(){ closeDrawer('collect'); showTab('settings'); }

/* 每日自动采集 —— 以前这里写的是「去终端敲 install-task」，
   对大学生等于劝退。改成按钮：开启 / 重新开启，状态一律问后端。 */
async function loadAutoStatus(){
  const r = await post('/api/collect/auto/status');
  const btn = $('#autoBtn');
  if(!btn) return;
  if(!r.ok) return;

  // 半装状态（只有采集任务、没浏览器任务）不能显示成"已开启"，
  // 那样用户会以为每天都在跑，其实每天都会失败
  const halfBroken = r.installed && !r.browser_task;
  btn.textContent = halfBroken ? '重新开启每日自动采集'
                : r.installed  ? '重新设置每日自动采集'
                : '开启每日自动采集';

  $('#autoNote').innerHTML = r.installed
    ? (halfBroken
        ? `<b style="color:var(--warn-700)">${esc(r.message)}</b><br>
           没有开机启动浏览器这一项，自动采集会失败。点上面的按钮重新设置一次就好。`
        : `<b style="color:var(--ok-600)">${esc(r.message)}</b><br>
           关机的那几天不会跑 —— 回来点首页「立即采集」补一次就行。`)
    : '上面三步做完，岗位就会攒起来。还想更省事，可以让电脑<b>每天定时自己采一次</b>。';
}

async function toggleAuto(){
  const btn = $('#autoBtn'), res = $('#autoRes');
  btn.disabled = true;
  res.textContent = '正在设置…';
  const r = await post('/api/collect/auto/install', {
    keyword: ($('#cKw')  && $('#cKw').value.trim())  || 'Java',
    city:    ($('#cCity') && $('#cCity').value.trim()) || '',
    at:      $('#autoAt') ? $('#autoAt').value : '09:30',
  });
  btn.disabled = false;
  if(r.ok){
    res.innerHTML = `<span style="color:var(--ok-600)">${esc(r.message)}</span>`;
    toast('每日自动采集已开启');
  } else {
    res.innerHTML = `<span style="color:var(--bad-600)">✗ ${esc(r.error || '设置失败')}</span>`;
  }
  await loadAutoStatus();
}

/* 设置页里的采集入口也走同一个向导 */
async function checkBrowser(fromSettings){
  if(fromSettings){ openCollectSettings(); return; }
  openCollectWizard();
}

async function loadCollectHist(){
  const c = await J('/api/collect/health');
  const hist = c.recent || [];
  $('#dsCollect').textContent = hist.length
    ? hist.filter(r=>r.ok).reduce((a,b)=>a+b.inserted,0) + ' 个'
    : '未启用';
  if(!hist.length){ $('#collectHist').innerHTML = ''; return; }
  $('#collectHist').innerHTML = '<div class="sec-hd" style="margin-top:16px"><h3>最近采集</h3></div>' +
    hist.map(r => `
      <div class="chist">
        <span class="st-ic">${r.ok?'✅':'❌'}</span>
        <div style="flex:1;min-width:0">
          ${String(r.started_at).slice(5,16)}　${esc(r.source)}　新增 <b>${r.inserted}</b>
          ${r.errors && r.errors.length ? `<div class="ch-msg">${esc(r.errors[0])}</div>` : ''}
        </div>
      </div>`).join('');
}

async function loadDataStats(){
  const [j, t, r] = await Promise.all([
    J('/api/summary'), J('/api/tracking/summary'), J('/api/resumes')]);
  $('#dsJobs').textContent = `${j.total} 个岗位`;
  $('#dsApps').textContent = `${t.summary.total} 条投递`;
  $('#dsRes').textContent = `${(r.resumes||[]).length} 个版本`;
  await loadCollectHist();
  await loadAutoStatus();
}

async function saveLLM(){
  const body = { base_url:$('#llmUrl').value.trim(), api_key:$('#llmKey').value.trim(),
                 model:$('#llmModel').value.trim() };
  const r = await post('/api/llm', body);
  const el = $('#llmRes');
  if(!r.ok){ el.innerHTML = `<span style="color:var(--bad-600)">${esc(r.error)}</span>`; return; }
  el.innerHTML = `<span style="color:var(--ok-600)">✓ 已保存（Key ${esc(r.config.masked_key)}）</span>`;
  loadLLM();
}
async function loadLLM(){
  const r = await J('/api/llm');
  const c = r.config || {};
  $('#llmUrl').value = c.base_url || '';
  $('#llmModel').value = c.model || '';
  $('#llmNote').innerHTML = c.configured
    ? `已接入 <b>${esc(c.model)}</b>，每个岗位下面都有「让模型再读一遍」的按钮`
    : '不填也能用 —— 规则引擎照常工作，这里只是补一层语义理解';
  state.llmReady = !!c.configured;
  renderCards();
}
async function testLLM(){
  const el = $('#llmRes'); el.textContent = '测试中…';
  const r = await post('/api/llm/test');
  el.innerHTML = r.ok
    ? `<span style="color:var(--ok-600)">✓ 连通正常（${r.elapsed}s）</span>`
    : `<span style="color:var(--bad-600)">✗ ${esc(r.error)}</span>`;
}

async function loadDataStats(){
  const [j, t, r] = await Promise.all([
    J('/api/summary'), J('/api/tracking/summary'), J('/api/resumes')]);
  $('#dsJobs').textContent = `${j.total} 个岗位`;
  $('#dsApps').textContent = `${t.summary.total} 条投递`;
  $('#dsRes').textContent = `${(r.resumes||[]).length} 个版本`;
}

$$('[data-k]').forEach(el => {
  el.addEventListener('keydown', e => { if(e.key === 'Enter'){ e.preventDefault(); load(); }});
});
$('#importText').addEventListener('keydown', e => {
  if(e.key === 'Enter' && (e.ctrlKey || e.metaKey)) doImport();
});

/* ── 启动 ───────────────────────────────────────────────────
   实测踩过：用正则批量替换函数调用名时，把这一整行一起删掉了，
   结果页面不报错、但除了首屏骨架什么都不加载 —— 最难查的一类问题。
   所以启动入口单独放在最后，并被 DOM 契约检查覆盖。 */
showTab(location.hash.slice(1) || 'today', false);
loadToday();
load();
loadLLM();

/* 启动 */
showTab(location.hash.slice(1) || 'today', false);
loadToday(); load(); loadLLM();

/* 启动自检：把绑定结果写进 DOM，排查「点了没反应」这类问题时
   比肉眼猜快得多。实测踩过：tab 点击无效，靠猜会以为是 CSS 问题。 */
window.addEventListener('load', () => {
  const tabs = document.querySelectorAll('.tab');
  const bound = [...tabs].filter(t => typeof t.onclick === 'function').length;
  const diag = document.createElement('div');
  diag.id = '_diag';
  diag.style.cssText = 'position:fixed;left:-9999px';
  diag.textContent = JSON.stringify({
    showTab: typeof window.showTab,
    tabs: tabs.length,
    bound,
    panes: document.querySelectorAll('.pane').length,
    jsVersion: '2.1',
  });
  document.body.appendChild(diag);
});
