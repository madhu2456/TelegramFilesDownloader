function toggleNoLimit(checked) {
  const input = document.getElementById('limitInput');
  if (!input) return;
  if (checked) {
    input.dataset.prevValue = input.value;
    input.value = '';
    input.disabled = true;
    input.placeholder = '∞ All Messages (unlimited)';
  } else {
    input.disabled = false;
    input.value = input.dataset.prevValue || '100';
    input.placeholder = 'e.g. 100 or toggle No Limit';
  }
}

// TeleVault Modern Web Dashboard Frontend Engine
let token = '';
let ws = null;
let currentDialogs = [];
let activeLightboxItem = null;
window._lastActiveElement = null;

// 1. Initialize Token & URL Cleanup
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
  if (!token) {
    setTimeout(() => {
      showToast(
        'Authentication token required. Please use the link printed in your terminal: http://127.0.0.1:8000/?token=...',
        'warning',
        'Token Required',
        8000
      );
    }, 100);
  }
})();

function getHeaders() {
  return {
    'Content-Type': 'application/json',
    'X-Auth-Token': token,
  };
}

// 2. Linear-Grade Floating Toast Notification Stack
function showToast(message, type = 'info', title = null, duration = 3800) {
  const container = document.getElementById('toastContainer');
  if (!container) return;

  // Enforce 3-toast maximum stack to prevent viewport flooding (Critic ELEV-02)
  while (container.children.length >= 3) {
    const oldest = container.firstElementChild;
    if (oldest._timer) clearTimeout(oldest._timer);
    oldest.remove();
  }

  const toast = document.createElement('div');
  toast.className = `toast toast-${type}`;

  const icons = {
    success: `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#10B981" stroke-width="2"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/></svg>`,
    error: `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#EF4444" stroke-width="2"><circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/><line x1="9" y1="9" x2="15" y2="15"/></svg>`,
    warning: `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#F59E0B" stroke-width="2"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>`,
    info: `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#00E5FF" stroke-width="2"><circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/></svg>`
  };

  const titles = {
    success: title || 'Success',
    error: title || 'Error',
    warning: title || 'Notice',
    info: title || 'Info'
  };

  toast.innerHTML = `
    <div class="toast-icon">${icons[type] || icons.info}</div>
    <div class="toast-content">
      <div class="toast-title">${titles[type]}</div>
      <div class="toast-message">${message}</div>
    </div>
    <button class="toast-close" aria-label="Dismiss">&times;</button>
  `;

  const dismiss = () => {
    if (toast._timer) clearTimeout(toast._timer);
    toast.classList.add('fade-out');
    setTimeout(() => { if (toast.parentNode) toast.remove(); }, 200);
  };

  toast.querySelector('.toast-close').onclick = dismiss;
  toast._timer = setTimeout(dismiss, duration);
  container.appendChild(toast);
}

// 3. Reconnecting WebSocket Telemetry
function connectWebSocket() {
  const dot = document.getElementById('wsDot');
  const text = document.getElementById('wsText');

  if (!token) {
    if (dot) dot.className = 'live-dot disconnected';
    if (text) text.innerText = 'No Token';
    return;
  }

  const loc = window.location;
  const proto = loc.protocol === 'https:' ? 'wss:' : 'ws:';
  const wsUrl = `${proto}//${loc.host}/ws/live?token=${encodeURIComponent(token)}`;

  ws = new WebSocket(wsUrl);

  ws.onopen = () => {
    if (dot) dot.className = 'live-dot';
    if (text) text.innerText = 'Live';
  };

  ws.onclose = (event) => {
    if (dot) dot.className = 'live-dot disconnected';
    if (event && (event.code === 1008 || event.code === 4403 || event.code === 1003)) {
      sessionStorage.removeItem('tele_vault_token');
      token = '';
      if (text) text.innerText = 'Session Expired';
      showToast(
        'Session token invalid or expired. Please close this tab or open the fresh link printed in your terminal.',
        'error',
        'Session Expired',
        10000
      );
      return;
    }
    if (text) text.innerText = 'Reconnecting...';
    setTimeout(connectWebSocket, 3000);
  };

  ws.onmessage = (event) => {
    try {
      const data = JSON.parse(event.data);
      handleWsEvent(data);
    } catch (e) {
      console.error('WS Parse Error:', e);
    }
  };
}

