/**
 * System health "what do I click" strip for advanced operators.
 * Expects logging.get_health.actions[] from the harness.
 * Classic script: loaded before renderer.js; mutates LoggingManager via hook.
 */
'use strict';

function healthNavigateSettingsPanel(panelId) {
  try {
    if (typeof UI !== 'undefined' && UI.switchView) {
      UI.switchView('settings');
    } else {
      const nav = document.querySelector('[data-view="settings"]');
      if (nav) nav.click();
    }
    const tab = document.querySelector(`.settings-sub-tab[data-panel="${panelId}"]`);
    if (tab) tab.click();
  } catch (err) {
    console.warn('[health_ops] settings navigation failed', err);
  }
}

function healthNavigateView(viewId) {
  try {
    if (viewId === 'mission-control') {
      if (typeof UI !== 'undefined' && UI.switchView) {
        UI.switchView('mission-control');
      } else {
        const nav = document.querySelector('[data-view="mission-control"]');
        if (nav) nav.click();
      }
      return;
    }
    if (typeof UI !== 'undefined' && UI.switchView) {
      UI.switchView(viewId);
    }
  } catch (err) {
    console.warn('[health_ops] view navigation failed', err);
  }
}

async function healthRunAction(action) {
  if (!action || !action.kind) return;
  const kind = action.kind;
  if (kind === 'settings_panel') {
    healthNavigateSettingsPanel(action.target || 'system');
    return;
  }
  if (kind === 'view' || kind === 'mission_control') {
    healthNavigateView(action.target === 'release-halt' ? 'mission-control' : (action.target || 'mission-control'));
    if (action.target === 'release-halt') {
      const btn = document.getElementById('btn-mc-release-halt');
      if (btn) {
        btn.scrollIntoView({ behavior: 'smooth', block: 'center' });
        btn.classList.add('health-action-pulse');
        setTimeout(() => btn.classList.remove('health-action-pulse'), 2400);
      }
    }
    return;
  }
  if (kind === 'reconnect') {
    try {
      if (typeof invoke === 'function') {
        await invoke('python:rpc', { method: 'system.ping', params: {} });
      }
      if (typeof StatusBar !== 'undefined' && StatusBar.refresh) StatusBar.refresh();
      else if (window.StatusBar && window.StatusBar.refresh) window.StatusBar.refresh();
    } catch (err) {
      alert(`Reconnect probe failed: ${err.message || err}. Check harness process / WS.`);
    }
    return;
  }
  if (kind === 'rpc') {
    const method = action.target;
    if (!method) return;
    if (!confirm(`Run ${method}?`)) return;
    try {
      const res = await invoke('python:rpc', { method, params: {} });
      const msg = res && (res.message || res.status || JSON.stringify(res));
      alert(`${method}: ${msg}`);
      if (typeof LoggingManager !== 'undefined' && LoggingManager.refresh) {
        LoggingManager.refresh();
      }
    } catch (err) {
      alert(`${method} failed: ${err.message || err}`);
    }
    return;
  }
  // inspect / unknown — surface detail
  alert(`${action.service || 'service'}: ${action.status || ''}\n${action.detail || action.label || ''}`);
}

function healthRenderActions(health) {
  const panel = document.querySelector('.system-health-panel');
  if (!panel) return;
  let strip = document.getElementById('health-actions-strip');
  if (!strip) {
    strip = document.createElement('div');
    strip.id = 'health-actions-strip';
    strip.className = 'health-actions-strip';
    const grid = panel.querySelector('.system-health-grid');
    if (grid) panel.insertBefore(strip, grid);
    else panel.appendChild(strip);
  }

  const actions = (health && Array.isArray(health.actions)) ? health.actions : [];
  const attention = actions.filter(a => a && a.status && a.status !== 'HEALTHY');
  if (!attention.length) {
    strip.hidden = true;
    strip.innerHTML = '';
    return;
  }

  strip.hidden = false;
  const overall = health.overall || '';
  const mode = health.mode || '';
  strip.innerHTML = `
    <div class="health-actions-header">
      <span class="health-actions-title">ATTENTION — ${escapeHtml(overall)}${mode ? ' / ' + escapeHtml(mode) : ''}</span>
      <span class="health-actions-sub">Failing / maintenance components and what to click next</span>
    </div>
    <ul class="health-actions-list">
      ${attention.map((a, i) => `
        <li class="health-action-row" data-status="${escapeHtml(a.status)}">
          <div class="health-action-meta">
            <span class="health-action-service">${escapeHtml(a.service || '')}</span>
            <span class="health-action-status">${escapeHtml(a.status || '')}</span>
            <span class="health-action-detail">${escapeHtml(a.detail || '')}</span>
          </div>
          <button type="button" class="btn btn-secondary btn-sm health-action-btn" data-action-index="${i}">
            ${escapeHtml(a.label || 'Inspect')}
          </button>
        </li>
      `).join('')}
    </ul>
  `;
  strip.querySelectorAll('.health-action-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      const idx = Number(btn.getAttribute('data-action-index'));
      healthRunAction(attention[idx]);
    });
  });
}

function escapeHtml(value) {
  const external = (typeof window !== 'undefined' && window.escapeHtml && window.escapeHtml !== escapeHtml)
    ? window.escapeHtml
    : null;
  if (external) return external(String(value ?? ''));
  return String(value ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

window.healthRenderActions = healthRenderActions;
window.healthRunAction = healthRunAction;
