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

function escapeHtml(str) {
  if (str === null || str === undefined) return '';
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
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
      <div class="toast-title">${escapeHtml(titles[type])}</div>
      <div class="toast-message">${escapeHtml(message)}</div>
    </div>
    <button class="toast-close" aria-label="Dismiss">&times;</button>
  `;

  const dismiss = () => {
    if (toast._timer) clearTimeout(toast._timer);
    toast.classList.add('fade-out');
    setTimeout(() => { if (toast.parentNode) toast.remove(); }, 200);
  };

  const startTimer = () => {
    toast._timer = setTimeout(dismiss, duration);
  };
  const pauseTimer = () => {
    if (toast._timer) clearTimeout(toast._timer);
  };

  toast.addEventListener('mouseenter', pauseTimer);
  toast.addEventListener('mouseleave', startTimer);
  toast.addEventListener('focusin', pauseTimer);
  toast.addEventListener('focusout', startTimer);

  toast.querySelector('.toast-close').onclick = dismiss;
  startTimer();
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
  if (data.type === 'INIT_STATE') {
    handleInitState(data);
  } else if (data.type === 'PROGRESS' || data.type === 'COMPLETED' || data.type === 'FAILED') {
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

function handleInitState(data) {
  if (data.state) updateJobUI(data.state);
  if (Array.isArray(data.logs)) {
    const term = document.getElementById('terminalContent') || document.getElementById('terminalLogs');
    if (term) term._logLines = [];
    if (data.logs.length > 0) {
      data.logs.forEach(log => appendLog(log));
    }
  }
}

function appendLog(log) {
  appendTerminalLog(log);
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

  const curFileEl = document.getElementById('jobCurrentFile');
  if (curFileEl) {
    curFileEl.innerText = s.current_file ? `File: ${s.current_file}` : 'File: --';
    curFileEl.title = s.current_file_path || s.relpath || s.current_file || '';
  }
  const etaEl = document.getElementById('jobEtaText');
  if (etaEl) {
    if (s.eta_seconds !== null && s.eta_seconds !== undefined && s.status === 'running') {
      const totalSec = Math.max(0, Math.floor(s.eta_seconds));
      if (totalSec >= 3600) {
        const h = Math.floor(totalSec / 3600);
        const m = Math.floor((totalSec % 3600) / 60);
        const sec = totalSec % 60;
        etaEl.innerText = `ETA: ${h}h ${m}m ${sec}s`;
      } else {
        const m = Math.floor(totalSec / 60);
        const sec = totalSec % 60;
        etaEl.innerText = `ETA: ${m}m ${sec}s`;
      }
    } else {
      etaEl.innerText = 'ETA: --';
    }
  }
}

function appendTerminalLog(msg) {
  const el = document.getElementById('terminalLogs');
  if (!el) return;
  const isNearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
  if (!el._logLines) el._logLines = [];
  el._logLines.push(msg);
  if (el._logLines.length > 500) el._logLines.shift();
  el.textContent = el._logLines.join('\n');
  if (isNearBottom) el.scrollTop = el.scrollHeight;
}

// 4. Telegram Account & Auth Wizard
async function checkAuth() {
  try {
    const res = await fetch('/api/auth/me', { headers: getHeaders() });
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

let qrPollTimer = null;

function startQrPolling() {
  stopQrPolling();
  qrPollTimer = setInterval(async () => {
    try {
      const res = await fetch('/api/auth/qr/status', { headers: getHeaders() });
      const data = await res.json();
      if (data.authorized) {
        stopQrPolling();
        showToast('Logged in successfully via Telegram QR!', 'success', 'Authenticated');
        checkAuth();
      }
    } catch (e) {}
  }, 2000);
}

function stopQrPolling() {
  if (qrPollTimer) {
    clearInterval(qrPollTimer);
    qrPollTimer = null;
  }
}

function switchAuthTab(tab) {
  const phoneBtn = document.getElementById('tabBtnPhone');
  const qrBtn = document.getElementById('tabBtnQr');
  const phoneTab = document.getElementById('phoneAuthTab');
  const qrTab = document.getElementById('qrAuthTab');

  if (tab === 'phone') {
    stopQrPolling();
    phoneBtn.classList.add('active');
    phoneBtn.setAttribute('aria-selected', 'true');
    qrBtn.classList.remove('active');
    qrBtn.setAttribute('aria-selected', 'false');
    phoneTab.style.display = 'block';
    qrTab.style.display = 'none';
  } else {
    qrBtn.classList.add('active');
    qrBtn.setAttribute('aria-selected', 'true');
    phoneBtn.classList.remove('active');
    phoneBtn.setAttribute('aria-selected', 'false');
    qrTab.style.display = 'block';
    phoneTab.style.display = 'none';
    loadQrCode();
  }
}

async function loadQrCode() {
  const container = document.getElementById('qrContainer');
  container.innerHTML = '<div style="color:#000; padding:2rem 0; font-size:0.85rem;">Generating QR Code...</div>';
  try {
    const res = await fetch('/api/auth/qr', { headers: getHeaders() });
    const data = await res.json();
    if (data.svg) {
      container.innerHTML = `
        <div style="width:180px; height:180px; display:flex; align-items:center; justify-content:center;">${data.svg}</div>
        <div style="font-size:0.75rem; color:#666; margin-top:0.5rem;">Expires: ${data.expires ? new Date(data.expires).toLocaleTimeString() : 'in 2 min'}</div>
      `;
      startQrPolling();
    } else if (data.url) {
      container.innerHTML = `
        <div style="color:#333; padding:1.5rem 0.5rem; font-size:0.82rem; word-break:break-all;">
          <p style="margin-bottom:0.5rem; font-weight:600;">Scan URL in Telegram:</p>
          <a href="${escapeHtml(data.url)}" target="_blank" style="color:#0369A1; font-weight:600; text-decoration:underline;">Open Telegram Login Link</a>
        </div>
      `;
      startQrPolling();
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
    const res = await fetch('/api/auth/phone/send_code', {
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
    const res = await fetch('/api/auth/phone/sign_in', {
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
    btn.setAttribute('aria-label', 'Hide password');
    btn.setAttribute('aria-pressed', 'true');
  } else {
    input.type = 'password';
    btn.innerHTML = `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>`;
    btn.setAttribute('aria-label', 'Show password');
    btn.setAttribute('aria-pressed', 'false');
  }
}

async function submitTwoFactorPassword() {
  const pwd = document.getElementById('twoFactorInput').value;
  if (!pwd) {
    showToast('Please enter your 2FA password.', 'warning');
    return;
  }
  try {
    const res = await fetch('/api/auth/2fa', {
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
    if (el) {
      el.classList.remove('active');
      el.setAttribute('aria-selected', 'false');
    }
  });
  const activeId = kind === 'channel' ? 'filterChannel' : (kind === 'group' ? 'filterGroup' : (kind === 'dm' ? 'filterDm' : 'filterAll'));
  const activeBtn = document.getElementById(activeId);
  if (activeBtn) {
    activeBtn.classList.add('active');
    activeBtn.setAttribute('aria-selected', 'true');
  }

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
      <svg class="spinner-rotate" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="var(--cyan-glow)" stroke-width="2.5" stroke-linecap="round">
        <circle cx="12" cy="12" r="10" stroke-opacity="0.25" />
        <path d="M12 2a10 10 0 0 1 10 10" />
      </svg>
      <span>Fetching dialogs...</span>
    </div>
  `;

  try {
    const res = await fetch('/api/dialogs?kind=' + encodeURIComponent(kind), { headers: getHeaders() });
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
          <div style="color:var(--accent-crimson); font-size:0.85rem; margin-bottom:0.5rem;">${escapeHtml(errMsg)}</div>
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

    return `
    <div class="dialog-item" data-target="${escapeHtml(target)}">
      <div style="overflow:hidden; padding-right:0.5rem; pointer-events:none;">
        <div style="font-weight:600; font-size:0.88rem; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;">${escapeHtml(d.name || 'Unnamed Chat')}</div>
        <div style="display:flex; align-items:center; gap:0.4rem; margin-top:0.25rem;">
          <span class="category-badge ${badgeClass[kind] || 'badge-channel'}">${escapeHtml(kind.toUpperCase())}</span>
          <span style="font-size:0.75rem; color:var(--text-muted); font-family:var(--font-mono);">${escapeHtml(displaySub)}</span>
        </div>
      </div>
      <button class="btn btn-cyan chat-arm-btn" data-target="${escapeHtml(target)}" aria-label="Arm ${escapeHtml(d.name || target)} for download" style="font-size:0.78rem; flex-shrink:0;">Arm</button>
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

function toggleAdvancedFilters() {
  const p = document.getElementById('advFiltersPanel');
  const btn = document.getElementById('advFilterToggleBtn');
  if (!p) return;
  const isOpen = p.style.display !== 'none';
  p.style.display = isOpen ? 'none' : 'grid';
  if (btn) btn.setAttribute('aria-expanded', String(!isOpen));
}

// 7. Download Management
async function startDownload() {
  const target = document.getElementById('targetInput').value.trim();
  const noLimit = document.getElementById('noLimitToggle')?.checked || false;
  const rawLimit = parseInt(document.getElementById('limitInput').value, 10);
  const limit = noLimit ? null : (isNaN(rawLimit) ? 100 : rawLimit);
  const filter = document.getElementById('filterSelect').value || null;
  const search = document.getElementById('searchInput').value.trim() || null;

  const after = document.getElementById('afterInput')?.value.trim() || null;
  const before = document.getElementById('beforeInput')?.value.trim() || null;
  const from_user = document.getElementById('fromUserInput')?.value.trim() || null;

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
    const res = await fetch('/api/download/start', {
      method: 'POST',
      headers: getHeaders(),
      body: JSON.stringify({ target, limit, no_limit: noLimit, filter, search, sync, resume, dry_run, takeout, join, after, before, from_user }),
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

let cancelConfirmTimer = null;

async function cancelDownload() {
  const btn = document.getElementById('cancelJobBtn');
  if (!btn) return;
  if (!btn.dataset.confirming) {
    btn.dataset.confirming = 'true';
    btn.dataset.originalHtml = btn.innerHTML;
    btn.innerHTML = '⚠️ Confirm Cancel?';
    btn.style.borderColor = 'var(--accent-amber)';
    cancelConfirmTimer = setTimeout(() => {
      btn.removeAttribute('data-confirming');
      btn.innerHTML = btn.dataset.originalHtml;
      btn.style.borderColor = '';
    }, 3000);
    return;
  }
  clearTimeout(cancelConfirmTimer);
  btn.removeAttribute('data-confirming');
  btn.innerHTML = btn.dataset.originalHtml;
  btn.style.borderColor = '';

  try {
    const res = await fetch('/api/download/cancel', {
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
let currentMediaKind = '';
let currentMediaPage = 1;
const currentMediaLimit = 24;
let totalMediaCount = 0;
let currentLightboxIndex = 0;

function setMediaKindFilter(kind) {
  currentMediaKind = kind;
  currentMediaPage = 1;
  ['All', 'Video', 'Photo', 'Audio', 'Doc'].forEach(tab => {
    const tabEl = document.getElementById(`mediaTab${tab}`);
    if (tabEl) {
      const isActive = (tab === 'All' && !kind) || (tab.toLowerCase() === kind) || (tab === 'Doc' && kind === 'document');
      tabEl.classList.toggle('active', isActive);
      tabEl.setAttribute('aria-selected', String(isActive));
    }
  });
  loadMedia();
}

function changeMediaPage(delta) {
  const maxPage = Math.max(1, Math.ceil(totalMediaCount / currentMediaLimit));
  const newPage = currentMediaPage + delta;
  if (newPage >= 1 && newPage <= maxPage) {
    currentMediaPage = newPage;
    loadMedia();
  }
}

async function loadMedia() {
  const grid = document.getElementById('mediaGrid');
  if (!grid) return;
  try {
    const offset = (currentMediaPage - 1) * currentMediaLimit;
    let url = `/api/media?limit=${currentMediaLimit}&offset=${offset}`;
    if (currentMediaKind) url += `&kind=${encodeURIComponent(currentMediaKind)}`;
    const res = await fetch(url, { headers: getHeaders() });
    const data = await res.json();
    mediaCache = data.items || [];
    totalMediaCount = data.total !== undefined ? data.total : (mediaCache.length + offset);

    const maxPage = Math.max(1, Math.ceil(totalMediaCount / currentMediaLimit));
    const pageInd = document.getElementById('mediaPageIndicator');
    if (pageInd) pageInd.innerText = `Page ${currentMediaPage} / ${maxPage}`;
    const prevBtn = document.getElementById('mediaPrevBtn');
    if (prevBtn) prevBtn.disabled = currentMediaPage <= 1;
    const nextBtn = document.getElementById('mediaNextBtn');
    if (nextBtn) nextBtn.disabled = currentMediaPage >= maxPage;

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

    grid.innerHTML = mediaCache.map((item, idx) => {
      const isMissing = item.exists === false;
      const streamUrlWithToken = `${item.stream_url}?token=${encodeURIComponent(token)}`;
      const actionBar = isMissing
        ? `<div class="card-action-bar" onclick="event.stopPropagation();"><span class="card-action-btn disabled" title="File missing from disk" aria-disabled="true" style="opacity:0.4; cursor:not-allowed;">⚠️</span></div>`
        : `<div class="card-action-bar" onclick="event.stopPropagation();">
            <a href="${streamUrlWithToken}" target="_blank" rel="noopener noreferrer" class="card-action-btn" title="Open in browser" aria-label="Open ${escapeHtml(item.filename)} in new tab">
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/></svg>
            </a>
            <a href="${streamUrlWithToken}&download=1" download="${escapeHtml(item.filename)}" class="card-action-btn" title="Download file" aria-label="Download ${escapeHtml(item.filename)}">
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
            </a>
          </div>`;

      let thumbContent = '';
      if (item.kind === 'video') {
        thumbContent = `<div style="display:flex; flex-direction:column; align-items:center; gap:0.3rem;"><svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="#00E5FF" stroke-width="2"><polygon points="5 3 19 12 5 21 5 3"/></svg><span style="font-size:0.65rem; color:var(--accent-cyan); font-weight:600;">VIDEO</span></div>`;
      } else if (item.kind === 'photo') {
        thumbContent = `<img src="${streamUrlWithToken}" loading="lazy" style="width:100%; height:100%; object-fit:cover;" alt="${escapeHtml(item.filename)}">`;
      } else if (item.kind === 'audio') {
        thumbContent = `<div style="display:flex; flex-direction:column; align-items:center; gap:0.3rem;"><svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="#A855F7" stroke-width="2"><path d="M3 18v-6a9 9 0 0 1 18 0v6"/><path d="M21 19a2 2 0 0 1-2 2h-1a2 2 0 0 1-2-2v-3a2 2 0 0 1 2-2h3zM3 19a2 2 0 0 0 2 2h1a2 2 0 0 0 2-2v-3a2 2 0 0 0-2-2H3z"/></svg><span style="font-size:0.65rem; color:#A855F7; font-weight:600;">AUDIO</span></div>`;
      } else {
        thumbContent = `<div style="display:flex; flex-direction:column; align-items:center; gap:0.3rem;"><svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="var(--text-muted)" stroke-width="1.8"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/></svg><span style="font-size:0.65rem; color:var(--text-muted); font-weight:600;">DOC</span></div>`;
      }

      return `
        <div class="media-card" data-idx="${idx}" tabindex="0" role="button" aria-label="View ${escapeHtml(item.filename)}">
          ${actionBar}
          <div class="media-thumb" style="pointer-events:none;">
            ${thumbContent}
          </div>
          <div class="media-meta" style="pointer-events:none;">
            <div class="name" title="${escapeHtml(item.filename)}">${escapeHtml(item.filename)}</div>
            <div style="display:flex; justify-content:space-between; color:var(--text-muted); font-size:0.75rem;">
              <span>${((item.size || 0) / (1024 * 1024)).toFixed(2)} MB</span>
              <span style="font-family:var(--font-mono); color:var(--accent-cyan);">${escapeHtml(item.kind.toUpperCase())}</span>
            </div>
          </div>
        </div>
      `;
    }).join('');
  } catch (e) {
    grid.innerHTML = '<div style="color:var(--accent-crimson); padding:1rem; grid-column:1/-1;">Failed to load media manifest.</div>';
  }
}

function openMediaLightboxByIndex(idx) {
  if (idx < 0) idx = mediaCache.length - 1;
  if (idx >= mediaCache.length) idx = 0;
  const item = mediaCache[idx];
  if (!item) return;
  currentLightboxIndex = idx;
  activeLightboxItem = item;
  window._lastActiveElement = document.activeElement;

  document.getElementById('lightboxTitle').innerText = item.filename;
  document.getElementById('lightboxSize').innerText = `${((item.size || 0) / (1024 * 1024)).toFixed(2)} MB (${item.size || 0} bytes)`;
  document.getElementById('lightboxMime').innerText = item.mime || 'application/octet-stream';
  document.getElementById('lightboxMsgId').innerText = `#${item.msg_id || '--'}`;
  document.getElementById('lightboxSha').innerText = item.sha256 || 'N/A';
  
  const streamUrlWithToken = `${item.stream_url}?token=${encodeURIComponent(token)}`;
  const dlLink = document.getElementById('lightboxDownloadLink');
  if (dlLink) dlLink.href = `${streamUrlWithToken}&download=1`;
  const openLink = document.getElementById('lightboxOpenLink');
  if (openLink) openLink.href = streamUrlWithToken;

  const container = document.getElementById('lightboxMediaContainer');
  if (item.kind === 'video') {
    container.innerHTML = `
      <video id="theaterVideo" controls autoplay preload="none" style="width:100%; height:100%; max-height:520px;">
        <source src="${streamUrlWithToken}">
        Your browser does not support HTML5 video.
      </video>
    `;
    const v = document.getElementById('theaterVideo');
    if (v && v.play) v.play().catch(() => {});
  } else if (item.kind === 'photo') {
    container.innerHTML = `<img src="${streamUrlWithToken}" style="max-width:100%; max-height:520px; object-fit:contain;" alt="${escapeHtml(item.filename)}">`;
  } else if (item.kind === 'audio') {
    container.innerHTML = `
      <div style="display:flex; flex-direction:column; align-items:center; justify-content:center; padding:2rem 1rem; width:100%;">
        <svg width="64" height="64" viewBox="0 0 24 24" fill="none" stroke="#A855F7" stroke-width="1.5" style="margin-bottom:1rem;"><path d="M3 18v-6a9 9 0 0 1 18 0v6"/><path d="M21 19a2 2 0 0 1-2 2h-1a2 2 0 0 1-2-2v-3a2 2 0 0 1 2-2h3zM3 19a2 2 0 0 0 2 2h1a2 2 0 0 0 2-2v-3a2 2 0 0 0-2-2H3z"/></svg>
        <audio id="theaterAudio" controls autoplay style="width:100%; max-width:480px; margin-top:1rem;">
          <source src="${streamUrlWithToken}">
          Your browser does not support HTML5 audio.
        </audio>
      </div>
    `;
    const a = document.getElementById('theaterAudio');
    if (a && a.play) a.play().catch(() => {});
  } else {
    container.innerHTML = `
      <div style="text-align:center; padding:3rem 1rem;">
        <div style="margin-bottom:1rem;">
          <svg width="56" height="56" viewBox="0 0 24 24" fill="none" stroke="var(--text-muted)" stroke-width="1.5"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/></svg>
        </div>
        <div style="font-size:0.9rem; font-weight:600; color:var(--text-main);">${escapeHtml(item.filename)}</div>
        <div style="font-size:0.8rem; color:var(--text-muted); margin-top:0.4rem;">Binary document ready for direct open or download.</div>
      </div>
    `;
  }

  document.getElementById('mediaLightboxModal').style.display = 'flex';
}

function prevMediaLightbox() {
  if (mediaCache.length > 0) openMediaLightboxByIndex(currentLightboxIndex - 1);
}

function nextMediaLightbox() {
  if (mediaCache.length > 0) openMediaLightboxByIndex(currentLightboxIndex + 1);
}

// Explicit Video/Audio Pause & Source Detachment Teardown
function closeMediaLightbox() {
  const modal = document.getElementById('mediaLightboxModal');
  const mediaEls = modal.querySelectorAll('video, audio');
  mediaEls.forEach(el => {
    try {
      el.pause();
      el.currentTime = 0;
      el.querySelectorAll('source').forEach(s => s.removeAttribute('src'));
      el.removeAttribute('src');
      el.load();
    } catch (e) {}
  });
  const container = document.getElementById('lightboxMediaContainer');
  if (container) container.innerHTML = '';

  modal.style.display = 'none';
  activeLightboxItem = null;

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
    const res = await fetch('/api/system/storage', { headers: getHeaders() });
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
  const activeTag = document.activeElement ? document.activeElement.tagName : '';
  const twoFactorModal = document.getElementById('twoFactorModal');
  const mediaLightboxModal = document.getElementById('mediaLightboxModal');
  const fileSelectorModal = document.getElementById('fileSelectorModal');
  const activeModal = (twoFactorModal && twoFactorModal.style.display !== 'none') ? twoFactorModal
                    : (mediaLightboxModal && mediaLightboxModal.style.display !== 'none') ? mediaLightboxModal
                    : (fileSelectorModal && fileSelectorModal.style.display !== 'none') ? fileSelectorModal
                    : null;
  const isModalOpen = !!activeModal;

  if (activeModal && e.key === 'Tab') {
    const focusableSelectors = 'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])';
    const focusable = Array.from(activeModal.querySelectorAll(focusableSelectors))
      .filter(el => !el.disabled && el.offsetParent !== null);
    if (focusable.length > 0) {
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (e.shiftKey) {
        if (document.activeElement === first || !activeModal.contains(document.activeElement)) {
          e.preventDefault();
          last.focus();
        }
      } else {
        if (document.activeElement === last || !activeModal.contains(document.activeElement)) {
          e.preventDefault();
          first.focus();
        }
      }
    } else {
      e.preventDefault();
    }
    return;
  }

  if (e.key === '/' && activeTag !== 'INPUT' && activeTag !== 'SELECT' && activeTag !== 'TEXTAREA' && !isModalOpen) {
    e.preventDefault();
    const search = document.getElementById('dialogSearch');
    if (search) search.focus();
  } else if (e.key === 'Escape') {
    const activeTip = document.querySelector('.tooltip-wrapper.active');
    if (activeTip) {
      activeTip.classList.remove('active');
      activeTip.querySelector('.tooltip-trigger')?.setAttribute('aria-expanded', 'false');
      return;
    }
    closeTwoFactorModal();
    closeMediaLightbox();
    closeFileSelectorModal();
  } else if (e.key === 'ArrowLeft') {
    if (document.getElementById('mediaLightboxModal')?.style.display !== 'none') {
      prevMediaLightbox();
    }
  } else if (e.key === 'ArrowRight') {
    if (document.getElementById('mediaLightboxModal')?.style.display !== 'none') {
      nextMediaLightbox();
    }
  }
});