function handleWsEvent(data) {
  if (data.type === 'SESSION_EXPIRED' || data.type === 'ORIGIN_FORBIDDEN') {
    sessionStorage.removeItem('tele_vault_token');
    token = '';
    const dot = document.getElementById('wsDot');
    const text = document.getElementById('wsText');
    if (dot) dot.className = 'live-dot disconnected';
    if (text) text.innerText = 'Session Expired';
    const msg = data.type === 'ORIGIN_FORBIDDEN'
      ? 'WebSocket connection rejected: Origin not allowed.'
      : 'Session token invalid or expired. Please click the fresh link printed in your terminal.';
    showToast(msg, 'error', 'Session Expired', 10000);
    if (ws) {
      ws.onclose = null;
      try { ws.close(1000); } catch (e) {}
    }
    return;
  }
  if (data.type === 'INIT_STATE' || data.type === 'PROGRESS' || data.type === 'COMPLETED' || data.type === 'FAILED') {
    if (data.state) updateJobUI(data.state);
  }
  if (data.type === 'LOG') {
    appendTerminalLog(data.message);
  }
  if (data.type === 'FLOOD_WAIT') {
    showToast(`Telegram rate limit active: waiting ${data.seconds}s`, 'warning', 'FloodWait Active', 5000);
  }
  if (data.type === 'COMPLETED') {
    showToast(`Download completed! Processed: ${data.result?.done || 0}, Skipped: ${data.result?.skipped || 0}`, 'success', 'Task Completed');
    loadMedia();
    updateStorage();
  }
}

