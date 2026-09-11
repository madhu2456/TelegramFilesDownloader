// TeleVault Frontend Engine: WebSocket, Auth, Execution, and Media Gallery
let token = '';
let ws = null;
let currentDialogs = [];

// 1. Initialize Token & URL cleanup
(function initAuthToken() {
  const params = new URLSearchParams(window.location.search);
  const t = params.get('token');
  if (t) {
    token = t;
    sessionStorage.setItem('tele_vault_token', t);
    window.history.replaceState({}, document.title, window.location.pathname);
  } else {
    token = sessionStorage.getItem('tele_vault_token') || '';
  }
})();

function getHeaders() {
  return {
    'Content-Type': 'application/json',
    'X-Auth-Token': token,
  };
}

// 2. WebSocket Connection
function connectWebSocket() {
  const loc = window.location;
  const proto = loc.protocol === 'https:' ? 'wss:' : 'ws:';
  const wsUrl = `${proto}//${loc.host}/ws/live?token=${encodeURIComponent(token)}`;

  ws = new WebSocket(wsUrl);
  const dot = document.getElementById('wsDot');
  const text = document.getElementById('wsText');

  ws.onopen = () => {
    dot.className = 'live-dot';
    text.innerText = 'Live';
  };

  ws.onclose = () => {
    dot.className = 'live-dot disconnected';
    text.innerText = 'Disconnected';
    setTimeout(connectWebSocket, 3000);
  };

  ws.onmessage = (event) => {
    try {
      const data = JSON.parse(event.data);
      handleWsEvent(data);
    } catch (e) {
      console.error(e);
    }
  };
}

function handleWsEvent(data) {
  if (data.type === 'INIT_STATE' || data.type === 'PROGRESS' || data.type === 'COMPLETED' || data.type === 'FAILED') {
    if (data.state) updateJobUI(data.state);
  }
  if (data.type === 'LOG') {
    appendTerminalLog(data.message);
  }
}

function updateJobUI(s) {
  document.getElementById('jobStatusText').innerText = `Status: ${s.status.toUpperCase()}`;
  document.getElementById('jobProgressText').innerText = `${s.progress}%`;
  document.getElementById('jobProgressBar').style.width = `${s.progress}%`;
  document.getElementById('speedText').innerText = (s.speed_mbps || 0).toFixed(1);

  // Update SVG speed gauge stroke (circumference = 377)
  const maxSpeed = 20.0;
  const pct = Math.min(1.0, (s.speed_mbps || 0) / maxSpeed);
  const offset = 377 - (377 * pct);
  document.getElementById('speedGauge').style.strokeDashoffset = offset;

  document.getElementById('jobDownloadedText').innerText = `Done: ${s.downloaded_msgs || 0}`;
  document.getElementById('jobSkippedText').innerText = `Skipped: ${s.skipped_msgs || 0}`;
  const mb = ((s.bytes_total || 0) / (1024 * 1024)).toFixed(1);
  document.getElementById('jobBytesText').innerText = `${mb} MB`;
}

function appendTerminalLog(msg) {
  const el = document.getElementById('terminalLogs');
  el.innerText += `\n${msg}`;
  el.scrollTop = el.scrollHeight;
}

// 3. User Auth Checks
async function checkAuth() {
  try {
    const res = await fetch(`/api/auth/me?token=${token}`, { headers: getHeaders() });
    const data = await res.json();
    if (data.authorized && data.user) {
      document.getElementById('userPill').innerText = `${data.user.name} (${data.user.phone})`;
      document.getElementById('authCard').style.display = 'none';
      loadDialogs('all');
    }
  } catch (e) {
    console.error('Auth check failed:', e);
  }
}

async function sendPhoneCode() {
  const phone = document.getElementById('phoneInput').value;
  const res = await fetch(`/api/auth/phone/send_code?token=${token}`, {
    method: 'POST',
    headers: getHeaders(),
    body: JSON.stringify({ phone }),
  });
  if (res.ok) {
    document.getElementById('codeSection').style.display = 'block';
  } else {
    alert('Failed to send code. Check phone format.');
  }
}

async function signInWithCode() {
  const phone = document.getElementById('phoneInput').value;
  const code = document.getElementById('codeInput').value;
  const res = await fetch(`/api/auth/phone/sign_in?token=${token}`, {
    method: 'POST',
    headers: getHeaders(),
    body: JSON.stringify({ phone, code }),
  });
  const data = await res.json();
  if (data.status === 'authorized') {
    checkAuth();
  } else if (data.status === '2fa_required') {
    const pwd = prompt('Enter Telegram 2FA Password:');
    if (pwd) {
      await fetch(`/api/auth/2fa?token=${token}`, {
        method: 'POST',
        headers: getHeaders(),
        body: JSON.stringify({ password: pwd }),
      });
      checkAuth();
    }
  }
}

