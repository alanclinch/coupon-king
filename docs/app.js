'use strict';

// ── Config ────────────────────────────────────────────────────────────────────
let API_BASE = localStorage.getItem('api_base') || 'https://coupon-king-api.onrender.com';

// ── State ─────────────────────────────────────────────────────────────────────
const state = {
  view:            'fixtures',
  fixtures:        [],
  leagues:         [],
  coupon:          JSON.parse(localStorage.getItem('coupon') || '[]'),
  leagueFilter:    null,
  dateFilter:      'week',    // 'today' | 'tomorrow' | 'week' | 'sat3pm'
  picks:           [],
  picksLoading:    false,
  picksLoaded:     false,
  picksBetType:    'BTTS',
  picksDateFilter: 'week',
  analysis:        null,
  analysisLoading: false,
  analysisOpen:    false,
  loading:         false,
  error:           null,
};

// ── Persistence ───────────────────────────────────────────────────────────────
function saveCoupon() {
  localStorage.setItem('coupon', JSON.stringify(state.coupon));
}

// ── Date / time helpers ───────────────────────────────────────────────────────
function fmtTime(iso) {
  if (!iso) return '';
  return new Date(iso).toLocaleTimeString('en-GB', {
    hour: '2-digit', minute: '2-digit', timeZone: 'Europe/London',
  });
}

function fmtDate(iso) {
  if (!iso) return '';
  return new Date(iso).toLocaleDateString('en-GB', {
    weekday: 'short', day: 'numeric', month: 'short', timeZone: 'Europe/London',
  });
}

function isoDateOffset(days) {
  const d = new Date();
  d.setDate(d.getDate() + days);
  return d.toISOString().split('T')[0];
}