// 11. Direct Browser File Extractor & Streaming
let scannedFiles = [];
let selectedFileIds = new Set();
let scannedKindFilter = '';
let scannedSearchText = '';
let scanOffsetId = 0;
let scanHasMore = false;
let isScanning = false;
let isBrowserDownloading = false;
let abortBrowserDownload = false;
let isPreparingZip = false;

function openFileSelectorModal() {
  const modal = document.getElementById('fileSelectorModal');
  if (!modal) return;
  window._lastActiveElement = document.activeElement;
  modal.style.display = 'flex';
  document.body.style.overflow = 'hidden';

  const mainTarget = document.getElementById('targetInput')?.value.trim() || '';
  const modalTarget = document.getElementById('modalTargetInput');
  if (modalTarget && mainTarget) {
    modalTarget.value = mainTarget;
  }

  const effectiveTarget = (modalTarget?.value || mainTarget).trim();
  if (effectiveTarget) {
    if (scannedFiles.length === 0 || modal.dataset.currentTarget !== effectiveTarget) {
      modal.dataset.currentTarget = effectiveTarget;
      scannedFiles = [];
      selectedFileIds.clear();
      scanOffsetId = 0;
      scanHasMore = false;
      runChatScan(effectiveTarget, 0);
    } else {
      renderScannedTable();
    }
  } else {
    const tbody = document.getElementById('fileSelectorTbody');
    if (tbody) {
      tbody.innerHTML = '<tr><td colspan="5" style="text-align:center; padding:2.5rem; color:var(--text-muted);"><div style="font-size:1.05rem; font-weight:600; margin-bottom:0.5rem; color:var(--text-main);">No Target Selected</div><div>Please enter a channel username (e.g. <code>@channel</code>) or link in the bar above and click <strong>Scan Media</strong>, or select a chat from the Chat Explorer on the left.</div></td></tr>';
    }
    setTimeout(() => modalTarget?.focus(), 50);
  }
}

