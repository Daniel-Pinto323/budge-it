const defaultBudgets = [
  { id: 'housing', group: 'Essentials', name: 'Housing', color: '#758d7b', spent: 1450, budget: 1450 },
  { id: 'groceries', group: 'Essentials', name: 'Groceries', color: '#3d9770', spent: 421, budget: 600 },
  { id: 'utilities', group: 'Essentials', name: 'Utilities', color: '#5f86ad', spent: 188, budget: 260 },
  { id: 'dining', group: 'Everyday', name: 'Dining', color: '#d3735f', spent: 294, budget: 250 },
  { id: 'transport', group: 'Everyday', name: 'Transportation', color: '#c59b48', spent: 116, budget: 180 },
  { id: 'shopping', group: 'Everyday', name: 'Shopping', color: '#8c77a3', spent: 112, budget: 260 },
  { id: 'subscriptions', group: 'Everyday', name: 'Subscriptions', color: '#608f99', spent: 54, budget: 80 },
  { id: 'travel', group: 'Goals & fun', name: 'Travel', color: '#4c94a5', spent: 49, budget: 300 },
  { id: 'fun', group: 'Goals & fun', name: 'Fun money', color: '#d29272', spent: 0, budget: 220 },
  { id: 'gifts', group: 'Goals & fun', name: 'Gifts', color: '#b17d89', spent: 0, budget: 200 },
  { id: 'saving', group: 'Goals & fun', name: 'Future you', color: '#438260', spent: 0, budget: 0 }
];

const defaultTransactions = [
  {id:'t1',date:'Nov 10',merchant:'Trader Joe’s',category:'Groceries',account:'Chase Checking ••1842',amount:64.28,icon:'TJ',bg:'#f2e6d1'},
  {id:'t2',date:'Nov 10',merchant:'Square *Greenleaf',category:'Uncategorized',account:'Chase Sapphire ••2091',amount:18.50,icon:'S',bg:'#eae8f4',pending:true},
  {id:'t3',date:'Nov 9',merchant:'Comcast',category:'Utilities',account:'Chase Checking ••1842',amount:72,icon:'C',bg:'#e5edf5'},
  {id:'t4',date:'Nov 9',merchant:'Uber',category:'Transportation',account:'Chase Sapphire ••2091',amount:16.43,icon:'U',bg:'#e8e9e7'},
  {id:'t5',date:'Nov 8',merchant:'The Corner Table',category:'Dining',account:'Chase Sapphire ••2091',amount:48.60,icon:'CT',bg:'#f7e6df'},
  {id:'t6',date:'Nov 7',merchant:'Payroll deposit',category:'Income',account:'Chase Checking ••1842',amount:-2400,icon:'$',bg:'#dff0e4'},
  {id:'t7',date:'Nov 6',merchant:'Netflix',category:'Subscriptions',account:'Capital One Savor ••5022',amount:15.49,icon:'N',bg:'#f5e3e3'},
  {id:'t8',date:'Nov 5',merchant:'Delta Air Lines',category:'Travel',account:'Chase Sapphire ••2091',amount:49,icon:'Δ',bg:'#e5eef5'},
  {id:'t9',date:'Nov 4',merchant:'Target',category:'Shopping',account:'Capital One Savor ••5022',amount:52.14,icon:'T',bg:'#f6e4e6'},
  {id:'t10',date:'Nov 3',merchant:'Shell',category:'Transportation',account:'Chase Sapphire ••2091',amount:38.10,icon:'S',bg:'#faf0d9'}
].map(transaction => ({...transaction, originalCategory: transaction.category}));

const accounts = [
  {institution:'Chase', letter:'C', account:'Total Checking', type:'Checking ••1842', balance:8620, included:true},
  {institution:'Chase', letter:'C', account:'Sapphire Preferred', type:'Credit card ••2091', balance:-10130, included:true},
  {institution:'Capital One', letter:'C1', account:'SavorOne', type:'Credit card ••5022', balance:-3550, included:true},
  {institution:'Capital One', letter:'C1', account:'360 Performance Savings', type:'Savings ••7914', balance:53300, included:true}
];

