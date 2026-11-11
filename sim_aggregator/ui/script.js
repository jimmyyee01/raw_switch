// script.js – dashboard UI for the Multi‑Carrier SIM Aggregator
const API_BASE = document.getElementById('apiBase').textContent.trim();
let devices = [];

async function fetchDevices() {
  const res = await fetch(`${API_BASE}/devices/`);
  if (!res.ok) throw new Error('Failed to load devices');
  devices = await res.json();
  renderDeviceTable();
  populateSwitchSelect();
  updateBandwidthChart();
}

function renderDeviceTable() {
  const tbody = document.getElementById('deviceTable');
  tbody.innerHTML = '';
  devices.forEach(d => {
    const lastEvent = d.last_network_event;
    const row = document.createElement('tr');
    row.innerHTML = `
      <td>${d.iccid}</td>
      <td>${d.current_carrier || '—'}</td>
      <td>${d.status || '—'}</td>
      <td>${lastEvent ? lastEvent.bandwidth_mbps.toFixed(1) : '—'}</td>
      <td>${lastEvent ? lastEvent.latency_ms.toFixed(1) : '—'}</td>
      <td>${lastEvent ? new Date(lastEvent.timestamp).toLocaleString() : '—'}</td>
      <td>
        <button class="smallBtn" data-iccid="${d.iccid}">Switch</button>
      </td>
    `;
    tbody.appendChild(row);
  });
  // attach switch listeners
  document.querySelectorAll('.smallBtn').forEach(btn => {
    btn.addEventListener('click', async () => {
      const iccid = btn.dataset.iccid;
      await triggerSwitch(iccid);
    });
  });
}

function populateSwitchSelect() {
  const select = document.getElementById('switchDevice');
  select.innerHTML = '<option value="">— Choose device —</option>';
  devices.forEach(d => {
    const opt = document.createElement('option');
    opt.value = d.iccid;
    opt.textContent = d.iccid;
    select.appendChild(opt);
  });
  document.getElementById('switchPanel').style.display = devices.length ? 'block' : 'none';
}

async function triggerSwitch(iccid) {
  const device = devices.find(d => d.iccid === iccid);
  if (!device) return alert('Device not found');
  const body = { iccid };
  const res = await fetch(`${API_BASE}/devices/${iccid}/switch`, {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify(body)
  });
  if (!res.ok) {
    const err = await res.text();
    return alert('Switch failed: ' + err);
  }
  const data = await res.json();
  alert(data.message || 'Switch initiated');
  fetchDevices(); // refresh
}

// --- init ---
fetchDevices().catch(err => console.error('UI error:', err));
// Auto‑refresh device table every 15 seconds
setInterval(fetchDevices, 15000);
// Initialize bandwidth chart (will be updated as data arrives)
initBandwidthChart();
// Keep last 10 bandwidth readings per device for charting
let esimProfiles = [];

async function fetchEsimProfiles() {
  const res = await fetch(`${API_BASE}/esim/profiles`);
  if (!res.ok) return console.error('Failed to load eSIM profiles');
  esimProfiles = await res.json();
  renderEsimTable(esimProfiles);
  updateStatusChart(esimProfiles);
  document.getElementById('esimPanel').style.display = 'block';
}