function triggerModalScan() {
  const modalTarget = document.getElementById('modalTargetInput');
  const target = modalTarget?.value.trim();
  if (!target) {
    showToast('Please enter a target chat or channel (@name, link, or ID).', 'warning', 'Target Required');
    modalTarget?.focus();
    return;
  }
  const mainTarget = document.getElementById('targetInput');
  if (mainTarget) mainTarget.value = target;

  const modal = document.getElementById('fileSelectorModal');
  if (modal) modal.dataset.currentTarget = target;
  scannedFiles = [];
  selectedFileIds.clear();
  scanOffsetId = 0;
  scanHasMore = false;
  runChatScan(target, 0);
}

function closeFileSelectorModal() {
  const modal = document.getElementById('fileSelectorModal');
  if (!modal || modal.style.display === 'none') return;
  if (isBrowserDownloading) {
    abortBrowserDownload = true;
  }
  modal.style.display = 'none';
  document.body.style.overflow = '';
  if (window._lastActiveElement && typeof window._lastActiveElement.focus === 'function') {
    window._lastActiveElement.focus();
  }
}

async function runChatScan(target, offsetId = 0) {
  if (isScanning) return;
  isScanning = true;
  const tbody = document.getElementById('fileSelectorTbody');
  if (offsetId === 0 && tbody) {
    tbody.innerHTML = '<tr><td colspan="5" style="text-align:center; padding:2rem; color:var(--accent-cyan);"><div class="spinner-rotate" style="display:inline-block; width:18px; height:18px; border:2px solid var(--accent-cyan); border-top-color:transparent; border-radius:50%; margin-right:8px; vertical-align:middle;"></div>Scanning messages for media...</td></tr>';
  }

  const limitVal = parseInt(document.getElementById('limitInput')?.value, 10) || 100;
  const filterVal = document.getElementById('filterSelect')?.value || '';
  const searchVal = document.getElementById('searchInput')?.value.trim() || '';
  const joinVal = document.getElementById('joinToggle')?.checked || false;

  try {
    const res = await fetch('/api/chat/scan', {
      method: 'POST',
      headers: getHeaders(),
      body: JSON.stringify({
        target: target,
        limit: Math.min(limitVal, 200),
        filter: filterVal || null,
        search: searchVal || null,
        offset_id: offsetId,
        join: joinVal
      })
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: res.statusText }));
      throw new Error(err.detail || 'Scan failed');
    }
    const data = await res.json();
    if (offsetId === 0) {
      scannedFiles = data.items || [];
    } else {
      scannedFiles = scannedFiles.concat(data.items || []);
    }
    scanOffsetId = data.next_offset_id || 0;
    scanHasMore = !!data.has_more;

    const titleEl = document.getElementById('fileSelectorSubtitle');
    if (titleEl) {
      titleEl.textContent = `${data.chat_title || target} (${scannedFiles.length} files found)`;
    }

    const scanMoreBtn = document.getElementById('scanMoreBtn');
    if (scanMoreBtn) {
      scanMoreBtn.style.display = scanHasMore ? 'inline-block' : 'none';
    }

    renderScannedTable();
    showToast(`Found ${data.count} media files.`, 'success', 'Scan Complete');
  } catch (err) {
    showToast(err.message, 'error', 'Scan Error');
    if (offsetId === 0 && tbody) {
      tbody.innerHTML = `<tr><td colspan="5" style="text-align:center; padding:2rem; color:var(--accent-crimson);">${escapeHtml(err.message)}</td></tr>`;
    }
  } finally {
    isScanning = false;
  }
}