let budgets = load('budge-it-budgets', defaultBudgets);
let transactions = load('budge-it-transactions', defaultTransactions).map(transaction => (transaction.manual || 'originalCategory' in transaction) ? transaction : {...transaction, originalCategory: transaction.category});
let selectedBudget = null;
const money = value => new Intl.NumberFormat('en-US',{style:'currency',currency:'USD',maximumFractionDigits:0}).format(value);
const preciseMoney = value => new Intl.NumberFormat('en-US',{style:'currency',currency:'USD'}).format(value);
const categoryNames = () => ['Uncategorized', ...budgets.map(b => b.name), 'Income', 'Transfer'].filter((value, index, list) => list.indexOf(value) === index);
const categoryColors = ['#3d9770','#5f86ad','#d3735f','#c59b48','#8c77a3','#608f99','#4c94a5','#d29272','#438260','#b17d89'];

function load(key, fallback) {
  try {
    const saved = JSON.parse(localStorage.getItem(key));
    return Array.isArray(saved) && saved.length ? saved : structuredClone(fallback);
  } catch {
    return structuredClone(fallback);
  }
}
function saveState() {
  localStorage.setItem('budge-it-budgets', JSON.stringify(budgets));
  localStorage.setItem('budge-it-transactions', JSON.stringify(transactions));
}
function categoryGroups() { return [...new Set(budgets.map(budget => budget.group))]; }
function categoryColor() { return categoryColors[budgets.length % categoryColors.length]; }
function renderGroupOptions() {
  document.querySelector('#categoryGroups').innerHTML = categoryGroups().map(group => `<option value="${group}"></option>`).join('');
}
function renderCategoryManager() {
  const list = document.querySelector('#categoryManagerList');
  list.innerHTML = budgets.map(budget => `<div class="category-admin-row" data-category="${budget.id}"><div class="category-admin-fields"><label>Name<input data-field="name" value="${budget.name}" maxlength="30" /></label><label>Group<input data-field="group" value="${budget.group}" maxlength="30" list="categoryGroups" /></label><label>Monthly limit<input data-field="budget" type="number" min="0" step="1" value="${budget.budget}" /></label></div><div class="category-admin-actions"><button class="quiet-button save-category" type="button" data-save="${budget.id}">Save</button><button class="delete-category" type="button" data-delete="${budget.id}">Delete</button></div></div>`).join('');
}
function budgetSpend(budget) {
  return budget.spent + transactions.reduce((adjustment, transaction) => {
    if (transaction.pending || transaction.amount <= 0) return adjustment;
    const original = transaction.originalCategory;
    if (transaction.hiddenFromBudget) return adjustment - (original === budget.name ? transaction.amount : 0);
    return adjustment + (transaction.category === budget.name ? transaction.amount : 0) - (original === budget.name ? transaction.amount : 0);
  }, 0);
}
function withSpend(budget) { return {...budget, spent: budgetSpend(budget)}; }
function budgetStatus(item) {
  const ratio = item.budget ? item.spent / item.budget : 0;
  return ratio > 1 ? 'over' : ratio > .8 ? 'near' : 'on';
}
function statusColor(item) { return budgetStatus(item) === 'over' ? '#cc725e' : budgetStatus(item) === 'near' ? '#d9a13b' : '#54a675'; }
function calculate() {
  return budgets.reduce((total, budget) => {
    const item = withSpend(budget);
    return {spent: total.spent + item.spent, budget: total.budget + item.budget};
  }, {spent:0,budget:0});
}