// 4. Dialog Explorer
async function loadDialogs(kind) {
  const list = document.getElementById('dialogList');
  list.innerHTML = 'Loading...';
  try {
    const res = await fetch(`/api/dialogs?kind=${kind}&token=${token}`, { headers: getHeaders() });
    const data = await res.json();
    currentDialogs = data.dialogs || [];
    renderDialogs(currentDialogs);
  } catch (e) {
    list.innerHTML = 'Failed to load dialogs.';
  }
}

function renderDialogs(dialogs) {
  const list = document.getElementById('dialogList');
  if (!dialogs.length) {
    list.innerHTML = '<p style="color:var(--text-muted);font-size:0.85rem;">No dialogs found.</p>';
    return;
  }
  list.innerHTML = dialogs.map(d => `
    <div class="dialog-item" onclick="armTarget('${d.username ? '@' + d.username : d.id}')">
      <div>
        <div style="font-weight:600;font-size:0.9rem;">${d.name || 'Unnamed'}</div>
        <div style="font-size:0.75rem;color:var(--text-muted);">${d.kind.toUpperCase()} ${d.username ? '@' + d.username : ''}</div>
      </div>
      <button class="btn btn-cyan" style="padding:0.25rem 0.5rem;font-size:0.75rem;">Arm</button>
    </div>
  `).join('');
}

function filterDialogs() {
  const q = document.getElementById('dialogSearch').value.toLowerCase();
  const filtered = currentDialogs.filter(d => (d.name || '').toLowerCase().includes(q) || (d.username || '').toLowerCase().includes(q));
  renderDialogs(filtered);
}

function armTarget(t) {
  document.getElementById('targetInput').value = t;
}

// 5. Download Actions
async function startDownload() {
  const target = document.getElementById('targetInput').value;
  const limit = parseInt(document.getElementById('limitInput').value, 10);
  const filter = document.getElementById('filterSelect').value || null;
  const search = document.getElementById('searchInput').value || null;

  if (!target) {
    alert('Please specify a target or arm one from the explorer.');
    return;
  }

  const res = await fetch(`/api/download/start?token=${token}`, {
    method: 'POST',
    headers: getHeaders(),
    body: JSON.stringify({ target, limit, filter, search, sync: true }),
  });

  if (res.status === 409) {
    alert('A download job is already active!');
  } else if (!res.ok) {
    const err = await res.json();
    alert('Failed to start: ' + JSON.stringify(err));
  }
}

async function cancelDownload() {
  await fetch(`/api/download/cancel?token=${token}`, {
    method: 'POST',
    headers: getHeaders(),
  });
}

// 6. Media Gallery
async function loadMedia() {
  const grid = document.getElementById('mediaGrid');
  try {
    const res = await fetch(`/api/media?limit=50&token=${token}`, { headers: getHeaders() });
    const data = await res.json();
    if (!data.items || !data.items.length) {
      grid.innerHTML = '<p style="color:var(--text-muted);font-size:0.85rem;">No media downloaded yet.</p>';
      return;
    }
    grid.innerHTML = data.items.map(item => `
      <div class="media-card">
        <div class="media-thumb">
          ${item.kind === 'video' ? `<video src="${item.stream_url}?token=${token}" preload="none" controls style="width:100%;height:100%;object-fit:cover;"></video>` : `<span style="font-size:2rem;">📁</span>`}
        </div>
        <div class="media-meta">
          <div class="name" title="${item.filename}">${item.filename}</div>
          <div style="color:var(--text-muted);">${(item.size / (1024 * 1024)).toFixed(2)} MB • ${item.kind.toUpperCase()}</div>
        </div>
      </div>
    `).join('');
  } catch (e) {
    grid.innerHTML = 'Failed to load media.';
  }
}

async function updateStorage() {
  try {
    const res = await fetch(`/api/system/storage?token=${token}`, { headers: getHeaders() });
    const data = await res.json();
    const freeGb = (data.free_bytes / (1024 * 1024 * 1024)).toFixed(1);
    const totalGb = (data.total_bytes / (1024 * 1024 * 1024)).toFixed(1);
    document.getElementById('storagePill').innerText = `Disk: ${freeGb}G / ${totalGb}G free (${data.used_percent}% used)`;
  } catch (e) {}
}

function copyToClipboard(text) {
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text);
  }
}

window.onload = () => {
  connectWebSocket();
  checkAuth();
  updateStorage();
  loadMedia();
};