function scanNextPage() {
  const modal = document.getElementById('fileSelectorModal');
  const target = (modal?.dataset.currentTarget || document.getElementById('modalTargetInput')?.value || document.getElementById('targetInput')?.value || '').trim();
  if (target && scanOffsetId) {
    runChatScan(target, scanOffsetId);
  }
}

function filterScannedKind(kind) {
  scannedKindFilter = kind;
  ['All', 'Video', 'Photo', 'Audio', 'Doc'].forEach(k => {
    const btn = document.getElementById(`fileFilter${k}`);
    if (btn) {
      const active = (k === 'All' && !kind) || (k.toLowerCase() === kind.toLowerCase()) || (k === 'Doc' && kind === 'document');
      btn.classList.toggle('active', active);
      btn.setAttribute('aria-selected', active);
    }
  });
  renderScannedTable();
}

function filterScannedSearch() {
  const searchInput = document.getElementById('fileSelectorSearch');
  scannedSearchText = (searchInput?.value || '').toLowerCase().trim();
  renderScannedTable();
}

function renderScannedTable() {
  const tbody = document.getElementById('fileSelectorTbody');
  if (!tbody) return;

  const filtered = scannedFiles.filter(item => {
    if (scannedKindFilter && item.kind !== scannedKindFilter) return false;
    if (scannedSearchText) {
      const matchName = (item.name || '').toLowerCase().includes(scannedSearchText);
      const matchCaption = (item.caption || '').toLowerCase().includes(scannedSearchText);
      if (!matchName && !matchCaption) return false;
    }
    return true;
  });

  if (filtered.length === 0) {
    tbody.innerHTML = '<tr><td colspan="5" style="text-align:center; padding:2rem; color:var(--text-muted);">No matching media files found.</td></tr>';
    updateSelectionCounter();
    return;
  }

  const formatSize = (b) => {
    if (!b) return '0 B';
    const units = ['B', 'KB', 'MB', 'GB'];
    let i = 0;
    while (b >= 1024 && i < units.length - 1) { b /= 1024; i++; }
    return `${b.toFixed(1)} ${units[i]}`;
  };

  tbody.innerHTML = filtered.map(item => {
    const isChecked = selectedFileIds.has(item.msg_id);
    const dlUrl = `/api/direct/download/${item.chat_id}/${item.msg_id}?token=${encodeURIComponent(token)}`;
    return `
      <tr>
        <td class="checkbox-cell">
          <input type="checkbox" ${isChecked ? 'checked' : ''} onchange="toggleFileSelection(${item.msg_id}, this.checked)" aria-label="Select ${escapeHtml(item.name)}">
        </td>
        <td style="max-width:380px;">
          <div style="font-weight:600; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;" title="${escapeHtml(item.name)}">${escapeHtml(item.name)}</div>
          ${item.caption ? `<div style="font-size:0.75rem; color:var(--text-muted); overflow:hidden; text-overflow:ellipsis; white-space:nowrap;">${escapeHtml(item.caption)}</div>` : ''}
        </td>
        <td>
          <span class="file-kind-badge file-kind-${item.kind}">${escapeHtml(item.kind)}</span>
        </td>
        <td style="font-family:var(--font-mono); white-space:nowrap;">
          ${formatSize(item.size)}
        </td>
        <td style="text-align:right;">
          <a href="${dlUrl}" class="file-action-btn" download="${escapeHtml(item.name)}" title="Download to Browser" aria-label="Download ${escapeHtml(item.name)}">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
          </a>
        </td>
      </tr>
    `;
  }).join('');

  updateSelectionCounter();
}

