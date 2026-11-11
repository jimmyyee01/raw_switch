/* Live analytics: replaces the hard-coded demo numbers on the Analytics page
   with figures computed from the inventory and billing APIs. */
(function () {
  'use strict';

  var COLORS = ['#ff9800', '#4f8cff', '#27ae84', '#9b6bff', '#e06666'];

  function gb(mb) {
    var value = Number(mb || 0);
    return value >= 1024 ? (value / 1024).toFixed(2) + ' GB' : value.toFixed(0) + ' MB';
  }

  function money(value, currency) {
    return (currency || 'USD') + ' ' + Number(value || 0).toLocaleString(undefined, {
      minimumFractionDigits: 2, maximumFractionDigits: 2
    });
  }

  function escapeHtml(value) {
    return String(value == null ? '' : value).replace(/[&<>"']/g, function (ch) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch];
    });
  }

  function bars(groups, unitLabel) {
    var entries = Object.keys(groups).map(function (key) { return [key, groups[key]]; });
    entries.sort(function (a, b) { return b[1] - a[1]; });
    if (!entries.length) { return '<p class="muted">No data yet.</p>'; }
    var max = entries[0][1] || 1;
    return '<div class="bar-analytics">' + entries.map(function (entry, index) {
      var width = Math.max(2, Math.round((entry[1] / max) * 100));
      return '<div><span>' + escapeHtml(entry[0]) + ' <b>' + gb(entry[1]) + '</b></span>'
        + '<div><i style="width:' + width + '%;background:' + COLORS[index % COLORS.length] + '"></i></div></div>';
    }).join('') + '</div>';
  }

  function statCard(label, value, foot) {
    return '<article class="stat-card"><p>' + escapeHtml(label) + '</p>'
      + '<div class="stat-value">' + value + '</div>'
      + '<span class="muted">' + escapeHtml(foot) + '</span></article>';
  }

  function render() {
    var section = document.getElementById('analytics');
    if (!section) { return; }

    var heading = '<div class="page-heading"><div><p class="eyebrow">PERFORMANCE</p>'
      + '<h1>Fleet analytics</h1><p>Live figures from the SIM inventory and billing ledger.</p></div>'
      + '<button class="btn btn-secondary" id="analyticsRefresh"><i data-lucide="refresh-ccw"></i>Refresh</button></div>';

    section.innerHTML = heading + '<p class="muted" style="padding:24px">Loading live analytics…</p>';

    Promise.all([
      authRequest('/inventory').catch(function () { return []; }),
      authRequest('/billing/summary').catch(function () { return {}; })
    ]).then(function (results) {
      var inventory = Array.isArray(results[0]) ? results[0] : [];
      var summary = results[1] || {};
      var currency = summary.currency || 'USD';

      var active = inventory.filter(function (sim) {
        return String(sim.status || '').toLowerCase() === 'active';
      }).length;
      var totalMb = inventory.reduce(function (sum, sim) { return sum + Number(sim.usageMb || 0); }, 0);

      var byType = {};
      var byTech = {};
      inventory.forEach(function (sim) {
        var type = sim.type || 'Unknown';
        var tech = sim.networkTech || 'Unknown';
        byType[type] = (byType[type] || 0) + Number(sim.usageMb || 0);
        byTech[tech] = (byTech[tech] || 0) + Number(sim.usageMb || 0);
      });

      section.innerHTML = heading
        + '<div class="stats-grid compact">'
        + statCard('Active SIMs', String(active), inventory.length + ' SIMs in inventory')
        + statCard('Data used', gb(totalMb), 'Across all SIMs')
        + statCard('Unbilled usage', money(summary.unbilledAmount, currency),
                   gb(summary.unbilledMb) + ' not yet invoiced')
        + statCard('Outstanding', money(summary.outstanding, currency),
                   (summary.invoiceCounts ? summary.invoiceCounts.issued + summary.invoiceCounts.partPaid : 0) + ' open invoices')
        + '</div>'
        + '<div class="two-col">'
        + '<article class="card"><div class="card-heading"><div><h2>Data usage by SIM format</h2>'
        + '<p>Where the megabytes actually go</p></div></div>' + bars(byType) + '</article>'
        + '<article class="card"><div class="card-heading"><div><h2>Data usage by radio technology</h2>'
        + '<p>2G through NB-IoT</p></div></div>' + bars(byTech) + '</article>'
        + '</div>'
        + '<article class="card"><div class="card-heading"><div><h2>Billing snapshot</h2>'
        + '<p>Current period</p></div><span class="badge active">' + escapeHtml(currency) + '</span></div>'
        + '<div class="quality-grid">'
        + '<span><b>' + money(summary.meteredAmount, currency) + '</b>Metered this period</span>'
        + '<span><b>' + money(summary.invoiced, currency) + '</b>Invoiced</span>'
        + '<span><b>' + money(summary.collected, currency) + '</b>Collected</span>'
        + '<span><b>' + money(summary.outstanding, currency) + '</b>Outstanding</span>'
        + '</div></article>';

      var refresh = document.getElementById('analyticsRefresh');
      if (refresh) {
        refresh.addEventListener('click', render);
      }
      if (typeof icons === 'function') { icons(); }
    });
  }

  if (typeof navigate === 'function') {
    var baseNavigate = navigate;
    navigate = function (view) {
      var result = baseNavigate.apply(this, arguments);
      if (view === 'analytics') { render(); }
      return result;
    };
  }

  var activeView = document.querySelector('.view.active');
  if (activeView && activeView.id === 'analytics') { render(); }
})();
