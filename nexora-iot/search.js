/* Context-aware topbar search.
   app.js used to push every query into the SIM table no matter which page you
   were on. This rewires the topbar search to the data on the current page, and
   hides it entirely on pages that have nothing to search. */
(function () {
  'use strict';

  var host = document.querySelector('.global-search');
  var input = document.getElementById('globalSearch');
  if (!host || !input) { return; }

  /* Replace the input to drop app.js's Enter handler (it always jumped to SIMs). */
  var fresh = input.cloneNode(true);
  input.parentNode.replaceChild(fresh, input);
  input = fresh;

  var currentView = '';
  var currentConfig = null;

  var CONFIG = {
    sims: {
      placeholder: 'Search SIMs by device, ICCID, customer or network…',
      apply: function (q) {
        var box = document.getElementById('simSearch');
        if (!box) { return; }
        if (box.value !== q) {
          box.value = q;
          box.dispatchEvent(new Event('input', { bubbles: true }));
        }
      }
    },
    users: {
      placeholder: 'Search users by name, email or role…',
      apply: function (q) {
        var box = document.getElementById('userSearch');
        if (!box) { return; }
        if (box.value !== q) {
          box.value = q;
          box.dispatchEvent(new Event('input', { bubbles: true }));
        }
      }
    },
    portal: {
      placeholder: 'Search my SIMs by device or ICCID…',
      apply: function (q) { filterNodes('#portalSimBody tr', q); }
    },
    'customer-dashboard': {
      placeholder: 'Search my recent SIM activity…',
      apply: function (q) { filterNodes('#customerActivityBody tr', q); }
    },
    customers: {
      placeholder: 'Search customers by company or contact…',
      apply: function (q) { filterNodes('#customerGrid .customer-card', q); }
    },
    marketplace: {
      placeholder: 'Search countries or carriers…',
      apply: function (q) {
        filterNodes('.marketplace-country-card', q);
        filterNodes('.marketplace-carrier-card', q);
      }
    },
    'network-apis': {
      placeholder: 'Search providers by name, country or endpoint…',
      apply: function (q) { filterNodes('#networkApiList .stack-row, #euiccList .stack-row', q); }
    },
    billing: {
      placeholder: 'Search invoices, payments or usage…',
      apply: function (q) {
        filterNodes('#billingInvoiceBody tr, #billingPaymentBody tr, #billingRateBody tr, #billingUsageBody tr', q);
      }
    }
  };

  function filterNodes(selector, q) {
    var needle = String(q || '').trim().toLowerCase();
    var nodes = document.querySelectorAll(selector);
    for (var i = 0; i < nodes.length; i += 1) {
      var match = !needle || (nodes[i].textContent || '').toLowerCase().indexOf(needle) >= 0;
      nodes[i].style.display = match ? '' : 'none';
    }
  }

  function clearFilters() {
    filterNodes('.marketplace-country-card, .marketplace-carrier-card, '
      + '#customerGrid .customer-card, #networkApiList .stack-row, #euiccList .stack-row, '
      + '#billingInvoiceBody tr, #billingPaymentBody tr, #billingRateBody tr, #billingUsageBody tr', '');
    ['#simSearch', '#userSearch'].forEach(function (selector) {
      var box = document.querySelector(selector);
      if (box && box.value) {
        box.value = '';
        box.dispatchEvent(new Event('input', { bubbles: true }));
      }
    });
  }

  function setView(view) {
    if (view === currentView) { return; }
    clearFilters();
    currentView = view;
    currentConfig = CONFIG[view] || null;
    input.value = '';

    /* One search box per page: the in-page inputs are hidden while the topbar
       search drives them. */
    document.body.classList.toggle('search-scope-sims', view === 'sims');
    document.body.classList.toggle('search-scope-users', view === 'users');

    if (!currentConfig) {
      host.style.display = 'none';
      return;
    }
    host.style.display = '';
    input.placeholder = currentConfig.placeholder;
    input.setAttribute('aria-label', currentConfig.placeholder);
  }

  function applyQuery() {
    if (currentConfig) { currentConfig.apply(input.value); }
  }

  var timer = null;
  input.addEventListener('input', function () {
    clearTimeout(timer);
    timer = setTimeout(applyQuery, 110);
  });
  input.addEventListener('keydown', function (event) {
    if (event.key === 'Escape') {
      input.value = '';
      applyQuery();
      input.blur();
    }
  });

  /* Pages load their rows asynchronously; re-apply the query after a render. */
  var reapplyTimer = null;
  var observer = new MutationObserver(function () {
    if (!currentConfig || !input.value.trim()) { return; }
    clearTimeout(reapplyTimer);
    reapplyTimer = setTimeout(function () {
      if (currentConfig) { currentConfig.apply(input.value); }
    }, 90);
  });
  observer.observe(document.querySelector('.content') || document.body, { childList: true, subtree: true });

  if (typeof navigate === 'function') {
    var baseNavigate = navigate;
    navigate = function (view) {
      var result = baseNavigate.apply(this, arguments);
      setView(view || '');
      return result;
    };
  }

  var active = document.querySelector('.view.active');
  setView(active ? active.id : '');
})();