function toggleFileSelection(msgId, checked) {
  if (checked) {
    selectedFileIds.add(msgId);
  } else {
    selectedFileIds.delete(msgId);
  }
  updateSelectionCounter();
}

function toggleSelectAll(checked) {
  scannedFiles.forEach(item => {
    if (checked) {
      selectedFileIds.add(item.msg_id);
    } else {
      selectedFileIds.delete(item.msg_id);
    }
  });
  renderScannedTable();
}

function updateSelectionCounter() {
  const countEl = document.getElementById('fileSelectedCountText');
  const btn = document.getElementById('downloadSelectedBtn');
  const selectAll = document.getElementById('selectAllCheckbox');

  let totalBytes = 0;
  scannedFiles.forEach(item => {
    if (selectedFileIds.has(item.msg_id)) {
      totalBytes += (item.size || 0);
    }
  });

  const formatSize = (b) => {
    if (!b) return '0.0 MB';
    return (b / (1024 * 1024)).toFixed(1) + ' MB';
  };

  const count = selectedFileIds.size;
  if (countEl) {
    countEl.textContent = `Selected: ${count} files (${formatSize(totalBytes)})`;
  }
  if (btn) {
    if (isBrowserDownloading) {
      btn.disabled = false;
      btn.className = 'btn btn-crimson';
      btn.innerHTML = `
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="6" y="6" width="12" height="12" rx="2"/></svg>
        Cancel Download
      `;
      btn.onclick = () => {
        abortBrowserDownload = true;
        showToast('Aborting browser downloads...', 'warning', 'Download Queue');
      };
    } else {
      btn.className = 'btn btn-purple';
      btn.disabled = (count === 0);
      btn.onclick = downloadSelectedFilesToBrowser;
      btn.innerHTML = `
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
        Download Selected (${count})
      `;
    }
  }
  const zipBtn = document.getElementById('downloadZipBtn');
  const zipBtnText = document.getElementById('downloadZipBtnText');
  if (zipBtn && zipBtnText) {
    if (isPreparingZip) {
      zipBtn.disabled = true;
      zipBtnText.innerHTML = '<span class="spinner-rotate" style="display:inline-block; width:14px; height:14px; border:2px solid #0B0E14; border-top-color:transparent; border-radius:50%; margin-right:6px; vertical-align:middle;"></span> Preparing ZIP...';
    } else if (selectedFileIds.size > 0) {
      zipBtn.disabled = false;
      zipBtnText.textContent = `Download as ZIP (${selectedFileIds.size} files)`;
    } else if (scannedFiles.length > 0) {
      zipBtn.disabled = false;
      zipBtnText.textContent = `Download All as ZIP (${scannedFiles.length} files)`;
    } else {
      zipBtn.disabled = true;
      zipBtnText.textContent = 'Download as ZIP';
    }
  }

  if (selectAll) {
    selectAll.checked = (scannedFiles.length > 0 && count === scannedFiles.length);
    selectAll.indeterminate = (count > 0 && count < scannedFiles.length);
  }
}