function renderOverview() {
  const primary = budgets.filter(b => ['Groceries','Dining','Transportation','Shopping'].includes(b.name)).map(withSpend);
  document.querySelector('#overviewBudget').innerHTML = primary.map(item => {
    const remaining = item.budget - item.spent;
    return `<article class="pulse-item"><div class="pulse-top"><i class="category-bullet" style="background:${item.color}"></i><span class="${budgetStatus(item)}">${budgetStatus(item) === 'over' ? 'Over plan' : budgetStatus(item) === 'near' ? 'Almost there' : 'On track'}</span></div><strong>${item.name}</strong><small>${item.spent > item.budget ? money(item.spent - item.budget)+' over' : money(remaining)+' left'}</small><div class="mini-meter"><span style="width:${Math.min(item.spent/item.budget*100,100)}%;background:${statusColor(item)}"></span></div></article>`;
  }).join('');
  document.querySelector('#recentTransactions').innerHTML = transactions.slice(0,5).map(transactionMarkup).join('');
  const totals = calculate();
  document.querySelector('#monthSpend').textContent = money(totals.spent);
  document.querySelector('#remainingTotal').textContent = money(totals.budget - totals.spent) + ' left';
  document.querySelector('#spendMeter').style.width = Math.min(totals.spent / totals.budget * 100, 100) + '%';
}
function transactionMarkup(t) {
  return `<div class="transaction-row"><div class="merchant-icon" style="background:${t.bg}">${t.icon}</div><div><div class="transaction-name">${t.merchant}${t.pending?'<span class="pending-badge">PENDING</span>':''}${t.hiddenFromBudget?'<span class="hidden-badge">HIDDEN</span>':''}</div><div class="transaction-meta">${t.category} · ${t.date}</div></div><div class="transaction-amount">${t.amount<0?'+':''}${preciseMoney(-t.amount)}<small>${t.account.split(' •')[0]}</small></div></div>`;
}

function renderBudgets() {
  const groups = [...new Set(budgets.map(b => b.group))];
  document.querySelector('#budgetGroups').innerHTML = groups.map(group => {
    const items = budgets.filter(b => b.group === group).map(withSpend);
    const groupSpent = items.reduce((total, item) => total + item.spent, 0);
    const groupBudget = items.reduce((total, item) => total + item.budget, 0);
    return `<section class="budget-group"><div class="group-title"><h2>${group}</h2><span>${money(groupSpent)} of ${money(groupBudget)}</span></div>${items.map(item => {
      const remaining = item.budget - item.spent;
      const ratio = item.budget ? item.spent / item.budget : 0;
      const over = item.spent > item.budget;
      return `<div class="budget-row"><div class="budget-name"><i class="category-bullet" style="background:${item.color}"></i>${item.name}<small>${item.spent?'':'No activity'}</small></div><div class="budget-progress"><div class="progress-label"><span>${item.budget?Math.round(Math.min(ratio,1)*100)+'% used':'No limit'}</span><span>${over?money(item.spent - item.budget)+' over':money(Math.max(remaining,0))+' left'}</span></div><div class="progress-track"><span style="width:${Math.min(ratio*100,100)}%;background:${statusColor(item)}"></span></div></div><div class="budget-numbers ${over?'over':''}"><strong>${money(item.spent)}</strong><span> of ${money(item.budget)}</span></div><button class="edit-budget" data-edit="${item.id}" aria-label="Edit ${item.name} budget">⋯</button></div>`;
    }).join('')}</section>`;
  }).join('');
  const totals = calculate();
  document.querySelector('#budgetTotal').textContent = money(totals.budget);
  document.querySelector('#budgetSpent').textContent = money(totals.spent);
  document.querySelector('#budgetRemaining').textContent = money(totals.budget - totals.spent);
}