// ── Sanitisation ──────────────────────────────────────────────────────────────
function esc(str) {
  return String(str ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

// ── API ───────────────────────────────────────────────────────────────────────
async function apiFetch(path, opts = {}) {
  const res = await fetch(API_BASE + path, opts);
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

async function loadLeagues() {
  if (state.leagues.length) return;
  state.leagues = await apiFetch('/leagues');
}

async function loadFixtures() {
  state.loading = true;
  state.error   = null;
  render();

  try {
    let path;
    if (state.dateFilter === 'sat3pm') {
      path = '/fixtures/saturday3pm';
    } else {
      const p = new URLSearchParams();
      if (state.dateFilter === 'today') {
        p.set('date_from', isoDateOffset(0));
        p.set('date_to',   isoDateOffset(0));
      } else if (state.dateFilter === 'tomorrow') {
        p.set('date_from', isoDateOffset(1));
        p.set('date_to',   isoDateOffset(1));
      } else {
        p.set('date_from', isoDateOffset(0));
        p.set('date_to',   isoDateOffset(7));
      }
      if (state.leagueFilter) p.set('league_id', state.leagueFilter);
      path = `/fixtures?${p}`;
    }
    state.fixtures = await apiFetch(path);
  } catch (e) {
    state.error = 'Could not load fixtures — check your API connection in Settings.';
  }

  state.loading = false;
  render();
}

async function loadPicks() {
  state.picksLoading = true;
  state.picksLoaded  = false;
  state.error        = null;
  render();

  try {
    const p = new URLSearchParams();
    p.set('bet_type', state.picksBetType);
    if (state.picksDateFilter === 'sat3pm') {
      p.set('saturday3pm', 'true');
    } else {
      if (state.picksDateFilter === 'today') {
        p.set('date_from', isoDateOffset(0));
        p.set('date_to',   isoDateOffset(0));
      } else if (state.picksDateFilter === 'tomorrow') {
        p.set('date_from', isoDateOffset(1));
        p.set('date_to',   isoDateOffset(1));
      } else {
        p.set('date_from', isoDateOffset(0));
        p.set('date_to',   isoDateOffset(7));
      }
    }
    state.picks = await apiFetch(`/picks?${p}`);
  } catch (e) {
    state.error = 'Could not load picks — check your API connection in Settings.';
    state.picks = [];
  }

  state.picksLoading = false;
  state.picksLoaded  = true;
  render();
}

// ── Coupon actions ────────────────────────────────────────────────────────────
function inCoupon(id) {
  return state.coupon.some(p => p.fixtureId === id);
}

function addToCoupon(fixture) {
  if (fixture.home_goals !== null && fixture.home_goals !== undefined) return;
  if (inCoupon(fixture.id)) {
    state.coupon = state.coupon.filter(p => p.fixtureId !== fixture.id);
  } else {
    state.coupon.push({
      fixtureId:   fixture.id,
      homeTeam:    fixture.home_team,
      awayTeam:    fixture.away_team,
      kickoffTime: fixture.kickoff_time,
      leagueName:  fixture.league_name,
      market:      '1',
    });
  }
  saveCoupon();
  render();
}

function removePick(fixtureId) {
  state.coupon = state.coupon.filter(p => p.fixtureId !== fixtureId);
  saveCoupon();
  render();
}

function updateMarket(fixtureId, market) {
  const pick = state.coupon.find(p => p.fixtureId === fixtureId);
  if (pick) { pick.market = market; saveCoupon(); }
}

function clearCoupon() {
  if (!confirm('Clear your entire coupon?')) return;
  state.coupon = [];
  state.analysis = null;
  state.analysisOpen = false;
  saveCoupon();
  render();
}

async function analyseCoupon() {
  if (!state.coupon.length) return;
  state.analysis        = null;
  state.analysisOpen    = true;
  state.analysisLoading = true;
  render();

  try {
    const body = state.coupon.map(p => ({ fixture_id: p.fixtureId, market: p.market }));
    state.analysis = await apiFetch('/coupon/check', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
  } catch (e) {
    state.error = 'Could not analyse coupon — check your API connection in Settings.';
  }

  state.analysisLoading = false;
  render();
}

function addPickToCoupon(pickIndex) {
  const pick = state.picks[pickIndex];
  if (!pick) return;
  // Map market from bet type
  const marketMap = {
    'BTTS':        'BTTS_YES',
    'OVER25':      'OVER25',
    'WIN':         pick.pick === 'home' ? '1' : pick.pick === 'away' ? '2' : '1',
    'BTTS_WIN':    pick.pick === 'home' ? '1' : pick.pick === 'away' ? '2' : '1',
    'BTTS_NODRAW': 'BTTS_YES',
    'BTTS_OVER25': 'BTTS_YES',
  };
  const market = marketMap[pick.bet_type] || '1';

  if (inCoupon(pick.id)) {
    state.coupon = state.coupon.filter(p => p.fixtureId !== pick.id);
  } else {
    state.coupon.push({
      fixtureId:   pick.id,
      homeTeam:    pick.home_team,
      awayTeam:    pick.away_team,
      kickoffTime: pick.kickoff_time,
      leagueName:  pick.league_name,
      market,
    });
  }
  saveCoupon();
  render();
}

// ── Filter actions ────────────────────────────────────────────────────────────
function setDateFilter(f) {
  state.dateFilter   = f;
  state.leagueFilter = null;
  state.fixtures     = [];
  loadFixtures();
}

function setLeagueFilter(id) {
  state.leagueFilter = id;
  state.fixtures     = [];
  if (state.dateFilter === 'sat3pm') state.dateFilter = 'week';
  loadFixtures();
}

function setPicksBetType(bt) {
  state.picksBetType = bt;
  loadPicks();
}

function setPicksDateFilter(f) {
  state.picksDateFilter = f;
  loadPicks();
}

function setView(v) {
  state.view  = v;
  state.error = null;
  if (v === 'fixtures' && !state.fixtures.length) {
    loadFixtures();
  } else if (v === 'picks' && !state.picksLoaded && !state.picksLoading) {
    loadPicks();
  } else {
    render();
  }
}

function updateApiBase(url) {
  API_BASE = url.trim().replace(/\/$/, '');
  localStorage.setItem('api_base', API_BASE);
}

// ── League display order ──────────────────────────────────────────────────────
const LEAGUE_ORDER = [
  'Champions League',
  'Europa League',
  'Conference League',
  'FA Cup',
  'EFL Cup',
  'Premier League',
  'Championship',
  'League One',
  'League Two',
  'National League',
  'Scottish Cup',
  'Scottish League Cup',
  'Scottish Premiership',
  'Scottish Championship',
  'Scottish League One',
  'Scottish League Two',
];

function leagueSortIndex(name) {
  const i = LEAGUE_ORDER.indexOf(name);
  return i === -1 ? 999 : i;
}

// ── Market labels ─────────────────────────────────────────────────────────────
const MARKETS = {
  '1':        'Home Win',
  'X':        'Draw',
  '2':        'Away Win',
  'BTTS_YES': 'BTTS Yes',
  'BTTS_NO':  'BTTS No',
  'OVER25':   'Over 2.5 Goals',
  'UNDER25':  'Under 2.5 Goals',
};

const BET_TYPES = {
  'BTTS':        'BTTS',
  'OVER25':      'Over 2.5',
  'WIN':         'Pick Winner',
  'BTTS_WIN':    'BTTS & Win',
  'BTTS_NODRAW': 'BTTS No Draw',
  'BTTS_OVER25': 'BTTS + Over 2.5',
};

function marketOptions(selected) {
  return Object.entries(MARKETS)
    .map(([v, l]) => `<option value="${v}"${v === selected ? ' selected' : ''}>${l}</option>`)
    .join('');
}

// ── Form guide renderer ───────────────────────────────────────────────────────
function formGuide(str) {
  if (!str) return '';
  return str.split('').map(c =>
    `<span class="form-char ${c}">${c}</span>`
  ).join('');
}

// ── View renderers ────────────────────────────────────────────────────────────
function renderFixturesView() {
  if (state.loading) return '<div class="loading">Loading fixtures&hellip;</div>';
  if (state.error)   return `<div class="error-msg">${esc(state.error)}</div>`;
  if (!state.fixtures.length) return '<div class="empty-state">No fixtures found for this filter.</div>';

  // Group by day, then league within each day
  const days = {};
  for (const f of state.fixtures) {
    const day = f.kickoff_time.split('T')[0];
    if (!days[day]) days[day] = {};
    if (!days[day][f.league_name]) days[day][f.league_name] = [];
    days[day][f.league_name].push(f);
  }

  const sortedDays = Object.keys(days).sort();

  let html = '<div class="fixtures-list">';
  for (const day of sortedDays) {
    const dayDate = new Date(day + 'T12:00:00Z');
    const dayLabel = dayDate.toLocaleDateString('en-GB', {
      weekday: 'long', day: 'numeric', month: 'long', timeZone: 'Europe/London',
    });
    html += `<div class="day-header">${esc(dayLabel)}</div>`;

    const leagues = Object.keys(days[day]).sort(
      (a, b) => leagueSortIndex(a) - leagueSortIndex(b)
    );

    for (const league of leagues) {
      const fixtures = days[day][league];
      html += `<div class="league-header">${esc(league)}</div>`;
      for (const f of fixtures) {
        const sel       = inCoupon(f.id);
        const completed = f.home_goals !== null && f.home_goals !== undefined;
        const middle    = completed
          ? `<span class="fixture-score">${f.home_goals}&ndash;${f.away_goals}</span>`
          : `<span class="vs-sep">vs</span>`;
        const classes   = ['fixture-card', sel ? 'selected' : '', completed ? 'completed' : ''].filter(Boolean).join(' ');

        html += `
          <div class="${classes}">
            <div class="fixture-main">
              <div class="fixture-meta">
                <span class="fixture-time">${esc(fmtDate(f.kickoff_time))} &middot; ${esc(fmtTime(f.kickoff_time))}</span>
                <span class="fixture-tick" aria-label="Selected">&#10003;</span>
              </div>
              <div class="fixture-teams">
                <span class="team-name home">${esc(f.home_team)}</span>
                ${middle}
                <span class="team-name away">${esc(f.away_team)}</span>
              </div>
            </div>
            ${!completed ? `<button class="add-btn${sel ? ' added' : ''}" onclick="handleAddBtn(event,${f.id})" aria-label="${sel ? 'Remove from coupon' : 'Add to coupon'}">${sel ? '&#10003;' : '+'}</button>` : ''}
          </div>`;
      }
    }
  }
  html += '</div>';
  return html;
}

function renderPicksView() {
  const betTypePills = Object.entries(BET_TYPES).map(([key, label]) => {
    const active = state.picksBetType === key ? ' active' : '';
    return `<button class="filter-pill${active}" onclick="setPicksBetType('${key}')">${esc(label)}</button>`;
  }).join('');

  const datePills = [
    { key: 'today',    label: 'Today' },
    { key: 'tomorrow', label: 'Tomorrow' },
    { key: 'week',     label: 'This Week' },
    { key: 'sat3pm',   label: 'Sat 3pm', extra: ' sat3pm' },
  ].map(({ key, label, extra = '' }) => {
    const active = state.picksDateFilter === key ? ' active' : '';
    return `<button class="filter-pill${extra}${active}" onclick="setPicksDateFilter('${key}')">${label}</button>`;
  }).join('');

  let html = `
    <div class="picks-filters">
      <div class="filter-bar no-border">${betTypePills}</div>
      <div class="filter-bar">${datePills}</div>
    </div>`;

  if (state.picksLoading) {
    return html + '<div class="loading">Finding best picks&hellip;</div>';
  }
  if (state.error) {
    return html + `<div class="error-msg">${esc(state.error)}</div>`;
  }
  if (!state.picks.length) {
    return html + '<div class="empty-state">No picks available for this filter. Form data may still be loading.</div>';
  }

  html += '<div class="picks-list">';
  state.picks.forEach((pick, i) => {
    const sel = inCoupon(pick.id);
    const scoreColor = pick.score >= 70 ? 'var(--accent)' : pick.score >= 45 ? 'var(--warning)' : 'var(--danger)';
    const pickBadge = pick.pick
      ? `<span class="pick-direction ${pick.pick}">${pick.pick === 'home' ? esc(pick.home_team) : esc(pick.away_team)}</span>`
      : '';

    const reasons = (pick.reasoning || []).map(r =>
      `<div class="pick-reason">${esc(r)}</div>`
    ).join('');

    html += `
      <div class="pick-card">
        <div class="pick-score-bar">
          <div class="pick-score-fill" style="width:${pick.score}%;background:${scoreColor}"></div>
        </div>
        <div class="pick-body">
          <div class="pick-header-row">
            <div class="pick-teams">${esc(pick.home_team)} <span class="vs-sep">vs</span> ${esc(pick.away_team)}</div>
            <div class="pick-score-num" style="color:${scoreColor}">${pick.score}</div>
          </div>
          <div class="pick-sub">${esc(pick.league_name)} &middot; ${esc(fmtDate(pick.kickoff_time))} ${esc(fmtTime(pick.kickoff_time))}</div>
          ${pickBadge}
          <div class="pick-reasons">${reasons}</div>
          <button class="pick-add-btn${sel ? ' added' : ''}" onclick="addPickToCoupon(${i})">
            ${sel ? '&#10003; Added to Coupon' : '+ Add to Coupon'}
          </button>
        </div>
      </div>`;
  });

  html += '</div>';
  return html;
}

function renderCouponView() {
  if (!state.coupon.length) {
    return `
      <div class="coupon-empty">
        <h3>Your coupon is empty</h3>
        <p>Go to Fixtures and tap <strong>+</strong> on a match to add it, or use Smart Picks.</p>
      </div>`;
  }

  let html = `<div class="section-label">${state.coupon.length} selection${state.coupon.length !== 1 ? 's' : ''}</div>`;
  html += '<div class="coupon-picks">';

  for (const pick of state.coupon) {
    html += `
      <div class="coupon-pick">
        <div class="pick-header">
          <div>
            <div class="pick-match">${esc(pick.homeTeam)} vs ${esc(pick.awayTeam)}</div>
            <div class="pick-meta">${esc(pick.leagueName)} &middot; ${esc(fmtDate(pick.kickoffTime))} ${esc(fmtTime(pick.kickoffTime))}</div>
          </div>
          <button class="btn-remove" onclick="removePick(${pick.fixtureId})" aria-label="Remove selection">&times;</button>
        </div>
        <select class="market-select" onchange="updateMarket(${pick.fixtureId}, this.value)">
          ${marketOptions(pick.market)}
        </select>
      </div>`;
  }

  html += `</div>
    <div class="coupon-actions">
      <button class="btn-primary" onclick="analyseCoupon()" ${state.analysisLoading ? 'disabled' : ''}>
        ${state.analysisLoading ? 'Analysing&hellip;' : state.analysisOpen ? 'Re-analyse Coupon' : 'Analyse Coupon'}
      </button>
      <button class="btn-secondary" onclick="clearCoupon()">Clear coupon</button>
    </div>`;

  // Inline analysis
  if (state.analysisOpen) {
    if (state.analysisLoading) {
      html += '<div class="loading">Analysing&hellip;</div>';
    } else if (state.analysis && state.analysis.length) {
      const highCount = state.analysis.filter(r => r.risk === 'high').length;
      const medCount  = state.analysis.filter(r => r.risk === 'medium').length;
      let summary     = 'Your coupon looks solid. Good luck!';
      if (highCount > 0) {
        summary = `${highCount} high-risk selection${highCount > 1 ? 's' : ''} flagged — worth reviewing before placing.`;
      } else if (medCount > 0) {
        summary = `${medCount} selection${medCount > 1 ? 's' : ''} with a concern. Proceed with caution.`;
      }

      html += `<div class="analysis-summary">${esc(summary)}</div><div class="analysis-list">`;

      for (const r of state.analysis) {
        const riskLabel = r.risk.charAt(0).toUpperCase() + r.risk.slice(1) + ' risk';
        const market    = MARKETS[r.market] || r.market;
        const flagsHtml = (r.flags && r.flags.length)
          ? r.flags.map(f => `<div class="flag-item">${esc(f)}</div>`).join('')
          : '<div class="no-flags">No concerns with this selection.</div>';

        const formRow = (r.home_form || r.away_form) ? `
          <div class="form-guide">
            ${esc(r.home_team)}: ${formGuide(r.home_form || '')} &nbsp;
            ${esc(r.away_team)}: ${formGuide(r.away_form || '')}
          </div>` : '';

        html += `
          <div class="analysis-card risk-${r.risk}">
            <div class="analysis-header">
              <div>
                <div class="analysis-match">${esc(r.home_team)} vs ${esc(r.away_team)}</div>
                <div class="analysis-sub">${esc(market)}${formRow}</div>
              </div>
              <span class="risk-badge ${r.risk}">${riskLabel}</span>
            </div>
            <div class="analysis-flags">${flagsHtml}</div>
          </div>`;
      }
      html += '</div>';
    }
  }

  return html;
}

function renderSettingsView() {
  return `
    <div class="settings-section">
      <div class="settings-title">API Connection</div>
      <div class="settings-card">
        <div class="settings-row column">
          <div class="settings-label">Backend URL (change after deploying to Render)</div>
          <input class="api-url-input" type="url" value="${esc(API_BASE)}"
            placeholder="http://localhost:8000"
            onchange="updateApiBase(this.value)" />
        </div>
      </div>
    </div>

    <div class="settings-section">
      <div class="settings-title">Coupon</div>
      <div class="settings-card">
        <div class="settings-row">
          <span>${state.coupon.length} selection${state.coupon.length !== 1 ? 's' : ''} saved</span>
          <button class="btn-danger-text" onclick="clearCoupon()">Clear all</button>
        </div>
      </div>
    </div>

    <div class="settings-section">
      <div class="settings-title">About</div>
      <div class="settings-card">
        <div class="settings-row">
          <span>Coupon King</span>
          <span style="color:var(--text-muted);font-size:13px">v1.0 &middot; UK Football</span>
        </div>
        <div class="settings-row">
          <span style="color:var(--text-muted);font-size:13px">Data updated nightly via GitHub Actions</span>
        </div>
      </div>
    </div>

    <div class="gamble-bar">
      <strong>Gamble Responsibly &mdash; 18+ only</strong><br>
      For help visit <a href="https://www.begambleaware.org" target="_blank" rel="noopener noreferrer">BeGambleAware.org</a>
      or call <strong>0808 8020 133</strong> (free, 24/7)<br>
      <a href="https://www.gamcare.org.uk" target="_blank" rel="noopener noreferrer">GamCare</a> &middot;
      <a href="https://www.gamstop.co.uk" target="_blank" rel="noopener noreferrer">GAMSTOP</a>
    </div>`;
}

function renderFilterBar() {
  if (state.view !== 'fixtures') return '';

  const datePills = [
    { key: 'today',    label: 'Today' },
    { key: 'tomorrow', label: 'Tomorrow' },
    { key: 'week',     label: 'This Week' },
    { key: 'sat3pm',   label: 'Sat 3pm', extra: ' sat3pm' },
  ].map(({ key, label, extra = '' }) => {
    const active = state.dateFilter === key ? ' active' : '';
    return `<button class="filter-pill${extra}${active}" onclick="setDateFilter('${key}')">${label}</button>`;
  }).join('');

  const leaguePills = state.leagues.length ? [
    `<button class="filter-pill${state.leagueFilter === null ? ' active' : ''}" onclick="setLeagueFilter(null)">All Leagues</button>`,
    ...state.leagues.map(l =>
      `<button class="filter-pill${state.leagueFilter === l.id ? ' active' : ''}" onclick="setLeagueFilter(${l.id})">${esc(l.name)}</button>`
    ),
  ].join('') : '';

  return `<div class="filter-bar">${datePills}${leaguePills}</div>`;
}

// ── Main render ───────────────────────────────────────────────────────────────
function render() {
  // Nav active state
  document.querySelectorAll('.nav-btn').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.view === state.view);
  });

  // Coupon pip on nav
  const pip = document.getElementById('nav-pip');
  if (pip) pip.classList.toggle('visible', state.coupon.length > 0);

  // Header badge (shows count on non-coupon views)
  const badge = document.getElementById('header-badge');
  if (badge) {
    badge.textContent = state.coupon.length;
    badge.classList.toggle('visible', state.coupon.length > 0);
  }

  // Filter bar (fixtures only)
  const filterArea = document.getElementById('filter-area');
  if (filterArea) filterArea.innerHTML = renderFilterBar();

  // Main content
  const main = document.getElementById('main-content');
  if (!main) return;
  if      (state.view === 'fixtures') main.innerHTML = renderFixturesView();
  else if (state.view === 'picks')    main.innerHTML = renderPicksView();
  else if (state.view === 'coupon')   main.innerHTML = renderCouponView();
  else if (state.view === 'settings') main.innerHTML = renderSettingsView();
}

// ── Global handlers (called from inline onclick) ───────────────────────────────
function handleAddBtn(event, id) {
  event.stopPropagation();
  const fixture = state.fixtures.find(f => f.id === id);
  if (fixture) addToCoupon(fixture);
}

// ── Init ──────────────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', async () => {
  // Wire nav buttons
  document.querySelectorAll('.nav-btn').forEach(btn => {
    btn.addEventListener('click', () => setView(btn.dataset.view));
  });

  // Load league list for filter pills (non-fatal if API is offline)
  try { await loadLeagues(); } catch (_) {}

  // Initial load
  await loadFixtures();

  // Register service worker
  if ('serviceWorker' in navigator) {
    navigator.serviceWorker.register('/sw.js').catch(() => {});
  }
});
