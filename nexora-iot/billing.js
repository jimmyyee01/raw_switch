/* RAW-Switch pay-per-data billing console + Pesapal checkout.
   Talks to /billing/* on the aggregator API. Reads authRequest/toast/icons/currentUser
   from app.js (loaded first). */
const billingState = {
  summary: null,
  rates: [],
  usage: [],
  invoices: [],
  payments: [],
  gateway: null,
  inventory: [],
  customers: [],
  plans: [],
  lastPayment: null,
  loaded: false,
};

function billingAdmin() {
  return !currentUser || currentUser.role === 'Administrator';
}

function billingScope() {
  return billingAdmin() ? '' : (currentUser && currentUser.customerName) || '';
}

function billingQuery(extra) {
  const params = new URLSearchParams();
  const scope = billingScope();
  if (scope) params.set('customer', scope);
  Object.entries(extra || {}).forEach(([key, value]) => {
    if (value !== undefined && value !== null && value !== '') params.set(key, value);
  });
  const text = params.toString();
  return text ? '?' + text : '';
}

function billingEsc(value) {
  return String(value === undefined || value === null ? '' : value).replace(
    /[&<>"']/g,
    (ch) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch])
  );
}

function billingMoney(value, currency) {
  const amount = Number(value || 0).toLocaleString(undefined, {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
  return `${currency || 'USD'} ${amount}`;
}

function billingMb(value) {
  const mb = Number(value || 0);
  if (mb >= 1024) return `${(mb / 1024).toFixed(2)} GB`;
  return `${mb.toFixed(mb && mb < 10 ? 2 : 1)} MB`;
}

function billingWhen(value) {
  if (!value) return '—';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return billingEsc(value);
  return date.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
}

const BILLING_STATUS_CLASS = {
  issued: 'bill-due',
  part_paid: 'bill-part',
  paid: 'bill-paid',
  void: 'inactive',
  pending: 'bill-pending',
  completed: 'bill-paid',
  failed: 'bill-failed',
  cancelled: 'inactive',
};

function billingBadge(status) {
  const key = String(status || '');
  const label = key.replace(/_/g, ' ') || 'unknown';
  return `<span class="badge ${BILLING_STATUS_CLASS[key] || ''}">${billingEsc(label)}</span>`;
}

function billingEmpty(cols, text) {
  return `<tr><td class="billing-empty" colspan="${cols}">${billingEsc(text)}</td></tr>`;
}

function billingSetText(selector, text) {
  const node = document.querySelector(selector);
  if (node) node.textContent = text;
}

async function loadBillingData() {
  if (!document.querySelector('#billing')) return;
  try {
    const [summary, rates, usage, invoices, payments, gateway, inventory, customers, plans] = await Promise.all([
      authRequest('/billing/summary' + billingQuery()),
      authRequest('/billing/rates'),
      authRequest('/billing/usage' + billingQuery({ limit: 25 })),
      authRequest('/billing/invoices' + billingQuery({ limit: 25 })),
      authRequest('/billing/payments' + billingQuery({ limit: 25 })),
      authRequest('/billing/gateway'),
      authRequest('/inventory?refresh=' + Date.now()).catch(() => []),
      authRequest('/customers').catch(() => []),
      authRequest('/plans').catch(() => []),
    ]);
    billingState.summary = summary;
    billingState.rates = rates || [];
    billingState.usage = usage || [];
    billingState.invoices = invoices || [];
    billingState.payments = payments || [];
    billingState.gateway = gateway;
    billingState.inventory = inventory || [];
    billingState.customers = customers || [];
    billingState.plans = plans || [];
    billingState.loaded = true;
    renderBilling();
  } catch (error) {
    toast(error.message || 'Unable to load billing data', 'error');
  }
}

function renderBilling() {
  renderBillingKpis();
  renderBillingRates();
  renderBillingUsage();
  renderBillingInvoices();
  renderBillingPayments();
  renderBillingGateway();
  populateBillingSelects();
  if (typeof icons === 'function') icons();
}

function renderBillingKpis() {
  const summary = billingState.summary || {};
  const currency = summary.currency || 'USD';
  const counts = summary.invoiceCounts || {};
  const payCounts = summary.paymentCounts || {};
  billingSetText('#billMeteredMb', billingMb(summary.meteredMb));
  billingSetText('#billMeteredNote', `${billingMoney(summary.meteredAmount, currency)} priced this period`);
  billingSetText('#billUnbilled', billingMoney(summary.unbilledAmount, currency));
  billingSetText('#billUnbilledNote', `${billingMb(summary.unbilledMb)} metered, not yet invoiced`);
  billingSetText('#billOutstanding', billingMoney(summary.outstanding, currency));
  billingSetText('#billOutstandingNote', `${(counts.issued || 0) + (counts.partPaid || 0)} open invoice(s)`);
  billingSetText('#billCollected', billingMoney(summary.collected, currency));
  billingSetText('#billCollectedNote', `${payCounts.completed || 0} confirmed Pesapal payment(s)`);
  const period = summary.period || {};
  billingSetText('#billingPeriodBadge', `${period.start || '—'} → ${period.end || '—'}`);
  const scope = billingScope();
  billingSetText(
    '#billingScopeNote',
    scope
      ? `Metered data for ${scope}: priced per megabyte, invoiced and settled through Pesapal.`
      : 'Every megabyte a SIM moves is priced against the rate card, invoiced per customer and settled through Pesapal.'
  );
}

function renderBillingRates() {
  const body = document.querySelector('#billingRateBody');
  if (!body) return;
  billingSetText('#billingRateCount', `${billingState.rates.length} rates`);
  body.innerHTML = billingState.rates.length
    ? billingState.rates
        .map((rate) => {
          const status = billingAdmin()
            ? `<button class="badge ${rate.active ? 'active' : 'inactive'}" data-billing-rate="${rate.id}" data-billing-rate-active="${rate.active ? 1 : 0}" title="Toggle this tariff">${rate.active ? 'Active' : 'Paused'}</button>`
            : `<span class="badge ${rate.active ? 'active' : 'inactive'}">${rate.active ? 'Active' : 'Paused'}</span>`;
          return `<tr>
            <td><strong>${billingEsc(rate.name)}</strong><small>${billingEsc(rate.description || 'Metered data')}</small></td>
            <td>${Number(rate.pricePerMb).toFixed(4)} ${billingEsc(rate.currency)}</td>
            <td>${billingMoney(rate.pricePerGb, rate.currency)}</td>
            <td>${Number(rate.blockMb).toFixed(3)} MB</td>
            <td>${billingMoney(rate.minCharge, rate.currency)}</td>
            <td>${billingEsc(rate.planName || 'All plans')}</td>
            <td>${status}</td>
          </tr>`;
        })
        .join('')
    : billingEmpty(7, 'No per-megabyte rates yet - add one above.');
}

function renderBillingUsage() {
  const body = document.querySelector('#billingUsageBody');
  if (!body) return;
  body.innerHTML = billingState.usage.length
    ? billingState.usage
        .map(
          (row) => `<tr>
            <td><strong>${billingEsc(row.device || row.iccid)}</strong><small>${billingEsc(row.iccid)}</small></td>
            <td>${billingEsc(row.customer || 'Unassigned')}</td>
            <td>${billingMb(row.megabytes)}</td>
            <td>${billingEsc(row.rateName || 'Default')}<small>${Number(row.pricePerMb || 0).toFixed(4)} ${billingEsc(row.currency)} / MB</small></td>
            <td>${billingMoney(row.amount, row.currency)}</td>
            <td>${row.invoiceId ? `<span class="badge active">#${row.invoiceId}</span>` : '<span class="badge bill-pending">Unbilled</span>'}</td>
            <td>${billingWhen(row.recordedAt)}</td>
          </tr>`
        )
        .join('')
    : billingEmpty(7, 'No metered usage yet - record a reading to start the meter.');
}

function renderBillingInvoices() {
  const body = document.querySelector('#billingInvoiceBody');
  if (!body) return;
  billingSetText('#billingInvoiceCount', `${billingState.invoices.length} invoices`);
  body.innerHTML = billingState.invoices.length
    ? billingState.invoices
        .map((invoice) => {
          const actions = [];
          if (invoice.status !== 'paid' && invoice.status !== 'void' && invoice.balance > 0) {
            actions.push(`<button class="row-action" data-billing-pay="${invoice.id}" title="Pay with Pesapal"><i data-lucide="credit-card"></i></button>`);
          }
          if (billingAdmin() && invoice.status !== 'paid' && invoice.status !== 'void') {
            actions.push(`<button class="row-action" data-billing-void="${invoice.id}" title="Void invoice"><i data-lucide="x-circle"></i></button>`);
          }
          return `<tr>
            <td><strong>${billingEsc(invoice.number)}</strong><small>${billingEsc(invoice.periodStart || '')} → ${billingEsc(invoice.periodEnd || '')}</small></td>
            <td>${billingEsc(invoice.customer)}</td>
            <td>${billingMb(invoice.usageMb)}</td>
            <td>${billingMoney(invoice.total, invoice.currency)}<small>tax ${Number(invoice.taxPct || 0).toFixed(1)}%${Number(invoice.platformFeePct || 0) > 0 ? ' \u00b7 fee ' + Number(invoice.platformFeePct).toFixed(1) + '%' : ''}</small></td>
            <td>${billingMoney(invoice.balance, invoice.currency)}</td>
            <td>${billingBadge(invoice.status)}</td>
            <td><div class="billing-row-actions">${actions.join('') || '<span class="muted">—</span>'}</div></td>
          </tr>`;
        })
        .join('')
    : billingEmpty(7, 'No invoices yet - generate one from unbilled usage.');
}

function renderBillingPayments() {
  const body = document.querySelector('#billingPaymentBody');
  if (!body) return;
  body.innerHTML = billingState.payments.length
    ? billingState.payments
        .map((payment) => {
          const actions = [];
          if (payment.status === 'pending') {
            if (payment.redirectUrl) {
              actions.push(`<button class="row-action" data-billing-payment-open="${billingEsc(payment.redirectUrl)}" title="Open checkout"><i data-lucide="external-link"></i></button>`);
            }
            actions.push(`<button class="row-action" data-billing-payment-refresh="${payment.id}" title="Check status"><i data-lucide="refresh-cw"></i></button>`);
            actions.push(`<button class="row-action" data-billing-payment-cancel="${payment.id}" title="Cancel"><i data-lucide="x"></i></button>`);
          }
          return `<tr>
            <td><strong>${billingEsc(payment.reference)}</strong><small>${billingEsc(payment.payerEmail || '')}</small></td>
            <td>${billingMoney(payment.amount, 'USD')}<small>${payment.currency !== 'USD' ? billingMoney(payment.amountLocal, payment.currency) : billingEsc(payment.purpose)}</small></td>
            <td><span class="badge ${payment.mode === 'simulated' ? 'bill-pending' : 'active'}">${billingEsc(payment.mode)}</span></td>
            <td>${billingBadge(payment.status)}<small>${billingEsc(payment.confirmationCode || payment.statusMessage || '')}</small></td>
            <td>${payment.orderTrackingId ? `<code class="billing-code">${billingEsc(payment.orderTrackingId)}</code>` : '—'}</td>
            <td><div class="billing-row-actions">${actions.join('') || '—'}</div></td>
          </tr>`;
        })
        .join('')
    : billingEmpty(6, 'No payments yet - start a Pesapal checkout above.');
}

function renderBillingGateway() {
  const gateway = billingState.gateway || {};
  billingSetText('#billingGatewayMode', gateway.mode || 'simulated');
  const status = document.querySelector('#billingGatewayStatus');
  if (status) {
    status.textContent = gateway.active === false ? 'disabled' : gateway.mode || 'simulated';
    status.className = `badge ${gateway.active === false ? 'inactive' : gateway.mode === 'live' ? 'active' : 'bill-pending'}`;
  }
  const card = document.querySelector('#billingGatewayCard');
  if (card) card.hidden = !billingAdmin();
  const form = document.querySelector('#billingGatewayForm');
  if (!form) return;
  form.environment.value = gateway.environment || 'simulated';
  if (form.currency) form.currency.value = gateway.currency || 'USD';
  if (form.fxRate) form.fxRate.value = gateway.fxRate || 1;
  if (form.ipnUrl) form.ipnUrl.value = gateway.ipnUrl || '';
  if (form.callbackUrl) form.callbackUrl.value = gateway.callbackUrl || '';
  if (form.ipnId) form.ipnId.value = gateway.ipnId || '';
  if (form.consumerKey) form.consumerKey.value = '';
  if (form.consumerSecret) form.consumerSecret.value = '';
  billingSetText(
    '#billingGatewayNote',
    gateway.environment === 'simulated'
      ? 'Simulated mode keeps every test charge on this box - no merchant contract required. Add sandbox or live keys to charge real money.'
      : `Calling ${gateway.apiBase || 'Pesapal'} · key ${gateway.consumerKeyMasked || 'not set'} · IPN ${gateway.ipnId || 'not registered'}`
  );
}

function populateBillingSelects() {
  const simSelect = document.querySelector('#billingSimSelect');
  if (simSelect) {
    const current = simSelect.value;
    const options = billingState.inventory
      .filter((device) => String(device.status || '').toLowerCase() !== 'terminated')
      .map(
        (device) =>
          `<option value="${billingEsc(device.iccid)}">${billingEsc(device.device || device.iccid)} · ${billingEsc(device.iccid)}</option>`
      )
      .join('');
    simSelect.innerHTML = options || '<option value="">No SIMs available</option>';
    if (current) simSelect.value = current;
  }
  const rateSelect = document.querySelector('#billingRateSelect');
  if (rateSelect) {
    const current = rateSelect.value;
    rateSelect.innerHTML =
      '<option value="">Use the plan tariff</option>' +
      billingState.rates
        .filter((rate) => rate.active)
        .map(
          (rate) =>
            `<option value="${rate.id}">${billingEsc(rate.name)} · ${Number(rate.pricePerMb).toFixed(4)} ${billingEsc(rate.currency)}/MB</option>`
        )
        .join('');
    if (current) rateSelect.value = current;
  }
  const planSelect = document.querySelector('#billingPlanSelect');
  if (planSelect) {
    const current = planSelect.value;
    planSelect.innerHTML =
      '<option value="">All plans (default tariff)</option>' +
      billingState.plans.map((plan) => `<option value="${billingEsc(plan.name)}">${billingEsc(plan.name)}</option>`).join('');
    if (current) planSelect.value = current;
  }
  const customerSelect = document.querySelector('#billingCustomerSelect');
  if (customerSelect) {
    const current = customerSelect.value;
    const names = [];
    const pushName = (name) => {
      if (name && !names.includes(name)) names.push(name);
    };
    billingState.customers.forEach((customer) => pushName(customer.name));
    // A metered SIM can bill a name that is not in the customer registry yet,
    // so the picker also offers everyone who shows up in usage or invoices.
    ((billingState.summary || {}).customers || []).forEach((row) => pushName(row.customer));
    billingState.usage.forEach((row) => pushName(row.customer));
    billingState.invoices.forEach((invoice) => pushName(invoice.customer));
    customerSelect.innerHTML =
      '<option value="">Select a customer…</option>' +
      names.map((name) => `<option value="${billingEsc(name)}">${billingEsc(name)}</option>`).join('');
    if (current) customerSelect.value = current;
    else if (!billingAdmin() && billingScope()) customerSelect.value = billingScope();
  }
  const payInvoice = document.querySelector('#billingPayInvoice');
  if (payInvoice) {
    const current = payInvoice.value;
    payInvoice.innerHTML =
      '<option value="">Top-up credit (no invoice)</option>' +
      billingState.invoices
        .filter((invoice) => invoice.status !== 'paid' && invoice.status !== 'void' && invoice.balance > 0)
        .map((invoice) => `<option value="${invoice.id}">${billingEsc(invoice.number)} · ${billingMoney(invoice.balance, invoice.currency)}</option>`)
        .join('');
    if (current) payInvoice.value = current;
  }
}

function renderBillingPayResult(payment) {
  const panel = document.querySelector('#billingPayResult');
  if (!panel) return;
  if (!payment) {
    panel.hidden = true;
    panel.innerHTML = '';
    return;
  }
  const checkout = payment.redirectUrl || payment.checkoutUrl || '';
  panel.hidden = false;
  panel.innerHTML = `<div class="billing-pay-card">
    <div class="billing-pay-head"><strong>${billingEsc(payment.reference)}</strong>${billingBadge(payment.status)}</div>
    <p>${billingMoney(payment.amount, 'USD')} · charged as ${billingMoney(payment.amountLocal, payment.currency)} · ${billingEsc(payment.mode)} mode</p>
    <div class="billing-actions">
      ${checkout ? `<a class="btn btn-primary" href="${billingEsc(checkout)}" target="_blank" rel="noreferrer"><i data-lucide="external-link"></i>Open Pesapal checkout</a>` : ''}
      <button class="btn btn-secondary" type="button" data-billing-payment-refresh="${payment.id}">Check status</button>
      ${payment.status === 'pending' ? `<button class="btn btn-secondary" type="button" data-billing-payment-cancel="${payment.id}">Cancel</button>` : ''}
    </div>
    <p class="muted">${billingEsc(payment.statusMessage || '')}</p>
  </div>`;
  if (typeof icons === 'function') icons();
}

async function submitBillingPayment(event) {
  event.preventDefault();
  const form = event.target;
  const data = new FormData(form);
  const payload = {
    email: String(data.get('email') || '').trim(),
    phone: String(data.get('phone') || '').trim(),
    name: String(data.get('name') || '').trim(),
    description: String(data.get('description') || '').trim(),
  };
  const invoiceId = String(data.get('invoiceId') || '').trim();
  if (invoiceId) {
    payload.invoiceId = Number(invoiceId);
  } else {
    const picker = document.querySelector('#billingCustomerSelect');
    payload.customer =
      billingScope() || (picker && picker.value) || (currentUser && currentUser.customerName) || 'Unassigned';
  }
  const amount = String(data.get('amountUsd') || '').trim();
  if (amount) payload.amountUsd = Number(amount);
  const submit = form.querySelector('button[type="submit"]');
  if (submit) submit.disabled = true;
  try {
    const payment = await authRequest('/billing/payments', { method: 'POST', body: JSON.stringify(payload) });
    billingState.lastPayment = payment;
    renderBillingPayResult(payment);
    toast(`Pesapal order ${payment.reference} created · ${payment.status}`);
    if (payment.mode === 'simulated' && payment.redirectUrl) {
      window.open(payment.redirectUrl, '_blank', 'noopener');
    }
    await loadBillingData();
  } catch (error) {
    toast(error.message || 'Unable to start the Pesapal payment', 'error');
  } finally {
    if (submit) submit.disabled = false;
  }
}

async function refreshBillingPayment(paymentId) {
  try {
    const payment = await authRequest(`/billing/payments/${paymentId}?refresh=true`);
    renderBillingPayResult(payment);
    toast(`Payment ${payment.reference} is ${payment.status}`);
    await loadBillingData();
  } catch (error) {
    toast(error.message || 'Unable to refresh the payment', 'error');
  }
}

async function cancelBillingPayment(paymentId) {
  try {
    const payment = await authRequest(`/billing/payments/${paymentId}/cancel`, { method: 'POST' });
    renderBillingPayResult(payment);
    toast(`Payment ${payment.reference} cancelled`);
    await loadBillingData();
  } catch (error) {
    toast(error.message || 'Unable to cancel the payment', 'error');
  }
}

async function submitBillingUsage(event) {
  event.preventDefault();
  const data = new FormData(event.target);
  const payload = {
    iccid: String(data.get('iccid') || '').trim(),
    megabytes: Number(data.get('megabytes') || 0),
  };
  const rateId = String(data.get('rateId') || '').trim();
  if (rateId) payload.rateId = Number(rateId);
  if (!payload.iccid) {
    toast('Select a SIM to meter', 'error');
    return;
  }
  try {
    const result = await authRequest('/billing/usage', { method: 'POST', body: JSON.stringify(payload) });
    const record = (result.records || [])[0] || {};
    toast(`${billingMb(record.megabytes)} metered · ${billingMoney(record.amount, record.currency)} booked`);
    await loadBillingData();
  } catch (error) {
    toast(error.message || 'Unable to record metered usage', 'error');
  }
}

async function simulateBillingUsage() {
  try {
    const result = await authRequest('/billing/usage/simulate', {
      method: 'POST',
      body: JSON.stringify({ megabytes: 25 }),
    });
    toast(`Metered 25 MB on ${result.sims} SIMs · ${billingMoney(result.totalAmount, result.currency)} booked`);
    await loadBillingData();
  } catch (error) {
    toast(error.message || 'Unable to simulate usage', 'error');
  }
}

async function submitBillingRate(event) {
  event.preventDefault();
  const form = event.target;
  const data = new FormData(form);
  const payload = {
    name: String(data.get('name') || '').trim(),
    pricePerMb: Number(data.get('pricePerMb') || 0),
    blockMb: Number(data.get('blockMb') || 0.01),
    minCharge: Number(data.get('minCharge') || 0),
    planName: String(data.get('planName') || '').trim(),
  };
  if (!payload.name) {
    toast('Give the rate a name', 'error');
    return;
  }
  try {
    const rate = await authRequest('/billing/rates', { method: 'POST', body: JSON.stringify(payload) });
    toast(`Rate "${rate.name}" saved at ${Number(rate.pricePerMb).toFixed(4)} ${rate.currency}/MB`);
    form.reset();
    form.blockMb.value = '0.01';
    form.minCharge.value = '0.01';
    await loadBillingData();
  } catch (error) {
    toast(error.message || 'Unable to save the rate', 'error');
  }
}

async function toggleBillingRate(rateId, active) {
  try {
    const rate = await authRequest(`/billing/rates/${rateId}`, {
      method: 'PATCH',
      body: JSON.stringify({ active: !active }),
    });
    toast(`Rate "${rate.name}" ${rate.active ? 're-activated' : 'paused'}`);
    await loadBillingData();
  } catch (error) {
    toast(error.message || 'Unable to update the rate', 'error');
  }
}

async function submitBillingInvoice(event) {
  event.preventDefault();
  const data = new FormData(event.target);
  const payload = {
    customer: String(data.get('customer') || '').trim(),
    taxPct: Number(data.get('taxPct') || 0),
    platformFeePct: Number(data.get('platformFeePct') || 0),
    dueDays: Number(data.get('dueDays') || 7),
  };
  if (!payload.customer) {
    toast('Select the customer to invoice', 'error');
    return;
  }
  try {
    const invoice = await authRequest('/billing/invoices', { method: 'POST', body: JSON.stringify(payload) });
    toast(`${invoice.number} issued for ${billingMoney(invoice.total, invoice.currency)}`);
    await loadBillingData();
  } catch (error) {
    toast(error.message || 'Unable to generate the invoice', 'error');
  }
}

async function voidBillingInvoice(invoiceId) {
  try {
    const invoice = await authRequest(`/billing/invoices/${invoiceId}/void`, {
      method: 'POST',
      body: JSON.stringify({ reason: 'Voided from the billing console' }),
    });
    toast(`${invoice.number} voided - usage released back to unbilled`);
    await loadBillingData();
  } catch (error) {
    toast(error.message || 'Unable to void the invoice', 'error');
  }
}

async function saveBillingGateway(event) {
  event.preventDefault();
  const form = event.target;
  const data = new FormData(form);
  const payload = {
    environment: String(data.get('environment') || 'simulated'),
    currency: String(data.get('currency') || 'USD'),
    fxRate: Number(data.get('fxRate') || 1),
    ipnUrl: String(data.get('ipnUrl') || '').trim(),
    callbackUrl: String(data.get('callbackUrl') || '').trim(),
    ipnId: String(data.get('ipnId') || '').trim(),
  };
  const key = String(data.get('consumerKey') || '').trim();
  const secret = String(data.get('consumerSecret') || '').trim();
  if (key) payload.consumerKey = key;
  if (secret) payload.consumerSecret = secret;
  try {
    const gateway = await authRequest('/billing/gateway', { method: 'PATCH', body: JSON.stringify(payload) });
    billingState.gateway = gateway;
    renderBillingGateway();
    toast(`Pesapal gateway saved · ${gateway.mode} mode`);
    await loadBillingData();
  } catch (error) {
    toast(error.message || 'Unable to save the Pesapal settings', 'error');
  }
}

async function testBillingGateway() {
  try {
    const result = await authRequest('/billing/gateway/test', { method: 'POST' });
    toast(`Pesapal ${result.mode} gateway online${result.latencyMs ? ` (${result.latencyMs} ms)` : ''}`);
  } catch (error) {
    toast(error.message || 'Pesapal gateway test failed', 'error');
  }
}

async function registerBillingIpn() {
  try {
    const result = await authRequest('/billing/gateway/register-ipn', { method: 'POST' });
    toast(`IPN registered · ${result.ipnId}`);
    await loadBillingData();
  } catch (error) {
    toast(error.message || 'Unable to register the IPN URL', 'error');
  }
}

/* ------------------------------------------------------------------ wiring */

function billingOn(selector, event, handler) {
  const node = document.querySelector(selector);
  if (node) node.addEventListener(event, handler);
}

billingOn('[data-view="billing"]', 'click', () => loadBillingData());
billingOn('#billingRefresh', 'click', () => loadBillingData());
billingOn('#billingRateForm', 'submit', submitBillingRate);
billingOn('#billingUsageForm', 'submit', submitBillingUsage);
billingOn('#billingSimulate', 'click', simulateBillingUsage);
billingOn('#billingInvoiceForm', 'submit', submitBillingInvoice);
billingOn('#billingPayForm', 'submit', submitBillingPayment);
billingOn('#billingGatewayForm', 'submit', saveBillingGateway);
billingOn('#billingGatewayTest', 'click', testBillingGateway);
billingOn('#billingGatewayIpn', 'click', registerBillingIpn);

document.addEventListener('click', async (event) => {
  const rate = event.target.closest('[data-billing-rate]');
  if (rate) {
    await toggleBillingRate(Number(rate.dataset.billingRate), rate.dataset.billingRateActive === '1');
    return;
  }
  const pay = event.target.closest('[data-billing-pay]');
  if (pay) {
    const picker = document.querySelector('#billingPayInvoice');
    if (picker) {
      picker.value = String(pay.dataset.billingPay);
      const amountField = document.querySelector('#billingPayAmount');
      if (amountField) amountField.value = '';
    }
    const form = document.querySelector('#billingPayForm');
    if (form) {
      form.scrollIntoView({ behavior: 'smooth', block: 'center' });
      const email = form.querySelector('input[name="email"]');
      if (email && !email.value) email.focus();
    }
    return;
  }
  const voidBtn = event.target.closest('[data-billing-void]');
  if (voidBtn) {
    await voidBillingInvoice(Number(voidBtn.dataset.billingVoid));
    return;
  }
  const open = event.target.closest('[data-billing-payment-open]');
  if (open) {
    window.open(open.dataset.billingPaymentOpen, '_blank', 'noopener');
    return;
  }
  const refresh = event.target.closest('[data-billing-payment-refresh]');
  if (refresh) {
    await refreshBillingPayment(Number(refresh.dataset.billingPaymentRefresh));
    return;
  }
  const cancel = event.target.closest('[data-billing-payment-cancel]');
  if (cancel) {
    await cancelBillingPayment(Number(cancel.dataset.billingPaymentCancel));
  }
});