function updateJobUI(s) {
  document.getElementById('jobStatusText').innerText = `Status: ${s.status.toUpperCase()}`;

  const isUnlimited = s.total_msgs === null || s.is_unlimited;
  const bar = document.getElementById('jobProgressBar');

  if (isUnlimited) {
    document.getElementById('jobProgressText').innerText = s.status === 'running' ? 'Uncapped (Streaming)' : `${s.progress}%`;
    if (s.status === 'running') {
      bar.style.width = '100%';
      bar.classList.add('progress-indeterminate');
      bar.classList.remove('progress-active-bar');
    } else {
      bar.classList.remove('progress-indeterminate');
      bar.style.width = `${s.progress}%`;
    }
  } else {
    document.getElementById('jobProgressText').innerText = `${s.progress}%`;
    bar.style.width = `${s.progress}%`;
    bar.classList.remove('progress-indeterminate');
    if (s.status === 'running') {
      bar.classList.add('progress-active-bar');
    } else {
      bar.classList.remove('progress-active-bar');
    }
  }

  document.getElementById('speedText').innerText = (s.speed_mbps || 0).toFixed(1);

  // Speedometer progress arc (circumference = 377)
  const maxSpeed = 25.0;
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

// 4. Telegram Account & Auth Wizard
async function checkAuth() {
  try {
    const res = await fetch(`/api/auth/me?token=${token}`, { headers: getHeaders() });
    const data = await res.json();
    if (data.authorized && data.user) {
      document.getElementById('userPill').innerText = `${data.user.name || 'User'} (${data.user.phone || 'Masked'})`;
      document.getElementById('authCard').style.display = 'none';
      loadDialogs('all');
    } else {
      document.getElementById('userPill').innerText = 'Unauthenticated';
      document.getElementById('authCard').style.display = 'block';
    }
  } catch (e) {
    console.error('Auth Check Error:', e);
  }
}

function switchAuthTab(tab) {
  const phoneBtn = document.getElementById('tabBtnPhone');
  const qrBtn = document.getElementById('tabBtnQr');
  const phoneTab = document.getElementById('phoneAuthTab');
  const qrTab = document.getElementById('qrAuthTab');

  if (tab === 'phone') {
    phoneBtn.classList.add('active');
    qrBtn.classList.remove('active');
    phoneTab.style.display = 'block';
    qrTab.style.display = 'none';
  } else {
    qrBtn.classList.add('active');
    phoneBtn.classList.remove('active');
    qrTab.style.display = 'block';
    phoneTab.style.display = 'none';
    loadQrCode();
  }
}

async function loadQrCode() {
  const container = document.getElementById('qrContainer');
  container.innerHTML = '<div style="color:#000; padding:2rem 0; font-size:0.85rem;">Generating QR Code...</div>';
  try {
    const res = await fetch(`/api/auth/qr?token=${token}`, { headers: getHeaders() });
    const data = await res.json();
    if (data.url) {
      container.innerHTML = `
        <img src="https://api.qrserver.com/v1/create-qr-code/?size=180x180&data=${encodeURIComponent(data.url)}" alt="Telegram QR" style="width:180px; height:180px; display:block;">
        <div style="font-size:0.75rem; color:#666; margin-top:0.5rem;">Expires: ${new Date(data.expires).toLocaleTimeString()}</div>
      `;
    } else {
      container.innerHTML = '<div style="color:#e11d48; padding:2rem 0;">QR generation unavailable.</div>';
    }
  } catch (e) {
    container.innerHTML = '<div style="color:#e11d48; padding:2rem 0;">Failed to load QR code.</div>';
  }
}

async function sendPhoneCode() {
  const phone = document.getElementById('phoneInput').value.trim();
  if (!phone) {
    showToast('Please enter your phone number in E.164 format (e.g. +15551234567).', 'warning');
    return;
  }
  try {
    const res = await fetch(`/api/auth/phone/send_code?token=${token}`, {
      method: 'POST',
      headers: getHeaders(),
      body: JSON.stringify({ phone }),
    });
    if (res.ok) {
      document.getElementById('codeSection').style.display = 'block';
      showToast(`Verification code sent to ${phone}`, 'success');
      document.getElementById('codeInput').focus();
    } else {
      const err = await res.json();
      showToast(err.detail || 'Failed to send verification code. Check phone format.', 'error');
    }
  } catch (e) {
    showToast('Network error while requesting code.', 'error');
  }
}

async function signInWithCode() {
  const phone = document.getElementById('phoneInput').value.trim();
  const code = document.getElementById('codeInput').value.trim();
  if (!code) {
    showToast('Please enter the verification code received on Telegram.', 'warning');
    return;
  }
  try {
    const res = await fetch(`/api/auth/phone/sign_in?token=${token}`, {
      method: 'POST',
      headers: getHeaders(),
      body: JSON.stringify({ phone, code }),
    });
    const data = await res.json();
    if (data.status === 'authorized') {
      showToast('Successfully logged in!', 'success');
      checkAuth();
    } else if (data.status === '2fa_required') {
      openTwoFactorModal();
    } else {
      showToast(data.detail || 'Sign-in failed. Please verify code.', 'error');
    }
  } catch (e) {
    showToast('Network error during sign in.', 'error');
  }
}

// 5. 2FA Cloud Password Modal
function openTwoFactorModal() {
  window._lastActiveElement = document.activeElement;
  const modal = document.getElementById('twoFactorModal');
  modal.style.display = 'flex';
  const input = document.getElementById('twoFactorInput');
  input.value = '';
  input.focus();
}

function closeTwoFactorModal() {
  document.getElementById('twoFactorModal').style.display = 'none';
  if (window._lastActiveElement) {
    window._lastActiveElement.focus();
  }
}

function togglePasswordVisibility(inputId, btn) {
  const input = document.getElementById(inputId);
  if (input.type === 'password') {
    input.type = 'text';
    btn.innerHTML = `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#00E5FF" stroke-width="2"><path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"/><line x1="1" y1="1" x2="23" y2="23"/></svg>`;
  } else {
    input.type = 'password';
    btn.innerHTML = `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>`;
  }
}

async function submitTwoFactorPassword() {
  const pwd = document.getElementById('twoFactorInput').value;
  if (!pwd) {
    showToast('Please enter your 2FA password.', 'warning');
    return;
  }
  try {
    const res = await fetch(`/api/auth/2fa?token=${token}`, {
      method: 'POST',
      headers: getHeaders(),
      body: JSON.stringify({ password: pwd }),
    });
    if (res.ok) {
      closeTwoFactorModal();
      showToast('Two-factor authentication verified!', 'success');
      checkAuth();
    } else {
      const err = await res.json();
      showToast(err.detail || 'Incorrect 2FA password.', 'error');
    }
  } catch (e) {
    showToast('Network error during 2FA authentication.', 'error');
  }
}

// 6. Chat Explorer & Badges
async function loadDialogs(kind = 'all') {
  ['filterAll', 'filterChannel', 'filterGroup', 'filterDm'].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.classList.remove('active');
  });
  const activeId = kind === 'channel' ? 'filterChannel' : (kind === 'group' ? 'filterGroup' : (kind === 'dm' ? 'filterDm' : 'filterAll'));
  const activeBtn = document.getElementById(activeId);
  if (activeBtn) activeBtn.classList.add('active');

  const list = document.getElementById('dialogList');
  if (!list) return;

  if (!token) {
    list.innerHTML = `
      <div style="padding:1.5rem 1rem; text-align:center;">
        <div style="color:var(--accent-amber); font-size:0.85rem; margin-bottom:0.5rem;">Authentication token required.</div>
        <div style="color:var(--text-muted); font-size:0.8rem;">Please open the dashboard link printed in your terminal (containing ?token=...).</div>
      </div>
    `;
    return;
  }

  list.innerHTML = `
    <div style="padding:2rem 1rem; text-align:center; color:var(--text-muted); font-size:0.85rem; display:flex; flex-direction:column; align-items:center; justify-content:center; gap:0.75rem;">
      <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="var(--cyan-glow)" stroke-width="2.5" stroke-linecap="round">
        <circle cx="12" cy="12" r="10" stroke-opacity="0.25" />
        <path d="M12 2a10 10 0 0 1 10 10">
          <animateTransform attributeName="transform" type="rotate" from="0 12 12" to="360 12 12" dur="1s" repeatCount="indefinite"/>
        </path>
      </svg>
      <span>Fetching dialogs...</span>
    </div>
  `;

  try {
    const res = await fetch('/api/dialogs?kind=' + encodeURIComponent(kind) + '&token=' + encodeURIComponent(token), { headers: getHeaders() });
    if (res.status === 401) {
      list.innerHTML = `
        <div style="padding:1.5rem 1rem; text-align:center;">
          <div style="color:var(--accent-crimson); font-size:0.85rem; margin-bottom:0.5rem;">Session expired. Please click the fresh link printed in your terminal.</div>
        </div>
      `;
      return;
    }
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      const errMsg = err.detail || 'Failed to load dialogs.';
      list.innerHTML = `
        <div style="padding:1.5rem 1rem; text-align:center;">
          <div style="color:var(--accent-crimson); font-size:0.85rem; margin-bottom:0.5rem;">${errMsg}</div>
          <button class="btn btn-cyan" onclick="loadDialogs('${kind}')" style="margin-top:0.5rem; padding:0.35rem 0.8rem; font-size:0.8rem;">↻ Retry</button>
        </div>
      `;
      return;
    }
    const data = await res.json();
    currentDialogs = data.dialogs || [];
    renderDialogs(currentDialogs);
  } catch (e) {
    list.innerHTML = `
      <div style="padding:1.5rem 1rem; text-align:center;">
        <div style="color:var(--accent-crimson); font-size:0.85rem; margin-bottom:0.5rem;">Connection lost or server is offline. Please ensure TeleVault is running.</div>
        <button class="btn btn-cyan" onclick="loadDialogs('${kind}')" style="padding:0.35rem 0.8rem; font-size:0.8rem;">↻ Retry</button>
      </div>
    `;
  }
}

