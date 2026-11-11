/* SIM studio - guided IoT SIM / i-SIM / eSIM provisioning and lifecycle control.
   Loaded after app.js and reuses its helpers: authRequest, toast, icons,
   navigate, loadActualInventory, currentUser, customers, users, sims. */
(function () {
  'use strict';

  const API = (typeof AUTH_API === 'string') ? AUTH_API : 'http://45.159.228.52:8000';
  const state = { step: 1, type: null, catalog: null, summary: null, results: [], batch: null, draft: {}, drawer: null, busy: false };

  const $ = function (selector, root) { return (root || document).querySelector(selector); };
  const $$ = function (selector, root) { return Array.prototype.slice.call((root || document).querySelectorAll(selector)); };

  /* ---------------------------------------------------------------- helpers */

  function api(path, options) {
    if (typeof authRequest === 'function') return authRequest(path, options);
    const config = Object.assign({ headers: { 'Content-Type': 'application/json' } }, options || {});
    return fetch(API + path, config).then(function (response) {
      return response.json().catch(function () { return {}; }).then(function (data) {
        if (!response.ok) throw new Error(data.detail || 'Request failed (' + response.status + ')');
        return data;
      });
    });
  }

  function notify(message, tone) {
    if (typeof toast === 'function') toast(message, tone);
  }

  function paintIcons() {
    if (typeof icons === 'function') icons();
  }

  function escapeHtml(value) {
    return String(value === null || value === undefined ? '' : value).replace(/[&<>"']/g, function (character) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[character];
    });
  }

  function copyValue(value, label) {
    const text = String(value || '');
    if (!text) return;
    const done = function () { notify((label || 'Value') + ' copied'); };
    const failed = function () { notify('Copy failed - select the text and copy it manually', 'error'); };
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(text).then(done).catch(failed);
      return;
    }
    try {
      const field = document.createElement('textarea');
      field.value = text;
      field.setAttribute('readonly', 'readonly');
      field.style.position = 'fixed';
      field.style.opacity = '0';
      document.body.appendChild(field);
      field.select();
      document.execCommand('copy');
      field.remove();
      done();
    } catch (error) {
      failed();
    }
  }

  function downloadText(filename, text, mime) {
    const blob = new Blob([text], { type: mime || 'text/plain;charset=utf-8' });
    const link = document.createElement('a');
    link.href = URL.createObjectURL(blob);
    link.download = filename;
    link.click();
    URL.revokeObjectURL(link.href);
  }

  function currentRole() {
    return (typeof currentUser !== 'undefined' && currentUser) ? currentUser.role : null;
  }

  function canUseStudio() {
    if (typeof currentUser === 'undefined' || !currentUser) return false;
    if (currentRole() !== 'Customer') return true;
    return typeof hasCustomerPermission === 'function' ? hasCustomerPermission('sims') : true;
  }

  function refreshAccess() {
    const navItem = $('[data-view="sim-studio"]');
    if (navItem) navItem.style.display = canUseStudio() ? '' : 'none';
    const openButton = $('[data-studio-open]');
    if (openButton) openButton.style.display = canUseStudio() ? '' : 'none';
  }

  function familyCount(key) {
    const families = (state.summary && state.summary.families) ? state.summary.families : {};
    return Number(families[key] || 0) + ' in fleet';
  }

  function badge(status) {
    const raw = String(status || 'Unknown');
    const tone = raw.toLowerCase();
    const label = raw.charAt(0).toUpperCase() + raw.slice(1);
    return '<span class="badge ' + escapeHtml(tone) + '">' + escapeHtml(label) + '</span>';
  }

  function familyLabel(sim) {
    if (sim.isEsim || sim.simTypeKey === 'e-sim') return 'eSIM';
    if (sim.isSim || sim.simTypeKey === 'i-sim') return 'i-SIM';
    return 'IoT SIM';
  }

  /* ------------------------------------------------------------ step engine */

  function setStep(step) {
    state.step = step;
    $$('#studioSteps li').forEach(function (item) {
      const index = Number(item.dataset.step);
      item.classList.toggle('current', index === state.step);
      item.classList.toggle('done', index < state.step);
    });
    [1, 2, 3, 4].forEach(function (index) {
      const panel = $('#studioStep' + index);
      if (panel) panel.hidden = index !== state.step;
    });
    if (step === 2) renderForm();
    if (step === 3) renderReview();
    if (step === 4) renderResults();
    const view = $('#sim-studio');
    if (view && view.classList.contains('active')) view.scrollIntoView({ block: 'start', behavior: 'smooth' });
  }

  function resetStudio() {
    state.results = [];
    state.batch = null;
    state.draft = {};
    state.type = null;
    renderCatalog();
    setStep(1);
    loadRecent();
  }

  function renderCatalog() {
    const host = $('#studioTypeGrid');
    if (!host) return;
    if (!state.catalog || !state.catalog.types) {
      host.innerHTML = '<div class="studio-empty">Connecting to the SIM platform API...</div>';
      return;
    }
    host.innerHTML = state.catalog.types.map(function (type) {
      const selected = state.type === type.key ? ' selected' : '';
      return '<button type="button" class="studio-type-card' + selected + '" data-studio-type="' + escapeHtml(type.key) + '">' +
        '<span class="studio-type-icon"><i data-lucide="' + escapeHtml(type.icon) + '"></i></span>' +
        '<h3>' + escapeHtml(type.label) + '</h3>' +
        '<p class="studio-type-tag">' + escapeHtml(type.tagline) + '</p>' +
        '<ul>' + (type.highlights || []).map(function (item) { return '<li>' + escapeHtml(item) + '</li>'; }).join('') + '</ul>' +
        '<span class="studio-type-foot"><span>' + escapeHtml(familyCount(type.key)) + '</span>' +
        '<span class="studio-pick"><i data-lucide="arrow-right"></i>Select</span></span>' +
        '</button>';
    }).join('');
    paintIcons();
  }

  function activeType() {
    if (!state.catalog || !state.catalog.types) return null;
    for (let index = 0; index < state.catalog.types.length; index += 1) {
      if (state.catalog.types[index].key === state.type) return state.catalog.types[index];
    }
    return null;
  }

  function customerAccounts() {
    // The studio only offers customers from the backend registry (/sims/catalog and
    // /customers). Browser-cached profile names are deliberately ignored so a stale
    // localStorage copy can never offer a tenant the server would reject.
    const accounts = [];
    const seen = {};
    const add = function (name, email) {
      const clean = String(name || '').trim();
      if (!clean || clean === 'Unassigned' || seen[clean]) return;
      seen[clean] = true;
      accounts.push({ name: clean, contactEmail: String(email || '') });
    };
    const registry = (state.catalog && Array.isArray(state.catalog.customers)) ? state.catalog.customers : [];
    registry.forEach(function (account) {
      // Suspended accounts stay in the registry but cannot be issued new SIMs
      // (the API answers 409), so they are not offered in the dropdown.
      if (account && String(account.status || '').toLowerCase() === 'suspended') return;
      add(account && account.name, account && account.contactEmail);
    });
    const locked = lockedCustomer();
    if (locked) add(locked, '');
    if (!accounts.length && typeof currentUser !== 'undefined' && currentUser && currentUser.customerName) {
      add(currentUser.customerName, '');
    }
    return accounts.sort(function (left, right) { return left.name.localeCompare(right.name); });
  }

  function customerNames() {
    return customerAccounts().map(function (account) { return account.name; });
  }

  function customerContact(name) {
    const target = String(name || '').trim();
    const match = customerAccounts().filter(function (account) { return account.name === target; })[0];
    return match ? match.contactEmail : '';
  }

  function sizeLabel(megabytes) {
    const value = Number(megabytes || 0);
    if (!value) return '0 MB';
    if (value >= 1024) {
      const gb = value / 1024;
      return (gb >= 10 ? Math.round(gb) : Math.round(gb * 10) / 10) + ' GB';
    }
    return Math.round(value) + ' MB';
  }

  function planMeta(name) {
    const plans = (state.catalog && Array.isArray(state.catalog.plans)) ? state.catalog.plans : [];
    for (let index = 0; index < plans.length; index += 1) {
      const plan = plans[index];
      if (typeof plan === 'string' && plan === name) return { name: plan };
      if (plan && plan.name === name) return plan;
    }
    return null;
  }

  function planOptions() {
    const plans = (state.catalog && Array.isArray(state.catalog.plans)) ? state.catalog.plans : [];
    if (!plans.length) return ['500 MB / month'];
    return plans.map(function (plan) {
      const name = typeof plan === 'string' ? plan : plan.name;
      const price = (plan && typeof plan === 'object' && Number(plan.priceUsd))
        ? ' - $' + Number(plan.priceUsd).toFixed(2) + '/mo' : '';
      const allowance = (plan && typeof plan === 'object' && Number(plan.dataMb))
        ? ' (' + sizeLabel(plan.dataMb) + ')' : '';
      return { value: name, label: name + allowance + price };
    });
  }

  function usageText(sim) {
    const used = Number(sim.usageMb || 0);
    const limit = Number(sim.dataLimitMb || 0);
    if (!limit) return 'Pooled data - ' + sizeLabel(used) + ' used';
    const text = sizeLabel(used) + ' of ' + sizeLabel(limit);
    return used > limit ? text + ' - over allowance' : text;
  }

  function lockedCustomer() {
    if (typeof currentUser === 'undefined' || !currentUser || currentUser.role !== 'Customer') return '';
    if (typeof currentCustomer === 'function') {
      const account = currentCustomer();
      if (account && account.name) return account.name;
    }
    return currentUser.customerName || '';
  }

  function selectField(name, label, options, selected, extra) {
    return '<label>' + escapeHtml(label) + '<select name="' + name + '"' + (extra || '') + '>' +
      options.map(function (option) {
        const value = typeof option === 'string' ? option : option.value;
        const text = typeof option === 'string' ? option : option.label;
        return '<option value="' + escapeHtml(value) + '"' + (String(value) === String(selected) ? ' selected' : '') + '>' + escapeHtml(text) + '</option>';
      }).join('') + '</select></label>';
  }


  function renderForm() {
    const host = $('#studioFormHost');
    const type = activeType();
    if (!host || !type || !state.catalog) return;
    const draft = state.draft;
    const locked = lockedCustomer();
    const customerOptions = customerNames();
    if (!customerOptions.length) customerOptions.push('Unassigned');
    const plans = planOptions();
    const techs = state.catalog.networkTechs || ['4G'];
    const regions = state.catalog.regions || ['Global'];
    const options = type.options || [];

    let typeFields = '';
    if (options.indexOf('formFactor') !== -1) {
      typeFields += selectField('formFactor', 'Card form factor', state.catalog.formFactors || ['4FF (nano)'], draft.formFactor || '4FF (nano)');
    }
    if (options.indexOf('protocol') !== -1) {
      typeFields += selectField('protocol', 'Download standard', state.catalog.protocols || ['SGP.32'], draft.protocol || 'SGP.32');
    }
    if (options.indexOf('eid') !== -1) {
      typeFields += '<label class="full">eUICC EID <span class="optional">Optional</span>' +
        '<input name="eid" inputmode="numeric" maxlength="32" placeholder="Leave empty to auto-generate" value="' + escapeHtml(draft.eid || '') + '"></label>';
    }

    host.innerHTML = '<form id="studioForm" class="studio-form">' +
      '<div class="form-grid">' +
        '<label class="full">Device name<input name="device" required maxlength="60" placeholder="e.g. Fleet tracker 042" value="' + escapeHtml(draft.device || '') + '"></label>' +
        (locked
          ? '<label>Customer<input name="customer" value="' + escapeHtml(locked) + '" readonly></label>'
          : selectField('customer', 'Customer', customerOptions, draft.customer || customerOptions[0])) +
        selectField('networkTech', 'Network technology', techs, draft.networkTech || '4G') +
        selectField('plan', 'Data plan', plans, draft.plan || (typeof plans[0] === 'string' ? plans[0] : plans[0].value)) +
        selectField('region', 'Home region', regions, draft.region || 'Global') +
        '<label>How many cards?<input name="quantity" type="number" min="1" max="' + Number(state.catalog.maxBatch || 25) + '" step="1" value="' + escapeHtml(draft.quantity || 1) + '"></label>' +
        '<label>Mobile number <span class="optional">Optional</span><input name="msisdn" inputmode="tel" placeholder="+8821600000000" value="' + escapeHtml(draft.msisdn || '') + '"></label>' +
        typeFields +
        '<label class="toggle-field full"><span><strong>Enable global roaming</strong><small>Connect to partner networks worldwide</small></span>' +
        '<span class="switch"><input name="roaming" type="checkbox"' + (draft.roaming === false ? '' : ' checked') + '><span></span></span></label>' +
      '</div>' +
      '<div class="studio-hint"><i data-lucide="info"></i><span>' + escapeHtml(type.delivery) + '</span></div>' +
      '<div class="studio-actions">' +
        '<button type="button" class="btn btn-secondary" data-studio-back><i data-lucide="arrow-left"></i>Back</button>' +
        '<button type="submit" class="btn btn-primary"><i data-lucide="arrow-right"></i>Review ' + escapeHtml(type.label) + '</button>' +
      '</div>' +
      '</form>';
    paintIcons();
  }

  function fieldValue(form, data, name) {
    // Read straight from the control first: some Chromium builds drop a
    // <select> from FormData when the surrounding label markup is rewritten,
    // which used to leave the customer blank and block provisioning.
    const control = (form && form.elements) ? form.elements.namedItem(name) : null;
    if (control && typeof control.value === 'string' && control.value) return control.value;
    const raw = data.get(name);
    return (raw === null || raw === undefined) ? '' : String(raw);
  }

  function draftFromForm(form) {
    const data = new FormData(form);
    const maxBatch = Number(state.catalog && state.catalog.maxBatch ? state.catalog.maxBatch : 25);
    const quantity = Math.max(1, Math.min(maxBatch, Number(fieldValue(form, data, 'quantity') || 1)));
    const roaming = form.elements.namedItem('roaming');
    return {
      device: String(fieldValue(form, data, 'device')).trim(),
      customer: String(fieldValue(form, data, 'customer')).trim(),
      networkTech: String(fieldValue(form, data, 'networkTech') || '4G'),
      plan: String(fieldValue(form, data, 'plan') || '500 MB / month'),
      region: String(fieldValue(form, data, 'region') || 'Global'),
      quantity: quantity,
      msisdn: String(fieldValue(form, data, 'msisdn')).trim(),
      formFactor: String(fieldValue(form, data, 'formFactor')).trim(),
      protocol: String(fieldValue(form, data, 'protocol')).trim(),
      eid: String(fieldValue(form, data, 'eid')).trim(),
      roaming: roaming ? !!roaming.checked : data.get('roaming') === 'on',
    };
  }


  function renderReview() {
    const host = $('#studioReview');
    const type = activeType();
    if (!host || !type) return;
    const draft = state.draft;
    const quantity = Number(draft.quantity || 1);
    const plan = planMeta(draft.plan);
    const contact = customerContact(draft.customer);
    const rows = [
      ['SIM type', type.label + ' - ' + type.format],
      ['Device name', draft.device || '-'],
      ['Customer', draft.customer || '-'],
      ['Network technology', draft.networkTech || '4G'],
      ['Data plan', draft.plan || '-'],
      ['Home region', draft.region || 'Global'],
      ['Cards to provision', String(quantity)],
      ['Global roaming', draft.roaming === false ? 'Disabled' : 'Enabled'],
    ];
    if (plan && Number(plan.dataMb)) rows.push(['Plan allowance', sizeLabel(plan.dataMb) + ' per card']);
    if (plan && Number(plan.priceUsd)) rows.push(['Plan price', '$' + Number(plan.priceUsd).toFixed(2) + ' / month per card']);
    if (plan && plan.description) rows.push(['Plan covers', plan.description]);
    if (contact) rows.push(['Customer contact', contact]);
    if (draft.formFactor) rows.push(['Card form factor', draft.formFactor]);
    if (draft.protocol) rows.push(['Download standard', draft.protocol]);
    if (draft.eid) rows.push(['eUICC EID', draft.eid]);
    if (draft.msisdn) rows.push(['Mobile number', draft.msisdn]);

    const generated = type.key === 'e-sim'
      ? ', EID, SM-DP+ address and the GSMA activation code'
      : type.key === 'i-sim'
        ? ', EID and the device pairing token'
        : ', APN, PIN and PUK';

    host.innerHTML = '<div class="studio-review">' + rows.map(function (row) {
      return '<div><span>' + escapeHtml(row[0]) + '</span><strong>' + escapeHtml(row[1]) + '</strong></div>';
    }).join('') + '</div>' +
      '<div class="studio-hint"><i data-lucide="shield-check"></i><span>ICCID, IMSI' + escapeHtml(generated) +
      ' are generated by the platform as soon as you confirm.</span></div>' +
      '<div class="studio-actions">' +
        '<button type="button" class="btn btn-secondary" data-studio-back><i data-lucide="arrow-left"></i>Back</button>' +
        '<button type="button" class="btn btn-primary" data-studio-provision' + (state.busy ? ' disabled' : '') + '>' +
        '<i data-lucide="zap"></i>' + (state.busy ? 'Provisioning...'
          : 'Provision ' + escapeHtml(String(quantity)) + ' ' + escapeHtml(type.label) + (quantity > 1 ? ' cards' : ' card')) +
        '</button>' +
      '</div>';
    paintIcons();
  }

  /* ------------------------------------------------------------ provisioning */

  function provision() {
    if (state.busy || !state.type) return;
    const draft = state.draft;
    if (!draft.device) { notify('Enter a device name first', 'error'); setStep(2); return; }
    if (!draft.customer) { notify('Select the customer that owns these SIMs', 'error'); setStep(2); return; }
    state.busy = true;
    renderReview();
    const payload = {
      simType: state.type,
      device: draft.device,
      customer: draft.customer,
      networkTech: draft.networkTech,
      plan: draft.plan,
      region: draft.region,
      quantity: draft.quantity,
      roamingEnabled: draft.roaming !== false,
      provisionedBy: (typeof currentUser !== 'undefined' && currentUser) ? currentUser.email : 'dashboard',
    };
    if (draft.msisdn) payload.msisdn = draft.msisdn;
    if (draft.formFactor) payload.formFactor = draft.formFactor;
    if (draft.protocol) payload.protocol = draft.protocol;
    if (draft.eid) payload.eid = draft.eid;

    api('/sims/provision', { method: 'POST', body: JSON.stringify(payload) }).then(function (result) {
      state.results = result.sims || [];
      state.batch = result.batchId || null;
      state.busy = false;
      setStep(4);
      notify(result.created + ' ' + (result.label || 'SIM') + ' provisioned and activated');
      refreshInventory();
      loadRecent();
      loadSummary();
    }).catch(function (error) {
      state.busy = false;
      renderReview();
      notify(error.message || 'Provisioning failed', 'error');
    });
  }

  function refreshInventory() {
    if (typeof loadActualInventory !== 'function') return;
    Promise.resolve(loadActualInventory()).then(function () {
      if (typeof renderSims === 'function') renderSims();
      if (typeof renderSimManagementStats === 'function') renderSimManagementStats();
    }).catch(function () { /* the inventory view refreshes on next navigation */ });
  }


  /* ---------------------------------------------------------------- results */

  function identRows(sim) {
    const rows = [
      ['ICCID', sim.iccid],
      ['IMSI', sim.imsi],
      ['MSISDN', sim.msisdn],
      ['APN', sim.apn],
      ['Network tech', sim.networkTech],
      ['Data plan', sim.dataPlan],
      ['Home region', sim.location],
    ];
    if (sim.planPriceUsd) rows.push(['Plan price', '$' + Number(sim.planPriceUsd).toFixed(2) + ' / month']);
    if (sim.dataLimitMb) rows.push(['Plan allowance', sizeLabel(sim.dataLimitMb)]);
    if (sim.isEsim) {
      rows.push(['SM-DP+ address', sim.smdpAddress]);
      rows.push(['Matching ID', sim.matchingId]);
      rows.push(['eUICC EID', sim.eid]);
    } else if (sim.isSim) {
      rows.push(['eUICC EID', sim.eid]);
      rows.push(['Pairing token', sim.activationToken]);
    } else {
      rows.push(['Form factor', sim.formFactor]);
      rows.push(['PIN 1', sim.pin1]);
      rows.push(['PUK 1', sim.puk1]);
    }
    return rows.filter(function (row) { return row[1]; }).map(function (row) {
      const value = String(row[1]);
      return '<div><span>' + escapeHtml(row[0]) + '</span><strong>' + escapeHtml(value) + '</strong>' +
        '<button type="button" class="sim-copy" data-copy="' + escapeHtml(value) + '">Copy</button></div>';
    }).join('');
  }

  function typeSpecificBlock(sim) {
    if (sim.isEsim) {
      return '<div class="studio-qr">' +
        '<img data-qr="1" alt="eSIM installation QR code" src="' + escapeHtml(API + sim.qrUrl) + '?v=' + Date.now() + '">' +
        '<div class="studio-qr-info">' +
          '<strong>Scan to install the eSIM</strong>' +
          '<small>Open the device eSIM settings, choose "Add eSIM" and scan this code. The activation code below does exactly the same job when typed by hand.</small>' +
          '<code class="sim-code">' + escapeHtml(sim.activationCode) + '</code>' +
          '<div class="studio-actions" style="justify-content:flex-start;margin-top:8px">' +
            '<button type="button" class="btn btn-secondary" data-copy="' + escapeHtml(sim.activationCode) + '">Copy activation code</button>' +
            '<button type="button" class="btn btn-secondary" data-download-qr="' + escapeHtml(sim.iccid) + '">Download QR</button>' +
          '</div>' +
        '</div></div>' +
        '<ol class="studio-install"><li><b>Open</b> Settings, then Mobile network, then Add eSIM</li>' +
        '<li><b>Scan</b> the QR code or type the activation code</li>' +
        '<li><b>Confirm</b> the profile - the SIM connects automatically</li></ol>';
    }
    if (sim.isSim) {
      return '<div class="studio-qr"><div class="studio-qr-info">' +
        '<strong>Pair the device with this token</strong>' +
        '<small>The i-SIM profile is already installed inside the device chipset. Register the token in your device-manufacturer console to bind the profile.</small>' +
        '<code class="sim-code">' + escapeHtml(sim.activationToken || '') + '</code>' +
        '<div class="studio-actions" style="justify-content:flex-start;margin-top:8px">' +
          '<button type="button" class="btn btn-secondary" data-copy="' + escapeHtml(sim.activationToken || '') + '">Copy pairing token</button>' +
        '</div></div></div>' +
        '<ol class="studio-install"><li><b>Pair</b> the device using the token above</li>' +
        '<li><b>Power on</b> the device - the profile is embedded in the chipset</li>' +
        '<li><b>Verify</b> live data usage in the portal</li></ol>';
    }
    return '<ol class="studio-install"><li><b>Insert</b> the ' + escapeHtml(sim.formFactor || '4FF (nano)') + ' card into the device</li>' +
      '<li><b>Enter</b> PIN 1 when the modem requests it</li>' +
      '<li><b>Check</b> that the device registers on the network in the portal</li></ol>';
  }

  function resultCard(sim) {
    const created = sim.createdAt ? new Date(sim.createdAt).toLocaleString() : 'just now';
    return '<article class="studio-result" data-result="' + escapeHtml(sim.iccid) + '">' +
      '<div class="studio-result-head"><div><h3>' + escapeHtml(sim.device) + '</h3>' +
      '<p>' + escapeHtml(familyLabel(sim)) + ' - provisioned ' + escapeHtml(created) + ' - ' + escapeHtml(sim.customer) + '</p></div>' +
      '<div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap">' + badge(sim.status) +
      '<span class="studio-chip">' + escapeHtml(sim.dataPlan || 'Plan') + '</span></div></div>' +
      '<div class="sim-ident">' + identRows(sim) + '</div>' +
      typeSpecificBlock(sim) +
      '<div class="studio-actions" style="justify-content:flex-start">' +
        '<button type="button" class="btn btn-secondary" data-download-card="' + escapeHtml(sim.iccid) + '"><i data-lucide="file-down"></i>Activation file</button>' +
        '<button type="button" class="btn btn-secondary" data-open-sim="' + escapeHtml(sim.iccid) + '"><i data-lucide="settings-2"></i>Manage SIM</button>' +
        '<button type="button" class="btn btn-primary" data-studio-restart><i data-lucide="plus"></i>Provision another</button>' +
      '</div></article>';
  }

  function renderResults() {
    const host = $('#studioResults');
    if (!host) return;
    if (!state.results.length) {
      host.innerHTML = '<div class="studio-empty">No SIMs provisioned yet.</div>';
      return;
    }
    const count = state.results.length;
    host.innerHTML = '<div class="studio-section-head"><h2>' + count + ' SIM' + (count > 1 ? 's' : '') + ' ready to use</h2>' +
      '<p>Batch ' + escapeHtml(state.batch || '-') + ' - the same activation details are stored against every SIM in the inventory.</p></div>' +
      state.results.map(resultCard).join('');
    paintIcons();
    $$('#studioResults img[data-qr]').forEach(function (image) {
      image.addEventListener('error', function () {
        const note = document.createElement('div');
        note.className = 'studio-empty';
        note.textContent = 'QR image unavailable - use the activation code instead.';
        if (image.parentNode) image.parentNode.replaceChild(note, image);
      });
    });
  }


  /* ------------------------------------------------------- downloads & lookups */

  function findSim(iccid) {
    const pool = state.results.concat(state.drawer ? [state.drawer] : []);
    for (let index = 0; index < pool.length; index += 1) {
      if (pool[index] && String(pool[index].iccid) === String(iccid)) return pool[index];
    }
    return null;
  }

  function activationText(sim) {
    const lines = [
      'RAW-Switch - SIM activation details',
      '===================================',
      'Card type:   ' + familyLabel(sim),
      'Device:      ' + (sim.device || '-'),
      'Customer:    ' + (sim.customer || '-'),
      'Status:      ' + (sim.status || '-'),
      'ICCID:       ' + (sim.iccid || '-'),
      'IMSI:        ' + (sim.imsi || '-'),
      'MSISDN:      ' + (sim.msisdn || '-'),
      'APN:         ' + (sim.apn || '-'),
      'Plan:        ' + (sim.dataPlan || '-'),
      'Home region: ' + (sim.location || '-'),
      'Network:     ' + (sim.networkTech || '-'),
    ];
    if (sim.isEsim) {
      lines.push('eUICC EID:   ' + (sim.eid || '-'));
      lines.push('SM-DP+:      ' + (sim.smdpAddress || '-'));
      lines.push('Matching ID: ' + (sim.matchingId || '-'));
      lines.push('Activation:  ' + (sim.activationCode || '-'));
    } else if (sim.isSim) {
      lines.push('eUICC EID:   ' + (sim.eid || '-'));
      lines.push('Pair token:  ' + (sim.activationToken || '-'));
    } else {
      lines.push('Form factor: ' + (sim.formFactor || '-'));
      lines.push('PIN 1:       ' + (sim.pin1 || '-'));
      lines.push('PUK 1:       ' + (sim.puk1 || '-'));
    }
    lines.push('');
    lines.push('Issued: ' + new Date().toISOString());
    lines.push('Keep this file safe - it contains credentials for the SIM.');
    return lines.join('\n');
  }

  function downloadCard(iccid) {
    const sim = findSim(iccid);
    if (!sim) { notify('SIM details are still loading', 'error'); return; }
    downloadText('sim-' + sim.iccid + '-activation.txt', activationText(sim));
    notify('Activation file downloaded');
  }

  function downloadQr(iccid) {
    const sim = findSim(iccid);
    if (!sim || !sim.qrUrl) { notify('This SIM has no activation QR code', 'error'); return; }
    fetch(API + sim.qrUrl).then(function (response) {
      if (!response.ok) throw new Error('QR request failed');
      return response.blob();
    }).then(function (blob) {
      const link = document.createElement('a');
      link.href = URL.createObjectURL(blob);
      link.download = 'esim-' + sim.iccid + '.svg';
      link.click();
      URL.revokeObjectURL(link.href);
      notify('QR code downloaded');
    }).catch(function () {
      notify('QR download failed - copy the activation code instead', 'error');
    });
  }

  /* ------------------------------------------------------------ recent list */

  function renderMetrics() {
    const host = $('#studioMetrics');
    const summary = state.summary;
    if (!host || !summary) return;
    const used = Number(summary.usageMb || 0);
    const limit = Number(summary.dataLimitMb || 0);
    const active = (summary.states && summary.states.active) ? Number(summary.states.active) : 0;
    const metrics = [
      ['SIMs in fleet', String(summary.total || 0), active + ' active'],
      ['Customers', String(summary.customers || 0), 'registered accounts'],
      ['Plans', String(summary.plans || 0), 'on the catalogue'],
      ['Data used', sizeLabel(used), limit ? 'of ' + sizeLabel(limit) + ' allowance' : 'pooled billing'],
      ['Monthly recurring', '$' + Number(summary.monthlyRecurringUsd || 0).toFixed(2),
        Number(summary.overLimit || 0) + ' over allowance'],
    ];
    host.innerHTML = metrics.map(function (metric) {
      return '<div class="studio-metric"><span>' + escapeHtml(metric[0]) + '</span><strong>' + escapeHtml(metric[1]) +
        '</strong><small>' + escapeHtml(metric[2]) + '</small></div>';
    }).join('');
  }

  function loadSummary() {
    api('/sims/summary').then(function (summary) {
      state.summary = summary;
      renderMetrics();
      renderCatalog();
    }).catch(function () { /* catalogue counters stay on their last value */ });
  }

  function loadRecent() {
    const host = $('#studioRecentList');
    if (!host) return;
    api('/sims/recent?limit=6').then(renderRecent).catch(function (error) {
      host.innerHTML = '<div class="studio-empty">' + escapeHtml(error.message || 'Unable to load recent SIMs') + '</div>';
    });
  }

  function renderRecent(items) {
    const host = $('#studioRecentList');
    if (!host) return;
    if (!items || !items.length) {
      host.innerHTML = '<div class="studio-empty">No SIMs yet - provision your first IoT SIM, i-SIM or eSIM above.</div>';
      return;
    }
    host.innerHTML = items.map(function (sim) {
      const icon = sim.isEsim ? 'smartphone' : sim.isSim ? 'cpu' : 'credit-card';
      return '<div class="studio-recent-row">' +
        '<span class="studio-type-icon" style="width:30px;height:30px"><i data-lucide="' + icon + '"></i></span>' +
        '<div class="studio-recent-name"><strong>' + escapeHtml(sim.device) + '</strong>' +
        '<small>' + escapeHtml(familyLabel(sim)) + ' - ' + escapeHtml(sim.iccid) + ' - ' + escapeHtml(sim.customer) +
        ' - ' + escapeHtml(usageText(sim)) + '</small></div>' +
        badge(sim.status) +
        '<button type="button" class="btn btn-secondary" data-open-sim="' + escapeHtml(sim.iccid) + '">Open</button>' +
        '</div>';
    }).join('');
    paintIcons();
  }


  /* ----------------------------------------------------------------- drawer */

  function openDrawer(iccid) {
    const backdrop = $('#simDrawerBackdrop');
    const body = $('#simDrawerBody');
    if (!backdrop || !body) return;
    backdrop.hidden = false;
    body.innerHTML = '<p class="muted">Loading SIM details...</p>';
    api('/sims/' + encodeURIComponent(iccid)).then(function (sim) {
      state.drawer = sim;
      renderDrawer(sim);
    }).catch(function (error) {
      body.innerHTML = '<div class="studio-empty">' + escapeHtml(error.message || 'Unable to load this SIM') + '</div>';
    });
  }

  function closeDrawer() {
    const backdrop = $('#simDrawerBackdrop');
    if (backdrop) backdrop.hidden = true;
    state.drawer = null;
  }

  function lifecycleButtons(sim) {
    const status = String(sim.status || '').toLowerCase();
    const buttons = [];
    if (status !== 'terminated') {
      if (status === 'suspended') {
        buttons.push(['resume', 'Resume', 'play']);
      } else {
        if (status !== 'active') buttons.push(['activate', 'Activate', 'power']);
        buttons.push(['suspend', 'Suspend', 'pause']);
      }
      buttons.push([sim.isEsim ? 'reissue' : sim.isSim ? 'reissue' : 'reissue',
        sim.isEsim ? 'New activation code' : sim.isSim ? 'New pairing token' : 'New PIN/PUK', 'refresh-cw']);
      buttons.push(['terminate', 'Terminate', 'trash-2']);
    }
    return buttons.map(function (button) {
      return '<button type="button" class="btn btn-secondary" data-lifecycle="' + escapeHtml(button[0]) + '" data-iccid="' + escapeHtml(sim.iccid) + '">' +
        '<i data-lucide="' + escapeHtml(button[2]) + '"></i>' + escapeHtml(button[1]) + '</button>';
    }).join('');
  }

  function renderDrawer(sim) {
    const body = $('#simDrawerBody');
    const title = $('#simDrawerTitle');
    const subtitle = $('#simDrawerSubtitle');
    if (!body) return;
    if (title) title.textContent = sim.device || 'SIM details';
    if (subtitle) subtitle.textContent = familyLabel(sim) + ' - ' + (sim.iccid || '');
    const used = Number(sim.usageMb || 0);
    const limit = Number(sim.dataLimitMb || 0);
    const percent = limit > 0 ? Math.min(100, (used / limit) * 100) : 0;
    const hasPrice = sim.planPriceUsd !== null && sim.planPriceUsd !== undefined;
    const planLine = hasPrice
      ? '<div class="sim-usage-top"><span>' + escapeHtml(sim.dataPlan || 'Data plan') + '</span><span>$' +
        Number(sim.planPriceUsd).toFixed(2) + ' / month' +
        (Number(sim.planOveragePerGb) ? ' - $' + Number(sim.planOveragePerGb).toFixed(2) + '/GB overage' : '') +
        '</span></div>'
      : '';
    body.innerHTML =
      '<div class="sim-ident">' + identRows(sim) + '</div>' +
      '<div class="sim-usage">' + planLine + '<div class="sim-usage-top"><span>Data used</span><span>' +
      escapeHtml(usageText(sim)) + '</span></div>' +
      '<div class="progress"><i style="width:' + percent.toFixed(0) + '%"></i></div></div>' +
      '<div class="sim-drawer-actions">' + lifecycleButtons(sim) + '</div>' +
      (sim.activationCode || sim.activationToken ? typeSpecificBlock(sim) : '') +
      '<div class="sim-drawer-section"><h4>Lifecycle</h4>' +
      '<div class="studio-empty" style="text-align:left">Status: ' + badge(sim.status) +
      '<br><small>Activated ' + escapeHtml(sim.activatedAt ? new Date(sim.activatedAt).toLocaleString() : 'not yet') +
      (sim.terminatedAt ? ' - terminated ' + escapeHtml(new Date(sim.terminatedAt).toLocaleString()) : '') +
      ' - profile state: ' + escapeHtml(sim.profileState || 'n/a') + '</small></div></div>' +
      '<div class="studio-actions" style="justify-content:flex-start">' +
      '<button type="button" class="btn btn-secondary" data-download-card="' + escapeHtml(sim.iccid) + '"><i data-lucide="file-down"></i>Activation file</button>' +
      '<button type="button" class="btn btn-primary" data-studio-restart><i data-lucide="plus"></i>Provision another</button>' +
      '</div>';
    paintIcons();
    const image = body.querySelector('img[data-qr]');
    if (image) {
      image.addEventListener('error', function () {
        const note = document.createElement('div');
        note.className = 'studio-empty';
        note.textContent = 'QR image unavailable - use the activation code instead.';
        if (image.parentNode) image.parentNode.replaceChild(note, image);
      });
    }
  }

  function runLifecycle(iccid, action) {
    if (action === 'terminate' && !window.confirm('Terminate ' + iccid + '? This permanently revokes the SIM activation.')) return;
    if (action === 'reissue') {
      api('/sims/' + encodeURIComponent(iccid) + '/activation', { method: 'POST' }).then(function (sim) {
        state.drawer = sim;
        renderDrawer(sim);
        notify('New activation credentials issued');
      }).catch(function (error) {
        notify(error.message || 'Unable to re-issue credentials', 'error');
      });
      return;
    }
    api('/sims/' + encodeURIComponent(iccid) + '/lifecycle', { method: 'POST', body: JSON.stringify({ action: action }) })
      .then(function (result) {
        state.drawer = result.sim;
        renderDrawer(result.sim);
        notify(action + ' completed - SIM is now ' + result.status);
        refreshInventory();
        loadRecent();
      }).catch(function (error) {
        notify(error.message || 'Lifecycle action failed', 'error');
      });
  }


  /* -------------------------------------------------------------- bootstrap */

  function refreshCustomerRegistry() {
    // Keeps the customer dropdown in step with the backend registry (new accounts,
    // suspensions) without re-rendering a form the user may already be filling in.
    if (!state.catalog) return;
    api('/customers').then(function (records) {
      if (!Array.isArray(records)) return;
      state.catalog.customers = records
        .filter(function (record) { return record && String(record.status || '').toLowerCase() !== 'suspended'; })
        .map(function (record) {
          return {
            name: record.name,
            contactName: record.contactName,
            contactEmail: record.contactEmail,
            status: record.status,
            sims: record.sims,
            usageMb: record.usageMb,
          };
        });
    }).catch(function () { /* keep the catalogue copy of the registry */ });
  }

  function loadStudio() {
    refreshAccess();
    if (!state.catalog) {
      api('/sims/catalog').then(function (catalog) {
        state.catalog = catalog;
        renderCatalog();
        loadSummary();
      }).catch(function (error) {
        const host = $('#studioTypeGrid');
        if (host) host.innerHTML = '<div class="studio-empty">SIM platform API unreachable: ' + escapeHtml(error.message || 'unknown error') + '</div>';
      });
    } else {
      renderCatalog();
      refreshCustomerRegistry();
    }
    loadSummary();
    loadRecent();
  }

  function openStudio() {
    if (typeof navigate === 'function') navigate('sim-studio');
    loadStudio();
  }

  function handleClick(event) {
    const target = event.target instanceof Element ? event.target : null;
    if (!target) return;

    if (target.closest('[data-view="sim-studio"]')) { loadStudio(); return; }
    if (target.closest('[data-studio-open]')) { openStudio(); return; }
    if (target.closest('#studioInventoryBtn')) { if (typeof navigate === 'function') navigate('sims'); return; }
    if (target.closest('#studioRestartBtn') || target.closest('[data-studio-restart]')) { resetStudio(); return; }
    if (target.closest('[data-studio-provision]')) { provision(); return; }
    if (target.closest('[data-studio-back]')) { setStep(Math.max(1, state.step - 1)); return; }

    const typeCard = target.closest('[data-studio-type]');
    if (typeCard) {
      state.type = typeCard.dataset.studioType;
      renderCatalog();
      setStep(2);
      return;
    }

    const copyButton = target.closest('[data-copy]');
    if (copyButton) { copyValue(copyButton.dataset.copy, 'Value'); return; }

    const cardButton = target.closest('[data-download-card]');
    if (cardButton) { downloadCard(cardButton.dataset.downloadCard); return; }

    const qrButton = target.closest('[data-download-qr]');
    if (qrButton) { downloadQr(qrButton.dataset.downloadQr); return; }

    const openSim = target.closest('[data-open-sim]');
    if (openSim) { openDrawer(openSim.dataset.openSim); return; }

    const rowAction = target.closest('.row-action[data-sim]');
    if (rowAction) {
      const inventory = (typeof sims !== 'undefined' && Array.isArray(sims)) ? sims : [];
      const entry = inventory[Number(rowAction.dataset.sim)];
      if (entry && entry.iccid) openDrawer(entry.iccid);
      return;
    }

    const lifecycleButton = target.closest('[data-lifecycle]');
    if (lifecycleButton) { runLifecycle(lifecycleButton.dataset.iccid, lifecycleButton.dataset.lifecycle); return; }

    if (target.closest('#simDrawerClose')) { closeDrawer(); return; }
    if (target.id === 'simDrawerBackdrop') { closeDrawer(); }
  }

  function hookAppEvents() {
    try {
      const base = window.applyCustomerPermissions;
      if (typeof base === 'function' && !base.__studioWrapped) {
        const wrapped = function () {
          const result = base.apply(this, arguments);
          try { refreshAccess(); } catch (error) { /* access badge is non critical */ }
          return result;
        };
        wrapped.__studioWrapped = true;
        window.applyCustomerPermissions = wrapped;
      }
    } catch (error) { /* keep the original permission behaviour */ }

    document.addEventListener('click', function (event) {
      const target = event.target instanceof Element ? event.target : null;
      if (!target) return;
      if (target.closest('#sidebarLogoutBtn') || target.closest('#logoutMenuItem') || target.closest('#loginForm button')) {
        window.setTimeout(refreshAccess, 150);
      }
    });
  }

  function init() {
    hookAppEvents();
    refreshAccess();
    document.addEventListener('click', handleClick);
    document.addEventListener('submit', function (event) {
      const form = event.target;
      if (!form || form.id !== 'studioForm') return;
      event.preventDefault();
      state.draft = draftFromForm(form);
      state.draft.customer = state.draft.customer || lockedCustomer();
      setStep(3);
    });
    document.addEventListener('keydown', function (event) {
      if (event.key === 'Escape') closeDrawer();
    });
    loadStudio();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();

