/* Access control: the permissions an administrator grants decide which areas a
   user sees and can open. Admins always pass. Loaded after app.js so it can wrap
   navigate() and observe the user modal. */
(function () {
  'use strict';

  var VIEW_PERMISSION = {
    dashboard: 'dashboard',
    'customer-dashboard': 'dashboard',
    sims: 'sims',
    'sim-studio': 'sims',
    roaming: 'sims',
    portal: 'sims',
    marketplace: 'sims',
    customers: 'customers',
    analytics: 'reports',
    reports: 'reports',
    'customer-reports': 'reports',
    billing: 'billing',
    'customer-team': 'team',
    users: 'settings',
    logs: 'settings',
    trash: 'settings',
    settings: 'settings'
  };

  var DEFAULT_PERMISSIONS = {
    Customer: ['dashboard', 'sims', 'data', 'reports', 'billing', 'team'],
    User: ['dashboard', 'sims', 'data', 'reports'],
    Viewer: ['dashboard', 'reports']
  };

  function isAdmin() {
    return typeof currentUser !== 'undefined' && !!currentUser && currentUser.role === 'Administrator';
  }

  function permissions() {
    if (typeof currentUser === 'undefined' || !currentUser) { return []; }
    if (Array.isArray(currentUser.permissions) && currentUser.permissions.length) {
      return currentUser.permissions;
    }
    /* Legacy accounts with no explicit list keep their role's defaults. */
    return DEFAULT_PERMISSIONS[currentUser.role] || [];
  }

  function can(permission) {
    if (!permission) { return true; }
    if (!currentUser) { return false; }
    if (isAdmin()) { return true; }
    return permissions().indexOf(permission) >= 0;
  }

  function applyAccess() {
    var signedIn = typeof currentUser !== 'undefined' && !!currentUser;
    if (!signedIn) { return; }

    document.querySelectorAll('.nav-item[data-view]').forEach(function (item) {
      var permission = VIEW_PERMISSION[item.dataset.view];
      var allowed = can(permission);
      item.style.display = allowed ? '' : 'none';
      item.setAttribute('aria-hidden', allowed ? 'false' : 'true');
    });

    /* Hide a section header when every item under it is hidden. */
    document.querySelectorAll('.nav-group').forEach(function (group) {
      var visible = Array.prototype.filter.call(
        group.querySelectorAll('.nav-item[data-view]'),
        function (item) { return item.style.display !== 'none'; }
      ).length;
      var toggle = document.querySelector('.nav-group-toggle[data-nav-group="' + group.dataset.navGroup + '"]');
      if (toggle) { toggle.style.display = visible ? '' : 'none'; }
    });

    document.body.classList.toggle('is-admin', isAdmin());
    document.body.classList.toggle('not-admin', !isAdmin());

    /* Never leave someone parked on a view they cannot access. */
    var active = document.querySelector('.view.active');
    if (active && VIEW_PERMISSION[active.id] && !can(VIEW_PERMISSION[active.id])) {
      var firstAllowed = Array.prototype.find.call(
        document.querySelectorAll('.nav-item[data-view]'),
        function (item) { return item.style.display !== 'none'; }
      );
      if (firstAllowed) { navigate(firstAllowed.dataset.view); }
    }
  }

  function polishUserPermissions() {
    var form = document.getElementById('userForm');
    if (!form || form.hidden) { return; }
    if (form.style.display === 'none') { return; }
    var fieldset = form.querySelector('.user-permission-set');
    if (!fieldset) { return; }
    var legend = fieldset.querySelector('legend');
    var roleField = form.elements.role;
    var customer = !!roleField && roleField.value === 'Customer';
    if (legend) {
      legend.textContent = customer
        ? 'Customer access - untick an area to hide it from this customer'
        : 'Permissions';
    }
    fieldset.querySelectorAll('#userPermissionChecks label').forEach(function (label) {
      var input = label.querySelector('input');
      if (!input) { return; }
      var adminOnly = input.value === 'customers' || input.value === 'settings';
      var hide = customer && adminOnly;
      label.style.display = hide ? 'none' : '';
      if (hide) { input.checked = false; }
    });
  }

  window.can = can;
  window.applyAccess = applyAccess;
  window.polishUserPermissions = polishUserPermissions;

  var ADMIN_VIEWS_FOR_CUSTOMERS = ['dashboard', 'sims', 'roaming', 'customers', 'analytics',
    'reports', 'settings', 'users', 'marketplace', 'network-apis'];

  function customerHome() {
    var portal = document.querySelector('.nav-item[data-view="portal"]');
    if (portal && portal.style.display !== 'none') { return 'portal'; }
    if (can('dashboard')) { return 'customer-dashboard'; }
    var first = Array.prototype.find.call(
      document.querySelectorAll('.nav-item[data-view]'),
      function (item) { return item.style.display !== 'none'; }
    );
    return first ? first.dataset.view : 'billing';
  }

  if (typeof navigate === 'function') {
    var baseNavigate = navigate;
    navigate = function (view) {
      if (view && currentUser && currentUser.role === 'Customer'
          && ADMIN_VIEWS_FOR_CUSTOMERS.indexOf(view) >= 0) {
        view = customerHome();
      }
      var permission = VIEW_PERMISSION[view];
      if (view && permission && !can(permission)) {
        toast('You do not have access to that area');
        return false;
      }
      return baseNavigate(view);
    };
  }

  if (typeof login === 'function') {
    var baseLogin = login;
    login = function () {
      var result = baseLogin.apply(this, arguments);
      if (result && typeof result.then === 'function') {
        return result.then(function (value) {
          applyAccess();
          if (typeof currentUser !== 'undefined' && currentUser && currentUser.role === 'Customer') {
            navigate(customerHome());
          }
          return value;
        });
      }
      applyAccess();
      return result;
    };
  }

  var backdrop = document.getElementById('modalBackdrop');
  if (backdrop) {
    new MutationObserver(function () {
      if (backdrop.classList.contains('open')) { setTimeout(polishUserPermissions, 40); }
    }).observe(backdrop, { attributes: true, attributeFilter: ['class'] });
  }

  document.addEventListener('click', function () {
    if (backdrop && backdrop.classList.contains('open')) {
      setTimeout(polishUserPermissions, 60);
    }
  });

  document.addEventListener('change', function (event) {
    if (event.target && event.target.closest && event.target.closest('#userForm select[name="role"]')) {
      setTimeout(polishUserPermissions, 20);
    }
  });

  applyAccess();
})();