function renderDialogs(dialogs) {
  const list = document.getElementById('dialogList');
  if (!list) return;
  if (!dialogs.length) {
    list.innerHTML = `
      <div class="empty-state">
        <div class="empty-state-icon">
          <svg width="36" height="36" viewBox="0 0 24 24" fill="none" stroke="rgba(255,255,255,0.2)" stroke-width="1.5"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>
        </div>
        <div class="empty-state-title">No Dialogs Found</div>
        <div class="empty-state-desc">Try clearing search filters or logging into Telegram.</div>
      </div>
    `;
    return;
  }

  const badgeClass = { channel: 'badge-channel', group: 'badge-group', dm: 'badge-dm' };

  list.innerHTML = dialogs.map(d => {
    const kind = String(d.kind || d.type || 'group').toLowerCase();
    const rawHandle = d.handle ? d.handle : (d.username ? (d.username.startsWith('@') ? d.username : '@' + d.username) : '');
    const target = rawHandle || String(d.id || '');
    const displaySub = rawHandle || ('#' + (d.id || ''));
    const safeTarget = target.replace(/'/g, "\'");

    return `
    <div class="dialog-item" onclick="armTarget('${safeTarget}')">
      <div style="overflow:hidden; padding-right:0.5rem;">
        <div style="font-weight:600; font-size:0.88rem; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;">${d.name || 'Unnamed Chat'}</div>
        <div style="display:flex; align-items:center; gap:0.4rem; margin-top:0.25rem;">
          <span class="category-badge ${badgeClass[kind] || 'badge-channel'}">${kind.toUpperCase()}</span>
          <span style="font-size:0.75rem; color:var(--text-muted); font-family:var(--font-mono);">${displaySub}</span>
        </div>
      </div>
      <button class="btn btn-cyan" onclick="event.stopPropagation(); armTarget('${safeTarget}')" style="padding:0.4rem 0.8rem; font-size:0.78rem; min-height:36px; flex-shrink:0;">Arm</button>
    </div>
    `;
  }).join('');
}

function filterDialogs() {
  const searchInput = document.getElementById('dialogSearch');
  if (!searchInput) return;
  const q = searchInput.value.toLowerCase().trim();
  if (!q) {
    renderDialogs(currentDialogs);
    return;
  }
  const qClean = q.startsWith('@') ? q.slice(1) : q;
  const filtered = currentDialogs.filter(d => {
    const name = String(d.name || '').toLowerCase();
    const handle = String(d.handle || '').toLowerCase();
    const username = String(d.username || '').toLowerCase();
    const id = String(d.id || '');
    return (
      name.includes(q) ||
      handle.includes(q) ||
      (username && username.includes(qClean)) ||
      (handle && handle.replace(/^@/, '').includes(qClean)) ||
      id.includes(q)
    );
  });
  renderDialogs(filtered);
}

function armTarget(t) {
  document.getElementById('targetInput').value = t;
  showToast(`Armed target: ${t}`, 'info', 'Target Configured');
}

// 7. Download Management
async function startDownload() {
  const target = document.getElementById('targetInput').value.trim();
  const noLimit = document.getElementById('noLimitToggle')?.checked || false;
  const rawLimit = parseInt(document.getElementById('limitInput').value, 10);
  const limit = noLimit ? null : (isNaN(rawLimit) ? 100 : rawLimit);
  const filter = document.getElementById('filterSelect').value || null;
  const search = document.getElementById('searchInput').value.trim() || null;

  const sync = document.getElementById('syncToggle')?.checked ?? true;
  const resume = document.getElementById('resumeToggle')?.checked ?? true;
  const dry_run = document.getElementById('dryRunToggle')?.checked ?? false;
  const takeout = document.getElementById('takeoutToggle')?.checked ?? false;
  const join = document.getElementById('joinToggle')?.checked ?? false;

  if (!target) {
    showToast('Please specify a target or arm one from the explorer.', 'warning', 'Missing Target');
    return;
  }

  showToast(`Initiating download job for ${target}...`, 'info');

  try {
    const res = await fetch(`/api/download/start?token=${token}`, {
      method: 'POST',
      headers: getHeaders(),
      body: JSON.stringify({ target, limit, no_limit: noLimit, filter, search, sync, resume, dry_run, takeout, join }),
    });

    if (res.status === 409) {
      showToast('A download job is already active! Only 1 job permitted at a time.', 'error', 'Job Conflict (409)');
    } else if (res.ok) {
      const data = await res.json();
      showToast(`Job ${data.job_id} started successfully!`, 'success', 'Download Active');
    } else {
      const err = await res.json();
      showToast(err.detail || 'Download initiation failed.', 'error');
    }
  } catch (e) {
    showToast('Network error while starting download.', 'error');
  }
}

async function cancelDownload() {
  try {
    const res = await fetch(`/api/download/cancel?token=${token}`, {
      method: 'POST',
      headers: getHeaders(),
    });
    const data = await res.json();
    if (data.status === 'cancelling') {
      showToast('Cancellation requested. Checkpoint will commit safely.', 'warning', 'Cancelling Job');
    } else {
      showToast('No active download job to cancel.', 'info');
    }
  } catch (e) {
    showToast('Failed to issue cancel command.', 'error');
  }
}

// 8. Media Gallery & Theater Lightbox Modal
let mediaCache = [];

async function loadMedia() {
  const grid = document.getElementById('mediaGrid');
  try {
    const res = await fetch(`/api/media?limit=50&token=${token}`, { headers: getHeaders() });
    const data = await res.json();
    mediaCache = data.items || [];
    if (!mediaCache.length) {
      grid.innerHTML = `
        <div class="empty-state" style="grid-column: 1 / -1;">
          <div class="empty-state-icon">
            <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="rgba(255,255,255,0.2)" stroke-width="1.5"><rect x="2" y="3" width="20" height="14" rx="2" ry="2"/><line x1="8" y1="21" x2="16" y2="21"/><line x1="12" y1="17" x2="12" y2="21"/></svg>
          </div>
          <div class="empty-state-title">No Media Downloaded</div>
          <div class="empty-state-desc">Arm a target from the Chat Explorer and click Start Download to populate your vault.</div>
        </div>
      `;
      return;
    }

    grid.innerHTML = mediaCache.map((item, idx) => `
      <div class="media-card" onclick="openMediaLightboxByIndex(${idx})">
        <div class="media-thumb">
          ${item.kind === 'video'
            ? `<div style="display:flex; flex-direction:column; align-items:center; gap:0.3rem;"><svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="#00E5FF" stroke-width="2"><polygon points="5 3 19 12 5 21 5 3"/></svg><span style="font-size:0.65rem; color:var(--accent-cyan); font-weight:600;">STREAM</span></div>`
            : (item.kind === 'photo'
              ? `<img src="${item.stream_url}?token=${token}" loading="lazy" style="width:100%; height:100%; object-fit:cover;">`
              : `<span style="font-size:2.5rem;">📄</span>`
            )
          }
        </div>
        <div class="media-meta">
          <div class="name" title="${item.filename}">${item.filename}</div>
          <div style="display:flex; justify-content:space-between; color:var(--text-muted); font-size:0.75rem;">
            <span>${(item.size / (1024 * 1024)).toFixed(2)} MB</span>
            <span style="font-family:var(--font-mono); color:var(--accent-cyan);">${item.kind.toUpperCase()}</span>
          </div>
        </div>
      </div>
    `).join('');
  } catch (e) {
    grid.innerHTML = '<div style="color:var(--accent-crimson); padding:1rem; grid-column:1/-1;">Failed to load media manifest.</div>';
  }
}

function openMediaLightboxByIndex(idx) {
  const item = mediaCache[idx];
  if (!item) return;
  activeLightboxItem = item;
  window._lastActiveElement = document.activeElement;

  document.getElementById('lightboxTitle').innerText = item.filename;
  document.getElementById('lightboxSize').innerText = `${(item.size / (1024 * 1024)).toFixed(2)} MB (${item.size} bytes)`;
  document.getElementById('lightboxMime').innerText = item.mime || 'application/octet-stream';
  document.getElementById('lightboxMsgId').innerText = `#${item.msg_id || '--'}`;
  document.getElementById('lightboxSha').innerText = item.sha256 || 'N/A';
  document.getElementById('lightboxDownloadLink').href = `${item.stream_url}?token=${token}`;

  const container = document.getElementById('lightboxMediaContainer');
  if (item.kind === 'video') {
    container.innerHTML = `
      <video id="theaterVideo" controls autoplay preload="none" style="width:100%; height:100%; max-height:520px;">
        <source src="${item.stream_url}?token=${token}">
        Your browser does not support HTML5 video.
      </video>
    `;
  } else if (item.kind === 'photo') {
    container.innerHTML = `<img src="${item.stream_url}?token=${token}" style="max-width:100%; max-height:520px; object-fit:contain;">`;
  } else {
    container.innerHTML = `
      <div style="text-align:center; padding:3rem 1rem;">
        <div style="font-size:3.5rem; margin-bottom:1rem;">📄</div>
        <div style="font-size:0.9rem; font-weight:600; color:var(--text-main);">${item.filename}</div>
        <div style="font-size:0.8rem; color:var(--text-muted); margin-top:0.4rem;">Binary document ready for download.</div>
      </div>
    `;
  }

  document.getElementById('mediaLightboxModal').style.display = 'flex';
}

// Explicit Video Pause & Source Detachment Teardown (Critic ELEV-01)
function closeMediaLightbox() {
  const modal = document.getElementById('mediaLightboxModal');
  const video = modal.querySelector('video');
  if (video) {
    video.pause();
    video.currentTime = 0;
    video.removeAttribute('src'); // Stop background HTTP 206 chunk buffering
    video.load();
  }
  const container = document.getElementById('lightboxMediaContainer');
  if (container) container.innerHTML = '';

  modal.style.display = 'none';
  activeLightboxItem = null;

  // Restore Focus (Critic ELEV-05)
  if (window._lastActiveElement) {
    window._lastActiveElement.focus();
  }
}

function copyHashFromLightbox() {
  if (activeLightboxItem && activeLightboxItem.sha256) {
    copyToClipboard(activeLightboxItem.sha256);
    const btn = document.getElementById('lightboxCopyBtn');
    btn.classList.add('copied');
    showToast('SHA-256 hash copied to clipboard!', 'success');
    setTimeout(() => btn.classList.remove('copied'), 2000);
  }
}

function copyToClipboard(text) {
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).catch(() => {});
  }
}

