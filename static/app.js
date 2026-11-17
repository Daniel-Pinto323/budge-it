'use strict';

// budge it browser client. All data lives on the server; this file only renders
// /api/state and sends edits back. Every server-supplied string is escaped before
// it reaches innerHTML, because merchant and institution names come from banks.

const $ = selector => document.querySelector(selector);
const $$ = selector => [...document.querySelectorAll(selector)];
const esc = value => String(value ?? '').replace(/[&<>"']/g, char => (
  {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[char]));

const FLOW_CATEGORIES = ['Income', 'Transfer'];   // never counted as spending
const ROW_LIMIT = 300;
const LINK_KEY = 'budgeit-plaid-link';
const ICON_COLORS = ['#f2e6d1', '#eae8f4', '#e5edf5', '#e8e9e7', '#f7e6df', '#dff0e4', '#f5e3e3', '#faf0d9'];
const STATUS = {
  healthy: ['', '● Healthy'],
  needs_reauth: ['warn', '● Needs sign-in'],
  error: ['bad', '● Sync problem'],
};

let session = null;
let csrf = '';
let state = null;
let month = monthKey(new Date());
let stateLoadedAt = 0;
let mxReconnect = false;

// --- Formatting ---------------------------------------------------------------

const wholeDollars = new Intl.NumberFormat('en-US', {style: 'currency', currency: 'USD', maximumFractionDigits: 0});
const exactDollars = new Intl.NumberFormat('en-US', {style: 'currency', currency: 'USD'});
const money = cents => wholeDollars.format(cents / 100);
const precise = cents => exactDollars.format(cents / 100);
const signedAmount = cents => (cents < 0 ? '+' : '') + precise(-cents);
const toCents = value => Math.round(Number(value) * 100);

function monthKey(date) {
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}`;
}
function todayISO() {
  const now = new Date();
  return `${monthKey(now)}-${String(now.getDate()).padStart(2, '0')}`;
}
function parseDay(iso) {
  const [year, monthNumber, day] = iso.split('-').map(Number);
  return new Date(year, monthNumber - 1, day || 1);
}
function shortDate(iso) {
  const date = parseDay(iso);
  const options = {month: 'short', day: 'numeric'};
  if (date.getFullYear() !== new Date().getFullYear()) options.year = 'numeric';
  return date.toLocaleDateString('en-US', options);
}
function shiftMonth(key, delta) {
  const date = parseDay(`${key}-01`);
  date.setMonth(date.getMonth() + delta);
  return monthKey(date);
}
function ago(stamp) {
  if (!stamp) return 'never';
  const minutes = Math.max(0, Math.round((Date.now() - new Date(stamp).getTime()) / 60000));
  if (minutes < 2) return 'just now';
  if (minutes < 60) return `${minutes}m ago`;
  if (minutes < 48 * 60) return `${Math.round(minutes / 60)}h ago`;
  return `${Math.round(minutes / 1440)}d ago`;
}
function initials(text) {
  const words = String(text).replace(/[^\p{L}\p{N} ]/gu, ' ').trim().split(/\s+/).filter(Boolean);
  return (words.slice(0, 2).map(word => word[0]).join('') || '•').toUpperCase();
}
function iconMarkup(text) {
  let hash = 0;
  for (const char of String(text)) hash = (hash * 31 + char.codePointAt(0)) >>> 0;
  return `<div class="merchant-icon" style="background:${ICON_COLORS[hash % ICON_COLORS.length]}">${esc(initials(text))}</div>`;
}

// --- Server -------------------------------------------------------------------

async function api(method, path, body) {
  const options = {method, credentials: 'same-origin', headers: {}};
  if (method !== 'GET') {
    options.headers['Content-Type'] = 'application/json';
    options.headers['X-CSRF-Token'] = csrf;
    options.body = JSON.stringify(body || {});
  }
  let response;
  try {
    response = await fetch(path, options);
  } catch {
    throw new Error('Could not reach the server.');
  }
  let data = {};
  try { data = await response.json(); } catch { /* empty or non-JSON body */ }
  if (response.status === 401 && path !== '/api/login') {
    showLogin();
    throw new Error(data.error || 'Sign in required.');
  }
  if (!response.ok) throw new Error(data.error || `Request failed (${response.status}).`);
  if (data.state) {
    state = data.state;
    stateLoadedAt = Date.now();
    renderAll();
  }
  return data;
}

/** Run a server action, toast the outcome, and never leave a rejected promise unhandled. */
async function act(work, success) {
  try {
    const result = await work();
    if (success) toast(typeof success === 'function' ? success(result) : success);
    return result;
  } catch (error) {
    toast(error.message || 'Something went wrong.');
    return null;
  }
}

function toast(message, action) {
  const el = $('#toast');
  el.textContent = message;
  if (action) {
    const button = document.createElement('button');
    button.type = 'button';
    button.textContent = action.label;
    button.addEventListener('click', () => { el.classList.remove('show'); action.run(); });
    el.append(button);
  }
  el.classList.add('show');
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => el.classList.remove('show'), action ? 8000 : 3200);
}

// --- Session ------------------------------------------------------------------

function showLogin() {
  session = null;
  state = null;
  $$('dialog[open]').forEach(dialog => dialog.close());
  $('#appShell').hidden = true;
  $('#loginScreen').hidden = false;
}

async function applySession(payload) {
  if (!payload || !payload.authenticated) { showLogin(); return; }
  session = payload;
  csrf = payload.csrf;
  $('#loginScreen').hidden = true;
  $('#appShell').hidden = false;
  $('#loginForm').reset();
  const name = payload.user.name;
  $('#avatar').textContent = initials(name).slice(0, 1);
  $('#profileName').textContent = name;
  $('#settingsUser').textContent = `${name} · ${payload.user.email}`;
  const hour = new Date().getHours();
  $('#greeting').textContent = `Good ${hour < 12 ? 'morning' : hour < 18 ? 'afternoon' : 'evening'}, ${name}.`;
  $('#todayLabel').textContent = new Date().toLocaleDateString('en-US', {weekday: 'long', month: 'long', day: 'numeric'});
  renderProviders();
  if (await act(() => api('GET', '/api/state'))) resumePlaidOAuth();
}

$('#loginForm').addEventListener('submit', async event => {
  event.preventDefault();
  const form = new FormData(event.currentTarget);
  const errorEl = $('#loginError');
  errorEl.hidden = true;
  try {
    await applySession(await api('POST', '/api/login', {email: form.get('email'), password: form.get('password')}));
  } catch (error) {
    errorEl.textContent = error.message;
    errorEl.hidden = false;
  }
});

// --- Budget maths -------------------------------------------------------------

const counted = t => !t.pending && !t.hidden;
const isFlow = t => FLOW_CATEGORIES.includes(t.category);
const isSpending = t => !isFlow(t) && (t.category !== 'Uncategorized' || t.amount > 0);

function monthSummary(key) {
  const byCategory = new Map();
  let spent = 0;
  let pending = 0;
  let uncategorized = 0;
  for (const t of state.transactions) {
    if (!t.date.startsWith(key) || t.hidden || !isSpending(t)) continue;
    if (t.pending) { if (t.amount > 0) pending += t.amount; continue; }
    spent += t.amount;
    byCategory.set(t.category, (byCategory.get(t.category) || 0) + t.amount);
    if (t.category === 'Uncategorized') uncategorized += t.amount;
  }
  const budget = state.categories.reduce((total, category) => total + category.budget, 0);
  return {byCategory, spent, pending, uncategorized, budget};
}
const hitBudget = (category, spent) => category.budget > 0 && spent >= category.budget;
function categoryItems(summary) {
  return state.categories.map(category => {
    const spent = Math.max(0, summary.byCategory.get(category.name) || 0);
    const ratio = category.budget ? spent / category.budget : (spent ? 2 : 0);
    return {...category, spent, ratio, status: ratio > 1 ? 'over' : hitBudget(category, spent) ? 'hit' : ratio > 0.8 ? 'near' : 'on'};
  });
}
const statusColor = item => ({over: '#cc725e', hit: '#cc725e', near: '#d9a13b', on: '#54a675'}[item.status]);
const STATUS_LABELS = {over: 'Budget passed!', hit: 'Budget hit', near: 'Almost there', on: 'On track'};
const leftLabel = item => item.spent > item.budget ? `${money(item.spent - item.budget)} over` : `${money(item.budget - item.spent)} left`;
function categoryNames() {
  return [...new Set(['Uncategorized', ...state.categories.map(category => category.name), ...state.special_categories])];
}
function netWorth() {
  const included = state.accounts.filter(account => account.included);
  const assets = included.filter(account => account.balance_cents >= 0);
  const debts = included.filter(account => account.balance_cents < 0);
  const sum = list => list.reduce((total, account) => total + account.balance_cents, 0);
  return {assets, debts, assetTotal: sum(assets), debtTotal: sum(debts), total: sum(included)};
}
const syncableConnections = () => state.connections.filter(connection => ['plaid', 'mx'].includes(connection.provider));
const realConnections = () => state.connections.filter(connection => !['demo', 'manual'].includes(connection.provider));

// --- Rendering ----------------------------------------------------------------

function renderAll() {
  if (!state) return;
  renderChrome();
  renderOverview();
  renderBudgets();
  renderTransactions();
  renderAccounts();
  renderWorth();
  renderSettings();
}

function renderChrome() {
  const label = parseDay(`${month}-01`);
  $$('[data-month-name]').forEach(el => { el.textContent = label.toLocaleDateString('en-US', {month: 'long'}); });
  $('#monthLabel').textContent = label.toLocaleDateString('en-US', {month: 'long', year: 'numeric'});
  $('#nextMonth').disabled = month >= monthKey(new Date());
  $('#demoBanner').hidden = !state.has_demo;
  // Only Plaid/MX connections can sync; CSV imports and manual accounts are updated by hand.
  const connections = syncableConnections();
  const latest = connections.map(connection => connection.last_success_at).filter(Boolean).sort().pop();
  const troubled = connections.some(connection => connection.status !== 'healthy');
  const handEntered = state.connections.some(connection => ['csv', 'manual'].includes(connection.provider));
  $('#syncChip').textContent = !connections.length ? (handEntered ? 'No linked accounts · CSV & manual data only' : 'No linked accounts')
    : troubled ? 'Connection needs attention' : latest ? `Synced ${ago(latest)}` : 'Not synced yet';
  $('#syncChip').title = connections.length ? '' : 'Nothing is linked through Plaid or MX, so nothing syncs automatically. Import CSVs or edit manual accounts to update your data.';
  $('#syncPulse').style.background = !connections.length ? '#c4ccc6' : troubled ? '#d9a13b' : '#58a779';
}

function transactionRow(t) {
  return `<div class="transaction-row clickable" role="button" tabindex="0" data-open-transaction="${esc(t.id)}" aria-label="Edit ${esc(t.merchant)}">${iconMarkup(t.merchant)}<div><div class="transaction-name">${esc(t.merchant)}${t.pending ? '<span class="pending-badge">PENDING</span>' : ''}${t.hidden ? '<span class="hidden-badge">HIDDEN</span>' : ''}</div><div class="transaction-meta">${esc(t.category)} · ${shortDate(t.date)}${t.notes ? ` · <span class="note-snippet">${esc(t.notes)}</span>` : ''}</div></div><div class="transaction-amount">${signedAmount(t.amount)}<small>${esc(t.account.split(' ••')[0])}</small></div></div>`;
}

function chartPoints(history, width, height, pad) {
  const points = history.length === 1 ? [history[0], history[0]] : history;
  const totals = points.map(point => point.total);
  const low = Math.min(...totals);
  const span = (Math.max(...totals) - low) || 1;
  return points.map((point, index) => [
    (index / (points.length - 1)) * width,
    height - pad - ((point.total - low) / span) * (height - pad * 2),
  ]);
}
const pathFrom = points => points.map(([x, y], index) => `${index ? 'L' : 'M'}${x.toFixed(1)},${y.toFixed(1)}`).join(' ');

function renderCharts() {
  const history = state.net_worth_history;
  if (!history.length) {
    $('#miniChart').innerHTML = '';
    $('#largeChart').innerHTML = '<p class="empty-note">Your net-worth history starts with your first sync.</p>';
    return;
  }
  const first = shortDate(history[0].day);
  const last = shortDate(history[history.length - 1].day);
  const mini = pathFrom(chartPoints(history, 300, 48, 5));
  $('#miniChart').innerHTML = `<svg viewBox="0 0 300 48" preserveAspectRatio="none" aria-hidden="true"><path class="chart-fill" d="${mini} L300,48 L0,48 Z"/><path class="chart-line" d="${mini}"/></svg><span>${first}</span><span>${last}</span>`;
  const middle = shortDate(history[Math.floor((history.length - 1) / 2)].day);
  const grid = [30, 75, 120].map(y => `<line class="gridline" x1="0" x2="600" y1="${y}" y2="${y}"/>`).join('');
  $('#largeChart').innerHTML = `<svg viewBox="0 0 600 150" preserveAspectRatio="none" role="img" aria-label="Net worth from ${first} to ${last}">${grid}<path class="net-line" d="${pathFrom(chartPoints(history, 600, 150, 14))}"/></svg><div class="chart-labels"><span>${first}</span><span>${history.length > 2 ? middle : ''}</span><span>${last}</span></div>`;
}

function worthTrend() {
  const history = state.net_worth_history;
  if (history.length < 2) return '<small>Tracking starts with your first sync</small>';
  const latest = history[history.length - 1];
  const cutoff = new Date(parseDay(latest.day).getTime() - 30 * 86400000);
  const baseline = [...history].reverse().find(point => parseDay(point.day) <= cutoff) || history[0];
  const change = latest.total - baseline.total;
  return `<span class="${change >= 0 ? 'good' : ''}">${change >= 0 ? '+' : '−'}${money(Math.abs(change))}</span><small>since ${shortDate(baseline.day)}</small>`;
}

function attentionItems(summary, items) {
  const list = [];
  for (const connection of realConnections()) {
    if (connection.status === 'needs_reauth') list.push(['coral', '!', `${connection.institution} needs you to sign in again`, 'Updates are paused until you reconnect.', 'accounts']);
    else if (connection.status === 'error') list.push(['amber', '!', `${connection.institution} could not sync`, connection.error || 'Try syncing again later.', 'accounts']);
  }
  const uncategorized = state.transactions.filter(t => t.category === 'Uncategorized' && !t.hidden && t.date.startsWith(month)).length;
  if (uncategorized) list.push(['blue', '?', `${uncategorized} uncategorized transaction${uncategorized === 1 ? '' : 's'}`, 'Give them a category so your budget is accurate.', 'transactions']);
  for (const item of items.filter(entry => entry.status === 'over').slice(0, 3)) {
    list.push(['coral', '↑', `${item.name} is ${money(item.spent - item.budget)} over`, `${money(item.spent)} spent of ${money(item.budget)}.`, 'budget']);
  }
  if (!state.accounts.length) list.push(['blue', '+', 'Connect your first institution', 'Or load sample data from Settings to look around.', 'accounts']);
  return list;
}

function renderOverview() {
  const summary = monthSummary(month);
  const items = categoryItems(summary);
  const previous = monthSummary(shiftMonth(month, -1));
  $('#monthSpend').textContent = money(summary.spent);
  $('#lastMonthSpend').innerHTML = `${money(previous.spent)}<small>last month</small>`;
  $('#spendMeter').style.width = `${summary.budget ? Math.min(Math.max(summary.spent, 0) / summary.budget * 100, 100) : 0}%`;
  $('#spentCaption').textContent = `of ${money(summary.budget)} budgeted`;
  const remaining = summary.budget - summary.spent;
  $('#remainingTotal').textContent = remaining >= 0 ? `${money(remaining)} left` : `${money(-remaining)} over`;

  const pulse = items.filter(item => item.budget > 0).sort((a, b) => b.ratio - a.ratio).slice(0, 4);
  $('#overviewBudget').innerHTML = pulse.map(item => `<article class="pulse-item clickable" role="button" tabindex="0" data-pulse="${esc(item.id)}" aria-label="Open ${esc(item.name)} in Budget"><div class="pulse-top"><i class="category-bullet" style="background:${esc(item.color)}"></i><span class="${item.status}">${STATUS_LABELS[item.status]}</span></div><strong>${esc(item.name)}</strong><small>${leftLabel(item)}</small><div class="mini-meter"><span style="width:${Math.min(item.ratio * 100, 100)}%;background:${statusColor(item)}"></span></div></article>`).join('')
    || '<p class="empty-note">Add a category with a monthly limit to see it here.</p>';

  $('#recentTransactions').innerHTML = state.transactions.slice(0, 6).map(transactionRow).join('')
    || '<p class="empty-note">No transactions yet. Connect an institution or add one manually.</p>';

  const attention = attentionItems(summary, items);
  $('#attentionList').innerHTML = attention.map(([tone, icon, title, text, view]) => `<div class="attention-item"><div class="attention-icon ${tone}">${icon}</div><div><strong>${esc(title)}</strong><p>${esc(text)}</p></div><button type="button" data-go="${view}" aria-label="Open ${view}">→</button></div>`).join('')
    || '<p class="empty-note">Nothing needs your attention.</p>';

  $('#netWorthHero').textContent = money(netWorth().total);
  $('#netWorthTrend').innerHTML = worthTrend();
  renderCharts();
}

let expandedCategory = null;
const monthLabel = key => parseDay(`${key}-01`).toLocaleDateString('en-US', {month: 'long', year: 'numeric'});

function categoryPreview(item) {
  const all = state.transactions.filter(t => t.category === item.name && t.date.startsWith(month));
  const latest = all.slice(0, 5);
  return `<div class="category-preview"><p class="preview-title">Latest in ${esc(item.name)} · ${monthLabel(month)}</p>${latest.map(transactionRow).join('') || '<p class="empty-note">No transactions in this category this month.</p>'}<button class="text-button" type="button" data-view-category="${esc(item.name)}">${all.length > latest.length ? `View all ${all.length}` : 'Open in Activity'} <span>→</span></button></div>`;
}

function renderBudgets() {
  const summary = monthSummary(month);
  const items = categoryItems(summary);
  const groups = [...new Set(items.map(item => item.group))];
  let markup = groups.map(group => {
    const members = items.filter(item => item.group === group);
    const spent = members.reduce((total, item) => total + item.spent, 0);
    const budget = members.reduce((total, item) => total + item.budget, 0);
    return `<section class="budget-group"><div class="group-title"><h2>${esc(group)}</h2><span>${money(spent)} of ${money(budget)}</span></div>${members.map(item => `<div class="budget-row clickable" role="button" tabindex="0" aria-expanded="${expandedCategory === item.id}" data-preview="${esc(item.id)}"><div class="budget-name"><i class="category-bullet" style="background:${esc(item.color)}"></i>${esc(item.name)}<small>${item.spent ? '' : 'No activity'}</small></div><div class="budget-progress"><div class="progress-label"><span>${item.budget ? `${Math.round(Math.min(item.ratio, 1) * 100)}% used` : 'No limit'}</span><span>${leftLabel(item)}</span></div><div class="progress-track"><span style="width:${Math.min(item.ratio * 100, 100)}%;background:${statusColor(item)}"></span></div></div><div class="budget-numbers ${item.status === 'over' || item.status === 'hit' ? 'over' : ''}"><strong>${money(item.spent)}</strong><span> of ${money(item.budget)}</span></div><button class="edit-budget" type="button" data-edit="${esc(item.id)}" aria-label="Edit ${esc(item.name)} budget">⋯</button></div>${expandedCategory === item.id ? categoryPreview(item) : ''}`).join('')}</section>`;
  }).join('');
  if (summary.uncategorized > 0) {
    markup += `<section class="budget-group"><div class="group-title"><h2>Uncategorized</h2><span>${money(summary.uncategorized)} counted in “Spent so far”</span></div><div class="budget-row loose-row"><div class="budget-name"><i class="category-bullet" style="background:#b6beb8"></i>Purchases without a category</div><button class="text-button" type="button" data-go="transactions">Review <span>→</span></button></div></section>`;
  }
  $('#budgetGroups').innerHTML = markup || '<p class="empty-note">No categories yet. Add one to start your plan.</p>';
  const remaining = summary.budget - summary.spent;
  $('#budgetTotal').textContent = money(summary.budget);
  $('#budgetSpent').textContent = money(summary.spent);
  $('#budgetRemainingLabel').textContent = remaining >= 0 ? 'Remaining' : 'Over budget';
  $('#budgetRemaining').textContent = money(Math.abs(remaining));
  $('#budgetRemaining').className = remaining >= 0 ? 'green' : '';
  $('#budgetPending').textContent = money(summary.pending);
  $('#categoryGroups').innerHTML = [...new Set(state.categories.map(category => category.group))].map(group => `<option value="${esc(group)}"></option>`).join('');
}

const EYE = '<svg viewBox="0 0 24 24" width="19" height="19" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/></svg>';
const EYE_OFF = '<svg viewBox="0 0 24 24" width="19" height="19" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M17.9 17.9A10.1 10.1 0 0 1 12 19C5.6 19 2 12 2 12a18 18 0 0 1 4.1-5.1M9.9 5.2A9.7 9.7 0 0 1 12 5c6.4 0 10 7 10 7a18 18 0 0 1-2.2 3.2M14.1 14.1a3 3 0 1 1-4.2-4.2"/><path d="M2 2l20 20"/></svg>';

function renderTransactions() {
  const names = categoryNames();
  const filter = $('#categoryFilter');
  const chosen = filter.value;
  filter.innerHTML = '<option value="all">All categories</option>' + names.map(name => `<option value="${esc(name)}">${esc(name)}</option>`).join('');
  filter.value = names.includes(chosen) ? chosen : 'all';
  const query = $('#transactionSearch').value.trim().toLowerCase();
  const stateFilter = $('#stateFilter').value;
  const matches = state.transactions.filter(t =>
    (!query || t.merchant.toLowerCase().includes(query) || t.category.toLowerCase().includes(query) || t.account.toLowerCase().includes(query) || (t.notes || '').toLowerCase().includes(query))
    && (filter.value === 'all' || t.category === filter.value)
    && (stateFilter === 'all' || (stateFilter === 'pending') === t.pending));
  const options = selected => (names.includes(selected) ? names : [selected, ...names])
    .map(name => `<option${name === selected ? ' selected' : ''}>${esc(name)}</option>`).join('');
  $('#transactionTable').innerHTML = matches.slice(0, ROW_LIMIT).map(t => `<tr class="${t.hidden ? 'budget-hidden-row' : ''}"><td>${shortDate(t.date)}${t.pending ? '<span class="pending-badge">PENDING</span>' : ''}</td><td><div class="merchant-cell">${iconMarkup(t.merchant)}${esc(t.merchant)}<button class="row-delete" type="button" data-edit-transaction="${esc(t.id)}" aria-label="Edit ${esc(t.merchant)}">✎</button>${t.notes ? `<span class="note-snippet table-note-snippet" title="${esc(t.notes)}">${esc(t.notes)}</span>` : ''}${t.manual ? `<span class="manual-badge">MANUAL</span><button class="row-delete" type="button" data-delete-transaction="${esc(t.id)}" aria-label="Delete ${esc(t.merchant)}">×</button>` : ''}</div></td><td><select class="category-select" data-transaction="${esc(t.id)}" aria-label="Category for ${esc(t.merchant)}">${options(t.category)}</select></td><td>${esc(t.account)}</td><td class="amount ${t.amount < 0 ? 'income' : ''}">${signedAmount(t.amount)}</td><td class="eye-col"><button class="eye-toggle ${t.hidden ? 'is-hidden' : ''}" type="button" data-hide="${esc(t.id)}" aria-pressed="${t.hidden}" title="${t.hidden ? 'Hidden from your budget. Click to include.' : 'Counts toward your budget. Click to hide.'}" aria-label="${t.hidden ? 'Hidden from budget; include' : 'Included in budget; hide'} ${esc(t.merchant)}">${t.hidden ? EYE_OFF : EYE}</button></td></tr>`).join('')
    || `<tr><td colspan="6" class="empty-cell">${state.transactions.length ? 'No transactions match those filters.' : 'No transactions yet.'}</td></tr>`;
  $('#transactionCount').textContent = matches.length > ROW_LIMIT
    ? `Showing the latest ${ROW_LIMIT} of ${matches.length} matching transactions. Search or filter to narrow them down; the CSV export has everything.`
    : `${matches.length} transaction${matches.length === 1 ? '' : 's'}`;
}

const KIND_LABELS = {credit: 'Credit card', depository: 'Bank account', investment: 'Investment', loan: 'Loan', other: 'Other'};
const KIND_SECTIONS = [['credit', 'Credit cards'], ['depository', 'Bank accounts'], ['investment', 'Investments'], ['other', 'Investments & other']];
const kindSection = kind => ({credit: 'credit', depository: 'depository', investment: 'investment'})[kind] || 'other';
const typeSelect = account => `<label class="account-type">Type <select data-account-kind="${esc(account.id)}">${Object.entries(KIND_LABELS).map(([value, label]) => `<option value="${value}"${value === account.kind ? ' selected' : ''}>${label}</option>`).join('')}</select></label>`;

function manualNote(account) {
  return `<div class="account-extra"><span>Entered by hand · updated ${ago(account.balance_at)}</span>${typeSelect(account)}<button class="quiet-button" type="button" data-edit-manual="${esc(account.id)}">Edit</button><button class="delete-category" type="button" data-delete-manual="${esc(account.id)}">Delete</button></div>`;
}

function ledgerNote(account) {
  const basis = account.anchor_date
    ? `Balance as of ${shortDate(account.anchor_date)}: ${account.anchor_cents < 0 ? '−' : ''}${precise(Math.abs(account.anchor_cents))}, plus activity since then.`
    : 'Showing an estimate from imported transactions only. Enter your real balance to make it accurate.';
  return `<div class="account-extra"><span>${basis}</span>${typeSelect(account)}<button class="quiet-button" type="button" data-set-balance="${esc(account.id)}">Set current balance</button></div>`;
}

function institutionCard(connection, accounts) {
  const demo = connection.provider === 'demo';
  const csv = connection.provider === 'csv';
  const manual = connection.provider === 'manual';
  const [tone, label] = demo ? ['', 'Sample data'] : csv ? ['', 'CSV import'] : manual ? ['', 'Manual'] : (STATUS[connection.status] || STATUS.error);
  const source = demo ? 'synthetic' : csv ? `imported from CSV · ${ago(connection.last_success_at)}` : manual ? 'entered by hand' : `via ${connection.provider === 'plaid' ? 'Plaid' : 'MX'} · synced ${ago(connection.last_success_at)}`;
  const id = esc(connection.id);
  const rows = accounts.map(account => `<div class="account-row"><div class="account-name">${esc(account.name)}<small>${esc(account.subtype || KIND_LABELS[account.kind] || account.kind)}${account.mask ? ` ••${esc(account.mask)}` : ''}</small></div><div class="account-balance">${account.balance_cents < 0 ? '−' : ''}${precise(Math.abs(account.balance_cents))}<small>${account.balance_cents < 0 ? 'Owed' : 'Balance'} · ${ago(account.balance_at)}</small></div><input class="toggle" type="checkbox" ${account.included ? 'checked' : ''} data-account="${esc(account.id)}" aria-label="Include ${esc(account.name)} in net worth" /></div>${csv ? ledgerNote(account) : manual ? manualNote(account) : `<div class="account-extra">${typeSelect(account)}</div>`}`).join('')
    || '<p class="empty-note padded">No accounts imported yet. Try Sync in a minute.</p>';
  const actions = demo || manual ? '' : csv ? `<div class="connection-actions"><button class="delete-category" type="button" data-disconnect="${id}">Remove</button></div>` : `<div class="connection-actions">${connection.status !== 'healthy' ? `<button class="primary-button" type="button" data-reconnect="${id}">Reconnect</button>` : ''}<button class="quiet-button" type="button" data-sync="${id}">↻ Sync</button><button class="delete-category" type="button" data-disconnect="${id}">Disconnect</button></div>`;
  return `<section class="institution-card"><div class="institution-head"><div class="institution-logo">${esc(initials(connection.institution))}</div><div><h2>${esc(connection.institution)}</h2><p>${accounts.length} account${accounts.length === 1 ? '' : 's'} · ${source}</p></div><span class="sync-status ${tone}">${label}</span></div>${connection.error && !demo ? `<p class="connection-error">${esc(connection.error)}</p>` : ''}${rows}${actions}</section>`;
}

function renderSyncButtons() {
  const none = !syncableConnections().length;
  $$('[data-action="sync-all"]').forEach(button => {
    button.setAttribute('aria-disabled', String(none));
    button.title = none ? 'No externally linked accounts to sync' : '';
  });
}

function renderAccounts() {
  renderSyncButtons();
  const real = realConnections();
  $('#accountsSubhead').textContent = real.length
    ? `${real.length} institution${real.length === 1 ? '' : 's'} connected · ${state.accounts.length} account${state.accounts.length === 1 ? '' : 's'}`
    : 'No institutions connected yet.';
  const sections = KIND_SECTIONS.map(([section, title]) => {
    const inSection = state.accounts.filter(account => kindSection(account.kind) === section);
    if (!inSection.length) return '';
    const total = inSection.filter(account => account.included).reduce((sum, account) => sum + account.balance_cents, 0);
    const cards = state.connections.map(connection => {
      const accounts = inSection.filter(account => account.connection_id === connection.id);
      return accounts.length ? institutionCard(connection, accounts) : '';
    }).join('');
    return `<div class="account-section"><h2 class="section-title">${title}<small>${inSection.length} · ${total < 0 ? '−' : ''}${money(Math.abs(total))}</small></h2>${cards}</div>`;
  }).join('');
  const waiting = state.connections.filter(connection => !state.accounts.some(account => account.connection_id === connection.id))
    .map(connection => institutionCard(connection, [])).join('');
  $('#accountsList').innerHTML = sections + (waiting ? `<div class="account-section"><h2 class="section-title">Waiting for accounts</h2>${waiting}</div>` : '')
    || '<section class="institution-card"><p class="empty-note padded">Nothing connected yet. Use <strong>Connect institution</strong> above, import a CSV, or load sample data from Settings to explore first.</p></section>';
}

function renderWorth() {
  const worth = netWorth();
  const latest = state.accounts.map(account => account.balance_at).filter(Boolean).sort().pop();
  $('#networthSubhead').textContent = latest ? `Balances as of ${ago(latest)}. Excluded accounts are left out.` : 'Connect an institution to start tracking.';
  $('#netWorthTotal').textContent = money(worth.total);
  $('#netWorthTrendLarge').innerHTML = worthTrend();
  $('#assetTotal').textContent = money(worth.assetTotal);
  $('#debtTotal').textContent = `−${money(Math.abs(worth.debtTotal))}`;
  const institutions = new Map(state.connections.map(connection => [connection.id, connection.institution]));
  const rows = (accounts, debt) => accounts.map(account => `<div class="worth-row"><div class="worth-name"><i style="background:${debt ? '#cc725e' : '#54a675'}"></i><div>${esc(account.name)}<small>${esc(institutions.get(account.connection_id) || '')} · ${esc(account.subtype || account.kind)}</small></div></div><strong>${debt ? '−' : ''}${money(Math.abs(account.balance_cents))}</strong></div>`).join('')
    || '<p class="empty-note">None included.</p>';
  const manualIds = new Set(state.connections.filter(connection => connection.provider === 'manual').map(connection => connection.id));
  const manualAccounts = state.accounts.filter(account => manualIds.has(account.connection_id));
  $('#manualAccountsList').innerHTML = manualAccounts.map(account => `<div class="manual-row"><div class="worth-name"><i style="background:${account.balance_cents < 0 ? '#cc725e' : '#54a675'}"></i><div>${esc(account.name)}<small>${esc(account.subtype || (account.balance_cents < 0 ? 'Liability' : 'Asset'))} · updated ${ago(account.balance_at)}${account.included ? '' : ' · excluded'}</small></div></div><div class="manual-actions"><strong>${account.balance_cents < 0 ? '−' : ''}${precise(Math.abs(account.balance_cents))}</strong><button type="button" data-edit-manual="${esc(account.id)}" aria-label="Edit ${esc(account.name)}">✎</button><button type="button" data-delete-manual="${esc(account.id)}" aria-label="Delete ${esc(account.name)}">×</button></div></div>`).join('')
    || '<p class="empty-note">No manual accounts yet.</p>';
  $('#assetsList').innerHTML = rows(worth.assets, false);
  $('#debtsList').innerHTML = rows(worth.debts, true);
}

function matchKeyword(text) {
  const lowered = text.toLowerCase();
  let best = null;
  for (const rule of state.keyword_rules) {
    if (lowered.includes(rule.keyword) && (!best || rule.keyword.length > best.keyword.length)) best = rule;
  }
  return best;
}

function renderKeywords() {
  const names = categoryNames();
  const select = $('#keywordCategory');
  const chosen = select.value;
  select.innerHTML = names.map(name => `<option>${esc(name)}</option>`).join('');
  if (names.includes(chosen)) select.value = chosen;
  const query = $('#keywordFilter').value.trim().toLowerCase();
  const counts = new Map();
  for (const t of state.transactions) {
    const rule = matchKeyword(t.merchant);
    if (rule) counts.set(rule.id, (counts.get(rule.id) || 0) + 1);
  }
  const groups = new Map();
  for (const rule of state.keyword_rules) {
    if (query && !rule.keyword.includes(query) && !rule.category.toLowerCase().includes(query)) continue;
    if (!groups.has(rule.category)) groups.set(rule.category, []);
    groups.get(rule.category).push(rule);
  }
  $('#keywordRules').innerHTML = [...groups].map(([category, rules]) => `<div class="keyword-group"><h3>${esc(category)} <small>${rules.length} keyword${rules.length === 1 ? '' : 's'}${names.includes(category) ? '' : ' · category no longer exists'}</small></h3>${rules.map(rule => `<span class="keyword-chip">${esc(rule.keyword)}${counts.get(rule.id) ? ` <small>${counts.get(rule.id)}</small>` : ''}<button type="button" data-delete-keyword="${esc(rule.id)}" aria-label="Remove ${esc(rule.keyword)}">×</button></span>`).join('')}</div>`).join('')
    || `<p class="empty-note">${state.keyword_rules.length ? 'No keywords match that filter.' : 'No keyword rules yet. Add one above.'}</p>`;
  renderKeywordTest();
}

function renderKeywordTest() {
  const text = $('#keywordTest').value.trim();
  const rule = text && matchKeyword(text);
  $('#keywordTestResult').textContent = !text ? 'Numbers next to a keyword are how many of your transactions it matches.'
    : rule ? `“${text}” → ${rule.category} (matched “${rule.keyword}”)` : `“${text}” matches no keyword, so it would stay Uncategorized (or use the bank’s category).`;
}

function renderSettings() {
  renderKeywords();
  $('#rulesList').innerHTML = state.rules.map(rule => `<div class="rule-row"><span><strong>${esc(rule.match)}</strong> → ${esc(rule.category)}</span><button class="delete-category" type="button" data-delete-rule="${esc(rule.id)}">Remove</button></div>`).join('')
    || '<p class="empty-note">No rules yet. Change a transaction’s category and choose “Always” to create one.</p>';
  $('#loadDemo').hidden = state.has_demo;
  $('#removeDemo').hidden = !state.has_demo;
}

function renderProviders() {
  const providers = session.providers;
  const plaidText = !providers.plaid ? 'Not configured on this server'
    : providers.plaid_env === 'sandbox' ? 'Sandbox mode — Plaid test institutions only' : 'Most US banks and card issuers';
  const mxText = !providers.mx ? 'Not configured on this server'
    : providers.mx_env === 'production' ? 'Banks and credit unions via MX' : 'Integration mode — MX test institutions only';
  $('#plaidHint').textContent = plaidText;
  $('#mxHint').textContent = mxText;
  $('#connectPlaid').disabled = !providers.plaid;
  $('#connectMx').disabled = !providers.mx;
  $('#providerStatus').innerHTML = `<div class="rule-row"><span><strong>Plaid</strong></span><span>${esc(plaidText)}</span></div><div class="rule-row"><span><strong>MX</strong></span><span>${esc(mxText)}</span></div>`;
}

// --- Navigation and generic controls ------------------------------------------

function go(view) {
  if (!document.getElementById(view)) return;
  $$('.view').forEach(el => el.classList.toggle('active', el.id === view));
  $$('.nav-item').forEach(el => el.classList.toggle('active', el.dataset.view === view));
  window.scrollTo({top: 0});
}

const ACTIONS = {
  'sync-all': () => syncAll(),
  'load-demo': () => act(() => api('POST', '/api/demo'), 'Sample data loaded'),
  'remove-demo': () => act(() => api('DELETE', '/api/demo'), 'Sample data removed'),
  'logout': async () => { await act(() => api('POST', '/api/logout')); showLogin(); },
};

document.addEventListener('keydown', event => {
  const target = event.target;
  if ((event.key === 'Enter' || event.key === ' ') && target instanceof Element && target.matches('[role="button"][tabindex]')) {
    event.preventDefault();
    target.click();
  }
});

document.addEventListener('click', event => {
  const target = event.target instanceof Element ? event.target : null;
  if (!target) return;
  const opener = target.closest('[data-open-transaction]');
  if (opener) { openTransactionEditor(opener.dataset.openTransaction); return; }
  const pulse = target.closest('[data-pulse]');
  if (pulse) {
    expandedCategory = pulse.dataset.pulse;
    go('budget');
    renderBudgets();
    document.querySelector(`[data-preview="${CSS.escape(expandedCategory)}"]`)?.scrollIntoView({block: 'center'});
    return;
  }
  const nav = target.closest('[data-view],[data-go]');
  if (nav) { go(nav.dataset.view || nav.dataset.go); return; }
  const brand = target.closest('a.brand');
  if (brand) { event.preventDefault(); go('overview'); return; }
  const close = target.closest('[data-close]');
  if (close) { close.closest('dialog').close(); return; }
  const action = target.closest('[data-action]');
  if (action && action.getAttribute('aria-disabled') !== 'true' && ACTIONS[action.dataset.action]) ACTIONS[action.dataset.action]();
});

async function syncAll() {
  if (!syncableConnections().length) { toast('No externally linked accounts to sync.'); return; }
  $('#syncChip').textContent = 'Syncing…';
  await act(() => api('POST', '/api/sync'), 'Sync finished');
  renderChrome();
}

$('#prevMonth').addEventListener('click', () => { month = shiftMonth(month, -1); renderAll(); });
$('#nextMonth').addEventListener('click', () => { if (month < monthKey(new Date())) { month = shiftMonth(month, 1); renderAll(); } });

// --- Budget and category editing ----------------------------------------------

$('#budgetGroups').addEventListener('click', event => {
  const id = event.target.dataset.edit;
  if (id) { openCategoryDialog(id); return; }
  const viewAll = event.target.closest('[data-view-category]');
  if (viewAll) { viewCategoryTransactions(viewAll.dataset.viewCategory); return; }
  const row = event.target.closest('[data-preview]');
  if (row && !event.target.closest('button')) {
    expandedCategory = expandedCategory === row.dataset.preview ? null : row.dataset.preview;
    renderBudgets();
    document.querySelector(`[data-preview="${CSS.escape(row.dataset.preview)}"]`)?.focus({preventScroll: true});
  }
});
function viewCategoryTransactions(name) {
  $('#transactionSearch').value = '';
  $('#stateFilter').value = 'all';
  $('#categoryFilter').value = name;
  renderTransactions();
  go('transactions');
}
// One dialog adds and edits categories: pick "+ New category" or an existing one from the list.

function categoryFormMode() {
  return $('#categoryPicker').value ? state.categories.find(item => item.id === $('#categoryPicker').value) : null;
}
function fillCategoryForm() {
  const category = categoryFormMode();
  const form = $('#categoryForm');
  $('#categoryError').hidden = true;
  form.elements.name.value = category ? category.name : '';
  form.elements.group.value = category ? category.group : (state.categories[0]?.group || '');
  form.elements.budget.value = category ? category.budget / 100 : 0;
  $('#categorySubmit').textContent = category ? 'Save changes' : 'Add category';
  $('#deleteCategory').hidden = !category;
  $('#categoryHint').textContent = category
    ? 'Rename it, move it to another group, or change its limit. Deleting keeps its transactions and marks them Uncategorized.'
    : 'Create a new budget category. To change one that already exists, choose it from the list above.';
}
function openCategoryDialog(selectedId = '') {
  const groups = [...new Set(state.categories.map(category => category.group))];
  $('#categoryPicker').innerHTML = '<option value="">＋ New category</option>' + groups.map(group =>
    `<optgroup label="${esc(group)}">${state.categories.filter(category => category.group === group)
      .map(category => `<option value="${esc(category.id)}">${esc(category.name)}</option>`).join('')}</optgroup>`).join('');
  $('#categoryPicker').value = state.categories.some(category => category.id === selectedId) ? selectedId : '';
  fillCategoryForm();
  $('#categoryDialog').showModal();
  (categoryFormMode() ? $('#categoryBudget') : $('#categoryName')).focus();
}
$('#manageCategories').addEventListener('click', () => openCategoryDialog());
$('#categoryPicker').addEventListener('change', fillCategoryForm);
$('#categoryForm').addEventListener('submit', async event => {
  event.preventDefault();
  const form = event.currentTarget;
  const category = categoryFormMode();
  const body = {name: form.elements.name.value.trim(), group: form.elements.group.value.trim(), budget: toCents(form.elements.budget.value)};
  const error = $('#categoryError');
  const clash = state.categories.find(item => item.name.toLowerCase() === body.name.toLowerCase() && (!category || item.id !== category.id));
  if (clash) {
    error.textContent = category ? `Another category is already called “${clash.name}”.` : `“${clash.name}” already exists. Choose it from the list above to edit it instead.`;
    error.hidden = false;
    return;
  }
  try {
    await (category ? api('PATCH', `/api/categories/${category.id}`, body) : api('POST', '/api/categories', body));
  } catch (failure) {
    error.textContent = failure.message || 'Something went wrong.';
    error.hidden = false;
    return;
  }
  $('#categoryDialog').close();
  toast(category ? `${body.name} saved` : `${body.name} category added`);
});
$('#deleteCategory').addEventListener('click', async () => {
  const category = categoryFormMode();
  if (!category || !window.confirm(`Delete “${category.name}”? Its transactions are kept and marked Uncategorized.`)) return;
  if (await act(() => api('DELETE', `/api/categories/${category.id}`), `${category.name} deleted`)) $('#categoryDialog').close();
});

// --- Transactions -------------------------------------------------------------

['input', 'change'].forEach(type => $('#transactionSearch').addEventListener(type, renderTransactions));
$('#categoryFilter').addEventListener('change', renderTransactions);
$('#stateFilter').addEventListener('change', renderTransactions);

$('#transactionTable').addEventListener('change', async event => {
  const id = event.target.dataset.transaction;
  const transaction = id && state.transactions.find(item => item.id === id);
  if (!transaction) return;
  const category = event.target.value;
  const merchant = transaction.merchant;
  if (!await act(() => api('PATCH', `/api/transactions/${id}`, {category}))) { renderTransactions(); return; }
  const offerRule = !transaction.manual && category !== 'Uncategorized';
  toast(`${merchant} categorized as ${category}`, offerRule ? {
    label: 'Always',
    run: () => act(() => api('POST', '/api/rules', {merchant, category}), `Future ${merchant} transactions will be ${category}`),
  } : undefined);
});
function openTransactionEditor(id) {
  const t = state.transactions.find(item => item.id === id);
  if (!t) return;
  const form = $('#editTransactionForm');
  form.dataset.id = id;
  form.dataset.editable = t.editable ? '1' : '';
  const names = categoryNames();
  form.elements.category.innerHTML = (names.includes(t.category) ? names : [t.category, ...names])
    .map(name => `<option${name === t.category ? ' selected' : ''}>${esc(name)}</option>`).join('');
  form.elements.merchant.value = t.merchant;
  form.elements.date.value = t.date;
  form.elements.amount.value = (Math.abs(t.amount) / 100).toFixed(2);
  form.elements.kind.value = t.amount < 0 ? 'income' : 'expense';
  form.elements.hidden.checked = t.hidden;
  form.elements.notes.value = t.notes || '';
  for (const field of ['merchant', 'date', 'amount', 'kind']) form.elements[field].disabled = !t.editable;
  $('#editTransactionHint').hidden = t.editable;
  $('#editTransactionDialog').showModal();
}

$('#transactionTable').addEventListener('click', event => {
  const hideId = event.target.closest('[data-hide]')?.dataset.hide;
  const deleteId = event.target.dataset.deleteTransaction;
  const editId = event.target.dataset.editTransaction;
  if (editId) {
    openTransactionEditor(editId);
  } else if (hideId) {
    const transaction = state.transactions.find(item => item.id === hideId);
    if (!transaction) return;
    const hidden = !transaction.hidden;
    act(() => api('PATCH', `/api/transactions/${hideId}`, {hidden}), `${transaction.merchant} is ${hidden ? 'hidden from' : 'included in'} your budget`);
  } else if (deleteId && window.confirm('Delete this manual transaction?')) {
    act(() => api('DELETE', `/api/transactions/${deleteId}`), 'Manual transaction deleted');
  }
});
$('#editTransactionForm').elements.category.addEventListener('change', event => {
  // Transfers stay out of the budget by default; picking another category brings it back.
  const form = $('#editTransactionForm');
  const t = state.transactions.find(item => item.id === form.dataset.id);
  if (event.target.value === 'Transfer') form.elements.hidden.checked = true;
  else if (t && t.category === 'Transfer') form.elements.hidden.checked = false;
});
$('#manualCategory').addEventListener('change', event => { $('#manualBudgetHidden').checked = event.target.value === 'Transfer'; });
$('#editTransactionForm').addEventListener('submit', async event => {
  event.preventDefault();
  const form = event.currentTarget;
  const body = {category: form.elements.category.value, hidden: form.elements.hidden.checked, notes: form.elements.notes.value.trim()};
  if (form.dataset.editable) {  // bank-synced rows are rewritten by every sync, so only manual and CSV rows change these
    Object.assign(body, {merchant: form.elements.merchant.value.trim(), date: form.elements.date.value,
      amount: toCents(form.elements.amount.value), kind: form.elements.kind.value});
  }
  if (await act(() => api('PATCH', `/api/transactions/${form.dataset.id}`, body), 'Transaction updated')) $('#editTransactionDialog').close();
});
$('#addTransaction').addEventListener('click', () => {
  $('#manualTransactionForm').reset();
  $('#manualDate').value = todayISO();
  $('#manualCategory').innerHTML = categoryNames().map(name => `<option>${esc(name)}</option>`).join('');
  $('#manualTransactionDialog').showModal();
});
$('#manualTransactionForm').addEventListener('submit', async event => {
  event.preventDefault();
  const form = new FormData(event.currentTarget);
  const body = {
    merchant: String(form.get('merchant')).trim(), date: form.get('date'), amount: toCents(form.get('amount')),
    kind: form.get('kind'), category: form.get('category'), account: String(form.get('account')).trim(),
    hidden: form.get('hidden') === 'on', notes: String(form.get('notes') || '').trim(),
  };
  if (await act(() => api('POST', '/api/transactions', body), 'Manual transaction added')) $('#manualTransactionDialog').close();
});

// --- Accounts and connections -------------------------------------------------

$('#importCsvButton').addEventListener('click', () => {
  $('#importCsvForm').reset();
  $('#importCsvDetected').textContent = '';
  $('#importCsvDialog').showModal();
});
function sniffCsv(header) {
  const columns = header.split(',').map(column => column.trim().toLowerCase().replace(/^"|"$/g, ''));
  const has = (...names) => names.every(name => columns.includes(name));
  if (has('card no.', 'debit', 'credit')) return {kind: 'credit', label: 'Capital One credit card', name: 'Capital One Card'};
  if (has('transaction description', 'transaction type', 'transaction amount')) return {kind: 'depository', label: 'Capital One bank account (checking or savings)'};
  if (has('transaction date', 'description', 'amount')) return {kind: 'credit', label: 'Chase card activity'};
  return null;
}
$('#importCsvFile').addEventListener('change', async event => {
  const file = event.target.files[0];
  const form = $('#importCsvForm');
  $('#importCsvDetected').textContent = '';
  if (!file) return;
  const found = sniffCsv((await file.slice(0, 2000).text()).replace(/^\ufeff/, '').split(/\r?\n/)[0] || '');
  if (found) {
    form.elements.kind.value = found.kind;
    $('#importCsvDetected').textContent = `Detected: ${found.label}. Change the account type if that’s wrong.`;
  } else {
    $('#importCsvDetected').textContent = 'This file’s columns aren’t recognised yet.';
  }
  if ($('#importCsvAccount').value) return;
  const chase = file.name.match(/^([A-Za-z]+)(\d{4})_/);
  const capitalOne = file.name.match(/^\d{4}-\d{2}-\d{2}_(.+?)\.{2,}(\d{4})\.csv$/i);
  const guess = chase ? [chase[1], chase[2]] : capitalOne ? [capitalOne[1].replace(/([a-z])(?=[A-Z0-9])|(\d)(?=[A-Za-z])/g, '$1$2 '), capitalOne[2]] : null;
  if (guess) {
    $('#importCsvAccount').value = chase ? `${guess[0]} ${guess[1]}` : guess[0];
    form.elements.mask.value = guess[1];
  } else if (found && found.name) {
    $('#importCsvAccount').value = found.name;
  }
});
$('#importCsvForm').addEventListener('submit', async event => {
  event.preventDefault();
  const form = event.currentTarget;
  const file = form.elements.file.files[0];
  if (!file) return;
  if (file.size > 1_500_000) { toast('That file is too large to import.'); return; }
  const csv = await file.text();
  const data = await act(() => api('POST', '/api/import/csv', {csv, kind: form.elements.kind.value, account: form.elements.account.value.trim(), mask: form.elements.mask.value.trim()}));
  if (data) {
    $('#importCsvDialog').close();
    toast(`Imported ${data.import.added} transaction${data.import.added === 1 ? '' : 's'}${data.import.skipped ? ` (${data.import.skipped} already imported)` : ''}${data.import.recategorized ? `, ${data.import.recategorized} recategorized` : ''}`);
  }
});

$('#accountsList').addEventListener('change', event => {
  const kindId = event.target.dataset.accountKind;
  if (kindId) {
    act(() => api('PATCH', `/api/accounts/${kindId}`, {kind: event.target.value}), `Account type set to ${KIND_LABELS[event.target.value].toLowerCase()}`);
    return;
  }
  const id = event.target.dataset.account;
  if (!id) return;
  const included = event.target.checked;
  act(() => api('PATCH', `/api/accounts/${id}`, {included}), `Account ${included ? 'included in' : 'excluded from'} net worth`);
});
$('#accountsList').addEventListener('click', event => {
  const balanceId = event.target.dataset.setBalance;
  if (balanceId) {
    const account = state.accounts.find(item => item.id === balanceId);
    if (!account) return;
    const form = $('#setBalanceForm');
    form.dataset.id = balanceId;
    $('#setBalanceTitle').textContent = `Set current balance — ${account.name}`;
    form.elements.owed.value = (account.balance_cents < 0 || (account.kind === 'credit' && !account.balance_cents)) ? '1' : '';
    form.elements.amount.value = (Math.abs(account.balance_cents) / 100).toFixed(2);
    form.elements.as_of.value = todayISO();
    $('#setBalanceDialog').showModal();
    return;
  }
  const {sync, reconnect, disconnect} = event.target.dataset;
  const connection = state.connections.find(item => item.id === (sync || reconnect || disconnect));
  if (!connection) return;
  if (sync) {
    toast(`Syncing ${connection.institution}…`);
    act(() => api('POST', `/api/connections/${connection.id}/sync`), `${connection.institution} synced`);
  } else if (reconnect) {
    if (connection.provider === 'plaid') startPlaid(connection.id);
    else startMx(connection.id);
  } else if (disconnect && window.confirm(`Disconnect ${connection.institution}? Access is revoked at the provider and its accounts and transactions are removed from budge it.`)) {
    act(() => api('DELETE', `/api/connections/${connection.id}`), `${connection.institution} disconnected`);
  }
});
$('#connectButton').addEventListener('click', () => {
  $('#connectionStatus').textContent = '';
  $('#connectDialog').showModal();
});
$('#connectPlaid').addEventListener('click', () => startPlaid(null));
$('#connectMx').addEventListener('click', () => startMx(null));

// Plaid Link. The link token is remembered briefly so the flow can resume after a
// bank's OAuth page redirects back here (always the case on phones).
async function startPlaid(connectionId) {
  if (!window.Plaid) {
    toast('Plaid Link could not load. Check your connection or content blocker.');
    return;
  }
  $('#connectionStatus').textContent = 'Starting Plaid…';
  const data = await act(() => api('POST', '/api/plaid/link-token', connectionId ? {connection_id: connectionId} : {}));
  $('#connectionStatus').textContent = '';
  if (!data) return;
  const token = data.link.link_token;
  try { localStorage.setItem(LINK_KEY, JSON.stringify({token, connectionId, at: Date.now()})); } catch { /* storage unavailable */ }
  if ($('#connectDialog').open) $('#connectDialog').close();  // a modal dialog would cover Link
  openPlaid(token, connectionId);
}
function openPlaid(token, connectionId, receivedRedirectUri) {
  const config = {
    token,
    onSuccess: publicToken => {
      localStorage.removeItem(LINK_KEY);
      go('accounts');
      toast('Importing accounts and transactions…');
      if (connectionId) act(() => api('POST', `/api/connections/${connectionId}/sync`), 'Connection repaired');
      else act(() => api('POST', '/api/plaid/exchange', {public_token: publicToken}), 'Institution connected. Full history can take a few minutes — sync again shortly.');
    },
    onExit: error => { if (error) toast(error.display_message || 'Plaid Link closed before finishing.'); },
  };
  if (receivedRedirectUri) config.receivedRedirectUri = receivedRedirectUri;
  window.Plaid.create(config).open();
}
function resumePlaidOAuth() {
  if (!new URLSearchParams(location.search).has('oauth_state_id')) return;
  const returnedTo = location.href;
  history.replaceState(null, '', location.pathname);
  let saved = null;
  try { saved = JSON.parse(localStorage.getItem(LINK_KEY)); } catch { /* ignore */ }
  if (!saved || !saved.token || Date.now() - saved.at > 30 * 60000 || !window.Plaid) {
    toast('That bank sign-in could not be resumed. Please start the connection again.');
    return;
  }
  openPlaid(saved.token, saved.connectionId, returnedTo);
}

// MX Connect Widget, embedded in an iframe served by MX.
async function startMx(connectionId) {
  $('#connectionStatus').textContent = 'Starting MX…';
  const data = await act(() => api('POST', '/api/mx/widget-url', connectionId ? {connection_id: connectionId} : {}));
  $('#connectionStatus').textContent = '';
  if (!data) return;
  if ($('#connectDialog').open) $('#connectDialog').close();
  mxReconnect = Boolean(connectionId);
  $('#mxFrame').src = data.widget.url;
  $('#mxDialog').showModal();
}
function closeMx() {
  if ($('#mxDialog').open) $('#mxDialog').close();
  $('#mxFrame').src = 'about:blank';
}
async function importMx(announceEmpty) {
  const data = await act(() => api('POST', '/api/mx/discover'));
  if (!data) return;
  if (data.members) toast(mxReconnect ? 'Connection updated' : `${data.members} MX institution${data.members === 1 ? '' : 's'} imported`);
  else if (announceEmpty) toast('MX has no finished connections yet.');
}
$('#mxClose').addEventListener('click', closeMx);
$('#mxDialog').addEventListener('cancel', () => { $('#mxFrame').src = 'about:blank'; });
$('#mxDone').addEventListener('click', () => { closeMx(); go('accounts'); importMx(true); });
window.addEventListener('message', event => {
  let host = '';
  try { host = new URL(event.origin).hostname; } catch { return; }
  if (!/(^|\.)(mx\.com|moneydesktop\.com)$/.test(host)) return;
  let data = event.data;
  if (typeof data === 'string') { try { data = JSON.parse(data); } catch { return; } }
  if (data && data.type === 'mx/connect/memberConnected') importMx(false);
});

// --- Settings -----------------------------------------------------------------

$('#rulesList').addEventListener('click', event => {
  const id = event.target.dataset.deleteRule;
  if (id) act(() => api('DELETE', `/api/rules/${id}`), 'Rule removed');
});
$('#deleteData').addEventListener('click', () => { $('#deleteForm').reset(); $('#deleteDialog').showModal(); });
$('#deleteForm').addEventListener('submit', async event => {
  event.preventDefault();
  const password = new FormData(event.currentTarget).get('password');
  if (await act(() => api('POST', '/api/data/delete', {password}), 'All your data was deleted')) {
    $('#deleteDialog').close();
    go('overview');
  }
});

// --- Start --------------------------------------------------------------------

document.addEventListener('visibilitychange', () => {
  // Pick up webhook- and schedule-driven syncs when the tab comes back into view.
  if (!document.hidden && session && Date.now() - stateLoadedAt > 60000) act(() => api('GET', '/api/state'));
});

(async () => {
  try {
    await applySession(await api('GET', '/api/session'));
  } catch (error) {
    showLogin();
    $('#loginError').textContent = error.message;
    $('#loginError').hidden = false;
  }
})();

// --- Keyword rules ------------------------------------------------------------

const appliedNote = data => data.applied ? ` · ${data.applied} existing transaction${data.applied === 1 ? '' : 's'} recategorized` : '';
$('#keywordForm').addEventListener('submit', async event => {
  event.preventDefault();
  const form = event.currentTarget;
  const keyword = form.elements.keyword.value.trim();
  const data = await act(() => api('POST', '/api/keyword-rules', {keyword, category: form.elements.category.value}));
  if (data) { form.elements.keyword.value = ''; toast(`“${keyword.toLowerCase()}” → ${form.elements.category.value}${appliedNote(data)}`); }
});
$('#keywordRules').addEventListener('click', event => {
  const id = event.target.dataset.deleteKeyword;
  if (id) act(() => api('DELETE', `/api/keyword-rules/${id}`), 'Keyword removed. Already-categorized transactions keep their category.');
});
$('#keywordFilter').addEventListener('input', renderKeywords);
$('#keywordTest').addEventListener('input', renderKeywordTest);
$('#applyKeywords').addEventListener('click', async () => {
  const data = await act(() => api('POST', '/api/keyword-rules/apply'));
  if (data) toast(data.applied ? `${data.applied} transaction${data.applied === 1 ? '' : 's'} recategorized` : 'Everything already matches your rules');
});
$('#resetKeywords').addEventListener('click', () => {
  if (window.confirm('Restore the built-in keyword list? Keywords you added or moved will be removed.')) act(() => api('POST', '/api/keyword-rules/reset'), 'Keyword rules reset to defaults');
});

// --- Manual net-worth accounts -------------------------------------------------

$('#addManualAccount').addEventListener('click', () => {
  const form = $('#manualAccountForm');
  form.reset();
  delete form.dataset.id;
  $('#manualAccountTitle').textContent = 'Add an account';
  $('#manualAccountDialog').showModal();
});
function manualAccountAction(event) {
  const editId = event.target.dataset.editManual;
  const deleteId = event.target.dataset.deleteManual;
  const account = state.accounts.find(item => item.id === (editId || deleteId));
  if (!account) return;
  if (editId) {
    const form = $('#manualAccountForm');
    form.dataset.id = editId;
    form.elements.name.value = account.name;
    form.elements.subtype.value = account.subtype || '';
    form.elements.kind.value = account.kind;
    form.elements.liability.value = account.balance_cents < 0 ? '1' : '';
    form.elements.amount.value = (Math.abs(account.balance_cents) / 100).toFixed(2);
    $('#manualAccountTitle').textContent = 'Edit account';
    $('#manualAccountDialog').showModal();
  } else if (window.confirm(`Delete ${account.name} and its history?`)) {
    act(() => api('DELETE', `/api/manual-accounts/${deleteId}`), `${account.name} deleted`);
  }
}
$('#manualAccountsList').addEventListener('click', manualAccountAction);
$('#addManualFromAccounts').addEventListener('click', () => $('#addManualAccount').click());
$('#accountsList').addEventListener('click', manualAccountAction);
$('#manualAccountForm').addEventListener('submit', async event => {
  event.preventDefault();
  const form = event.currentTarget;
  const body = {name: form.elements.name.value.trim(), subtype: form.elements.subtype.value.trim(), kind: form.elements.kind.value,
    liability: form.elements.liability.value === '1', amount: toCents(form.elements.amount.value)};
  const id = form.dataset.id;
  const ok = await act(() => id ? api('PATCH', `/api/manual-accounts/${id}`, body) : api('POST', '/api/manual-accounts', body), id ? 'Account updated' : 'Account added');
  if (ok) $('#manualAccountDialog').close();
});

$('#setBalanceForm').addEventListener('submit', async event => {
  event.preventDefault();
  const form = event.currentTarget;
  const body = {amount: toCents(form.elements.amount.value), owed: form.elements.owed.value === '1', as_of: form.elements.as_of.value};
  if (await act(() => api('PATCH', `/api/accounts/${form.dataset.id}`, body), 'Balance set')) $('#setBalanceDialog').close();
});