async function downloadSelectedFilesToBrowser() {
  const selectedItems = scannedFiles.filter(item => selectedFileIds.has(item.msg_id));
  if (selectedItems.length === 0 || isBrowserDownloading) return;

  isBrowserDownloading = true;
  abortBrowserDownload = false;
  updateSelectionCounter();

  showToast(`Initiating download for ${selectedItems.length} files...`, 'info', 'Browser Downloads');

  for (let i = 0; i < selectedItems.length; i++) {
    if (abortBrowserDownload) {
      showToast('Browser download sequence aborted.', 'warning', 'Aborted');
      break;
    }
    const item = selectedItems[i];
    const dlUrl = `/api/direct/download/${item.chat_id}/${item.msg_id}?token=${encodeURIComponent(token)}`;

    // Create an invisible anchor to trigger browser download
    const a = document.createElement('a');
    a.style.display = 'none';
    a.href = dlUrl;
    a.download = item.name || `file_${item.chat_id}_${item.msg_id}`;
    document.body.appendChild(a);
    a.click();
    setTimeout(() => a.remove(), 1000);

    // Stagger downloads by 750ms to allow browser download manager to register without popup block
    if (i < selectedItems.length - 1) {
      await new Promise(resolve => setTimeout(resolve, 750));
    }
  }

  isBrowserDownloading = false;
  updateSelectionCounter();
  if (!abortBrowserDownload) {
    showToast(`Completed initiating ${selectedItems.length} browser downloads.`, 'success', 'Downloads Dispatched');
  }
}

