/**
 * Plugins / MCP Bridge card rendering helpers (advanced operator UX).
 */
'use strict';

function pluginsBuildCardHtml(p, escapeHtml) {
  const permissions = (p.permissions || []);
  const tools = (p.tools || []);
  const mcpServers = (p.mcp_servers || []);
  const isSynthetic = Boolean(p.synthetic) || p.id === 'mcp-bridge' || !p.path;
  const runtimeRaw = p.runtime_status || (p.valid ? 'ready' : 'error');
  const runtimeLabel = ({
    loaded: 'manifest loaded',
    ready: 'ready',
    idle: 'idle (no MCP servers)',
    error: 'error',
    crashed: 'crashed',
  })[runtimeRaw] || runtimeRaw;
  const validityLabel = p.valid ? 'valid' : 'invalid';
  const permissionHtml = permissions.length
    ? permissions.map(perm => `<span class="admin-tag${String(perm).includes('shell') ? ' admin-tag-risk' : ''}">${escapeHtml(perm)}</span>`).join('')
    : '<span class="admin-tag admin-tag-muted">no permissions</span>';
  const manifestStats = [
    `${tools.length} tools`,
    `${mcpServers.length} MCP`,
    `v${p.version || 'unknown'}`,
  ];
  if (isSynthetic) manifestStats.push('synthetic');
  const runtimeChipClass = runtimeRaw === 'crashed' || runtimeRaw === 'error'
    ? 'status-error'
    : (runtimeRaw === 'ready' || runtimeRaw === 'loaded' ? 'status-ok' : 'status-info');
  const nextAction = p.next_action || '';
  const errLine = p.error || '';
  const actions = p.path
    ? '<button class="btn btn-secondary btn-sm admin-plugin-validate">Validate</button>'
    : (isSynthetic ? '<button class="btn btn-secondary btn-sm admin-plugin-open-mcp">Open MCP servers</button>' : '');
  return {
    isSynthetic,
    className: `admin-plugin-card${p.valid ? '' : ' admin-plugin-card-error'}`,
    html: `
        <div class="admin-card-header">
          <div class="admin-card-title-wrap">
            <h4 class="admin-card-title">${escapeHtml(p.name || p.id || 'Unnamed Plugin')}</h4>
            <div class="admin-card-meta">${escapeHtml(p.id || 'no-id')}</div>
          </div>
          <div class="admin-status-stack">
            <span class="admin-status-chip ${p.valid ? 'status-ok' : 'status-error'}" title="${isSynthetic ? 'Synthetic bridge manifest (no plugin.json on disk)' : 'On-disk manifest schema'}">${escapeHtml(validityLabel)}</span>
            <span class="admin-status-chip ${runtimeChipClass}" title="${isSynthetic ? 'Bridge runtime / MCP registry state' : 'Manifest parse state, not process liveness'}">${escapeHtml(runtimeLabel)}</span>
          </div>
        </div>
        <div class="admin-card-meta-row">${manifestStats.map(escapeHtml).join(' / ')}</div>
        <div class="admin-tag-row">${permissionHtml}</div>
        ${errLine ? `<div class="admin-card-error-text">${escapeHtml(errLine)}</div>` : ''}
        ${nextAction ? `<div class="admin-card-next-action"><strong>Next:</strong> ${escapeHtml(nextAction)}</div>` : ''}
        <div class="admin-card-actions">
          ${actions}
        </div>
      `,
  };
}

function pluginsWireCard(card, p, validateFn) {
  const btnValidate = card.querySelector('.admin-plugin-validate');
  if (btnValidate && validateFn) btnValidate.addEventListener('click', () => validateFn(p.path));
  const btnOpenMcp = card.querySelector('.admin-plugin-open-mcp');
  if (btnOpenMcp) btnOpenMcp.addEventListener('click', () => {
    if (typeof UI !== 'undefined' && UI.switchView) UI.switchView('settings');
    const tab = document.querySelector('.settings-sub-tab[data-panel="plugins"]');
    if (tab) tab.click();
    const mcp = document.getElementById('mcp-server-list');
    if (mcp) mcp.scrollIntoView({ behavior: 'smooth', block: 'start' });
  });
}

window.pluginsBuildCardHtml = pluginsBuildCardHtml;
window.pluginsWireCard = pluginsWireCard;
