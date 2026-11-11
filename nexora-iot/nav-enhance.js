/* Sidebar navigation upgrade.
   Replaces the one-group-at-a-time accordion in app.js with independent
   collapsible sections that remember their state, highlight the section that
   owns the current view, and keep the nav in sync with live counts. */
(function () {
  'use strict';

  var STORAGE_KEY = 'rawSwitchNavGroups';
  var nav = document.querySelector('.nav');
  if (!nav) { return; }

  var collapsed = {};
  var stored = null;
  try { stored = localStorage.getItem(STORAGE_KEY); } catch (error) { stored = null; }
  if (stored) {
    try { collapsed = JSON.parse(stored) || {}; } catch (error) { collapsed = {}; }
  } else {
    /* First visit: keep the sections the markup ships collapsed. */
    Array.prototype.forEach.call(document.querySelectorAll('.nav-group'), function (group) {
      if (group.classList.contains('collapsed')) {
        collapsed[group.dataset.navGroup] = true;
      }
    });
  }

  function persist() {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(collapsed));
    } catch (error) { /* private mode */ }
  }

  function groups() {
    return Array.prototype.slice.call(document.querySelectorAll('.nav-group'));
  }

  function apply() {
    groups().forEach(function (group) {
      var name = group.dataset.navGroup;
      var isCollapsed = !!collapsed[name];
      group.classList.toggle('collapsed', isCollapsed);
      var toggle = document.querySelector('.nav-group-toggle[data-nav-group="' + name + '"]');
      if (toggle) {
        toggle.setAttribute('aria-expanded', isCollapsed ? 'false' : 'true');
        toggle.classList.toggle('is-open', !isCollapsed);
      }
    });
    if (nav) {
      nav.classList.toggle('is-scrollable', nav.scrollHeight - nav.clientHeight > 4);
    }
  }

  /* Keep the section that owns the active view open; never force the others. */
  window.setNavGroup = function (name) {
    if (name) {
      collapsed[name] = false;
      persist();
    }
    apply();
    groups().forEach(function (group) {
      var active = !!name && group.dataset.navGroup === name;
      group.classList.toggle('is-active-group', active);
    });
    document.querySelectorAll('.nav-group-toggle').forEach(function (toggle) {
      var active = !!name && toggle.dataset.navGroup === name;
      toggle.classList.toggle('is-active-group', active);
    });
  };

  window.toggleNavGroup = function (name) {
    if (!name) { return; }
    collapsed[name] = !collapsed[name];
    persist();
    apply();
  };

  /* Live SIM count in the sidebar badge. */
  if (typeof renderSims === 'function') {
    var baseRenderSims = renderSims;
    renderSims = function () {
      baseRenderSims.apply(this, arguments);
      var badge = document.getElementById('simsCount');
      if (!badge) { return; }
      var count = (typeof sims !== 'undefined' && sims.length) ? sims.length : 0;
      badge.textContent = count ? String(count) : '';
      badge.hidden = !count;
    };
  }

  /* Restore saved state, then open the section holding the current view. */
  apply();
  var activeView = document.querySelector('.view.active');
  var activeGroup = activeView ? navGroupForView(activeView.id) : '';
  if (activeGroup) { window.setNavGroup(activeGroup); }

  /* Some views fill themselves from their nav click handler only. Programmatic
     navigation (e.g. the "Network APIs" link in the carrier form) has to load
     them too, so wrap navigate once. */
  if (typeof navigate === 'function') {
    var baseNavigate = navigate;
    navigate = function (view) {
      var result = baseNavigate.apply(this, arguments);
      if (view === 'network-apis' && typeof loadNetworkApiData === 'function') {
        loadNetworkApiData();
      }
      if (view === 'marketplace' && typeof loadMarketplaceData === 'function') {
        loadMarketplaceData();
      }
      if (view === 'settings' && typeof renderBrandingPanel === 'function') {
        renderBrandingPanel();
      }
      return result;
    };
  }

  document.addEventListener('click', function (event) {
    if (!event.target.closest) { return; }
    if (event.target.closest('[data-view="settings"]')) { apply(); }
  });
})();