async function downloadSelectedFilesAsZip() {
  if (isPreparingZip) return;

  let msgIds = [];
  if (selectedFileIds.size > 0) {
    msgIds = Array.from(selectedFileIds);
  } else if (scannedFiles.length > 0) {
    msgIds = scannedFiles.map(f => f.msg_id);
  } else {
    showToast('No files available to package into ZIP.', 'warning', 'ZIP Download');
    return;
  }

  const chatId = scannedFiles[0]?.chat_id;
  if (!chatId) {
    showToast('Cannot determine chat ID for ZIP download.', 'error', 'ZIP Error');
    return;
  }

  console.log('[TeleVault] downloadSelectedFilesAsZip triggered', { selectedCount: selectedFileIds.size, scannedCount: scannedFiles.length, chatId });
  showToast('Preparing ZIP download ticket...', 'info', 'ZIP Download');

  const subtitleEl = document.getElementById('fileSelectorSubtitle');
  let chatTitle = subtitleEl ? subtitleEl.textContent.split('(')[0].trim() : '';
  if (!chatTitle) {
    chatTitle = document.getElementById('modalTargetInput')?.value.trim() || document.getElementById('targetInput')?.value.trim() || String(chatId);
  }

  const zipBtn = document.getElementById('downloadZipBtn');
  const zipBtnText = document.getElementById('downloadZipBtnText');

  isPreparingZip = true;
  if (zipBtn) zipBtn.disabled = true;
  if (zipBtnText) {
    zipBtnText.innerHTML = '<span class="spinner-rotate" style="display:inline-block; width:14px; height:14px; border:2px solid #0B0E14; border-top-color:transparent; border-radius:50%; margin-right:6px; vertical-align:middle;"></span> Preparing ZIP...';
  }

  const formatSize = (b) => {
    if (!b) return '0.0 MB';
    const units = ['B', 'KB', 'MB', 'GB'];
    let i = 0;
    let val = b;
    while (val >= 1024 && i < units.length - 1) { val /= 1024; i++; }
    return `${val.toFixed(1)} ${units[i]}`;
  };

  try {
    const res = await fetch('/api/direct/zip/prepare', {
      method: 'POST',
      headers: getHeaders(),
      body: JSON.stringify({
        chat_id: chatId,
        msg_ids: msgIds,
        chat_title: chatTitle,
      }),
    });

    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: res.statusText }));
      showToast(err.detail || 'Failed to prepare ZIP archive', 'error', 'ZIP Error');
      return;
    }

    const data = await res.json();
    const { ticket, suggested_filename, file_count, archive_size } = data;

    const zipUrl = `/api/direct/zip/stream/${ticket}?token=${encodeURIComponent(token)}`;
    console.log('[TeleVault] Starting ZIP download stream:', zipUrl);

    // Single-trigger: invisible iframe guarantees download initiation without popup blocker drops or duplicate downloads
    const iframe = document.createElement('iframe');
    iframe.style.display = 'none';
    iframe.src = zipUrl;
    document.body.appendChild(iframe);
    setTimeout(() => iframe.remove(), 60000);

    showToast(`Streaming ${file_count} files as single ZIP (${formatSize(archive_size)}). Check your browser download shelf!`, 'success', 'Single ZIP Download Started');
  } catch (err) {
    console.error('[TeleVault] ZIP download failed:', err);
    showToast(err.message || 'Error communicating with server', 'error', 'ZIP Error');
  } finally {
    isPreparingZip = false;
    updateSelectionCounter();
  }
}

