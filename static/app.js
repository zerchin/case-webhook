'use strict';
const $ = (selector, root = document) => root.querySelector(selector);
const page = document.body.dataset.page;
const csrf = $('meta[name="csrf-token"]').content;
const zone = Intl.DateTimeFormat().resolvedOptions().timeZone;
let rows = [];
let support = [];
let columns = [];
let sort = { key: page === 'support' ? 'updated_at' : 'created_at', direction: page === 'support' ? 1 : -1 };
let modalAction;
let busy = false;
let settingsLoaded = false;
let menuTrigger = null;
let selectedCases = new Set();
let visibleRows = [];

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}
function notice(message, type = '') {
  const box = $('#notice');
  box.textContent = message;
  box.className = `notice ${type}`;
  box.hidden = false;
}
async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf, ...options.headers },
    credentials: 'same-origin',
  });
  if (response.status === 401) { location.assign('/login'); throw new Error('Your session has expired.'); }
  const result = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(result.error || result.reason || `Request failed (${response.status}).`);
  return result;
}
function dateText(value) {
  if (!value) return '—';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : localDateValue(date);
}
function localDateValue(date) {
  const pad = value => String(value).padStart(2, '0');
  return `${String(date.getFullYear()).padStart(4, '0')}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
}
function label(key) {
  return key.split('_').map(word => word === 'id' ? 'ID' : word[0].toUpperCase() + word.slice(1)).join(' ');
}
function badge(status) { return element('span', status, `badge ${status === 'online' ? '' : 'offline'}`); }
function compare(a, b) {
  const left = a[sort.key], right = b[sort.key];
  if (left == null) return right == null ? 0 : -sort.direction;
  if (right == null) return sort.direction;
  let result;
  if (typeof left === 'number' && typeof right === 'number') result = left - right;
  else if (sort.key.endsWith('_at')) result = new Date(left) - new Date(right);
  else result = String(left).localeCompare(String(right), undefined, { numeric: true, sensitivity: 'base' });
  return result * sort.direction;
}
function rowAction(text, action, danger = false) {
  const button = element('button', text, `menu-item${danger ? ' delete' : ''}`);
  button.type = 'button'; button.setAttribute('role', 'menuitem'); button.tabIndex = -1;
  button.addEventListener('click', () => { closeRowMenu(true); action(); });
  return button;
}
function closeRowMenu(restoreFocus = false) {
  $('#row-menu').hidden = true;
  if (menuTrigger) {
    menuTrigger.setAttribute('aria-expanded', 'false');
    if (restoreFocus) menuTrigger.focus();
  }
  menuTrigger = null;
}
function openRowMenu(trigger, row, last = false) {
  if (menuTrigger === trigger) { closeRowMenu(true); return; }
  closeRowMenu(); menuTrigger = trigger;
  const menu = $('#row-menu');
  const actions = page === 'support' ? [
    rowAction('Change status', () => editSupport(row, 'status')),
    rowAction('Set Updated At', () => editSupport(row, 'updated_at')),
    rowAction('Set priority', () => editSupport(row, 'priority')),
    rowAction('Delete Support', () => deleteSupport(row), true),
  ] : [rowAction('Reassign Support', () => reassignCase(row)), rowAction('Delete Case', () => deleteCase(row), true)];
  menu.replaceChildren(...actions); menu.hidden = false;
  trigger.setAttribute('aria-expanded', 'true');
  const rect = trigger.getBoundingClientRect();
  menu.style.left = `${Math.max(8, Math.min(rect.right - menu.offsetWidth, window.innerWidth - menu.offsetWidth - 8))}px`;
  menu.style.top = `${Math.max(8, rect.bottom + menu.offsetHeight + 6 <= window.innerHeight ? rect.bottom + 6 : rect.top - menu.offsetHeight - 6)}px`;
  actions[last ? actions.length - 1 : 0].focus();
}
function actionsButton(row) {
  const button = element('button', '⋮', 'actions-button');
  button.type = 'button';
  button.setAttribute('aria-label', `Actions for ${page === 'support' ? row.name : `Case ${row.id}`}`);
  button.setAttribute('aria-haspopup', 'menu');
  button.setAttribute('aria-expanded', 'false');
  button.setAttribute('aria-controls', 'row-menu');
  button.addEventListener('click', () => openRowMenu(button, row));
  button.addEventListener('keydown', event => {
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault(); openRowMenu(button, row, event.key === 'ArrowUp');
    }
  });
  return button;
}
function updateSelection() {
  if (page !== 'cases') return;
  const selectedCount = selectedCases.size;
  const selectAll = $('#select-all-cases');
  selectAll.checked = visibleRows.length > 0 && selectedCount === visibleRows.length;
  selectAll.indeterminate = selectedCount > 0 && selectedCount < visibleRows.length;
  selectAll.disabled = visibleRows.length === 0;
  $('#delete-selected').disabled = selectedCount === 0;
  $('#delete-selected').textContent = `Delete selected (${selectedCount})`;
  document.querySelectorAll('input[data-case-id]').forEach(checkbox => {
    checkbox.checked = selectedCases.has(checkbox.dataset.caseId);
    checkbox.closest('tr').classList.toggle('selected', checkbox.checked);
  });
}
function caseCheckbox(row) {
  const checkbox = element('input', undefined, 'selection-checkbox');
  checkbox.type = 'checkbox'; checkbox.dataset.caseId = row.id;
  checkbox.setAttribute('aria-label', `Select Case ${row.id}`);
  checkbox.addEventListener('change', () => {
    if (checkbox.checked) selectedCases.add(row.id);
    else selectedCases.delete(row.id);
    updateSelection();
  });
  return checkbox;
}
function renderTable() {
  closeRowMenu();
  const query = $('#search').value.trim().toLowerCase();
  const filtered = rows.filter(row => columns.some(key => (key.endsWith('_at') ? dateText(row[key]) : String(row[key] ?? '')).toLowerCase().includes(query))).sort(compare);
  visibleRows = filtered;
  const visibleIds = new Set(filtered.map(row => row.id));
  selectedCases = new Set([...selectedCases].filter(id => visibleIds.has(id)));
  $('#count').textContent = `${filtered.length} ${page === 'support' ? 'people' : 'cases'}`;
  const header = element('tr');
  if (page === 'cases') {
    const cell = element('th', undefined, 'selection-cell'); cell.scope = 'col';
    const checkbox = element('input', undefined, 'selection-checkbox');
    checkbox.type = 'checkbox'; checkbox.id = 'select-all-cases';
    checkbox.setAttribute('aria-label', 'Select all filtered cases');
    checkbox.addEventListener('change', () => {
      filtered.forEach(row => {
        if (checkbox.checked) selectedCases.add(row.id);
        else selectedCases.delete(row.id);
      });
      updateSelection();
    });
    cell.append(checkbox); header.append(cell);
  }
  columns.forEach(key => {
    const cell = element('th'); cell.scope = 'col';
    cell.setAttribute('aria-sort', sort.key === key ? (sort.direction === 1 ? 'ascending' : 'descending') : 'none');
    const button = element('button', `${label(key)} ${sort.key === key ? (sort.direction === 1 ? '↑' : '↓') : '↕'}`);
    button.type = 'button';
    button.addEventListener('click', () => { sort = { key, direction: sort.key === key ? -sort.direction : 1 }; renderTable(); });
    cell.append(button); header.append(cell);
  });
  const actionsHeader = element('th', 'Actions'); actionsHeader.scope = 'col'; header.append(actionsHeader);
  $('#table-head').replaceChildren(header);
  $('#table-body').replaceChildren(...filtered.map(row => {
    const tr = element('tr');
    if (page === 'cases') {
      const cell = element('td', undefined, 'selection-cell');
      cell.append(caseCheckbox(row)); tr.append(cell);
    }
    columns.forEach(key => {
      const td = element('td');
      const value = row[key];
      if (key === 'status') td.append(badge(value));
      else if (key === 'priority') td.append(element('span', value ?? '—', 'priority'));
      else td.textContent = key.endsWith('_at') ? dateText(value) : (value ?? '—');
      if (key === 'name' || key === 'support_name') td.className = 'name-cell';
      if (key === 'id' || key === 'support_id') td.className = 'id-cell';
      tr.append(td);
    });
    const td = element('td', undefined, 'actions-cell');
    td.append(actionsButton(row)); tr.append(td); return tr;
  }));
  if (!filtered.length) {
    const tr = element('tr'), td = element('td', query ? 'No matching records.' : `No ${page === 'support' ? 'support yet. Add your first team member to get started.' : 'cases yet. Add a case to get started.'}`, 'empty');
    td.colSpan = columns.length + 1 + (page === 'cases' ? 1 : 0); tr.append(td); $('#table-body').append(tr);
  }
  updateSelection();
  $('#sort-note').textContent = `Sorted by ${label(sort.key)} · ${sort.direction === 1 ? 'ascending' : 'descending'}`;
}
async function loadRows() {
  const refresh = $('#refresh'); refresh.disabled = true;
  try {
    if (page === 'support') rows = await api('/api/support');
    else [rows, support] = await Promise.all([api('/api/cases'), api('/api/support')]);
    const defaults = page === 'support' ? ['name', 'id', 'status', 'priority', 'updated_at', 'created_at'] : ['id', 'support_name', 'created_at'];
    columns = page === 'cases' ? defaults : [...new Set([...defaults, ...rows.flatMap(row => Object.keys(row))])];
    renderTable();
  } finally { refresh.disabled = false; }
}
function openModal(title, html, action, submitText = 'Save changes', danger = false) {
  if (busy) return;
  $('#modal-title').textContent = title; $('#modal-fields').innerHTML = html;
  $('#modal-error').hidden = true;
  $('#modal-submit').textContent = submitText;
  $('#modal-submit').className = danger ? 'danger' : 'primary';
  modalAction = action;
  $('#modal').showModal();
}
function fillSupport(select, excludeId) {
  const available = support.filter(row => row.id !== excludeId);
  select.replaceChildren(...available.map(row => {
    const option = element('option', `${row.name} · ${row.status}`); option.value = row.id; return option;
  }));
  if (!available.length) { select.append(element('option', 'No support available')); select.options[0].value = ''; }
}
function addSupport() {
  openModal('Add Support', `
    <label>Name <span class="required">*</span><input name="name" required maxlength="30" autocomplete="off"></label>
    <label>ID <span class="required">*</span><input name="id" required maxlength="20" autocomplete="off"></label>
    <label>Status<select name="status"><option>online</option><option>offline</option></select></label>
    <label>Priority<input name="priority" type="number" step="1" min="-2147483648" max="2147483647" value="100" required></label>
    <p class="field-hint">Created At and Updated At are set to the current time automatically.</p>`,
    form => api('/api/support', { method: 'POST', body: JSON.stringify({ name: form.elements.name.value, id: form.elements.id.value, status: form.elements.status.value, priority: Number(form.elements.priority.value) }) }), 'Add Support');
}
function editSupport(row, field) {
  let html;
  if (field === 'status') html = '<label>Status<select name="status"><option>online</option><option>offline</option></select></label><p class="field-hint">Offline support is excluded from automatic assignment.</p>';
  else if (field === 'priority') html = '<label>Priority<input type="number" name="priority" min="-2147483648" max="2147483647" step="1" required></label><p class="field-hint">100 is the standard priority. Higher values receive a supplement; lower values skip a round.</p>';
  else html = '<label>Updated At<input type="datetime-local" name="updated_at" step="1" min="1000-01-01T00:00:00" max="9999-12-31T23:59:59" required></label><p class="field-hint" id="edit-time-hint"></p>';
  openModal(`Edit ${label(field)}`, html, form => {
    let value = form.elements[field].value;
    if (field === 'priority') value = Number(value);
    if (field === 'updated_at') {
      const normalized = value.length === 16 ? `${value}:00` : value;
      const date = new Date(normalized);
      if (Number.isNaN(date.getTime()) || localDateValue(date).replace(' ', 'T') !== normalized) throw new Error('Select a valid local date and time.');
      value = date.toISOString();
    }
    return api(`/api/support/${encodeURIComponent(row.id)}`, { method: 'PATCH', body: JSON.stringify({ [field]: value }) });
  });
  $('#modal-fields [name]').value = field === 'updated_at' ? (row[field] ? localDateValue(new Date(row[field])).replace(' ', 'T') : '') : row[field];
  if (field === 'updated_at') $('#edit-time-hint').textContent = `Enter time in ${zone}. It is converted to UTC before saving.`;
}
function addCase() {
  openModal('Add Case', `
    <label>Case ID <span class="required">*</span><input name="id" inputmode="numeric" pattern="[0-9]+" title="Use digits only (0–9)." required maxlength="30" autocomplete="off"></label>
    <label class="check"><input name="notify" type="checkbox" checked>Send Slack notification</label>
    <label>Priority<select name="priority"><option>Critical</option><option>High</option><option selected>Medium</option><option>Low</option></select></label>
    <label>Account Name <span class="required">*</span><input name="account_name" required maxlength="255" autocomplete="off"></label>
    <details class="case-advanced" id="case-advanced">
      <summary>Advanced options</summary>
      <div class="form-stack">
        <label>Subject<input name="subject" maxlength="1000" autocomplete="off"></label>
        <label>Product<input name="product" maxlength="255" autocomplete="off"></label>
        <label>Salesforce URL<input name="url" type="url" maxlength="2048" value="https://suse.lightning.force.com" pattern="https?://.+" title="Enter an HTTP or HTTPS URL." autocomplete="off"></label>
      </div>
    </details>
    <label id="manual-label" hidden>Support <span class="required">*</span><select name="support_id" disabled></select></label>
    <p class="field-hint" id="assignment-hint"></p>`, form => {
      const notify = form.elements.notify.checked;
      const data = { id: form.elements.id.value, notify, url: form.elements.url.value };
      if (notify) {
        data.priority = form.elements.priority.value; data.account_name = form.elements.account_name.value;
        data.subject = form.elements.subject.value; data.product = form.elements.product.value;
      }
      else data.support_id = form.elements.support_id.value;
      return api('/api/cases', { method: 'POST', body: JSON.stringify(data) });
    }, 'Add Case');
  const form = $('#modal-form'); fillSupport(form.elements.support_id);
  const toggle = () => {
    const notify = form.elements.notify.checked;
    form.elements.priority.disabled = !notify;
    form.elements.account_name.disabled = !notify;
    form.elements.account_name.required = notify;
    for (const field of ['subject', 'product']) form.elements[field].disabled = !notify;
    form.elements.support_id.disabled = notify;
    form.elements.support_id.required = !notify;
    $('#manual-label').hidden = notify;
    $('#assignment-hint').textContent = notify ? 'Support is assigned automatically using the support queue.' : 'Without a notification, you must select support manually. This does not trigger support rotation or change priority or Updated At.';
  };
  form.elements.notify.addEventListener('change', toggle); toggle();
  form.elements.url.addEventListener('invalid', () => { $('#case-advanced').open = true; });
}
function reassignCase(row) {
  openModal('Reassign Case', `
    <p class="field-hint" id="current-owner"></p>
    <label>New Support <span class="required">*</span><select name="support_id" required></select></label>
    <label class="check"><input type="checkbox" name="notify" checked>Send Slack notification</label>
    <label class="check"><input type="checkbox" name="adjust_priority" checked>Backfilled case: adjust support priority</label>
    <p class="field-hint" id="priority-adjustment-hint"></p>`,
    form => api(`/api/cases/${encodeURIComponent(row.id)}/reassign`, { method: 'POST', body: JSON.stringify({ support_id: form.elements.support_id.value, notify: form.elements.notify.checked, adjust_priority: form.elements.adjust_priority.checked }) }), 'Reassign Case');
  $('#current-owner').textContent = `Case ${row.id} · Currently assigned to ${row.support_name}`;
  $('#priority-adjustment-hint').textContent = support.some(person => person.id === row.support_id)
    ? 'When enabled: previous support +1, new support −1. Both Updated At values stay unchanged.'
    : 'The previous support has been deleted. When enabled, only the new support priority is reduced by 1. Updated At stays unchanged.';
  fillSupport($('#modal-form').elements.support_id, row.support_id);
}
function deleteCase(row) {
  openModal('Delete Case', '<p id="delete-description" class="muted"></p><p class="field-hint">Support priority and queue times will remain unchanged.</p>',
    () => api(`/api/cases/${encodeURIComponent(row.id)}`, { method: 'DELETE' }), 'Delete Case', true);
  $('#delete-description').textContent = `Delete Case ${row.id}? This removes the case record permanently.`;
}
function deleteSupport(row) {
  openModal('Delete Support', '<p id="delete-description" class="muted"></p><p class="field-hint">Existing cases keep this support’s historical name and ID. Other support priorities and queue times stay unchanged.</p>',
    () => api(`/api/support/${encodeURIComponent(row.id)}`, { method: 'DELETE' }), 'Delete Support', true);
  $('#delete-description').textContent = `Delete ${row.name} (${row.id}) from the support queue?`;
}
function deleteSelectedCases() {
  const ids = visibleRows.filter(row => selectedCases.has(row.id)).map(row => row.id);
  if (!ids.length) return;
  openModal('Delete selected cases', '<p id="delete-description" class="muted"></p><ul id="delete-case-list" class="delete-case-list"></ul><p class="field-hint">Only these selected cases will be deleted. Support priority and queue times stay unchanged.</p>',
    () => api('/api/cases/bulk-delete', { method: 'POST', body: JSON.stringify({ ids }) }), `Delete ${ids.length} case(s)`, true);
  $('#delete-description').textContent = `Permanently delete these ${ids.length} selected case(s)?`;
  $('#delete-case-list').replaceChildren(...ids.map(id => element('li', id)));
}
$('#modal-form').addEventListener('submit', async event => {
  event.preventDefault(); if (busy) return;
  busy = true; $('#modal-submit').disabled = true;
  try {
    const result = await modalAction(event.currentTarget);
    $('#modal').close();
    notice(result.message || 'Changes saved successfully.', result.status === 'partial_success' ? 'warning' : '');
    await loadRows().catch(error => notice(`Changes saved. Could not refresh the list: ${error.message}`, 'warning'));
  } catch (error) { $('#modal-error').textContent = error.message; $('#modal-error').hidden = false; }
  finally { busy = false; $('#modal-submit').disabled = false; }
});
$('#close-modal').addEventListener('click', () => { if (!busy) $('#modal').close(); });
$('#cancel-modal').addEventListener('click', () => { if (!busy) $('#modal').close(); });
$('#modal').addEventListener('cancel', event => { if (busy) event.preventDefault(); });
document.addEventListener('pointerdown', event => {
  if (menuTrigger && !$('#row-menu').contains(event.target) && !menuTrigger.contains(event.target)) closeRowMenu();
});
document.addEventListener('keydown', event => {
  if (!menuTrigger) return;
  if (event.key === 'Escape' || event.key === 'Tab') {
    if (event.key === 'Escape') event.preventDefault();
    closeRowMenu(true);
  }
});
$('#row-menu').addEventListener('keydown', event => {
  const items = [...$('#row-menu').querySelectorAll('[role="menuitem"]')];
  const index = items.indexOf(document.activeElement);
  let next;
  if (event.key === 'ArrowDown') next = (index + 1) % items.length;
  else if (event.key === 'ArrowUp') next = (index - 1 + items.length) % items.length;
  else if (event.key === 'Home') next = 0;
  else if (event.key === 'End') next = items.length - 1;
  if (next !== undefined) { event.preventDefault(); items[next].focus(); }
});
window.addEventListener('resize', () => closeRowMenu());
document.addEventListener('scroll', () => closeRowMenu(), true);
$('#logout').addEventListener('click', async () => {
  try { await api('/api/logout', { method: 'POST' }); location.assign('/login'); } catch (error) { notice(error.message, 'error'); }
});
function configuredWebhookPath() {
  const suffix = $('#webhook-path').value.trim();
  return suffix ? $('#webhook-prefix').textContent + suffix : '';
}
function previewPath() {
  const path = configuredWebhookPath();
  $('#webhook-preview').textContent = path ? `${location.origin}${path}` : 'Not configured';
}
function applySettings(settings) {
  const path = settings.webhook_path;
  const prefix = $('#webhook-prefix').textContent;
  const suffix = path.startsWith(prefix) ? path.slice(prefix.length) : '';
  const valid = suffix && suffix.length <= $('#webhook-path').maxLength && /^[A-Za-z0-9_-]+$/.test(suffix);
  $('#webhook-path').value = valid ? suffix : '';
  if (path && !valid) notice(`The saved path ${path} is disabled. Enter a suffix under ${prefix}, save it, and update the PagerDuty webhook URL.`, 'warning');
  $('#slack-state').textContent = settings.slack_webhook_url_configured ? 'Configured' : 'Not configured';
  $('#secret-state').textContent = settings.pagerduty_webhook_secret_configured ? 'Configured' : 'Not configured';
  $('#token-state').textContent = settings.pagerduty_api_token_configured ? 'Configured' : 'Not configured';
  $('#slack-url').value = ''; $('#pd-secret').value = ''; $('#pd-token').value = '';
  $('#slack-url').placeholder = settings.slack_webhook_url_configured ? 'Leave blank to keep saved URL' : 'Enter a webhook URL';
  $('#pd-secret').placeholder = settings.pagerduty_webhook_secret_configured ? 'Leave blank to keep saved secret' : 'Enter your signing secret';
  $('#pd-token').placeholder = settings.pagerduty_api_token_configured ? 'Leave blank to keep saved token' : 'Enter your API token';
  $('#clear-slack').checked = false; $('#clear-secret').checked = false; $('#clear-token').checked = false;
  previewPath();
}
async function loadSettings() {
  applySettings(await api('/api/settings'));
  settingsLoaded = true; $('#save-settings').disabled = false;
}
if (page === 'settings') {
  $('#webhook-path').addEventListener('input', previewPath);
  $('#generate').addEventListener('click', () => {
    const bytes = new Uint8Array(32); crypto.getRandomValues(bytes);
    $('#webhook-path').value = Array.from(bytes, value => value.toString(16).padStart(2, '0')).join('');
    previewPath();
  });
  $('#settings-form').addEventListener('submit', async event => {
    event.preventDefault(); if (!settingsLoaded) return;
    const button = $('#save-settings'); if (button.disabled) return; button.disabled = true;
    const data = { webhook_path: configuredWebhookPath() };
    if ($('#slack-url').value) data.slack_webhook_url = $('#slack-url').value;
    if ($('#pd-secret').value) data.pagerduty_webhook_secret = $('#pd-secret').value;
    if ($('#pd-token').value) data.pagerduty_api_token = $('#pd-token').value;
    if ($('#clear-slack').checked) data.slack_webhook_url = '';
    if ($('#clear-secret').checked) data.pagerduty_webhook_secret = '';
    if ($('#clear-token').checked) data.pagerduty_api_token = '';
    try { applySettings(await api('/api/settings', { method: 'PUT', body: JSON.stringify(data) })); notice('Settings saved. Changes are now active.'); }
    catch (error) { notice(error.message, 'error'); }
    finally { button.disabled = false; }
  });
  loadSettings().catch(error => notice(error.message, 'error'));
} else {
  if (page === 'cases') $('#delete-selected').addEventListener('click', deleteSelectedCases);
  $('#search').addEventListener('input', renderTable);
  $('#refresh').addEventListener('click', () => loadRows().catch(error => notice(error.message, 'error')));
  $('#add').addEventListener('click', page === 'support' ? addSupport : addCase);
  loadRows().catch(error => notice(error.message, 'error'));
}
