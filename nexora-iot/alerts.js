/* Live alerts: replaces the static notification panel with data derived from
   SIM usage, invoices and payments. */
(function () {
  'use strict';

  var button = document.getElementById('notificationBtn');
  var panel = document.getElementById('notificationPanel');
  if (!button || !panel) { return; }

  var state = { count: 0, alerts: [] };

  function escapeHtml(value) {
    return String(value == null ? '' : value).replace(/[&<>"']/g, function (ch) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch];
    });
  }

  function scopeQuery() {
    if (typeof currentUser === 'undefined' || !currentUser) { return ''; }
    var customer = currentUser.role === 'Customer' ? (currentUser.customerName || '') : '';
    return customer ? '?customer=' + encodeURIComponent(customer) : '';
  }

  function paintBadge() {
    var dot = button.querySelector('b');
    if (dot) { dot.style.display = state.count ? '' : 'none'; }
    button.title = state.count ? state.count + ' open alert(s)' : 'No open alerts';
  }

  function paintPanel() {
    var items = state.alerts.map(function (alert) {
      var link = alert.view ? '<span class="alert-go">Open</span>' : '';
      return '<button type="button" class="alert-item severity-' + escapeHtml(alert.severity) + '" data-alert-view="'
        + escapeHtml(alert.view || '') + '">'
        + '<span class="alert-dot"></span>'
        + '<span class="alert-copy"><strong>' + escapeHtml(alert.title) + '</strong>'
        + '<small>' + escapeHtml(alert.detail) + '</small></span>'
        + link
        + '</button>';
    }).join('');

    panel.innerHTML = '<div class="alert-head"><strong>Alerts</strong>'
      + '<span class="alert-count">' + (state.count || 0) + '</span>'
      + '<button id="closeNotifications" aria-label="Close alerts"><i data-lucide="x"></i></button></div>'
      + (state.count ? '<div class="alert-list">' + items + '</div>'
                     : '<p class="alert-empty">You are all caught up - no usage, invoice or payment alerts.</p>');

    var close = panel.querySelector('#closeNotifications');
    if (close) {
      close.addEventListener('click', function () { panel.classList.remove('open'); });
    }
    panel.querySelectorAll('[data-alert-view]').forEach(function (item) {
      item.addEventListener('click', function () {
        var view = item.getAttribute('data-alert-view');
        panel.classList.remove('open');
        if (view && typeof navigate === 'function') { navigate(view); }
      });
    });
    if (typeof icons === 'function') { icons(); }
  }

  function loadAlerts() {
    if (typeof currentUser === 'undefined' || !currentUser) { return; }
    fetch(AUTH_API + '/alerts' + scopeQuery(), { credentials: 'include', headers: authHeaders() })
      .then(function (response) { return response.ok ? response.json() : { count: 0, alerts: [] }; })
      .then(function (data) {
        state = { count: Number(data.count || 0), alerts: data.alerts || [] };
        paintBadge();
        paintPanel();
      })
      .catch(function () { /* alerts are advisory */ });
  }

  if (typeof login === 'function') {
    var baseLogin = login;
    login = function () {
      var result = baseLogin.apply(this, arguments);
      if (result && typeof result.then === 'function') {
        return result.then(function (value) { loadAlerts(); return value; });
      }
      loadAlerts();
      return result;
    };
  }

  /* Refresh after actions that can change alert state. */
  if (typeof navigate === 'function') {
    var baseNavigate = navigate;
    navigate = function (view) {
      var result = baseNavigate.apply(this, arguments);
      if (view === 'billing' || view === 'sims' || view === 'dashboard' || view === 'portal') {
        loadAlerts();
      }
      return result;
    };
  }

  setInterval(loadAlerts, 120000);
  loadAlerts();
})();