// Event delegations
document.addEventListener('DOMContentLoaded', () => {
  // Contextual Tooltips tap-to-toggle & click-outside dismissal
  document.addEventListener('click', (e) => {
    const trigger = e.target.closest('.tooltip-trigger');
    const activeWrappers = document.querySelectorAll('.tooltip-wrapper.active');

    if (trigger) {
      e.preventDefault();
      e.stopPropagation();
      const wrapper = trigger.closest('.tooltip-wrapper');
      const isAlreadyActive = wrapper?.classList.contains('active');
      activeWrappers.forEach(w => {
        w.classList.remove('active');
        w.querySelector('.tooltip-trigger')?.setAttribute('aria-expanded', 'false');
      });
      if (wrapper && !isAlreadyActive) {
        wrapper.classList.add('active');
        trigger.setAttribute('aria-expanded', 'true');
      }
    } else if (!e.target.closest('.tooltip-bubble')) {
      activeWrappers.forEach(w => {
        w.classList.remove('active');
        w.querySelector('.tooltip-trigger')?.setAttribute('aria-expanded', 'false');
      });
    }
  });

  document.getElementById('dialogList')?.addEventListener('click', (e) => {
    const item = e.target.closest('[data-target]');
    if (item && item.dataset.target) {
      armTarget(item.dataset.target);
    }
  });

  const mediaGrid = document.getElementById('mediaGrid');
  if (mediaGrid) {
    mediaGrid.addEventListener('click', (e) => {
      const card = e.target.closest('.media-card');
      if (card && card.dataset.idx !== undefined) {
        openMediaLightboxByIndex(parseInt(card.dataset.idx, 10));
      }
    });
    mediaGrid.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') {
        const card = e.target.closest('.media-card');
        if (card && card.dataset.idx !== undefined) {
          e.preventDefault();
          openMediaLightboxByIndex(parseInt(card.dataset.idx, 10));
        }
      }
    });
  }
});

window.onload = () => {
  connectWebSocket();
  checkAuth();
  updateStorage();
  loadMedia();
};
