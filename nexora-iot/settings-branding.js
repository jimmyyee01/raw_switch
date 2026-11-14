/* Settings → Workspace branding.
   Replaces the minimal branding card with a full editor: drag-and-drop logo,
   type/size validation, client-side downscaling and a live sidebar/sign-in
   preview. Extends app.js (companyBranding, applyCompanyBranding, toast). */
(function () {
  'use strict';

  var MAX_BYTES = 2 * 1024 * 1024;
  var MAX_DIMENSION = 512;
  var ALLOWED = ['image/png', 'image/jpeg', 'image/webp', 'image/svg+xml'];

  function bytesLabel(bytes) {
    if (!bytes) { return ''; }
    if (bytes >= 1024 * 1024) { return (bytes / 1024 / 1024).toFixed(1) + ' MB'; }
    return Math.max(1, Math.round(bytes / 1024)) + ' KB';
  }

  function escapeHtml(value) {
    return String(value == null ? '' : value).replace(/[&<>"']/g, function (ch) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch];
    });
  }

  function current() {
    return (typeof companyBranding !== 'undefined' && companyBranding) ? companyBranding : {};
  }

  function buildCard() {
    var card = document.createElement('article');
    card.id = 'brandingCard';
    card.className = 'card settings-branding';
    card.innerHTML = [
      '<div class="settings-section-head">',
      '  <div><h2>Workspace branding</h2><p>Name, logo and colours used across the dashboard and the sign-in screen.</p></div>',
      '  <span class="badge active">Admin only</span>',
      '</div>',
      '<div class="branding-grid">',
      '  <form id="brandingForm" class="branding-controls">',
      '    <label class="branding-field">Company name',
      '      <input name="name" maxlength="60" placeholder="RAW-Switch (optional)">',
      '      <small class="field-help">Shown in the sidebar, sign-in card and browser tab.</small>',
      '    </label>',
      '    <div class="branding-field">',
      '      <span class="field-label">Company logo</span>',
      '      <div class="logo-drop" id="logoDrop" tabindex="0" role="button" aria-label="Upload a company logo">',
      '        <input type="file" id="logoInput" accept="image/png,image/jpeg,image/webp,image/svg+xml" hidden>',
      '        <div class="logo-drop-empty" id="logoDropEmpty">',
      '          <i data-lucide="image-plus"></i>',
      '          <strong>Drop your logo here</strong>',
      '          <small>or click to browse — PNG, JPG, WebP or SVG, up to 2 MB</small>',
      '        </div>',
      '        <img id="logoDropPreview" alt="Logo preview" hidden>',
      '      </div>',
      '      <div class="logo-meta" id="logoMeta" hidden></div>',
      '    </div>',
      '    <div class="branding-colors">',
      '      <label>Primary colour<input name="primary" type="color"></label>',
      '      <label>Navigation colour<input name="accent" type="color"></label>',
      '    </div>',
      '    <div class="branding-actions">',
      '      <button type="button" class="btn btn-secondary" id="removeBrandLogo"><i data-lucide="trash-2"></i>Remove logo</button>',
      '      <button type="submit" class="btn btn-primary"><i data-lucide="check"></i>Save branding</button>',
      '    </div>',
      '  </form>',
      '  <aside class="branding-preview">',
      '    <span class="preview-label">Live preview</span>',
      '    <div class="preview-sidebar" id="previewSidebar">',
      '      <div class="preview-brand"><span class="brand-mark" id="previewMark"><img id="previewLogo" alt=""><i data-lucide="orbit"></i></span><strong id="previewName">RAW-Switch</strong></div>',
      '      <div class="preview-nav"><span class="on"></span><span></span><span></span><span></span></div>',
      '      <div class="preview-status">All networks online</div>',
      '    </div>',
      '    <div class="preview-signin">',
      '      <div class="preview-brand"><span class="brand-mark" id="previewMark2"><img id="previewLogo2" alt=""><i data-lucide="orbit"></i></span><strong id="previewName2">RAW-Switch</strong></div>',
      '      <div class="preview-line"></div><div class="preview-line short"></div>',
      '      <div class="preview-btn">Sign in</div>',
      '    </div>',
      '  </aside>',
      '</div>'
    ].join('\n');
    return card;
  }

  window.renderBrandingPanel = function () {
    var settings = document.querySelector('#settings');
    if (!settings) { return; }

    var existing = document.querySelector('#brandingCard');
    if (existing) { existing.remove(); }
    var card = buildCard();
    // Branding sits right under the Settings heading: the first thing admins look for.
    var heading = settings.querySelector('.page-heading');
    if (heading && heading.nextSibling) {
      settings.insertBefore(card, heading.nextSibling);
    } else {
      settings.appendChild(card);
    }

    var form = card.querySelector('#brandingForm');
    var drop = card.querySelector('#logoDrop');
    var input = card.querySelector('#logoInput');
    var preview = card.querySelector('#logoDropPreview');
    var empty = card.querySelector('#logoDropEmpty');
    var meta = card.querySelector('#logoMeta');
    var brand = current();

    var state = { logo: brand.logo || '', name: brand.name || 'RAW-Switch' };

    form.name.value = state.name;
    form.primary.value = brand.primary || '#ff9800';
    form.accent.value = brand.accent || '#182238';

    function paintPreview() {
      var name = state.name || 'Workspace';
      card.querySelector('#previewName').textContent = name;
      card.querySelector('#previewName2').textContent = name;
      [card.querySelector('#previewLogo'), card.querySelector('#previewLogo2')].forEach(function (img) {
        img.src = state.logo || '';
        img.hidden = !state.logo;
      });
      [card.querySelector('#previewMark'), card.querySelector('#previewMark2')].forEach(function (mark) {
        mark.style.background = form.primary.value || '#ff9800';
        mark.classList.toggle('has-logo', !!state.logo);
      });
      card.querySelector('#previewSidebar').style.background = form.accent.value || '#182238';
    }

    function paintLogo() {
      if (state.logo) {
        preview.src = state.logo;
        preview.hidden = false;
        empty.hidden = true;
        drop.classList.add('has-logo');
        meta.hidden = false;
        meta.innerHTML = '<i data-lucide="check-circle-2"></i><span>' + escapeHtml(state.logoName || 'Logo ready')
          + (state.logoDims ? ' · ' + escapeHtml(state.logoDims) : '')
          + (state.logoBytes ? ' · ' + escapeHtml(bytesLabel(state.logoBytes)) : '') + '</span>';
      } else {
        preview.hidden = true;
        empty.hidden = false;
        drop.classList.remove('has-logo');
        meta.hidden = true;
        meta.innerHTML = '';
      }
      paintPreview();
      if (typeof icons === 'function') { icons(); }
    }

    function setLogo(dataUrl, name, dims, bytes) {
      state.logo = dataUrl;
      state.logoName = name;
      state.logoDims = dims;
      state.logoBytes = bytes;
      paintLogo();
    }

    function downscale(image, file) {
      var ratio = Math.min(1, MAX_DIMENSION / Math.max(image.width, image.height));
      var width = Math.max(1, Math.round(image.width * ratio));
      var height = Math.max(1, Math.round(image.height * ratio));
      var canvas = document.createElement('canvas');
      canvas.width = width;
      canvas.height = height;
      canvas.getContext('2d').drawImage(image, 0, 0, width, height);
      var type = file.type === 'image/jpeg' ? 'image/jpeg' : 'image/png';
      var dataUrl = canvas.toDataURL(type, 0.92);
      setLogo(dataUrl, file.name, width + '×' + height, Math.round(dataUrl.length * 0.75));
    }

    function importFile(file) {
      if (!file) { return; }
      if (ALLOWED.indexOf(file.type) < 0) {
        toast('Use a PNG, JPG, WebP or SVG logo');
        return;
      }
      if (file.size > MAX_BYTES) {
        toast('Logo must be 2 MB or smaller');
        return;
      }
      var reader = new FileReader();
      reader.onerror = function () { toast('That file could not be read'); };
      reader.onload = function () {
        if (file.type === 'image/svg+xml') {
          // Vectors need no raster pass; keep them crisp.
          setLogo(String(reader.result), file.name, 'Vector', file.size);
          return;
        }
        var image = new Image();
        image.onerror = function () { toast('That image could not be read'); };
        image.onload = function () { downscale(image, file); };
        image.src = String(reader.result);
      };
      reader.readAsDataURL(file);
    }

    drop.addEventListener('click', function () { input.click(); });
    drop.addEventListener('keydown', function (event) {
      if (event.key === 'Enter' || event.key === ' ') {
        event.preventDefault();
        input.click();
      }
    });
    input.addEventListener('change', function () {
      importFile(input.files && input.files[0]);
      input.value = '';
    });

    ['dragenter', 'dragover'].forEach(function (name) {
      drop.addEventListener(name, function (event) {
        event.preventDefault();
        drop.classList.add('dragging');
      });
    });
    ['dragleave', 'drop'].forEach(function (name) {
      drop.addEventListener(name, function (event) {
        event.preventDefault();
        drop.classList.remove('dragging');
      });
    });
    drop.addEventListener('drop', function (event) {
      var file = event.dataTransfer && event.dataTransfer.files && event.dataTransfer.files[0];
      importFile(file);
    });

    card.querySelector('#removeBrandLogo').addEventListener('click', function () {
      setLogo('', '', '', 0);
      toast('Logo removed. Save branding to apply it');
    });

    form.addEventListener('input', function (event) {
      if (event.target.name === 'name') { state.name = form.name.value; }
      paintPreview();
    });

    form.addEventListener('submit', function (event) {
      event.preventDefault();
      if (typeof currentUser !== 'undefined' && (!currentUser || currentUser.role !== 'Administrator')) {
        toast('Only administrators can update company branding');
        return;
      }
      companyBranding = {
        name: (form.name.value || 'RAW-Switch').trim(),
        logo: state.logo || '',
        primary: form.primary.value || '#ff9800',
        accent: form.accent.value || '#182238'
      };
      try {
        localStorage.setItem('companyBranding', JSON.stringify(companyBranding));
      } catch (error) {
        toast('Branding is too large to store in this browser');
        return;
      }
      if (typeof applyCompanyBranding === 'function') { applyCompanyBranding(); }
      if (typeof AUTH_API !== 'undefined') {
        fetch(AUTH_API + '/branding', { method: 'PUT', credentials: 'include', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(companyBranding) }).catch(function () {});
      }
      toast('Workspace branding saved');
    });

    paintLogo();
    if (typeof icons === 'function') { icons(); }
  };

  // If Settings is already on screen when this script loads, refresh it.
  var settingsView = document.querySelector('#settings.view.active');
  if (settingsView) { window.renderBrandingPanel(); }
})();
