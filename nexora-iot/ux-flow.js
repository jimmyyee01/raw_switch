/* Flow polish: real Settings saving, one place for team info, and a working
   Help sheet behind "Help & support" / "Contact us". */
(function () {
  'use strict';

  var NOTIF_KEY = 'rawSwitchNotificationPrefs';

  /* ---------------- Settings: actually save ---------------- */
  var form = document.getElementById('profileForm');
  var saveBtn = document.getElementById('saveSettings');

  if (form && saveBtn) {
    var freshSave = saveBtn.cloneNode(true);
    saveBtn.parentNode.replaceChild(freshSave, saveBtn);
    freshSave.addEventListener('click', function () {
      if (!form.reportValidity()) { return; }
      var inputs = form.querySelectorAll('input');
      var first = (inputs[0].value || '').trim();
      var last = (inputs[1].value || '').trim();
      var email = (inputs[2].value || '').trim();
      var name = (first + ' ' + last).trim();
      if (!name) { toast('Enter your name first'); return; }
      if (typeof currentUser === 'undefined' || !currentUser) { return; }

      freshSave.disabled = true;
      updateUser(currentUser.id, { name: name, email: email }).then(function (ok) {
        if (ok) {
          document.querySelectorAll('.profile-banner strong').forEach(function (el) { el.textContent = name; });
          var sideName = document.getElementById('sidebar-profile-name');
          if (sideName) { sideName.textContent = name; }
          toast('Settings saved');
        }
      }).finally(function () {
        freshSave.disabled = false;
      });
    });
  }

  function fillProfile() {
    if (!form || typeof currentUser === 'undefined' || !currentUser) { return; }
    var inputs = form.querySelectorAll('input');
    var parts = String(currentUser.name || '').split(' ');
    if (inputs[0]) { inputs[0].value = parts.shift() || ''; }
    if (inputs[1]) { inputs[1].value = parts.join(' '); }
    if (inputs[2]) { inputs[2].value = currentUser.email || ''; }
    if (inputs[3]) { inputs[3].value = currentUser.role || ''; }
  }

  function wireNotificationPrefs() {
    var card = null;
    var cards = document.querySelectorAll('#settings .card');
    for (var i = 0; i < cards.length; i += 1) {
      var h = cards[i].querySelector('h2');
      if (h && h.textContent.trim() === 'Notifications') { card = cards[i]; break; }
    }
    if (!card) { return; }

    var saved = {};
    try { saved = JSON.parse(localStorage.getItem(NOTIF_KEY) || '{}'); } catch (error) { saved = {}; }

    var rows = card.querySelectorAll('.setting-row');
    for (var r = 0; r < rows.length; r += 1) {
      var label = rows[r].querySelector('strong');
      var box = rows[r].querySelector('input[type="checkbox"]');
      if (!box) { continue; }
      var key = label ? label.textContent.trim() : 'pref' + r;
      box.dataset.pref = key;
      if (Object.prototype.hasOwnProperty.call(saved, key)) { box.checked = !!saved[key]; }
      if (box.dataset.wired) { continue; }
      box.dataset.wired = '1';
      box.addEventListener('change', function () {
        var state = {};
        var boxes = card.querySelectorAll('input[data-pref]');
        for (var b = 0; b < boxes.length; b += 1) { state[boxes[b].dataset.pref] = boxes[b].checked; }
        try { localStorage.setItem(NOTIF_KEY, JSON.stringify(state)); } catch (error) { /* ignore */ }
      });
    }
  }

  /* ---------------- one place for team numbers ---------------- */
  function renderTeamMeta() {
    var el = document.getElementById('teamAccessMeta');
    if (!el) { return; }
    var people = (typeof users !== 'undefined' && users) ? users : [];
    var admins = people.filter(function (user) { return user.role === 'Administrator'; }).length;
    var roles = (typeof platformRoles !== 'undefined') ? platformRoles.length : 0;
    el.innerHTML = '<span><strong>' + people.length + '</strong> users</span>'
      + '<span><strong>' + admins + '</strong> administrators</span>'
      + '<span><strong>' + roles + '</strong> roles</span>'
      + '<span class="muted">Permissions are edited on the User profiles page.</span>';
  }

  /* ---------------- help sheet ---------------- */
  function closeHelp() {
    var sheet = document.getElementById('helpSheet');
    if (sheet) { sheet.classList.remove('open'); }
  }

  function helpSheet() {
    var existing = document.getElementById('helpSheet');
    if (existing) { return existing; }

    var sheet = document.createElement('div');
    sheet.id = 'helpSheet';
    sheet.className = 'help-sheet';
    sheet.innerHTML = [
      '<div class="help-card" role="dialog" aria-modal="true" aria-labelledby="helpTitle">',
      '  <div class="help-head"><div><h2 id="helpTitle">Help &amp; support</h2>',
      '  <p>Shortcuts to the parts of the platform you are most likely to need.</p></div>',
      '  <button class="icon-btn" id="helpClose" aria-label="Close help"><i data-lucide="x"></i></button></div>',
      '  <div class="help-grid">',
      '    <a class="help-link" href="mailto:support@raw-switch.io?subject=RAW-Switch%20support">',
      '      <i data-lucide="mail"></i><span><strong>Email support</strong>',
      '      <small>support@raw-switch.io · replies within one business day</small></span></a>',
      '    <button type="button" class="help-link" data-view="sim-studio">',
      '      <i data-lucide="sparkles"></i><span><strong>Provision a SIM</strong>',
      '      <small>IoT SIM, iSIM or eSIM in four steps</small></span></button>',
      '    <button type="button" class="help-link" data-view="network-apis">',
      '      <i data-lucide="plug-zap"></i><span><strong>Connect a provider</strong>',
      '      <small>Local or international network APIs</small></span></button>',
      '    <button type="button" class="help-link" data-view="marketplace">',
      '      <i data-lucide="globe"></i><span><strong>Manage carriers</strong>',
      '      <small>Coverage and linked providers</small></span></button>',
      '  </div>',
      '  <p class="help-foot">RAW-Switch · SIM / iSIM / eSIM connectivity operations</p>',
      '</div>'
    ].join('\n');
    document.body.appendChild(sheet);

    sheet.addEventListener('click', function (event) {
      if (event.target === sheet) { closeHelp(); }
      var link = event.target.closest ? event.target.closest('[data-view]') : null;
      if (link) { closeHelp(); }
    });
    sheet.querySelector('#helpClose').addEventListener('click', closeHelp);
    if (typeof icons === 'function') { icons(); }
    return sheet;
  }

  function openHelp() {
    helpSheet().classList.add('open');
  }

  ['helpBtn', 'contactSupport'].forEach(function (id) {
    var button = document.getElementById(id);
    if (!button) { return; }
    var clone = button.cloneNode(true);
    button.parentNode.replaceChild(clone, button);
    clone.addEventListener('click', function (event) {
      event.preventDefault();
      openHelp();
    });
  });

  document.addEventListener('keydown', function (event) {
    if (event.key === 'Escape') { closeHelp(); }
  });

  /* Refresh the Settings view whenever it is opened. */
  if (typeof navigate === 'function') {
    var baseNavigate = navigate;
    navigate = function (view) {
      var result = baseNavigate.apply(this, arguments);
      if (view === 'settings') {
        fillProfile();
        renderTeamMeta();
        wireNotificationPrefs();
      }
      return result;
    };
  }

  var active = document.querySelector('.view.active');
  if (active && active.id === 'settings') {
    fillProfile();
    renderTeamMeta();
    wireNotificationPrefs();
  }
  renderTeamMeta();
})();
