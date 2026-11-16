/* Admin tools: activity logs, trash/restore, and alert-email settings. */
(function () {
  'use strict';

  function esc(value) {
    return String(value == null ? '' : value).replace(/[&<>"']/g, function (ch) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch];
    });
  }

  function isAdmin() {
    return typeof currentUser !== 'undefined' && !!currentUser && currentUser.role === 'Administrator';
  }

  function fmtDate(value) {
    if (!value) { return '—'; }
    var date = new Date(value);
    if (isNaN(date.getTime())) { return String(value); }
    return date.toLocaleString();
  }

  var logRows = [];

  function renderLogs() {
    var body = document.getElementById('logsBody');
    if (!body) { return; }
    if (!isAdmin()) {
      body.innerHTML = '<tr><td colspan="5" class="muted">Administrators only.</td></tr>';
      return;
    }
    var query = (document.getElementById('logsSearch').value || '').toLowerCase();
    var action = document.getElementById('logsActionFilter').value;
    var rows = logRows.filter(function (row) {
      if (action && row.action.indexOf(action) !== 0) { return false; }
      if (query && (row.actor + ' ' + row.action + ' ' + row.target + ' ' + row.detail).toLowerCase().indexOf(query) < 0) {
        return false;
      }
      return true;
    });
    body.innerHTML = rows.length ? rows.map(function (row) {
      return '<tr><td>' + fmtDate(row.createdAt) + '</td><td>' + esc(row.actor) + '</td>'
        + '<td><code>' + esc(row.action) + '</code></td><td>' + esc(row.target || '—') + '</td>'
        + '<td class="log-detail">' + esc(row.detail || '') + '</td></tr>';
    }).join('') : '<tr><td colspan="5" class="muted">No matching activity.</td></tr>';
  }

  function loadLogs() {
    var body = document.getElementById('logsBody');
    if (!body || !isAdmin()) { return; }
    body.innerHTML = '<tr><td colspan="5" class="muted">Loading activity…</td></tr>';
    authRequest('/logs?limit=200').then(function (rows) {
      logRows = Array.isArray(rows) ? rows : [];
      var select = document.getElementById('logsActionFilter');
      var actions = [];
      logRows.forEach(function (row) {
        if (row.action && actions.indexOf(row.action) < 0) { actions.push(row.action); }
      });
      actions.sort();
      var current = select.value;
      select.innerHTML = '<option value="">All actions</option>'
        + actions.map(function (item) { return '<option>' + esc(item) + '</option>'; }).join('');
      select.value = current;
      renderLogs();
    }).catch(function (error) {
      body.innerHTML = '<tr><td colspan="5" class="muted">' + esc(error.message) + '</td></tr>';
    });
  }

  var trashRows = [];

  function kindLabel(kind) {
    return { carrier: 'Carrier', network: 'Network provider', provider: 'RSP provider', user: 'User' }[kind] || kind;
  }

  function renderTrash() {
    var body = document.getElementById('trashBody');
    if (!body) { return; }
    var empty = document.getElementById('trashEmpty');
    body.innerHTML = trashRows.map(function (item) {
      return '<tr><td>' + esc(kindLabel(item.kind)) + '</td><td><strong>' + esc(item.name) + '</strong></td>'
        + '<td>' + esc(item.deletedBy || '—') + '</td><td>' + fmtDate(item.deletedAt) + '</td>'
        + '<td><div class="billing-row-actions">'
        + '<button class="btn btn-secondary btn-small" data-trash-restore="' + item.id + '"><i data-lucide="rotate-ccw"></i>Restore</button>'
        + '<button class="btn btn-secondary btn-small" data-trash-purge="' + item.id + '"><i data-lucide="trash-2"></i>Delete forever</button>'
        + '</div></td></tr>';
    }).join('');
    if (empty) { empty.hidden = trashRows.length > 0; }
    if (typeof icons === 'function') { icons(); }
  }

  function loadTrash() {
    var body = document.getElementById('trashBody');
    if (!body || !isAdmin()) { return; }
    authRequest('/trash').then(function (rows) {
      trashRows = Array.isArray(rows) ? rows : [];
      renderTrash();
    }).catch(function (error) { toast(error.message, 'error'); });
  }

  document.addEventListener('click', function (event) {
    var restore = event.target.closest ? event.target.closest('[data-trash-restore]') : null;
    var purge = event.target.closest ? event.target.closest('[data-trash-purge]') : null;
    if (!restore && !purge) { return; }
    if (!isAdmin()) { toast('Administrators only', 'error'); return; }
    if (restore) {
      authRequest('/trash/' + restore.getAttribute('data-trash-restore') + '/restore', { method: 'POST' })
        .then(function (result) { toast('Restored ' + (result.name || 'item')); loadTrash(); })
        .catch(function (error) { toast(error.message, 'error'); });
      return;
    }
    if (!window.confirm('Delete this record forever? This cannot be undone.')) { return; }
    authRequest('/trash/' + purge.getAttribute('data-trash-purge'), { method: 'DELETE' })
      .then(function () { toast('Deleted forever'); loadTrash(); })
      .catch(function (error) { toast(error.message, 'error'); });
  });

  /* ---------------- email settings ---------------- */

  function renderOutbox(rows) {
    var host = document.getElementById('emailOutbox');
    if (!host) { return; }
    if (!rows || !rows.length) {
      host.innerHTML = '<p class="muted">No alert emails yet.</p>';
      return;
    }
    host.innerHTML = rows.map(function (row) {
      return '<div class="email-out-row"><div><strong>' + esc(row.subject) + '</strong>'
        + '<small>' + esc(row.to) + ' · ' + fmtDate(row.createdAt) + '</small></div>'
        + '<span class="badge ' + (row.status === 'failed' ? 'bill-failed' : 'active') + '">'
        + esc(row.status) + '</span></div>';
    }).join('');
  }

  function loadEmail() {
    var host = document.getElementById('emailSettingsCard');
    if (!host || !isAdmin()) { return; }
    authRequest('/email/settings').then(function (settings) {
      var form = document.getElementById('emailForm');
      if (!form) { return; }
      form.enabled.checked = !!settings.enabled;
      form.mode.value = settings.mode || 'simulated';
      form.host.value = settings.host || '';
      form.port.value = settings.port || 587;
      form.username.value = settings.username || '';
      form.fromAddress.value = settings.fromAddress || '';
      form.adminRecipients.value = settings.adminRecipients || '';
      form.useTls.checked = settings.useTls !== false;
      form.password.value = '';
      var note = document.getElementById('emailModeNote');
      if (note) {
        note.textContent = settings.mode === 'simulated'
          ? 'Simulated mode records emails in the outbox without sending them.'
          : 'SMTP mode sends real email using the server below.';
      }
    }).catch(function () {});
    authRequest('/email/outbox?limit=8').then(renderOutbox).catch(function () {});
  }

  function buildEmailCard() {
    var settings = document.getElementById('settings');
    if (!settings || !isAdmin() || document.getElementById('emailSettingsCard')) { return; }
    var card = document.createElement('article');
    card.id = 'emailSettingsCard';
    card.className = 'card email-settings-card';
    card.innerHTML = [
      '<div class="card-heading"><div><h2>Alert emails</h2>',
      '<p>Send usage, invoice and payment alerts to admins and customers.</p></div>',
      '<span class="badge active">Admin only</span></div>',
      '<form id="emailForm"><div class="form-grid">',
      '<label class="toggle-field full"><span><strong>Enable alert emails</strong>',
      '<small>New alerts are emailed within five minutes</small></span>',
      '<span class="switch"><input name="enabled" type="checkbox"><span></span></span></label>',
      '<label>Mode<select name="mode" id="emailMode">',
      '<option value="simulated">Simulated (outbox only)</option>',
      '<option value="smtp">SMTP (send real mail)</option></select></label>',
      '<label class="toggle-field"><span><strong>Use TLS</strong><small>STARTTLS on port 587</small></span>',
      '<span class="switch"><input name="useTls" type="checkbox" checked><span></span></span></label>',
      '<label>SMTP host<input name="host" placeholder="smtp.example.com"></label>',
      '<label>Port<input name="port" type="number" min="1" max="65535" value="587"></label>',
      '<label>Username<input name="username" autocomplete="off"></label>',
      '<label>Password <span class="optional">Leave blank to keep</span>',
      '<input name="password" type="password" autocomplete="new-password"></label>',
      '<label>From address<input name="fromAddress" type="email" value="alerts@raw-switch.io"></label>',
      '<label>Admin recipients<input name="adminRecipients" placeholder="ops@example.com, cto@example.com"></label>',
      '</div>',
      '<p class="field-help" id="emailModeNote"></p>',
      '<div class="modal-actions email-actions">',
      '<button type="button" class="btn btn-secondary" id="emailTest">Send test email</button>',
      '<button type="button" class="btn btn-secondary" id="emailDispatch">Send alerts now</button>',
      '<button type="submit" class="btn btn-primary"><i data-lucide="check"></i>Save email settings</button>',
      '</div></form>',
      '<div class="email-outbox"><h3>Recent alert emails</h3><div id="emailOutbox"></div></div>'
    ].join('');
    settings.appendChild(card);

    card.querySelector('#emailForm').addEventListener('submit', function (event) {
      event.preventDefault();
      var form = event.target;
      var payload = {
        enabled: form.enabled.checked,
        mode: form.mode.value,
        host: form.host.value.trim(),
        port: Number(form.port.value || 587),
        username: form.username.value.trim(),
        fromAddress: form.fromAddress.value.trim(),
        useTls: form.useTls.checked,
        adminRecipients: form.adminRecipients.value.trim()
      };
      if (form.password.value.trim()) { payload.password = form.password.value.trim(); }
      authRequest('/email/settings', { method: 'PATCH', body: JSON.stringify(payload) })
        .then(function () { form.password.value = ''; toast('Email settings saved'); loadEmail(); })
        .catch(function (error) { toast(error.message, 'error'); });
    });

    card.querySelector('#emailTest').addEventListener('click', function () {
      authRequest('/email/test', { method: 'POST' })
        .then(function (result) {
          var statuses = Object.keys(result.results || {}).map(function (key) { return key + ': ' + result.results[key]; });
          toast('Test email ' + (statuses.join(', ') || 'queued'));
          loadEmail();
        })
        .catch(function (error) { toast(error.message, 'error'); });
    });

    card.querySelector('#emailDispatch').addEventListener('click', function () {
      authRequest('/email/dispatch', { method: 'POST' })
        .then(function (result) {
          toast('Alerts: ' + result.alerts + ' · sent: ' + result.sent + ' · skipped: ' + result.skipped);
          loadEmail();
        })
        .catch(function (error) { toast(error.message, 'error'); });
    });

    if (typeof icons === 'function') { icons(); }
    loadEmail();
  }


  /* ---------------- two-factor authentication ---------------- */

  function loadTwoFactor() {
    var body = document.getElementById('twoFactorBody');
    if (!body || !isAdmin()) { return; }
    authRequest('/auth/2fa/status').then(function (status) {
      var badge = document.getElementById('twoFactorBadge');
      if (badge) {
        badge.textContent = status.enabled ? 'Enabled' : 'Disabled';
        badge.className = 'badge ' + (status.enabled ? 'active' : 'inactive');
      }
      if (status.enabled) {
        body.innerHTML = '<div class="twofa-row"><div><strong>Authenticator app</strong>'
          + '<small>A 6-digit code is required at every sign-in.</small></div>'
          + '<div class="twofa-actions"><input id="twofaDisableCode" maxlength="6" placeholder="6-digit code">'
          + '<button class="btn btn-secondary" id="twofaDisable">Disable</button></div></div>';
        document.getElementById('twofaDisable').addEventListener('click', function () {
          var code = document.getElementById('twofaDisableCode').value.trim();
          authRequest('/auth/2fa/disable', { method: 'POST', body: JSON.stringify({ code: code }) })
            .then(function () { toast('Two-factor disabled'); loadTwoFactor(); })
            .catch(function (error) { toast(error.message, 'error'); });
        });
        return;
      }
      body.innerHTML = '<div class="twofa-row"><div><strong>Not enabled</strong>'
        + '<small>Protect this account with an authenticator app.</small></div>'
        + '<button class="btn btn-primary" id="twofaSetup">Set up</button></div>'
        + '<div id="twofaSetupBox" hidden></div>';
      document.getElementById('twofaSetup').addEventListener('click', function () {
        authRequest('/auth/2fa/setup', { method: 'POST' }).then(function (setup) {
          var box = document.getElementById('twofaSetupBox');
          box.hidden = false;
          box.innerHTML = '<div class="twofa-secret"><strong>Add this to your authenticator</strong>'
            + '<code>' + esc(setup.secret) + '</code>'
            + '<small>Or use this link: ' + esc(setup.otpauthUrl) + '</small></div>'
            + '<div class="twofa-actions" style="margin-top:12px">'
            + '<input id="twofaEnableCode" maxlength="6" placeholder="6-digit code">'
            + '<button class="btn btn-primary" id="twofaEnable">Enable</button></div>';
          document.getElementById('twofaEnable').addEventListener('click', function () {
            var code = document.getElementById('twofaEnableCode').value.trim();
            authRequest('/auth/2fa/enable', { method: 'POST', body: JSON.stringify({ code: code }) })
              .then(function () { toast('Two-factor enabled'); loadTwoFactor(); })
              .catch(function (error) { toast(error.message, 'error'); });
          });
        }).catch(function (error) { toast(error.message, 'error'); });
      });
    }).catch(function () {});
  }

  function buildTwoFactorCard() {
    var settings = document.getElementById('settings');
    if (!settings || !isAdmin() || document.getElementById('twoFactorCard')) { return; }
    var card = document.createElement('article');
    card.id = 'twoFactorCard';
    card.className = 'card email-settings-card';
    card.innerHTML = '<div class="card-heading"><div><h2>Two-factor authentication</h2>'
      + '<p>Require an authenticator code when signing in.</p></div>'
      + '<span class="badge inactive" id="twoFactorBadge">Checking...</span></div>'
      + '<div id="twoFactorBody"></div>';
    settings.appendChild(card);
    loadTwoFactor();
  }

    /* ---------------- prepaid lifecycle + customer fees ---------------- */

  function buildPrepaidPolicyCard() {
    var settings = document.getElementById('settings');
    if (!settings || !isAdmin() || document.getElementById('prepaidPolicyCard')) return;
    var card = document.createElement('article');
    card.id = 'prepaidPolicyCard';
    card.className = 'card email-settings-card';
    card.innerHTML = '<div class="card-heading"><div><h2>Prepaid lifecycle</h2>' +
      '<p>Auto-suspend SIMs whose account has not paid, then deactivate them.</p></div>' +
      '<span class="badge active" id="prepaidPolicyBadge">Loading…</span></div>' +
      '<div class="twofa-row"><label style="font-size:11px">Grace days<input id="prepaidGrace" type="number" min="1" style="width:80px;margin-left:8px"></label>' +
      '<label style="font-size:11px">Deactivate days<input id="prepaidDeactivate" type="number" min="1" style="width:80px;margin-left:8px"></label>' +
      '<label style="font-size:11px"><input id="prepaidEnabled" type="checkbox" style="margin-left:8px"> Enabled</label></div>' +
      '<div class="twofa-actions" style="margin-top:10px"><button class="btn btn-primary" id="prepaidSave">Save policy</button>' +
      '<button class="btn btn-secondary" id="prepaidRun">Run sweep now</button></div>' +
      '<p class="muted" id="prepaidResult" style="margin-top:8px"></p>';
    settings.appendChild(card);
    var load = function () {
      authRequest('/billing/prepaid-policy').then(function (p) {
        document.getElementById('prepaidGrace').value = p.graceDays;
        document.getElementById('prepaidDeactivate').value = p.deactivateDays;
        document.getElementById('prepaidEnabled').checked = !!p.enabled;
        var b = document.getElementById('prepaidPolicyBadge');
        b.textContent = p.enabled ? 'Enabled' : 'Disabled';
        b.className = 'badge ' + (p.enabled ? 'active' : 'inactive');
      }).catch(function (e) { toast(e.message, 'error'); });
    };
    document.getElementById('prepaidSave').addEventListener('click', function () {
      authRequest('/billing/prepaid-policy', { method: 'PUT', body: JSON.stringify({
        graceDays: Number(document.getElementById('prepaidGrace').value || 15),
        deactivateDays: Number(document.getElementById('prepaidDeactivate').value || 16),
        enabled: document.getElementById('prepaidEnabled').checked
      }) }).then(function () { toast('Prepaid policy saved'); load(); }).catch(function (e) { toast(e.message, 'error'); });
    });
    document.getElementById('prepaidRun').addEventListener('click', function () {
      authRequest('/billing/apply-prepaid', { method: 'POST' }).then(function (r) {
        document.getElementById('prepaidResult').textContent = r.enabled === false
          ? 'Policy disabled - nothing swept'
          : 'Suspended ' + r.suspended + ' · deactivated ' + r.deactivated;
        toast('Prepaid sweep complete');
      }).catch(function (e) { toast(e.message, 'error'); });
    });
    load();
    if (typeof icons === 'function') icons();
  }

  function buildCustomerFeesCard() {
    var settings = document.getElementById('settings');
    if (!settings || !isAdmin() || document.getElementById('customerFeesCard')) return;
    var card = document.createElement('article');
    card.id = 'customerFeesCard';
    card.className = 'card email-settings-card';
    card.innerHTML = '<div class="card-heading"><div><h2>Customer fees</h2>' +
      '<p>Pre-set the new SIM addition fee and platform fee per customer.</p></div></div>' +
      '<div id="customerFeesList" class="stack-list"></div>';
    settings.appendChild(card);
    authRequest('/customers').then(function (list) {
      document.getElementById('customerFeesList').innerHTML = list.map(function (c) {
        return '<div class="twofa-row" style="border-top:1px solid var(--border);padding-top:10px;flex-wrap:wrap">' +
          '<div><strong>' + esc(c.name) + '</strong><small>' + (c.sims || 0) + ' SIMs · last paid ' + (c.lastPaidAt ? new Date(c.lastPaidAt).toLocaleDateString() : 'never') + '</small></div>' +
          '<div class="twofa-actions">' +
          '<label style="font-size:10px">SIM fee $<input data-cust-fee="' + c.id + '" type="number" step="0.01" min="0" value="' + (c.simAdditionFee || 0) + '" style="width:70px"></label>' +
          '<label style="font-size:10px">Platform %<input data-cust-pct="' + c.id + '" type="number" step="0.01" min="0" value="' + (c.platformFeePct || 0) + '" style="width:70px"></label>' +
          '<label style="font-size:10px">Discount %<input data-cust-disc="' + c.id + '" type="number" step="0.01" min="0" max="100" value="' + (c.discountPct || 0) + '" style="width:70px"></label>' +
          '<button class="btn btn-secondary" data-cust-save="' + c.id + '">Save</button>' +
          '</div></div>';
      }).join('') || '<p class="muted">No customers yet.</p>';
      if (typeof icons === 'function') icons();
    }).catch(function (e) { toast(e.message, 'error'); });
    card.addEventListener('click', function (event) {
      var btn = event.target.closest('[data-cust-save]');
      if (!btn) return;
      var id = btn.dataset.custSave;
      var fee = document.querySelector('[data-cust-fee="' + id + '"]').value;
      var pct = document.querySelector('[data-cust-pct="' + id + '"]').value;
      var disc = document.querySelector('[data-cust-disc="' + id + '"]').value;
      authRequest('/customers/' + id, { method: 'PATCH', body: JSON.stringify({ simAdditionFee: Number(fee || 0), platformFeePct: Number(pct || 0), discountPct: Number(disc || 0) }) })
        .then(function () { toast('Customer fees saved'); })
        .catch(function (e) { toast(e.message, 'error'); });
    });
  }


    /* ---------------- platform pricing + discount tiers ---------------- */

  function buildPlatformPricingCard() {
    var settings = document.getElementById('settings');
    if (!settings || !isAdmin() || document.getElementById('platformPricingCard')) return;
    var card = document.createElement('article');
    card.id = 'platformPricingCard';
    card.className = 'card email-settings-card';
    card.innerHTML = '<div class="card-heading"><div><h2>Platform pricing</h2>' +
      '<p>Monthly SIM fee and one-time activation fee applied across the platform.</p></div></div>' +
      '<div class="twofa-row"><label style="font-size:11px">Monthly SIM fee $<input id="pricingMonthly" type="number" step="0.01" min="0" style="width:90px;margin-left:8px"></label>' +
      '<label style="font-size:11px">Activation fee $<input id="pricingActivation" type="number" step="0.01" min="0" style="width:90px;margin-left:8px"></label>' +
      '<button class="btn btn-primary" id="pricingSave">Save</button></div>';
    settings.appendChild(card);
    var load = function () {
      authRequest('/billing/platform-pricing').then(function (p) {
        document.getElementById('pricingMonthly').value = p.monthlySimFee;
        document.getElementById('pricingActivation').value = p.activationFee;
      }).catch(function (e) { toast(e.message, 'error'); });
    };
    document.getElementById('pricingSave').addEventListener('click', function () {
      authRequest('/billing/platform-pricing', { method: 'PUT', body: JSON.stringify({
        monthlySimFee: Number(document.getElementById('pricingMonthly').value || 0),
        activationFee: Number(document.getElementById('pricingActivation').value || 0)
      }) }).then(function () { toast('Platform pricing saved'); load(); }).catch(function (e) { toast(e.message, 'error'); });
    });
    load();
    if (typeof icons === 'function') icons();
  }

  function buildDiscountTiersCard() {
    var settings = document.getElementById('settings');
    if (!settings || !isAdmin() || document.getElementById('discountTiersCard')) return;
    var card = document.createElement('article');
    card.id = 'discountTiersCard';
    card.className = 'card email-settings-card';
    card.innerHTML = '<div class="card-heading"><div><h2>Volume discounts</h2>' +
      '<p>Discount by number of SIMs a customer has (e.g. 50 SIMs gives 5%).</p></div></div>' +
      '<div id="discountTierList" class="stack-list"></div>' +
      '<div class="twofa-actions" style="margin-top:10px"><input id="tierMin" type="number" min="0" placeholder="Min SIMs" style="width:100px">' +
      '<input id="tierPct" type="number" min="0" max="100" placeholder="Discount %" style="width:100px">' +
      '<button class="btn btn-primary" id="tierAdd">Add tier</button></div>';
    settings.appendChild(card);
    function render() {
      authRequest('/billing/discount-tiers').then(function (tiers) {
        document.getElementById('discountTierList').innerHTML = tiers.map(function (t, i) {
          return '<div class="twofa-row" style="border-top:1px solid var(--border);padding-top:8px"><div><strong>' + t.minSims + '+ SIMs</strong><small>' + t.discountPct + '% discount</small></div>' +
            '<button class="btn btn-secondary" data-tier-remove="' + i + '">Remove</button></div>';
        }).join('') || '<p class="muted">No discount tiers yet.</p>';
        if (typeof icons === 'function') icons();
      }).catch(function (e) { toast(e.message, 'error'); });
    }
    document.getElementById('tierAdd').addEventListener('click', function () {
      var min = Number(document.getElementById('tierMin').value || 0);
      var pct = Number(document.getElementById('tierPct').value || 0);
      authRequest('/billing/discount-tiers').then(function (tiers) {
        tiers.push({ minSims: min, discountPct: pct });
        return authRequest('/billing/discount-tiers', { method: 'PUT', body: JSON.stringify({ tiers: tiers }) });
      }).then(function () { toast('Discount tier saved'); render(); }).catch(function (e) { toast(e.message, 'error'); });
    });
    card.addEventListener('click', function (event) {
      var btn = event.target.closest('[data-tier-remove]');
      if (!btn) return;
      var idx = Number(btn.dataset.tierRemove);
      authRequest('/billing/discount-tiers').then(function (tiers) {
        tiers.splice(idx, 1);
        return authRequest('/billing/discount-tiers', { method: 'PUT', body: JSON.stringify({ tiers: tiers }) });
      }).then(function () { render(); }).catch(function (e) { toast(e.message, 'error'); });
    });
    render();
  }


  var logsSearch = document.getElementById('logsSearch');
  if (logsSearch) { logsSearch.addEventListener('input', renderLogs); }
  var logsFilter = document.getElementById('logsActionFilter');
  if (logsFilter) { logsFilter.addEventListener('change', renderLogs); }
  var logsRefresh = document.getElementById('logsRefresh');
  if (logsRefresh) { logsRefresh.addEventListener('click', loadLogs); }
  var trashRefresh = document.getElementById('trashRefresh');
  if (trashRefresh) { trashRefresh.addEventListener('click', loadTrash); }

  var logsExport = document.getElementById('logsExport');
  if (logsExport) {
    logsExport.addEventListener('click', function () {
      var rows = [['When', 'Actor', 'Action', 'Target', 'Detail']].concat(logRows.map(function (row) {
        return [fmtDate(row.createdAt), row.actor, row.action, row.target || '', row.detail || ''];
      }));
      var csv = rows.map(function (row) {
        return row.map(function (cell) { return '"' + String(cell).replace(/"/g, '""') + '"'; }).join(',');
      }).join('\n');
      var link = document.createElement('a');
      link.href = URL.createObjectURL(new Blob([csv], { type: 'text/csv' }));
      link.download = 'activity-log.csv';
      link.click();
    });
  }

  if (typeof navigate === 'function') {
    var baseNavigate = navigate;
    navigate = function (view) {
      var result = baseNavigate.apply(this, arguments);
      if (view === 'logs') { loadLogs(); }
      if (view === 'trash') { loadTrash(); }
      if (view === 'settings') { buildEmailCard(); buildTwoFactorCard(); buildPrepaidPolicyCard(); buildCustomerFeesCard(); buildPlatformPricingCard(); buildDiscountTiersCard(); }
      return result;
    };
  }

  var active = document.querySelector('.view.active');
  if (active && active.id === 'settings') { buildEmailCard(); buildTwoFactorCard(); buildPrepaidPolicyCard(); buildCustomerFeesCard(); buildPlatformPricingCard(); buildDiscountTiersCard(); }
})();
