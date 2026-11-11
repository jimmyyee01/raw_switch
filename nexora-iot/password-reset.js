/* Password reset: request a link from the sign-in page, or set a new password
   when arriving with ?reset=<token>. */
(function () {
  'use strict';

  var token = null;
  var sheet = null;

  function esc(value) {
    return String(value == null ? '' : value).replace(/[&<>"']/g, function (ch) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch];
    });
  }

  function build() {
    if (sheet) { return sheet; }
    sheet = document.createElement('div');
    sheet.className = 'reset-sheet';
    sheet.id = 'resetSheet';
    sheet.innerHTML = [
      '<div class="reset-card" role="dialog" aria-modal="true" aria-labelledby="resetTitle">',
      '  <div class="reset-head"><div><h2 id="resetTitle">Reset password</h2>',
      '  <p id="resetSub">Enter your work email and we will send a reset link.</p></div>',
      '  <button class="icon-btn" id="resetClose" aria-label="Close"><i data-lucide="x"></i></button></div>',
      '  <form id="resetRequestForm" class="reset-form">',
      '    <label>Work email<input name="email" type="email" required placeholder="you@company.com"></label>',
      '    <p class="reset-error" id="resetError" hidden></p>',
      '    <button class="btn btn-primary btn-full" type="submit">Send reset link</button>',
      '  </form>',
      '  <form id="resetConfirmForm" class="reset-form" hidden>',
      '    <label>New password<input name="password" type="password" minlength="8" required autocomplete="new-password"></label>',
      '    <label>Confirm password<input name="confirm" type="password" minlength="8" required autocomplete="new-password"></label>',
      '    <p class="reset-error" id="resetConfirmError" hidden></p>',
      '    <button class="btn btn-primary btn-full" type="submit">Update password</button>',
      '  </form>',
      '  <p class="reset-note">Links expire after 30 minutes and can be used once.</p>',
      '</div>'
    ].join('');
    document.body.appendChild(sheet);

    sheet.addEventListener('click', function (event) {
      if (event.target === sheet) { close(); }
    });
    sheet.querySelector('#resetClose').addEventListener('click', close);
    sheet.querySelector('#resetRequestForm').addEventListener('submit', requestLink);
    sheet.querySelector('#resetConfirmForm').addEventListener('submit', confirmReset);
    if (typeof icons === 'function') { icons(); }
    return sheet;
  }

  function showError(id, message) {
    var el = document.getElementById(id);
    if (!el) { return; }
    el.textContent = message;
    el.hidden = !message;
  }

  function openRequest() {
    build().classList.add('open');
    token = null;
    document.getElementById('resetRequestForm').hidden = false;
    document.getElementById('resetConfirmForm').hidden = true;
    document.getElementById('resetSub').textContent = 'Enter your work email and we will send a reset link.';
    showError('resetError', '');
    var emailInput = document.querySelector('#resetRequestForm input[name=email]');
    if (emailInput) {
      var loginEmail = document.querySelector('#loginForm input[name=email]');
      if (loginEmail && loginEmail.value) { emailInput.value = loginEmail.value; }
      emailInput.focus();
    }
  }

  function openConfirm(value) {
    build().classList.add('open');
    token = value;
    document.getElementById('resetRequestForm').hidden = true;
    document.getElementById('resetConfirmForm').hidden = false;
    document.getElementById('resetSub').textContent = 'Choose a new password for your account.';
    showError('resetConfirmError', '');
    var field = document.querySelector('#resetConfirmForm input[name=password]');
    if (field) { field.focus(); }
  }

  function close() {
    if (sheet) { sheet.classList.remove('open'); }
  }

  function requestLink(event) {
    event.preventDefault();
    var form = event.target;
    showError('resetError', '');
    form.querySelector('button').disabled = true;
    fetch(AUTH_API + '/auth/password-reset/request', {
      method: 'POST',
      credentials: 'include',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email: form.email.value.trim() })
    }).then(function (response) {
      return response.json().then(function (data) {
        if (!response.ok) { throw new Error(data.detail || 'Could not send the reset link'); }
        toast('If that address exists, a reset link has been sent');
        close();
      });
    }).catch(function (error) {
      showError('resetError', error.message);
    }).then(function () {
      form.querySelector('button').disabled = false;
    });
  }

  function confirmReset(event) {
    event.preventDefault();
    var form = event.target;
    showError('resetConfirmError', '');
    if (form.password.value !== form.confirm.value) {
      showError('resetConfirmError', 'The two passwords do not match');
      return;
    }
    form.querySelector('button').disabled = true;
    fetch(AUTH_API + '/auth/password-reset/confirm', {
      method: 'POST',
      credentials: 'include',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ token: token, password: form.password.value })
    }).then(function (response) {
      return response.json().then(function (data) {
        if (!response.ok) { throw new Error(data.detail || 'Could not reset the password'); }
        close();
        toast('Password updated. Please sign in.');
        if (window.history && history.replaceState) {
          history.replaceState(null, '', location.pathname);
        }
        form.reset();
      });
    }).catch(function (error) {
      showError('resetConfirmError', error.message);
    }).then(function () {
      form.querySelector('button').disabled = false;
    });
  }

  var forgot = document.querySelector('.forgot');
  if (forgot) {
    forgot.addEventListener('click', function (event) {
      event.preventDefault();
      openRequest();
    });
  }

  var params = new URLSearchParams(location.search);
  var resetToken = params.get('reset');
  if (resetToken) {
    openConfirm(resetToken);
  }
})();
