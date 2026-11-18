'use strict';
const $ = id => document.getElementById(id);
let state = null;
let balanceData = null;
let noticeTimer;
const labels = {ok: '已更新', error: '查询失败', unsupported: '未接入查询', not_configured: '待配置', no_limit: '未返回额度'};

function node(tag, cls, text) {
  const el = document.createElement(tag);
  if (cls) el.className = cls;
  if (text !== undefined) el.textContent = text;
  return el;
}
function notice(message) {
  $('notice').textContent = message;
  $('notice').hidden = false;
  clearTimeout(noticeTimer);
  noticeTimer = setTimeout(() => { $('notice').hidden = true; }, 5000);
}
function showLogin() {
  state = null; balanceData = null;
  $('login-view').hidden = false;
  $('app-view').hidden = true;
  $('provider-dialog').close();
  $('provider-key').value = '';
}
async function api(path, body) {
  const options = {credentials: 'same-origin', headers: {}};
  if (body !== undefined) {
    options.method = 'POST';
    options.headers = {'Content-Type': 'application/json', 'X-Dashboard-Client': '1', 'X-CSRF-Token': state?.csrf || ''};
    options.body = JSON.stringify(body);
  }
  const response = await fetch(path, options);
  const data = await response.json();
  if (!response.ok) {
    if (response.status === 401 && path !== '/api/login') showLogin();
    throw new Error(data.error || '请求未完成，请稍后重试');
  }
  return data;
}
function view(name) {
  const titles = {overview: '余额总览', providers: '提供商配置', network: '访问配置'};
  for (const key of Object.keys(titles)) $(key + '-view').hidden = key !== name;
  document.querySelectorAll('.nav').forEach(el => el.classList.toggle('active', el.dataset.view === name));
  $('breadcrumb').textContent = '工作空间 / ' + titles[name];
}
document.querySelectorAll('[data-view]').forEach(el => el.addEventListener('click', () => view(el.dataset.view)));