function renderEsimTable(profiles) {
  const tbody = document.getElementById('esimTable').querySelector('tbody');
  tbody.innerHTML = '';
  profiles.forEach(p => {
    const device = devices.find(d => d.iccid === p.iccid);
    const carrier = device ? device.carrier.name : p.carrier_name;
    const row = document.createElement('tr');
    row.innerHTML = `
      <td>${p.iccid}</td>
      <td>${carrier}</td>
      <td><span class="statusBadge" data-status="${p.status}">${p.status}</span></td>
      <td>${p.activated_at ? new Date(p.activated_at).toLocaleString() : '—'}</td>
      <td>${p.deactivated_at ? new Date(p.deactivated_at).toLocaleString() : '—'}</td>
      <td>
        <button class="smallBtn" data-iccid="${p.iccid}" data-action="deactivate">Deactivate</button>
      </td>
    `;
    tbody.appendChild(row);
  });
  // attach deactivate listeners
  document.querySelectorAll('.smallBtn[data-action="deactivate"]').forEach(btn => {
    btn.addEventListener('click', async () => {
      const iccid = btn.dataset.iccid;
      if (confirm(`Deactivate eSIM profile for ICCID ${iccid}?`)) {
        const res = await fetch(`${API_BASE}/esim/deactivate`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ iccid })
        });
        if (!res.ok) return alert('Deactivate failed: ' + await res.text());
        alert('Profile deactivated');
        fetchEsimProfiles(); // refresh
      }
    });
  });
}

function initStatusChart() {
  const ctx = document.getElementById('statusChart').getContext('2d');
  window.statusChart = new Chart(ctx, {
    type: 'pie',
    data: {
      labels: ['provisioned', 'active', 'deactivated', 'failed'],
      datasets: [{
        label: 'Profile Status',
        data: [0, 0, 0, 0],
        backgroundColor: ['rgba(75, 192, 192, 0.5)', 'rgba(255, 206, 86, 0.5)', 'rgba(255, 99, 132, 0.5)', 'rgba(153, 102, 255, 0.5)']
      }],
      options: {
        responsive: true,
        plugins: {
          legend: { position: 'bottom' }
        }
      }
    }
  });
}

function updateStatusChart(profiles) {
  const counts = { provisioned: 0, active: 0, deactivated: 0, failed: 0 };
  profiles.forEach(p => {
    const s = p.status && counts.hasOwnProperty(p.status) ? p.status : 'provisioned';
    counts[s]++;
  });
  if (window.statusChart) {
    window.statusChart.data.datasets[0].data = [
      counts.provisioned,
      counts.active,
      counts.deactivated,
      counts.failed
    ];
    window.statusChart.update();
  }
}

// integrate into the initial load
fetchEsimProfiles().catch(err => console.error('eSIM UI error:', err));
const deviceBandwidthHistory = {};

function initBandwidthChart() {
  const ctx = document.getElementById('bandwidthChart').getContext('2d');
  // We'll build the chart data after the first fetch; start with empty data
  window.bandwidthChart = new Chart(ctx, {
    type: 'bar',
    data: {
      labels: [],
      datasets: [{
        label: 'Avg Bandwidth (Mbps)',
        data: [],
        backgroundColor: 'rgba(54, 162, 235, 0.5)',
        borderColor: 'rgba(54, 162, 235, 1)',
        borderWidth: 1
      }]
    },
    options: {
      responsive: true,
      scales: {
        y: { beginAtZero: true, title: { display: true, text: 'Mbps' } },
        x: { title: { display: true, text: 'Device ICCID' } }
      },
      plugins: {
        legend: { display: false }
      }
    }
  });
}

function updateBandwidthChart() {
  // Compute average bandwidth per device from the last fetched data
  const avgData = {};
  devices.forEach(d => {
    const hist = deviceBandwidthHistory[d.iccid] || [];
    const avg = hist.length ? hist.reduce((a,b) => a + b, 0) / hist.length : 0;
    avgData[d.iccid] = avg;
    // keep only last 10 entries
    if (hist.length > 10) hist.shift();
    hist.push(d.last_network_event ? d.last_network_event.bandwidth_mbps : 0);
    deviceBandwidthHistory[d.iccid] = hist;
  });
  const labels = Object.keys(avgData);
  const data = Object.values(avgData);
  if (window.bandwidthChart) {
    window.bandwidthChart.data.labels = labels;
    window.bandwidthChart.data.datasets[0].data = data;
    window.bandwidthChart.update();
  }
}

// In fetchDevices, after rendering table, call updateBandwidthChart