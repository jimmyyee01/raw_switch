/* Login feedback and step-up verification: failed sign-in popup, human check,
   and authenticator codes. */
(function () {
  'use strict';

  var form = document.getElementById('loginForm');
  if (!form) { return; }

  var error = document.getElementById('loginError');
  if (!error) {
    error = document.createElement('p');
    error.id = 'loginError';
    error.className = 'login-error';
    error.setAttribute('role', 'alert');
    error.hidden = true;
    form.appendChild(error);
  }

  var verify = document.createElement('div');
  verify.className = 'login-verify';
  verify.id = 'loginVerify';
  verify.hidden = true;
  verify.innerHTML = [
    '<p class="login-verify-label" id="loginVerifyLabel"></p>',
    '<input id="loginVerifyInput" autocomplete="one-time-code" inputmode="text" placeholder="Your answer">',
    '<button type="button" class="btn btn-primary btn-block" id="loginVerifySubmit">Verify and sign in</button>'
  ].join('');
  error.insertAdjacentElement('afterend', verify);

  var label = verify.querySelector('#loginVerifyLabel');
  var input = verify.querySelector('#loginVerifyInput');
  var submit = verify.querySelector('#loginVerifySubmit');

  var lastIdentifier = '';
  var lastPassword = '';

  function shake() {
    var card = form.closest('.auth-card');
    if (!card) { return; }
    card.classList.remove('shake');
    void card.offsetWidth;
    card.classList.add('shake');
  }

  function showError(message) {
    error.textContent = message;
    error.hidden = false;
    verify.hidden = true;
    shake();
  }

  function hideError() {
    error.hidden = true;
    error.textContent = '';
  }

  function showVerify() {
    hideError();
    verify.hidden = false;
    if (window.__authChallenge) {
      label.textContent = 'Human check: ' + (window.__authChallenge.question || '') + ' = ?';
      input.placeholder = 'Answer';
      input.setAttribute('inputmode', 'numeric');
    } else {
      label.textContent = 'Enter the 6-digit code from your authenticator app';
      input.placeholder = '000000';
      input.setAttribute('inputmode', 'numeric');
    }
    input.value = '';
    input.focus();
  }

  function hideVerify() {
    verify.hidden = true;
    input.value = '';
  }

  form.addEventListener('input', function () {
    hideError();
  });

  /* Error-styled toast (app.js passes a type). */
  if (typeof toast === 'function') {
    var baseToast = toast;
    toast = function (message, type) {
      baseToast(message);
      var el = document.getElementById('toast');
      if (el) { el.classList.toggle('toast-error', type === 'error'); }
    };
  }

  if (typeof login === 'function') {
    var baseLogin = login;

    login = function (identifier, password, extra) {
      lastIdentifier = identifier;
      lastPassword = password;
      hideError();
      var result = baseLogin(identifier, password, extra);
      if (!result || typeof result.then !== 'function') { return result; }
      return result.then(function (ok) {
        if (ok) {
          hideVerify();
          return true;
        }
        if (window.__authChallenge || window.__authTotp) {
          showVerify();
          return false;
        }
        showError('Invalid email or password. Check your details and try again.');
        return false;
      });
    };

    function completeLogin() {
      hideVerify();
      hideError();
      var overlay = document.getElementById('authOverlay');
      if (overlay) { overlay.classList.remove('open'); }
      if (typeof currentUser !== 'undefined' && currentUser) {
        navigate(currentUser.role === 'Customer' ? 'customer-dashboard' : 'dashboard');
        toast('Welcome back, ' + currentUser.name);
      }
    }

    submit.addEventListener('click', function () {
      var answer = input.value.trim();
      if (!answer) { showError('Enter the verification answer'); return; }
      var extra = window.__authChallenge
        ? { challengeId: window.__authChallenge.challengeId, challengeAnswer: answer }
        : { totp: answer };
      submit.disabled = true;
      baseLogin(lastIdentifier, lastPassword, extra).then(function (ok) {
        if (ok) { completeLogin(); return; }
        showVerify();
        showError(window.__authChallenge
          ? 'That answer was not accepted - try the new question'
          : 'That code was not accepted');
      }).catch(function (err) {
        showError(err.message || 'Verification failed');
      }).then(function () {
        submit.disabled = false;
      });
    });

    input.addEventListener('keydown', function (event) {
      if (event.key === 'Enter') {
        event.preventDefault();
        submit.click();
      }
    });
  }
})();