function icon(provider) {
  const marks = {deepseek: 'D', codex: 'G', openai: 'G', anthropic: 'C', xai: 'x', openrouter: 'O', openrouter_key: 'O', custom: '↗'};
  return node('div', 'provider-icon ' + provider.kind, marks[provider.kind] || 'M');
}
function editButton(provider, text = '配置账号 ↗') {
  const button = node('button', 'text-button', text);
  button.type = 'button';
  button.addEventListener('click', () => editProvider(provider));
  return button;
}
function balanceCard(row) {
  const provider = state.providers.find(p => p.id === row.id);
  const card = node('article', 'balance-card');
  const top = node('div', 'card-top');
  const title = node('div');
  title.append(node('div', 'card-title', row.name), node('div', 'card-subtitle', row.metric));
  top.append(icon(row), title, node('span', 'badge ' + row.status, labels[row.status] || '未查询'));
  card.append(top);
  if (row.status === 'ok') {
    for (const balance of row.balances) {
      const amount = node('div', 'amount-row');
      amount.append(node('strong', 'amount-value', balance.total), node('span', 'amount-unit', balance.currency));
      card.append(amount);
      if (balance.granted !== undefined) card.append(node('p', 'balance-detail', `赠送 ${balance.granted} · 充值 ${balance.topped_up}`));
    }
  } else {
    const text = {not_configured: '等待配置密钥', error: '暂时无法查询', unsupported: '请到官方页面查看', no_limit: '未返回预算余额'}[row.status] || '尚未查询';
    card.append(node('div', 'amount-row balance-placeholder', text));
  }
  if (row.message) card.append(node('div', 'balance-detail', row.message));
  const tags = node('div', 'model-tags');
  for (const name of row.models) tags.append(node('span', 'model-tag', name));
  if (!row.models.length) tags.append(node('span', 'model-tag', '未关联模型'));
  card.append(tags);
  const actions = node('div', 'card-actions');
  if (row.console) {
    const link = node('a', '', '官方控制台 ↗');
    link.href = row.console; link.target = '_blank'; link.rel = 'noopener noreferrer';
    actions.append(link);
  } else actions.append(node('span'));
  if (provider) actions.append(editButton(provider));
  card.append(actions);
  return card;
}
function renderBalances() {
  const rows = balanceData?.providers || [];
  $('account-count').textContent = state.providers.length;
  $('ready-count').textContent = balanceData ? rows.filter(p => p.status === 'ok').length : '—';
  $('model-count').textContent = new Set(state.providers.flatMap(p => p.models)).size;
  $('cards-count').textContent = state.providers.length;
  $('updated-at').textContent = balanceData ? new Date(balanceData.updated_at).toLocaleTimeString('zh-CN', {hour12: false}) : '尚未查询';
  const cards = $('balance-cards'); cards.replaceChildren();
  for (const row of rows) cards.append(balanceCard(row));
  if (!state.providers.length) cards.append(node('div', 'empty', '还没有账号，在「提供商配置」中添加第一个账号。'));
}
function renderProviders() {
  const list = $('provider-list'); list.replaceChildren();
  for (const provider of state.providers) {
    const row = node('article', 'provider-row');
    const meta = node('div', 'provider-meta');
    const kind = state.kinds[provider.kind];
    meta.append(node('h2', '', provider.name), node('p', '', kind.label + ' · ' + (provider.key_configured ? '查询密钥已保存' : kind.endpoint || provider.kind === 'custom' ? '未保存查询密钥' : '官方页面查询')));
    const actions = node('div', 'provider-actions');
    actions.append(editButton(provider, '编辑'));
    const remove = node('button', 'text-button delete-button', '移除');
    remove.addEventListener('click', async () => {
      if (!confirm(`移除「${provider.name}」及其余额查询配置？模型调用配置不受影响。`)) return;
      remove.disabled = true;
      try {
        await api('/api/providers/delete', {id: provider.id});
        await loadState(); await refreshBalances(false);
        notice('账号已移除');
      } catch (error) { notice(error.message); }
      finally { remove.disabled = false; }
    });
    actions.append(remove); row.append(icon(provider), meta, actions); list.append(row);
  }
  if (!state.providers.length) list.append(node('div', 'empty', '点击「添加账号」开始配置。'));
}
function renderNetwork() {
  $('network-host').value = state.network.host;
  $('network-port').value = state.network.port;
  $('network-url').value = state.network.public_url;
  $('current-address').textContent = location.origin;
  $('network-message').textContent = JSON.stringify(state.network) !== JSON.stringify(state.active_network) ? '已保存的访问配置与当前运行配置不同；重启管理服务后生效。' : '';
}
async function loadState() {
  state = await api('/api/state');
  $('login-view').hidden = true; $('app-view').hidden = false;
  renderProviders(); renderNetwork(); renderBalances();
}
async function refreshBalances(force) {
  $('refresh').disabled = true;
  $('balance-loading').hidden = false;
  $('balance-loading').textContent = '正在读取余额…';
  try {
    const data = await api(force ? '/api/balances/refresh' : '/api/balances', force ? {} : undefined);
    if (!state) return;
    balanceData = data; renderBalances();
    if (data.changed) notice('查询过程中配置发生变化，请再次刷新。');
    $('balance-loading').hidden = true;
  } catch (error) {
    $('balance-loading').textContent = '余额未更新：' + error.message;
    notice(error.message);
  } finally { $('refresh').disabled = false; }
}
function kindFields() {
  const kind = state.kinds[$('provider-kind').value];
  $('kind-note').textContent = kind.note || '通过官方余额接口查询，同一账号下的模型共享余额。';
  $('key-fields').hidden = !kind.endpoint && $('provider-kind').value !== 'custom';
  $('custom-fields').hidden = $('provider-kind').value !== 'custom';
  $('provider-endpoint').required = $('provider-kind').value === 'custom';
  $('provider-path').required = $('provider-kind').value === 'custom';
}
function editProvider(provider = null) {
  $('provider-form').reset(); $('provider-error').textContent = '';
  $('provider-title').textContent = provider ? '编辑账号' : '添加账号';
  $('provider-id').value = provider?.id || '';
  $('provider-name').value = provider?.name || '';
  $('provider-kind').replaceChildren();
  for (const [key, kind] of Object.entries(state.kinds)) {
    const option = node('option', '', kind.label); option.value = key;
    $('provider-kind').append(option);
  }
  $('provider-kind').value = provider?.kind || 'deepseek';
  $('provider-models').value = (provider?.models || []).join(', ');
  $('provider-endpoint').value = provider?.endpoint || '';
  $('provider-path').value = provider?.amount_path || '';
  $('provider-currency').value = provider?.currency || 'CNY';
  $('key-note').textContent = provider?.key_configured ? '已有密钥，留空保持原值。更换接口类型或地址后需要重新填写。' : '填写余额接口的查询密钥，不是管理登录令牌或路由连接密钥。';
  kindFields(); $('provider-dialog').showModal();
}
$('add-provider').addEventListener('click', () => editProvider());
$('close-dialog').addEventListener('click', () => $('provider-dialog').close());
$('provider-dialog').addEventListener('close', () => { $('provider-key').value = ''; });
$('provider-kind').addEventListener('change', kindFields);
$('provider-form').addEventListener('submit', async event => {
  event.preventDefault();
  const button = event.submitter; button.disabled = true;
  const value = {name: $('provider-name').value.trim(), kind: $('provider-kind').value,
    models: $('provider-models').value.split(/[,，\n]/).map(v => v.trim()).filter(Boolean),
    api_key: $('provider-key').value.trim(), clear_key: $('clear-key').checked};
  if ($('provider-id').value) value.id = $('provider-id').value;
  if (value.kind === 'custom') Object.assign(value, {endpoint: $('provider-endpoint').value.trim(), amount_path: $('provider-path').value.trim(), currency: $('provider-currency').value});
  try {
    await api('/api/providers/save', value);
    $('provider-dialog').close(); balanceData = null;
    await loadState(); notice('账号配置已保存'); await refreshBalances(false);
  } catch (error) { $('provider-error').textContent = error.message; }
  finally { button.disabled = false; }
});
$('network-form').addEventListener('submit', async event => {
  event.preventDefault(); const button = event.submitter; button.disabled = true;
  try {
    await api('/api/network', {host: $('network-host').value.trim(), port: Number($('network-port').value), public_url: $('network-url').value.trim()});
    await loadState(); notice('访问配置已保存，重启管理服务后生效');
  } catch (error) { notice(error.message); }
  finally { button.disabled = false; }
});
$('login-form').addEventListener('submit', async event => {
  event.preventDefault(); const button = event.submitter; button.disabled = true;
  $('login-error').textContent = '';
  try {
    await api('/api/login', {token: $('login-token').value});
    $('login-token').value = ''; await loadState(); await refreshBalances(false);
  } catch (error) { $('login-error').textContent = error.message; }
  finally { button.disabled = false; }
});
async function logout() {
  try { await api('/api/logout', {}); showLogin(); }
  catch (error) { notice(error.message); }
}
$('logout').addEventListener('click', logout);
$('mobile-logout').addEventListener('click', logout);
$('refresh').addEventListener('click', () => refreshBalances(true));
$('copy-address').addEventListener('click', async () => {
  try { await navigator.clipboard.writeText(location.origin); notice('地址已复制'); }
  catch { notice('当前浏览器不支持复制，请手动复制地址'); }
});
(async () => {
  try { await loadState(); await refreshBalances(false); }
  catch { showLogin(); }
})();