function renderTransactions() {
  const categorySelect = document.querySelector('#categoryFilter');
  const previousCategory = categorySelect.value;
  const categories = ['all', ...new Set(transactions.map(t => t.category))];
  categorySelect.innerHTML = categories.map(category => `<option value="${category}">${category === 'all' ? 'All categories' : category}</option>`).join('');
  categorySelect.value = categories.includes(previousCategory) ? previousCategory : 'all';
  const query = document.querySelector('#transactionSearch').value.toLowerCase();
  const category = categorySelect.value;
  const state = document.querySelector('#stateFilter').value;
  const matches = transactions.filter(t => (!query || t.merchant.toLowerCase().includes(query) || t.category.toLowerCase().includes(query)) && (category === 'all' || t.category === category) && (state === 'all' || (state === 'pending') === !!t.pending));
  document.querySelector('#transactionTable').innerHTML = matches.map(t => `<tr class="${t.hiddenFromBudget ? 'budget-hidden-row' : ''}"><td>${t.date}${t.pending?'<span class="pending-badge">PENDING</span>':''}</td><td><div class="merchant-cell"><div class="merchant-icon" style="background:${t.bg}">${t.icon}</div>${t.merchant}${t.manual?'<span class="manual-badge">MANUAL</span>':''}</div></td><td><select class="category-select" data-transaction="${t.id}">${categoryNames().map(name => `<option ${name === t.category ? 'selected' : ''}>${name}</option>`).join('')}</select></td><td>${t.account}</td><td><button class="budget-toggle ${t.hiddenFromBudget ? 'is-hidden' : ''}" data-hide="${t.id}">${t.hiddenFromBudget ? 'Hidden' : 'Included'}</button></td><td class="amount ${t.amount<0?'income':''}">${t.amount<0?'+':''}${preciseMoney(-t.amount)}</td></tr>`).join('') || '<tr><td colspan="6" style="text-align:center;color:#67756d;padding:34px">No transactions match those filters.</td></tr>';
}
function renderAccounts() {
  const institutions = [...new Set(accounts.map(a => a.institution))];
  document.querySelector('#accountsList').innerHTML = institutions.map(inst => {
    const items = accounts.filter(x => x.institution === inst);
    const capital = inst === 'Capital One';
    return `<section class="institution-card"><div class="institution-head"><div class="institution-logo ${capital?'capital':''}">${items[0].letter}</div><div><h2>${inst}</h2><p>${items.length} accounts connected</p></div><span class="sync-status">● Healthy</span></div>${items.map(account => `<div class="account-row"><div class="account-name">${account.account}<small>${account.type}</small></div><div class="account-balance">${account.balance<0?'−':''}${money(Math.abs(account.balance))}<small>${account.balance<0?'Current balance':'Available balance'}</small></div><input class="toggle" type="checkbox" ${account.included?'checked':''} data-account="${accounts.indexOf(account)}" aria-label="Include ${account.account} in net worth" /></div>`).join('')}</section>`;
  }).join('');
}
function renderWorth() {
  const assets = accounts.filter(a => a.balance > 0);
  const debts = accounts.filter(a => a.balance < 0);
  const markup = (items, debt) => items.map(account => `<div class="worth-row"><div class="worth-name"><i style="background:${debt?'#cc725e':'#54a675'}"></i><div>${account.account}<small>${account.institution} · ${account.type.split(' •')[0]}</small></div></div><strong>${debt?'−':''}${money(Math.abs(account.balance))}</strong></div>`).join('');
  document.querySelector('#assetsList').innerHTML = markup(assets);
  document.querySelector('#debtsList').innerHTML = markup(debts, true);
}
function renderAll() { renderOverview(); renderBudgets(); renderTransactions(); renderAccounts(); renderWorth(); renderGroupOptions(); }
function toast(message) {
  const el = document.querySelector('#toast');
  el.textContent = message;
  el.classList.add('show');
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => el.classList.remove('show'), 2800);
}
function transactionById(id) { return transactions.find(transaction => transaction.id === id); }
function newId() { return globalThis.crypto?.randomUUID?.() || `manual-${Date.now()}`; }

