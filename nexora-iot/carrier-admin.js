/* Carrier administration for the Network marketplace.
   Create / edit / activate / delete carriers and link each one to any number of
   Network APIs providers (local and international).
   Loaded after app.js and reuses its globals: AUTH_API, authRequest, toast,
   icons, loadMarketplaceData, loadCountryCarriers, navigate, currentUser. */
(function () {
  'use strict';

  var formCard = document.getElementById('carrierFormCard');
  var form = document.getElementById('carrierForm');
  var addButton = document.getElementById('marketplaceAddCarrier');
  if (!formCard || !form || !addButton) { return; }

  var editingId = null;
  var networks = null;

  if (typeof loadMarketplaceData === 'function') {
    var baseLoad = loadMarketplaceData;
    loadMarketplaceData = function () {
      addButton.hidden = !isAdmin();
      return baseLoad.apply(this, arguments);
    };
  }

  function isAdmin() {
    return typeof currentUser !== 'undefined' && !!currentUser && currentUser.role === 'Administrator';
  }

  function escapeHtml(value) {
    return String(value == null ? '' : value).replace(/[&<>"']/g, function (ch) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch];
    });
  }

  function currentCountry() {
    var label = document.getElementById('marketplaceCountryName');
    return label && label.textContent ? label.textContent.trim() : '';
  }

  function providerSubtitle(network) {
    var bits = [];
    if (network.scope === 'international') {
      bits.push(network.coverage || 'Global coverage');
    } else if (network.country) {
      bits.push(network.country);
    }
    if (network.isActive === false) { bits.push('inactive'); }
    return escapeHtml(bits.join(' · '));
  }

  function providerOption(network) {
    var intl = network.scope === 'international';
    return '<label class="network-option">' +
      '<input type="checkbox" value="' + network.id + '">' +
      '<span class="network-option-body"><strong>' + escapeHtml(network.name) + '</strong>' +
      '<small>' + providerSubtitle(network) + '</small></span>' +
      (intl ? '<span class="network-option-scope">International</span>' : '') +
      '</label>';
  }

  function renderNetworkPicker() {
    var box = document.getElementById('carrierNetworkPicker');
    var hint = document.getElementById('carrierNetworkHint');
    if (!box) { return; }
    var list = networks || [];

    if (!list.length) {
      box.innerHTML = '<p class="network-picker-empty">No providers configured yet.</p>';
      if (hint) {
        hint.innerHTML = 'No telecom networks yet — add one under '
          + '<a href="#" data-goto-network-apis>Network APIs</a>.';
      }
      return;
    }

    var local = list.filter(function (n) { return (n.scope || 'local') !== 'international'; });
    var international = list.filter(function (n) { return n.scope === 'international'; });
    var out = '';

    if (local.length) {
      out += '<div class="network-picker-group"><span class="network-picker-title">Local providers</span>'
        + local.map(providerOption).join('') + '</div>';
    }
    if (international.length) {
      out += '<div class="network-picker-group"><span class="network-picker-title">International providers</span>'
        + international.map(providerOption).join('') + '</div>';
    }
    box.innerHTML = out;
    if (hint) {
      hint.textContent = 'Link this carrier to one or more providers configured under Network APIs.';
    }
  }

  function loadNetworks(force) {
    if (networks && !force) {
      renderNetworkPicker();
      return Promise.resolve(networks);
    }
    return authRequest('/telecom/networks').then(function (list) {
      networks = Array.isArray(list) ? list : [];
      window.__telecomNetworks = networks;
      renderNetworkPicker();
      return networks;
    }).catch(function () {
      networks = [];
      window.__telecomNetworks = [];
      renderNetworkPicker();
      return [];
    });
  }

  function setBusy(busy) {
    var submit = document.getElementById('carrierFormSubmit');
    if (submit) { submit.disabled = busy; }
  }

  function selectedNetworkIds() {
    var boxes = document.querySelectorAll('#carrierNetworkPicker input[type="checkbox"]');
    var ids = [];
    for (var i = 0; i < boxes.length; i += 1) {
      if (boxes[i].checked) { ids.push(Number(boxes[i].value)); }
    }
    return ids;
  }

  function openForm(carrier, country) {
    editingId = carrier ? carrier.id : null;
    document.getElementById('carrierFormTitle').textContent =
      carrier ? 'Edit ' + carrier.name : 'Add a carrier';
    document.getElementById('carrierFormSubtitle').textContent = carrier
      ? 'Update the API details, available networks or availability of this provider.'
      : 'Register a network provider the platform can provision SIMs on.';

    form.reset();
    form.elements.name.value = carrier ? carrier.name : '';
    form.elements.country.value = carrier ? carrier.country : (country || '');
    form.elements.apiEndpoint.value = carrier ? carrier.apiEndpoint : '';
    form.elements.bandwidthMbps.value = carrier ? Number(carrier.bandwidthMbps || 0) : 0;
    form.elements.reliabilityScore.value =
      carrier ? Number(carrier.reliabilityScore == null ? 1 : carrier.reliabilityScore) : 1;
    form.elements.isActive.checked = carrier ? !!carrier.isActive : true;
    form.elements.apiKey.value = '';
    form.elements.apiKey.required = !carrier;
    form.elements.apiKey.placeholder = carrier ? 'Leave blank to keep the stored key' : '••••••••';
    document.getElementById('carrierKeyHint').textContent = carrier ? 'Optional' : 'Required';

    formCard.hidden = false;
    if (formCard.scrollIntoView) { formCard.scrollIntoView({ behavior: 'smooth', block: 'nearest' }); }
    form.elements.name.focus();
    if (typeof icons === 'function') { icons(); }

    loadNetworks(true).then(function () {
      var linked = carrier
        ? (carrier.telecomNetworkIds || (carrier.telecomNetworkId ? [carrier.telecomNetworkId] : []))
        : [];
      var boxes = document.querySelectorAll('#carrierNetworkPicker input[type="checkbox"]');
      for (var i = 0; i < boxes.length; i += 1) {
        boxes[i].checked = linked.indexOf(Number(boxes[i].value)) >= 0;
      }
    });
  }

  function closeForm() {
    editingId = null;
    formCard.hidden = true;
    form.reset();
  }

  function refreshBadge() {
    var badge = document.getElementById('marketplaceCount');
    if (!badge) { return; }
    fetch(AUTH_API + '/marketplace/carriers', { credentials: 'include', headers: authHeaders() })
      .then(function (response) { return response.ok ? response.json() : []; })
      .then(function (list) { badge.textContent = String(list.length); })
      .catch(function () { /* the badge is decorative */ });
  }

  function refresh(country) {
    if (typeof loadMarketplaceData === 'function') { loadMarketplaceData(); }
    if (country && typeof loadCountryCarriers === 'function') { loadCountryCarriers(country); }
    refreshBadge();
  }

  form.addEventListener('submit', function (event) {
    event.preventDefault();
    if (!isAdmin()) { toast('Only administrators can manage carriers'); return; }

    var payload = {
      name: form.elements.name.value.trim(),
      country: form.elements.country.value.trim(),
      apiEndpoint: form.elements.apiEndpoint.value.trim(),
      bandwidthMbps: Number(form.elements.bandwidthMbps.value || 0),
      reliabilityScore: Number(form.elements.reliabilityScore.value || 0),
      isActive: form.elements.isActive.checked,
      telecomNetworkIds: selectedNetworkIds()
    };
    var key = form.elements.apiKey.value.trim();
    if (key) { payload.apiKey = key; }

    var wasEditing = !!editingId;
    var request = wasEditing
      ? authRequest('/marketplace/' + editingId, { method: 'PATCH', body: JSON.stringify(payload) })
      : authRequest('/marketplace', { method: 'POST', body: JSON.stringify(payload) });

    setBusy(true);
    request.then(function () {
      var country = payload.country || currentCountry();
      closeForm();
      refresh(country);
      toast(wasEditing ? 'Carrier updated' : 'Carrier added');
    }).catch(function (error) {
      toast(error.message || 'Could not save the carrier');
    }).then(function () {
      setBusy(false);
    });
  });

  document.getElementById('carrierFormCancel').addEventListener('click', closeForm);
  document.getElementById('carrierFormClose').addEventListener('click', closeForm);

  addButton.addEventListener('click', function () {
    if (!isAdmin()) { toast('Only administrators can manage carriers'); return; }
    openForm(null, currentCountry());
  });

  document.getElementById('marketplaceCarrierGrid').addEventListener('click', function (event) {
    var edit = event.target.closest('[data-carrier-edit]');
    var toggle = event.target.closest('[data-carrier-toggle]');
    var remove = event.target.closest('[data-carrier-delete]');
    if (!edit && !toggle && !remove) { return; }
    if (!isAdmin()) { toast('Only administrators can manage carriers'); return; }

    var carriers = window.__marketplaceCarriers || [];
    function find(id) {
      for (var i = 0; i < carriers.length; i += 1) {
        if (carriers[i].id === id) { return carriers[i]; }
      }
      return null;
    }

    if (edit) {
      var carrier = find(Number(edit.getAttribute('data-carrier-edit')));
      if (carrier) { openForm(carrier, currentCountry()); }
      return;
    }

    if (toggle) {
      var toggleId = Number(toggle.getAttribute('data-carrier-toggle'));
      var next = toggle.getAttribute('data-carrier-next') === '1';
      authRequest('/marketplace/' + toggleId, {
        method: 'PATCH',
        body: JSON.stringify({ isActive: next })
      }).then(function () {
        refresh(currentCountry());
        toast(next ? 'Carrier activated' : 'Carrier deactivated');
      }).catch(function (error) {
        toast(error.message || 'Could not update the carrier');
      });
      return;
    }

    var removeId = Number(remove.getAttribute('data-carrier-delete'));
    var target = find(removeId);
    var label = target ? target.name : 'this carrier';
    if (!window.confirm('Remove ' + label + ' from the marketplace? SIMs stay in place and are simply unassigned.')) {
      return;
    }
    authRequest('/marketplace/' + removeId, { method: 'DELETE' }).then(function () {
      refresh(currentCountry());
      toast('Carrier removed');
    }).catch(function (error) {
      toast(error.message || 'Could not remove the carrier');
    });
  });

  document.addEventListener('click', function (event) {
    var goto = event.target.closest ? event.target.closest('[data-goto-network-apis]') : null;
    if (goto) {
      event.preventDefault();
      if (typeof navigate === 'function') { navigate('network-apis'); }
      return;
    }
    var countryCard = event.target.closest ? event.target.closest('.marketplace-country-card') : null;
    if (!countryCard) { return; }
    var country = countryCard.getAttribute('data-country');
    if (!country) { return; }
    var list = document.getElementById('carrierCountryList');
    if (list && !list.querySelector('option[value="' + country.replace(/"/g, '&quot;') + '"]')) {
      var option = document.createElement('option');
      option.value = country;
      list.appendChild(option);
    }
  });

  addButton.hidden = !isAdmin();
  loadNetworks();
  refreshBadge();
})();