// 9. Storage & System Health Monitor
async function updateStorage() {
  try {
    const res = await fetch(`/api/system/storage?token=${token}`, { headers: getHeaders() });
    const data = await res.json();
    const freeGb = (data.free_bytes / (1024 * 1024 * 1024)).toFixed(1);
    const totalGb = (data.total_bytes / (1024 * 1024 * 1024)).toFixed(1);
    const usedGb = (data.used_bytes / (1024 * 1024 * 1024)).toFixed(1);
    const pill = document.getElementById('storagePill');
    if (pill) {
      pill.innerText = `Disk: ${freeGb}G free / ${totalGb}G (${data.used_percent}% used)`;
      pill.title = `Partition Storage: ${usedGb} GB used, ${freeGb} GB free of ${totalGb} GB total`;
      if (data.is_low_space) {
        pill.style.borderColor = 'var(--accent-crimson)';
        pill.style.color = '#F87171';
      }
    }
  } catch (e) {}
}

// 10. Global Keyboard Navigation (Productivity Shortcuts)
document.addEventListener('keydown', (e) => {
  if (e.key === '/' && document.activeElement.tagName !== 'INPUT' && document.activeElement.tagName !== 'SELECT') {
    e.preventDefault();
    const search = document.getElementById('dialogSearch');
    if (search) search.focus();
  } else if (e.key === 'Escape') {
    closeTwoFactorModal();
    closeMediaLightbox();
  }
});

window.onload = () => {
  connectWebSocket();
  checkAuth();
  updateStorage();
  loadMedia();
};