document.querySelectorAll('[data-view],[data-go]').forEach(el => el.addEventListener('click', () => {
  const view = el.dataset.view || el.dataset.go;
  document.querySelectorAll('.view').forEach(x => x.classList.toggle('active', x.id === view));
  document.querySelectorAll('.nav-item').forEach(x => x.classList.toggle('active', x.dataset.view === view));
  window.scrollTo({top:0,behavior:'smooth'});
}));
document.querySelector('#budgetGroups').addEventListener('click', event => {
  const id = event.target.dataset.edit;
  if (!id) return;
  selectedBudget = budgets.find(b => b.id === id);
  document.querySelector('#dialogTitle').textContent = selectedBudget.name;
  document.querySelector('#budgetAmount').value = selectedBudget.budget;
  document.querySelector('#budgetDialog').showModal();
});
document.querySelector('#saveBudget').addEventListener('click', () => {
  if (!selectedBudget) return;
  const value = Number(document.querySelector('#budgetAmount').value);
  if (Number.isFinite(value) && value >= 0) {
    selectedBudget.budget = value;
    saveState();
    renderAll();
    toast(`${selectedBudget.name} budget updated`);
  }
});
document.querySelector('#addBudget').addEventListener('click', () => {
  renderGroupOptions();
  document.querySelector('#categoryName').value = '';
  document.querySelector('#categoryGroup').value = categoryGroups()[0] || '';
  document.querySelector('#categoryDialog').showModal();
});
document.querySelector('#categoryForm').addEventListener('submit', event => {
  event.preventDefault();
  const form = new FormData(event.currentTarget);
  const name = String(form.get('name')).trim();
  const group = String(form.get('group')).trim();
  const budget = Number(form.get('budget'));
  if (!name || !group || !Number.isFinite(budget) || budget < 0) return;
  if (budgets.some(item => item.name.toLowerCase() === name.toLowerCase())) { toast('A category with that name already exists'); return; }
  budgets.push({id:newId(), group, name, color:categoryColor(), spent:0, budget});
  saveState();
  renderAll();
  document.querySelector('#categoryDialog').close();
  toast(name + ' category added');
});
document.querySelector('#manageCategories').addEventListener('click', () => {
  renderGroupOptions();
  renderCategoryManager();
  document.querySelector('#categoryManagerDialog').showModal();
});
document.querySelector('#categoryManagerList').addEventListener('click', event => {
  const saveId = event.target.dataset.save;
  const deleteId = event.target.dataset.delete;
  if (!saveId && !deleteId) return;
  const id = saveId || deleteId;
  const category = budgets.find(item => item.id === id);
  if (!category) return;
  if (deleteId) {
    if (!window.confirm('Delete “' + category.name + '” from this budget? Its transaction records will be kept and marked Uncategorized.')) return;
    const oldName = category.name;
    budgets = budgets.filter(item => item.id !== id);
    transactions = transactions.map(transaction => transaction.category === oldName ? {...transaction, category:'Uncategorized'} : transaction);
    saveState();
    renderAll();
    renderCategoryManager();
    toast(oldName + ' category deleted');
    return;
  }
  const row = event.target.closest('.category-admin-row');
  const name = row.querySelector('[data-field="name"]').value.trim();
  const group = row.querySelector('[data-field="group"]').value.trim();
  const budget = Number(row.querySelector('[data-field="budget"]').value);
  if (!name || !group || !Number.isFinite(budget) || budget < 0) return;
  if (budgets.some(item => item.id !== id && item.name.toLowerCase() === name.toLowerCase())) { toast('A category with that name already exists'); return; }
  const oldName = category.name;
  category.name = name;
  category.group = group;
  category.budget = budget;
  if (oldName !== name) {
    transactions = transactions.map(transaction => ({...transaction, category: transaction.category === oldName ? name : transaction.category, originalCategory: transaction.originalCategory === oldName ? name : transaction.originalCategory}));
  }
  saveState();
  renderAll();
  renderCategoryManager();
  toast(name + ' category saved');
});
document.querySelector('#resetData').addEventListener('click', () => {
  budgets = structuredClone(defaultBudgets);
  transactions = structuredClone(defaultTransactions);
  saveState();
  renderAll();
  toast('Demo budget and transactions restored');
});
document.querySelector('#transactionSearch').addEventListener('input', renderTransactions);
document.querySelector('#categoryFilter').addEventListener('change', renderTransactions);
document.querySelector('#stateFilter').addEventListener('change', renderTransactions);
document.querySelector('#transactionTable').addEventListener('change', event => {
  const id = event.target.dataset.transaction;
  if (!id) return;
  const transaction = transactionById(id);
  transaction.category = event.target.value;
  saveState();
  renderAll();
  toast(`${transaction.merchant} categorized as ${transaction.category}`);
});
document.querySelector('#transactionTable').addEventListener('click', event => {
  const id = event.target.dataset.hide;
  if (!id) return;
  const transaction = transactionById(id);
  transaction.hiddenFromBudget = !transaction.hiddenFromBudget;
  saveState();
  renderAll();
  toast(`${transaction.merchant} is ${transaction.hiddenFromBudget ? 'hidden from' : 'included in'} your budget`);
});
document.querySelector('#addTransaction').addEventListener('click', () => {
  document.querySelector('#manualDate').value = '2026-11-10';
  document.querySelector('#manualMerchant').value = '';
  document.querySelector('#manualAmount').value = '';
  document.querySelector('#manualBudgetHidden').checked = false;
  document.querySelector('#manualCategory').innerHTML = categoryNames().map(name => `<option>${name}</option>`).join('');
  document.querySelector('#manualTransactionDialog').showModal();
});
document.querySelector('#manualTransactionForm').addEventListener('submit', event => {
  event.preventDefault();
  const form = new FormData(event.currentTarget);
  const amount = Number(form.get('amount'));
  const date = new Date(`${form.get('date')}T12:00:00`);
  if (!form.get('merchant') || !Number.isFinite(amount) || amount <= 0 || Number.isNaN(date.valueOf())) return;
  const transaction = {
    id: newId(),
    date: date.toLocaleDateString('en-US',{month:'short',day:'numeric'}),
    merchant: String(form.get('merchant')).trim(),
    category: String(form.get('category')),
    account: String(form.get('account')).trim() || 'Manual entry',
    amount: form.get('kind') === 'income' ? -amount : amount,
    icon: '+', bg:'#e2f0e5', manual:true,
    hiddenFromBudget: form.get('hidden') === 'on'
  };
  transactions.unshift(transaction);
  saveState();
  renderAll();
  document.querySelector('#manualTransactionDialog').close();
  toast('Manual transaction added');
});
document.querySelector('#accountsList').addEventListener('change', event => {
  const index = event.target.dataset.account;
  if (index === undefined) return;
  accounts[index].included = event.target.checked;
  toast(`${accounts[index].account} ${event.target.checked?'included in':'excluded from'} net worth`);
});
document.querySelector('#refreshButton').addEventListener('click', () => toast('Everything is already up to date'));
document.querySelector('#connectButton').addEventListener('click', () => toast('Secure bank linking is intentionally not enabled in this synthetic-data prototype.'));
document.querySelector('#exportButton').addEventListener('click', () => {
  const rows = ['Date,Merchant,Category,Account,Budget status,Amount', ...transactions.map(t => `"${t.date}","${t.merchant}","${t.category}","${t.account}","${t.hiddenFromBudget?'Hidden':'Included'}",${t.amount}`)];
  const link = document.createElement('a');
  link.href = URL.createObjectURL(new Blob([rows.join('\n')],{type:'text/csv'}));
  link.download = 'budge-it-november-transactions.csv';
  link.click();
  URL.revokeObjectURL(link.href);
  toast('Transaction CSV downloaded');
});
renderAll();
