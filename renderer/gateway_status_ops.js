/**
 * Gateway / chat status helpers.
 * Classic script — used by GatewayStatusManager and InferenceManager.
 *
 * A sidecar that is alive but mid-inference is busy, not degraded.
 * RPC timeouts on gateway.status / tools.catalogue / agent.get_agents
 * during a chat turn must not paint the red System Degraded bar.
 */
'use strict';

function gatewayInterpretStatus(res, err) {
  const timeout = err && /rpc timeout/i.test(String(err.message || err));
  if (res && res.busy) {
    return {
      kind: 'busy',
      message: res.busy_detail || 'Tala is busy',
    };
  }
  if (timeout) {
    return { kind: 'busy', message: 'Tala is busy' };
  }
  if (err) {
    return {
      kind: 'degraded',
      message: 'Connection failed: ' + (err.message || err),
    };
  }
  if (res && res.status === 'online') {
    return { kind: 'ok', message: '' };
  }
  return { kind: 'degraded', message: 'Gateway reported offline or degraded status.' };
}

function gatewayApplyPanel(panel, msgsEl, labelEl, interpretation) {
  if (!panel) return;
  if (interpretation.kind === 'ok') {
    panel.style.display = 'none';
    panel.classList.remove('busy-state');
    return;
  }
  panel.style.display = 'flex';
  panel.classList.toggle('busy-state', interpretation.kind === 'busy');
  if (labelEl) {
    labelEl.textContent = interpretation.kind === 'busy' ? 'Tala is busy:' : 'System Degraded:';
  }
  if (msgsEl) msgsEl.textContent = interpretation.message;
}

function showInferenceFallbackNotice(params) {
  const from = (params && params.from) || 'remote';
  const to = (params && params.to) || 'local';
  const reason = (params && params.reason) || 'failed';
  const text = `⚠️ ${reason}: "${from}" → "${to}"`;
  if (window.ChatManager && typeof window.ChatManager.appendMessage === 'function') {
    window.ChatManager.appendMessage({
      sender: 'system',
      body: text,
      timestamp: new Date().toISOString(),
      isSystem: true,
    });
  }
  if (window.AgentChat && typeof window.AgentChat.appendMessage === 'function') {
    window.AgentChat.appendMessage('agent', 'Tala', text);
  }
  if (window.IPCLog && typeof window.IPCLog.append === 'function') {
    window.IPCLog.append('warn', `inference.fallback: ${from} → ${to}`);
  }
}

window.gatewayInterpretStatus = gatewayInterpretStatus;
window.gatewayApplyPanel = gatewayApplyPanel;
window.showInferenceFallbackNotice = showInferenceFallbackNotice;

const GatewayStatusManager = {
  _panel: null,
  _msgs: null,
  _btnRetry: null,
  _btnLogs: null,
  _pollingInterval: null,

  init() {
    this._panel = document.querySelector('#degraded-state-panel');
    this._msgs = document.querySelector('#degraded-state-messages');
    this._btnRetry = document.querySelector('#btn-degraded-retry');
    this._btnLogs = document.querySelector('#btn-degraded-logs');
    if (this._btnRetry) this._btnRetry.addEventListener('click', () => this.checkStatus());
    if (this._btnLogs) {
      this._btnLogs.addEventListener('click', () => {
        if (window.tala && window.tala.openLogs) window.tala.openLogs();
      });
    }
    this.startPolling();
  },

  startPolling() {
    if (this._pollingInterval) clearInterval(this._pollingInterval);
    this._pollingInterval = setInterval(() => this.checkStatus(), 10000);
    setTimeout(() => this.checkStatus(), 2000);
  },

  async checkStatus() {
    try {
      const res = await invoke('gateway.status', {});
      this.renderAuthPosture(res);
      this.applyInterpretation(gatewayInterpretStatus(res, null));
    } catch (err) {
      const interpretation = gatewayInterpretStatus(null, err);
      if (interpretation.kind !== 'busy') this.renderAuthPosture(null, err);
      this.applyInterpretation(interpretation);
    }
  },

  applyInterpretation(interpretation) {
    gatewayApplyPanel(
      this._panel,
      this._msgs,
      document.querySelector('#degraded-state-label'),
      interpretation,
    );
  },

  renderAuthPosture(status, err) {
    const card = document.querySelector('#gateway-auth-posture');
    if (!card) return;
    const title = document.querySelector('#gateway-auth-title');
    const copy = document.querySelector('#gateway-auth-copy');
    const credentials = document.querySelector('#gateway-auth-credentials');
    const remotes = document.querySelector('#gateway-auth-remotes');
    const contacts = document.querySelector('#gateway-auth-contacts');

    const authCredentials = Number((status && status.auth_credentials_count) || 0);
    const authedRemotes = Number((status && status.authenticated_remote_instances) || 0);
    const trustedContacts = Number((status && status.trusted_contacts_count) || 0);
    const ready = Boolean(status && status.auth_ready);

    card.classList.toggle('ready', ready);
    card.classList.toggle('degraded', Boolean(err) || !ready);
    if (title) {
      title.textContent = err
        ? 'Gateway unreachable'
        : (ready ? 'Auth boundary configured' : 'Auth boundary needs credentials');
    }
    if (copy) {
      copy.textContent = err
        ? `Status check failed: ${err.message || err}`
        : (ready
          ? 'Remote gateway requests have credential, peer, or authenticated runtime material available without exposing secrets to the UI.'
          : 'Add an auth credential, authenticated remote instance, or trusted peer before exposing remote gateway workflows.');
    }
    if (credentials) credentials.textContent = authCredentials.toString();
    if (remotes) remotes.textContent = authedRemotes.toString();
    if (contacts) contacts.textContent = trustedContacts.toString();
  },
};

window.GatewayStatusManager = GatewayStatusManager;
