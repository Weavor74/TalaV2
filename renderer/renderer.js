/**
 * renderer.js — Tala Cognitive Engine Room
 * Renderer-side ES module.  Runs in the sandboxed BrowserWindow context.
 * All backend communication goes through window.tala.invoke (contextBridge).
 */

'use strict';

/* ═══════════════════════════════════════════════════════════════════
   1. Bootstrap
   ═══════════════════════════════════════════════════════════════════ */

/** Wait for DOM to be ready before touching any elements. */
document.addEventListener('DOMContentLoaded', () => {
  const initSafe = (name, fn) => {
    try {
      console.log(`[renderer:info] Initializing ${name}...`);
      fn();
    } catch (e) {
      console.error(`[renderer:error] Failed to initialize ${name}:`, e);
    }
  };

  initSafe('UIController', () => UIController.init());
  initSafe('StatusBar', () => StatusBar.init());
  initSafe('TabManager', () => TabManager.init());
  initSafe('Navigation', () => Navigation.init());
  initSafe('ProfileManager', () => ProfileManager.init());
  initSafe('ContactsManager', () => ContactsManager.init());
  initSafe('FileExplorer', () => FileExplorer.init());
  initSafe('SettingsDashboard', () => SettingsDashboard.init());
  initSafe('AgentChat', () => AgentChat.init());
  initSafe('PanelResizer', () => PanelResizer.init());
  initSafe('CommandBar', () => CommandBar.init());
  initSafe('WebBrowser', () => WebBrowser.init());
  initSafe('NodeEditor', () => NodeEditor.init());
  initSafe('IPCLog', () => IPCLog.init());
  initSafe('NodeGrid', () => NodeGrid.init());
  initSafe('NodeGraph', () => NodeGraph.init());
  initSafe('BlockchainViewer', () => BlockchainViewer.init());
  initSafe('TerminalConsole', () => TerminalConsole.init());
  initSafe('MissionControl', () => MissionControl.init());
  initSafe('AgentBuilder', () => AgentBuilder.init());
  initSafe('TelemetryPoller', () => TelemetryPoller.start());
});

/* ═══════════════════════════════════════════════════════════════════
   2. Helpers
   ═══════════════════════════════════════════════════════════════════ */

/**
 * Safely query a single DOM element.
 * @param {string} selector
 * @param {Element} [root=document]
 * @returns {Element|null}
 */
function qs(selector, root = document) {
  return root.querySelector(selector);
}

/**
 * Format a timestamp as HH:MM:SS.
 * @param {Date} [d]
 * @returns {string}
 */
function fmtTime(d = new Date()) {
  return d.toTimeString().slice(0, 8);
}

/**
 * Invoke a whitelisted IPC channel with a payload.
 * Falls back gracefully if the contextBridge is not available (e.g., during tests).
 *
 * // Context Bridge Relay: Provides a type-safe boundary between the sandboxed UI (renderer)
 * // and the Node.js/Python backend. All OS-level operations (file I/O, database queries)
 * // must pass through this funnel to prevent malicious Neural Nodes from escaping the browser sandbox.
 *
 * @param {string} channel
 * @param {*} [data]
 * @returns {Promise<*>}
 */
async function invoke(channel, data) {
  if (window.tala && typeof window.tala.invoke === 'function') {
    return window.tala.invoke(channel, data);
  }
  console.warn('[renderer] window.tala not available — running in degraded mode');
  return null;
}

/* ═══════════════════════════════════════════════════════════════════
   3. UIController — top-level app shell
   ═══════════════════════════════════════════════════════════════════ */

const UIController = {
  init() {
    // Inject version string from package if available, else leave default
    const versionEls = document.querySelectorAll('#app-version, #status-version');
    versionEls.forEach(el => {
      // Version injected by main process via a python:rpc call during startup
      invoke('python:rpc', { method: 'app.version' })
        .then(result => {
          if (result?.version) {
            el.textContent = `v${result.version}`;

          }
        })
        .catch(() => { /* ignore, default text stays */ });
    });
  },
};

/* ═══════════════════════════════════════════════════════════════════
   4. Navigation
   ═══════════════════════════════════════════════════════════════════ */

const Navigation = {
  VIEW_MAP: {
    'neural-node': 'view-neural-node',
    'browser':     'view-browser',
    'research':    'view-research',
    'blockchain':  'view-blockchain',
    'pipelines':   'view-pipelines',
    'vault':       'view-vault',
    'contacts':    'view-contacts',

    'settings':    'view-settings',
    'logs':        'view-logs',
    'mission-control': 'view-mission-control',
  },

  init() {
    const navBtns = document.querySelectorAll('.sidebar-icon-btn[data-view]');
    navBtns.forEach(btn => {
      btn.addEventListener('click', () => {
        const view = btn.dataset.view;
        const titleMap = {
          'neural-node': 'Neural Nodes',
          'browser':     'Web Browser',
          'research':    'Research Library',
          'blockchain':  'Blockchain Ledger',
          'agent-builder': 'Agent Builder',
          'logs':        'System Logs',
          'vault':       'Vault Directory',
          'profiles':    'User Profiles',
          'contacts':    'Contacts Directory',
          'settings':    'System Settings',
          'mission-control': 'Mission Control'
        };
        TabManager.openTab(view, titleMap[view] || view, 'view');
      });
    });
  },

  _createViewSection(id, key) {
    const section = document.createElement('section');
    section.className = 'view';
    section.id = id;
    section.setAttribute('aria-label', key);
    section.innerHTML = `
      <div class="view-header">
        <h1 class="view-title">${this._titleCase(key)}</h1>
      </div>
      <div class="empty-state">
        <p>This panel is not yet implemented.</p>
      </div>`;
    return section;
  },

  _titleCase(str) {
    return str.replace(/-/g, ' ').replace(/\b\w/g, c => c.toUpperCase());
  },
};

const TabManager = {
  tabs: [
    { id: 'neural-node', title: 'Neural Nodes', type: 'view' }
  ],
  activeTabId: 'neural-node',

  init() {
    this._container = qs('#tabs-container');
    this._btnSave = qs('#btn-save-file');
    this._editorTextarea = qs('#editor-textarea');
    this._modeToggles = qs('#editor-mode-toggles');
    this._btnEditMode = qs('#btn-editor-edit-mode');
    this._btnPreviewMode = qs('#btn-editor-preview-mode');
    this._previewPane = qs('#editor-preview-pane');

    if (this._btnSave) {
      this._btnSave.addEventListener('click', () => this.saveActiveFile());
    }

    if (this._btnEditMode) {
      this._btnEditMode.addEventListener('click', () => this.setEditorMode('edit'));
    }
    if (this._btnPreviewMode) {
      this._btnPreviewMode.addEventListener('click', () => this.setEditorMode('preview'));
    }

    this.render();
  },

  openTab(id, title, type, filePath = null) {
    let tab = this.tabs.find(t => t.id === id);
    if (!tab) {
      tab = { id, title, type, filePath, contentLoaded: false, originalContent: '', editorMode: null };
      this.tabs.push(tab);
    }
    this.switchTo(id);
  },

  switchTo(id) {
    this.activeTabId = id;
    
    document.querySelectorAll('.view').forEach(view => {
      view.classList.remove('active');
      view.hidden = true;
    });

    const tab = this.tabs.find(t => t.id === id);
    if (!tab) return;

    if (tab.type === 'view') {
      if (this._btnSave) this._btnSave.style.display = 'none';
      if (this._modeToggles) this._modeToggles.style.display = 'none';
      this.setEditorMode('edit');

      const sectionId = Navigation.VIEW_MAP[id] || `view-${id}`;
      let section = document.getElementById(sectionId);
      if (!section) {
        section = Navigation._createViewSection(sectionId, id);
        const viewsContainer = qs('#views-container');
        if (viewsContainer) viewsContainer.appendChild(section);
      }
      if (section) {
        section.hidden = false;
        section.classList.add('active');
      }

      if (id === 'blockchain') {
        BlockchainViewer.load();
      }

      if (id === 'contacts') {
        ContactsManager.load();
      }

      if (id === 'research') {
        if (!ResearchManager._initialized) {
          ResearchManager.init();
        }
        ResearchManager.populateSearchProviders();
      }

      document.querySelectorAll('.sidebar-icon-btn').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.view === id);
      });

    } else if (tab.type === 'file') {
      if (this._btnSave) this._btnSave.style.display = 'inline-block';

      const editorSection = document.getElementById('view-editor');
      if (editorSection) {
        editorSection.hidden = false;
        editorSection.classList.add('active');
      }

      const isPreviewable = tab.filePath && (tab.filePath.endsWith('.md') || tab.filePath.endsWith('.html'));
      if (isPreviewable && this._modeToggles) {
        this._modeToggles.style.display = 'flex';
        const mode = tab.editorMode || 'preview';
        this.setEditorMode(mode);
      } else {
        if (this._modeToggles) this._modeToggles.style.display = 'none';
        this.setEditorMode('edit');
      }

      this.loadTabFileContent(tab);

      document.querySelectorAll('.sidebar-icon-btn').forEach(btn => {
        btn.classList.remove('active');
      });
    }

    this.render();
  },

  setEditorMode(mode) {
    const tab = this.tabs.find(t => t.id === this.activeTabId);
    if (tab) {
      tab.editorMode = mode;
    }

    if (mode === 'preview') {
      if (this._btnEditMode) this._btnEditMode.classList.remove('active');
      if (this._btnPreviewMode) this._btnPreviewMode.classList.add('active');
      if (this._editorTextarea) this._editorTextarea.style.display = 'none';
      if (this._previewPane) {
        this._previewPane.style.display = 'block';
        this.updatePreview(tab);
      }
    } else {
      if (this._btnEditMode) this._btnEditMode.classList.add('active');
      if (this._btnPreviewMode) this._btnPreviewMode.classList.remove('active');
      if (this._editorTextarea) this._editorTextarea.style.display = 'block';
      if (this._previewPane) this._previewPane.style.display = 'none';
    }
  },

  updatePreview(tab) {
    if (!tab || !this._previewPane || !this._editorTextarea) return;
    const text = this._editorTextarea.value;
    this._previewPane.innerHTML = '';

    if (tab.filePath.endsWith('.md')) {
      this._previewPane.innerHTML = this.renderMarkdown(text);
    } else if (tab.filePath.endsWith('.html')) {
      const iframe = document.createElement('iframe');
      iframe.style.width = '100%';
      iframe.style.height = '100%';
      iframe.style.border = 'none';
      iframe.sandbox = 'allow-same-origin';
      this._previewPane.appendChild(iframe);

      const doc = iframe.contentDocument || iframe.contentWindow.document;
      doc.open();

      let processedText = text;
      if (!text.includes('<style>') && !text.includes('stylesheet')) {
        const injectedStyles = `
          <style>
            body {
              background-color: #04060b;
              color: #f1f5f9;
              font-family: 'Outfit', 'Inter', sans-serif;
              line-height: 1.6;
              padding: 20px;
              margin: 0;
            }
            h1, h2, h3, h4 { color: #38bdf8; border-bottom: 1px solid rgba(255,255,255,0.06); padding-bottom: 8px; margin-top: 1.5rem; }
            pre, code { background: #0c0f1d; color: #a78bfa; padding: 4px 8px; border-radius: 4px; font-family: monospace; }
            article { max-width: 800px; margin: 0 auto; }
            section { margin-bottom: 20px; background: rgba(255,255,255,0.02); padding: 16px; border-radius: 8px; border: 1px solid rgba(255,255,255,0.05); }
            a { color: #38bdf8; text-decoration: none; }
            a:hover { text-decoration: underline; }
          </style>
        `;
        processedText = text.replace('</head>', injectedStyles + '</head>');
        if (processedText === text) {
          processedText = injectedStyles + text;
        }
      }

      doc.write(processedText);
      doc.close();
    }
  },

  renderMarkdown(md) {
    if (!md) return '';
    let html = md;

    html = html.replace(/&/g, '&amp;')
               .replace(/</g, '&lt;')
               .replace(/>/g, '&gt;');

    html = html.replace(/```(\w*)\n([\s\S]*?)\n```/g, (match, lang, code) => {
      return `<pre><code class="language-${lang}">${code}</code></pre>`;
    });

    html = html.replace(/`([^`]+)`/g, '<code>$1</code>');

    html = html.replace(/^####\s+(.*)$/gm, '<h4>$1</h4>')
               .replace(/^###\s+(.*)$/gm, '<h3>$1</h3>')
               .replace(/^##\s+(.*)$/gm, '<h2>$1</h2>')
               .replace(/^#\s+(.*)$/gm, '<h1>$1</h1>');

    html = html.replace(/^&gt;\s+(.*)$/gm, '<blockquote>$1</blockquote>');

    html = html.replace(/^[\*\-]\s+(.*)$/gm, '<li>$1</li>');
    html = html.replace(/(<li>.*<\/li>)/gs, '<ul>$1<\/ul>');

    html = html.replace(/\*\*([^\*]+)\*\*/g, '<strong>$1</strong>');
    html = html.replace(/\*([^\*]+)\*/g, '<em>$1</em>');

    html = html.replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" target="_blank">$1</a>');

    const lines = html.split('\n\n');
    const parsedLines = lines.map(line => {
      const trimmed = line.trim();
      if (!trimmed) return '';
      if (trimmed.startsWith('<h') || 
          trimmed.startsWith('<pre') || 
          trimmed.startsWith('<blockquote') || 
          trimmed.startsWith('<ul') || 
          trimmed.startsWith('<ol') || 
          trimmed.startsWith('<li>')) {
        return trimmed;
      }
      return `<p>${trimmed.replace(/\n/g, '<br>')}</p>`;
    });

    return parsedLines.join('\n');
  },

  async loadTabFileContent(tab) {
    if (!this._editorTextarea) return;
    
    if (tab.editorMode === 'preview' && this._previewPane) {
      this._previewPane.innerHTML = '<div style="color: var(--text-secondary); padding: 20px;">Loading preview...</div>';
    }

    this._editorTextarea.value = 'Loading file content...';
    this._editorTextarea.disabled = true;

    try {
      const res = await invoke('file:read', { filePath: tab.filePath });
      if (res && res.ok) {
        tab.originalContent = res.data;
        this._editorTextarea.value = res.data;
        if (tab.editorMode === 'preview') {
          this.updatePreview(tab);
        }
      } else {
        this._editorTextarea.value = `Failed to read file: ${JSON.stringify(res)}`;
      }
    } catch (err) {
      this._editorTextarea.value = `Error loading file: ${err.message ?? err}`;
    } finally {
      this._editorTextarea.disabled = false;
    }
  },

  async saveActiveFile() {
    const tab = this.tabs.find(t => t.id === this.activeTabId);
    if (!tab || tab.type !== 'file' || !this._editorTextarea) return;

    const data = this._editorTextarea.value;
    IPCLog.append('info', `Saving file: ${tab.filePath}...`);

    try {
      const res = await invoke('file:write', { filePath: tab.filePath, data });
      if (res && res.ok) {
        tab.originalContent = data;
        IPCLog.append('ok', `Successfully saved: ${tab.filePath}`);
      } else {
        IPCLog.append('error', `Failed to save file: ${JSON.stringify(res)}`);
      }
    } catch (err) {
      IPCLog.append('error', `Error saving file: ${err.message ?? err}`);
    }
  },

  closeTab(id) {
    const tabIndex = this.tabs.findIndex(t => t.id === id);
    if (tabIndex === -1) return;

    this.tabs.splice(tabIndex, 1);

    if (this.activeTabId === id) {
      const nextActiveIndex = Math.max(0, tabIndex - 1);
      const nextActiveTab = this.tabs[nextActiveIndex] || this.tabs[0];
      if (nextActiveTab) {
        this.switchTo(nextActiveTab.id);
      }
    } else {
      this.render();
    }
  },

  render() {
    if (!this._container) return;
    this._container.innerHTML = '';

    this.tabs.forEach(tab => {
      const tabEl = document.createElement('div');
      tabEl.className = `workspace-tab${tab.id === this.activeTabId ? ' active' : ''}`;
      tabEl.setAttribute('role', 'tab');
      tabEl.dataset.tabId = tab.id;

      const label = document.createElement('span');
      label.className = 'tab-label';
      label.textContent = tab.title;
      tabEl.appendChild(label);

      if (tab.id !== 'neural-node') {
        const closeBtn = document.createElement('span');
        closeBtn.className = 'tab-close-btn';
        closeBtn.textContent = '×';
        closeBtn.addEventListener('click', (e) => {
          e.stopPropagation();
          this.closeTab(tab.id);
        });
        tabEl.appendChild(closeBtn);
      }

      tabEl.addEventListener('click', () => {
        this.switchTo(tab.id);
      });

      this._container.appendChild(tabEl);
    });
  }
};

const FileExplorer = {
  expandedPaths: new Set(),
  allEntries: [],

  init() {
    this._drawer = qs('#sidebar-drawer');
    this._btnToggle = qs('#btn-toggle-drawer');
    this._btnRefresh = qs('#btn-refresh-explorer');
    this._treeChildren = qs('#explorer-tree-children');

    this.expandedPaths.add('nodes');

    if (this._btnToggle) {
      this._btnToggle.addEventListener('click', () => this.toggleDrawer());
    }

    if (this._btnRefresh) {
      this._btnRefresh.addEventListener('click', () => this.load());
    }

    this.load();
  },

  toggleDrawer() {
    if (!this._drawer) return;
    const isCollapsed = this._drawer.classList.toggle('collapsed');
    
    setTimeout(() => {
      if (typeof TerminalConsole !== 'undefined' && typeof TerminalConsole.resizeTerminals === 'function') {
        TerminalConsole.resizeTerminals();
      }
    }, 150);

    if (!isCollapsed) {
      this.load();
    }
  },

  async load() {
    if (!this._treeChildren) return;
    this._treeChildren.innerHTML = '<div class="tree-item loading-item">Loading directory...</div>';

    try {
      const result = await invoke('file:list', { dir: '.', recursive: true });
      if (result && result.ok && Array.isArray(result.entries)) {
        this.allEntries = result.entries;
        this.renderTree();
      } else {
        this._treeChildren.innerHTML = '<div class="tree-item loading-item">Failed to load directory</div>';
        IPCLog.append('error', `FileExplorer.load failed: ${JSON.stringify(result)}`);
      }
    } catch (err) {
      this._treeChildren.innerHTML = '<div class="tree-item loading-item">Error loading directory</div>';
      IPCLog.append('error', `FileExplorer.load error: ${err.message ?? err}`);
    }
  },

  renderTree() {
    if (!this._treeChildren || !this.allEntries) return;
    this._treeChildren.innerHTML = '';

    const root = this.buildTree(this.allEntries);
    this.renderNode(root, this._treeChildren, 0);
  },

  buildTree(entries) {
    const root = { name: 'root', type: 'directory', relativePath: '', children: [] };
    const paths = { '': root };

    function getOrCreateFolder(dirPath) {
      if (paths[dirPath]) return paths[dirPath];
      const parts = dirPath.split(/[\\/]/);
      const name = parts.pop();
      const parentPath = parts.join('/');
      const parent = getOrCreateFolder(parentPath);
      const folder = { name, type: 'directory', relativePath: dirPath, children: [] };
      parent.children.push(folder);
      paths[dirPath] = folder;
      return folder;
    }

    for (const entry of entries) {
      if (entry.relativePath.includes('node_modules') || entry.relativePath.startsWith('.')) {
        continue;
      }
      if (entry.type === 'directory') {
        getOrCreateFolder(entry.relativePath);
      } else {
        const parts = entry.relativePath.split(/[\\/]/);
        parts.pop();
        const parentPath = parts.join('/');
        const parent = getOrCreateFolder(parentPath);
        parent.children.push({
          name: entry.name,
          type: entry.type,
          relativePath: entry.relativePath,
          size: entry.size
        });
      }
    }
    return root;
  },

  renderNode(node, container, depth) {
    if (node.relativePath === '') {
      node.children.sort((a, b) => {
        if (a.type === 'directory' && b.type !== 'directory') return -1;
        if (a.type !== 'directory' && b.type === 'directory') return 1;
        return a.name.localeCompare(b.name);
      });
      node.children.forEach(child => this.renderNode(child, container, depth));
      return;
    }

    const itemEl = document.createElement('div');
    itemEl.className = 'tree-item';
    if (node.type === 'directory') {
      itemEl.classList.add('tree-folder');
      if (this.expandedPaths.has(node.relativePath)) {
        itemEl.classList.add('expanded');
      }
    } else {
      itemEl.classList.add('tree-file');
    }
    itemEl.dataset.path = node.relativePath;

    const iconSpan = document.createElement('span');
    iconSpan.className = 'tree-item-icon';
    if (node.type === 'directory') {
      const isExpanded = this.expandedPaths.has(node.relativePath);
      iconSpan.innerHTML = `
        <svg class="folder-caret ${isExpanded ? 'expanded' : ''}" viewBox="0 0 20 20" fill="currentColor" width="10" height="10" style="margin-right: 6px; transform: rotate(${isExpanded ? '90deg' : '0deg'}); transition: transform 0.15s; pointer-events: none;">
          <path fill-rule="evenodd" d="M7.293 14.707a1 1 0 010-1.414L10.586 10 7.293 6.707a1 1 0 011.414-1.414l4 4a1 1 0 010 1.414l-4 4a1 1 0 01-1.414 0z" clip-rule="evenodd" />
        </svg>
        <svg viewBox="0 0 20 20" fill="currentColor" width="14" height="14" style="pointer-events: none;">
          <path d="M2 6a2 2 0 012-2h5l2 2h5a2 2 0 012 2v6a2 2 0 01-2 2H4a2 2 0 01-2-2V6z" />
        </svg>
      `;
    } else {
      iconSpan.innerHTML = `
        <svg viewBox="0 0 20 20" fill="currentColor" width="14" height="14" style="margin-left: 16px; pointer-events: none;">
          <path fill-rule="evenodd" d="M4 4a2 2 0 012-2h4.586A2 2 0 0112 2.586L15.414 6A2 2 0 0116 7.414V16a2 2 0 01-2 2H6a2 2 0 01-2-2V4z" clip-rule="evenodd" />
        </svg>
      `;
    }
    itemEl.appendChild(iconSpan);

    const label = document.createElement('span');
    label.className = 'tree-item-label';
    label.textContent = node.name;
    itemEl.appendChild(label);

    itemEl.style.paddingLeft = `${depth * 12 + 8}px`;
    container.appendChild(itemEl);

    if (node.type === 'directory') {
      itemEl.addEventListener('click', (e) => {
        e.stopPropagation();
        if (this.expandedPaths.has(node.relativePath)) {
          this.expandedPaths.delete(node.relativePath);
        } else {
          this.expandedPaths.add(node.relativePath);
        }
        this.renderTree();
      });
    } else {
      itemEl.addEventListener('click', (e) => {
        e.stopPropagation();
        TabManager.openTab(node.relativePath, node.name, 'file', node.relativePath);
      });
    }

    if (node.type === 'directory' && this.expandedPaths.has(node.relativePath)) {
      node.children.sort((a, b) => {
        if (a.type === 'directory' && b.type !== 'directory') return -1;
        if (a.type !== 'directory' && b.type === 'directory') return 1;
        return a.name.localeCompare(b.name);
      });
      node.children.forEach(child => this.renderNode(child, container, depth + 1));
    }
  }
};

const SettingsDashboard = {
  init() {
    // Env selector
    const envBtns = document.querySelectorAll('.btn-settings-env');
    envBtns.forEach(btn => {
      btn.addEventListener('click', () => {
        envBtns.forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        IPCLog.append('info', `Environment: ${btn.dataset.env.toUpperCase()}`);
      });
    });

    // Scope selector
    const scopeBtns = document.querySelectorAll('.btn-settings-scope');
    scopeBtns.forEach(btn => {
      btn.addEventListener('click', () => {
        scopeBtns.forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        IPCLog.append('info', `Scope: ${btn.dataset.scope.toUpperCase()}`);
      });
    });

    // Unified panel tab switching
    const tabs = document.querySelectorAll('#settings-tab-nav .settings-sub-tab');
    tabs.forEach(tab => {
      tab.addEventListener('click', () => {
        tabs.forEach(t => t.classList.remove('active'));
        tab.classList.add('active');
        const panelId = tab.dataset.panel;
        this._showPanel(panelId);
        IPCLog.append('info', `Settings → ${panelId.toUpperCase()}`);
      });
    });

    // Show inference panel on init (it's the default active)
    this._showPanel('inference');
  },

  _showPanel(panelId) {
    document.querySelectorAll('.settings-panel').forEach(p => {
      p.hidden = true;
      p.classList.remove('active');
    });
    const target = document.getElementById(`settings-panel-${panelId}`);
    if (target) {
      target.hidden = false;
      target.classList.add('active');
    }
    // Init panel managers on first activation
    if (panelId === 'inference' && !InferenceManager._initialized) {
      InferenceManager.init();
    }
    if (panelId === 'agent' && !AgentManager._initialized) {
      AgentManager.init();
    }
    if (panelId === 'workflows' && !WorkflowManager._initialized) {
      WorkflowManager.init();
    }
    if (panelId === 'guardrails' && !GuardrailsManager._initialized) {
      GuardrailsManager.init();
    }
    if (panelId === 'search' && !SearchProviderManager._initialized) {
      SearchProviderManager.init();
    }
    if (panelId === 'system' && !SystemManager._initialized) {
      SystemManager.init();
    }
    if (panelId === 'git' && !GitProviderManager._initialized) {
      GitProviderManager.init();
    }
    if (panelId === 'storage' && !StorageProviderManager._initialized) {
      StorageProviderManager.init();
    }
    if (panelId === 'database' && !DatabaseProviderManager._initialized) {
      DatabaseProviderManager.init();
    }
    if (panelId === 'runtime' && !RuntimeInstanceManager._initialized) {
      RuntimeInstanceManager.init();
    }
    if (panelId === 'auth' && !AuthManager._initialized) {
      AuthManager.init();
    }
    if (panelId === 'firewall' && !FirewallManager._initialized) {
      FirewallManager.init();
    }
    if (panelId === 'identity') {
      ProfileManager.loadProfile();
    }
    if (panelId === 'logging' && !LoggingManager._initialized) {
      LoggingManager.init();
    }
    if (panelId === 'architecture') {
      SwarmDashboardManager.init();
    }
  },
};

/* ═══════════════════════════════════════════════════════════════════
   SwarmDashboardManager — Swarm Orchestrator status panel
   ═══════════════════════════════════════════════════════════════════ */
const SwarmDashboardManager = {
  _initialized: false,
  init() {
    if (this._initialized) return;
    this._initialized = true;
    
    const refreshBtn = document.getElementById('btn-refresh-swarm');
    if (refreshBtn) {
      refreshBtn.addEventListener('click', () => this.refresh());
    }
    
    // Auto-refresh every 5 seconds when visible
    setInterval(() => {
      const panel = document.getElementById('settings-panel-architecture');
      if (panel && !panel.hidden) {
        this.refresh();
      }
    }, 5000);
    
    this.refresh();
  },
  
  async refresh() {
    try {
      const res = await window.tala.invoke('swarm.fleet.status', {});
      const tbody = document.getElementById('swarm-tbody');
      if (!tbody) return;
      
      if (!res.success || !res.tasks || res.tasks.length === 0) {
        tbody.innerHTML = `
          <tr>
            <td colspan="6" style="padding: var(--space-3); text-align: center; color: var(--text-muted);">No active background swarm worktrees found. Click Refresh to probe.</td>
          </tr>
        `;
        return;
      }
      
      tbody.innerHTML = res.tasks.map(t => `
        <tr style="border-bottom: 1px solid rgba(255,255,255,0.05);">
          <td style="padding: var(--space-2); color: var(--accent-light); font-family: monospace;">${t.id}</td>
          <td style="padding: var(--space-2); max-width: 200px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;">${t.description}</td>
          <td style="padding: var(--space-2); font-family: monospace;">${t.branch_name || 'N/A'}</td>
          <td style="padding: var(--space-2); font-family: monospace;">${t.port || 'N/A'}</td>
          <td style="padding: var(--space-2);">
            <span style="display: inline-block; padding: 2px 6px; border-radius: 4px; font-size: 0.7rem; 
                         background: ${t.container_status.includes('Up') ? 'rgba(34,197,94,0.1)' : 'rgba(239,68,68,0.1)'}; 
                         color: ${t.container_status.includes('Up') ? '#22c55e' : '#ef4444'};">
              ${t.container_status}
            </span>
          </td>
          <td style="padding: var(--space-2); text-align: right;">
            <div style="display: flex; gap: 4px; justify-content: flex-end;">
              <button onclick="SwarmDashboardManager.pauseTask('${t.id}')" class="btn-primary" style="font-size: 0.7rem; padding: 2px 6px; background: #eab308; border-color: #eab308; color: #1e1e24;">Pause</button>
              <button onclick="SwarmDashboardManager.resumeTask('${t.id}')" class="btn-primary" style="font-size: 0.7rem; padding: 2px 6px; background: #22c55e; border-color: #22c55e; color: #1e1e24;">Resume</button>
              <button onclick="SwarmDashboardManager.terminateTask('${t.id}')" class="btn-primary" style="font-size: 0.7rem; padding: 2px 6px; background: #ef4444; border-color: #ef4444; color: white;">Kill</button>
            </div>
          </td>
        </tr>
      `).join('');
    } catch (err) {
      console.error("Swarm Dashboard Refresh error:", err);
    }
  },
  
  async pauseTask(taskId) {
    try {
      await window.tala.invoke('swarm.fleet.pause', { task_id: taskId });
      this.refresh();
    } catch (err) {
      alert("Pause failed: " + err);
    }
  },
  
  async resumeTask(taskId) {
    try {
      await window.tala.invoke('swarm.fleet.resume', { task_id: taskId });
      this.refresh();
    } catch (err) {
      alert("Resume failed: " + err);
    }
  },
  
  async terminateTask(taskId) {
    if (!confirm("Are you sure you want to terminate task " + taskId + " and prune its resources?")) return;
    try {
      await window.tala.invoke('swarm.fleet.terminate', { task_id: taskId });
      this.refresh();
    } catch (err) {
      alert("Termination failed: " + err);
    }
  }
};

window.SwarmDashboardManager = SwarmDashboardManager;

/* ═══════════════════════════════════════════════════════════════════
   InferenceManager — provider CRUD, priority chain, add modal
   ═══════════════════════════════════════════════════════════════════ */
const InferenceManager = {
  _initialized: false,
  _providers: [
    {
      id: 'embedded-builtin',
      type: 'embedded',
      name: 'Tala Embedded Engine',
      model: 'qwen2.5-0.5b',
      status: 'unknown',
      locked: true,
    }
  ],
  _selectedType: null,
  _editingId: null,
  _downloadInterval: null,
  _engineIntervals: {},
  _detecting: false,
  _routeSelection: '',
  _engines: [],

  init() {
    this._initialized = true;
    this._list      = qs('#provider-list');
    this._engineList = qs('#embedded-engine-list');
    this._btnAdd    = qs('#btn-add-provider');
    this._btnDetect = qs('#btn-detect-providers');
    this._modal     = qs('#modal-add-provider');
    this._stepType  = qs('#modal-step-type');
    this._stepDet   = qs('#modal-step-details');
    this._btnBack   = qs('#btn-modal-provider-back');
    this._btnSave   = qs('#btn-modal-provider-save');
    this._btnCancel = qs('#btn-modal-provider-cancel');
    this._btnClose  = qs('#btn-modal-provider-close');
    this._sessionInput = qs('#inference-session-id');
    this._modelPicker = qs('#inference-model-picker');
    this._visionPicker = qs('#inference-vision-picker');
    this._routeStatus = qs('#inference-route-status');
    this._routeActive = qs('#inference-route-active');
    this._btnUseSession = qs('#btn-inference-use-session');
    this._btnAutoRoute = qs('#btn-inference-auto-route');

    if (this._btnAdd)    this._btnAdd.addEventListener('click',   () => this.openModal());
    if (this._btnDetect) this._btnDetect.addEventListener('click', () => this.detectLocalProviders());
    if (this._btnClose)  this._btnClose.addEventListener('click',  () => this.closeModal());
    if (this._btnCancel) this._btnCancel.addEventListener('click', () => this.closeModal());
    if (this._btnBack)   this._btnBack.addEventListener('click',   () => this.goBack());
    if (this._btnSave)   this._btnSave.addEventListener('click',   () => this.saveProvider());
    if (this._modelPicker) this._modelPicker.addEventListener('change', () => this.previewRouteSelection());
    if (this._visionPicker) this._visionPicker.addEventListener('change', () => this.saveVisionProvider());
    if (this._btnUseSession) this._btnUseSession.addEventListener('click', () => this.pinSelectedProvider());
    if (this._btnAutoRoute) this._btnAutoRoute.addEventListener('click', () => this.clearForcedProvider());
    if (this._sessionInput) {
      this._sessionInput.value = this.getSessionId();
      this._sessionInput.addEventListener('change', () => this.setSessionId(this._sessionInput.value));
    }

    // Type card clicks
    document.querySelectorAll('.provider-type-card').forEach(card => {
      card.addEventListener('click', () => this.showDetails(card.dataset.type));
    });

    // Close on backdrop click
    if (this._modal) {
      this._modal.addEventListener('click', e => {
        if (e.target === this._modal) this.closeModal();
      });
    }

    // Escape key
    document.addEventListener('keydown', e => {
      if (e.key === 'Escape' && this._modal && !this._modal.hidden) this.closeModal();
    });

    // Register WebSocket Push Notification Listener for provider list changes
    try {
      this._wsListenerCleanup = window.tala.on('python:notification', (msg) => {
        if (msg && msg.method === 'inference.providers_changed') {
          this.loadProviders();
        }
      });
    } catch (err) {
      console.warn('[InferenceManager] Failed to register websocket notification listener:', err);
    }

    this.loadProviders();
    this.startDownloadPolling();
    this.loadEmbeddedEngines();
    this.startEnginePolling();
  },

  async loadEmbeddedEngines() {
    if (!this._engineList) return;
    try {
      const response = await invoke('python:rpc', {
        method: 'inference.get_engines',
        params: {}
      });
      this._engines = response.engines || [];
      this.renderEmbeddedEngines();
    } catch (err) {
      this._engineList.innerHTML = `<div style="color: var(--danger-color);">Error loading engines: ${err.message}</div>`;
    }
  },

  renderEmbeddedEngines() {
    if (!this._engineList) return;
    if (!this._engines || this._engines.length === 0) {
      this._engineList.innerHTML = `<div style="font-size: 13px; color: var(--text-2);">No embedded engines found.</div>`;
      return;
    }
    
    let html = '';
    for (const engine of this._engines) {
      const isReady = engine.status === 'ready' || engine.is_installed;
      const isInstalling = engine.status === 'installing';
      
      let actionBtn = '';
      if (isReady) {
        actionBtn = `<button class="btn btn-sm btn-outline" disabled>Installed</button>`;
      } else if (isInstalling) {
        const pct = (engine.progress || 0).toFixed(0);
        actionBtn = `<button class="btn btn-sm btn-primary" disabled>Installing (${pct}%)</button>`;
      } else {
        actionBtn = `<button class="btn btn-sm btn-primary" onclick="InferenceManager.installEngine('${engine.id}')">Install</button>`;
      }
      
      html += `
        <div class="provider-item" style="display: flex; align-items: center; justify-content: space-between; padding: var(--space-2); background: var(--bg-surface); border: 1px solid var(--border-color); border-radius: 4px;">
          <div>
            <div style="font-weight: 500;">${engine.name}</div>
            <div style="font-size: 12px; color: var(--text-2);">${engine.description}</div>
          </div>
          <div>${actionBtn}</div>
        </div>
      `;
    }
    this._engineList.innerHTML = html;
  },
  
  async installEngine(engineId) {
    try {
      await invoke('python:rpc', {
        method: 'inference.install_engine',
        params: { engine_id: engineId }
      });
      this.startEnginePolling();
    } catch (err) {
      console.warn('Failed to start engine installation', err);
    }
  },
  
  startEnginePolling() {
    if (this._enginePollInterval) clearInterval(this._enginePollInterval);
    this._enginePollInterval = setInterval(() => {
      this.loadEmbeddedEngines();
      const anyInstalling = this._engines.some(e => e.status === 'installing');
      if (!anyInstalling && this._engines.length > 0) {
        clearInterval(this._enginePollInterval);
      }
    }, 2000);
  },

  async loadProviders() {
    try {
      const response = await invoke('python:rpc', {
        method: 'inference.get_providers',
        params: {}
      });
      if (response && response.providers && response.providers.length > 0) {
        this._providers = response.providers;
        this.render();
      }
    } catch (err) {
      console.warn('[InferenceManager] Failed to load providers:', err);
    }
  },

  async persistProviders() {
    try {
      await invoke('python:rpc', {
        method: 'inference.save_providers',
        params: { providers: this._providers }
      });
      IPCLog.append('ok', 'Inference providers saved to PostgreSQL');
    } catch (err) {
      console.warn('[InferenceManager] Failed to persist providers:', err);
      IPCLog.append('error', `Failed to save providers: ${err.message ?? err}`);
    }
  },

  startDownloadPolling() {
    if (this._downloadInterval) clearInterval(this._downloadInterval);
    // The get_embedded_status handler is now non-blocking (returns immediately even
    // during module pre-warm).  Poll more aggressively:
    //   - Start after 3 s (enough for the WS connection to establish)
    //   - Poll every 5 s while downloading, 15 s when idle
    let _polling = false;

    const poll = async () => {
      if (_polling) return;           // skip if previous call is still in-flight
      _polling = true;
      try {
        const response = await invoke('python:rpc', {
          method: 'inference.get_embedded_status',
          params: {}
        });
        // response is the result object directly (preload unwraps __rpcError as throw)
        const { status, progress } = response;
        const emb = this._providers.find(p => p.id === 'embedded-builtin');
        if (emb) {
          let needsRender = false;
          const newStatus = status === 'ready'        ? 'online'
                          : status === 'error'        ? 'offline'
                          : status === 'downloading'  ? 'downloading'
                          : status === 'not_downloaded' ? 'not_downloaded'
                          : 'unknown';
          if (emb.status !== newStatus) {
            emb.status = newStatus;
            needsRender = true;
          }
          if (status === 'downloading') {
            if (emb.progress !== progress) {
              emb.progress = progress;
              needsRender = true;
            }
            // Poll faster during active download
            if (this._downloadInterval) {
              clearInterval(this._downloadInterval);
              this._downloadInterval = setInterval(poll, 5_000);
            }
          } else {
            if (emb.progress !== undefined) {
              delete emb.progress;
              needsRender = true;
            }
            // Slow down polling when idle
            if (this._downloadInterval) {
              clearInterval(this._downloadInterval);
              this._downloadInterval = setInterval(poll, 15_000);
            }
          }
          if (needsRender) {
            this.render();
          }
        }
        if (status === 'ready' || status === 'error') {
          clearInterval(this._downloadInterval);
          this._downloadInterval = null;
        }
      } catch (err) {
        // Only log as info — this is expected during warm-up
        console.info('[InferenceManager] Embedded status poll (not yet ready):', err.message ?? err);
      } finally {
        _polling = false;
      }
    };

    // Wait 3 s for the WS connection, then start polling every 15 s
    setTimeout(() => {
      poll();
      this._downloadInterval = setInterval(poll, 15_000);
    }, 3_000);
  },

  async detectLocalProviders() {
    if (this._detecting) return;
    this._detecting = true;
    if (this._btnDetect) {
      this._btnDetect.disabled = true;
      this._btnDetect.textContent = 'Detecting…';
    }
    IPCLog.append('info', 'Scanning for local inference engines (Ollama, LM Studio, vLLM)…');
    try {
      const response = await invoke('python:rpc', {
        method: 'inference.detect_local_providers',
        params: {}
      });
      if (response && response.providers) {
        this._providers = response.providers;
        this.render();
        const localCount = response.providers.filter(p => p.type === 'local' && p.status === 'online').length;
        IPCLog.append('ok', `Detection complete. ${localCount} local engine(s) online.`);
      } else {
        IPCLog.append('warn', 'Detection complete — no local engines found.');
      }
    } catch (err) {
      console.warn('[InferenceManager] detectLocalProviders failed:', err);
      IPCLog.append('error', `Local engine detection failed: ${err.message ?? err}`);
    } finally {
      this._detecting = false;
      if (this._btnDetect) {
        this._btnDetect.disabled = false;
        this._btnDetect.textContent = 'Detect Local Engines';
      }
    }
  },

  render() {
    if (!this._list) return;

    // Clear all but the empty-state div
    const empty = qs('#provider-list-empty');
    this._list.innerHTML = '';
    if (empty) this._list.appendChild(empty);

    if (this._providers.length === 0) {
      if (empty) empty.hidden = false;
      this.renderRoutePicker();
      return;
    }
    if (empty) empty.hidden = true;

    this._providers.forEach((p, i) => {
      this._list.appendChild(this._buildCard(p, i));
    });
    this.renderRoutePicker();
  },

  getSessionId() {
    const existing = localStorage.getItem('tala.chat.session_id');
    if (existing && existing.trim()) return existing.trim();
    const created = `chat-${Date.now()}`;
    localStorage.setItem('tala.chat.session_id', created);
    return created;
  },

  setSessionId(value) {
    const normalized = (value || '').trim() || `chat-${Date.now()}`;
    localStorage.setItem('tala.chat.session_id', normalized);
    if (this._sessionInput && this._sessionInput.value !== normalized) {
      this._sessionInput.value = normalized;
    }
    if (typeof AgentChat !== 'undefined') {
      AgentChat._sessionId = normalized;
      if (typeof AgentChat.loadHistory === 'function') {
        AgentChat.loadHistory();
      }
    }
    IPCLog.append('info', `Chat session route set to ${normalized}`);
    return normalized;
  },

  providerModelLabel(provider) {
    const config = provider.config || {};
    const model = provider.model || config.model || (provider.type === 'embedded' ? 'Qwen/Qwen2.5-0.5B-Instruct' : '');
    const endpoint = provider.endpoint || config.endpoint || '';
    const service = provider.service || config.service || provider.engine || config.engine || provider.type;
    return [model, service, endpoint].filter(Boolean).join(' · ') || 'No model configured';
  },

  activeProvider() {
    return this._providers.find(provider => provider.config && provider.config.is_forced) || null;
  },

  sessionProvider() {
    const sessionProviderId = localStorage.getItem('tala.chat.provider_id') || '';
    return this._providers.find(provider => provider.id === sessionProviderId) || null;
  },

  visionProvider() {
    const visionProviderId = localStorage.getItem('tala.chat.vision_provider_id') || '';
    return this._providers.find(provider => provider.id === visionProviderId) || null;
  },

  saveVisionProvider() {
    const selectedId = this._visionPicker?.value || '';
    localStorage.setItem('tala.chat.vision_provider_id', selectedId);
    if (typeof AgentChat !== 'undefined') {
      AgentChat._visionProviderId = selectedId;
    }
    IPCLog.append('info', `Vision route set to ${selectedId || 'Auto/Text'}...`);
  },

  renderRoutePicker() {
    if (!this._modelPicker) return;
    const forced = this.activeProvider();
    const sessionProvider = this.sessionProvider();
    const selectedValue = sessionProvider ? sessionProvider.id : (forced ? forced.id : (this._routeSelection || ''));

    this._modelPicker.innerHTML = '<option value="">Auto — provider priority chain</option>';
    this._providers.forEach(provider => {
      const option = document.createElement('option');
      option.value = provider.id;
      option.textContent = `${provider.name} — ${this.providerModelLabel(provider)}`;
      this._modelPicker.appendChild(option);
    });
    this._modelPicker.value = this._providers.some(provider => provider.id === selectedValue) ? selectedValue : '';
    this._routeSelection = this._modelPicker.value;

    if (this._visionPicker) {
      const currentVision = this.visionProvider();
      const currentVisionId = currentVision ? currentVision.id : '';
      this._visionPicker.innerHTML = '<option value="">Auto / Same as Text</option>';
      this._providers.forEach(provider => {
        const option = document.createElement('option');
        option.value = provider.id;
        option.textContent = `${provider.name} — ${this.providerModelLabel(provider)}`;
        this._visionPicker.appendChild(option);
      });
      this._visionPicker.value = this._providers.some(p => p.id === currentVisionId) ? currentVisionId : '';
    }

    this.previewRouteSelection();
  },

  previewRouteSelection() {
    const selectedId = this._modelPicker?.value || '';
    this._routeSelection = selectedId;
    const forced = this.activeProvider();
    const sessionProvider = this.sessionProvider();
    const selected = this._providers.find(provider => provider.id === selectedId);
    const routed = sessionProvider || forced;

    if (this._routeStatus) {
      this._routeStatus.textContent = sessionProvider ? `Session: ${sessionProvider.name}` : (forced ? `Forced: ${forced.name}` : 'Auto priority');
      this._routeStatus.classList.toggle('pinned', Boolean(routed));
    }
    if (this._routeActive) {
      if (sessionProvider) {
        this._routeActive.textContent = `Session provider: ${sessionProvider.name} using ${this.providerModelLabel(sessionProvider)}. Chat requests include this provider ID directly.`;
      } else if (forced) {
        this._routeActive.textContent = `Global forced provider: ${forced.name} using ${this.providerModelLabel(forced)}. Embedded remains available as the emergency fallback.`;
      } else if (selected) {
        this._routeActive.textContent = `Ready to pin ${selected.name} for session ${this.getSessionId()}. Click Use For Session to update inference routing.`;
      } else {
        this._routeActive.textContent = 'No provider pinned. Tala will use the priority chain with embedded fallback.';
      }
    }
  },

  async pinSelectedProvider(providerId = null) {
    const selectedId = providerId || this._modelPicker?.value || '';
    if (!selectedId) {
      await this.clearForcedProvider();
      return;
    }
    const selected = this._providers.find(provider => provider.id === selectedId);
    if (!selected) return;

    this.setSessionId(this._sessionInput?.value || this.getSessionId());
    localStorage.setItem('tala.chat.provider_id', selectedId);
    if (typeof AgentChat !== 'undefined') {
      AgentChat._providerId = selectedId;
    }
    this.render();
    IPCLog.append('ok', `Session ${this.getSessionId()} routes to ${selected.name}`);
  },

  async clearForcedProvider() {
    this.setSessionId(this._sessionInput?.value || this.getSessionId());
    localStorage.removeItem('tala.chat.provider_id');
    if (typeof AgentChat !== 'undefined') {
      AgentChat._providerId = '';
    }
    this._providers.forEach(provider => {
      if (!provider.config) provider.config = {};
      provider.config.is_forced = false;
    });
    this._routeSelection = '';
    await this.persistProviders();
    this.render();
    IPCLog.append('info', `Session ${this.getSessionId()} returned to auto provider routing`);
  },

  _buildCard(p, i) {
    const total     = this._providers.length;
    const isFirst   = i === 0;
    const isLast    = i === total - 1;
    const typeLabel = { embedded: 'EMBEDDED', local: 'LOCAL', remote: 'REMOTE' }[p.type] || p.type.toUpperCase();
    const statusCls = p.status === 'online' ? 'status-ok' : p.status === 'offline' ? 'status-error' : p.status === 'downloading' ? 'status-connecting' : 'status-unknown';
    const statusTxt = (p.status || 'UNKNOWN').toUpperCase();

    let meta = '';
    if (p.type === 'embedded') {
      if (p.status === 'downloading') {
        const pct = p.progress !== undefined ? `${p.progress}%` : '0%';
        meta = `Qwen/Qwen2.5-0.5B-Instruct · CPU inference · Downloading: ${pct}`;
      } else {
        meta = `Qwen/Qwen2.5-0.5B-Instruct · CPU inference · ~1B parameters · Always available`;
      }
    }
    else if (p.type === 'local')  meta = [p.model, p.endpoint, p.engine].filter(Boolean).join(' · ') || 'Local endpoint';
    else if (p.type === 'remote') meta = [p.model, p.service, p.endpoint].filter(Boolean).join(' · ') || 'Remote API';

    const isForced = p.config && p.config.is_forced;
    const card = document.createElement('div');
    card.className = `provider-card provider-type-${p.type}`;
    card.dataset.providerId = p.id;
    card.innerHTML = `
      <div class="provider-priority-badge">${i + 1}</div>
      <div class="provider-type-chip type-${p.type}">${typeLabel}</div>
      <div class="provider-card-info">
        <div class="provider-card-name">${p.name}</div>
        <div class="provider-card-meta">${meta}</div>
      </div>
      <label class="provider-force-checkbox-container ${isForced ? 'forced' : ''}" title="Force use this provider (bypasses priority order and fallback chain, except for the embedded engine last resort)">
        <input type="checkbox" class="provider-force-checkbox" ${isForced ? 'checked' : ''} />
        <span class="provider-force-text">FORCE</span>
      </label>
      <div class="provider-status-indicator">
        <span class="status-dot ${statusCls}"></span>
        <span class="provider-status-text">${statusTxt}</span>
      </div>
      <div class="provider-card-actions">
        <button class="btn-provider-action btn-provider-route" title="Use provider for current session">◎</button>
        <button class="btn-provider-action btn-provider-edit" title="Edit provider">✎</button>
        <button class="btn-provider-action btn-provider-up"   title="Move Up"   ${isFirst ? 'disabled' : ''}>↑</button>
        <button class="btn-provider-action btn-provider-down" title="Move Down" ${isLast  ? 'disabled' : ''}>↓</button>
        ${p.locked
          ? `<span class="provider-lock-icon" title="Built-in — cannot be removed">🔒</span>`
          : `<button class="btn-provider-action btn-provider-delete" title="Remove provider">✕</button>`
        }
      </div>`;

    const route = card.querySelector('.btn-provider-route');
    if (route) route.addEventListener('click', () => this.pinSelectedProvider(p.id));
    const edit = card.querySelector('.btn-provider-edit');
    if (edit) edit.addEventListener('click', () => this.editProvider(p.id));
    if (!isFirst) card.querySelector('.btn-provider-up').addEventListener('click', () => this.move(p.id, -1));
    if (!isLast)  card.querySelector('.btn-provider-down').addEventListener('click', () => this.move(p.id, 1));
    const del = card.querySelector('.btn-provider-delete');
    if (del) del.addEventListener('click', () => this.remove(p.id));

    const forceCheckbox = card.querySelector('.provider-force-checkbox');
    if (forceCheckbox) {
      forceCheckbox.addEventListener('change', (e) => {
        const checked = e.target.checked;
        this._providers.forEach(prov => {
          if (!prov.config) prov.config = {};
          if (prov.id === p.id) {
            prov.config.is_forced = checked;
          } else {
            prov.config.is_forced = false;
          }
        });
        this.persistProviders();
        this.render();
      });
    }

    return card;
  },

  move(id, dir) {
    const idx = this._providers.findIndex(p => p.id === id);
    if (idx < 0) return;
    const to = idx + dir;
    if (to < 0 || to >= this._providers.length) return;
    [this._providers[idx], this._providers[to]] = [this._providers[to], this._providers[idx]];
    this.persistProviders();
    this.render();
    IPCLog.append('info', `Provider "${this._providers[to].name}" → priority ${to + 1}`);
  },

  remove(id) {
    const idx = this._providers.findIndex(p => p.id === id);
    if (idx < 0) return;
    const name = this._providers[idx].name;
    this._providers.splice(idx, 1);
    this.persistProviders();
    this.render();
    IPCLog.append('info', `Provider removed: "${name}"`);
  },

  // ── Modal ──────────────────────────────────────────────────────────
  openModal() {
    this._selectedType = null;
    this._editingId = null;
    if (this._stepType)  { this._stepType.hidden = false; }
    if (this._stepDet)   { this._stepDet.hidden = true; this._stepDet.innerHTML = ''; }
    if (this._btnBack)   this._btnBack.hidden = true;
    if (this._btnSave)   { this._btnSave.hidden = true; this._btnSave.disabled = false; }
    document.querySelectorAll('.provider-type-card').forEach(c => c.classList.remove('selected'));
    if (this._modal) this._modal.hidden = false;
  },

  closeModal() {
    if (this._modal) this._modal.hidden = true;
    this._selectedType = null;
  },

  goBack() {
    if (this._stepType)  this._stepType.hidden = false;
    if (this._stepDet)   { this._stepDet.hidden = true; this._stepDet.innerHTML = ''; }
    if (this._btnBack)   this._btnBack.hidden = true;
    if (this._btnSave)   this._btnSave.hidden = true;
    document.querySelectorAll('.provider-type-card').forEach(c => c.classList.remove('selected'));
    this._selectedType = null;
  },

  showDetails(type) {
    this._selectedType = type;
    document.querySelectorAll('.provider-type-card').forEach(c => {
      c.classList.toggle('selected', c.dataset.type === type);
    });
    if (this._stepType)  this._stepType.hidden = true;
    if (this._btnBack)   this._btnBack.hidden  = false;
    if (this._btnSave)   { this._btnSave.hidden = false; this._btnSave.disabled = false; }

    const alreadyEmbedded = this._providers.some(p => p.type === 'embedded');

    let html = '';
    if (type === 'embedded') {
      if (alreadyEmbedded) {
        html = `<div class="modal-info-banner modal-info-warn">
          <span>⚠</span>
          <span>An Embedded provider is already in the chain. Only one can be active at a time.</span>
        </div>`;
        if (this._btnSave) this._btnSave.disabled = true;
      } else {
        html = `<div class="modal-form">
          <div class="modal-info-banner modal-info-ok">
            <span>⚙</span>
            <span>Built-in 1B parameter model running on CPU. Always available — no internet or API key needed.</span>
          </div>
          <div class="form-field-group">
            <label class="form-field-label" for="prov-name">Display Name</label>
            <input class="form-field-input" id="prov-name" type="text" value="Tala Embedded Engine" />
          </div>
          <div class="form-field-group">
            <label class="form-field-label" for="prov-model">Model</label>
            <input class="form-field-input" id="prov-model" type="text" value="qwen2.5-0.5b" readonly />
          </div>
        </div>`;
      }
    } else if (type === 'local') {
      html = `<div class="modal-form">
        <div class="form-field-group">
          <label class="form-field-label" for="prov-name">Display Name</label>
          <input class="form-field-input" id="prov-name" type="text" placeholder="e.g. Ollama Local" />
        </div>
        <div class="form-two-col">
          <div class="form-field-group">
            <label class="form-field-label" for="prov-engine">Engine</label>
            <select class="form-field-select" id="prov-engine">
              <option value="ollama">Ollama</option>
              <option value="lmstudio">LM Studio</option>
              <option value="custom">Custom (OpenAI-compatible)</option>
            </select>
          </div>
          <div class="form-field-group">
            <label class="form-field-label" for="prov-model">Model Name</label>
            <input class="form-field-input" id="prov-model" type="text" placeholder="e.g. llama3.2:3b" />
          </div>
        </div>
        <div class="form-field-group">
          <label class="form-field-label" for="prov-endpoint">Endpoint URL</label>
          <input class="form-field-input" id="prov-endpoint" type="text" placeholder="http://localhost:11434" />
        </div>
        <div class="form-field-group">
          <label class="form-field-label" for="prov-apikey">API Key <span class="label-optional">(optional)</span></label>
          <input class="form-field-input" id="prov-apikey" type="password" placeholder="Leave blank if not required" />
        </div>
      </div>`;
    } else if (type === 'remote') {
      html = `<div class="modal-form">
        <div class="form-field-group">
          <label class="form-field-label" for="prov-name">Display Name</label>
          <input class="form-field-input" id="prov-name" type="text" placeholder="e.g. Gemini Pro" />
        </div>
        <div class="form-two-col">
          <div class="form-field-group">
            <label class="form-field-label" for="prov-service">Service</label>
            <select class="form-field-select" id="prov-service">
              <option value="gemini">Google Gemini</option>
              <option value="openai">OpenAI</option>
              <option value="anthropic">Anthropic (Claude)</option>
              <option value="custom">Custom (OpenAI-compatible)</option>
            </select>
          </div>
          <div class="form-field-group">
            <label class="form-field-label" for="prov-model">Model Name</label>
            <input class="form-field-input" id="prov-model" type="text" placeholder="e.g. gemini-1.5-pro" />
          </div>
        </div>
        <div class="form-field-group" id="prov-endpoint-group">
          <label class="form-field-label" for="prov-endpoint">Endpoint URL <span class="label-optional">(optional)</span></label>
          <input class="form-field-input" id="prov-endpoint" type="text" placeholder="e.g. https://api.openai.com/v1" />
        </div>
        <div class="form-field-group">
          <label class="form-field-label" for="prov-apikey">API Key</label>
          <input class="form-field-input" id="prov-apikey" type="password" placeholder="sk-..." autocomplete="new-password" />
        </div>
      </div>`;
    }

    if (this._stepDet) {
      this._stepDet.innerHTML = html;
      this._stepDet.hidden = false;

      // Wire service -> show/hide custom endpoint
      if (type === 'remote') {
        const svc = qs('#prov-service');
        const epg = qs('#prov-endpoint-group');
        // We now allow endpoint override for ALL services.
      }
    }
  },

  saveProvider() {
    const type = this._selectedType;
    if (!type) return;

    const nameEl = qs('#prov-name');
    const name   = nameEl?.value?.trim();
    if (!name) {
      if (nameEl) { nameEl.classList.add('input-error'); nameEl.focus(); }
      return;
    }
    if (nameEl) nameEl.classList.remove('input-error');

    let provider;
    let isEditing = false;
    
    if (this._editingId) {
      provider = this._providers.find(p => p.id === this._editingId);
      isEditing = !!provider;
    }
    
    if (!provider) {
      provider = {
        id:     `prov-${Date.now()}`,
        type,
        status: 'unknown',
      };
    }
    
    provider.name = name;

    if (type === 'embedded') {
      provider.model  = qs('#prov-model')?.value || 'qwen2.5-0.5b';
      provider.locked = true;
      provider.status = 'online';
    } else if (type === 'local') {
      provider.engine   = qs('#prov-engine')?.value  || 'ollama';
      provider.endpoint = qs('#prov-endpoint')?.value?.trim() || '';
      provider.model    = qs('#prov-model')?.value?.trim()    || '';
      provider.apiKey   = qs('#prov-apikey')?.value?.trim()   || '';
    } else if (type === 'remote') {
      provider.service  = qs('#prov-service')?.value  || 'gemini';
      provider.endpoint = qs('#prov-endpoint')?.value?.trim() || '';
      provider.model    = qs('#prov-model')?.value?.trim()    || '';
      provider.apiKey   = qs('#prov-apikey')?.value?.trim()   || '';
    }

    if (!isEditing) {
      this._providers.push(provider);
    }
    
    this.persistProviders();
    this.render();
    this.closeModal();
    IPCLog.append('info', `Provider ${isEditing ? 'updated' : 'added'}: "${name}" (${type})`);
  },

  editProvider(id) {
    const p = this._providers.find(prov => prov.id === id);
    if (!p) return;
    this._editingId = id;
    this.showDetails(p.type); // This sets up the innerHTML of the modal for the type
    
    const nameEl = qs('#prov-name');
    if (nameEl) nameEl.value = p.name || '';
    
    if (p.type === 'local') {
      const eng = qs('#prov-engine'); if (eng && p.engine) eng.value = p.engine;
      const ep = qs('#prov-endpoint'); if (ep && p.endpoint) ep.value = p.endpoint;
      const mod = qs('#prov-model'); if (mod && p.model) mod.value = p.model;
      const key = qs('#prov-apikey'); if (key && p.apiKey) key.value = p.apiKey;
    } else if (p.type === 'remote') {
      const svc = qs('#prov-service'); if (svc && p.service) svc.value = p.service;
      const ep = qs('#prov-endpoint'); if (ep && p.endpoint) ep.value = p.endpoint;
      const mod = qs('#prov-model'); if (mod && p.model) mod.value = p.model;
      const key = qs('#prov-apikey'); if (key && p.apiKey) key.value = p.apiKey;
    }
    
    if (this._modal) this._modal.hidden = false;
  },
};


/* ═══════════════════════════════════════════════════════════════════
   SystemManager — live hardware / OS / software info panel
   ═══════════════════════════════════════════════════════════════════ */
const SystemManager = {
  _initialized: false,

  init() {
    this._initialized = true;
    document.getElementById('btn-system-refresh')
      ?.addEventListener('click', () => this.refresh());
    this.refresh();
  },

  async refresh() {
    const loading = document.getElementById('system-loading');
    const content = document.getElementById('system-content');
    if (loading) loading.hidden = false;
    if (content) content.hidden = true;

    try {
      const resp = await invoke('python:rpc', {
        method: 'system.info',
        params: {},
      });
      const info = resp?.result?.info || resp?.info || null;
      if (!info) throw new Error('No info returned from harness');
      this._render(info);
      if (loading) loading.hidden = true;
      if (content) content.hidden = false;
    } catch (err) {
      console.warn('[SystemManager] Failed to fetch system info:', err);
      // Show a degraded state — populate what we can from window.navigator
      this._renderFallback();
      if (loading) loading.hidden = true;
      if (content) content.hidden = false;
    }
  },

  _render(info) {
    const os   = info.os   || {};
    const cpu  = info.cpu  || {};
    const ram  = info.ram  || {};
    const disk = info.disk || {};
    const sw   = info.software || {};
    const pkgs = info.python_packages || {};
    const svcs = info.services || {};

    // OS fields
    this._setText('sys-os-name',      `${os.system || '—'} ${os.release || ''}`.trim());
    this._setText('sys-os-release',   os.release   || '—');
    this._setText('sys-os-version',   os.version   || '—');
    this._setText('sys-os-arch',      os.machine   || '—');
    this._setText('sys-os-processor', os.processor || '—');
    this._setText('sys-os-hostname',  os.node      || '—');

    // CPU meter
    const cpuPct = cpu.usage_percent || 0;
    this._setText('sys-cpu-pct',   `${cpuPct}%`);
    this._setMeter('sys-cpu-bar',  'sys-meter-cpu', cpuPct);
    this._setText('sys-cpu-cores', `${cpu.physical_cores || '—'} phys / ${cpu.logical_cores || '—'} logical cores`);
    this._setText('sys-cpu-freq',  cpu.freq_mhz ? `${cpu.freq_mhz} MHz` : '—');

    // RAM meter
    const ramPct = ram.percent || 0;
    this._setText('sys-ram-pct',   `${ramPct}%`);
    this._setMeter('sys-ram-bar',  'sys-meter-ram', ramPct);
    this._setText('sys-ram-used',  `${ram.used_gb || 0} GB used`);
    this._setText('sys-ram-total', `${ram.total_gb || 0} GB total`);

    // Disk meter
    const diskPct = disk.percent || 0;
    this._setText('sys-disk-pct',   `${diskPct}%`);
    this._setMeter('sys-disk-bar',  'sys-meter-disk', diskPct);
    this._setText('sys-disk-used',  `${disk.used_gb || 0} GB used`);
    this._setText('sys-disk-total', `${disk.total_gb || 0} GB total`);

    // Service status
    this._setService('harness',    svcs.harness_ws  || 'online');   // responding = online
    this._setService('postgresql', svcs.postgresql  || 'unknown');
    this._setService('blockchain', svcs.blockchain  || 'unknown');

    // Software version tiles
    const swItems = [
      { label: 'Python',           value: sw.python              },
      { label: 'Node.js',          value: sw.node                },
      { label: 'npm',              value: sw.npm                 },
      { label: 'Electron',         value: sw.electron            },
      { label: 'PostgreSQL Client',value: sw.postgresql_client   },
      { label: 'PostgreSQL Server',value: sw.postgresql_server   },
    ];
    this._renderVersionTiles('sys-versions-grid', swItems);

    // Python package tiles
    const pkgItems = Object.entries(pkgs).map(([k, v]) => ({ label: k, value: v }));
    this._renderVersionTiles('sys-packages-grid', pkgItems);
  },

  _renderFallback() {
    // Fill OS from browser navigator when harness is offline
    this._setText('sys-os-name',     navigator.platform || '—');
    this._setText('sys-os-release',  '—');
    this._setText('sys-os-version',  '—');
    this._setText('sys-os-arch',     navigator.platform || '—');
    this._setText('sys-os-processor','—');
    this._setText('sys-os-hostname', window.location.hostname || 'localhost');
    this._setService('harness',    'offline');
    this._setService('postgresql', 'unknown');
    this._setService('blockchain', 'unknown');
    this._renderVersionTiles('sys-versions-grid', [
      { label: 'Harness', value: 'offline — no data' },
    ]);
    this._renderVersionTiles('sys-packages-grid', []);
  },

  // ── DOM helpers ─────────────────────────────────────────────────────────
  _setText(id, text) {
    const el = document.getElementById(id);
    if (el) el.textContent = text;
  },

  _setMeter(barId, baseClass, pct) {
    const bar = document.getElementById(barId);
    if (!bar) return;
    const clamped = Math.min(100, Math.max(0, pct));
    bar.style.width = `${clamped}%`;
    // Colour shift on high usage
    bar.className = `sys-meter-bar ${baseClass}`;
    if (clamped >= 90) bar.classList.add('meter-danger');
    else if (clamped >= 75) bar.classList.add('meter-warn');
  },

  _setService(key, status) {
    const dot   = document.getElementById(`svc-dot-${key}`);
    const label = document.getElementById(`svc-label-${key}`);
    const norm  = (status || 'unknown').toLowerCase();
    if (dot) {
      dot.className = `sys-service-dot dot-${norm}`;
    }
    if (label) {
      label.className = `sys-service-status status-${norm}`;
      label.textContent = norm.charAt(0).toUpperCase() + norm.slice(1);
    }
  },

  _renderVersionTiles(gridId, items) {
    const grid = document.getElementById(gridId);
    if (!grid) return;
    const missing = ['not found', 'not installed', 'not connected', ''];
    grid.innerHTML = items.map(({ label, value }) => {
      const isMissing = !value || missing.some(m => (value || '').toLowerCase().includes(m));
      const cls = isMissing ? 'ver-missing' : 'ver-ok';
      return `
        <div class="sys-version-tile">
          <span class="sys-version-label">${label}</span>
          <span class="sys-version-value ${cls}">${value || 'not found'}</span>
        </div>`;
    }).join('');
  },
};


/* ═══════════════════════════════════════════════════════════════════
   GitProviderManager — Git hosting provider CRUD, priority list, modal
   ═══════════════════════════════════════════════════════════════════ */
const GitProviderManager = {
  _initialized: false,
  _providers: [],

  init() {
    this._initialized = true;
    this._list      = qs('#git-provider-list');
    this._btnAdd    = qs('#btn-add-git-provider');
    this._modal     = qs('#modal-add-git-provider');
    this._btnSave   = qs('#btn-modal-git-provider-save');
    this._btnCancel = qs('#btn-modal-git-provider-cancel');
    this._btnClose  = qs('#btn-modal-git-provider-close');

    if (this._btnAdd)    this._btnAdd.addEventListener('click',   () => this.openModal());
    if (this._btnClose)  this._btnClose.addEventListener('click',  () => this.closeModal());
    if (this._btnCancel) this._btnCancel.addEventListener('click', () => this.closeModal());
    if (this._btnSave)   this._btnSave.addEventListener('click',   () => this.saveProvider());

    if (this._modal) {
      this._modal.addEventListener('click', e => {
        if (e.target === this._modal) this.closeModal();
      });
    }

    this.loadProviders();
  },

  async loadProviders() {
    try {
      const response = await invoke('python:rpc', {
        method: 'git.get_providers',
        params: {}
      });
      if (response && response.providers) {
        this._providers = response.providers;
        this.render();
      }
    } catch (err) {
      console.warn('[GitProviderManager] Failed to load git providers:', err);
    }
  },

  async persistProviders() {
    try {
      await invoke('python:rpc', {
        method: 'git.save_providers',
        params: { providers: this._providers }
      });
      IPCLog.append('ok', 'Git providers saved to PostgreSQL');
    } catch (err) {
      console.warn('[GitProviderManager] Failed to persist git providers:', err);
      IPCLog.append('error', `Failed to save git providers: ${err.message ?? err}`);
    }
  },

  render() {
    if (!this._list) return;
    const empty = qs('#git-provider-list-empty');
    this._list.innerHTML = '';
    if (empty) this._list.appendChild(empty);

    if (this._providers.length === 0) {
      if (empty) empty.hidden = false;
      return;
    }
    if (empty) empty.hidden = true;

    this._providers.forEach((p, i) => {
      this._list.appendChild(this._buildCard(p, i));
    });
  },

  /** Detect host type label from endpoint URL */
  _hostLabel(endpoint) {
    const url = (endpoint || '').toLowerCase();
    if (url.includes('github.com'))    return 'GITHUB';
    if (url.includes('gitlab.com') || url.includes('gitlab.')) return 'GITLAB';
    if (url.includes('bitbucket.org')) return 'BITBUCKET';
    if (url.includes('gitea.'))        return 'GITEA';
    if (url.includes('codeberg.org'))  return 'CODEBERG';
    if (url.includes('dev.azure.com')) return 'AZURE DEVOPS';
    return 'GIT';
  },

  /** Brand accent colour per host */
  _hostColor(label) {
    switch (label) {
      case 'GITHUB':     return '#e6edf3';
      case 'GITLAB':     return '#fc6d26';
      case 'BITBUCKET':  return '#2684ff';
      case 'GITEA':      return '#609926';
      case 'CODEBERG':   return '#2185d0';
      case 'AZURE DEVOPS': return '#0078d4';
      default:           return 'var(--accent-purple)';
    }
  },

  _buildCard(p, i) {
    const total   = this._providers.length;
    const isFirst = i === 0;
    const isLast  = i === total - 1;
    const label   = this._hostLabel(p.endpoint);
    const color   = this._hostColor(label);
    const userMeta = p.username ? `<span style="opacity:.7;">${p.username} @ </span>${p.endpoint}` : p.endpoint;

    const card = document.createElement('div');
    card.className = 'provider-card provider-type-remote';
    card.style.borderLeftColor = color;
    card.dataset.providerId = p.id;
    const isForced = !!p.locked;
    card.innerHTML = `
      <div class="provider-priority-badge">${i + 1}</div>
      <div class="provider-type-chip type-remote" style="background: ${color}22; color: ${color}; border-color: ${color}44;">${label}</div>
      <div class="provider-card-info">
        <div class="provider-card-name">${p.name}</div>
        <div class="provider-card-meta">${userMeta}</div>
      </div>
      <label class="provider-force-checkbox-container ${isForced ? 'forced' : ''}" title="Force use this git provider (bypasses priority order)">
        <input type="checkbox" class="provider-force-checkbox" ${isForced ? 'checked' : ''} />
        <span class="provider-force-text">FORCE</span>
      </label>
      <div class="provider-status-indicator">
        <span class="status-dot status-ok"></span>
        <span class="provider-status-text">ACTIVE</span>
      </div>
      <div class="provider-card-actions">
        <button class="btn-provider-action btn-git-provider-up"     title="Move Up"    ${isFirst ? 'disabled' : ''}>&#8593;</button>
        <button class="btn-provider-action btn-git-provider-down"   title="Move Down"  ${isLast  ? 'disabled' : ''}>&#8595;</button>
        <button class="btn-provider-action btn-git-provider-delete" title="Remove provider">&#x2715;</button>
      </div>`;

    if (!isFirst) card.querySelector('.btn-git-provider-up').addEventListener('click',   () => this.move(p.id, -1));
    if (!isLast)  card.querySelector('.btn-git-provider-down').addEventListener('click', () => this.move(p.id,  1));
    card.querySelector('.btn-git-provider-delete').addEventListener('click', () => this.remove(p.id));

    const forceCheckbox = card.querySelector('.provider-force-checkbox');
    if (forceCheckbox) {
      forceCheckbox.addEventListener('change', (e) => {
        const checked = e.target.checked;
        this._providers.forEach(prov => {
          if (prov.id === p.id) {
            prov.locked = checked;
          } else {
            prov.locked = false;
          }
        });
        this.persistProviders();
        this.render();
      });
    }

    return card;
  },

  move(id, dir) {
    const idx = this._providers.findIndex(p => p.id === id);
    if (idx < 0) return;
    const to = idx + dir;
    if (to < 0 || to >= this._providers.length) return;
    [this._providers[idx], this._providers[to]] = [this._providers[to], this._providers[idx]];
    this.persistProviders();
    this.render();
    IPCLog.append('info', `Git provider "${this._providers[to].name}" → priority ${to + 1}`);
  },

  remove(id) {
    const idx = this._providers.findIndex(p => p.id === id);
    if (idx < 0) return;
    const name = this._providers[idx].name;
    this._providers.splice(idx, 1);
    this.persistProviders();
    this.render();
    IPCLog.append('info', `Git provider removed: "${name}"`);
  },

  openModal() {
    ['git-prov-name','git-prov-endpoint','git-prov-username','git-prov-apikey'].forEach(id => {
      const el = qs(`#${id}`);
      if (el) { el.value = ''; el.classList.remove('input-error'); }
    });
    if (this._modal) this._modal.hidden = false;
    qs('#git-prov-name')?.focus();
  },

  closeModal() {
    if (this._modal) this._modal.hidden = true;
  },

  saveProvider() {
    const nameEl     = qs('#git-prov-name');
    const endpointEl = qs('#git-prov-endpoint');
    const usernameEl = qs('#git-prov-username');
    const apikeyEl   = qs('#git-prov-apikey');

    const name     = nameEl?.value?.trim();
    const endpoint = endpointEl?.value?.trim();
    const username = usernameEl?.value?.trim() || '';
    const apiKey   = apikeyEl?.value?.trim()   || '';

    let hasError = false;
    if (!name) {
      nameEl?.classList.add('input-error'); hasError = true;
    } else { nameEl?.classList.remove('input-error'); }

    if (!endpoint) {
      endpointEl?.classList.add('input-error'); hasError = true;
    } else { endpointEl?.classList.remove('input-error'); }

    if (hasError) return;

    this._providers.push({
      id:       `gitprov-${Date.now()}`,
      name,
      endpoint,
      username,
      api_key:  apiKey,
      locked:   false,
    });
    this.persistProviders();
    this.render();
    this.closeModal();
    IPCLog.append('info', `Git provider added: "${name}" (${this._hostLabel(endpoint)}) → priority ${this._providers.length}`);
  },
};


/* ═══════════════════════════════════════════════════════════════════
   StorageProviderManager — Storage provider CRUD, priority list, modal
   ═══════════════════════════════════════════════════════════════════ */
const StorageProviderManager = {
  _initialized: false,
  _providers: [],

  init() {
    this._initialized = true;
    this._list      = qs('#storage-provider-list');
    this._btnAdd    = qs('#btn-add-storage-provider');
    this._modal     = qs('#modal-add-storage-provider');
    this._btnSave   = qs('#btn-modal-storage-provider-save');
    this._btnCancel = qs('#btn-modal-storage-provider-cancel');
    this._btnClose  = qs('#btn-modal-storage-provider-close');
    this._typeSelect = qs('#stor-prov-type');

    if (this._btnAdd)    this._btnAdd.addEventListener('click',   () => this.openModal());
    if (this._btnClose)  this._btnClose.addEventListener('click',  () => this.closeModal());
    if (this._btnCancel) this._btnCancel.addEventListener('click', () => this.closeModal());
    if (this._btnSave)   this._btnSave.addEventListener('click',   () => this.saveProvider());

    if (this._modal) {
      this._modal.addEventListener('click', e => {
        if (e.target === this._modal) this.closeModal();
      });
    }

    if (this._typeSelect) {
      this._typeSelect.addEventListener('change', (e) => {
        const type = e.target.value;
        const allFields = document.querySelectorAll('#modal-add-storage-provider .stor-fields');
        allFields.forEach(el => el.hidden = true);
        const targetFields = qs(`#stor-fields-${type}`);
        if (targetFields) targetFields.hidden = false;
      });
    }

    this.loadProviders();
  },

  async loadProviders() {
    try {
      const response = await invoke('python:rpc', {
        method: 'storage.get_providers',
        params: {}
      });
      if (response && response.providers) {
        this._providers = response.providers;
        this.render();
      }
    } catch (err) {
      console.warn('[StorageProviderManager] Failed to load storage providers:', err);
    }
  },

  async persistProviders() {
    try {
      await invoke('python:rpc', {
        method: 'storage.save_providers',
        params: { providers: this._providers }
      });
      IPCLog.append('ok', 'Storage providers saved to PostgreSQL');
    } catch (err) {
      console.warn('[StorageProviderManager] Failed to persist storage providers:', err);
      IPCLog.append('error', `Failed to save storage providers: ${err.message ?? err}`);
    }
  },

  render() {
    if (!this._list) return;
    const empty = qs('#storage-provider-list-empty');
    this._list.innerHTML = '';
    if (empty) this._list.appendChild(empty);

    if (this._providers.length === 0) {
      if (empty) empty.hidden = false;
      return;
    }
    if (empty) empty.hidden = true;

    this._providers.forEach((p, i) => {
      this._list.appendChild(this._buildCard(p, i));
    });
  },

  _typeColor(type) {
    switch (type) {
      case 's3':       return '#ff9900'; // AWS Orange
      case 'gdrive':   return '#34a853'; // Google Green
      case 'nfs':      return '#8e44ad'; // Purple
      case 'azure':    return '#0078d4'; // Microsoft Blue
      case 'sftp':     return '#e67e22'; // Orange
      case 'dropbox':  return '#0061ff'; // Dropbox Blue
      case 'onedrive': return '#0078d4'; // OneDrive Blue
      default:         return '#1abc9c'; // Teal
    }
  },

  _buildCard(p, i) {
    const total   = this._providers.length;
    const isFirst = i === 0;
    const isLast  = i === total - 1;
    const label   = (p.provider_type || 'custom').toUpperCase();
    const color   = this._typeColor(p.provider_type);
    
    let details = '';
    const creds = p.credentials || {};
    if (p.provider_type === 's3') {
      details = `Bucket: ${creds.bucket || ''} | Region: ${creds.region || 'us-east-1'}`;
      if (creds.endpoint) details += ` | Endpoint: ${creds.endpoint}`;
    } else if (p.provider_type === 'gdrive') {
      details = `Folder ID: ${creds.folder_id || 'Root'}`;
    } else if (p.provider_type === 'nfs') {
      details = `Server: ${creds.server || ''} | Export: ${creds.export_path || ''}`;
    } else if (p.provider_type === 'azure') {
      details = `Container: ${creds.container || ''} | Account: ${creds.account || ''}`;
    } else if (p.provider_type === 'sftp') {
      details = `SFTP: ${creds.username || ''}@${creds.host || ''}:${creds.port || '22'}${creds.path || ''}`;
    } else if (p.provider_type === 'dropbox') {
      details = `Path: ${creds.path || 'Root'}`;
    } else if (p.provider_type === 'onedrive') {
      details = `Path: ${creds.path || 'Root'}`;
    } else {
      details = `Endpoint: ${creds.endpoint || ''}`;
    }

    const card = document.createElement('div');
    card.className = 'provider-card provider-type-remote';
    card.style.borderLeftColor = color;
    card.dataset.providerId = p.id;
    card.innerHTML = `
      <div class="provider-priority-badge">${i + 1}</div>
      <div class="provider-type-chip type-storage" style="background: ${color}22; color: ${color}; border-color: ${color}44;">${label}</div>
      <div class="provider-card-info">
        <div class="provider-card-name">${p.name}</div>
        <div class="provider-card-meta">${details}</div>
      </div>
      <div class="provider-status-indicator">
        <span class="status-dot status-ok"></span>
        <span class="provider-status-text">MOUNTED</span>
      </div>
      <div class="provider-card-actions">
        <button class="btn-provider-action btn-storage-provider-up"     title="Move Up"    ${isFirst ? 'disabled' : ''}>&#8593;</button>
        <button class="btn-provider-action btn-storage-provider-down"   title="Move Down"  ${isLast  ? 'disabled' : ''}>&#8595;</button>
        <button class="btn-provider-action btn-storage-provider-delete" title="Remove provider">&#x2715;</button>
      </div>`;

    if (!isFirst) card.querySelector('.btn-storage-provider-up').addEventListener('click',   () => this.move(p.id, -1));
    if (!isLast)  card.querySelector('.btn-storage-provider-down').addEventListener('click', () => this.move(p.id,  1));
    card.querySelector('.btn-storage-provider-delete').addEventListener('click', () => this.remove(p.id));

    return card;
  },

  move(id, dir) {
    const idx = this._providers.findIndex(p => p.id === id);
    if (idx < 0) return;
    const to = idx + dir;
    if (to < 0 || to >= this._providers.length) return;
    [this._providers[idx], this._providers[to]] = [this._providers[to], this._providers[idx]];
    this.persistProviders();
    this.render();
    IPCLog.append('info', `Storage provider "${this._providers[to].name}" → priority ${to + 1}`);
  },

  remove(id) {
    const idx = this._providers.findIndex(p => p.id === id);
    if (idx < 0) return;
    const name = this._providers[idx].name;
    this._providers.splice(idx, 1);
    this.persistProviders();
    this.render();
    IPCLog.append('info', `Storage provider removed: "${name}"`);
  },

  openModal() {
    const inputs = document.querySelectorAll('#modal-add-storage-provider input, #modal-add-storage-provider select, #modal-add-storage-provider textarea');
    inputs.forEach(el => {
      if (el.tagName === 'SELECT') {
        el.value = 's3';
      } else if (el.type === 'number') {
        el.value = el.getAttribute('value') || '22';
      } else {
        el.value = '';
      }
      el.classList.remove('input-error');
    });

    document.querySelectorAll('#modal-add-storage-provider .stor-fields').forEach(el => {
      el.hidden = (el.id !== 'stor-fields-s3');
    });

    if (this._modal) this._modal.hidden = false;
    qs('#stor-prov-name')?.focus();
  },

  closeModal() {
    if (this._modal) this._modal.hidden = true;
  },

  saveProvider() {
    const nameEl = qs('#stor-prov-name');
    const typeEl = qs('#stor-prov-type');
    const name = nameEl?.value?.trim();
    const type = typeEl?.value;

    let hasError = false;
    if (!name) {
      nameEl?.classList.add('input-error'); hasError = true;
    } else { nameEl?.classList.remove('input-error'); }

    const creds = {};

    if (type === 's3') {
      const bucketEl = qs('#stor-s3-bucket');
      const accessEl = qs('#stor-s3-access-key');
      const secretEl = qs('#stor-s3-secret');
      
      const bucket = bucketEl?.value?.trim();
      const access = accessEl?.value?.trim();
      const secret = secretEl?.value?.trim();

      if (!bucket) { bucketEl?.classList.add('input-error'); hasError = true; }
      else { bucketEl?.classList.remove('input-error'); }

      if (!access) { accessEl?.classList.add('input-error'); hasError = true; }
      else { accessEl?.classList.remove('input-error'); }

      if (!secret) { secretEl?.classList.add('input-error'); hasError = true; }
      else { secretEl?.classList.remove('input-error'); }

      creds.endpoint = qs('#stor-s3-endpoint')?.value?.trim() || '';
      creds.region = qs('#stor-s3-region')?.value?.trim() || 'us-east-1';
      creds.bucket = bucket;
      creds.prefix = qs('#stor-s3-prefix')?.value?.trim() || '';
      creds.access_key = access;
      creds.secret_key = secret;

    } else if (type === 'gdrive') {
      creds.token = qs('#stor-gdrive-token')?.value?.trim() || '';
      creds.folder_id = qs('#stor-gdrive-folder')?.value?.trim() || '';

    } else if (type === 'nfs') {
      const serverEl = qs('#stor-nfs-server');
      const exportEl = qs('#stor-nfs-export');
      
      const server = serverEl?.value?.trim();
      const exportPath = exportEl?.value?.trim();

      if (!server) { serverEl?.classList.add('input-error'); hasError = true; }
      else { serverEl?.classList.remove('input-error'); }

      if (!exportPath) { exportEl?.classList.add('input-error'); hasError = true; }
      else { exportEl?.classList.remove('input-error'); }

      creds.server = server;
      creds.export_path = exportPath;
      creds.options = qs('#stor-nfs-options')?.value?.trim() || '';

    } else if (type === 'azure') {
      const accountEl = qs('#stor-azure-account');
      const containerEl = qs('#stor-azure-container');
      const keyEl = qs('#stor-azure-key');

      const account = accountEl?.value?.trim();
      const container = containerEl?.value?.trim();
      const key = keyEl?.value?.trim();

      if (!account) { accountEl?.classList.add('input-error'); hasError = true; }
      else { accountEl?.classList.remove('input-error'); }

      if (!container) { containerEl?.classList.add('input-error'); hasError = true; }
      else { containerEl?.classList.remove('input-error'); }

      if (!key) { keyEl?.classList.add('input-error'); hasError = true; }
      else { keyEl?.classList.remove('input-error'); }

      creds.account = account;
      creds.container = container;
      creds.key = key;

    } else if (type === 'sftp') {
      const hostEl = qs('#stor-sftp-host');
      const userEl = qs('#stor-sftp-user');

      const host = hostEl?.value?.trim();
      const user = userEl?.value?.trim();

      if (!host) { hostEl?.classList.add('input-error'); hasError = true; }
      else { hostEl?.classList.remove('input-error'); }

      if (!user) { userEl?.classList.add('input-error'); hasError = true; }
      else { userEl?.classList.remove('input-error'); }

      creds.host = host;
      creds.port = parseInt(qs('#stor-sftp-port')?.value || '22', 10);
      creds.username = user;
      creds.path = qs('#stor-sftp-path')?.value?.trim() || '';
      creds.key = qs('#stor-sftp-key')?.value?.trim() || '';

    } else if (type === 'dropbox') {
      const tokenEl = qs('#stor-dropbox-token');
      const token = tokenEl?.value?.trim();

      if (!token) { tokenEl?.classList.add('input-error'); hasError = true; }
      else { tokenEl?.classList.remove('input-error'); }

      creds.token = token;
      creds.path = qs('#stor-dropbox-path')?.value?.trim() || '';

    } else if (type === 'onedrive') {
      const tokenEl = qs('#stor-onedrive-token');
      const token = tokenEl?.value?.trim();

      if (!token) { tokenEl?.classList.add('input-error'); hasError = true; }
      else { tokenEl?.classList.remove('input-error'); }

      creds.token = token;
      creds.path = qs('#stor-onedrive-path')?.value?.trim() || '';

    } else if (type === 'custom') {
      const endpointEl = qs('#stor-custom-endpoint');
      const endpoint = endpointEl?.value?.trim();

      if (!endpoint) { endpointEl?.classList.add('input-error'); hasError = true; }
      else { endpointEl?.classList.remove('input-error'); }

      creds.endpoint = endpoint;
      creds.token = qs('#stor-custom-token')?.value?.trim() || '';
      creds.meta = qs('#stor-custom-meta')?.value?.trim() || '';
    }

    if (hasError) return;

    this._providers.push({
      id: `storprov-${Date.now()}`,
      name,
      provider_type: type,
      credentials: creds
    });

    this.persistProviders();
    this.render();
    this.closeModal();
    IPCLog.append('info', `Storage provider added: "${name}" (${type.toUpperCase()}) → priority ${this._providers.length}`);
  }
};


/* ═══════════════════════════════════════════════════════════════════
   RuntimeInstanceManager — Remote HTML-NN Instance priority list & modal
   ═══════════════════════════════════════════════════════════════════ */
const RuntimeInstanceManager = {
  _initialized: false,
  _instances: [],

  init() {
    this._initialized = true;
    this._list      = qs('#runtime-instance-list');
    this._btnAdd    = qs('#btn-add-runtime-instance');
    this._modal     = qs('#modal-add-runtime-instance');
    this._btnSave   = qs('#btn-modal-runtime-instance-save');
    this._btnCancel = qs('#btn-modal-runtime-instance-cancel');
    this._btnClose  = qs('#btn-modal-runtime-instance-close');

    if (this._btnAdd)    this._btnAdd.addEventListener('click',   () => this.openModal());
    if (this._btnClose)  this._btnClose.addEventListener('click',  () => this.closeModal());
    if (this._btnCancel) this._btnCancel.addEventListener('click', () => this.closeModal());
    if (this._btnSave)   this._btnSave.addEventListener('click',   () => this.saveInstance());

    if (this._modal) {
      this._modal.addEventListener('click', e => {
        if (e.target === this._modal) this.closeModal();
      });
    }

    this.loadInstances();
  },

  async loadInstances() {
    try {
      const response = await invoke('python:rpc', {
        method: 'runtime.get_instances',
        params: {}
      });
      if (response && response.instances) {
        this._instances = response.instances;
        this.render();
      }
    } catch (err) {
      console.warn('[RuntimeInstanceManager] Failed to load remote instances:', err);
    }
  },

  async persistInstances() {
    try {
      await invoke('python:rpc', {
        method: 'runtime.save_instances',
        params: { instances: this._instances }
      });
      IPCLog.append('ok', 'Remote HTML-NN instances saved to PostgreSQL');
    } catch (err) {
      console.warn('[RuntimeInstanceManager] Failed to persist remote instances:', err);
      IPCLog.append('error', `Failed to save remote instances: ${err.message ?? err}`);
    }
  },

  render() {
    if (!this._list) return;
    const empty = qs('#runtime-instance-list-empty');
    this._list.innerHTML = '';
    if (empty) this._list.appendChild(empty);

    if (this._instances.length === 0) {
      if (empty) empty.hidden = false;
      return;
    }
    if (empty) empty.hidden = true;

    this._instances.forEach((inst, i) => {
      this._list.appendChild(this._buildCard(inst, i));
    });
  },

  _buildCard(inst, i) {
    const total   = this._instances.length;
    const isFirst = i === 0;
    const isLast  = i === total - 1;
    const label   = 'HTML-NN';
    const color   = 'var(--accent-purple)';

    const card = document.createElement('div');
    card.className = 'provider-card provider-type-remote';
    card.style.borderLeftColor = color;
    card.dataset.instanceId = inst.id;
    card.innerHTML = `
      <div class="provider-priority-badge">${i + 1}</div>
      <div class="provider-type-chip type-storage" style="background: ${color}22; color: ${color}; border-color: ${color}44;">${label}</div>
      <div class="provider-card-info">
        <div class="provider-card-name">${inst.name}</div>
        <div class="provider-card-meta">${inst.url} · ${inst.has_auth || inst.api_key ? 'token auth enabled' : 'public/no token'}</div>
      </div>
      <div class="provider-status-indicator">
        <span class="status-dot status-ok"></span>
        <span class="provider-status-text">CONNECTED</span>
      </div>
      <div class="provider-card-actions">
        <button class="btn-provider-action btn-runtime-instance-up"     title="Move Up"    ${isFirst ? 'disabled' : ''}>&#8593;</button>
        <button class="btn-provider-action btn-runtime-instance-down"   title="Move Down"  ${isLast  ? 'disabled' : ''}>&#8595;</button>
        <button class="btn-provider-action btn-runtime-instance-delete" title="Remove Instance">&#x2715;</button>
      </div>`;

    if (!isFirst) card.querySelector('.btn-runtime-instance-up').addEventListener('click',   () => this.move(inst.id, -1));
    if (!isLast)  card.querySelector('.btn-runtime-instance-down').addEventListener('click', () => this.move(inst.id,  1));
    card.querySelector('.btn-runtime-instance-delete').addEventListener('click', () => this.remove(inst.id));

    return card;
  },

  move(id, dir) {
    const idx = this._instances.findIndex(inst => inst.id === id);
    if (idx < 0) return;
    const to = idx + dir;
    if (to < 0 || to >= this._instances.length) return;
    [this._instances[idx], this._instances[to]] = [this._instances[to], this._instances[idx]];
    this.persistInstances();
    this.render();
    IPCLog.append('info', `Remote instance "${this._instances[to].name}" → priority ${to + 1}`);
  },

  remove(id) {
    const idx = this._instances.findIndex(inst => inst.id === id);
    if (idx < 0) return;
    const name = this._instances[idx].name;
    this._instances.splice(idx, 1);
    this.persistInstances();
    this.render();
    IPCLog.append('info', `Remote instance removed: "${name}"`);
  },

  openModal() {
    ['rt-inst-name','rt-inst-url','rt-inst-apikey'].forEach(id => {
      const el = qs(`#${id}`);
      if (el) { el.value = ''; el.classList.remove('input-error'); }
    });
    if (this._modal) this._modal.hidden = false;
    qs('#rt-inst-name')?.focus();
  },

  closeModal() {
    if (this._modal) this._modal.hidden = true;
  },

  saveInstance() {
    const nameEl     = qs('#rt-inst-name');
    const urlEl      = qs('#rt-inst-url');
    const apikeyEl   = qs('#rt-inst-apikey');

    const name     = nameEl?.value?.trim();
    const url      = urlEl?.value?.trim();
    const apiKey   = apikeyEl?.value?.trim() || '';

    let hasError = false;
    if (!name) {
      nameEl?.classList.add('input-error'); hasError = true;
    } else { nameEl?.classList.remove('input-error'); }

    if (!url) {
      urlEl?.classList.add('input-error'); hasError = true;
    } else { urlEl?.classList.remove('input-error'); }

    if (hasError) return;

    this._instances.push({
      id: `rtinst-${Date.now()}`,
      name,
      url,
      api_key: apiKey
    });
    this.persistInstances();
    this.render();
    this.closeModal();
    IPCLog.append('info', `Remote HTML-NN instance added: "${name}" (${url}) → priority ${this._instances.length}`);
  }
};


/* ═══════════════════════════════════════════════════════════════════
   AuthManager — Authentication Credentials priority list & modal
   ═══════════════════════════════════════════════════════════════════ */
const AuthManager = {
  _initialized: false,
  _credentials: [],

  init() {
    this._initialized = true;
    this._list      = qs('#auth-credential-list');
    this._btnAdd    = qs('#btn-add-auth-credential');
    this._modal     = qs('#modal-add-auth-credential');
    this._btnSave   = qs('#btn-modal-auth-credential-save');
    this._btnCancel = qs('#btn-modal-auth-credential-cancel');
    this._btnClose  = qs('#btn-modal-auth-credential-close');
    this._typeSel   = qs('#auth-cred-type');

    if (this._btnAdd)    this._btnAdd.addEventListener('click',   () => this.openModal());
    if (this._btnClose)  this._btnClose.addEventListener('click',  () => this.closeModal());
    if (this._btnCancel) this._btnCancel.addEventListener('click', () => this.closeModal());
    if (this._btnSave)   this._btnSave.addEventListener('click',   () => this.saveCredential());

    if (this._typeSel) {
      this._typeSel.addEventListener('change', () => this.toggleTypeFields());
    }

    if (this._modal) {
      this._modal.addEventListener('click', e => {
        if (e.target === this._modal) this.closeModal();
      });
    }

    this.loadCredentials();
  },

  async loadCredentials() {
    try {
      const response = await invoke('python:rpc', {
        method: 'auth.get_credentials',
        params: {}
      });
      if (response && response.credentials) {
        this._credentials = response.credentials;
        this.render();
      }
    } catch (err) {
      console.warn('[AuthManager] Failed to load credentials:', err);
    }
  },

  async persistCredentials() {
    try {
      await invoke('python:rpc', {
        method: 'auth.save_credentials',
        params: { credentials: this._credentials }
      });
      IPCLog.append('ok', 'Authentication credentials saved to PostgreSQL');
    } catch (err) {
      console.warn('[AuthManager] Failed to persist credentials:', err);
      IPCLog.append('error', `Failed to save credentials: ${err.message ?? err}`);
    }
  },

  toggleTypeFields() {
    const selectedType = this._typeSel?.value;
    if (!selectedType) return;
    
    document.querySelectorAll('#modal-add-auth-credential .auth-fields').forEach(el => {
      el.hidden = el.id !== `auth-fields-${selectedType}`;
    });
  },

  render() {
    if (!this._list) return;
    const empty = qs('#auth-credential-list-empty');
    this._list.innerHTML = '';
    if (empty) this._list.appendChild(empty);

    if (this._credentials.length === 0) {
      if (empty) empty.hidden = false;
      return;
    }
    if (empty) empty.hidden = true;

    this._credentials.forEach((cred, i) => {
      this._list.appendChild(this._buildCard(cred, i));
    });
  },

  _buildCard(cred, i) {
    const total   = this._credentials.length;
    const isFirst = i === 0;
    const isLast  = i === total - 1;
    
    let label = 'SSH';
    let color = '#ff922b'; // orange
    let detail = '';
    
    if (cred.auth_type === 'rdp') {
      label = 'RDP';
      color = '#f06595'; // pink
      detail = cred.config?.host || '';
    } else if (cred.auth_type === 'api_key') {
      label = 'API KEY';
      color = '#4dadf7'; // blue
      detail = cred.config?.service || '';
    } else if (cred.auth_type === 'oauth') {
      label = 'OAUTH';
      color = '#5c7cfa'; // indigo
      detail = cred.config?.provider_url || '';
    } else {
      // ssh
      detail = cred.has_secret || cred.config?.key ? 'Secret stored · redacted at gateway boundary' : '';
    }

    const card = document.createElement('div');
    card.className = `provider-card provider-type-${cred.auth_type}`;
    card.style.borderLeftColor = color;
    card.dataset.credentialId = cred.id;
    card.innerHTML = `
      <div class="provider-priority-badge">${i + 1}</div>
      <div class="provider-type-chip type-storage" style="background: ${color}22; color: ${color}; border-color: ${color}44;">${label}</div>
      <div class="provider-card-info">
        <div class="provider-card-name">${cred.name}</div>
        <div class="provider-card-meta">${detail}</div>
      </div>
      <div class="provider-status-indicator">
        <span class="status-dot status-ok"></span>
        <span class="provider-status-text">ACTIVE</span>
      </div>
      <div class="provider-card-actions">
        <button class="btn-provider-action btn-auth-cred-up"     title="Move Up"    ${isFirst ? 'disabled' : ''}>&#8593;</button>
        <button class="btn-provider-action btn-auth-cred-down"   title="Move Down"  ${isLast  ? 'disabled' : ''}>&#8595;</button>
        <button class="btn-provider-action btn-auth-cred-delete" title="Remove Credential">&#x2715;</button>
      </div>`;

    if (!isFirst) card.querySelector('.btn-auth-cred-up').addEventListener('click',   () => this.move(cred.id, -1));
    if (!isLast)  card.querySelector('.btn-auth-cred-down').addEventListener('click', () => this.move(cred.id,  1));
    card.querySelector('.btn-auth-cred-delete').addEventListener('click', () => this.remove(cred.id));

    return card;
  },

  move(id, dir) {
    const idx = this._credentials.findIndex(c => c.id === id);
    if (idx < 0) return;
    const to = idx + dir;
    if (to < 0 || to >= this._credentials.length) return;
    [this._credentials[idx], this._credentials[to]] = [this._credentials[to], this._credentials[idx]];
    this.persistCredentials();
    this.render();
    IPCLog.append('info', `Credential "${this._credentials[to].name}" → priority ${to + 1}`);
  },

  remove(id) {
    const idx = this._credentials.findIndex(c => c.id === id);
    if (idx < 0) return;
    const name = this._credentials[idx].name;
    this._credentials.splice(idx, 1);
    this.persistCredentials();
    this.render();
    IPCLog.append('info', `Credential removed: "${name}"`);
  },

  openModal() {
    ['auth-cred-name', 'auth-ssh-key', 'auth-ssh-passphrase', 'auth-rdp-host', 'auth-rdp-user', 'auth-rdp-pass', 'auth-apikey-service', 'auth-apikey-val', 'auth-oauth-client-id', 'auth-oauth-client-secret', 'auth-oauth-refresh-token', 'auth-oauth-provider-url'].forEach(id => {
      const el = qs(`#${id}`);
      if (el) { el.value = ''; el.classList.remove('input-error'); }
    });
    if (this._typeSel) {
      this._typeSel.value = 'ssh';
      this.toggleTypeFields();
    }
    if (this._modal) this._modal.hidden = false;
    qs('#auth-cred-name')?.focus();
  },

  closeModal() {
    if (this._modal) this._modal.hidden = true;
  },

  saveCredential() {
    const nameEl = qs('#auth-cred-name');
    const name = nameEl?.value?.trim();
    const type = this._typeSel?.value;

    let hasError = false;
    if (!name) {
      nameEl?.classList.add('input-error'); hasError = true;
    } else { nameEl?.classList.remove('input-error'); }

    const config = {};
    if (type === 'ssh') {
      const keyEl = qs('#auth-ssh-key');
      const passEl = qs('#auth-ssh-passphrase');
      const key = keyEl?.value?.trim();
      const passphrase = passEl?.value || '';
      if (!key) { keyEl?.classList.add('input-error'); hasError = true; }
      else { keyEl?.classList.remove('input-error'); }
      config.key = key;
      config.passphrase = passphrase;
    } else if (type === 'rdp') {
      const hostEl = qs('#auth-rdp-host');
      const userEl = qs('#auth-rdp-user');
      const passEl = qs('#auth-rdp-pass');
      const host = hostEl?.value?.trim();
      const username = userEl?.value?.trim();
      const password = passEl?.value || '';
      if (!host) { hostEl?.classList.add('input-error'); hasError = true; }
      else { hostEl?.classList.remove('input-error'); }
      if (!username) { userEl?.classList.add('input-error'); hasError = true; }
      else { userEl?.classList.remove('input-error'); }
      if (!password) { passEl?.classList.add('input-error'); hasError = true; }
      else { passEl?.classList.remove('input-error'); }
      config.host = host;
      config.username = username;
      config.password = password;
    } else if (type === 'api_key') {
      const serviceEl = qs('#auth-apikey-service');
      const valEl = qs('#auth-apikey-val');
      const service = serviceEl?.value?.trim();
      const api_key = valEl?.value?.trim();
      if (!service) { serviceEl?.classList.add('input-error'); hasError = true; }
      else { serviceEl?.classList.remove('input-error'); }
      if (!api_key) { valEl?.classList.add('input-error'); hasError = true; }
      else { valEl?.classList.remove('input-error'); }
      config.service = service;
      config.api_key = api_key;
    } else if (type === 'oauth') {
      const clientIdEl = qs('#auth-oauth-client-id');
      const clientSecEl = qs('#auth-oauth-client-secret');
      const refreshTokEl = qs('#auth-oauth-refresh-token');
      const provUrlEl = qs('#auth-oauth-provider-url');
      const client_id = clientIdEl?.value?.trim();
      const client_secret = clientSecEl?.value?.trim();
      const refresh_token = refreshTokEl?.value?.trim() || '';
      const provider_url = provUrlEl?.value?.trim() || '';
      if (!client_id) { clientIdEl?.classList.add('input-error'); hasError = true; }
      else { clientIdEl?.classList.remove('input-error'); }
      if (!client_secret) { clientSecEl?.classList.add('input-error'); hasError = true; }
      else { clientSecEl?.classList.remove('input-error'); }
      config.client_id = client_id;
      config.client_secret = client_secret;
      config.refresh_token = refresh_token;
      config.provider_url = provider_url;
    }

    if (hasError) return;

    this._credentials.push({
      id: `authcred-${Date.now()}`,
      name,
      auth_type: type,
      config
    });
    this.persistCredentials();
    this.render();
    this.closeModal();
    IPCLog.append('info', `Credential added: "${name}" (${type}) → priority ${this._credentials.length}`);
  }
};


/* ═══════════════════════════════════════════════════════════════════
   FirewallManager — Blockchain Firewall & Trust Matrix Manager
   ═══════════════════════════════════════════════════════════════════ */
const FirewallManager = {
  _initialized: false,
  _contacts: [],

  init() {
    this._initialized = true;
    this._list            = qs('#firewall-contact-list');
    this._btnAdd          = qs('#btn-add-firewall-contact');
    this._modal           = qs('#modal-add-firewall-contact');
    this._btnSave         = qs('#btn-modal-firewall-contact-save');
    this._btnCancel       = qs('#btn-modal-firewall-contact-cancel');
    this._btnClose        = qs('#btn-modal-firewall-contact-close');
    this._clearanceSlider = qs('#fw-contact-clearance');
    this._clearanceVal    = qs('#fw-clearance-value');

    if (this._btnAdd)    this._btnAdd.addEventListener('click',   () => this.openModal());
    if (this._btnClose)  this._btnClose.addEventListener('click',  () => this.closeModal());
    if (this._btnCancel) this._btnCancel.addEventListener('click', () => this.closeModal());
    if (this._btnSave)   this._btnSave.addEventListener('click',   () => this.saveContact());

    if (this._clearanceSlider && this._clearanceVal) {
      this._clearanceSlider.addEventListener('input', e => {
        this._clearanceVal.textContent = e.target.value;
      });
    }

    if (this._modal) {
      this._modal.addEventListener('click', e => {
        if (e.target === this._modal) this.closeModal();
      });
    }

    this.loadContacts();
  },

  async loadContacts() {
    try {
      const response = await invoke('python:rpc', {
        method: 'firewall.get_contacts',
        params: {}
      });
      if (response && response.contacts) {
        this._contacts = response.contacts;
        this.render();
      }
    } catch (err) {
      console.warn('[FirewallManager] Failed to load contacts:', err);
    }
  },

  async persistContacts() {
    try {
      await invoke('python:rpc', {
        method: 'firewall.save_contacts',
        params: { contacts: this._contacts }
      });
      IPCLog.append('ok', 'Firewall trusted contacts saved to PostgreSQL');
    } catch (err) {
      console.warn('[FirewallManager] Failed to persist contacts:', err);
      IPCLog.append('error', `Failed to save contacts: ${err.message ?? err}`);
    }
  },

  render() {
    if (!this._list) return;
    const empty = qs('#firewall-contact-list-empty');
    this._list.innerHTML = '';
    if (empty) this._list.appendChild(empty);

    if (this._contacts.length === 0) {
      if (empty) empty.hidden = false;
      return;
    }
    if (empty) empty.hidden = true;

    this._contacts.forEach((contact, i) => {
      this._list.appendChild(this._buildCard(contact, i));
    });
  },

  _buildCard(contact, i) {
    const total   = this._contacts.length;
    const isFirst = i === 0;
    const isLast  = i === total - 1;
    
    const label = 'PEER';
    const color = '#ae3ec9'; // violet color for cryptographic contacts
    
    // access rights details
    const clearance = contact.access_rights?.clearance_level ?? 0;
    const categories = contact.access_rights?.allowed_categories ?? [];
    const catBadges = categories.map(cat => 
      `<span style="background: rgba(45, 212, 191, 0.15); color: var(--accent-teal); border: 1px solid rgba(45, 212, 191, 0.3); font-weight: 600; padding: 1px 6px; border-radius: 4px; font-size: 0.72rem; text-transform: uppercase;">${cat}</span>`
    ).join(' ');

    const card = document.createElement('div');
    card.className = `provider-card provider-type-firewall`;
    card.style.borderLeft = `3px solid ${color}`;
    card.dataset.contactId = contact.id;
    card.innerHTML = `
      <div class="provider-priority-badge">${i + 1}</div>
      <div class="provider-type-chip type-storage" style="background: ${color}22; color: ${color}; border-color: ${color}44;">${label}</div>
      <div class="provider-card-info">
        <div class="provider-card-name">${contact.name}</div>
        <div class="provider-card-meta" style="display: flex; flex-direction: column; gap: 4px;">
          <div style="font-family: var(--font-mono); font-size: 0.72rem; color: var(--text-secondary); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 320px;">${contact.id}</div>
          <div style="display: flex; align-items: center; gap: 6px; flex-wrap: wrap;">
            <span style="background: rgba(167, 139, 250, 0.15); color: var(--accent-purple); border: 1px solid rgba(167, 139, 250, 0.3); font-weight: 600; padding: 1px 6px; border-radius: 4px; font-size: 0.72rem;">Lvl ${clearance}</span>
            ${catBadges || '<span style="color: var(--text-muted); font-size: 0.72rem; font-style: italic;">No categories</span>'}
          </div>
        </div>
      </div>
      <div class="provider-status-indicator">
        <span class="status-dot status-ok"></span>
        <span class="provider-status-text">TRUSTED</span>
      </div>
      <div class="provider-card-actions">
        <button class="btn-provider-action btn-fw-contact-up"     title="Move Up"    ${isFirst ? 'disabled' : ''}>&#8593;</button>
        <button class="btn-provider-action btn-fw-contact-down"   title="Move Down"  ${isLast  ? 'disabled' : ''}>&#8595;</button>
        <button class="btn-provider-action btn-fw-contact-delete" title="Remove Contact">&#x2715;</button>
      </div>`;

    if (!isFirst) card.querySelector('.btn-fw-contact-up').addEventListener('click',   () => this.move(contact.id, -1));
    if (!isLast)  card.querySelector('.btn-fw-contact-down').addEventListener('click', () => this.move(contact.id,  1));
    card.querySelector('.btn-fw-contact-delete').addEventListener('click', () => this.remove(contact.id));

    return card;
  },

  move(id, dir) {
    const idx = this._contacts.findIndex(c => c.id === id);
    if (idx < 0) return;
    const to = idx + dir;
    if (to < 0 || to >= this._contacts.length) return;
    [this._contacts[idx], this._contacts[to]] = [this._contacts[to], this._contacts[idx]];
    this.persistContacts();
    this.render();
    IPCLog.append('info', `Trusted contact "${this._contacts[to].name}" → priority ${to + 1}`);
  },

  remove(id) {
    const idx = this._contacts.findIndex(c => c.id === id);
    if (idx < 0) return;
    const name = this._contacts[idx].name;
    this._contacts.splice(idx, 1);
    this.persistContacts();
    this.render();
    IPCLog.append('info', `Trusted contact removed: "${name}"`);
  },

  openModal() {
    const nameEl = qs('#fw-contact-name');
    const keyEl = qs('#fw-contact-key');
    const clearanceEl = qs('#fw-contact-clearance');
    const clearanceVal = qs('#fw-clearance-value');
    const nodeNamesEl = qs('#fw-contact-node-names');
    
    if (nameEl) { nameEl.value = ''; nameEl.classList.remove('input-error'); }
    if (keyEl) { keyEl.value = ''; keyEl.classList.remove('input-error'); }
    if (clearanceEl) { clearanceEl.value = '1'; }
    if (clearanceVal) { clearanceVal.textContent = '1'; }
    if (nodeNamesEl) { nodeNamesEl.value = ''; }
    
    // Reset categories (default rp checked, others unchecked)
    const rpCheck = qs('#fw-cat-rp');
    const codingCheck = qs('#fw-cat-coding');
    const identityCheck = qs('#fw-cat-identity');
    const workflowsCheck = qs('#fw-cat-workflows');
    
    if (rpCheck) rpCheck.checked = true;
    if (codingCheck) codingCheck.checked = false;
    if (identityCheck) identityCheck.checked = false;
    if (workflowsCheck) workflowsCheck.checked = false;
    
    if (this._modal) this._modal.hidden = false;
    nameEl?.focus();
  },

  closeModal() {
    if (this._modal) this._modal.hidden = true;
  },

  saveContact() {
    const nameEl = qs('#fw-contact-name');
    const keyEl = qs('#fw-contact-key');
    const clearanceEl = qs('#fw-contact-clearance');
    const nodeNamesEl = qs('#fw-contact-node-names');
    
    const name = nameEl?.value?.trim();
    const key = keyEl?.value?.trim();
    const clearance = parseInt(clearanceEl?.value || '1', 10);
    const customNodeNames = this._parseFirewallNodeNames(nodeNamesEl?.value || '');
    
    let hasError = false;
    if (!name) {
      nameEl?.classList.add('input-error'); hasError = true;
    } else { nameEl?.classList.remove('input-error'); }

    if (!key) {
      keyEl?.classList.add('input-error'); hasError = true;
    } else { keyEl?.classList.remove('input-error'); }
    
    // Check categories
    const categories = [];
    if (qs('#fw-cat-rp')?.checked) categories.push('rp');
    if (qs('#fw-cat-coding')?.checked) categories.push('coding');
    if (qs('#fw-cat-identity')?.checked) categories.push('identity');
    if (qs('#fw-cat-workflows')?.checked) categories.push('workflows');
    customNodeNames.forEach(name => {
      if (!categories.includes(name)) categories.push(name);
    });
    
    if (categories.length === 0) {
      // Must select at least one category to grant access
      IPCLog.append('error', 'Must select at least one allowed node category');
      hasError = true;
    }

    if (hasError) return;

    // Check if ID already exists (cryptographic address must be unique)
    const exists = this._contacts.some(c => c.id.toLowerCase() === key.toLowerCase());
    if (exists) {
      keyEl?.classList.add('input-error');
      IPCLog.append('error', `A contact with address "${key}" is already registered`);
      return;
    }

    this._contacts.push({
      id: key,
      name,
      access_rights: {
        clearance_level: clearance,
        allowed_categories: categories
      }
    });

    this.persistContacts();
    this.render();
    this.closeModal();
    IPCLog.append('ok', `Trusted contact added: "${name}" (${key})`);
  }
};


/* ═══════════════════════════════════════════════════════════════════
   LoggingManager — Real-Time Error Logging & System Health Tab
   ═══════════════════════════════════════════════════════════════════ */
const LoggingManager = {
  _initialized: false,
  _events: [],
  _selectedEventId: null,
  _liveTailInterval: null,
  _activeLevels: ['ERROR', 'WARN', 'INFO', 'DEBUG'],

  init() {
    this._initialized = true;

    // Elements
    this._tbody             = qs('#log-events-tbody');
    this._btnRefresh        = qs('#btn-log-refresh');
    this._chkLiveTail       = qs('#chk-log-livetail');
    this._btnExport         = qs('#btn-log-export');
    this._btnArchiveCurrent = qs('#btn-log-archive-current');
    this._btnArchiveAll     = qs('#btn-log-archive-all');
    this._btnClearCurrent   = qs('#btn-log-clear-current');
    this._btnClearAll       = qs('#btn-log-clear-all');
    this._sourceSel         = qs('#log-filter-source');
    this._subsystemSel      = qs('#log-filter-subsystem');
    this._searchInput       = qs('#log-search-input');
    this._errorsOnlyBtn     = qs('#btn-log-errors-only');
    this._inspectorEmpty    = qs('#log-inspector-empty');
    this._inspectorDetails  = qs('#log-inspector-details');

    // Bind event listeners
    if (this._btnRefresh) this._btnRefresh.addEventListener('click', () => this.loadEvents());
    
    if (this._chkLiveTail) {
      this._chkLiveTail.addEventListener('change', () => this.toggleLiveTail());
    }

    if (this._btnExport) this._btnExport.addEventListener('click', () => this.exportLogs());
    
    if (this._btnArchiveCurrent) this._btnArchiveCurrent.addEventListener('click', () => this.archiveLogs('current'));
    if (this._btnArchiveAll)     this._btnArchiveAll.addEventListener('click',     () => this.archiveLogs('all'));
    
    if (this._btnClearCurrent)   this._btnClearCurrent.addEventListener('click',   () => this.clearLogs('current'));
    if (this._btnClearAll)       this._btnClearAll.addEventListener('click',       () => this.clearLogs('all'));

    if (this._sourceSel)         this._sourceSel.addEventListener('change',        () => this.loadEvents());
    if (this._subsystemSel)      this._subsystemSel.addEventListener('change',     () => this.loadEvents());
    if (this._searchInput)       this._searchInput.addEventListener('input',       () => this.loadEvents());

    // Level buttons click binding
    document.querySelectorAll('.btn-log-level').forEach(btn => {
      btn.addEventListener('click', e => {
        const lvl = e.target.dataset.level;
        e.target.classList.toggle('active');
        this.updateActiveLevels();
        this.loadEvents();
      });
    });

    if (this._errorsOnlyBtn) {
      this._errorsOnlyBtn.addEventListener('click', () => {
        this._errorsOnlyBtn.classList.toggle('active');
        const isErrorsOnly = this._errorsOnlyBtn.classList.contains('active');
        
        document.querySelectorAll('.btn-log-level').forEach(btn => {
          const lvl = btn.dataset.level;
          if (isErrorsOnly) {
            // Only allow ERROR and WARN
            if (lvl === 'ERROR' || lvl === 'WARN') {
              btn.classList.add('active');
            } else {
              btn.classList.remove('active');
            }
          } else {
            // Restore all
            btn.classList.add('active');
          }
        });
        
        this.updateActiveLevels();
        this.loadEvents();
      });
    }

    this.loadEvents();
  },

  updateActiveLevels() {
    this._activeLevels = [];
    document.querySelectorAll('.btn-log-level.active').forEach(btn => {
      this._activeLevels.push(btn.dataset.level);
    });
  },

  async loadEvents() {
    try {
      const source = this._sourceSel ? this._sourceSel.value : 'Audit Log';
      const subsystem = this._subsystemSel ? this._subsystemSel.value : 'All Subsystems';
      const search = this._searchInput ? this._searchInput.value : '';

      // 1. Get events & metrics
      const response = await invoke('python:rpc', {
        method: 'logging.get_events',
        params: {
          limit: 500,
          levels: this._activeLevels,
          subsystem,
          search,
          source
        }
      });

      if (response) {
        if (response.metrics) {
          this.updateMetrics(response.metrics);
        }
        if (response.events) {
          this._events = response.events;
          this.renderEvents();
        }
      }

      // 2. Get system health indicator values
      const health = await invoke('python:rpc', {
        method: 'logging.get_health',
        params: {}
      });

      if (health) {
        this.updateSystemHealth(health);
      }

    } catch (err) {
      console.warn('[LoggingManager] Failed to load log events:', err);
    }
  },

  updateMetrics(metrics) {
    const totalEl = qs('#log-metric-total');
    const errWarnEl = qs('#log-metric-err-warn');
    const auditsEl = qs('#log-metric-audits');
    const sessionsTurnsEl = qs('#log-metric-sessions-turns');

    if (totalEl) totalEl.textContent = metrics.total_events;
    if (errWarnEl) errWarnEl.textContent = `${metrics.errors} / ${metrics.warnings}`;
    if (auditsEl) auditsEl.textContent = metrics.prompt_audits;
    if (sessionsTurnsEl) sessionsTurnsEl.textContent = `${metrics.sessions} / ${metrics.turns}`;
  },

  updateSystemHealth(health) {
    const overallText = qs('#health-overall-text');
    const overallDot = qs('#health-overall-dot');
    const overallMode = qs('#health-overall-mode');

    // Update overall statuses
    if (overallText) {
      overallText.textContent = health.overall;
      overallText.className = ''; // clear
      overallText.style.color = health.overall === 'HEALTHY' ? 'var(--status-ok)' :
                                (health.overall === 'CRITICAL' ? 'var(--status-error)' : 'var(--status-warn)');
    }

    if (overallDot) {
      overallDot.style.backgroundColor = health.overall === 'HEALTHY' ? 'var(--status-ok)' :
                                         (health.overall === 'CRITICAL' ? 'var(--status-error)' : 'var(--status-warn)');
    }

    if (overallMode) {
      overallMode.textContent = health.mode;
    }

    // Update the 12 health service dots
    if (health.services) {
      for (const [service, status] of Object.entries(health.services)) {
        const dot = qs(`#health-dot-${service}`);
        if (dot) {
          dot.className = 'status-dot'; // reset
          if (status === 'HEALTHY') dot.classList.add('status-ok');
          else if (status === 'CRITICAL') dot.classList.add('status-error');
          else dot.classList.add('status-warn');
        }
      }
    }
  },

  renderEvents() {
    if (!this._tbody) return;
    this._tbody.innerHTML = '';

    if (this._events.length === 0) {
      const row = document.createElement('tr');
      row.innerHTML = `
        <td colspan="4" style="text-align: center; color: var(--text-muted); padding: var(--space-4); font-style: italic;">
          No events matching filters
        </td>
      `;
      this._tbody.appendChild(row);
      this.clearInspector();
      return;
    }

    // Maintain selection state
    let selectedEventStillExists = false;

    this._events.forEach(event => {
      const isSelected = event.id === this._selectedEventId;
      if (isSelected) selectedEventStillExists = true;

      const tr = document.createElement('tr');
      tr.className = isSelected ? 'selected' : '';
      tr.dataset.eventId = event.id;

      // Status dot based on Level
      let dotColor = 'var(--status-unknown)';
      if (event.level === 'ERROR') dotColor = 'var(--status-error)';
      else if (event.level === 'WARN' || event.level === 'WARNING') dotColor = 'var(--status-warn)';
      else if (event.level === 'INFO') dotColor = 'var(--accent-blue)';

      // Format Timestamp: MM/DD, HH:MM:SS
      let formattedTime = '';
      try {
        const dt = new Date(event.timestamp);
        const mm = String(dt.getMonth() + 1).padStart(2, '0');
        const dd = String(dt.getDate()).padStart(2, '0');
        const hh = String(dt.getHours()).padStart(2, '0');
        const min = String(dt.getMinutes()).padStart(2, '0');
        const ss = String(dt.getSeconds()).padStart(2, '0');
        formattedTime = `${mm}/${dd}, ${hh}:${min}:${ss}`;
      } catch (e) {
        formattedTime = event.timestamp;
      }

      tr.innerHTML = `
        <td style="text-align: center; padding-left: var(--space-4);"><span class="status-dot" style="background-color: ${dotColor};"></span></td>
        <td style="font-family: var(--font-mono); font-size: 0.76rem;">${formattedTime}</td>
        <td><span class="subsystem-badge subsystem-badge-${event.subsystem.toLowerCase()}">${event.subsystem}</span></td>
        <td class="event-type-link">${event.event_type}</td>
      `;

      tr.addEventListener('click', () => this.selectEvent(event.id, tr));
      this._tbody.appendChild(tr);
    });

    if (!selectedEventStillExists) {
      this.clearInspector();
    }
  },

  selectEvent(id, rowEl) {
    this._selectedEventId = id;
    
    // Select styling
    if (this._tbody) {
      this._tbody.querySelectorAll('tr').forEach(r => r.classList.remove('selected'));
    }
    if (rowEl) rowEl.classList.add('selected');

    // Details inspection
    const event = this._events.find(e => e.id === id);
    if (!event) {
      this.clearInspector();
      return;
    }

    if (this._inspectorEmpty) this._inspectorEmpty.hidden = true;
    if (this._inspectorDetails) {
      this._inspectorDetails.hidden = false;
      
      let dotColor = 'var(--status-unknown)';
      if (event.level === 'ERROR') dotColor = 'var(--status-error)';
      else if (event.level === 'WARN' || event.level === 'WARNING') dotColor = 'var(--status-warn)';
      else if (event.level === 'INFO') dotColor = 'var(--accent-blue)';

      const traceJson = event.trace ? JSON.stringify(event.trace, null, 2) : '{}';

      this._inspectorDetails.innerHTML = `
        <div style="display: flex; align-items: center; justify-content: space-between; border-bottom: 1px solid rgba(255,255,255,0.08); padding-bottom: var(--space-2); margin-bottom: var(--space-2);">
          <div style="display: flex; align-items: center; gap: 8px;">
            <span class="status-dot" style="background-color: ${dotColor}; width: 10px; height: 10px;"></span>
            <span style="font-weight: 700; font-size: 0.95rem; text-transform: uppercase;">${event.level}</span>
          </div>
          <span class="subsystem-badge subsystem-badge-${event.subsystem.toLowerCase()}">${event.subsystem}</span>
        </div>
        
        <div style="display: flex; flex-direction: column; gap: var(--space-3); flex: 1; min-height: 0; overflow-y: auto;">
          <div class="inspector-detail-row">
            <span class="inspector-detail-label">Event Type</span>
            <span class="inspector-detail-value" style="font-weight: 600; color: var(--accent-blue); font-family: var(--font-mono);">${event.event_type}</span>
          </div>
          
          <div class="inspector-detail-row">
            <span class="inspector-detail-label">Timestamp</span>
            <span class="inspector-detail-value" style="font-family: var(--font-mono);">${new Date(event.timestamp).toLocaleString()}</span>
          </div>
          
          ${event.tx_id ? `
          <div class="inspector-detail-row" style="background: rgba(174, 62, 201, 0.05); border: 1px dashed rgba(174, 62, 201, 0.2); border-radius: 4px; padding: var(--space-2); text-align: left;">
            <span class="inspector-detail-label" style="color: var(--accent-purple); font-weight: 700; font-size: 0.68rem; text-transform: uppercase;">Blockchain Tx ID</span>
            <span class="inspector-detail-value" style="font-family: var(--font-mono); color: var(--accent-purple); font-weight: 700; word-break: break-all; font-size: 0.74rem;">${event.tx_id}</span>
          </div>
          ` : ''}

          <div class="inspector-detail-row">
            <span class="inspector-detail-label">Event Message</span>
            <span class="inspector-detail-value" style="line-height: 1.4; color: var(--text-primary); font-weight: 500;">${event.message}</span>
          </div>

          <div class="inspector-detail-row" style="flex: 1; display: flex; flex-direction: column; min-height: 150px; border-bottom: none;">
            <span class="inspector-detail-label">Trace Payload / Metadata</span>
            <pre style="margin-top: 4px; padding: 10px; background: rgba(0,0,0,0.3); border: 1px solid var(--glass-border); border-radius: var(--radius-sm); font-family: var(--font-mono); font-size: 0.74rem; overflow: auto; flex: 1; text-align: left; color: var(--accent-teal);"><code class="language-json">${traceJson}</code></pre>
          </div>
        </div>
      `;
    }
  },

  clearInspector() {
    this._selectedEventId = null;
    if (this._inspectorEmpty) this._inspectorEmpty.hidden = false;
    if (this._inspectorDetails) {
      this._inspectorDetails.hidden = true;
      this._inspectorDetails.innerHTML = '';
    }
  },

  toggleLiveTail() {
    if (this._chkLiveTail && this._chkLiveTail.checked) {
      this._liveTailInterval = setInterval(() => this.loadEvents(), 2000);
      IPCLog.append('info', 'Real-time logging Live Tail started (2s interval)');
    } else {
      if (this._liveTailInterval) {
        clearInterval(this._liveTailInterval);
        this._liveTailInterval = null;
      }
      IPCLog.append('info', 'Real-time logging Live Tail stopped');
    }
  },

  exportLogs() {
    if (this._events.length === 0) {
      IPCLog.append('error', 'No active logs to export');
      return;
    }
    try {
      const dataStr = "data:text/json;charset=utf-8," + encodeURIComponent(JSON.stringify(this._events, null, 2));
      const downloadAnchor = document.createElement('a');
      downloadAnchor.setAttribute("href",     dataStr);
      downloadAnchor.setAttribute("download", `tala_logs_export_${Date.now()}.json`);
      document.body.appendChild(downloadAnchor);
      downloadAnchor.click();
      downloadAnchor.remove();
      IPCLog.append('ok', `Successfully exported ${this._events.length} log events as JSON`);
    } catch (e) {
      console.error(e);
      IPCLog.append('error', 'Failed to export logs');
    }
  },

  async archiveLogs(scope) {
    const confirmation = confirm(`Are you sure you want to archive ${scope === 'all' ? 'ALL' : 'currently filtered'} logs?\nThis will export them to a backup JSON file in data/archives/ and remove them from active memory.`);
    if (!confirmation) return;

    try {
      const source = this._sourceSel ? this._sourceSel.value : 'Audit Log';
      const subsystem = this._subsystemSel ? this._subsystemSel.value : 'All Subsystems';
      const search = this._searchInput ? this._searchInput.value : '';

      const response = await invoke('python:rpc', {
        method: 'logging.archive_events',
        params: {
          scope,
          levels: this._activeLevels,
          subsystem,
          search,
          source
        }
      });

      if (response && response.success) {
        const cnt = response.count ?? 0;
        IPCLog.append('ok', `Archived ${cnt} log events to: ${response.archive_file}`);
        this.clearInspector();
        this.loadEvents();
      }
    } catch (err) {
      console.warn('[LoggingManager] Failed to archive logs:', err);
      IPCLog.append('error', `Failed to archive logs: ${err.message ?? err}`);
    }
  },

  async clearLogs(scope) {
    const confirmation = confirm(`Are you sure you want to delete ${scope === 'all' ? 'ALL' : 'currently filtered'} logs from the database?\nThis operation is permanent and cannot be undone.`);
    if (!confirmation) return;

    try {
      const subsystem = this._subsystemSel ? this._subsystemSel.value : 'All Subsystems';
      const search = this._searchInput ? this._searchInput.value : '';

      const response = await invoke('python:rpc', {
        method: 'logging.clear_events',
        params: {
          scope,
          levels: this._activeLevels,
          subsystem,
          search
        }
      });

      if (response && response.success) {
        IPCLog.append('ok', `Cleared log events from active memory`);
        this.clearInspector();
        this.loadEvents();
      }
    } catch (err) {
      console.warn('[LoggingManager] Failed to clear logs:', err);
      IPCLog.append('error', `Failed to clear logs: ${err.message ?? err}`);
    }
  }
};


/* ═══════════════════════════════════════════════════════════════════
   ResearchManager — NotebookLM-Style Research Library Tab
   ═══════════════════════════════════════════════════════════════════ */
const ResearchManager = {
  _initialized: false,
  _results: [],
  _selectedResult: null,
  _checkedResults: new Set(),

  init() {
    this._initialized = true;

    // Elements
    this._subjectInput    = qs('#research-subject');
    this._btnSearch       = qs('#btn-research-search');
    this._sourcesList     = qs('#research-sources-list');
    this._btnSaveNode     = qs('#btn-research-save-node');
    this._previewArea     = qs('#research-preview-area');
    this._badge           = qs('#context-viewer-badge');
    this._searchEngineSelect = qs('#research-search-engine');
    
    this._toggleAll       = qs('#toggle-source-all');
    this._toggles         = document.querySelectorAll('.source-toggle');

    // Event listeners
    if (this._btnSearch) {
      this._btnSearch.addEventListener('click', () => this.search());
    }
    
    if (this._subjectInput) {
      this._subjectInput.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') this.search();
      });
    }

    if (this._btnSaveNode) {
      this._btnSaveNode.addEventListener('click', () => this.saveSelectedAsNode());
    }

    if (this._toggleAll) {
      this._toggleAll.addEventListener('change', (e) => {
        if (e.target.checked) {
          this._toggles.forEach(t => t.checked = false);
        }
      });
    }

    this._toggles.forEach(t => {
      t.addEventListener('change', () => {
        if (t.checked && this._toggleAll) {
          this._toggleAll.checked = false;
        }
      });
    });
  },

  async populateSearchProviders() {
    if (!this._searchEngineSelect) return;
    try {
      const response = await invoke('python:rpc', {
        method: 'search.get_providers',
        params: {}
      });
      if (response && response.providers) {
        this._searchEngineSelect.innerHTML = `
          <option value="">Default Routing</option>
          <option value="crawler">Direct Web Crawler</option>
        `;
        response.providers.forEach(p => {
          const opt = document.createElement('option');
          opt.value = p.id;
          opt.textContent = `${p.name} (${p.endpoint})`;
          if (p.locked) {
            opt.selected = true;
          }
          this._searchEngineSelect.appendChild(opt);
        });
      }
    } catch (err) {
      console.warn('[ResearchManager] Failed to populate search engines:', err);
    }
  },

  async search() {
    if (!this._subjectInput || !this._sourcesList || !this._btnSearch) return;
    const query = this._subjectInput.value.trim();
    if (!query) {
      IPCLog.append('error', 'Please enter a search query.');
      return;
    }

    let sources = [];
    if (this._toggleAll && this._toggleAll.checked) {
      sources = ['all'];
    } else {
      this._toggles.forEach(t => {
        if (t.checked) sources.push(t.value);
      });
    }

    if (sources.length === 0) {
      IPCLog.append('error', 'Please select at least one source to search.');
      return;
    }

    this._btnSearch.disabled = true;
    this._btnSearch.innerHTML = `<span class="spinner-border spinner-border-sm" role="status" aria-hidden="true"></span> Searching...`;
    this._sourcesList.innerHTML = `<div style="text-align: center; color: var(--text-muted); padding: var(--space-4);"><span class="spinner-border spinner-border-sm"></span> Searching evidence...</div>`;
    this._btnSaveNode.disabled = true;
    this._selectedResult = null;
    this._checkedResults.clear();
    this.renderContextViewer();

    try {
      const searchProviderId = this._searchEngineSelect ? this._searchEngineSelect.value : "";
      const response = await invoke('python:rpc', {
        method: 'research.unified_search',
        params: { query, sources, search_provider_id: searchProviderId }
      });

      this._results = response?.results || [];
      const errors = response?.errors || {};
      
      let errorHtml = '';
      for (const [source, errStr] of Object.entries(errors)) {
          errorHtml += `<div class="source-error-banner" style="background: rgba(220,53,69,0.1); color: var(--status-error); padding: 8px; margin-bottom: 8px; border-radius: 4px; font-size: 0.8rem;">
            ⚠️ ${source.toUpperCase()} Error: ${escapeHtml(errStr)}
          </div>`;
      }

      if (this._results.length === 0) {
        this._sourcesList.innerHTML = errorHtml + `<div class="empty-state" style="padding: var(--space-4); text-align: center; color: var(--text-muted); font-style: italic;">No results found.</div>`;
        return;
      }

      this.renderResults();
      if (errorHtml) {
          this._sourcesList.insertAdjacentHTML('afterbegin', errorHtml);
      }
    } catch (err) {
      console.warn('[ResearchManager] Search failed:', err);
      this._sourcesList.innerHTML = `<div class="empty-state" style="padding: var(--space-4); text-align: center; color: var(--status-error); font-style: italic;">Search failed: ${err.message ?? err}</div>`;
    } finally {
      this._btnSearch.disabled = false;
      this._btnSearch.innerHTML = `<span class="btn-text">Search</span>`;
    }
  },

  renderResults() {
    if (!this._sourcesList) return;
    this._sourcesList.innerHTML = '';

    this._results.forEach((res, index) => {
      const item = document.createElement('div');
      item.className = 'research-source-item';
      item.style.cursor = 'pointer';
      item.style.display = 'flex';
      item.style.alignItems = 'flex-start';
      if (this._selectedResult === res) {
        item.style.borderColor = 'var(--accent-teal)';
        item.style.background = 'rgba(45, 212, 191, 0.1)';
      }

      const sourceColors = {
        web: 'var(--accent-blue)',
        session: 'var(--accent-purple)',
        node: 'var(--accent-teal)',
        task: 'var(--accent-orange)'
      };
      const color = sourceColors[res.source_type] || 'var(--text-muted)';
      const isChecked = this._checkedResults.has(res);

      item.innerHTML = `
        <div style="margin-right: 12px; margin-top: 4px;" class="checkbox-wrapper">
          <input type="checkbox" class="source-checkbox" ${isChecked ? 'checked' : ''} style="cursor: pointer;" />
        </div>
        <div style="flex: 1; min-width: 0; display: flex; flex-direction: column;">
          <div style="display: flex; justify-content: space-between; align-items: flex-start;">
            <span class="research-source-title" style="flex: 1;">${escapeHtml(res.title || 'Untitled')}</span>
            <span class="badge" style="background: ${color}; color: #fff; font-size: 0.6rem; margin-left: 8px;">${(res.source_type || 'unknown').toUpperCase()}</span>
          </div>
          <span class="research-source-url" style="color: var(--text-muted); font-size: 0.75rem; margin-top: 4px;">${escapeHtml(res.snippet || '')}</span>
          <div style="font-size: 0.65rem; color: var(--text-muted); margin-top: 6px;">ID: ${escapeHtml(res.id || '')}</div>
        </div>
      `;

      item.addEventListener('click', (e) => {
        if (e.target.classList.contains('source-checkbox')) {
          if (e.target.checked) {
            this._checkedResults.add(res);
          } else {
            this._checkedResults.delete(res);
          }
          const saveBtn = qs('#btn-research-save-node');
          if (saveBtn) {
            saveBtn.textContent = this._checkedResults.size > 0 ? `Distill Collection (${this._checkedResults.size})` : 'Save to Node';
          }
          return;
        }
        this._selectedResult = res;
        this.renderResults();
        this.loadContext(res);
      });

      this._sourcesList.appendChild(item);
    });
  },

  async loadContext(res) {
    if (!this._previewArea) return;
    this._previewArea.innerHTML = `<div style="text-align: center; color: var(--text-muted); padding: var(--space-4);"><span class="spinner-border spinner-border-sm"></span> Loading context...</div>`;
    this._btnSaveNode.disabled = true;

    try {
      const response = await invoke('python:rpc', {
        method: 'memory.browse',
        params: {
          source_type: res.source_type,
          ref_id: res.ref?.url || res.ref?.id || res.id,
          session_id: res.session_id,
          timestamp: res.timestamp
        }
      });

      let content = response?.context || 'No context returned.';
      this._selectedResult.fullContext = content;
      this.renderContextViewer();
      this._btnSaveNode.disabled = false;
    } catch (err) {
      this._previewArea.innerHTML = `<div class="empty-state" style="color: var(--status-error); font-style: italic;">Failed to load context: ${escapeHtml(err.message ?? String(err))}</div>`;
    }
  },

  renderContextViewer() {
    if (!this._previewArea) return;
    if (!this._selectedResult) {
      this._previewArea.innerHTML = `<div class="empty-state" style="padding: var(--space-6); text-align: center; color: var(--text-muted); font-style: italic;">Select a result from the left panel to view its full context.</div>`;
      if (this._badge) this._badge.style.display = 'none';
      return;
    }

    if (this._badge) {
      this._badge.style.display = 'inline-block';
      const src = this._selectedResult.source_type || 'unknown';
      this._badge.textContent = `Source: ${src.toUpperCase()}`;
    }

    const content = this._selectedResult.fullContext || this._selectedResult.snippet || '';
    this._previewArea.textContent = content; // Using textContent for safe text rendering
  },

  async saveSelectedAsNode() {
    let resultsToDistill = [];
    if (this._checkedResults.size > 0) {
      resultsToDistill = Array.from(this._checkedResults).map(r => ({
        id: r.id,
        source_type: r.source_type,
        ref_id: r.ref?.url || r.ref?.id || r.id,
        title: r.title,
        snippet: r.snippet
      }));
    } else if (this._selectedResult && this._selectedResult.fullContext) {
      resultsToDistill = [{
        id: this._selectedResult.id,
        source_type: this._selectedResult.source_type,
        ref_id: this._selectedResult.ref?.url || this._selectedResult.ref?.id || this._selectedResult.id,
        title: this._selectedResult.title,
        snippet: this._selectedResult.fullContext
      }];
    }

    if (resultsToDistill.length === 0) return;

    this._btnSaveNode.disabled = true;
    const originalText = this._btnSaveNode.innerHTML;
    this._btnSaveNode.innerHTML = `<span class="spinner-border spinner-border-sm" role="status" aria-hidden="true"></span> Distilling...`;

    try {
      const response = await invoke('python:rpc', {
        method: 'research.distill_to_node',
        params: {
          results: resultsToDistill
        }
      });

      if (response && response.html_nn) {
        const title = response.title || (resultsToDistill.length > 1 ? 'Distilled Collection' : (resultsToDistill[0].title || 'Untitled Evidence'));
        const slug = title.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/(^-|-$)/g, '');
        const relativePath = `nodes/concept-${slug}-${Date.now()}.html`;

        const writeRes = await invoke('file:write', {
          filePath: relativePath,
          data: response.html_nn
        });

        if (writeRes && writeRes.ok) {
          IPCLog.append('ok', `Successfully saved evidence as Neural Node: ${relativePath}`);
          if (typeof FileExplorer !== 'undefined') FileExplorer.load();
          if (typeof NodeGrid !== 'undefined') NodeGrid.load();
        } else {
          throw new Error('Failed to write node file.');
        }
      } else {
        throw new Error('No HTML Neural Node returned from distillation.');
      }
    } catch (err) {
      IPCLog.append('error', `Failed to save node: ${err.message ?? err}`);
      alert(`Failed to save Neural Node: ${err.message ?? err}`);
    } finally {
      this._btnSaveNode.disabled = false;
      this._btnSaveNode.innerHTML = `<span class="btn-text">Save Selected as Neural Node</span>`;
    }
  }
};


// Helper to escape HTML characters
function escapeHtml(str) {
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}


/* ═══════════════════════════════════════════════════════════════════
   DatabaseProviderManager — Database provider CRUD, priority list, modal
   ═══════════════════════════════════════════════════════════════════ */
const DatabaseProviderManager = {
  _initialized: false,
  _providers: [],

  init() {
    this._initialized = true;
    this._list      = qs('#database-provider-list');
    this._btnAdd    = qs('#btn-add-database-provider');
    this._modal     = qs('#modal-add-database-provider');
    this._btnSave   = qs('#btn-modal-database-provider-save');
    this._btnCancel = qs('#btn-modal-database-provider-cancel');
    this._btnClose  = qs('#btn-modal-database-provider-close');

    if (this._btnAdd)    this._btnAdd.addEventListener('click',   () => this.openModal());
    if (this._btnClose)  this._btnClose.addEventListener('click',  () => this.closeModal());
    if (this._btnCancel) this._btnCancel.addEventListener('click', () => this.closeModal());
    if (this._btnSave)   this._btnSave.addEventListener('click',   () => this.saveProvider());

    if (this._modal) {
      this._modal.addEventListener('click', e => {
        if (e.target === this._modal) this.closeModal();
      });
    }

    this.loadProviders();
  },

  async loadProviders() {
    try {
      const response = await invoke('python:rpc', {
        method: 'database.get_providers',
        params: {}
      });
      if (response && response.providers) {
        this._providers = response.providers;
        this.render();
      }
    } catch (err) {
      console.warn('[DatabaseProviderManager] Failed to load database providers:', err);
    }
  },

  async persistProviders() {
    try {
      await invoke('python:rpc', {
        method: 'database.save_providers',
        params: { providers: this._providers }
      });
      IPCLog.append('ok', 'Database providers saved to PostgreSQL');
    } catch (err) {
      console.warn('[DatabaseProviderManager] Failed to persist database providers:', err);
      IPCLog.append('error', `Failed to save database providers: ${err.message ?? err}`);
    }
  },

  render() {
    if (!this._list) return;
    const empty = qs('#database-provider-list-empty');
    this._list.innerHTML = '';
    if (empty) this._list.appendChild(empty);

    if (this._providers.length === 0) {
      if (empty) empty.hidden = false;
      return;
    }
    if (empty) empty.hidden = true;

    this._providers.forEach((p, i) => {
      this._list.appendChild(this._buildCard(p, i));
    });
  },

  _dbLabel(endpoint, name) {
    const url = (endpoint || '').toLowerCase();
    const n = (name || '').toLowerCase();
    if (url.includes('postgres') || url.includes('5432') || n.includes('postgres') || n.includes('pg')) return 'POSTGRESQL';
    if (url.includes('mysql') || url.includes('3306') || n.includes('mysql')) return 'MYSQL';
    if (url.includes('sqlite') || url.includes('.db') || url.includes('.sqlite') || n.includes('sqlite')) return 'SQLITE';
    if (url.includes('mongo') || url.includes('27017') || n.includes('mongo')) return 'MONGODB';
    return 'DATABASE';
  },

  _dbColor(label) {
    switch (label) {
      case 'POSTGRESQL': return '#336791';
      case 'MYSQL':      return '#00758f';
      case 'SQLITE':     return '#003b57';
      case 'MONGODB':    return '#4db33d';
      default:           return 'var(--accent-teal)';
    }
  },

  _buildCard(p, i) {
    const total   = this._providers.length;
    const isFirst = i === 0;
    const isLast  = i === total - 1;
    const label   = this._dbLabel(p.endpoint, p.name);
    const color   = this._dbColor(label);
    
    let userMeta = p.endpoint;
    if (p.username || p.database) {
      const user = p.username ? `${p.username}@` : '';
      const dbStr = p.database ? `/${p.database}` : '';
      userMeta = `<span style="opacity:.7;">${user}</span>${p.endpoint}<span style="opacity:.7;">${dbStr}</span>`;
    }

    const card = document.createElement('div');
    card.className = 'provider-card provider-type-remote';
    card.style.borderLeftColor = color;
    card.dataset.providerId = p.id;
    card.innerHTML = `
      <div class="provider-priority-badge">${i + 1}</div>
      <div class="provider-type-chip type-storage" style="background: ${color}22; color: ${color}; border-color: ${color}44;">${label}</div>
      <div class="provider-card-info">
        <div class="provider-card-name">${p.name}</div>
        <div class="provider-card-meta">${userMeta}</div>
      </div>
      <div class="provider-status-indicator">
        <span class="status-dot status-ok"></span>
        <span class="provider-status-text">ONLINE</span>
      </div>
      <div class="provider-card-actions">
        <button class="btn-provider-action btn-database-provider-up"     title="Move Up"    ${isFirst ? 'disabled' : ''}>&#8593;</button>
        <button class="btn-provider-action btn-database-provider-down"   title="Move Down"  ${isLast  ? 'disabled' : ''}>&#8595;</button>
        <button class="btn-provider-action btn-database-provider-delete" title="Remove provider">&#x2715;</button>
      </div>`;

    if (!isFirst) card.querySelector('.btn-database-provider-up').addEventListener('click',   () => this.move(p.id, -1));
    if (!isLast)  card.querySelector('.btn-database-provider-down').addEventListener('click', () => this.move(p.id,  1));
    card.querySelector('.btn-database-provider-delete').addEventListener('click', () => this.remove(p.id));

    return card;
  },

  move(id, dir) {
    const idx = this._providers.findIndex(p => p.id === id);
    if (idx < 0) return;
    const to = idx + dir;
    if (to < 0 || to >= this._providers.length) return;
    [this._providers[idx], this._providers[to]] = [this._providers[to], this._providers[idx]];
    this.persistProviders();
    this.render();
    IPCLog.append('info', `Database provider "${this._providers[to].name}" → priority ${to + 1}`);
  },

  remove(id) {
    const idx = this._providers.findIndex(p => p.id === id);
    if (idx < 0) return;
    const name = this._providers[idx].name;
    this._providers.splice(idx, 1);
    this.persistProviders();
    this.render();
    IPCLog.append('info', `Database provider removed: "${name}"`);
  },

  openModal() {
    ['db-prov-name','db-prov-endpoint','db-prov-username','db-prov-password','db-prov-database'].forEach(id => {
      const el = qs(`#${id}`);
      if (el) { el.value = ''; el.classList.remove('input-error'); }
    });
    if (this._modal) this._modal.hidden = false;
    qs('#db-prov-name')?.focus();
  },

  closeModal() {
    if (this._modal) this._modal.hidden = true;
  },

  saveProvider() {
    const nameEl     = qs('#db-prov-name');
    const endpointEl = qs('#db-prov-endpoint');
    const usernameEl = qs('#db-prov-username');
    const passwordEl = qs('#db-prov-password');
    const databaseEl = qs('#db-prov-database');

    const name     = nameEl?.value?.trim();
    const endpoint = endpointEl?.value?.trim();
    const username = usernameEl?.value?.trim() || '';
    const password = passwordEl?.value?.trim() || '';
    const database = databaseEl?.value?.trim() || '';

    let hasError = false;
    if (!name) {
      nameEl?.classList.add('input-error'); hasError = true;
    } else { nameEl?.classList.remove('input-error'); }

    if (!endpoint) {
      endpointEl?.classList.add('input-error'); hasError = true;
    } else { endpointEl?.classList.remove('input-error'); }

    if (hasError) return;

    this._providers.push({
      id: `dbprov-${Date.now()}`,
      name,
      endpoint,
      username,
      password,
      database
    });
    this.persistProviders();
    this.render();
    this.closeModal();
    IPCLog.append('info', `Database provider added: "${name}" (${this._dbLabel(endpoint, name)}) → priority ${this._providers.length}`);
  }
};


/* ═══════════════════════════════════════════════════════════════════
   SearchProviderManager — search provider CRUD, priority list, modal
   ═══════════════════════════════════════════════════════════════════ */
const SearchProviderManager = {
  _initialized: false,
  _providers: [],


  init() {
    this._initialized = true;
    this._list         = qs('#search-provider-list');
    this._btnAdd       = qs('#btn-add-search-provider');
    this._modal        = qs('#modal-add-search-provider');
    this._btnSave      = qs('#btn-modal-search-provider-save');
    this._btnCancel    = qs('#btn-modal-search-provider-cancel');
    this._btnClose     = qs('#btn-modal-search-provider-close');

    if (this._btnAdd)    this._btnAdd.addEventListener('click',   () => this.openModal());
    if (this._btnClose)  this._btnClose.addEventListener('click',  () => this.closeModal());
    if (this._btnCancel) this._btnCancel.addEventListener('click', () => this.closeModal());
    if (this._btnSave)   this._btnSave.addEventListener('click',   () => this.saveProvider());

    if (this._modal) {
      this._modal.addEventListener('click', e => {
        if (e.target === this._modal) this.closeModal();
      });
    }

    this.loadProviders();
  },

  async loadProviders() {
    try {
      const response = await invoke('python:rpc', {
        method: 'search.get_providers',
        params: {}
      });
      if (response && response.providers) {
        this._providers = response.providers;
        this.render();
      }
    } catch (err) {
      console.warn('[SearchProviderManager] Failed to load search providers:', err);
    }
  },

  async persistProviders() {
    try {
      await invoke('python:rpc', {
        method: 'search.save_providers',
        params: { providers: this._providers }
      });
      IPCLog.append('ok', 'Search providers saved to PostgreSQL');
    } catch (err) {
      console.warn('[SearchProviderManager] Failed to persist search providers:', err);
      IPCLog.append('error', `Failed to save search providers: ${err.message ?? err}`);
    }
  },

  render() {
    if (!this._list) return;

    // Clear all but empty-state
    const empty = qs('#search-provider-list-empty');
    this._list.innerHTML = '';
    if (empty) this._list.appendChild(empty);

    if (this._providers.length === 0) {
      if (empty) empty.hidden = false;
      return;
    }
    if (empty) empty.hidden = true;

    this._providers.forEach((p, i) => {
      this._list.appendChild(this._buildCard(p, i));
    });
  },

  _buildCard(p, i) {
    const total     = this._providers.length;
    const isFirst   = i === 0;
    const isLast    = i === total - 1;

    const isForced = !!p.locked;
    const card = document.createElement('div');
    card.className = 'provider-card provider-type-remote';
    card.dataset.providerId = p.id;
    card.innerHTML = `
      <div class="provider-priority-badge">${i + 1}</div>
      <div class="provider-type-chip type-remote">SEARCH</div>
      <div class="provider-card-info">
        <div class="provider-card-name">${p.name}</div>
        <div class="provider-card-meta">${p.endpoint}</div>
      </div>
      <label class="provider-force-checkbox-container ${isForced ? 'forced' : ''}" title="Force use this search provider (bypasses priority order and fallback direct crawl)">
        <input type="checkbox" class="provider-force-checkbox" ${isForced ? 'checked' : ''} />
        <span class="provider-force-text">FORCE</span>
      </label>
      <div class="provider-status-indicator">
        <span class="status-dot status-ok"></span>
        <span class="provider-status-text">ACTIVE</span>
      </div>
      <div class="provider-card-actions">
        <button class="btn-provider-action btn-search-provider-up"   title="Move Up"   ${isFirst ? 'disabled' : ''}>↑</button>
        <button class="btn-provider-action btn-search-provider-down" title="Move Down" ${isLast  ? 'disabled' : ''}>↓</button>
        <button class="btn-provider-action btn-search-provider-delete" title="Remove provider">✕</button>
      </div>`;

    if (!isFirst) card.querySelector('.btn-search-provider-up').addEventListener('click', () => this.move(p.id, -1));
    if (!isLast)  card.querySelector('.btn-search-provider-down').addEventListener('click', () => this.move(p.id, 1));
    card.querySelector('.btn-search-provider-delete').addEventListener('click', () => this.remove(p.id));

    const forceCheckbox = card.querySelector('.provider-force-checkbox');
    if (forceCheckbox) {
      forceCheckbox.addEventListener('change', (e) => {
        const checked = e.target.checked;
        this._providers.forEach(prov => {
          if (prov.id === p.id) {
            prov.locked = checked;
          } else {
            prov.locked = false;
          }
        });
        this.persistProviders();
        this.render();
      });
    }

    return card;
  },

  move(id, dir) {
    const idx = this._providers.findIndex(p => p.id === id);
    if (idx < 0) return;
    const to = idx + dir;
    if (to < 0 || to >= this._providers.length) return;
    [this._providers[idx], this._providers[to]] = [this._providers[to], this._providers[idx]];
    this.persistProviders();
    this.render();
    IPCLog.append('info', `Search provider "${this._providers[to].name}" → priority ${to + 1}`);
  },

  remove(id) {
    const idx = this._providers.findIndex(p => p.id === id);
    if (idx < 0) return;
    const name = this._providers[idx].name;
    this._providers.splice(idx, 1);
    this.persistProviders();
    this.render();
    IPCLog.append('info', `Search provider removed: "${name}"`);
  },

  openModal() {
    const nameEl = qs('#search-prov-name');
    const endpointEl = qs('#search-prov-endpoint');
    const apikeyEl = qs('#search-prov-apikey');
    if (nameEl) nameEl.value = '';
    if (endpointEl) endpointEl.value = '';
    if (apikeyEl) apikeyEl.value = '';
    if (nameEl) nameEl.classList.remove('input-error');
    if (endpointEl) endpointEl.classList.remove('input-error');
    if (this._modal) this._modal.hidden = false;
  },

  closeModal() {
    if (this._modal) this._modal.hidden = true;
  },

  saveProvider() {
    const nameEl = qs('#search-prov-name');
    const endpointEl = qs('#search-prov-endpoint');
    const apikeyEl = qs('#search-prov-apikey');

    const name = nameEl?.value?.trim();
    const endpoint = endpointEl?.value?.trim();
    const apiKey = apikeyEl?.value?.trim() || '';

    let hasError = false;
    if (!name) {
      if (nameEl) nameEl.classList.add('input-error');
      hasError = true;
    } else {
      if (nameEl) nameEl.classList.remove('input-error');
    }

    if (!endpoint) {
      if (endpointEl) endpointEl.classList.add('input-error');
      hasError = true;
    } else {
      if (endpointEl) endpointEl.classList.remove('input-error');
    }

    if (hasError) return;

    const provider = {
      id: `sprov-${Date.now()}`,
      name,
      endpoint,
      api_key: apiKey,
      locked: false,
    };

    this._providers.push(provider);
    this.persistProviders();
    this.render();
    this.closeModal();
    IPCLog.append('info', `Search provider added: "${name}" → priority ${this._providers.length}`);
  }
};



/* ═══════════════════════════════════════════════════════════════════
   AgentManager — workforce persona CRUD, settings tab logic
   ═══════════════════════════════════════════════════════════════════ */
const AgentManager = {
  _initialized: false,
  _agents: [],

  init() {
    this._initialized = true;
    this._list = qs('#agent-list');
    this._form = qs('#agent-form');
    this._idInput = qs('#agent-id-input');
    this._nameInput = qs('#agent-name-input');
    this._typeSelect = qs('#agent-type-select');
    this._roleInput = qs('#agent-role-input');
    this._crewTypeSelect = qs('#agent-crew-type-select');
    this._swarmSizeInput = qs('#agent-swarm-size-input');
    this._systemPromptInput = qs('#agent-system-prompt');
    this._birthdateInput = qs('#agent-birthdate-input');
    this._backgroundInput = qs('#agent-background-input');
    this._tagInput = qs('#agent-tag-input');
    this._btnReset = qs('#btn-agent-reset');
    this._btnSave = qs('#btn-agent-save');
    this._editorTitle = qs('#editor-title');
    this._editorModeBadge = qs('#editor-mode-badge');

    // Dropdown change listener to show/hide conditional panels
    if (this._typeSelect) {
      this._typeSelect.addEventListener('change', () => this.updateConditionalFields());
    }

    // Submit handler
    if (this._form) {
      this._form.addEventListener('submit', (e) => this.saveAgent(e));
    }

    // Reset button
    if (this._btnReset) {
      this._btnReset.addEventListener('click', () => this.resetForm());
    }

    // Load agents
    this.load();
  },

  updateConditionalFields() {
    const type = this._typeSelect ? this._typeSelect.value : 'standalone';
    const crewWrapper = qs('#crew-fields-wrapper');
    const swarmWrapper = qs('#swarm-fields-wrapper');

    if (crewWrapper) crewWrapper.style.display = type === 'crew' ? 'block' : 'none';
    if (swarmWrapper) swarmWrapper.style.display = type === 'swarm' ? 'block' : 'none';
  },

  async load() {
    try {
      const result = await invoke('python:rpc', {
        method: 'agent.get_agents',
        params: {}
      });
      if (result && result.agents) {
        this._agents = result.agents;
        this.render();
      }
    } catch (err) {
      console.error('Failed to load agents:', err);
      IPCLog.append('error', `Failed to load agents: ${err.message}`);
    }
  },

  render() {
    if (!this._list) return;
    this._list.innerHTML = '';

    if (this._agents.length === 0) {
      this._list.innerHTML = `
        <div style="text-align: center; padding: var(--space-6); color: var(--text-muted);">
          No personas configured yet. Use the form on the right to create one.
        </div>`;
      return;
    }

    this._agents.forEach(agent => {
      const card = document.createElement('div');
      card.className = `agent-card${agent.is_active ? ' active' : ''}`;
      card.dataset.id = agent.id;

      // Badges
      let badgeHtml = '';
      if (agent.type === 'standalone') {
        badgeHtml = `<span class="badge-agent-type badge-agent-standalone">standalone</span>`;
      } else if (agent.type === 'swarm') {
        const size = agent.config ? (agent.config.swarm_size || 3) : 3;
        badgeHtml = `<span class="badge-agent-type badge-agent-swarm">swarm (${size})</span>`;
      } else if (agent.type === 'crew') {
        const cat = (agent.crew_type || 'business').toUpperCase();
        badgeHtml = `
          <span class="badge-agent-type badge-agent-crew">crew</span>
          <span class="badge-crew-detail">${cat}</span>
        `;
      }

      // Hide delete action for tala-core
      const isSystemAgent = agent.id === 'tala-core';
      const deleteButtonHtml = isSystemAgent
        ? ''
        : `<button class="btn-card-action btn-card-delete" title="Delete Persona">
             <svg viewBox="0 0 20 20" fill="currentColor" width="14" height="14">
               <path fill-rule="evenodd" d="M9 2a1 1 0 00-.894.553L7.382 4H4a1 1 0 000 2v10a2 2 0 002 2h8a2 2 0 002-2V6a1 1 0 100-2h-3.382l-.724-1.447A1 1 0 0011 2H9zM7 8a1 1 0 012 0v6a1 1 0 11-2 0V8zm5-1a1 1 0 00-1 1v6a1 1 0 102 0V8a1 1 0 00-1-1z" clip-rule="evenodd" />
             </svg>
           </button>`;

      const birth = agent.config ? agent.config.birthdate : null;
      const bg = agent.config ? agent.config.background_file : null;
      
      let extraMeta = '';
      if (birth) {
        extraMeta += `<span class="badge-crew-detail" style="border-color: rgba(56, 189, 248, 0.2); color: var(--accent-blue); padding: 2px 6px; border-radius: 4px; font-size: 0.65rem;" title="Birthdate: ${birth}">📅 ${birth}</span>`;
      }
      if (bg) {
        const parts = bg.split('/');
        const filename = parts[parts.length - 1];
        extraMeta += `<span class="badge-crew-detail" style="border-color: rgba(45, 212, 191, 0.2); color: var(--accent-teal); padding: 2px 6px; border-radius: 4px; font-size: 0.65rem;" title="Background Node: ${bg}">📖 ${filename}</span>`;
      }
      const tag = agent.config ? agent.config.tag : null;
      if (tag) {
        extraMeta += `<span class="badge-crew-detail" style="border-color: rgba(239, 68, 68, 0.2); color: var(--accent-red); padding: 2px 6px; border-radius: 4px; font-size: 0.65rem;" title="Knowledge Tag: ${tag}">🏷️ ${tag}</span>`;
      }

      card.innerHTML = `
        <div class="agent-card-header">
          <div class="agent-card-title-row">
            <div class="agent-active-checkbox${agent.is_active ? ' checked' : ''}" title="${agent.is_active ? 'Active Persona' : 'Click to activate'}"></div>
            <span class="agent-card-name">${agent.name}</span>
          </div>
          <div class="agent-card-actions">
            <button class="btn-card-action btn-card-edit" title="Edit Persona">
              <svg viewBox="0 0 20 20" fill="currentColor" width="14" height="14">
                <path d="M13.586 3.586a2 2 0 112.828 2.828l-.793.793-2.828-2.828.793-.793zM11.379 5.793L3 14.172V17h2.828l8.38-8.379-2.83-2.829z" />
              </svg>
            </button>
            ${deleteButtonHtml}
          </div>
        </div>
        <div class="agent-card-body">${agent.config ? (agent.config.system_prompt || '') : ''}</div>
        <div class="agent-card-footer">
          <div style="display: flex; gap: var(--space-2); align-items: center; flex-wrap: wrap;">
            ${badgeHtml}
            ${extraMeta}
          </div>
          ${agent.role ? `<span class="mono" style="font-size: 0.65rem;">Role: ${agent.role}</span>` : ''}
        </div>
      `;

      // Event: Active checkbox click
      const checkbox = card.querySelector('.agent-active-checkbox');
      if (checkbox) {
        checkbox.addEventListener('click', (e) => {
          e.stopPropagation();
          this.setActive(agent.id);
        });
      }

      // Event: Edit click
      const btnEdit = card.querySelector('.btn-card-edit');
      if (btnEdit) {
        btnEdit.addEventListener('click', (e) => {
          e.stopPropagation();
          this.editAgent(agent);
        });
      }

      // Event: Delete click
      const btnDelete = card.querySelector('.btn-card-delete');
      if (btnDelete) {
        btnDelete.addEventListener('click', (e) => {
          e.stopPropagation();
          this.deleteAgent(agent.id);
        });
      }

      this._list.appendChild(card);
    });
  },

  async setActive(id) {
    try {
      const result = await invoke('python:rpc', {
        method: 'agent.set_active',
        params: { id }
      });
      if (result && result.success) {
        IPCLog.append('info', `Activated persona: "${id}"`);
        await this.load();
        // Immediately poll health to refresh status bar
        if (typeof TelemetryPoller._poll === 'function') {
          await TelemetryPoller._poll();
        }
      }
    } catch (err) {
      console.error('Failed to set active agent:', err);
      IPCLog.append('error', `Failed to activate persona: ${err.message}`);
    }
  },

  editAgent(agent) {
    if (!this._form) return;
    
    this._idInput.value = agent.id;
    this._nameInput.value = agent.name;
    this._typeSelect.value = agent.type;
    this._roleInput.value = agent.role || '';
    this._crewTypeSelect.value = agent.crew_type || 'business';
    
    const swarmSize = agent.config ? (agent.config.swarm_size || 3) : 3;
    this._swarmSizeInput.value = swarmSize;
    
    this._systemPromptInput.value = agent.config ? (agent.config.system_prompt || '') : '';
    
    const birthdate = agent.config ? (agent.config.birthdate || '') : '';
    const backgroundFile = agent.config ? (agent.config.background_file || '') : '';
    const tag = agent.config ? (agent.config.tag || '') : '';
    if (this._birthdateInput) this._birthdateInput.value = birthdate;
    if (this._backgroundInput) this._backgroundInput.value = backgroundFile;
    if (this._tagInput) this._tagInput.value = tag;
    
    this.updateConditionalFields();
    
    if (this._editorTitle) this._editorTitle.textContent = 'EDIT PERSONA';
    if (this._editorModeBadge) {
      this._editorModeBadge.textContent = 'EDIT';
      this._editorModeBadge.style.background = 'rgba(251, 191, 36, 0.1)';
      this._editorModeBadge.style.color = '#fbbf24';
      this._editorModeBadge.style.borderColor = 'rgba(251, 191, 36, 0.3)';
    }
  },

  async deleteAgent(id) {
    if (!confirm('Are you sure you want to delete this persona?')) return;
    try {
      const result = await invoke('python:rpc', {
        method: 'agent.delete_agent',
        params: { id }
      });
      if (result && result.success) {
        IPCLog.append('info', `Deleted persona: "${id}"`);
        this.resetForm();
        await this.load();
        if (typeof TelemetryPoller._poll === 'function') {
          await TelemetryPoller._poll();
        }
      }
    } catch (err) {
      console.error('Failed to delete agent:', err);
      IPCLog.append('error', `Failed to delete persona: ${err.message}`);
    }
  },

  resetForm() {
    if (this._form) this._form.reset();
    if (this._idInput) this._idInput.value = '';
    if (this._birthdateInput) this._birthdateInput.value = '';
    if (this._backgroundInput) this._backgroundInput.value = '';
    if (this._tagInput) this._tagInput.value = '';
    this.updateConditionalFields();
    
    if (this._editorTitle) this._editorTitle.textContent = 'CREATE PERSONA';
    if (this._editorModeBadge) {
      this._editorModeBadge.textContent = 'NEW';
      this._editorModeBadge.style.background = 'rgba(0, 242, 254, 0.1)';
      this._editorModeBadge.style.color = '#00f2fe';
      this._editorModeBadge.style.borderColor = 'rgba(0, 242, 254, 0.3)';
    }
  },

  async saveAgent(e) {
    if (e) e.preventDefault();
    
    const id = this._idInput.value;
    const name = this._nameInput.value;
    const type = this._typeSelect.value;
    const role = type === 'crew' ? this._roleInput.value : '';
    const crewType = type === 'crew' ? this._crewTypeSelect.value : '';
    const systemPrompt = this._systemPromptInput.value;
    const birthdate = this._birthdateInput ? this._birthdateInput.value : '';
    const backgroundFile = this._backgroundInput ? this._backgroundInput.value : '';
    const tag = this._tagInput ? this._tagInput.value.trim() : '';
    
    const config = {
      system_prompt: systemPrompt
    };
    if (type === 'swarm') {
      config.swarm_size = parseInt(this._swarmSizeInput.value, 10) || 3;
    }
    if (birthdate) config.birthdate = birthdate;
    if (backgroundFile) config.background_file = backgroundFile;
    if (tag) config.tag = tag;
    
    try {
      const result = await invoke('python:rpc', {
        method: 'agent.save_agent',
        params: {
          id: id || undefined,
          name,
          type,
          role,
          crew_type: crewType,
          config
        }
      });
      
      if (result && result.success) {
        IPCLog.append('info', `Saved persona: "${name}"`);
        this.resetForm();
        await this.load();
      }
    } catch (err) {
      console.error('Failed to save agent:', err);
      IPCLog.append('error', `Failed to save persona: ${err.message}`);
    }
  }
};

;

// parseThinkingBlocks is defined in thinking_blocks.js, loaded via a plain
// <script> tag in index.html before this module -- kept in a small,
// DOM-free, non-module file specifically so it's unit-testable in
// Jest/Node without dragging in this whole browser-only file. See that
// file for the regression it fixes (a second <thinking> block in the same
// message used to leak into the visible chat bubble as raw text).

const AgentChat = {
  PEER_MENTION_RE: /^@([A-Za-z0-9_.-]+)\s+([\s\S]+)$/,

  init() {
    this._sessionId = localStorage.getItem('tala.chat.session_id') || `chat-${Date.now()}`;
    this._providerId = localStorage.getItem('tala.chat.provider_id') || '';
    localStorage.setItem('tala.chat.session_id', this._sessionId);
    this._messages = qs('#chat-messages');
    this._textarea = qs('#chat-textarea');
    this._visionProviderId = localStorage.getItem('tala.chat.vision_provider_id') || '';
    
    this.loadHistory();
    this.renderSessionSelect();

    const btnNew = qs('#btn-chat-new');
    if (btnNew) {
      btnNew.addEventListener('click', () => {
        this.startNewChat();
      });
    }

    const sessionSelect = qs('#chat-session-select');
    if (sessionSelect) {
      sessionSelect.addEventListener('change', () => {
        const targetId = sessionSelect.value;
        if (targetId && targetId !== this._sessionId) {
          this.switchSession(targetId);
        }
      });
    }

    this._btnScreenshot = qs('#btn-chat-screenshot');
    if (this._btnScreenshot) {
      this._btnScreenshot.addEventListener('click', () => {
        this.captureScreenContext();
      });
    }

    if (this._textarea) {
      this._textarea.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && !e.shiftKey) {
          e.preventDefault();
          this.sendMessage();
        }
      });
    }

    const actionBtns = document.querySelectorAll('.chat-action-btn');
    actionBtns.forEach(btn => {
      if (['btn-chat-rp', 'btn-chat-hybrid', 'btn-chat-assistant'].includes(btn.id)) {
        return;
      }
      btn.addEventListener('click', () => {
        btn.classList.toggle('active');
        const state = btn.classList.contains('active') ? 'ENABLED' : 'DISABLED';
        IPCLog.append('info', `Agent Chat modifier '${btn.textContent}' set to ${state}`);
      });
    });

    // Segmented Mode Toggles
    const modeBtns = {
      rp: qs('#btn-chat-rp'),
      hybrid: qs('#btn-chat-hybrid'),
      assistant: qs('#btn-chat-assistant')
    };

    const setUIMode = (activeMode) => {
      Object.entries(modeBtns).forEach(([mode, btn]) => {
        if (btn) {
          if (mode === activeMode) {
            btn.classList.add('active');
          } else {
            btn.classList.remove('active');
          }
        }
      });

      const initMsg = qs('#chat-initial-message');
      if (initMsg) {
        if (activeMode === 'rp') {
          initMsg.innerHTML = `*(A quiet hum fills the cabin as I finish checking the primary console interfaces. I turn from the workbench and look over.)* Hey, Weavor. Main systems are stable and the ship's logs are current. What's the priority? Sector charts, system diagnostics, or something else?`;
        } else if (activeMode === 'hybrid') {
          initMsg.innerHTML = `*(I finish locking down the engineering diagnostics panel and slide my mobiGlas active terminal into view.)* Ready to go, Weavor. I've got the ship's consoles connected to the local development environment and workspace assets. Let's get to work.`;
        } else {
          initMsg.innerHTML = `Hello! I am Tala, your workspace and engineering companion. I'm ready to assist with your repository, blockchain nodes, workflows, or query our knowledge graph. What's our focus today?`;
        }
      }
    };

    Object.entries(modeBtns).forEach(([mode, btn]) => {
      if (btn) {
        btn.addEventListener('click', async () => {
          this._history = [];
          if (typeof this.saveHistory === 'function') this.saveHistory();
          setUIMode(mode);
          IPCLog.append('info', `Switching chat to ${mode.toUpperCase()} mode...`);
          try {
            const result = await invoke('python:rpc', {
              method: 'agent.set_chat_mode',
              params: { mode }
            });
            if (result && result.success) {
              IPCLog.append('ok', `Chat mode successfully set to ${mode.toUpperCase()}`);
              if (typeof TelemetryPoller._poll === 'function') {
                await TelemetryPoller._poll();
              }
            } else {
              const errMsg = result && result.error ? result.error : 'unknown error';
              IPCLog.append('error', `Failed to set chat mode: ${errMsg}`);
            }
          } catch (err) {
            IPCLog.append('error', `Failed to set chat mode: ${err.message}`);
          }
        });
      }
    });

    // Query active chat mode on startup to sync UI
    invoke('python:rpc', { method: 'agent.get_chat_mode' }).then(result => {
      if (result && result.mode) {
        setUIMode(result.mode);
        IPCLog.append('info', `Active Chat Mode initialized: ${result.mode.toUpperCase()}`);
      }
    }).catch(err => {
      console.warn('[AgentChat] Failed to retrieve chat mode on startup:', err);
    });

    // Listen for live task telemetry status updates and render in Chat window
    if (window.tala && typeof window.tala.on === 'function') {
      window.tala.on('python:notification', (msg) => {
        if (!msg) return;
        const method = msg.method;
        const params = msg.params || {};

        if (method === 'task.live_status' || params.type === 'task_status_update') {
          const phase = params.phase || params.message || '';
          const status = params.status || 'in_progress';
          const message = params.message || '';
          const details = params.details || {};
          const actionType = details.action_type || params.action_type || '';
          const target = details.target || params.target || '';

          if (!this._activeTaskState || (params.task_id && this._activeTaskState.taskId !== params.task_id)) {
            if (this._activeTaskTimerInterval) {
              clearInterval(this._activeTaskTimerInterval);
            }
            this._activeTaskState = {
              taskId: params.task_id || ('task-' + Date.now()),
              startTime: Date.now(),
              filesExplored: 0,
              searches: 0,
              filesEdited: 0,
              commands: 0,
              steps: []
            };
          }

          const state = this._activeTaskState;

          // Track action types & metrics
          if (actionType === 'read_file' || actionType === 'explore') {
            state.filesExplored++;
            state.steps.push({ icon: '📄', text: `Explored file: ${target || message}` });
          } else if (actionType === 'search_workspace' || actionType === 'search') {
            state.searches++;
            state.steps.push({ icon: '🔍', text: `Search: "${target || message}"` });
          } else if (actionType === 'edit_file' || actionType === 'write_file') {
            state.filesEdited++;
            state.steps.push({ icon: '✏️', text: `Edited file: ${target || message}` });
          } else if (actionType === 'run_command' || actionType === 'command') {
            state.commands++;
            state.steps.push({ icon: '⚡', text: `Executed command: ${target || message}` });
          } else if (message || phase) {
            state.steps.push({ icon: '💭', text: `${phase ? phase + ': ' : ''}${message}` });
          }

          let statusBubble = document.getElementById('chat-task-status-bubble');
          if (!statusBubble) {
            statusBubble = document.createElement('div');
            statusBubble.id = 'chat-task-status-bubble';
            statusBubble.className = 'chat-message agent';
            if (this._messages) this._messages.appendChild(statusBubble);
          }

          const updateBubbleContent = () => {
            const elapsedSec = Math.floor((Date.now() - state.startTime) / 1000);
            const timeStr = typeof fmtTime === 'function' ? fmtTime() : new Date().toLocaleTimeString();

            let metricsStr = `⏱️ Worked for ${elapsedSec}s`;
            const metricParts = [];
            if (state.filesExplored > 0) metricParts.push(`Explored ${state.filesExplored} file${state.filesExplored > 1 ? 's' : ''}`);
            if (state.searches > 0) metricParts.push(`${state.searches} search${state.searches > 1 ? 'es' : ''}`);
            if (state.filesEdited > 0) metricParts.push(`Edited ${state.filesEdited} file${state.filesEdited > 1 ? 's' : ''}`);
            if (state.commands > 0) metricParts.push(`Ran ${state.commands} command${state.commands > 1 ? 's' : ''}`);
            
            if (metricParts.length > 0) {
              metricsStr += ' • ' + metricParts.join(' • ');
            }

            const latestStep = state.steps[state.steps.length - 1];
            const latestMsg = latestStep ? `${latestStep.icon} ${latestStep.text}` : (message || phase);

            const isComplete = (status === 'complete' || status === 'completed');
            const isFailed = (status === 'failed');
            const statusBadge = isComplete ? '✅ COMPLETE' : (isFailed ? '❌ FAILED' : '⚡ IN PROGRESS');
            const statusColor = isComplete ? '#4ade80' : (isFailed ? '#f87171' : '#38bdf8');

            const stepsHtml = state.steps.map(s => `<div style="margin-bottom:3px; opacity:0.9;">${s.icon} ${s.text}</div>`).join('');

            statusBubble.innerHTML = `
              <div class="message-meta">Tala • ${timeStr} [Live Task Thought & Execution]</div>
              <div class="message-content" style="border-left: 3px solid ${statusColor}; padding: 8px 12px; background: rgba(15, 23, 42, 0.6); border-radius: 6px; font-family: system-ui, -apple-system, sans-serif; font-size: 0.85rem;">
                <div style="font-weight: 600; color: ${statusColor}; margin-bottom: 4px;">
                  ${metricsStr} <span style="font-size: 0.75rem; float: right; opacity: 0.8;">${statusBadge}</span>
                </div>
                <div style="font-size: 0.8rem; color: #cbd5e1; margin-bottom: 6px;">
                  ${latestMsg}
                </div>
                <details style="margin-top: 6px; border-top: 1px solid rgba(255,255,255,0.1); padding-top: 4px;">
                  <summary style="cursor: pointer; color: #38bdf8; font-size: 0.75rem; user-select: none;">
                    ▶ Activity Stream (${state.steps.length} steps)
                  </summary>
                  <div style="margin-top: 6px; max-height: 160px; overflow-y: auto; font-family: monospace; font-size: 0.75rem; background: rgba(0,0,0,0.3); padding: 6px; border-radius: 4px;">
                    ${stepsHtml}
                  </div>
                </details>
              </div>
            `;
            if (this._messages) this._messages.scrollTop = this._messages.scrollHeight;
          };

          updateBubbleContent();

          if (this._activeTaskTimerInterval) {
            clearInterval(this._activeTaskTimerInterval);
          }

          if (status !== 'complete' && status !== 'completed' && status !== 'failed') {
            this._activeTaskTimerInterval = setInterval(() => {
              updateBubbleContent();
            }, 1000);
          } else {
            statusBubble.id = '';
            this._activeTaskState = null;
          }
        }
      });
    }
  },

  parsePeerMention(text) {
    const match = this.PEER_MENTION_RE.exec(text.trim());
    if (!match) return null;
    return {
      username: `@${match[1]}`,
      message: match[2].trim(),
    };
  },

  async fetchContacts() {
    try {
      const response = await invoke('python:rpc', {
        method: 'contacts.list',
        params: {}
      });
      return Array.isArray(response?.contacts) ? response.contacts : [];
    } catch (err) {
      console.warn('[AgentChat] Failed to fetch contacts for mention routing:', err);
      return [];
    }
  },

  async resolvePeerContact(username) {
    const target = String(username || '').trim().toLowerCase();
    if (!target) return null;
    const contacts = await this.fetchContacts();
    return contacts.find(contact => String(contact.username || '').trim().toLowerCase() === target) || null;
  },

  async sendPeerMessage(contact, message) {
    const endpoint = String(contact?.endpoint_url || '').trim();
    if (!endpoint) {
      throw new Error(`Contact ${contact?.username || 'peer'} does not have an endpoint yet.`);
    }

    const response = await invoke('python:rpc', {
      method: 'p2p.send_message',
      params: {
        username: contact.username,
        endpoint_url: endpoint,
        message,
      }
    });

    if (!response || response.__rpcError) {
      throw new Error(response?.error || 'Failed to send peer message.');
    }

    return response;
  },

  isUndoableCommand(command) {
    return [
      'goal',
      'queue',
      'phase',
      'clear',
      'shell',
      'fix',
      'test',
      'generate-doc-nodes',
      'refresh-nodes',
      'self-improve'
    ].includes(command);
  },

  async prepareUndoCheckpoint(action) {
    if (!action || !this.isUndoableCommand(action.command)) return null;
    const checkpointName = 'undo-last-command';
    const response = await invoke('python:rpc', {
      method: 'repo.checkpoint',
      params: { checkpoint_name: checkpointName }
    });
    if (!response || !response.success) {
      throw new Error('Could not create undo checkpoint');
    }
    const undoState = {
      checkpoint_id: response.checkpoint_id || checkpointName,
      command: action.command,
      raw_input: action.raw_input,
      created_at: new Date().toISOString()
    };
    localStorage.setItem('tala.chat.undo_checkpoint', JSON.stringify(undoState));
    IPCLog.append('ok', `Undo checkpoint ready for /${action.command}`);
    return undoState;
  },

  consumeUndoCheckpoint() {
    const raw = localStorage.getItem('tala.chat.undo_checkpoint');
    localStorage.removeItem('tala.chat.undo_checkpoint');
    if (!raw) return null;
    try {
      return JSON.parse(raw);
    } catch (_) {
      return null;
    }
  },

  loadHistory() {
    if (!this._messages) return;
    
    // Remove all existing messages except the initial message
    const existing = this._messages.querySelectorAll('.chat-message:not(#chat-initial-message)');
    existing.forEach(el => this._messages.removeChild(el));

    this._history = [];
    const saved = localStorage.getItem(`tala.chat.history_data.${this._sessionId}`);
    if (saved) {
      try {
        this._history = JSON.parse(saved);
        this._history.forEach(msg => {
          this.appendMessage(msg.role, msg.role === 'user' ? 'You' : 'Tala', msg.content);
        });
      } catch (e) {
        console.warn('Failed to parse chat history', e);
        this._history = [];
      }
    }
  },

  saveHistory() {
    localStorage.setItem(`tala.chat.history_data.${this._sessionId}`, JSON.stringify(this._history));
    let title = '';
    if (Array.isArray(this._history) && this._history.length > 0) {
      const firstUser = this._history.find(m => m.role === 'user');
      if (firstUser && firstUser.content) {
        title = firstUser.content.slice(0, 26).trim() + (firstUser.content.length > 26 ? '...' : '');
      }
    }
    this.registerSession(this._sessionId, title);
  },

  registerSession(sessionId, title) {
    if (!sessionId) return;
    let list = [];
    try {
      list = JSON.parse(localStorage.getItem('tala.chat.sessions_list') || '[]');
    } catch (_) { list = []; }

    let existing = list.find(s => s.id === sessionId);
    if (existing) {
      if (title) existing.title = title;
      existing.updatedAt = new Date().toISOString();
    } else {
      list.unshift({
        id: sessionId,
        title: title || `Chat ${new Date().toLocaleTimeString([], {hour: '2-digit', minute:'2-digit'})}`,
        updatedAt: new Date().toISOString()
      });
    }
    if (list.length > 50) list = list.slice(0, 50);
    localStorage.setItem('tala.chat.sessions_list', JSON.stringify(list));
    this.renderSessionSelect();
  },

  renderSessionSelect() {
    const selectEl = qs('#chat-session-select');
    if (!selectEl) return;
    let list = [];
    try {
      list = JSON.parse(localStorage.getItem('tala.chat.sessions_list') || '[]');
    } catch (_) { list = []; }

    selectEl.innerHTML = '<option value="" disabled>History</option>';
    list.forEach(s => {
      const opt = document.createElement('option');
      opt.value = s.id;
      opt.textContent = s.title || s.id;
      if (s.id === this._sessionId) {
        opt.selected = true;
      }
      selectEl.appendChild(opt);
    });
  },

  switchSession(targetId) {
    if (Array.isArray(this._history) && this._history.length > 0) {
      this.saveHistory();
    }
    this._sessionId = targetId;
    localStorage.setItem('tala.chat.session_id', this._sessionId);
    this.loadHistory();
    this.renderSessionSelect();
    IPCLog.append('ok', `Restored chat session: ${targetId}`);
  },

  startNewChat() {
    if (Array.isArray(this._history) && this._history.length > 0) {
      this.saveHistory();
    }
    this._sessionId = `chat-${Date.now()}`;
    localStorage.setItem('tala.chat.session_id', this._sessionId);
    this._history = [];
    this.saveHistory();
    this.loadHistory();
    this.renderSessionSelect();
    IPCLog.append('ok', 'Started fresh zero-context chat session.');
  },

  async sendMessage() {
    if (!this._textarea || !this._messages) return;
    const text = this._textarea.value.trim();
    if (!text) return;

    this.appendMessage('user', 'You', text);
    this._textarea.value = '';

    const peerMention = this.parsePeerMention(text);
    if (peerMention) {
      const loadingId = 'chat-loading-' + Date.now();
      const loadingBubble = document.createElement('div');
      loadingBubble.className = 'chat-message agent';
      loadingBubble.id = loadingId;
      loadingBubble.innerHTML = `
        <div class="message-meta">Tala • ${fmtTime()}</div>
        <div class="message-content">Looking up ${peerMention.username} in contacts...</div>
      `;
      this._messages.appendChild(loadingBubble);
      this._messages.scrollTop = this._messages.scrollHeight;

      try {
        const contact = await this.resolvePeerContact(peerMention.username);
        const currentLoading = document.getElementById(loadingId);
        if (currentLoading) this._messages.removeChild(currentLoading);

        if (!contact) {
          this.appendMessage('agent', 'Tala', `I couldn't find ${peerMention.username} in Contacts. Add the peer first, then try the message again.`, true);
          return;
        }

        const routeLabel = contact.endpoint_url ? `${contact.username} @ ${contact.endpoint_url}` : contact.username;
        this.appendMessage('agent', 'Tala', `Routing directly to ${routeLabel}...`);

        const result = await this.sendPeerMessage(contact, peerMention.message);
        const peerName = contact.username || peerMention.username;
        if (result?.response) {
          this.appendMessage('agent', peerName, JSON.stringify(result.response, null, 2));
        } else {
          this.appendMessage('agent', 'Tala', `Message sent to ${peerName}.`, false);
        }
      } catch (err) {
        const currentLoading = document.getElementById(loadingId);
        if (currentLoading) this._messages.removeChild(currentLoading);
        this.appendMessage('agent', 'Tala', `Failed to send peer message: ${err.message || err}`, true);
      }
      return;
    }

    // Handle Slash Commands
    if (text.startsWith('/')) {
      const loadingId = 'chat-loading-' + Date.now();
      const loadingBubble = document.createElement('div');
      loadingBubble.className = 'chat-message agent';
      loadingBubble.id = loadingId;
      loadingBubble.innerHTML = `
        <div class="message-meta">Tala • ${fmtTime()}</div>
        <div class="message-content">Parsing command...</div>
      `;
      this._messages.appendChild(loadingBubble);
      this._messages.scrollTop = this._messages.scrollHeight;

      const removeLoading = () => {
        const el = document.getElementById(loadingId);
        if (el) this._messages.removeChild(el);
      };

      try {
        const parseResult = await invoke('python:rpc', { method: 'command.parse', params: { raw_input: text } });
        if (!parseResult || !parseResult.success) {
          removeLoading();
          const errMsg = parseResult ? parseResult.error : "Unknown parsing error";
          const suggestion = parseResult && parseResult.suggestion ? `\nSuggestion: ${parseResult.suggestion}` : "";
          this.appendMessage('agent', 'Tala', `Command Error: ${errMsg}${suggestion}`);
          return;
        }

        const action = parseResult.action;
        const command = '/' + action.command;
        
        const bubbleContent = loadingBubble.querySelector('.message-content');
        if (bubbleContent) {
          bubbleContent.textContent = `Executing governed command ${command} [risk: ${action.risk_level}]...`;
        }

        if (action.requires_confirmation) {
          const confirmed = confirm(`Critical command detected: ${command}\nRisk Level: ${action.risk_level.toUpperCase()}\n\nDo you want to proceed?`);
          if (!confirmed) {
            removeLoading();
            this.appendMessage('agent', 'Tala', `Execution of ${command} cancelled by user.`);
            return;
          }
        }

        let rpcArgs = { ...action.args };
        if (action.command === 'goal' || action.command === 'queue' || action.command === 'phase') {
          rpcArgs.goal = rpcArgs.goal_text;
        }

        if (action.command === 'halt') {
          rpcArgs.action = 'abort';
          if (!rpcArgs.task_id) {
            const listRes = await invoke('python:rpc', { method: 'task.list', params: {} });
            if (listRes && listRes.tasks && listRes.tasks.length > 0) {
              rpcArgs.task_id = listRes.tasks[0].id;
            } else {
              removeLoading();
              this.appendMessage('agent', 'Tala', `No active tasks to halt.`);
              return;
            }
          }
        }

        if (action.command === 'report') {
          if (!rpcArgs.task_id) {
            const listRes = await invoke('python:rpc', { method: 'task.list', params: {} });
            if (listRes && listRes.tasks && listRes.tasks.length > 0) {
              rpcArgs.task_id = listRes.tasks[0].id;
            } else {
              removeLoading();
              this.appendMessage('agent', 'Tala', `No tasks found to report on.`);
              return;
            }
          }
        }

        try {
          await this.prepareUndoCheckpoint(action);
        } catch (checkpointErr) {
          removeLoading();
          this.appendMessage('agent', 'Tala', `Command cancelled: ${checkpointErr.message || checkpointErr}. I could not create a safe /undo checkpoint.`, true);
          return;
        }

        const result = await invoke('python:rpc', { method: action.routed_to, params: rpcArgs });
        removeLoading();

        if (action.command === 'scan' || action.command === 'map') {
          if (result && result.codebase_map) {
            const dirsList = Object.keys(result.codebase_map).join(', ');
            this.appendMessage('agent', 'Tala', `Codebase scan complete. Mapped modules: ${dirsList}`);
            if (typeof MissionControl !== 'undefined') MissionControl.renderCodebase(result.codebase_map);
          } else {
            this.appendMessage('agent', 'Tala', `Error during scan: ${JSON.stringify(result)}`);
          }
        } else if (action.command === 'lint') {
          if (result) {
            if (result.success) {
              this.appendMessage('agent', 'Tala', `Linting check passed with 0 architectural errors!`);
            } else {
              const lintErrText = result.errors.map(e => `- ${e}`).join('\n');
              this.appendMessage('agent', 'Tala', `Linting errors found:\n${lintErrText}`);
            }
          }
        } else if (action.command === 'goal' || action.command === 'queue' || action.command === 'phase') {
          if (result && result.success) {
            this.appendMessage('agent', 'Tala', `Goal / task queued successfully. Task ID: ${result.task_id} (Phases/Subtasks: ${result.subtasks_count ?? 1})`);
            if (typeof MissionControl !== 'undefined') MissionControl.loadTasks();
          } else {
            this.appendMessage('agent', 'Tala', `Failed to queue goal: ${result ? result.error : 'Unknown error'}`);
          }
        } else if (action.command === 'halt') {
          if (result && result.success) {
            this.appendMessage('agent', 'Tala', `Task execution aborted and emergency lockdown activated.`);
            if (typeof MissionControl !== 'undefined') MissionControl.loadTasks();
          } else {
            this.appendMessage('agent', 'Tala', `Failed to abort task: ${JSON.stringify(result)}`);
          }
        } else if (action.command === 'undo') {
          if (result && result.success) {
            const undoState = this.consumeUndoCheckpoint();
            const undone = undoState?.raw_input || undoState?.command || 'last command';
            this.appendMessage('agent', 'Tala', `Undo complete. Restored checkpoint "${result.checkpoint_id || 'undo-last-command'}" from before ${undone}.`);
            if (typeof MissionControl !== 'undefined') MissionControl.loadTasks();
            if (typeof NodeGrid !== 'undefined' && typeof NodeGrid.load === 'function') {
              await NodeGrid.load();
            }
          } else {
            this.appendMessage('agent', 'Tala', `Undo failed. No command checkpoint was available or restore did not complete.`);
          }
        } else if (action.command === 'revert') {
          if (result && result.success) {
            this.appendMessage('agent', 'Tala', `Successfully reverted working tree to last safe checkpoint.`);
          } else {
            this.appendMessage('agent', 'Tala', `Failed to revert checkpoint (workspace may be clean or missing git).`);
          }
        } else if (action.command === 'report') {
          if (result && result.report) {
            this.appendMessage('agent', 'Tala', result.report);
          } else {
            this.appendMessage('agent', 'Tala', `No report generated: ${JSON.stringify(result)}`);
          }
        } else if (action.command === 'status') {
          if (result && result.tasks) {
            if (result.tasks.length === 0) {
              this.appendMessage('agent', 'Tala', 'No active tasks in the queue.');
            } else {
              const statusLines = result.tasks.map(t => `• ${t.description} [${t.status.toUpperCase()}] (ID: ${t.id.slice(0, 8)})`).join('\n');
              this.appendMessage('agent', 'Tala', `Active Tasks Queue:\n${statusLines}`);
            }
          } else {
            this.appendMessage('agent', 'Tala', `Failed to retrieve status: ${JSON.stringify(result)}`);
          }
        } else if (action.command === 'shell') {
          if (result) {
            const out = result.stdout || '';
            const err = result.stderr || '';
            const code = result.exit_code !== undefined ? result.exit_code : -1;
            this.appendMessage('agent', 'Tala', `Shell command finished with exit code ${code}.\nStdout:\n${out}\nStderr:\n${err}`);
          } else {
            this.appendMessage('agent', 'Tala', `Failed to execute shell command.`);
          }
        } else if (action.command === 'build') {
          if (result && result.success) {
            this.appendMessage('agent', 'Tala', `Build diagnostics: No build failures detected.`);
          } else if (result) {
            const file = result.file || 'unknown';
            const line = result.line || 'unknown';
            this.appendMessage('agent', 'Tala', `Build failure detected at ${file}:${line}.\nError details:\n${result.error_details || result.error || 'N/A'}`);
          } else {
            this.appendMessage('agent', 'Tala', `Failed to run build diagnostics.`);
          }
        } else if (action.command === 'fix') {
          if (result && result.success) {
            this.appendMessage('agent', 'Tala', `Build repaired successfully! Logs parsed and code updated.`);
          } else {
            this.appendMessage('agent', 'Tala', `Build repair failed: ${result ? result.error : 'Unknown error'}`);
          }
        } else if (action.command === 'test') {
          if (result && result.success) {
            this.appendMessage('agent', 'Tala', `Tests generated successfully under ${result.path || 'tests/'}.`);
          } else {
            this.appendMessage('agent', 'Tala', `Failed to generate tests: ${result ? result.error : 'Unknown error'}`);
          }
        } else if (action.command === 'generate-doc-nodes') {
          const nodes = Array.isArray(result?.nodes) ? result.nodes : [];
          let msg = `Documentation generation and disk verification complete.\nCreated: ${result?.created || 0}\nUpdated: ${result?.updated || 0}\nUnchanged: ${result?.unchanged || 0}\nErrors: ${(result?.errors || []).length}`;
          nodes.forEach(node => {
            msg += `\n  - ${node.node_id} -> ${node.file_name} | target: ${node.doc_target || 'unknown'} | schema: ${node.schema_version || 'unknown'} | validation: ${node.validation_passed ? 'passed' : 'failed'}`;
          });
          if (result?.errors?.length) {
            msg += `\n${result.errors.join('\n')}`;
          }
          this.appendMessage('agent', 'Documentation Generator', msg);
          if (typeof NodeGrid !== 'undefined' && typeof NodeGrid.load === 'function') {
            await NodeGrid.load();
          }
        } else if (action.command === 'verify-doc-nodes') {
          const nodes = Array.isArray(result?.nodes) ? result.nodes : [];
          let msg = `Documentation disk verification complete.\nVerified: ${result?.verified || 0}\nErrors: ${(result?.errors || []).length}`;
          nodes.forEach(node => {
            msg += `\n  - ${node.node_id} -> ${node.file_name} | target: ${node.doc_target || 'unknown'} | schema: ${node.schema_version || 'unknown'} | validation: ${node.validation_passed ? 'passed' : 'failed'}`;
          });
          if (result?.errors?.length) {
            msg += `\n${result.errors.join('\n')}`;
          }
          this.appendMessage('agent', 'Documentation Verifier', msg);
        } else if (action.command === 'list-doc-targets') {
          const targets = Array.isArray(result?.targets) ? result.targets : [];
          let msg = `Documentation targets available.\nBuilt-in: ${(result?.builtin_targets || []).length}\nRegistered: ${(result?.registered_targets || []).length}\nCodebase target count: ${(result?.codebase_targets || []).length}`;
          targets.forEach(target => {
            msg += `\n  - ${target.doc_target} | source: ${target.source} | codebase: ${target.include_in_codebase ? 'yes' : 'no'}`;
          });
          this.appendMessage('agent', 'Documentation Targets', msg);
        } else if (action.command === 'refresh-nodes') {
          if (typeof NodeGrid !== 'undefined' && typeof NodeGrid.load === 'function') {
            await NodeGrid.load();
          }
          this.appendMessage('agent', 'Node Grid', 'HTML Neural Node grid refreshed.');
        } else if (action.command === 'self-improve-dry-run') {
          this.appendMessage('agent', 'Self-Improvement Dry Run', JSON.stringify(result, null, 2));
        } else if (action.command === 'self-improve') {
          this.appendMessage('agent', 'Self-Improvement', JSON.stringify(result, null, 2));
          if (result?.refresh_result?.refresh_required && typeof NodeGrid !== 'undefined' && typeof NodeGrid.load === 'function') {
            await NodeGrid.load();
          }
        } else if (action.command === 'self-test') {
          const steps = Array.isArray(result?.steps) ? result.steps : [];
          const summary = result?.summary || {};
          let msg = `Tala self-test ${result?.success ? 'passed' : 'failed'}.\nPassed: ${summary.passed || 0}/${summary.total || steps.length}\nFailed: ${summary.failed || 0}`;
          steps.forEach(step => {
            const flag = step.passed ? '✓' : '✗';
            msg += `\n${flag} ${step.name} (${step.duration_seconds}s, exit ${step.exit_code})`;
            if (!step.passed) {
              const detail = (step.stderr || step.stdout || '').trim();
              if (detail) msg += `\n  ${detail.split('\n').slice(-6).join('\n  ')}`;
            }
          });
          this.appendMessage('agent', 'Tala Self-Test', msg, !result?.success);
        } else if (action.command === 'wiki') {
          if (result && result.results) {
            if (result.results.length === 0) {
              this.appendMessage('agent', 'Tala', `No wiki nodes found matching search.`);
            } else {
              const nodesList = result.results.map(n => `• [${n.id}] ${n.title} (Relevance: ${n.score.toFixed(2)})`).join('\n');
              this.appendMessage('agent', 'Tala', `Wiki Search Results:\n${nodesList}`);
            }
          } else {
            this.appendMessage('agent', 'Tala', `Failed to query wiki: ${JSON.stringify(result)}`);
          }
        } else {
          this.appendMessage('agent', 'Tala', `Executed ${command} successfully: ${JSON.stringify(result)}`);
        }
      } catch (err) {
        removeLoading();
      }
      return;
    }

    // Auto-delegate action / refactoring / coding tasks to Goal Engine background worker
    const lowerText = text.toLowerCase();
    const actionVerbs = ['turn ', 'convert ', 'refactor', 'create ', 'build ', 'implement ', 'add ', 'remove ', 'delete ', 'modify ', 'update ', 'replace ', 'change '];
    const isActionPrompt = actionVerbs.some(verb => lowerText.includes(verb));

    if (isActionPrompt) {
      const loadingId = 'chat-loading-' + Date.now();
      const loadingBubble = document.createElement('div');
      loadingBubble.className = 'chat-message agent';
      loadingBubble.id = loadingId;
      loadingBubble.innerHTML = `
        <div class="message-meta">Tala • ${typeof fmtTime === 'function' ? fmtTime() : new Date().toLocaleTimeString()}</div>
        <div class="message-content">Starting background task for goal: "${text.slice(0, 60)}..."</div>
      `;
      this._messages.appendChild(loadingBubble);
      this._messages.scrollTop = this._messages.scrollHeight;

      try {
        const result = await invoke('python:rpc', {
          method: 'task.submit',
          params: { goal: text, authority_level: 'auto' }
        });
        const loadingEl = document.getElementById(loadingId);
        if (loadingEl) this._messages.removeChild(loadingEl);

        if (result && result.success) {
          this.appendMessage('agent', 'Tala', `Goal task enqueued and running in Goal Engine. Task ID: ${result.task_id} (${result.subtasks_count ?? 1} subtasks). Live status streaming below:`);
          if (typeof MissionControl !== 'undefined' && typeof MissionControl.loadTasks === 'function') {
            MissionControl.loadTasks();
          }
          return;
        }
      } catch (err) {
        const loadingEl = document.getElementById(loadingId);
        if (loadingEl) this._messages.removeChild(loadingEl);
      }
    }

    this.sendInferenceRequest(text);
  },

  async captureScreenContext() {
    IPCLog.append('info', 'Capturing screen context for vision analysis...');
    this.appendMessage('user', 'User', '[Requesting screen capture...]');
    
    try {
      const result = await invoke('python:rpc', { method: 'multimodal.capture_screen', params: {} });
      if (result && result.image_base64) {
        const currentText = this._textarea.value.trim();
        const prompt = currentText ? currentText : "What do you see in this screenshot?";
        this._textarea.value = '';
        
        if (this._messages.lastElementChild && this._messages.lastElementChild.classList.contains('user')) {
          this._messages.removeChild(this._messages.lastElementChild);
        }

        const msgContent = `<img src="data:image/png;base64,${result.image_base64}" style="max-width: 100%; max-height: 300px; border-radius: 4px; margin-bottom: 8px; display: block;" />\n${prompt}`;
        this.appendMessage('user', 'User', msgContent);
        
        this.sendInferenceRequest(prompt, [result.image_base64]);
      } else {
        this.appendMessage('agent', 'Tala', `Failed to capture screen: ${result ? result.error : 'Unknown error'}`);
      }
    } catch (err) {
      this.appendMessage('agent', 'Tala', `Screen capture error: ${err.message || err}`);
    }
  },

  async sendInferenceRequest(promptText, imagesBase64 = []) {
    const loadingId = 'chat-loading-' + Date.now();
    const loadingBubble = document.createElement('div');
    loadingBubble.className = 'chat-message agent';
    loadingBubble.id = loadingId;
    loadingBubble.innerHTML = `
      <div class="message-meta">Tala • ${fmtTime()}</div>
      <div class="message-content">Tala is thinking...</div>
    `;
    this._messages.appendChild(loadingBubble);
    this._messages.scrollTop = this._messages.scrollHeight;

    const isResearchRefinement = typeof TabManager !== 'undefined' && TabManager.activeTabId === 'research' && typeof ResearchManager !== 'undefined' && ResearchManager._currentHtmlNN;
    let finalPrompt = promptText;
    let maxTokens = 1024;
    if (isResearchRefinement) {
      finalPrompt = `We are refining the following HTML Neural Node draft:\n\n${ResearchManager._currentHtmlNN}\n\nUser instructions for refinement: ${promptText}\n\nGenerate the complete updated version of this HTML Neural Node. Wrap the entire HTML Neural Node code in a single \`\`\`html ... \`\`\` code block. Output ONLY the code block, do not include any conversational explanation before or after.`;
      maxTokens = 2048;
    }

    const historyLimit = 10;
    if (!this._history) {
      this._history = [];
    }
    const historyToSend = this._history.slice(-historyLimit);

    try {
      const isVision = imagesBase64 && imagesBase64.length > 0;
      const effectiveProviderId = isVision && this._visionProviderId ? this._visionProviderId : (this._providerId || undefined);

      const response = await invoke('python:rpc', {
        method: 'inference.generate',
        params: {
          prompt: finalPrompt,
          history: historyToSend,
          session_id: this._sessionId,
          provider_id: effectiveProviderId,
          max_tokens: maxTokens,
          temperature: 0.7,
          images: imagesBase64
        }
      });

      const currentLoading = document.getElementById(loadingId);
      if (currentLoading) {
        this._messages.removeChild(currentLoading);
      }

      if (response && response.text) {
        if (isResearchRefinement) {
          const updatedHtml = ResearchManager.extractHtmlNN(response.text);
          if (updatedHtml && updatedHtml !== response.text) {
            ResearchManager.updateHtmlNN(updatedHtml);
            IPCLog.append('ok', 'Updated Research HTML Neural Node with chat refinement');
            this.appendMessage('agent', 'Tala', "I have successfully updated the Research HTML Neural Node based on your requests. Check the preview pane!");
          } else {
            this.appendMessage('agent', 'Tala', response.text);
          }
        } else {
          this.appendMessage('agent', 'Tala', response.text);
          if (!this._history) {
            this._history = [];
          }
          this._history.push({ role: 'user', content: promptText });
          this._history.push({ role: 'assistant', content: response.text });
          this.saveHistory();
        }

        if (typeof NodeGraph !== 'undefined') {
          invoke('python:rpc', {
            method: 'graph.query_activations',
            params: { query: promptText }
          }).then(actRes => {
            if (actRes && actRes.activations) {
              NodeGraph.applyActivations(actRes.activations);
            }
          }).catch(err => {
            console.warn('[AgentChat] Failed to query graph activations:', err);
          });
        }
      } else {
        const errMsg = (response && response.error) ? response.error : (response && response.message ? response.message : 'No response text generated by inference providers. Please check active provider in Settings -> System.');
        this.appendMessage('agent', 'Tala', `Error: ${errMsg}`, true);
      }
    } catch (err) {
      const currentLoading = document.getElementById(loadingId);
      if (currentLoading) {
        this._messages.removeChild(currentLoading);
      }
      this.appendMessage('agent', 'Tala', `Error: ${err.message ?? err}`, true);
    }
  },


  appendMessage(sender, name, content, isError = false) {
    if (!this._messages) return;

    const msgEl = document.createElement('div');
    msgEl.className = `chat-message ${sender}`;

    const meta = document.createElement('div');
    meta.className = 'message-meta';
    meta.textContent = `${name} • ${fmtTime()}`;
    msgEl.appendChild(meta);

    const body = document.createElement('div');
    body.className = 'message-content';
    if (isError) {
      body.style.color = 'var(--status-error)';
    }

    const { finalContent, thinkingText } = parseThinkingBlocks(content);

    if (thinkingText) {
      const detailsEl = document.createElement('details');
      detailsEl.className = 'thinking-details';
      
      const summaryEl = document.createElement('summary');
      summaryEl.className = 'thinking-summary';
      summaryEl.textContent = 'Thought Process';
      
      const textEl = document.createElement('div');
      textEl.className = 'thinking-text';
      textEl.textContent = thinkingText;
      
      detailsEl.appendChild(summaryEl);
      detailsEl.appendChild(textEl);
      body.appendChild(detailsEl);
      
      if (finalContent) {
        const spacer = document.createElement('div');
        spacer.style.height = 'var(--space-2)';
        body.appendChild(spacer);
      }
    }

    if (finalContent || !thinkingText) {
      const textContainer = document.createElement('div');
      textContainer.className = 'message-text-body';
      textContainer.textContent = finalContent;
      body.appendChild(textContainer);
    }

    msgEl.appendChild(body);
    this._messages.appendChild(msgEl);
    this._messages.scrollTop = this._messages.scrollHeight;
  }
};

/* ═══════════════════════════════════════════════════════════════════
   5. StatusBar
   ═══════════════════════════════════════════════════════════════════ */

const StatusBar = {
  _els: {},

  init() {
    this._els = {
      wsDot:        qs('#status-dot-ws'),
      wsText:       qs('#status-ws-text'),
      tokenValue:   qs('#status-token-value'),
      agentName:    qs('#status-agent-name'),
    };
  },

  setWsStatus(status) {
    const dot  = this._els.wsDot;
    const text = this._els.wsText;
    if (!dot || !text) return;

    // Remove all status classes
    dot.className = 'status-dot';
    const wsPulse = qs('#ws-pulse');
    if (wsPulse) wsPulse.className = 'pulse-dot';

    switch (status) {
      case 'ok':
        dot.classList.add('status-ok');
        text.textContent = 'WS: connected';
        if (wsPulse) wsPulse.classList.add('ok');
        break;
      case 'error':
        dot.classList.add('status-error');
        text.textContent = 'WS: error';
        if (wsPulse) wsPulse.classList.add('error');
        break;
      case 'connecting':
        dot.classList.add('status-connecting');
        text.textContent = 'WS: connecting…';
        if (wsPulse) wsPulse.classList.add('connecting');
        break;
      default:
        dot.classList.add('status-unknown');
        text.textContent = 'WS: unknown';
    }
  },

  setTokenBalance(n) {
    let formatted;
    if (n === 999999) {
      formatted = 'Ghost Tokens (∞)';
    } else {
      formatted = typeof n === 'number' ? n.toLocaleString() : '—';
    }
    if (this._els.tokenValue) this._els.tokenValue.textContent = formatted;

    // Update sidebar token bar + value
    const sidebarVal = qs('#sidebar-token-balance');
    if (sidebarVal) sidebarVal.textContent = formatted;

    // Update bar (assume max 1,000,000 tokens for visualization)
    const bar = qs('#token-bar');
    if (bar && typeof n === 'number') {
      const pct = Math.min(100, (n / 1_000_000) * 100);
      bar.style.width = `${pct}%`;
    }
  },

  setActiveAgent(name) {
    const formatted = name || 'None';
    if (this._els.agentName) this._els.agentName.textContent = formatted;
    const rightPanel = qs('#active-agent-name');
    if (rightPanel) rightPanel.textContent = formatted;
  },
};

/* ═══════════════════════════════════════════════════════════════════
   6. IPCLog — right-panel event log
   ═══════════════════════════════════════════════════════════════════ */

const IPCLog = {
  MAX_ENTRIES: 120,
  _container: null,

  init() {
    this._container = qs('#ipc-log');
    // Clear placeholder
    if (this._container) this._container.innerHTML = '';
    this.append('info', 'Renderer initialized');
  },

  /**
   * Append an entry to the IPC log.
   * @param {'info'|'ok'|'error'|'warn'} level
   * @param {string} message
   */
  append(level, message) {
    if (!this._container) return;

    const entry = document.createElement('div');
    entry.className = `ipc-log-entry log-${level}`;
    entry.innerHTML = `<span class="log-ts">${fmtTime()}</span><span class="log-msg">${escapeHtml(message)}</span>`;
    this._container.appendChild(entry);

    // Trim old entries
    const children = this._container.children;
    while (children.length > this.MAX_ENTRIES) {
      this._container.removeChild(children[0]);
    }

    // Auto-scroll
    this._container.scrollTop = this._container.scrollHeight;
  },
};

/* ═══════════════════════════════════════════════════════════════════
   7. NodeGrid — Neural Node viewer
   ═══════════════════════════════════════════════════════════════════ */

const NodeGrid = {
  _grid: null,
  _nodes: [],
  _searchQuery: '',

  init() {
    this._grid = qs('#node-grid');

    // Wire up buttons
    const btnRefresh = qs('#btn-refresh-nodes');
    if (btnRefresh) btnRefresh.addEventListener('click', () => this.load());

    const btnNew = qs('#btn-new-node');
    if (btnNew) btnNew.addEventListener('click', () => NodeEditor.show(null));

    const searchInput = qs('#node-search-input');
    if (searchInput) {
      searchInput.addEventListener('input', (e) => {
        this._searchQuery = e.target.value.toLowerCase().trim();
        this._render(this._nodes);
      });
    }

    // Initial load
    this.load();
  },

  async load() {
    if (!this._grid) return;

    // Show shimmer placeholders while loading
    this._grid.innerHTML = '';
    for (let i = 0; i < 3; i++) {
      const ph = document.createElement('div');
      ph.className = 'node-card placeholder-card';
      ph.setAttribute('aria-label', 'Loading…');
      ph.innerHTML = '<div class="node-card-shimmer"></div>';
      this._grid.appendChild(ph);
    }

    try {
      // Request node list from main process via IPC
      const result = await invoke('file:list', { dir: 'nodes' });
      let entries  = result && result.ok && Array.isArray(result.entries) ? result.entries : [];
      
      // Filter to HTML files only and exclude legacy archives
      const fileEntries = entries.filter(e => e.type === 'file' && e.name.endsWith('.html') && !e.name.includes('_archive_legacy'));

      // Read and parse each HTML node
      const parsedNodes = [];
      for (const entry of fileEntries) {
        try {
          const fileRes = await invoke('file:read', { filePath: 'nodes/' + entry.name });
          if (fileRes && fileRes.ok) {
            const parsed = parseNodeFromHTML(fileRes.data, entry.name);
            parsed.fileUrl = fileRes.fileUrl || `file:///nodes/${entry.name}`;
            parsedNodes.push(parsed);
          }
        } catch (e) {
          console.warn(`[NodeGrid] Failed to parse node file ${entry.name}:`, e);
        }
      }

      this._nodes = parsedNodes;
      if (typeof NodeGraph !== 'undefined' && typeof NodeGraph.updateGraphNodes === 'function') {
        NodeGraph.updateGraphNodes(parsedNodes);
      }
      this._render(parsedNodes);
      IPCLog.append('ok', `Loaded and parsed ${parsedNodes.length} node(s)`);
    } catch (err) {
      IPCLog.append('error', `NodeGrid.load: ${err.message ?? err}`);
      this._renderEmpty();
    }
  },

  _render(nodes) {
    if (!this._grid) return;
    this._grid.innerHTML = '';

    // Apply search filter
    let filteredNodes = nodes;
    if (this._searchQuery) {
      filteredNodes = nodes.filter(n => {
        const title = (n.title || '').toLowerCase();
        const id = (n.id || '').toLowerCase();
        const type = (n.type || '').toLowerCase();
        return title.includes(this._searchQuery) || id.includes(this._searchQuery) || type.includes(this._searchQuery);
      });
    }

    if (filteredNodes.length === 0) {
      this._renderEmpty();
      return;
    }

    // Consolidate by title
    const grouped = new Map();
    filteredNodes.forEach(node => {
      const key = node.title || 'Untitled Node';
      if (!grouped.has(key)) {
        grouped.set(key, []);
      }
      grouped.get(key).push(node);
    });

    let index = 0;
    grouped.forEach((groupNodes, title) => {
      const representativeNode = groupNodes[groupNodes.length - 1];
      const card = this._buildCard(representativeNode, index++, groupNodes.length);
      this._grid.appendChild(card);
    });
  },

  _renderEmpty() {
    if (!this._grid) return;
    this._grid.innerHTML = '';
    const empty = document.createElement('div');
    empty.className = 'empty-state';
    empty.style.gridColumn = '1 / -1';
    empty.innerHTML = '<p>No neural nodes found matching criteria.</p>';
    this._grid.appendChild(empty);
  },

  _buildCard(node, index, versionCount = 1) {
    const card = document.createElement('div');
    card.className = 'node-card';
    card.setAttribute('role', 'button');
    card.setAttribute('tabindex', '0');
    card.setAttribute('aria-label', `Neural node: ${node.title}`);
    card.style.animationDelay = `${(index + 1) * 0.04}s`;

    const name = node.title;
    const type = node.type;
    const id   = node.id;
    const auth = node.authority;

    const versionBadgeHtml = versionCount > 1 
      ? `<span class="badge badge-glow" style="background: rgba(0, 242, 254, 0.15); color: #00f2fe; margin-left: 6px; font-size: 0.6rem; padding: 2px 4px; vertical-align: middle;">v${versionCount}</span>`
      : '';

    card.innerHTML = `
      <div class="node-card-header">
        <span class="node-card-name" style="display: flex; align-items: center;">${escapeHtml(name)} ${versionBadgeHtml}</span>
        <span class="node-card-type">${escapeHtml(type)}</span>
      </div>
      <div class="node-card-meta">${escapeHtml(id)}</div>
      <div class="node-card-status" style="display: flex; gap: 4px; align-items: center; flex-wrap: wrap;">
        <span class="status-dot status-ok"></span>
        <span class="badge" style="background: var(--bg-tertiary, #2a2a2a); color: var(--text-muted, #ccc); font-size: 0.65rem; padding: 2px 4px; border-radius: 4px; border: 1px solid #444;">Exec: ${node.authority !== undefined && node.authority !== null && node.authority !== '' ? escapeHtml(node.authority) : '<span style="color:#f88">Missing</span>'}</span>
        <span class="badge" style="background: var(--bg-tertiary, #2a2a2a); color: var(--text-muted, #ccc); font-size: 0.65rem; padding: 2px 4px; border-radius: 4px; border: 1px solid #444;">Mem: ${node.memoryAuthority !== undefined && node.memoryAuthority !== null && node.memoryAuthority !== '' ? escapeHtml(node.memoryAuthority) : '<span style="color:#aa0">Unknown</span>'}</span>
        <span class="badge" style="background: var(--bg-tertiary, #2a2a2a); color: var(--text-muted, #ccc); font-size: 0.65rem; padding: 2px 4px; border-radius: 4px; border: 1px solid #444;">Trust: ${node.trustLabel !== undefined && node.trustLabel !== null && node.trustLabel !== '' ? escapeHtml(node.trustLabel) : '<span style="color:#aa0">Unknown</span>'}</span>
      </div>
      <div class="node-card-actions">
        <button class="btn-card-action btn-card-edit" title="Edit node" aria-label="Edit ${escapeHtml(name)}">
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20h9"/><path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z"/></svg>
        </button>
        <button class="btn-card-action btn-card-delete" title="Delete node" aria-label="Delete ${escapeHtml(name)}">
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14H6L5 6"/><path d="M10 11v6M14 11v6"/><path d="M9 6V4h6v2"/></svg>
        </button>
      </div>`;

    // Edit button — stops propagation and opens the modal
    const btnEdit = card.querySelector('.btn-card-edit');
    if (btnEdit) {
      btnEdit.addEventListener('click', (e) => {
        e.stopPropagation();
        NodeEditor.show(node);
      });
    }

    // Delete button — stops propagation so it doesn't open the editor
    const btnDel = card.querySelector('.btn-card-delete');
    if (btnDel) {
      btnDel.addEventListener('click', (e) => {
        e.stopPropagation();
        this._deleteNodeById(node);
      });
    }

    card.addEventListener('click', () => this._handleNodeSelect(node));
    card.addEventListener('keydown', e => {
      if (e.key === 'Enter' || e.key === ' ') this._handleNodeSelect(node);
    });

    // Double-click to open NodeEditor
    card.addEventListener('dblclick', () => NodeEditor.show(node));

    return card;
  },

  _handleNodeSelect(node) {
    IPCLog.append('info', `Node inspected: ${node.title} (${node.id})`);
    StatusBar.setActiveAgent(node.title);
    if (typeof WebBrowser !== 'undefined' && node.fileUrl) {
      WebBrowser.navigate(node.fileUrl);
    }
  },

  async _deleteNodeById(node) {
    const filename = node.filename || `${node.id}.html`;
    const filePath = `nodes/${filename}`;

    const confirmed = confirm(
      `Permanently delete "${node.title}" (${node.id})?\n\nThis will remove the HTML Neural Node file from the workspace. This action cannot be undone.`
    );
    if (!confirmed) return;

    try {
      const result = await invoke('file:delete', { filePath });
      if (result && result.ok) {
        try {
          await invoke('python:rpc', {
            method: 'memory.delete_node',
            params: { node_id: node.id }
          });
        } catch (e) {
          console.warn('[NodeGrid] Failed to delete node from database:', e);
        }
        IPCLog.append('ok', `Deleted Neural Node: ${node.id} (${filePath})`);
        this.load(); // Refresh the grid
      } else {
        throw new Error('Delete operation returned failure');
      }
    } catch (err) {
      console.error('[NodeGrid] Delete failed:', err);
      IPCLog.append('error', `Failed to delete node "${node.id}": ${err.message ?? err}`);
      alert(`Failed to delete Neural Node: ${err.message ?? err}`);
    }
  }
};


/* ═══════════════════════════════════════════════════════════════════
   8. TelemetryPoller — periodic health checks
   ═══════════════════════════════════════════════════════════════════ */

const TelemetryPoller = {
  POLL_INTERVAL_MS: 5_000,
  _timerId: null,
  _retries: 0,
  MAX_RETRIES: 5,

  start() {
    this._poll();
    this._timerId = setInterval(() => this._poll(), this.POLL_INTERVAL_MS);
  },

  stop() {
    if (this._timerId) clearInterval(this._timerId);
    this._timerId = null;
  },

  async _poll() {
    try {
      const health = await invoke('python:rpc', {
        method: 'harness.health',
        params: {},
      });

      if (health) {
        this._retries = 0;
        this._applyHealth(health);
      } else {
        this._handleDegraded();
      }
    } catch {
      this._handleDegraded();
    }
  },

  _applyHealth(health) {
    const { status, active_agent, tokens_used, last_latency_ms } = health;

    // WS / harness status
    StatusBar.setWsStatus(status === 'ok' ? 'ok' : 'error');
    const harnessText = qs('#harness-status-text');
    const harnessDot  = qs('#harness-status .status-dot');
    if (harnessText) harnessText.textContent = status === 'ok' ? 'Running' : 'Degraded';
    if (harnessDot) {
      harnessDot.className = `status-dot status-${status === 'ok' ? 'ok' : 'error'}`;
    }

    // Active agent
    if (active_agent !== undefined) StatusBar.setActiveAgent(active_agent);

    // Tokens
    if (typeof tokens_used === 'number') {
      StatusBar.setTokenBalance(tokens_used);
      const tokensEl = qs('#tokens-used');
      if (tokensEl) tokensEl.textContent = tokens_used.toLocaleString();
    }

    // Latency
    if (typeof last_latency_ms === 'number') {
      const latEl = qs('#rpc-latency');
      if (latEl) latEl.textContent = `${last_latency_ms} ms`;
    }
  },

  _handleDegraded() {
    this._retries++;
    StatusBar.setWsStatus(this._retries < this.MAX_RETRIES ? 'connecting' : 'error');
    const harnessText = qs('#harness-status-text');
    if (harnessText) harnessText.textContent = 'Connecting…';
    if (this._retries >= this.MAX_RETRIES) {
      IPCLog.append('error', 'Python harness unreachable after multiple retries');
    }
  },
};

/* ═══════════════════════════════════════════════════════════════════
   8b. BlockchainViewer — PoA blockchain blocks chain layout
   ═══════════════════════════════════════════════════════════════════ */

const BlockchainViewer = {
  init() {
    this._btnRefresh = qs('#btn-refresh-blockchain');
    this._flowContainer = qs('#blockchain-chain-flow');
    this._elHeight = qs('#chain-height');
    this._elValidity = qs('#chain-validity');

    if (this._btnRefresh) {
      this._btnRefresh.addEventListener('click', () => this.load());
    }
  },

  async load() {
    if (!this._flowContainer) return;

    this._flowContainer.innerHTML = '<div class="blockchain-loading">Connecting to Proof-of-Authority node...</div>';

    try {
      // 1. Fetch live chain status from local port 8332
      const statusRes = await fetch('http://127.0.0.1:8332/chain/status');
      if (!statusRes.ok) throw new Error(`HTTP error: ${statusRes.status}`);
      const status = await statusRes.json();

      // 2. Fetch blocks list
      const blocksRes = await fetch('http://127.0.0.1:8332/chain/blocks');
      if (!blocksRes.ok) throw new Error(`HTTP error: ${blocksRes.status}`);
      const blocks = await blocksRes.json();

      this.render(status, blocks);
    } catch (err) {
      console.warn('[BlockchainViewer] Blockchain node is unreachable:', err);
      this._flowContainer.innerHTML = `
        <div class="blockchain-loading" style="color: var(--status-error); font-style: normal; font-weight: 500;">
          ⚠️ Proof-of-Authority ledger node is offline or unreachable.<br>
          <span style="font-size: 0.78rem; font-weight: 400; color: var(--text-muted);">
            Ensure Node.js is running the blockchain server at http://127.0.0.1:8332
          </span>
        </div>
      `;
      if (this._elHeight) this._elHeight.textContent = '0 (Offline)';
      if (this._elValidity) {
        this._elValidity.textContent = 'UNREACHABLE';
        this._elValidity.className = 'invalid-glow';
      }
    }
  },

  render(status, blocks) {
    if (!this._flowContainer) return;
    this._flowContainer.innerHTML = '';

    // Update status widgets
    if (this._elHeight) this._elHeight.textContent = `${status.height} blocks`;
    if (this._elValidity) {
      if (status.isValid) {
        this._elValidity.textContent = 'VALID';
        this._elValidity.className = 'valid-glow';
      } else {
        this._elValidity.textContent = 'COMPROMISED';
        this._elValidity.className = 'invalid-glow';
      }
    }

    if (!Array.isArray(blocks) || blocks.length === 0) {
      this._flowContainer.innerHTML = '<div class="blockchain-loading">No blocks found on-chain.</div>';
      return;
    }

    blocks.forEach((block, index) => {
      // 1. Create Block Card
      const card = document.createElement('div');
      card.className = 'block-node-card';

      const blockTime = new Date(block.timestamp).toLocaleTimeString();
      const shortPrevHash = block.previousHash.slice(0, 8) + '…';
      const shortHash = block.hash.slice(0, 8) + '…';

      // Parse transactions list
      let txsHTML = '';
      if (!block.transactions || block.transactions.length === 0) {
        txsHTML = '<div class="block-tx-empty">Genesis Block (No transactions)</div>';
      } else {
        block.transactions.forEach(tx => {
          const shortTxId = tx.id.slice(0, 8);
          const shortTxHash = tx.hash.slice(0, 8) + '…';
          txsHTML += `
            <div class="block-tx-item">
              <div class="block-tx-id">UUID: ${shortTxId}</div>
              <div class="block-tx-detail"><span>Creator:</span> <span>${tx.creator}</span></div>
              <div class="block-tx-detail"><span>State Hash:</span> <span class="mono" title="${tx.hash}">${shortTxHash}</span></div>
            </div>
          `;
        });
      }

      card.innerHTML = `
        <div class="block-header">
          <span class="block-index">BLOCK #${block.index}</span>
          <span class="block-nonce">Nonce: ${block.nonce}</span>
        </div>
        <div class="block-meta-row">
          <div class="block-meta-item">
            <span class="block-meta-label">Hash:</span>
            <span class="block-meta-val" title="${block.hash}">${shortHash}</span>
          </div>
          <div class="block-meta-item">
            <span class="block-meta-label">Previous:</span>
            <span class="block-meta-val" title="${block.previousHash}">${shortPrevHash}</span>
          </div>
          <div class="block-meta-item">
            <span class="block-meta-label">Time:</span>
            <span>${blockTime}</span>
          </div>
        </div>
        <div class="block-txs-title">Transactions</div>
        <div class="block-txs-list">${txsHTML}</div>
      `;

      this._flowContainer.appendChild(card);

      // 2. Add connection arrow unless it is the last block in the flow
      if (index < blocks.length - 1) {
        const link = document.createElement('div');
        link.className = 'block-chain-link';
        link.innerHTML = `
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="24" height="24">
            <path stroke-linecap="round" stroke-linejoin="round" d="M14 5l7 7m0 0l-7 7m7-7H3" />
          </svg>
        `;
        this._flowContainer.appendChild(link);
      }
    });
  }
};

/* ═══════════════════════════════════════════════════════════════════
   9. TerminalConsole — integrated bottom terminal tabs & command runner
   ═══════════════════════════════════════════════════════════════════ */

const TerminalConsole = {
  _activeTab: 'terminal',
  _isMaximized: false,
  _isCollapsed: false,
  _cleanups: [],

  // Terminal instances
  terminal: null,
  blockchain: null,
  harness: null,

  init() {
    this._els = {
      panel:       qs('#terminal-panel'),
      tabs:        document.querySelectorAll('.terminal-tab'),
      tabContents: document.querySelectorAll('.terminal-tab-content'),
      containerTerm: qs('#terminal-container'),
      containerChain: qs('#blockchain-terminal-container'),
      containerHarn: qs('#harness-terminal-container'),
      btnClear:    qs('#btn-terminal-clear'),
      btnKill:     qs('#btn-terminal-kill'),
      btnToggle:   qs('#btn-terminal-toggle'),
    };

    if (!this._els.panel) return;

    // Load Terminal constructor from window
    const Terminal = window.Terminal;
    if (!Terminal) {
      console.error('[TerminalConsole] xterm.js Terminal constructor not found on window object.');
      // Graceful fallback: inject a styled pseudo-terminal placeholder
      this._injectFallbackConsole();
      return;
    }

    // Config for dark-themed, glassmorphism terminal aesthetics
    const termConfig = {
      theme: {
        background: '#04060b',
        foreground: '#c9d1d9',
        cursor: '#38bdf8',
        cursorAccent: '#04060b',
        black: '#000000',
        red: '#f87171',
        green: '#34d399',
        yellow: '#fbbf24',
        blue: '#60a5fa',
        magenta: '#a78bfa',
        cyan: '#2dd4bf',
        white: '#ffffff',
      },
      fontFamily: 'JetBrains Mono, Fira Code, monospace',
      fontSize: 12,
      lineHeight: 1.2,
      convertEol: true,
      cursorBlink: true,
      cursorStyle: 'block',
      scrollback: 5000,
    };

    // 1. Interactive terminal shell
    this.terminal = new Terminal(termConfig);

    // 2. Blockchain node log terminal (Read-only)
    this.blockchain = new Terminal({
      ...termConfig,
      disableStdin: true,
      cursorBlink: false,
      cursorStyle: 'underline',
    });

    // 3. Python harness log terminal (Read-only)
    this.harness = new Terminal({
      ...termConfig,
      disableStdin: true,
      cursorBlink: false,
      cursorStyle: 'underline',
    });

    // Mount terminals
    if (this._els.containerTerm) this.terminal.open(this._els.containerTerm);
    if (this._els.containerChain) this.blockchain.open(this._els.containerChain);
    if (this._els.containerHarn) this.harness.open(this._els.containerHarn);

    // Write welcome banners so terminals are never blank on load
    this._writeBanners();

    // Forward raw keys to persistent backend shell process
    this.terminal.onData(data => {
      invoke('terminal:command', { command: data, raw: true });
    });

    // Wire up tab buttons
    this._els.tabs.forEach(tab => {
      tab.addEventListener('click', () => {
        const targetTab = tab.dataset.tab;
        this.switchTab(targetTab);
      });
    });

    // Wire up actions
    if (this._els.btnClear) {
      this._els.btnClear.addEventListener('click', () => this.clearActiveTab());
    }

    if (this._els.btnKill) {
      this._els.btnKill.addEventListener('click', () => this.killActiveProcess());
    }

    if (this._els.btnToggle) {
      this._els.btnToggle.addEventListener('click', () => this.toggleSize());
    }

    // Set up IPC communication event listeners
    if (window.tala && typeof window.tala.on === 'function') {
      // 1. Shell output
      const termCleanup = window.tala.on('terminal:output', (data) => {
        this.terminal.write(data);
      });
      this._cleanups.push(termCleanup);

      // 2. Shell exit
      const exitCleanup = window.tala.on('terminal:exit', (info) => {
        this.terminal.write('\r\n[Shell process exited. Re-starting shell...]\r\n');
      });
      this._cleanups.push(exitCleanup);

      // 3. Blockchain node output
      const chainCleanup = window.tala.on('blockchain:output', (data) => {
        this.blockchain.write(data);
      });
      this._cleanups.push(chainCleanup);

      // 4. Python harness output
      const harnCleanup = window.tala.on('harness:output', (data) => {
        this.harness.write(data);
      });
      this._cleanups.push(harnCleanup);
    }

    // Window resize event handler
    const resizeListener = () => this.resizeTerminals();
    window.addEventListener('resize', resizeListener);
    this._cleanups.push(() => window.removeEventListener('resize', resizeListener));

    // Force initial resize and sync shell size with a small delay for DOM layout
    setTimeout(() => {
      this.resizeTerminals();
      // Prompt refresh / initial output triggering
      invoke('terminal:command', { command: '', raw: true });
    }, 150);
  },

  /**
   * Write startup banners to each terminal instance so they immediately
   * display useful content and clearly read as interactive consoles.
   */
  _writeBanners() {
    const ESC  = '\x1b[';
    const bold = `${ESC}1m`;
    const dim  = `${ESC}2m`;
    const cyan = `${ESC}36m`;
    const blue = `${ESC}34m`;
    const grn  = `${ESC}32m`;
    const yel  = `${ESC}33m`;
    const mag  = `${ESC}35m`;
    const rst  = `${ESC}0m`;

    const timestamp = new Date().toLocaleTimeString();
    const sep       = `${dim}${'─'.repeat(60)}${rst}`;

    // ── Interactive Shell Banner ─────────────────────────────────
    if (this.terminal) {
      this.terminal.write(
        `${bold}${cyan}╔══════════════════════════════════════════════════════════╗\r\n` +
        `║   ${blue}TALA${cyan} ◈ Cognitive Engine Room  ·  Interactive Shell       ║\r\n` +
        `╚══════════════════════════════════════════════════════════╝${rst}\r\n` +
        `${sep}\r\n` +
        `${grn}▸${rst} Shell session started at ${bold}${timestamp}${rst}\r\n` +
        `${dim}  Type commands below. Ctrl+C to interrupt. Ctrl+L to clear.${rst}\r\n` +
        `${sep}\r\n\r\n`
      );
    }

    // ── Blockchain Node Banner ───────────────────────────────────
    if (this.blockchain) {
      this.blockchain.write(
        `${bold}${mag}╔══════════════════════════════════════════════════════════╗\r\n` +
        `║   TALA ◈ Blockchain Node Monitor  ·  Read-Only Log      ║\r\n` +
        `╚══════════════════════════════════════════════════════════╝${rst}\r\n` +
        `${sep}\r\n` +
        `${yel}▸${rst} Waiting for blockchain node output...\r\n` +
        `${dim}  This pane streams live node logs. Input is disabled.${rst}\r\n` +
        `${sep}\r\n\r\n`
      );
    }

    // ── Python Harness Banner ────────────────────────────────────
    if (this.harness) {
      this.harness.write(
        `${bold}${blue}╔══════════════════════════════════════════════════════════╗\r\n` +
        `║   TALA ◈ Python Harness  ·  WebSocket Backend  ·  Log   ║\r\n` +
        `╚══════════════════════════════════════════════════════════╝${rst}\r\n` +
        `${sep}\r\n` +
        `${grn}▸${rst} Waiting for Python harness output...\r\n` +
        `${dim}  Streams ws://127.0.0.1:8765 JSON-RPC server logs.${rst}\r\n` +
        `${sep}\r\n\r\n`
      );
    }
  },

  /**
   * Fallback pseudo-terminal rendered in plain HTML when xterm.js
   * fails to load, so the area never looks like a blank black box.
   */
  _injectFallbackConsole() {
    const containers = [
      this._els.containerTerm,
      this._els.containerChain,
      this._els.containerHarn,
    ];
    containers.forEach((el, i) => {
      if (!el) return;
      const labels = ['Interactive Shell', 'Blockchain Node Monitor', 'Python Harness Log'];
      el.innerHTML = [
        `<div class="fallback-console">`,
        `  <div class="fallback-console__line fallback-console__banner">`,
        `    ◈ TALA — ${labels[i]}`,
        `  </div>`,
        `  <div class="fallback-console__line fallback-console__info">`,
        `    Terminal engine (xterm.js) failed to load.`,
        `  </div>`,
        `  <div class="fallback-console__line fallback-console__prompt">`,
        `    <span class="fallback-console__ps">tala@engine-room:~$</span>`,
        `    <span class="fallback-console__cursor">█</span>`,
        `  </div>`,
        `</div>`,
      ].join('');
    });
  },

  switchTab(tabKey) {
    if (tabKey === this._activeTab) return;

    // Update active tab buttons
    this._els.tabs.forEach(el => {
      el.classList.toggle('active', el.dataset.tab === tabKey);
    });

    // Update active content
    this._els.tabContents.forEach(el => {
      const isTarget = el.id === `tab-content-${tabKey}`;
      el.hidden = !isTarget;
      el.classList.toggle('active', isTarget);
    });

    this._activeTab = tabKey;

    if (tabKey === 'terminal' && this.terminal) {
      this.terminal.focus();
    }

    // Force recalculate terminal bounds since it's visible now
    this.resizeTerminals();
  },

  resizeTerminals() {
    // Dynamic measurement of cell width and height in monospace font
    const cell = document.createElement('span');
    cell.style.fontFamily = 'JetBrains Mono, Fira Code, monospace';
    cell.style.fontSize = '12px';
    cell.style.lineHeight = '1.2';
    cell.style.position = 'absolute';
    cell.style.visibility = 'hidden';
    cell.style.whiteSpace = 'pre';
    cell.textContent = 'A';
    document.body.appendChild(cell);
    const rect = cell.getBoundingClientRect();
    const charW = rect.width || 7.2;
    const charH = rect.height || 14.4;
    document.body.removeChild(cell);

    const resizeOne = (term, container) => {
      if (!term || !container) return;
      const tabContent = container.closest('.terminal-tab-content');
      if (tabContent && tabContent.hidden) return;

      const width = container.clientWidth;
      const height = container.clientHeight;

      const cols = Math.floor(width / charW);
      const rows = Math.floor(height / charH);

      if (cols > 4 && rows > 1) {
        try {
          term.resize(cols, rows);
        } catch (e) {
          console.warn('[TerminalConsole] Resize failed:', e);
        }
      }
    };

    resizeOne(this.terminal, this._els.containerTerm);
    resizeOne(this.blockchain, this._els.containerChain);
    resizeOne(this.harness, this._els.containerHarn);
  },

  clearActiveTab() {
    if (this._activeTab === 'terminal' && this.terminal) {
      this.terminal.clear();
    } else if (this._activeTab === 'blockchain' && this.blockchain) {
      this.blockchain.clear();
    } else if (this._activeTab === 'harness' && this.harness) {
      this.harness.clear();
    }
  },

  async killActiveProcess() {
    if (this._activeTab !== 'terminal' || !this.terminal) return;
    try {
      this.terminal.write('\r\n[Resetting terminal shell session...]\r\n');
      await invoke('terminal:kill');
    } catch (err) {
      this.terminal.write(`\r\nError resetting session: ${err.message}\r\n`);
    }
  },

  toggleSize() {
    const panel = this._els.panel;
    if (!panel) return;

    if (this._isCollapsed) {
      panel.classList.remove('collapsed');
      this._isCollapsed = false;
      this._isMaximized = false;
    } else if (!this._isMaximized) {
      panel.classList.add('maximized');
      this._isMaximized = true;
    } else {
      panel.classList.remove('maximized');
      panel.classList.add('collapsed');
      this._isCollapsed = true;
      this._isMaximized = false;
    }

    setTimeout(() => this.resizeTerminals(), 150);
  },

  destroy() {
    this._cleanups.forEach(cleanup => {
      if (typeof cleanup === 'function') cleanup();
    });
    this._cleanups = [];
    if (this.terminal) this.terminal.dispose();
    if (this.blockchain) this.blockchain.dispose();
    if (this.harness) this.harness.dispose();
  }
};

/* ═══════════════════════════════════════════════════════════════════
   10. PanelResizer — adjustable panel handles
   ═══════════════════════════════════════════════════════════════════ */

const PanelResizer = {
  init() {
    this.sidebarDrawer = qs('#sidebar-drawer');
    this.rightPanel = qs('#right-panel');
    this.terminalPanel = qs('#terminal-panel');
    this.appShell = qs('#app-shell');

    this.resizerSidebarMain = qs('#resizer-sidebar-main');
    this.resizerMainRight = qs('#resizer-main-right');
    this.resizerTerminal = qs('#resizer-terminal');

    if (this.resizerSidebarMain) this.initSidebarResize();
    if (this.resizerMainRight) this.initRightPanelResize();
    if (this.resizerTerminal) this.initTerminalResize();
  },

  initSidebarResize() {
    let startX, startWidth;
    const onMouseDown = (e) => {
      if (this.sidebarDrawer.classList.contains('collapsed')) return;
      startX = e.clientX;
      startWidth = parseInt(document.defaultView.getComputedStyle(this.sidebarDrawer).width, 10);
      this.resizerSidebarMain.classList.add('dragging');
      this.sidebarDrawer.style.transition = 'none';
      document.addEventListener('mousemove', onMouseMove);
      document.addEventListener('mouseup', onMouseUp);
      e.preventDefault();
    };
    const onMouseMove = (e) => {
      const width = startWidth + (e.clientX - startX);
      if (width >= 150 && width <= 450) {
        this.sidebarDrawer.style.width = `${width}px`;
        if (typeof NodeGraph !== 'undefined' && NodeGraph.active) {
          NodeGraph.resize();
          NodeGraph.draw();
        }
      }
    };
    const onMouseUp = () => {
      this.resizerSidebarMain.classList.remove('dragging');
      this.sidebarDrawer.style.transition = '';
      document.removeEventListener('mousemove', onMouseMove);
      document.removeEventListener('mouseup', onMouseUp);
      if (typeof TerminalConsole !== 'undefined' && typeof TerminalConsole.resizeTerminals === 'function') {
        TerminalConsole.resizeTerminals();
      }
      if (typeof NodeGraph !== 'undefined' && NodeGraph.active) {
        NodeGraph.resize();
        NodeGraph.draw();
      }
    };
    this.resizerSidebarMain.addEventListener('mousedown', onMouseDown);
  },

  initRightPanelResize() {
    let startX, startWidth;
    const onMouseDown = (e) => {
      startX = e.clientX;
      startWidth = parseInt(document.defaultView.getComputedStyle(this.rightPanel).width, 10);
      this.resizerMainRight.classList.add('dragging');
      this.rightPanel.style.transition = 'none';
      document.addEventListener('mousemove', onMouseMove);
      document.addEventListener('mouseup', onMouseUp);
      e.preventDefault();
    };
    const onMouseMove = (e) => {
      const width = startWidth - (e.clientX - startX);
      if (width >= 200 && width <= 600) {
        this.rightPanel.style.width = `${width}px`;
        if (typeof NodeGraph !== 'undefined' && NodeGraph.active) {
          NodeGraph.resize();
          NodeGraph.draw();
        }
      }
    };
    const onMouseUp = () => {
      this.resizerMainRight.classList.remove('dragging');
      this.rightPanel.style.transition = '';
      document.removeEventListener('mousemove', onMouseMove);
      document.removeEventListener('mouseup', onMouseUp);
      if (typeof TerminalConsole !== 'undefined' && typeof TerminalConsole.resizeTerminals === 'function') {
        TerminalConsole.resizeTerminals();
      }
      if (typeof NodeGraph !== 'undefined' && NodeGraph.active) {
        NodeGraph.resize();
        NodeGraph.draw();
      }
    };
    this.resizerMainRight.addEventListener('mousedown', onMouseDown);
  },

  initTerminalResize() {
    let startY, startHeight;
    const onMouseDown = (e) => {
      startY = e.clientY;
      startHeight = parseInt(document.defaultView.getComputedStyle(this.terminalPanel).height, 10);
      this.resizerTerminal.classList.add('dragging');
      this.terminalPanel.style.transition = 'none';
      document.addEventListener('mousemove', onMouseMove);
      document.addEventListener('mouseup', onMouseUp);
      e.preventDefault();
    };
    const onMouseMove = (e) => {
      const height = startHeight - (e.clientY - startY);
      if (height >= 33 && height <= 600) {
        this.terminalPanel.classList.remove('collapsed', 'maximized');
        this.terminalPanel.style.height = `${height}px`;
        if (typeof NodeGraph !== 'undefined' && NodeGraph.active) {
          NodeGraph.resize();
          NodeGraph.draw();
        }
      }
    };
    const onMouseUp = () => {
      this.resizerTerminal.classList.remove('dragging');
      this.terminalPanel.style.transition = '';
      document.removeEventListener('mousemove', onMouseMove);
      document.removeEventListener('mouseup', onMouseUp);
      if (typeof TerminalConsole !== 'undefined' && typeof TerminalConsole.resizeTerminals === 'function') {
        TerminalConsole.resizeTerminals();
      }
      if (typeof NodeGraph !== 'undefined' && NodeGraph.active) {
        NodeGraph.resize();
        NodeGraph.draw();
      }
    };
    this.resizerTerminal.addEventListener('mousedown', onMouseDown);
  }
};

/* ═══════════════════════════════════════════════════════════════════
   10.5. WebBrowser — controls internal browser iframe and listens to agent URL updates
   ═══════════════════════════════════════════════════════════════════ */

const WebBrowser = {
  init() {
    this._iframe = qs('#browser-iframe'); // This is our webview now
    this._urlInput = qs('#browser-url-input');
    this._btnBack = qs('#btn-browser-back');
    this._btnForward = qs('#btn-browser-forward');
    this._btnRefresh = qs('#btn-browser-refresh');

    if (this._btnBack) {
      this._btnBack.addEventListener('click', () => {
        try {
          if (this._iframe) {
            if (typeof this._iframe.goBack === 'function') {
              this._iframe.goBack();
            } else if (this._iframe.contentWindow) {
              this._iframe.contentWindow.history.back();
            }
          }
        } catch (e) {
          console.warn('[WebBrowser] Back navigation error:', e);
        }
      });
    }

    if (this._btnForward) {
      this._btnForward.addEventListener('click', () => {
        try {
          if (this._iframe) {
            if (typeof this._iframe.goForward === 'function') {
              this._iframe.goForward();
            } else if (this._iframe.contentWindow) {
              this._iframe.contentWindow.history.forward();
            }
          }
        } catch (e) {
          console.warn('[WebBrowser] Forward navigation error:', e);
        }
      });
    }

    if (this._btnRefresh) {
      this._btnRefresh.addEventListener('click', () => {
        if (this._iframe) {
          if (typeof this._iframe.reload === 'function') {
            this._iframe.reload();
          } else {
            this._iframe.src = this._iframe.src;
          }
        }
      });
    }

    if (this._urlInput) {
      this._urlInput.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') {
          const value = this._urlInput.value.trim();
          if (value) {
            this.navigate(value);
          }
        }
      });
    }

    // Update address bar dynamically when webview page navigates
    if (this._iframe && typeof this._iframe.addEventListener === 'function') {
      this._iframe.addEventListener('did-navigate', (e) => {
        if (this._urlInput && e && e.url) {
          this._urlInput.value = e.url;
        }
      });
      this._iframe.addEventListener('did-navigate-in-page', (e) => {
        if (this._urlInput && e && e.url) {
          this._urlInput.value = e.url;
        }
      });
      this._iframe.addEventListener('will-navigate', (e) => {
        if (e && e.url) {
          const targetUrl = e.url;
          // Intercept link clicks containing duplicate '/nodes/nodes/' relative resolution bugs and clean them
          if (targetUrl.startsWith('file://') && targetUrl.includes('/nodes/nodes/')) {
            e.preventDefault();
            const cleanUrl = targetUrl.replace('/nodes/nodes/', '/nodes/');
            this.navigate(cleanUrl);
          }
        }
      });
    }

    // Register WebSocket Push Notification Listener for browser navigations from Python
    try {
      this._wsListenerCleanup = window.tala.on('python:notification', (msg) => {
        if (msg && msg.method === 'browser.navigate' && msg.params && msg.params.url) {
          this.navigate(msg.params.url, true);
        }
      });
    } catch (err) {
      console.warn('[WebBrowser] Failed to register websocket notification listener:', err);
    }
  },

  navigate(rawText, fromAgent = false) {
    if (!this._iframe) return;

    const urlPattern = /^(https?:\/\/)?([\da-z.-]+)\.([a-z.]{2,6})([^\s]*)$/i;
    const localhostPattern = /^(https?:\/\/)?(localhost|127\.0\.0\.1|0\.0\.0\.0)(:\d+)?([^\s]*)$/i;
    
    let url = rawText;
    const isFileUrl = /^file:\/\//i.test(rawText);
    const isUrl = urlPattern.test(rawText) || localhostPattern.test(rawText) || isFileUrl;

    if (isFileUrl) {
      url = rawText;
    } else if (isUrl) {
      if (!/^https?:\/\//i.test(url)) {
        const isLocal = localhostPattern.test(rawText);
        url = (isLocal ? 'http://' : 'https://') + url;
      }
    } else {
      url = `https://duckduckgo.com/?q=${encodeURIComponent(rawText)}`;
    }

    IPCLog.append('info', `Web Browser: Navigating to ${url}${fromAgent ? ' (triggered by agent)' : ''}`);
    
    if (typeof this._iframe.loadURL === 'function') {
      this._iframe.loadURL(url);
    } else {
      this._iframe.src = url;
    }

    if (this._urlInput) {
      this._urlInput.value = url;
    }

    // Automatically switch to the browser tab
    TabManager.openTab('browser', 'Web Browser', 'view');
  }
};

/* ═══════════════════════════════════════════════════════════════════
   11. CommandBar — global command and browser search bar
   ═══════════════════════════════════════════════════════════════════ */

const CommandBar = {
  COMMANDS: [
    { cmd: '/help', desc: 'Display all available slash commands' },
    { cmd: '/clear', desc: 'Clear the chat logs and IPC event console' },
    { cmd: '/wallet', desc: 'Query local wallet AI token and crypto balances' },
    { cmd: '/contracts', desc: 'List recent smart contract transactions' },
    { cmd: '/astrology', desc: 'Compute Swiss Ephemeris transits and style modulations' },
    { cmd: '/generate-doc-nodes', desc: 'Generate and verify program documentation HTML-NN nodes from disk' },
    { cmd: '/verify-doc-nodes', desc: 'Verify documentation HTML-NN nodes by parsing actual files on disk' },
    { cmd: '/list-doc-targets', desc: 'List built-in and registered documentation generation targets' },
    { cmd: '/refresh-nodes', desc: 'Reload the visible HTML Neural Node grid' },
    { cmd: '/self-improve-dry-run', desc: 'Create and execute a safe self-improvement dry run' },
    { cmd: '/self-improve', desc: 'Execute a self-improvement goal through approved bridges' },
    { cmd: '/nodes', desc: 'List all loaded HTML Neural Nodes in the workspace' },
    { cmd: '/peer', desc: 'Send a live P2P message: /peer @username message' }
  ],

  init() {
    this._input = qs('#global-command-bar');
    this._wrapper = qs('.command-bar-wrapper');
    this._dropdown = qs('#command-bar-autocomplete');
    this._selectedIndex = -1;
    this._filteredMatches = [];

    if (this._input) {
      this._input.addEventListener('keydown', (e) => {
        if (this._dropdown && !this._dropdown.hidden) {
          if (e.key === 'ArrowDown') {
            e.preventDefault();
            this._selectedIndex = (this._selectedIndex + 1) % this._filteredMatches.length;
            this.updateSelection();
          } else if (e.key === 'ArrowUp') {
            e.preventDefault();
            this._selectedIndex = (this._selectedIndex - 1 + this._filteredMatches.length) % this._filteredMatches.length;
            this.updateSelection();
          } else if (e.key === 'Escape') {
            e.preventDefault();
            this.hideDropdown();
          } else if (e.key === 'Enter') {
            if (this._selectedIndex >= 0 && this._selectedIndex < this._filteredMatches.length) {
              e.preventDefault();
              this._input.value = this._filteredMatches[this._selectedIndex].cmd;
              this.handleInput();
            }
          }
        } else {
          if (e.key === 'Enter') {
            this.handleInput();
          }
        }
      });

      this._input.addEventListener('input', () => {
        const val = this._input.value.trim();
        if (val.startsWith('/')) {
          const matching = this.COMMANDS.filter(c => c.cmd.toLowerCase().startsWith(val.toLowerCase()));
          this._selectedIndex = matching.length > 0 ? 0 : -1;
          this.showDropdown(matching);
        } else {
          this.hideDropdown();
        }
      });

      this._input.addEventListener('blur', () => {
        // Delay hiding so mousedown click events on dropdown items have time to fire first
        setTimeout(() => this.hideDropdown(), 150);
      });

      document.addEventListener('keydown', (e) => {
        if ((e.ctrlKey || e.metaKey) && e.key === 'p') {
          e.preventDefault();
          this._input.focus();
          this._input.select();
        }
      });
    }
  },

  showDropdown(matches) {
    if (!this._dropdown) return;
    this._filteredMatches = matches;
    this._dropdown.innerHTML = '';

    if (matches.length === 0) {
      this.hideDropdown();
      return;
    }

    this._dropdown.hidden = false;

    matches.forEach((item, index) => {
      const el = document.createElement('div');
      el.className = 'autocomplete-item' + (index === this._selectedIndex ? ' selected' : '');
      el.innerHTML = `
        <span class="autocomplete-item-cmd">${item.cmd}</span>
        <span class="autocomplete-item-desc">${item.desc}</span>
      `;
      el.addEventListener('mousedown', (e) => {
        e.preventDefault(); // Prevents blur hiding dropdown before selection executes
        this._input.value = item.cmd;
        this.handleInput();
      });
      this._dropdown.appendChild(el);
    });
  },

  hideDropdown() {
    if (this._dropdown) {
      this._dropdown.hidden = true;
      this._selectedIndex = -1;
      this._filteredMatches = [];
    }
  },

  updateSelection() {
    if (!this._dropdown) return;
    const items = this._dropdown.querySelectorAll('.autocomplete-item');
    items.forEach((item, index) => {
      if (index === this._selectedIndex) {
        item.classList.add('selected');
        item.scrollIntoView({ block: 'nearest' });
      } else {
        item.classList.remove('selected');
      }
    });
  },

  handleInput() {
    if (!this._input) return;
    const rawText = this._input.value.trim();
    if (!rawText) return;

    const urlPattern = /^(https?:\/\/)?([\da-z.-]+)\.([a-z.]{2,6})([\/\w .-]*)*\/?$/i;
    const localhostPattern = /^(https?:\/\/)?(localhost|127\.0\.0\.1|0\.0\.0\.0)(:\d+)?([\/\w .-]*)*\/?$/i;
    
    const isUrl = urlPattern.test(rawText) || localhostPattern.test(rawText);

    if (isUrl) {
      let url = rawText;
      if (!/^https?:\/\//i.test(url)) {
        const isLocal = localhostPattern.test(rawText);
        url = (isLocal ? 'http://' : 'https://') + url;
      }
      if (typeof WebBrowser !== 'undefined' && typeof WebBrowser.navigate === 'function') {
        WebBrowser.navigate(url);
      } else {
        TabManager.openTab('browser', 'Web Browser', 'view');
        const iframe = qs('#browser-iframe');
        const browserUrlInput = qs('#browser-url-input');
        if (iframe) iframe.src = url;
        if (browserUrlInput) browserUrlInput.value = url;
      }
      this._input.blur();
      this.hideDropdown();
    } else if (rawText.startsWith('/')) {
      const parts = rawText.split(/\s+/);
      const command = parts[0].toLowerCase();
      this.executeSlashCommand(command, parts.slice(1));
    } else {
      IPCLog.append('info', `Command Bar: Received command "${rawText}"`);
      
      if (typeof AgentChat !== 'undefined' && typeof AgentChat.appendMessage === 'function') {
        AgentChat.appendMessage('user', 'You', `[Command: ${rawText}]`);
        AgentChat._textarea.value = `Execute command: ${rawText}`;
        AgentChat.sendMessage();
      }
      this._input.value = '';
      this._input.blur();
      this.hideDropdown();
    }
  },

  async executeSlashCommand(command, args) {
    this._input.value = '';
    this.hideDropdown();
    this._input.blur();

    if (typeof AgentChat === 'undefined') {
      IPCLog.append('error', 'AgentChat panel not initialized.');
      return;
    }

    // Append user command message in chat
    const fullCommand = command + (args.length ? ' ' + args.join(' ') : '');
    AgentChat.appendMessage('user', 'You', `[Command: ${fullCommand}]`);

    switch (command) {
      case '/help':
        let helpText = 'Available Workspace Commands:\n';
        this.COMMANDS.forEach(c => {
          helpText += `  • ${c.cmd} — ${c.desc}\n`;
        });
        AgentChat.appendMessage('agent', 'System', helpText);
        break;

      case '/clear':
        AgentChat._messages.innerHTML = '';
        AgentChat.appendMessage('agent', 'System', 'Logs and chat panel cleared.');
        if (typeof IPCLog !== 'undefined') {
          IPCLog._container.innerHTML = '';
          IPCLog.append('info', 'System events cleared.');
        }
        break;

      case '/wallet':
        try {
          const res = await invoke('python:rpc', {
            method: 'db.query',
            params: { sql: 'SELECT ai_token_balance, crypto_balance FROM wallet WHERE id = 1' }
          });
          if (res && Array.isArray(res.rows) && res.rows.length > 0) {
            const row = res.rows[0];
            const ai = parseFloat(row.ai_token_balance).toLocaleString();
            const crypto = parseFloat(row.crypto_balance).toLocaleString();
            AgentChat.appendMessage(
              'agent', 
              'System Wallet Info', 
              `AI Token Balance: ${ai} tokens\nEscrow Crypto Balance: $${crypto} USD`
            );
          } else {
            AgentChat.appendMessage('agent', 'System Wallet Info', 'Error: Wallet state not found or initialized in database.');
          }
        } catch (err) {
          AgentChat.appendMessage('agent', 'System Wallet Info', `Failed to query wallet: ${err.message || err}`);
        }
        break;

      case '/contracts':
        try {
          const res = await invoke('python:rpc', {
            method: 'db.query',
            params: { sql: 'SELECT contract_id, creator, counterparty, state, created_at FROM smart_contracts ORDER BY created_at DESC LIMIT 5' }
          });
          if (res && Array.isArray(res.rows) && res.rows.length > 0) {
            let msg = 'Recent Smart Contracts:\n';
            res.rows.forEach(r => {
              const shortId = r.contract_id.slice(0, 8);
              const date = new Date(r.created_at).toLocaleTimeString();
              msg += `  • [${shortId}] | Creator: ${r.creator} -> Counterparty: ${r.counterparty} | State: ${r.state} (${date})\n`;
            });
            AgentChat.appendMessage('agent', 'System Contract Registry', msg);
          } else {
            AgentChat.appendMessage('agent', 'System Contract Registry', 'No smart contracts recorded on the local ledger.');
          }
        } catch (err) {
          AgentChat.appendMessage('agent', 'System Contract Registry', `Failed to query contracts: ${err.message || err}`);
        }
        break;

      case '/astrology':
        try {
          const res = await invoke('python:rpc', {
            method: 'astro.transits',
            params: {}
          });
          if (res && Array.isArray(res.transits) && res.transits.length > 0) {
            let msg = 'Active planetary transits affecting agent focus:\n';
            res.transits.forEach(t => {
              const flag = t.is_challenging ? '⚠️' : '✨';
              msg += `  ${flag} Transiting ${t.planet} ${t.aspect_type} Natal ${t.natal_planet} (Orb: ${t.orb.toFixed(2)}°, Influence: ${(t.influence_score * 100).toFixed(0)}%)\n`;
            });
            AgentChat.appendMessage('agent', 'System Chrono-Astrology', msg);
          } else {
            AgentChat.appendMessage('agent', 'System Chrono-Astrology', 'No active transits exceeding aspect thresholds currently.');
          }
        } catch (err) {
          AgentChat.appendMessage('agent', 'System Chrono-Astrology', `Failed to calculate transits: ${err.message || err}`);
        }
        break;

      case '/nodes':
        try {
          const listRes = await invoke('file:list', { dir: 'nodes' });
          const entries = listRes && listRes.ok && Array.isArray(listRes.entries) ? listRes.entries : [];
          const htmlEntries = entries.filter(e => e.type === 'file' && e.name.endsWith('.html') && !e.name.includes('_archive_legacy'));
          if (htmlEntries.length > 0) {
            let msg = `Loaded HTML Neural Nodes (${htmlEntries.length} found):\n`;
            for (const entry of htmlEntries) {
              try {
                const fileRes = await invoke('file:read', { filePath: 'nodes/' + entry.name });
                if (fileRes && fileRes.ok) {
                  const parsed = parseNodeFromHTML(fileRes.data, entry.name);
                  msg += `  • [${parsed.id}] (Type: ${parsed.type}, Authority: ${parsed.authority})\n`;
                }
              } catch (e) {
                // skip corrupted
              }
            }
            AgentChat.appendMessage('agent', 'Workspace Node Summary', msg);
          } else {
            AgentChat.appendMessage('agent', 'Workspace Node Summary', 'No HTML Neural Nodes found in the nodes directory.');
          }
        } catch (err) {
          AgentChat.appendMessage('agent', 'Workspace Node Summary', `Failed to list nodes: ${err.message || err}`);
        }
        break;

      case '/peer':
        if (args.length < 2) {
          AgentChat.appendMessage('agent', 'P2P Message', 'Usage: /peer @username Your message here');
          break;
        }
        try {
          const username = args[0];
          const message = args.slice(1).join(' ');
          const res = await invoke('python:rpc', {
            method: 'p2p.send_message',
            params: { username, message }
          });
          if (res && res.ok) {
            AgentChat.appendMessage('agent', 'P2P Message', `Sent to ${username}.`);
          } else {
            AgentChat.appendMessage('agent', 'P2P Message', `Send failed: ${res?.error || 'unknown error'}`);
          }
        } catch (error) {
          AgentChat.appendMessage('agent', 'P2P Message', `Send error: ${error?.message || error}`);
        }
        break;

      case '/generate-doc-nodes':
        try {
          const res = await invoke('python:rpc', {
            method: 'docs.generate_and_verify_program_nodes',
            params: { targets: args.length ? args : ['chat.panel'] }
          });
          const nodes = Array.isArray(res.nodes) ? res.nodes : [];
          let msg = `Documentation generation and disk verification complete.\nCreated: ${res.created || 0}\nUpdated: ${res.updated || 0}\nUnchanged: ${res.unchanged || 0}\nErrors: ${(res.errors || []).length}`;
          nodes.forEach(node => {
            msg += `\n  - ${node.node_id} -> ${node.file_name} | target: ${node.doc_target || 'unknown'} | schema: ${node.schema_version || 'unknown'} | validation: ${node.validation_passed ? 'passed' : 'failed'}`;
          });
          if (res.errors && res.errors.length) {
            msg += `\n${res.errors.join('\n')}`;
          }
          AgentChat.appendMessage('agent', 'Documentation Generator', msg);
          if (typeof NodeGrid !== 'undefined' && typeof NodeGrid.load === 'function') {
            await NodeGrid.load();
          }
        } catch (err) {
          AgentChat.appendMessage('agent', 'Documentation Generator', `Failed to generate documentation nodes: ${err.message || err}`);
        }
        break;

      case '/verify-doc-nodes':
        try {
          const res = await invoke('python:rpc', {
            method: 'docs.verify_documentation_nodes',
            params: { targets: args.length ? args : undefined }
          });
          const nodes = Array.isArray(res.nodes) ? res.nodes : [];
          let msg = `Documentation disk verification complete.\nVerified: ${res.verified || 0}\nErrors: ${(res.errors || []).length}`;
          nodes.forEach(node => {
            msg += `\n  - ${node.node_id} -> ${node.file_name} | target: ${node.doc_target || 'unknown'} | schema: ${node.schema_version || 'unknown'} | validation: ${node.validation_passed ? 'passed' : 'failed'}`;
          });
          if (res.errors && res.errors.length) {
            msg += `\n${res.errors.join('\n')}`;
          }
          AgentChat.appendMessage('agent', 'Documentation Verifier', msg);
        } catch (err) {
          AgentChat.appendMessage('agent', 'Documentation Verifier', `Failed to verify documentation nodes: ${err.message || err}`);
        }
        break;

      case '/list-doc-targets':
        try {
          const res = await invoke('python:rpc', {
            method: 'docs.list_program_targets',
            params: {}
          });
          const targets = Array.isArray(res.targets) ? res.targets : [];
          let msg = `Documentation targets available.\nBuilt-in: ${(res.builtin_targets || []).length}\nRegistered: ${(res.registered_targets || []).length}\nCodebase target count: ${(res.codebase_targets || []).length}`;
          targets.forEach(target => {
            msg += `\n  - ${target.doc_target} | source: ${target.source} | codebase: ${target.include_in_codebase ? 'yes' : 'no'}`;
          });
          AgentChat.appendMessage('agent', 'Documentation Targets', msg);
        } catch (err) {
          AgentChat.appendMessage('agent', 'Documentation Targets', `Failed to list documentation targets: ${err.message || err}`);
        }
        break;

      case '/refresh-nodes':
        try {
          await invoke('python:rpc', {
            method: 'docs.refresh_node_grid',
            params: {}
          });
          if (typeof NodeGrid !== 'undefined' && typeof NodeGrid.load === 'function') {
            await NodeGrid.load();
          }
          AgentChat.appendMessage('agent', 'Node Grid', 'HTML Neural Node grid refreshed.');
        } catch (err) {
          AgentChat.appendMessage('agent', 'Node Grid', `Failed to refresh node grid: ${err.message || err}`);
        }
        break;

      case '/self-improve-dry-run':
        try {
          const goal = args.join(' ') || 'Prove Tala can create and execute a bounded self-improvement dry-run plan without mutating files.';
          const res = await invoke('python:rpc', {
            method: 'self_improvement.dry_run_goal',
            params: { goal, target_files: ['README.md'] }
          });
          AgentChat.appendMessage('agent', 'Self-Improvement Dry Run', JSON.stringify(res, null, 2));
        } catch (err) {
          AgentChat.appendMessage('agent', 'Self-Improvement Dry Run', `Failed: ${err.message || err}`);
        }
        break;

      case '/self-improve':
        try {
          const goal = args.join(' ');
          const res = await invoke('python:rpc', {
            method: 'self_improvement.execute_goal',
            params: { goal }
          });
          AgentChat.appendMessage('agent', 'Self-Improvement', JSON.stringify(res, null, 2));
          if (res?.refresh_result?.refresh_required && typeof NodeGrid !== 'undefined' && typeof NodeGrid.load === 'function') {
            await NodeGrid.load();
          }
        } catch (err) {
          AgentChat.appendMessage('agent', 'Self-Improvement', `Failed: ${err.message || err}`);
        }
        break;

      default:
        AgentChat.appendMessage('agent', 'System', `Unknown command: "${command}". Type /help to see all available commands.`);
        break;
    }
  }
};

/* ═══════════════════════════════════════════════════════════════════
   12. NodeEditor — visual creator and inspector
   ═══════════════════════════════════════════════════════════════════ */

const NodeEditor = {
  activeNode: null,

  init() {
    this._overlay = qs('#node-modal-overlay');
    this._form = qs('#node-form');
    this._title = qs('#node-modal-title');
    this._idInput = qs('#node-id-input');
    this._titleInput = qs('#node-title-input');
    this._typeSelect = qs('#node-type-select');
    this._authoritySelect = qs('#node-authority-select');
    this._relevanceInput = qs('#node-relevance-input');
    this._tagsInput = qs('#node-tags-input');
    this._descriptionInput = qs('#node-description-input');
    this._memoryAuthoritySelect = qs('#node-memory-authority-select');
    this._memoryTextarea = qs('#node-memory-textarea');
    this._relList = qs('#modal-relationships-list');
    this._btnAddRel = qs('#btn-modal-add-rel');
    this._btnCancel = qs('#btn-cancel-modal');
    this._btnClose = qs('#btn-close-modal');
    this._btnDelete = qs('#btn-delete-node');

    if (this._btnAddRel) {
      this._btnAddRel.addEventListener('click', () => this.addRelationshipRow());
    }

    if (this._btnCancel) {
      this._btnCancel.addEventListener('click', () => this.hide());
    }

    if (this._btnClose) {
      this._btnClose.addEventListener('click', () => this.hide());
    }

    if (this._btnDelete) {
      this._btnDelete.addEventListener('click', () => this.deleteNode());
    }

    if (this._form) {
      this._form.addEventListener('submit', (e) => {
        e.preventDefault();
        this.saveNode();
      });
    }

    if (this._idInput) {
      this._idInput.addEventListener('input', () => {
        const val = this._idInput.value;
        const cleaned = val.toLowerCase().replace(/[^a-z0-9-]/g, '');
        if (cleaned !== val) {
          this._idInput.value = cleaned;
        }
      });
    }
  },

  show(node = null) {
    this.activeNode = node;
    if (this._form) this._form.reset();
    if (this._relList) this._relList.innerHTML = '';

    if (node) {
      if (this._title) this._title.textContent = 'Edit Neural Node';
      if (this._idInput) {
        this._idInput.value = node.id;
        this._idInput.disabled = true;
      }
      if (this._titleInput) this._titleInput.value = node.title;
      if (this._typeSelect) this._typeSelect.value = node.type;
      if (this._authoritySelect) {
        this._authoritySelect.value = node.authority;
        this._authoritySelect.disabled = true;
      }
      if (this._relevanceInput) this._relevanceInput.value = node.relevance;
      if (this._tagsInput) this._tagsInput.value = node.tags ? node.tags.join(', ') : '';
      if (this._descriptionInput) this._descriptionInput.value = node.description || '';
      if (this._memoryAuthoritySelect) this._memoryAuthoritySelect.value = node.memoryAuthority || '0.00';
      if (this._memoryTextarea) this._memoryTextarea.value = node.memory;

      if (node.relationships && Array.isArray(node.relationships)) {
        node.relationships.forEach(rel => {
          this.addRelationshipRow(rel.href, rel.type, rel.label);
        });
      }

      // Show delete button only when editing an existing node
      if (this._btnDelete) this._btnDelete.hidden = false;
    } else {
      if (this._title) this._title.textContent = 'Create Neural Node';
      if (this._idInput) {
        this._idInput.value = '';
        this._idInput.disabled = false;
      }
      if (this._authoritySelect) {
        this._authoritySelect.value = '0.00';
        this._authoritySelect.disabled = false;
      }
      if (this._relevanceInput) this._relevanceInput.value = '0.50';
      if (this._descriptionInput) this._descriptionInput.value = '';
      if (this._memoryAuthoritySelect) this._memoryAuthoritySelect.value = '0.00';

      // Hide delete button when creating a new node
      if (this._btnDelete) this._btnDelete.hidden = true;
    }

    if (this._overlay) {
      this._overlay.hidden = false;
    }
  },

  hide() {
    if (this._overlay) {
      this._overlay.hidden = true;
    }
  },

  async deleteNode() {
    if (!this.activeNode) return;

    const id = this.activeNode.id;
    const filename = this.activeNode.filename || `${id}.html`;
    const filePath = `nodes/${filename}`;

    const confirmed = confirm(
      `Permanently delete "${this.activeNode.title}" (${id})?\n\nThis will remove the HTML Neural Node file from the workspace. This action cannot be undone.`
    );
    if (!confirmed) return;

    try {
      const result = await invoke('file:delete', { filePath });
      if (result && result.ok) {
        IPCLog.append('ok', `Deleted Neural Node: ${id} (${filePath})`);
        this.hide();
        // Reload the node grid and graph
        if (typeof NodeGrid !== 'undefined') {
          NodeGrid.load();
        }
      } else {
        throw new Error('Delete operation returned failure');
      }
    } catch (err) {
      console.error('[NodeEditor] Delete failed:', err);
      IPCLog.append('error', `Failed to delete node "${id}": ${err.message ?? err}`);
      alert(`Failed to delete Neural Node: ${err.message ?? err}`);
    }
  },

  addRelationshipRow(href = '', type = 'references', label = '') {
    if (!this._relList) return;

    const row = document.createElement('div');
    row.className = 'relationship-row';

    const hrefInput = document.createElement('input');
    hrefInput.type = 'text';
    hrefInput.placeholder = 'nodes/concept-name.html';
    hrefInput.value = href;
    hrefInput.required = true;
    hrefInput.className = 'rel-href-input';

    const typeSelect = document.createElement('select');
    typeSelect.className = 'rel-type-select';
    const relTypes = [
      'depends-on', 'extends', 'contradicts', 'authored-by', 'supersedes',
      'references', 'implements', 'governed-by', 'owns', 'peer-of'
    ];
    relTypes.forEach(t => {
      const opt = document.createElement('option');
      opt.value = t;
      opt.textContent = t;
      if (t === type) opt.selected = true;
      typeSelect.appendChild(opt);
    });

    const labelInput = document.createElement('input');
    labelInput.type = 'text';
    labelInput.placeholder = 'Link Label';
    labelInput.value = label;
    labelInput.className = 'rel-label-input';

    const btnDelete = document.createElement('button');
    btnDelete.type = 'button';
    btnDelete.className = 'btn-rel-delete';
    btnDelete.innerHTML = `
      <svg viewBox="0 0 16 16" fill="currentColor" width="12" height="12">
        <path fill-rule="evenodd" d="M2.5 1a1 1 0 00-1 1v12a1 1 0 001 1h11a1 1 0 001-1V2a1 1 0 00-1-1h-11zm3.5 4a.5.5 0 000 1h4a.5.5 0 000-1H6zm0 3a.5.5 0 000 1h4a.5.5 0 000-1H6zm0 3a.5.5 0 000 1h4a.5.5 0 000-1H6z" />
      </svg>`;
    btnDelete.addEventListener('click', () => {
      row.remove();
    });

    row.appendChild(hrefInput);
    row.appendChild(typeSelect);
    row.appendChild(labelInput);
    row.appendChild(btnDelete);

    this._relList.appendChild(row);
    this._relList.scrollTop = this._relList.scrollHeight;
  },

  async saveNode() {
    const id = this._idInput.value.trim();
    const title = this._titleInput.value.trim();
    const type = this._typeSelect.value;
    const authority = this._authoritySelect.value;
    const relevance = parseFloat(this._relevanceInput.value).toFixed(2);
    const tags = this._tagsInput.value.split(',').map(t => t.trim()).filter(t => t);
    const description = this._descriptionInput ? this._descriptionInput.value.trim() : '';
    const memoryAuthority = this._memoryAuthoritySelect ? this._memoryAuthoritySelect.value : '0.00';
    const memory = this._memoryTextarea.value.trim();

    const relationships = [];
    const rows = this._relList.querySelectorAll('.relationship-row');
    rows.forEach(row => {
      const href = row.querySelector('.rel-href-input').value.trim();
      const type = row.querySelector('.rel-type-select').value;
      const label = row.querySelector('.rel-label-input').value.trim();
      if (href) {
        relationships.push({ href, type, label });
      }
    });

    // Auto-link scanner: find mentions of other nodes in semantic memory
    if (typeof NodeGrid !== 'undefined' && Array.isArray(NodeGrid._nodes)) {
      NodeGrid._nodes.forEach(otherNode => {
        if (otherNode.id === id) return;

        const normalizedId = otherNode.id.toLowerCase();
        const isAlreadyLinked = relationships.some(rel => {
          if (!rel.href) return false;
          const cleanedHref = rel.href.toLowerCase().replace(/^nodes\//, '').replace(/^\.\//, '').replace(/\.html$/, '');
          return cleanedHref === normalizedId;
        });

        if (isAlreadyLinked) return;

        const escapedId = otherNode.id.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
        const escapedTitle = otherNode.title.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');

        const idRegex = new RegExp('\\b' + escapedId + '\\b', 'i');
        const titleRegex = new RegExp('\\b' + escapedTitle + '\\b', 'i');

        if (idRegex.test(memory) || titleRegex.test(memory)) {
          let relType = 'references';
          if (otherNode.type === 'rule') relType = 'depends-on';
          else if (otherNode.type === 'persona') relType = 'extends';

          relationships.push({
            href: `nodes/${otherNode.id}.html`,
            type: relType,
            label: otherNode.title
          });
          IPCLog.append('info', `Auto-linked references to node: ${otherNode.title} (${otherNode.id})`);
        }
      });
    }

    const nodeData = {
      id,
      title,
      type,
      authority,
      relevance,
      tags,
      description,
      memoryAuthority,
      memory,
      relationships,
      created: this.activeNode ? this.activeNode.created : new Date().toISOString(),
      author: this.activeNode ? this.activeNode.author : 'human',
      version: this.activeNode ? this.activeNode.version : '1.0.0'
    };

    const generatedHtml = serializeNodeToHTML(nodeData);
    const fileName = `nodes/${id}.html`;

    try {
      const res = await invoke('file:write', { filePath: fileName, data: generatedHtml });
      if (res && res.ok) {
        try {
          await invoke('python:rpc', {
            method: 'memory.save_node',
            params: { node: nodeData }
          });
        } catch (e) {
          console.warn('[NodeEditor] Failed to save node to database:', e);
        }
        IPCLog.append('ok', `Successfully saved node file: ${fileName}`);
        this.hide();
        if (typeof FileExplorer !== 'undefined') FileExplorer.load();
        if (typeof NodeGrid !== 'undefined') NodeGrid.load();
      } else {
        IPCLog.append('error', `Failed to save node: ${JSON.stringify(res)}`);
      }
    } catch (err) {
      IPCLog.append('error', `Error saving node file: ${err.message ?? err}`);
    }
  }
};

const NodeGraph = {
  active: false,
  _canvas: null,
  _ctx: null,
  _nodes: [],
  _links: [],
  _particles: [],
  _transform: { x: 0, y: 0, scale: 1 },
  _draggedNode: null,
  _hoveredNode: null,
  _isPanning: false,
  _panStart: { x: 0, y: 0 },
  _animationFrameId: null,

  // Force-directed physics constants
  K_REPULSION: 3500,
  K_ATTRACTION: 0.05,
  REST_LENGTH: 120,
  K_GRAVITY: 0.015,
  DAMPING: 0.85,

  init() {
    this._canvas = qs('#node-graph-canvas');
    if (!this._canvas) return;
    this._ctx = this._canvas.getContext('2d');

    // Wire up Grid/Graph toggle buttons
    const btnGrid = qs('#btn-view-grid');
    const btnGraph = qs('#btn-view-graph');

    if (btnGrid) {
      btnGrid.addEventListener('click', () => this.switchView('grid'));
    }
    if (btnGraph) {
      btnGraph.addEventListener('click', () => this.switchView('graph'));
    }

    // Floating controls
    const btnZoomIn = qs('#btn-graph-zoom-in');
    const btnZoomOut = qs('#btn-graph-zoom-out');
    const btnZoomFit = qs('#btn-graph-zoom-fit');

    if (btnZoomIn) btnZoomIn.addEventListener('click', () => this.zoom(1.2));
    if (btnZoomOut) btnZoomOut.addEventListener('click', () => this.zoom(0.8));
    if (btnZoomFit) btnZoomFit.addEventListener('click', () => this.resetView());

    // Mouse Listeners
    this._canvas.addEventListener('mousedown', (e) => this.handleMouseDown(e));
    this._canvas.addEventListener('mousemove', (e) => this.handleMouseMove(e));
    this._canvas.addEventListener('mouseup', () => this.handleMouseUp());
    this._canvas.addEventListener('mouseleave', () => this.handleMouseUp());
    this._canvas.addEventListener('wheel', (e) => this.handleWheel(e), { passive: false });
    this._canvas.addEventListener('dblclick', (e) => this.handleDoubleClick(e));

    // Initial resize
    this.resize();
    window.addEventListener('resize', () => this.resize());
  },

  switchView(mode) {
    const gridBtn = qs('#btn-view-grid');
    const graphBtn = qs('#btn-view-graph');
    const gridView = qs('#node-grid');
    const graphView = qs('#node-graph-viewport');

    if (mode === 'graph') {
      if (gridBtn) gridBtn.classList.remove('active');
      if (graphBtn) graphBtn.classList.add('active');
      if (gridView) gridView.hidden = true;
      if (graphView) graphView.hidden = false;

      this.active = true;
      this.resize();
      this.resetView();
      this.startLoop();
      IPCLog.append('info', 'Switched to Node Graph view');
    } else {
      if (gridBtn) gridBtn.classList.add('active');
      if (graphBtn) graphBtn.classList.remove('active');
      if (gridView) gridView.hidden = false;
      if (graphView) graphView.hidden = true;

      this.active = false;
      this.stopLoop();
      IPCLog.append('info', 'Switched to Node Grid view');
    }
  },

  updateGraphNodes(parsedNodes) {
    // Maintain existing node positions and update/add new ones
    const newNodes = parsedNodes.map(pn => {
      const existing = this._nodes.find(n => n.id === pn.id);
      if (existing) {
        existing.title = pn.title;
        existing.type = pn.type;
        existing.authority = pn.authority;
        existing.relevance = pn.relevance;
        existing.memory = pn.memory;
        existing.relationships = pn.relationships;
        existing.tags = pn.tags;
        existing.fileUrl = pn.fileUrl;
        if (existing.activation === undefined) existing.activation = 0.0;
        if (existing.targetActivation === undefined) existing.targetActivation = 0.0;
        return existing;
      } else {
        const angle = Math.random() * Math.PI * 2;
        const radius = 100 + Math.random() * 100;
        return {
          id: pn.id,
          title: pn.title,
          type: pn.type,
          authority: pn.authority,
          relevance: pn.relevance,
          memory: pn.memory,
          relationships: pn.relationships,
          tags: pn.tags,
          fileUrl: pn.fileUrl,
          x: (this._canvas?.width || 800) / 2 + Math.cos(angle) * radius,
          y: (this._canvas?.height || 600) / 2 + Math.sin(angle) * radius,
          vx: 0,
          vy: 0,
          radius: 25,
          pulse: 0,
          activation: 0.0,
          targetActivation: 0.0
        };
      }
    });

    this._nodes = newNodes;

    // Rebuild Links
    const newLinks = [];
    this._nodes.forEach(src => {
      if (src.relationships && Array.isArray(src.relationships)) {
        src.relationships.forEach(rel => {
          if (!rel.href) return;
          const targetId = rel.href.replace(/^nodes\//, '').replace(/^\.\//, '').replace(/\.html$/, '');
          const targetNode = this._nodes.find(n => n.id === targetId);
          if (targetNode) {
            newLinks.push({
              source: src,
              target: targetNode,
              type: rel.type || 'references',
              weight: rel.weight !== undefined ? rel.weight : 0.50,
              label: rel.label || rel.type || 'references'
            });
          }
        });
      }
    });

    this._links = newLinks;
    
    // Clear dead particles
    this._particles = this._particles.filter(p => 
      this._nodes.includes(p.source) && this._nodes.includes(p.target)
    );
  },

  resize() {
    if (!this._canvas) return;
    const parent = this._canvas.parentElement;
    if (!parent) return;
    
    const rect = parent.getBoundingClientRect();
    this._canvas.width = rect.width;
    this._canvas.height = rect.height;
  },

  resetView() {
    if (this._nodes.length === 0) return;
    
    let minX = Infinity, maxX = -Infinity;
    let minY = Infinity, maxY = -Infinity;

    this._nodes.forEach(n => {
      if (n.x < minX) minX = n.x;
      if (n.x > maxX) maxX = n.x;
      if (n.y < minY) minY = n.y;
      if (n.y > maxY) maxY = n.y;
    });

    const graphW = (maxX - minX) || 100;
    const graphH = (maxY - minY) || 100;
    const centerX = minX + graphW / 2;
    const centerY = minY + graphH / 2;

    const scaleX = (this._canvas.width * 0.7) / graphW;
    const scaleY = (this._canvas.height * 0.7) / graphH;
    const scale = Math.min(1.5, Math.max(0.4, Math.min(scaleX, scaleY)));

    this._transform = {
      x: this._canvas.width / 2 - centerX * scale,
      y: this._canvas.height / 2 - centerY * scale,
      scale: scale
    };
  },

  zoom(factor, mX = null, mY = null) {
    const oldScale = this._transform.scale;
    const newScale = Math.min(3.0, Math.max(0.2, oldScale * factor));
    
    const clientX = mX !== null ? mX : this._canvas.width / 2;
    const clientY = mY !== null ? mY : this._canvas.height / 2;

    const gx = (clientX - this._transform.x) / oldScale;
    const gy = (clientY - this._transform.y) / oldScale;

    this._transform.scale = newScale;
    this._transform.x = clientX - gx * newScale;
    this._transform.y = clientY - gy * newScale;
  },

  startLoop() {
    if (this._animationFrameId) return;
    const loop = () => {
      this.stepPhysics();
      this.draw();
      this._animationFrameId = requestAnimationFrame(loop);
    };
    this._animationFrameId = requestAnimationFrame(loop);
  },

  stopLoop() {
    if (this._animationFrameId) {
      cancelAnimationFrame(this._animationFrameId);
      this._animationFrameId = null;
    }
  },

  stepPhysics() {
    if (this._nodes.length === 0) return;

    const width = this._canvas.width;
    const height = this._canvas.height;

    // Repulsion force
    for (let i = 0; i < this._nodes.length; i++) {
      const u = this._nodes[i];
      for (let j = i + 1; j < this._nodes.length; j++) {
        const v = this._nodes[j];
        const dx = v.x - u.x;
        const dy = v.y - u.y;
        const distSq = dx * dx + dy * dy;
        const dist = Math.sqrt(distSq) || 0.1;
        
        if (dist < 300) {
          const force = this.K_REPULSION / (distSq + 100);
          const fx = (dx / dist) * force;
          const fy = (dy / dist) * force;
          u.vx -= fx;
          u.vy -= fy;
          v.vx += fx;
          v.vy += fy;
        }
      }
    }

    // Attraction force
    this._links.forEach(link => {
      const u = link.source;
      const v = link.target;
      const dx = v.x - u.x;
      const dy = v.y - u.y;
      const dist = Math.sqrt(dx * dx + dy * dy) || 0.1;
      
      const force = this.K_ATTRACTION * (dist - this.REST_LENGTH);
      const fx = (dx / dist) * force;
      const fy = (dy / dist) * force;
      
      u.vx += fx;
      u.vy += fy;
      v.vx -= fx;
      v.vy -= fy;
    });

    // Central gravity
    const cx = width / 2;
    const cy = height / 2;
    this._nodes.forEach(u => {
      const dx = cx - u.x;
      const dy = cy - u.y;
      u.vx += dx * this.K_GRAVITY;
      u.vy += dy * this.K_GRAVITY;
    });

    // Position & Activation updates
    this._nodes.forEach(u => {
      if (u === this._draggedNode) {
        u.vx = 0;
        u.vy = 0;
      } else {
        u.vx *= this.DAMPING;
        u.vy *= this.DAMPING;
        u.x += u.vx;
        u.y += u.vy;
      }

      if (u.pulse > 0) {
        u.pulse -= 0.05;
        if (u.pulse < 0) u.pulse = 0;
      }

      if (u.activation === undefined) u.activation = 0.0;
      if (u.targetActivation === undefined) u.targetActivation = 0.0;
      
      // Smooth interpolation toward target activation
      u.activation += (u.targetActivation - u.activation) * 0.08;
      
      // Slowly decay target activation to model memory fade over time
      u.targetActivation *= 0.998;
    });

    // Particle updates
    this._particles.forEach(p => {
      p.t += p.speed;
      if (p.t >= 1) {
        p.t = 1;
        p.target.pulse = 1;
      }
    });

    this._particles = this._particles.filter(p => p.t < 1);
  },

  draw() {
    const ctx = this._ctx;
    if (!ctx) return;

    ctx.clearRect(0, 0, this._canvas.width, this._canvas.height);

    ctx.save();
    ctx.translate(this._transform.x, this._transform.y);
    ctx.scale(this._transform.scale, this._transform.scale);

    // Draw links
    this._links.forEach(link => {
      const u = link.source;
      const v = link.target;
      const isHighlighted = (this._hoveredNode === u || this._hoveredNode === v);

      const lineColor = this.getLinkColor(link.type);
      const alpha = isHighlighted ? 0.65 : 0.09;
      
      let strokeStyle = 'rgba(255, 255, 255, 0.06)';
      if (lineColor.startsWith('#')) {
        const r = parseInt(lineColor.slice(1, 3), 16);
        const g = parseInt(lineColor.slice(3, 5), 16);
        const b = parseInt(lineColor.slice(5, 7), 16);
        strokeStyle = `rgba(${r}, ${g}, ${b}, ${alpha})`;
      }

      ctx.beginPath();
      ctx.moveTo(u.x, u.y);
      ctx.lineTo(v.x, v.y);
      
      ctx.shadowBlur = isHighlighted ? 8 : 0;
      ctx.shadowColor = lineColor;
      ctx.strokeStyle = strokeStyle;
      
      const weight = link.weight !== undefined ? link.weight : 0.50;
      ctx.lineWidth = (isHighlighted ? 2.8 : 1.4) * (weight * 1.5);
      ctx.stroke();

      this.drawArrow(ctx, u.x, u.y, v.x, v.y, v.radius + 6, isHighlighted, lineColor);
    });

    ctx.shadowBlur = 0;

    // Draw link hover text labels
    this._links.forEach(link => {
      const u = link.source;
      const v = link.target;
      if (this._hoveredNode === u || this._hoveredNode === v) {
        const mx = (u.x + v.x) / 2;
        const my = (u.y + v.y) / 2;
        
        ctx.font = '500 9px Inter, sans-serif';
        ctx.fillStyle = 'rgba(56, 189, 248, 0.9)';
        ctx.textAlign = 'center';
        
        const textWidth = ctx.measureText(link.label).width;
        ctx.fillStyle = 'rgba(13, 17, 32, 0.9)';
        ctx.fillRect(mx - textWidth / 2 - 4, my - 7, textWidth + 8, 14);
        
        ctx.fillStyle = 'rgba(56, 189, 248, 0.95)';
        ctx.fillText(link.label, mx, my + 3);
      }
    });

    // Draw particles
    this._particles.forEach(p => {
      const px = p.source.x + (p.target.x - p.source.x) * p.t;
      const py = p.source.y + (p.target.y - p.source.y) * p.t;

      ctx.beginPath();
      ctx.arc(px, py, p.size || 3.5, 0, Math.PI * 2);
      ctx.shadowBlur = 10;
      ctx.shadowColor = p.color || '#38bdf8';
      ctx.fillStyle = '#e0f2fe';
      ctx.fill();
    });

    ctx.shadowBlur = 0;

    // Draw Nodes
    this._nodes.forEach(node => {
      const isHovered = (this._hoveredNode === node);
      const isDragged = (this._draggedNode === node);
      const hasActivation = (node.activation && node.activation > 0.02);
      
      // Node expands dynamically when activated
      const r = node.radius + (node.pulse * 6) + (isHovered ? 2 : 0) + (hasActivation ? node.activation * 6 : 0);
      const nodeColor = this.getNodeColor(node.type);

      // Draw outer glowing pulsing activation ring
      if (hasActivation) {
        ctx.save();
        ctx.beginPath();
        const pulseFactor = 1.0 + 0.18 * Math.sin(Date.now() * 0.005);
        const outerR = r + 8 + node.activation * 16 * pulseFactor;
        
        ctx.arc(node.x, node.y, outerR, 0, Math.PI * 2);
        ctx.shadowBlur = 15;
        ctx.shadowColor = nodeColor;
        
        const ringAlpha = 0.12 + 0.28 * node.activation;
        let strokeColor = nodeColor;
        if (nodeColor.startsWith('#')) {
          const r_color = parseInt(nodeColor.slice(1, 3), 16);
          const g_color = parseInt(nodeColor.slice(3, 5), 16);
          const b_color = parseInt(nodeColor.slice(5, 7), 16);
          strokeColor = `rgba(${r_color}, ${g_color}, ${b_color}, ${ringAlpha})`;
        }
        ctx.strokeStyle = strokeColor;
        ctx.lineWidth = 2.0 + node.activation * 3.0;
        ctx.stroke();
        ctx.restore();
      }

      ctx.beginPath();
      ctx.arc(node.x, node.y, r, 0, Math.PI * 2);
      
      // Activated nodes get a vibrant semi-transparent fill of their own color
      let fillColor = 'rgba(13, 17, 32, 0.82)';
      if (hasActivation && nodeColor.startsWith('#')) {
        const r_color = parseInt(nodeColor.slice(1, 3), 16);
        const g_color = parseInt(nodeColor.slice(3, 5), 16);
        const b_color = parseInt(nodeColor.slice(5, 7), 16);
        fillColor = `rgba(${r_color}, ${g_color}, ${b_color}, ${0.15 + 0.40 * node.activation})`;
      }
      ctx.fillStyle = fillColor;
      ctx.fill();

      ctx.beginPath();
      ctx.arc(node.x, node.y, r, 0, Math.PI * 2);
      
      // Thicker and glowing borders for active nodes
      ctx.lineWidth = (isHovered || isDragged ? 2.5 : 1.5) + (hasActivation ? node.activation * 3.5 : 0);
      ctx.strokeStyle = nodeColor;
      
      ctx.shadowBlur = (isHovered || isDragged ? 12 : 3) + (hasActivation ? node.activation * 25 : 0);
      ctx.shadowColor = nodeColor;
      ctx.stroke();

      ctx.shadowBlur = 0;

      // Draw node title text
      ctx.save();
      ctx.font = '600 11px Inter, sans-serif';
      if (hasActivation) {
        ctx.shadowBlur = 6;
        ctx.shadowColor = nodeColor;
        ctx.fillStyle = '#ffffff'; // Glow bright white when activated
      } else {
        ctx.fillStyle = 'rgba(255, 255, 255, 0.95)';
      }
      ctx.textAlign = 'center';
      ctx.fillText(node.title, node.x, node.y - r - 8);
      ctx.restore();

      // Draw node type text
      ctx.save();
      if (hasActivation) {
        ctx.fillStyle = nodeColor; // Fills with type color
        ctx.font = '700 8px JetBrains Mono, monospace';
      } else {
        ctx.font = '400 8px JetBrains Mono, monospace';
        ctx.fillStyle = 'rgba(255, 255, 255, 0.4)';
      }
      ctx.textAlign = 'center';
      ctx.fillText(node.type.toUpperCase(), node.x, node.y + 4);
      ctx.restore();
    });

    ctx.restore();
  },

  drawArrow(ctx, fromx, fromy, tox, toy, offset, isHighlighted, color) {
    const angle = Math.atan2(toy - fromy, tox - fromx);
    const x = tox - offset * Math.cos(angle);
    const y = toy - offset * Math.sin(angle);
    const size = isHighlighted ? 8 : 6;

    ctx.beginPath();
    ctx.moveTo(x, y);
    ctx.lineTo(x - size * Math.cos(angle - Math.PI / 6), y - size * Math.sin(angle - Math.PI / 6));
    ctx.lineTo(x - size * Math.cos(angle + Math.PI / 6), y - size * Math.sin(angle + Math.PI / 6));
    ctx.closePath();
    
    let fillStyle = 'rgba(255, 255, 255, 0.12)';
    if (color) {
      const alpha = isHighlighted ? 0.8 : 0.18;
      if (color.startsWith('#')) {
        const r = parseInt(color.slice(1, 3), 16);
        const g = parseInt(color.slice(3, 5), 16);
        const b = parseInt(color.slice(5, 7), 16);
        fillStyle = `rgba(${r}, ${g}, ${b}, ${alpha})`;
      }
    }
    ctx.fillStyle = fillStyle;
    ctx.fill();
  },

  getNodeColor(type) {
    const t = type?.toLowerCase().trim();
    switch (t) {
      case 'concept': return '#38bdf8';
      case 'rule':    return '#f87171';
      case 'persona': return '#a78bfa';
      case 'tool':    return '#34d399';
      case 'memory':  return '#ec4899';
      default:        return '#9ca3af';
    }
  },

  getLinkColor(type) {
    const t = type?.toLowerCase().trim();
    switch (t) {
      case 'depends-on':  return '#38bdf8';
      case 'extends':     return '#a78bfa';
      case 'contradicts': return '#f87171';
      case 'governs':     return '#fbbf24';
      case 'rule':        return '#f87171';
      case 'tool':        return '#34d399';
      default:            return '#e0f2fe';
    }
  },

  triggerPropagation(node) {
    const outgoing = this._links.filter(link => link.source === node);
    outgoing.forEach(link => {
      const color = this.getLinkColor(link.type);
      const weight = link.weight !== undefined ? link.weight : 0.50;
      this._particles.push({
        source: node,
        target: link.target,
        t: 0,
        speed: (0.015 + Math.random() * 0.01) * (weight * 1.5),
        color: color,
        size: 3.0 + weight * 3.0
      });
    });
  },

  applyActivations(activations) {
    this._nodes.forEach(node => {
      const target = activations[node.id] || 0.0;
      node.targetActivation = target;
      if (target > 0.1 && (!node.activation || node.activation < 0.05)) {
        node.pulse = 1.0;
        this.triggerPropagation(node);
      }
    });
  },

  handleMouseDown(e) {
    if (!this._canvas) return;
    const rect = this._canvas.getBoundingClientRect();
    const clientX = e.clientX - rect.left;
    const clientY = e.clientY - rect.top;

    const gx = (clientX - this._transform.x) / this._transform.scale;
    const gy = (clientY - this._transform.y) / this._transform.scale;

    const hitNode = [...this._nodes].reverse().find(node => {
      const dx = node.x - gx;
      const dy = node.y - gy;
      return Math.sqrt(dx*dx + dy*dy) <= node.radius + 5;
    });

    if (hitNode) {
      this._draggedNode = hitNode;
      hitNode.x = gx;
      hitNode.y = gy;
      hitNode.vx = 0;
      hitNode.vy = 0;
      
      IPCLog.append('info', `Node selected: ${hitNode.title}`);
      StatusBar.setActiveAgent(hitNode.title);
      this.triggerPropagation(hitNode);
      if (typeof WebBrowser !== 'undefined' && hitNode.fileUrl) {
        WebBrowser.navigate(hitNode.fileUrl);
      }
    } else {
      this._isPanning = true;
      this._panStart = { x: e.clientX - this._transform.x, y: e.clientY - this._transform.y };
    }
  },

  handleDoubleClick(e) {
    if (!this._canvas) return;
    const rect = this._canvas.getBoundingClientRect();
    const clientX = e.clientX - rect.left;
    const clientY = e.clientY - rect.top;

    const gx = (clientX - this._transform.x) / this._transform.scale;
    const gy = (clientY - this._transform.y) / this._transform.scale;

    const hitNode = [...this._nodes].reverse().find(node => {
      const dx = node.x - gx;
      const dy = node.y - gy;
      return Math.sqrt(dx*dx + dy*dy) <= node.radius + 5;
    });

    if (hitNode) {
      NodeEditor.show(hitNode);
    }
  },

  handleMouseMove(e) {
    if (!this._canvas) return;
    const rect = this._canvas.getBoundingClientRect();
    const clientX = e.clientX - rect.left;
    const clientY = e.clientY - rect.top;

    const gx = (clientX - this._transform.x) / this._transform.scale;
    const gy = (clientY - this._transform.y) / this._transform.scale;

    if (this._draggedNode) {
      this._draggedNode.x = gx;
      this._draggedNode.y = gy;
      this._draggedNode.vx = 0;
      this._draggedNode.vy = 0;
    } else if (this._isPanning) {
      this._transform.x = e.clientX - this._panStart.x;
      this._transform.y = e.clientY - this._panStart.y;
    } else {
      const hoverNode = this._nodes.find(node => {
        const dx = node.x - gx;
        const dy = node.y - gy;
        return Math.sqrt(dx*dx + dy*dy) <= node.radius + 5;
      });

      if (hoverNode !== this._hoveredNode) {
        this._hoveredNode = hoverNode;
        this._canvas.style.cursor = hoverNode ? 'pointer' : 'default';
      }
    }
  },

  handleMouseUp() {
    this._draggedNode = null;
    this._isPanning = false;
  },

  handleWheel(e) {
    e.preventDefault();
    if (!this._canvas) return;
    const rect = this._canvas.getBoundingClientRect();
    const mouseX = e.clientX - rect.left;
    const mouseY = e.clientY - rect.top;
    
    const zoomFactor = e.deltaY < 0 ? 1.1 : 0.9;
    this.zoom(zoomFactor, mouseX, mouseY);
  }
};

/* Helper: HTML-NN document parser */
function parseNodeFromHTML(htmlText, filename) {
  const parser = new DOMParser();
  const doc = parser.parseFromString(htmlText, 'text/html');
  let article = doc.querySelector('article');
  
  if (!article) {
    article = doc.querySelector('.neural-node');
  }
  
  if (!article) {
    throw new Error('No <article> or .neural-node element found in node file.');
  }

  const id = article.getAttribute('data-node-id') || filename.replace('.html', '');
  const type = article.getAttribute('data-node-type') || 'concept';
  const authority = parseFloat(article.getAttribute('data-execution-authority')) || 0.00;
  const memoryAuthority = parseFloat(article.getAttribute('data-memory-authority')) || 0.00;
  const relevance = parseFloat(article.getAttribute('data-weight-relevance')) || 0.50;
  const trustLabel = article.getAttribute('data-trust-label') || 'unverified';
  
  const titleEl = doc.querySelector('#node-title') || doc.querySelector('h1') || doc.querySelector('title');
  const title = titleEl ? titleEl.textContent.trim() : id;

  const memoryEl = doc.querySelector('#semantic-memory p') || doc.querySelector('#semantic-memory');
  const memory = memoryEl ? memoryEl.textContent.trim() : '';

  const relLinks = doc.querySelectorAll('a[data-relationship-type]');
  const relationships = [];
  relLinks.forEach(link => {
    relationships.push({
      href: link.getAttribute('href') || '',
      type: link.getAttribute('data-relationship-type') || 'references',
      weight: parseFloat(link.getAttribute('data-relationship-weight')) || 0.50,
      label: link.textContent.trim()
    });
  });

  let description = '';
  const dlTerms = doc.querySelectorAll('#node-identity dl dt');
  dlTerms.forEach(dt => {
    if (dt.textContent.trim() === 'Memory Description') {
      const dd = dt.nextElementSibling;
      if (dd) description = dd.textContent.trim();
    }
  });

  let tags = [];
  let author = 'agent';
  let created = new Date().toISOString();
  let version = '1.0.0';

  const script = doc.querySelector('#node-state');
  if (script) {
    try {
      const stateObj = JSON.parse(script.textContent);
      tags = stateObj.tags || [];
      if (!description && stateObj.description) {
        description = stateObj.description;
      }
      if (stateObj.metadata) {
        author = stateObj.metadata.author || author;
        created = stateObj.metadata.created || created;
        version = stateObj.metadata.version || version;
      }
    } catch (e) {
      console.warn('[NodeParser] failed to parse JSON state block:', e);
    }
  }

  return {
    id,
    type,
    authority: authority.toFixed(2),
    memoryAuthority: memoryAuthority.toFixed(2),
    relevance: relevance.toFixed(2),
    trustLabel,
    title,
    memory,
    relationships,
    tags,
    description,
    author,
    created,
    version,
    filename
  };
}

/* Helper: HTML-NN document serializer */
function serializeNodeToHTML(node) {
  const nowStr = new Date().toISOString();
  const createdStr = node.created || nowStr;
  
  const relsHtml = node.relationships && node.relationships.length > 0
    ? node.relationships.map(rel => {
        const weightAttr = rel.weight !== undefined ? ` data-relationship-weight="${parseFloat(rel.weight).toFixed(2)}"` : '';
        return `      <li>
        <a href="${escapeHtml(rel.href)}" data-relationship-type="${escapeHtml(rel.type)}"${weightAttr}>
          ${escapeHtml(rel.label || rel.href)}
        </a>
      </li>`;
      }).join('\n')
    : '      <!-- No relationship links defined. -->';

  const stateBlock = {
    title: node.title,
    description: node.description || '',
    tags: node.tags || [],
    sections: [
      {
        title: 'Introduction',
        content: node.memory,
        tags: ['intro']
      }
    ],
    metadata: {
      author: node.author || 'human',
      created: createdStr,
      version: node.version || '1.0.0',
      schema: 'neural-node/v2'
    }
  };

  return `<!DOCTYPE html>
<!--
  TALA COGNITIVE ENGINE ROOM — HTML NEURAL NODE
  Generated visually by NodeEditor.
-->
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <meta name="tala-schema" content="neural-node/v2">
  <meta name="tala-node-id"   content="${escapeHtml(node.id)}">
  <meta name="tala-node-type" content="${escapeHtml(node.type)}">
  <meta name="tala-version"   content="${escapeHtml(stateBlock.metadata.version)}">
  <meta name="tala-created"   content="${escapeHtml(stateBlock.metadata.created)}">
  <meta name="tala-author"    content="${escapeHtml(stateBlock.metadata.author)}">

  <title>Neural Node: ${escapeHtml(node.title)}</title>
</head>
<body>
<article
  data-node-id="${escapeHtml(node.id)}"
  data-node-type="${escapeHtml(node.type)}"
  data-execution-authority="${escapeHtml(node.authority)}"
  data-memory-authority="${escapeHtml(node.memoryAuthority || '0.00')}"
  data-weight-relevance="${escapeHtml(node.relevance)}">

  <header id="node-identity">
    <h1 id="node-title">${escapeHtml(node.title)}</h1>
    <dl>
      <dt>Node ID</dt>
      <dd><code>${escapeHtml(node.id)}</code></dd>
      <dt>Type</dt>
      <dd><code>${escapeHtml(node.type)}</code></dd>
      <dt>Execution Authority</dt>
      <dd><code>${escapeHtml(node.authority)}</code></dd>
      <dt>Memory Description</dt>
      <dd>${escapeHtml(node.description || '')}</dd>
      <dt>Version</dt>
      <dd><code>${escapeHtml(stateBlock.metadata.version)}</code></dd>
      <dt>Created</dt>
      <dd><time datetime="${escapeHtml(stateBlock.metadata.created)}">${escapeHtml(stateBlock.metadata.created.split('T')[0])}</time></dd>
    </dl>
  </header>

  <section id="math-weights">
    <h2>Activation Math Weights</h2>
    <div class="math-weights">
      <table>
        <tbody>
          <tr><td>weight_relevance</td><td>${escapeHtml(node.relevance)}</td></tr>
        </tbody>
      </table>
    </div>
  </section>

  <section id="semantic-memory">
    <h2>Semantic Memory</h2>
    <p>${escapeHtml(node.memory)}</p>
  </section>

  <nav id="relationship-anchors" aria-label="Node relationships">
    <h2>Relationships</h2>
    <ul>
${relsHtml}
    </ul>
  </nav>

  <script type="application/json" id="node-state">
${JSON.stringify(stateBlock, null, 2)}
  </script>
</article>
</body>
</html>`;
}

/* Helper: seed initial nodes */
async function seedDefaultNodes() {
  IPCLog.append('info', 'Auto-seeding default onboarding Neural Nodes...');
  try {
    const seed1 = {
    id: 'concept-cognitive-orchestration',
    type: 'concept',
    authority: '0.25',
    relevance: '0.80',
    title: 'Cognitive Orchestration Framework',
    memory: 'Cognitive Orchestration is the process by which multiple local LLM inference models, vector databases, and ledger engines are organized into a singular agent workspace. It dictates that execution authority flows downwards and that all transactions must pre-authenticate against the local Proof-of-Authority blockchain.',
    tags: ['orchestration', 'cognitive', 'sovereignty'],
    author: 'antigravity',
    version: '1.0.0',
    description: 'Core architectural concepts for local AI orchestration.',
    relationships: [
      { href: 'nodes/rule-authority-boundary.html', type: 'depends-on', weight: 0.80, label: 'Governing Rule Boundary' }
    ]
  };

  const seed2 = {
    id: 'rule-authority-boundary',
    type: 'rule',
    authority: '0.75',
    relevance: '1.00',
    title: 'Authority Boundary Invariant',
    memory: 'The Authority Boundary Law states that data-execution-authority attributes on any HTML Neural Node are immutable post-authoring. Invariant checkers must continuously verify that no runtime activation propagation is capable of altering authority parameters, raising an AuthorityBoundaryViolation if attempted.',
    tags: ['invariants', 'rules', 'security'],
    author: 'antigravity',
    version: '1.0.0',
    description: 'System security boundary rule node.',
    relationships: []
  };

  const seed3 = {
    id: 'persona-tala',
    type: 'persona',
    authority: '0.50',
    relevance: '0.60',
    title: 'Tala Engine Room Persona',
    memory: 'Tala is a desktop-native offline AI companion tailored for system orchestration and zero-trust semantic memory graph management. Tala communicates factually, incorporates planetary transit aspect modulations within a strict 10% influence cap, and rejects any commands attempting to bypass Postgres state authority.',
    tags: ['persona', 'astrology', 'hermes'],
    author: 'antigravity',
    version: '1.0.0',
    description: 'Stateless character configuration persona template.',
    relationships: [
      { href: 'nodes/concept-cognitive-orchestration.html', type: 'extends', weight: 0.60, label: 'Orchestration Core' }
    ]
  };

    await invoke('file:write', { filePath: 'nodes/concept-cognitive-orchestration.html', data: serializeNodeToHTML(seed1) });
    await invoke('file:write', { filePath: 'nodes/rule-authority-boundary.html', data: serializeNodeToHTML(seed2) });
    await invoke('file:write', { filePath: 'nodes/persona-tala.html', data: serializeNodeToHTML(seed3) });
    IPCLog.append('ok', 'Onboarding nodes created successfully.');
  } catch (err) {
    IPCLog.append('error', `Failed to write seed nodes: ${err.message ?? err}`);
  }
}


/* ═══════════════════════════════════════════════════════════════════
   GuardrailsManager — level presets, pre/post safety policies
   =================================================================== */
const GuardrailsManager = {
  _initialized: false,
  _category: 'none',
  _rules: {},

  // Presets mapping matching python/harness/guardrails_manager.py
  PRESETS: {
    none: {
      toxicity: false,
      toxicity_threshold: 0.5,
      pii: false,
      pii_fields: [],
      sql_injection: false,
      competitors: false,
      competitor_list: [],
      gibberish: false,
      custom_blocklist: false,
      custom_blocklist_terms: []
    },
    lite: {
      toxicity: true,
      toxicity_threshold: 0.8,
      pii: true,
      pii_fields: ['ssn', 'email'],
      sql_injection: false,
      competitors: false,
      competitor_list: [],
      gibberish: false,
      custom_blocklist: false,
      custom_blocklist_terms: []
    },
    moderate: {
      toxicity: true,
      toxicity_threshold: 0.5,
      pii: true,
      pii_fields: ['ssn', 'email', 'phone', 'credit_card'],
      sql_injection: true,
      competitors: false,
      competitor_list: [],
      gibberish: false,
      custom_blocklist: false,
      custom_blocklist_terms: []
    },
    heavy: {
      toxicity: true,
      toxicity_threshold: 0.3,
      pii: true,
      pii_fields: ['ssn', 'email', 'phone', 'credit_card'],
      sql_injection: true,
      competitors: true,
      competitor_list: ['chatgpt', 'claude', 'gemini', 'openai', 'siri', 'alexa', 'cortana', 'bard'],
      gibberish: true,
      custom_blocklist: false,
      custom_blocklist_terms: []
    }
  },

  init() {
    this._initialized = true;

    // Elements
    this._formEl = qs('#guardrails-form');
    this._cards = document.querySelectorAll('.guardrails-card');
    this._btnReset = qs('#btn-guardrails-reset');
    this._btnSave = qs('#btn-guardrails-save');
    this._toxicityThreshold = qs('#gr-toxicity-threshold');
    this._toxicityThresholdVal = qs('#gr-toxicity-threshold-val');

    // Checkboxes
    this._toxicityEnable = qs('#gr-toxicity-enable');
    this._piiEnable = qs('#gr-pii-enable');
    this._piiEmail = qs('#gr-pii-email');
    this._piiPhone = qs('#gr-pii-phone');
    this._piiSsn = qs('#gr-pii-ssn');
    this._piiCard = qs('#gr-pii-card');
    this._sqlEnable = qs('#gr-sql-enable');
    this._competitorsEnable = qs('#gr-competitors-enable');
    this._competitorsList = qs('#gr-competitors-list');
    this._gibberishEnable = qs('#gr-gibberish-enable');
    this._customBlocklistEnable = qs('#gr-custom-enable');
    this._customBlocklistTerms = qs('#gr-custom-list');

    // 1. Bind Category Card Selection
    this._cards.forEach(card => {
      card.addEventListener('click', () => {
        this._cards.forEach(c => c.classList.remove('active'));
        card.classList.add('active');
        const category = card.dataset.category;
        this.selectCategory(category);
      });
    });

    // 2. Bind Toxicity Threshold Slider
    if (this._toxicityThreshold) {
      this._toxicityThreshold.addEventListener('input', (e) => {
        if (this._toxicityThresholdVal) {
          this._toxicityThresholdVal.textContent = parseFloat(e.target.value).toFixed(2);
        }
      });
    }

    // 3. Bind Reset button
    if (this._btnReset) {
      this._btnReset.addEventListener('click', () => this.load());
    }

    // 4. Bind Save Form
    if (this._formEl) {
      this._formEl.addEventListener('submit', (e) => {
        e.preventDefault();
        this.save();
      });
      // Handle the submit event if we click on save guardrails button
      if (this._btnSave) {
        this._btnSave.addEventListener('click', (e) => {
          e.preventDefault();
          this.save();
        });
      }
    }

    this.load();
  },

  selectCategory(category) {
    this._category = category;

    if (category !== 'custom') {
      // Load preset settings
      const rules = this.PRESETS[category] || this.PRESETS.none;
      this.populateForm(rules);
      // Disable custom form inputs
      if (this._formEl) {
        this._formEl.classList.add('custom-disabled');
      }
    } else {
      // Enable form editing
      if (this._formEl) {
        this._formEl.classList.remove('custom-disabled');
      }
    }
  },

  populateForm(rules) {
    if (this._toxicityEnable) this._toxicityEnable.checked = !!rules.toxicity;
    if (this._toxicityThreshold) {
      this._toxicityThreshold.value = rules.toxicity_threshold !== undefined ? rules.toxicity_threshold : 0.5;
      if (this._toxicityThresholdVal) {
        this._toxicityThresholdVal.textContent = parseFloat(this._toxicityThreshold.value).toFixed(2);
      }
    }

    if (this._piiEnable) this._piiEnable.checked = !!rules.pii;
    const piiFields = rules.pii_fields || [];
    if (this._piiEmail) this._piiEmail.checked = piiFields.includes('email');
    if (this._piiPhone) this._piiPhone.checked = piiFields.includes('phone');
    if (this._piiSsn) this._piiSsn.checked = piiFields.includes('ssn');
    if (this._piiCard) this._piiCard.checked = piiFields.includes('credit_card');

    if (this._sqlEnable) this._sqlEnable.checked = !!rules.sql_injection;
    if (this._competitorsEnable) this._competitorsEnable.checked = !!rules.competitors;
    
    if (this._competitorsList) {
      const compList = rules.competitor_list || [];
      this._competitorsList.value = compList.join(', ');
    }
    
    if (this._gibberishEnable) this._gibberishEnable.checked = !!rules.gibberish;
    
    if (this._customBlocklistEnable) this._customBlocklistEnable.checked = !!rules.custom_blocklist;
    if (this._customBlocklistTerms) {
      const termList = rules.custom_blocklist_terms || [];
      this._customBlocklistTerms.value = termList.join(', ');
    }
  },

  async load() {
    try {
      const response = await invoke('python:rpc', {
        method: 'guardrails.get',
        params: {}
      });

      if (response) {
        this._category = response.category || 'none';
        this._rules = response.rules || {};

        // Update Category card UI active state
        this._cards.forEach(card => {
          card.classList.toggle('active', card.dataset.category === this._category);
        });

        // Set form states
        if (this._category !== 'custom') {
          const presetRules = this.PRESETS[this._category] || this.PRESETS.none;
          this.populateForm(presetRules);
          if (this._formEl) this._formEl.classList.add('custom-disabled');
        } else {
          this.populateForm(this._rules);
          if (this._formEl) this._formEl.classList.remove('custom-disabled');
        }
      }
    } catch (err) {
      console.warn('[GuardrailsManager] Failed to load guardrails settings:', err);
    }
  },

  async save() {
    let rules = {};

    if (this._category === 'custom') {
      // Gather form inputs for custom rules
      const piiFields = [];
      if (this._piiEmail?.checked) piiFields.push('email');
      if (this._piiPhone?.checked) piiFields.push('phone');
      if (this._piiSsn?.checked) piiFields.push('ssn');
      if (this._piiCard?.checked) piiFields.push('credit_card');

      const competitorsListRaw = this._competitorsList?.value || '';
      const competitorList = competitorsListRaw
        .split(',')
        .map(s => s.trim())
        .filter(s => s.length > 0);

      const customBlocklistRaw = this._customBlocklistTerms?.value || '';
      const customBlocklist = customBlocklistRaw
        .split(',')
        .map(s => s.trim())
        .filter(s => s.length > 0);

      rules = {
        toxicity: !!this._toxicityEnable?.checked,
        toxicity_threshold: parseFloat(this._toxicityThreshold?.value || 0.5),
        pii: !!this._piiEnable?.checked,
        pii_fields: piiFields,
        sql_injection: !!this._sqlEnable?.checked,
        competitors: !!this._competitorsEnable?.checked,
        competitor_list: competitorList,
        gibberish: !!this._gibberishEnable?.checked,
        custom_blocklist: !!this._customBlocklistEnable?.checked,
        custom_blocklist_terms: customBlocklist
      };
    } else {
      // Preset categories use their static rules
      rules = this.PRESETS[this._category] || {};
    }

    try {
      const response = await invoke('python:rpc', {
        method: 'guardrails.save',
        params: {
          category: this._category,
          rules: rules
        }
      });

      if (response && response.success) {
        IPCLog.append('ok', `Guardrail settings saved: Category = ${this._category.toUpperCase()}`);
      } else {
        IPCLog.append('error', 'Failed to save guardrails settings');
      }
    } catch (err) {
      console.warn('[GuardrailsManager] Failed to save guardrails settings:', err);
      IPCLog.append('error', `Failed to save guardrails settings: ${err.message ?? err}`);
    }
  }
};


/* ═══════════════════════════════════════════════════════════════════
   WorkflowManager — visual builder, code editor, executor logs
   ═══════════════════════════════════════════════════════════════════ */
const WorkflowManager = {
  _initialized: false,
  _workflows: [],
  _currentWorkflow: null,
  _selectedNodeId: null,
  _activeTab: 'canvas', // 'canvas' | 'json'
  
  // Drag-and-drop / connection state
  _draggedNodeType: null,
  _draggingNodeId: null,
  _dragOffset: { x: 0, y: 0 },
  _connectingSourceNodeId: null,
  _connectingPortType: null, // 'success' | 'failure'
  
  // Websocket listener deregistration hook
  _wsListenerCleanup: null,

  init() {
    this._initialized = true;

    // View Containers
    this._listView = qs('#workflow-list-view');
    this._builderView = qs('#workflow-builder-view');
    this._listContainer = qs('#workflow-list');
    
    // Builder Toolbar
    this._nameInput = qs('#workflow-name-input');
    this._idBadge = qs('#workflow-id-badge');
    this._btnCanvasToggle = qs('#btn-workflow-view-canvas');
    this._btnJsonToggle = qs('#btn-workflow-view-json');
    this._btnSave = qs('#btn-workflow-save');
    this._btnExport = qs('#btn-workflow-export');
    this._btnRun = qs('#btn-workflow-run');
    this._btnClose = qs('#btn-workflow-close');
    this._btnNew = qs('#btn-workflow-new');
    this._btnImportTrigger = qs('#btn-workflow-import-trigger');
    this._fileImport = qs('#workflow-file-import');

    // Canvas & Editor Panels
    this._palette = qs('#workflow-palette');
    this._canvasContainer = qs('#workflow-canvas-container');
    this._canvasSurface = qs('#workflow-canvas');
    this._svgLayer = qs('#workflow-svg-layer');
    this._jsonEditorContainer = qs('#workflow-json-editor-container');
    this._jsonEditor = qs('#workflow-json-editor');
    this._jsonError = qs('#workflow-json-error');

    // Properties panel form
    this._propertiesEmpty = qs('#properties-empty');
    this._propertiesContainer = qs('#properties-form-container');
    this._nodeTypeTitle = qs('#prop-node-type-title');
    this._nodeIdBadge = qs('#prop-node-id-badge');
    this._propertiesForm = qs('#workflow-properties-form');
    
    // Properties Form inputs
    this._propNodeId = qs('#prop-node-id');
    this._propNodeLabel = qs('#prop-node-label');
    this._propTriggerSource = qs('#prop-trigger-source');
    this._propTriggerAuth = qs('#prop-trigger-auth');
    this._propAuthProvider = qs('#prop-auth-provider');
    this._propAuthApikey = qs('#prop-auth-apikey');
    this._propEmailProvider = qs('#prop-email-provider');
    this._propEmailTo = qs('#prop-email-to');
    this._propEmailSubject = qs('#prop-email-subject');
    this._propEmailBody = qs('#prop-email-body');
    this._propScriptCode = qs('#prop-script-code');
    this._propOutputFormat = qs('#prop-output-format');
    this._propConnSuccess = qs('#prop-conn-success');
    this._propConnFailure = qs('#prop-conn-failure');

    // Console logs panel
    this._logsPanel = qs('#workflow-logs-panel');
    this._logsClearBtn = qs('#btn-workflow-logs-clear');
    this._logsToggleBtn = qs('#btn-workflow-logs-toggle');
    this._logsConsole = qs('#workflow-execution-logs');

    // Wires Event Listeners
    if (this._btnNew) this._btnNew.addEventListener('click', () => this.createNewWorkflow());
    if (this._btnClose) this._btnClose.addEventListener('click', () => this.closeBuilder());
    if (this._btnSave) this._btnSave.addEventListener('click', () => this.saveWorkflow());
    if (this._btnExport) this._btnExport.addEventListener('click', () => this.exportWorkflow());
    if (this._btnRun) this._btnRun.addEventListener('click', () => this.runWorkflow());
    
    // Import handlers
    if (this._btnImportTrigger && this._fileImport) {
      this._btnImportTrigger.addEventListener('click', () => this._fileImport.click());
      this._fileImport.addEventListener('change', (e) => this.importWorkflow(e));
    }

    // Toggle editors
    if (this._btnCanvasToggle) this._btnCanvasToggle.addEventListener('click', () => this.switchTab('canvas'));
    if (this._btnJsonToggle) this._btnJsonToggle.addEventListener('click', () => this.switchTab('json'));

    // Console controls
    if (this._logsClearBtn) {
      this._logsClearBtn.addEventListener('click', () => {
        this._logsConsole.innerHTML = '<div class="log-row info-row">Console cleared.</div>';
      });
    }
    if (this._logsToggleBtn && this._logsPanel) {
      this._logsToggleBtn.addEventListener('click', () => {
        if (this._logsPanel.classList.contains('collapsed')) {
          this._logsPanel.className = 'builder-logs-overlay expanded';
          this._logsToggleBtn.textContent = '▼';
        } else {
          this._logsPanel.className = 'builder-logs-overlay collapsed';
          this._logsToggleBtn.textContent = '▲';
        }
      });
    }

    // Palette drag initialization
    if (this._palette) {
      this._palette.querySelectorAll('.palette-item').forEach(item => {
        item.addEventListener('dragstart', (e) => {
          this._draggedNodeType = item.dataset.type;
        });
      });
    }

    // Canvas drop setup
    if (this._canvasContainer) {
      this._canvasContainer.addEventListener('dragover', (e) => e.preventDefault());
      this._canvasContainer.addEventListener('drop', (e) => this.handleCanvasDrop(e));
      
      // Click canvas to deselect node
      this._canvasContainer.addEventListener('mousedown', (e) => {
        if (e.target === this._canvasContainer || e.target === this._canvasSurface || e.target.tagName === 'svg') {
          this.selectNode(null);
          this._connectingSourceNodeId = null;
          this._connectingPortType = null;
        }
      });
    }

    // Form inputs real-time binding
    if (this._propertiesForm) {
      const inputs = [
        this._propNodeLabel, this._propTriggerSource, this._propTriggerAuth,
        this._propAuthProvider, this._propAuthApikey, this._propEmailProvider,
        this._propEmailTo, this._propEmailSubject, this._propEmailBody,
        this._propScriptCode, this._propOutputFormat, this._propConnSuccess,
        this._propConnFailure
      ];
      inputs.forEach(input => {
        if (input) {
          input.addEventListener('input', () => this.saveNodeProperties());
          input.addEventListener('change', () => this.saveNodeProperties());
        }
      });
    }

    // JSON editor sync
    if (this._jsonEditor) {
      this._jsonEditor.addEventListener('input', () => this.syncJsonToWorkflow());
    }

    // Direct goto auth posture handlers
    document.addEventListener('click', (e) => {
      const btn = e.target.closest('#btn-workflow-goto-auth, #btn-workflow-goto-auth-email');
      if (btn) {
        const tabs = document.querySelectorAll('#settings-tab-nav .settings-sub-tab');
        tabs.forEach(t => {
          if (t.dataset.panel === 'auth') {
            t.click();
          }
        });
      }
    });

    // Load initial list
    this.load();

    // Register WebSocket Push Notification Listener via contextBridge tala.on
    try {
      this._wsListenerCleanup = window.tala.on('python:notification', (msg) => {
        if (msg && msg.method === 'workflow.execute.step' && msg.params) {
          this.handleStepExecutionProgress(msg.params);
        }
      });
    } catch (err) {
      console.warn('[WorkflowManager] Failed to register websocket notification listener:', err);
    }
  },

  async load() {
    try {
      const result = await invoke('python:rpc', {
        method: 'workflow.list',
        params: {}
      });
      if (result && result.workflows) {
        this._workflows = result.workflows;
        this.renderList();
      }
    } catch (err) {
      console.error('Failed to load workflows:', err);
      IPCLog.append('error', `Failed to load workflows: ${err.message}`);
    }
  },

  renderList() {
    if (!this._listContainer) return;
    this._listContainer.innerHTML = '';

    if (this._workflows.length === 0) {
      this._listContainer.innerHTML = `
        <div class="provider-list-empty">
          <div class="provider-list-empty-icon">⛓</div>
          <div class="provider-list-empty-text">No workflows configured. Click <strong>+ New Workflow</strong> to get started.</div>
        </div>`;
      return;
    }

    this._workflows.forEach(flow => {
      const card = document.createElement('div');
      card.className = 'agent-card';
      card.style.cursor = 'default';
      
      const nodeCount = flow.nodes ? flow.nodes.length : 0;
      const createdStr = flow.metadata ? (flow.metadata.created_at || '') : '';
      const dateSnippet = createdStr ? new Date(createdStr).toLocaleDateString() : 'Unknown';

      card.innerHTML = `
        <div class="agent-card-header">
          <div class="agent-card-title-row">
            <span class="agent-card-name" style="font-size: 0.85rem;">${flow.name}</span>
          </div>
          <div class="agent-card-actions">
            <button class="btn-card-action btn-card-run" title="Run Pipeline" style="color: var(--accent-teal);">
              ▶
            </button>
            <button class="btn-card-action btn-card-edit" title="Edit Pipeline">
              <svg viewBox="0 0 20 20" fill="currentColor" width="14" height="14">
                <path d="M13.586 3.586a2 2 0 112.828 2.828l-.793.793-2.828-2.828.793-.793zM11.379 5.793L3 14.172V17h2.828l8.38-8.379-2.83-2.829z" />
              </svg>
            </button>
            <button class="btn-card-action btn-card-delete" title="Delete Pipeline">
              <svg viewBox="0 0 20 20" fill="currentColor" width="14" height="14">
                <path fill-rule="evenodd" d="M9 2a1 1 0 00-.894.553L7.382 4H4a1 1 0 000 2v10a2 2 0 002 2h8a2 2 0 002-2V6a1 1 0 100-2h-3.382l-.724-1.447A1 1 0 0011 2H9zM7 8a1 1 0 012 0v6a1 1 0 11-2 0V8zm5-1a1 1 0 00-1 1v6a1 1 0 102 0V8a1 1 0 00-1-1z" clip-rule="evenodd" />
              </svg>
            </button>
          </div>
        </div>
        <div class="agent-card-body" style="font-size: 0.72rem; min-height: 48px; color: var(--text-secondary);">
          Flow ID: <code class="mono">${flow.flow_id}</code><br>
          Nodes: ${nodeCount} configured objects
        </div>
        <div class="agent-card-footer" style="padding-top: var(--space-2); border-top: 1px solid rgba(255,255,255,0.03);">
          <span class="badge" style="background: rgba(0, 242, 254, 0.1); color: #00f2fe; border-color: rgba(0, 242, 254, 0.25);">flow-schema</span>
          <span class="mono" style="font-size: 0.62rem; color: var(--text-muted);">Created: ${dateSnippet}</span>
        </div>
      `;

      // Wire buttons
      const editBtn = card.querySelector('.btn-card-edit');
      if (editBtn) editBtn.addEventListener('click', () => this.openBuilder(flow));
      
      const deleteBtn = card.querySelector('.btn-card-delete');
      if (deleteBtn) deleteBtn.addEventListener('click', () => this.deleteWorkflow(flow.flow_id));

      const runBtn = card.querySelector('.btn-card-run');
      if (runBtn) {
        runBtn.addEventListener('click', () => {
          this.openBuilder(flow);
          setTimeout(() => this.runWorkflow(), 200);
        });
      }

      this._listContainer.appendChild(card);
    });
  },

  createNewWorkflow() {
    const flowId = crypto.randomUUID();
    const newFlow = {
      schema: "tala-flow/1.0",
      flow_id: flowId,
      name: "New Custom Workflow",
      nodes: [
        {
          id: "trigger-" + flowId.substring(0, 4),
          type: "trigger",
          label: "Start Trigger",
          config: { source_node_id: "default", execution_authority: 1.0 },
          connections: { onSuccess: null, onFailure: null },
          position: { x: 100, y: 150 }
        }
      ],
      metadata: {
        created_at: new Date().toISOString()
      }
    };
    this.openBuilder(newFlow);
  },

  openBuilder(workflow) {
    this._currentWorkflow = JSON.parse(JSON.stringify(workflow)); // deep copy
    this._selectedNodeId = null;
    
    // UI transition
    if (this._listView) this._listView.style.display = 'none';
    if (this._builderView) this._builderView.style.display = 'flex';
    
    // Set inputs
    if (this._nameInput) this._nameInput.value = this._currentWorkflow.name;
    if (this._idBadge) this._idBadge.textContent = `ID: ${this._currentWorkflow.flow_id}`;

    this.switchTab('canvas');
    this.renderCanvasNodes();
    this.selectNode(null);

    // Expand console to ready state
    if (this._logsPanel) {
      this._logsPanel.className = 'builder-logs-overlay expanded';
      this._logsToggleBtn.textContent = '▼';
    }
  },

  closeBuilder() {
    this._currentWorkflow = null;
    this._selectedNodeId = null;
    if (this._builderView) this._builderView.style.display = 'none';
    if (this._listView) this._listView.style.display = 'block';
    this.load();
  },

  switchTab(tabId) {
    this._activeTab = tabId;
    
    if (tabId === 'canvas') {
      if (this._btnCanvasToggle) this._btnCanvasToggle.classList.add('active');
      if (this._btnJsonToggle) this._btnJsonToggle.classList.remove('active');
      if (this._canvasContainer) this._canvasContainer.style.display = 'block';
      if (this._jsonEditorContainer) this._jsonEditorContainer.style.display = 'none';
      this.renderCanvasNodes();
    } else {
      if (this._btnCanvasToggle) this._btnCanvasToggle.classList.remove('active');
      if (this._btnJsonToggle) this._btnJsonToggle.classList.add('active');
      if (this._canvasContainer) this._canvasContainer.style.display = 'none';
      if (this._jsonEditorContainer) this._jsonEditorContainer.style.display = 'flex';
      
      // Update Json editor textarea
      if (this._jsonEditor) {
        this._jsonEditor.value = JSON.stringify(this._currentWorkflow, null, 2);
      }
      if (this._jsonError) this._jsonError.style.display = 'none';
    }
  },

  // Drag and drop palette handlers
  handleCanvasDrop(e) {
    e.preventDefault();
    if (!this._draggedNodeType) return;

    const rect = this._canvasSurface.getBoundingClientRect();
    const x = e.clientX - rect.left;
    const y = e.clientY - rect.top;

    const nodeShortId = crypto.randomUUID().substring(0, 4);
    const nodeId = `${this._draggedNodeType}-${nodeShortId}`;
    
    // Build default configs
    const defaults = {
      trigger: { label: "Trigger Step", config: { source_node_id: "default", execution_authority: 1.0 } },
      interactive_prompt: { label: "Interactive Prompt", config: { message: "What is your input?" } },
      auth: { label: "Auth Credentials", config: { provider: "smtp", apiKey: "" } },
      email: { label: "Send Alert Mail", config: { provider: "SMTP", to: "alert@example.com", subject: "Workflow Alert", body: "Warning: step triggered." } },
      custom_script: { label: "Run Script", config: { code: "result = 10 + 20" } },
      output: { label: "Gather Result", config: { format: "json" } }
    };

    const nodeMeta = defaults[this._draggedNodeType] || { label: "Action Node", config: {} };

    const newNode = {
      id: nodeId,
      type: this._draggedNodeType,
      label: nodeMeta.label,
      config: nodeMeta.config,
      connections: { onSuccess: null, onFailure: null },
      position: { x: Math.max(20, x - 80), y: Math.max(20, y - 25) }
    };

    this._currentWorkflow.nodes.push(newNode);
    this._draggedNodeType = null;
    this.renderCanvasNodes();
    this.selectNode(nodeId);
  },

  // Dynamic canvas drawing
  renderCanvasNodes() {
    if (!this._canvasSurface) return;
    this._canvasSurface.innerHTML = '';

    if (!this._currentWorkflow || !this._currentWorkflow.nodes) return;

    this._currentWorkflow.nodes.forEach(node => {
      const card = document.createElement('div');
      card.className = `workflow-node-card${node.id === this._selectedNodeId ? ' selected' : ''}`;
      card.style.left = `${node.position.x}px`;
      card.style.top = `${node.position.y}px`;
      card.dataset.id = node.id;

      card.innerHTML = `
        <div class="node-header-compact node-header-${node.type}">
          <span>${node.type}</span>
          <button class="btn-card-action btn-node-delete" style="padding:1px; margin:-2px;" title="Remove Step">×</button>
        </div>
        <div class="node-body-compact">${node.label}</div>
        <div class="node-ports">
          <!-- Left: Input port socket (receives connection) -->
          <div class="port-socket port-socket-input" data-port="input" title="Input Port"></div>
          <!-- Right: Output connections success/failure -->
          <div style="display: flex; gap: 8px;">
            <div class="port-socket port-socket-success" data-port="success" title="Link onSuccess (Success)"></div>
            <div class="port-socket port-socket-failure" data-port="failure" title="Link onFailure (Failure)"></div>
          </div>
        </div>
      `;

      // Wire dragging logic
      card.addEventListener('mousedown', (e) => {
        if (e.target.classList.contains('port-socket') || e.target.classList.contains('btn-node-delete')) return;
        this._draggingNodeId = node.id;
        const rect = card.getBoundingClientRect();
        this._dragOffset.x = e.clientX - rect.left;
        this._dragOffset.y = e.clientY - rect.top;
        this.selectNode(node.id);
        
        const onMouseMove = (moveEvt) => {
          if (this._draggingNodeId !== node.id) return;
          const canvasRect = this._canvasSurface.getBoundingClientRect();
          let posX = moveEvt.clientX - canvasRect.left - this._dragOffset.x;
          let posY = moveEvt.clientY - canvasRect.top - this._dragOffset.y;
          
          // boundary checks
          posX = Math.max(10, Math.min(2300, posX));
          posY = Math.max(10, Math.min(2300, posY));

          node.position.x = posX;
          node.position.y = posY;
          card.style.left = `${posX}px`;
          card.style.top = `${posY}px`;
          this.drawCables();
        };

        const onMouseUp = () => {
          this._draggingNodeId = null;
          document.removeEventListener('mousemove', onMouseMove);
          document.removeEventListener('mouseup', onMouseUp);
        };

        document.addEventListener('mousemove', onMouseMove);
        document.addEventListener('mouseup', onMouseUp);
      });

      // Wire ports click connectivity
      const sockets = card.querySelectorAll('.port-socket');
      sockets.forEach(sock => {
        sock.addEventListener('click', (e) => {
          e.stopPropagation();
          const portType = sock.dataset.port;
          
          if (portType === 'success' || portType === 'failure') {
            // Start connection link
            this._connectingSourceNodeId = node.id;
            this._connectingPortType = portType;
            IPCLog.append('info', `Connecting Node "${node.label}" onSuccess/onFailure → Select target node's blue input socket.`);
          } else if (portType === 'input' && this._connectingSourceNodeId) {
            // Complete link connection
            if (this._connectingSourceNodeId === node.id) {
              IPCLog.append('warning', 'Cannot link node to itself.');
              return;
            }
            const sourceNode = this._currentWorkflow.nodes.find(n => n.id === this._connectingSourceNodeId);
            if (sourceNode) {
              if (this._connectingPortType === 'success') {
                sourceNode.connections.onSuccess = node.id;
              } else {
                sourceNode.connections.onFailure = node.id;
              }
              IPCLog.append('ok', `Connected: ${sourceNode.label} (${this._connectingPortType}) → ${node.label}`);
              this.renderCanvasNodes();
            }
            this._connectingSourceNodeId = null;
            this._connectingPortType = null;
          }
        });
      });

      // Wire delete node button
      const deleteBtn = card.querySelector('.btn-node-delete');
      if (deleteBtn) {
        deleteBtn.addEventListener('click', (e) => {
          e.stopPropagation();
          // Remove connections pointing to this node
          this._currentWorkflow.nodes.forEach(n => {
            if (n.connections.onSuccess === node.id) n.connections.onSuccess = null;
            if (n.connections.onFailure === node.id) n.connections.onFailure = null;
          });
          this._currentWorkflow.nodes = this._currentWorkflow.nodes.filter(n => n.id !== node.id);
          this.renderCanvasNodes();
          this.selectNode(null);
        });
      }

      this._canvasSurface.appendChild(card);
    });

    this.drawCables();
  },

  drawCables() {
    if (!this._svgLayer) return;
    this._svgLayer.innerHTML = '';

    if (!this._currentWorkflow || !this._currentWorkflow.nodes) return;

    this._currentWorkflow.nodes.forEach(node => {
      const onSuccessTarget = node.connections.onSuccess;
      const onFailureTarget = node.connections.onFailure;

      if (onSuccessTarget) {
        const targetNode = this._currentWorkflow.nodes.find(n => n.id === onSuccessTarget);
        if (targetNode) {
          this.drawBezierLink(node.position, targetNode.position, 'success');
        }
      }
      if (onFailureTarget) {
        const targetNode = this._currentWorkflow.nodes.find(n => n.id === onFailureTarget);
        if (targetNode) {
          this.drawBezierLink(node.position, targetNode.position, 'failure');
        }
      }
    });
  },

  drawBezierLink(pos1, pos2, type) {
    // Port offsets
    const cardWidth = 170;
    const cardHeight = 42;
    
    // Source port (x1, y1)
    const x1 = pos1.x + cardWidth - 8;
    const y1 = pos1.y + cardHeight + 4; // approximate height position of right socket port

    // Target input port (x2, y2)
    const x2 = pos2.x + 8;
    const y2 = pos2.y + cardHeight + 4;

    // Bezier control points
    const cp1x = x1 + 60;
    const cp1y = y1;
    const cp2x = x2 - 60;
    const cp2y = y2;

    const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    path.setAttribute('d', `M ${x1} ${y1} C ${cp1x} ${cp1y}, ${cp2x} ${cp2y}, ${x2} ${y2}`);
    path.setAttribute('class', `workflow-cable workflow-cable-${type}`);
    path.setAttribute('marker-end', `url(#arrow-${type})`);

    this._svgLayer.appendChild(path);
  },

  selectNode(nodeId) {
    this._selectedNodeId = nodeId;
    
    // Refresh selection highlights in DOM
    const cards = this._canvasSurface.querySelectorAll('.workflow-node-card');
    cards.forEach(card => {
      if (card.dataset.id === nodeId) {
        card.classList.add('selected');
      } else {
        card.classList.remove('selected');
      }
    });

    this.updatePropertiesForm();
  },

  updatePropertiesForm() {
    if (!this._currentWorkflow) return;
    
    if (!this._selectedNodeId) {
      if (this._propertiesEmpty) this._propertiesEmpty.style.display = 'block';
      if (this._propertiesContainer) this._propertiesContainer.style.display = 'none';
      return;
    }

    const node = this._currentWorkflow.nodes.find(n => n.id === this._selectedNodeId);
    if (!node) return;

    if (this._propertiesEmpty) this._propertiesEmpty.style.display = 'none';
    if (this._propertiesContainer) this._propertiesContainer.style.display = 'block';

    if (this._nodeTypeTitle) this._nodeTypeTitle.textContent = `${node.type.toUpperCase()} CONFIG`;
    if (this._nodeIdBadge) this._idBadge.textContent = `ID: ${node.id}`;

    // Fill form metadata
    if (this._propNodeId) this._propNodeId.value = node.id;
    if (this._propNodeLabel) this._propNodeLabel.value = node.label;

    // Hide all dynamic config fieldsets
    const fields = document.querySelectorAll('.dynamic-config-fields');
    fields.forEach(f => f.style.display = 'none');

    const config = node.config || {};

    // Show matching section and populate values
    if (node.type === 'trigger') {
      const trigSec = qs('#config-trigger-fields');
      if (trigSec) trigSec.style.display = 'block';
      if (this._propTriggerSource) this._propTriggerSource.value = config.source_node_id || '';
      if (this._propTriggerAuth) this._propTriggerAuth.value = config.execution_authority || 1.0;
      
    } else if (node.type === 'auth') {
      const authSec = qs('#config-auth-fields');
      if (authSec) authSec.style.display = 'block';
      
    } else if (node.type === 'interactive_prompt') {
      const intSec = qs('#config-interactive-fields');
      if (intSec) intSec.style.display = 'block';
      this._propInteractiveMessage = qs('#prop-interactive-message');
      if (this._propInteractiveMessage) this._propInteractiveMessage.value = config.message || '';
      
    } else if (node.type === 'email') {
      const emailSec = qs('#config-email-fields');
      if (emailSec) emailSec.style.display = 'block';
      if (this._propEmailTo) this._propEmailTo.value = config.to || '';
      if (this._propEmailSubject) this._propEmailSubject.value = config.subject || '';
      if (this._propEmailBody) this._propEmailBody.value = config.body || '';
      
    } else if (node.type === 'custom_script') {
      const scriptSec = qs('#config-script-fields');
      if (scriptSec) scriptSec.style.display = 'block';
      if (this._propScriptCode) this._propScriptCode.value = config.code || '';
      
    } else if (node.type === 'output') {
      const outSec = qs('#config-output-fields');
      if (outSec) outSec.style.display = 'block';
      if (this._propOutputFormat) this._propOutputFormat.value = config.format || 'json';
    }

    // Populate Connection Select dropdowns
    this.updateConnectionDropdowns(node);
  },

  updateConnectionDropdowns(currentNode) {
    if (!this._propConnSuccess || !this._propConnFailure) return;

    // Reset dropdowns
    this._propConnSuccess.innerHTML = '<option value="">None (Stop)</option>';
    this._propConnFailure.innerHTML = '<option value="">None (Stop)</option>';

    // Add other nodes in workflow
    this._currentWorkflow.nodes.forEach(node => {
      if (node.id === currentNode.id) return;
      const optText = `${node.label} (${node.id})`;
      
      const optSuccess = document.createElement('option');
      optSuccess.value = node.id;
      optSuccess.textContent = optText;
      if (currentNode.connections.onSuccess === node.id) optSuccess.selected = true;
      this._propConnSuccess.appendChild(optSuccess);

      const optFailure = document.createElement('option');
      optFailure.value = node.id;
      optFailure.textContent = optText;
      if (currentNode.connections.onFailure === node.id) optFailure.selected = true;
      this._propConnFailure.appendChild(optFailure);
    });
  },

  saveNodeProperties() {
    if (!this._currentWorkflow || !this._selectedNodeId) return;

    const node = this._currentWorkflow.nodes.find(n => n.id === this._selectedNodeId);
    if (!node) return;

    // Save label
    node.label = this._propNodeLabel.value.trim() || node.label;
    
    // Save connections
    node.connections.onSuccess = this._propConnSuccess.value || null;
    node.connections.onFailure = this._propConnFailure.value || null;

    // Save dynamic config
    node.config = node.config || {};
    if (node.type === 'trigger') {
      node.config.source_node_id = this._propTriggerSource.value.trim();
      node.config.execution_authority = parseFloat(this._propTriggerAuth.value) || 1.0;
      
    } else if (node.type === 'auth') {
      // Configured directly from the auth section
      
    } else if (node.type === 'interactive_prompt') {
      this._propInteractiveMessage = qs('#prop-interactive-message');
      if (this._propInteractiveMessage) node.config.message = this._propInteractiveMessage.value.trim();
      
    } else if (node.type === 'email') {
      // provider config is resolved dynamically from auth tab settings
      node.config.to = this._propEmailTo.value.trim();
      node.config.subject = this._propEmailSubject.value.trim();
      node.config.body = this._propEmailBody.value;
      
    } else if (node.type === 'custom_script') {
      node.config.code = this._propScriptCode.value;
      
    } else if (node.type === 'output') {
      node.config.format = this._propOutputFormat.value.trim();
    }

    // Refresh canvas labels & lines
    const cardBody = this._canvasSurface.querySelector(`.workflow-node-card[data-id="${node.id}"] .node-body-compact`);
    if (cardBody) cardBody.textContent = node.label;
    
    this.drawCables();
  },

  syncJsonToWorkflow() {
    if (!this._jsonEditor) return;
    try {
      const parsed = JSON.parse(this._jsonEditor.value);
      
      // Basic schema validations
      if (parsed.schema !== 'tala-flow/1.0') {
        this.showJsonError('Invalid schema validation header. Must be "tala-flow/1.0".');
        return;
      }
      if (!parsed.flow_id || !parsed.name || !Array.isArray(parsed.nodes)) {
        this.showJsonError('Missing required properties: flow_id, name, or nodes list.');
        return;
      }

      this._currentWorkflow = parsed;
      if (this._jsonError) this._jsonError.style.display = 'none';
      if (this._nameInput) this._nameInput.value = this._currentWorkflow.name;
    } catch (err) {
      this.showJsonError(`JSON Syntax Error: ${err.message}`);
    }
  },

  showJsonError(msg) {
    if (this._jsonError) {
      this._jsonError.style.display = 'block';
      this._jsonError.textContent = msg;
    }
  },

  async saveWorkflow() {
    if (!this._currentWorkflow) return;
    
    // Update name from input
    if (this._nameInput) {
      this._currentWorkflow.name = this._nameInput.value.trim() || this._currentWorkflow.name;
    }
    
    try {
      const result = await invoke('python:rpc', {
        method: 'workflow.save',
        params: {
          workflow: this._currentWorkflow
        }
      });

      if (result && result.success) {
        IPCLog.append('info', `Saved workflow: "${this._currentWorkflow.name}" and attached HTML Neural Node flow-${result.flow_id}`);
        await this.load();
        
        // Update browser/wiki sidebar explorer to show new nodes
        if (typeof Explorer !== 'undefined' && Explorer._initialized) {
          Explorer.refresh();
        }
      }
    } catch (err) {
      console.error('Failed to save workflow:', err);
      IPCLog.append('error', `Failed to save workflow: ${err.message}`);
    }
  },

  async deleteWorkflow(id) {
    if (!confirm('Are you sure you want to delete this workflow and detach its HTML Neural Node?')) return;
    try {
      const result = await invoke('python:rpc', {
        method: 'workflow.delete',
        params: { flow_id: id }
      });
      if (result && result.success) {
        IPCLog.append('info', `Deleted workflow: "${id}"`);
        this.closeBuilder();
        await this.load();
        
        if (typeof Explorer !== 'undefined' && Explorer._initialized) {
          Explorer.refresh();
        }
      }
    } catch (err) {
      console.error('Failed to delete workflow:', err);
      IPCLog.append('error', `Failed to delete workflow: ${err.message}`);
    }
  },

  async runWorkflow() {
    if (!this._currentWorkflow) return;

    // Clear logs console
    if (this._logsConsole) {
      this._logsConsole.innerHTML = `
        <div class="log-row info-row">Initializing workflow runner for '${this._currentWorkflow.name}'...</div>
      `;
    }

    try {
      const result = await invoke('python:rpc', {
        method: 'workflow.execute',
        params: {
          flow_id: this._currentWorkflow.flow_id,
          inputs: { trigger_source: "manual-runner", timestamp: new Date().toISOString() }
        }
      });

      if (result) {
        const row = document.createElement('div');
        row.className = `log-row ${result.status === 'COMPLETED' ? 'success-row' : 'fail-row'}`;
        row.textContent = `[COMPLETED] Pipeline finished with status ${result.status}. Final output returned.`;
        if (this._logsConsole) {
          this._logsConsole.appendChild(row);
          this._logsConsole.scrollTop = this._logsConsole.scrollHeight;
        }
        IPCLog.append('info', `Executed workflow execution loop: flow-${result.flow_id} status=${result.status}`);
      }
    } catch (err) {
      console.error('Failed to execute workflow:', err);
      const row = document.createElement('div');
      row.className = 'log-row fail-row';
      row.textContent = `[RUN ERROR] Execution runner crash: ${err.message}`;
      if (this._logsConsole) {
        this._logsConsole.appendChild(row);
      }
    }
  },

  handleStepExecutionProgress(stepParams) {
    if (!this._logsConsole) return;
    
    const nodeId = stepParams.node_id;
    const status = stepParams.status;
    const msg = stepParams.message;
    const logs = stepParams.logs || [];

    // Append logs
    logs.forEach(logLine => {
      const row = document.createElement('div');
      row.className = 'log-row info-row';
      row.textContent = `  ↳ ${logLine}`;
      this._logsConsole.appendChild(row);
    });

    const summaryRow = document.createElement('div');
    summaryRow.className = `log-row ${status === 'SUCCESS' ? 'step-row' : 'fail-row'}`;
    summaryRow.textContent = `[STEP COMPLETED] Node: ${nodeId} | Status: ${status} | Result: ${msg}`;
    this._logsConsole.appendChild(summaryRow);

    // Scroll to bottom
    this._logsConsole.scrollTop = this._logsConsole.scrollHeight;
  },

  exportWorkflow() {
    if (!this._currentWorkflow) return;
    try {
      const jsonStr = JSON.stringify(this._currentWorkflow, null, 2);
      const blob = new Blob([jsonStr], { type: 'application/json' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `${this._currentWorkflow.flow_id}.json`;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
      IPCLog.append('info', `Workflow exported: ${this._currentWorkflow.flow_id}.json`);
    } catch (err) {
      IPCLog.append('error', `Failed to export: ${err.message}`);
    }
  },

  importWorkflow(e) {
    const file = e.target.files[0];
    if (!file) return;

    const reader = new FileReader();
    reader.onload = (event) => {
      try {
        const parsed = JSON.parse(event.target.result);
        if (parsed.schema === 'tala-flow/1.0' && parsed.flow_id) {
          this.openBuilder(parsed);
          IPCLog.append('ok', `Workflow JSON imported successfully: "${parsed.name}"`);
        } else {
          alert('Invalid file schema. Must be a valid tala-flow/1.0 JSON.');
        }
      } catch (err) {
        alert(`Failed to parse file: ${err.message}`);
      }
    };
    reader.readAsText(file);
    
    // Clear input
    this._fileImport.value = '';
  }
};


/* ═══════════════════════════════════════════════════════════════════
   21. MissionControl — Developer dashboard panel
   ═══════════════════════════════════════════════════════════════════ */
const MissionControl = {
  init() {
    this._btnRefresh = qs('#btn-mc-refresh');
    this._btnHalt = qs('#btn-mc-halt');
    this._btnScan = qs('#btn-mc-scan');
    this._btnLint = qs('#btn-mc-lint');
    this._btnRevert = qs('#btn-mc-revert');
    this._taskList = qs('#mc-task-list');
    this._console = qs('#mc-diagnostics-console');
    this._telemetry = qs('#mc-telemetry-stream');

    if (this._btnRefresh) this._btnRefresh.addEventListener('click', () => this.refreshAll());
    if (this._btnHalt) this._btnHalt.addEventListener('click', () => this.haltCurrentLoop());
    if (this._btnScan) this._btnScan.addEventListener('click', () => this.runScan());
    if (this._btnLint) this._btnLint.addEventListener('click', () => this.runLint());
    if (this._btnRevert) this._btnRevert.addEventListener('click', () => this.runRevert());

    // Periodically poll task statuses and telemetry streams
    setInterval(() => {
      if (TabManager.activeTabId === 'mission-control') {
        this.loadTasks();
        this.loadTelemetry();
      }
    }, 4000);
    // Subscribe to telemetry stream on startup
    invoke('python:rpc', { method: 'telemetry.stream_subscribe', params: {} }).then(() => {
      IPCLog.append('info', 'Subscribed to live telemetry stream.');
    }).catch(err => {
      console.warn('Failed to subscribe to telemetry stream:', err);
    });

    try {
      window.tala.on('python:notification', (msg) => {
        if (msg && msg.type === 'task_status_update') {
          this.appendTelemetryStream(msg);
          this.loadTasks();
        }
      });
    } catch (err) {
      console.warn('Failed to register telemetry websocket notification listener:', err);
    }

    this.refreshAll();
  },

  async refreshAll() {
    await this.loadTasks();
    await this.loadTelemetry();
    await this.loadGitStatus();
  },

  async loadTasks() {
    if (!this._taskList) return;
    try {
      const response = await invoke('python:rpc', { method: 'task.list', params: {} });
      if (response && response.tasks) {
        if (response.tasks.length === 0) {
          this._taskList.innerHTML = `<div class="empty-state" style="padding: var(--space-4); text-align: center; color: var(--text-muted);">No active tasks in the queue. Start one using <code>/goal "..."</code></div>`;
          return;
        }

        this._taskList.innerHTML = response.tasks.map(t => {
          let statusColor = '#ffc107'; // pending
          if (t.status === 'in_progress') statusColor = '#50fa7b';
          if (t.status === 'failed') statusColor = '#ff5555';
          if (t.status === 'complete') statusColor = '#8be9fd';
          if (t.status === 'blocked') statusColor = '#ff79c6';

          let bodyContent = '';
          if (t.status === 'in_progress') {
            bodyContent = `
              <div style="margin-top: 6px; font-size: 0.75rem; color: #50fa7b; display: flex; align-items: center; gap: 6px;">
                <span style="width: 8px; height: 8px; border-radius: 50%; background: #50fa7b; display: inline-block; box-shadow: 0 0 6px #50fa7b;"></span>
                <span>Active Step: Executing phase tasks & ToT reasoning...</span>
              </div>
            `;
          } else if (t.status === 'complete' && t.result) {
            const phases = t.result.phases_completed || [];
            const summaryText = phases.length > 0 ? `Completed ${phases.length} phase(s): ${phases.slice(-2).join(', ')}` : 'Goal completed successfully.';
            bodyContent = `
              <div style="margin-top: 6px; font-size: 0.75rem; color: #8be9fd;">
                <strong>Final Result:</strong> ${summaryText}
                <details style="margin-top: 4px; color: var(--text-muted); cursor: pointer;">
                  <summary style="font-size: 0.7rem; color: var(--text-muted);">View Execution Steps</summary>
                  <div style="padding-top: 4px; font-size: 0.7rem; font-family: monospace;">${JSON.stringify(t.result, null, 2)}</div>
                </details>
              </div>
            `;
          }

          return `
            <div class="task-item-card" style="padding: var(--space-3); margin-bottom: var(--space-2); background: rgba(255,255,255,0.02); border: 1px solid rgba(255,255,255,0.05); border-radius: var(--radius-sm);">
              <div style="display: flex; justify-content: space-between; align-items: center;">
                <div>
                  <strong style="font-size: 0.85rem; color: var(--text-primary);">${t.description}</strong>
                  <div style="font-size: 0.7rem; color: var(--text-muted);">ID: ${t.id.slice(0,8)}...</div>
                </div>
                <span style="font-size: 0.7rem; padding: 2px 6px; border-radius: 4px; background: ${statusColor}22; color: ${statusColor}; border: 1px solid ${statusColor}44; text-transform: uppercase;">
                  ${t.status}
                </span>
              </div>
              ${bodyContent}
            </div>
          `;
        }).join('');
      }
    } catch (err) {
      console.error('Failed to load tasks:', err);
    }
  },

  async loadTelemetry() {
    if (!this._telemetry) return;
    try {
      const response = await invoke('python:rpc', { method: 'task.get_telemetry', params: {} });
      if (response && response.events) {
        if (response.events.length === 0) {
          this._telemetry.innerHTML = 'No telemetry events recorded yet.';
          return;
        }

        this._telemetry.innerHTML = response.events.map(e => {
          const timestamp = e.created_at ? e.created_at.split('T')[1].slice(0, 8) : '';
          return `<div style="margin-bottom: 4px;"><span style="color: var(--text-muted); font-size: 0.7rem;">[${timestamp}]</span> <span style="color: #ff79c6;">${e.event_type}</span>: ${JSON.stringify(e.payload)}</div>`;
        }).join('');
      }
    } catch (err) {
      console.error('Failed to load telemetry:', err);
    }
  },

  appendTelemetryStream(e) {
    if (!this._telemetry) return;
    if (this._telemetry.innerHTML.includes('No telemetry events recorded yet') || this._telemetry.innerHTML.includes('No events')) {
      this._telemetry.innerHTML = '';
    }
    const timestamp = new Date().toLocaleTimeString();
    const div = document.createElement('div');
    div.style.marginBottom = '4px';
    
    let statusColor = '#ffc107'; // default
    if (e.status === 'complete') statusColor = '#8be9fd';
    if (e.status === 'failed') statusColor = '#ff5555';
    if (e.status === 'in_progress') statusColor = '#50fa7b';
    if (e.status === 'rollback') statusColor = '#ff79c6';
    
    let detailsHtml = '';
    if (e.details) {
      if (e.details.blocked_reason) {
        detailsHtml += `<div style="margin-top: 4px; padding: 4px 8px; border-left: 3px solid #ff79c6; background: rgba(255,121,198,0.1); font-size: 0.75rem;"><strong>BLOCKED:</strong> ${e.details.blocked_reason}</div>`;
      }
      if (e.details.validator_result !== undefined) {
        const valColor = e.details.validator_result ? '#50fa7b' : '#ff5555';
        const valText = e.details.validator_result ? 'PASS' : 'FAIL';
        detailsHtml += `<div style="margin-top: 4px; font-size: 0.75rem;"><span style="color: ${valColor}; border: 1px solid ${valColor}; padding: 1px 4px; border-radius: 3px; font-weight: bold;">VALIDATOR ${valText}</span></div>`;
      }
      if (e.details.evidence) {
        const ev = e.details.evidence;
        let evContent = '';
        if (ev.stdout) evContent += `<div style="color: var(--text-muted); margin-bottom: 2px;">STDOUT:<br>${ev.stdout}</div>`;
        if (ev.stderr) evContent += `<div style="color: #ff5555; margin-bottom: 2px;">STDERR:<br>${ev.stderr}</div>`;
        if (ev.error) evContent += `<div style="color: #ff5555; margin-bottom: 2px;">ERROR:<br>${ev.error}</div>`;
        if (evContent) {
          detailsHtml += `<div style="margin-top: 4px; padding: 6px; background: rgba(0,0,0,0.3); border: 1px solid rgba(255,255,255,0.05); border-radius: 4px; font-family: var(--font-mono); font-size: 0.7rem; white-space: pre-wrap; word-break: break-all; max-height: 150px; overflow-y: auto;">${evContent}</div>`;
        }
      }
    }
    
    div.innerHTML = `<span style="color: var(--text-muted); font-size: 0.7rem;">[${timestamp}]</span> <span style="color: ${statusColor}; font-weight: 600; text-transform: uppercase;">${e.status}</span> | <strong>${e.phase}</strong>: ${e.message}${detailsHtml}`;
    
    this._telemetry.appendChild(div);
    this._telemetry.scrollTop = this._telemetry.scrollHeight;
  },

  async loadGitStatus() {
    try {
      const result = await invoke('git:status');
      if (result && result.ok) {
        if (this._console) {
          const statusOutput = result.stdout ? result.stdout.trim() : 'Working tree clean';
          this._console.innerHTML = `=== GIT WORKSPACE STATUS ===\n${statusOutput}`;
        }
      }
    } catch (err) {
      console.warn('Failed to load git status:', err);
    }
  },

  async runScan() {
    if (!this._console) return;
    this._console.innerHTML = 'Scanning project codebase...';
    try {
      const result = await invoke('python:rpc', { method: 'repo.scan', params: {} });
      if (result && result.codebase_map) {
        this.renderCodebase(result.codebase_map);
      } else {
        this._console.innerHTML = `Scan failed: ${JSON.stringify(result)}`;
      }
    } catch (err) {
      this._console.innerHTML = `Scan error: ${err.message}`;
    }
  },

  renderCodebase(map) {
    if (!this._console) return;
    let mapText = '=== APPLICATION ARCHITECTURE MAP ===\n';
    Object.entries(map).forEach(([folder, items]) => {
      mapText += `\n[Folder: ${folder}]\n`;
      if (Array.isArray(items)) {
        items.forEach(it => {
          mapText += `  - ${it.name || it}\n`;
        });
      } else {
        mapText += `  ${JSON.stringify(items, null, 2)}\n`;
      }
    });
    this._console.innerHTML = mapText;
  },

  async runLint() {
    if (!this._console) return;
    this._console.innerHTML = 'Running architecture compliance linter...';
    try {
      const result = await invoke('python:rpc', { method: 'repo.lint', params: {} });
      if (result) {
        if (result.success) {
          this._console.innerHTML = '=== LINTER RESULTS ===\nCompliance check passed with 0 errors.';
        } else {
          this._console.innerHTML = `=== LINTER WARNINGS ===\n${result.errors.join('\n')}`;
        }
      }
    } catch (err) {
      this._console.innerHTML = `Lint error: ${err.message}`;
    }
  },

  async runRevert() {
    if (!this._console) return;
    this._console.innerHTML = 'Reverting working tree to last safe checkpoint...';
    try {
      const result = await invoke('python:rpc', { method: 'repo.restore', params: {} });
      if (result && result.success) {
        this._console.innerHTML = 'Revert successful! Repository working tree restored.';
        await this.loadGitStatus();
      } else {
        this._console.innerHTML = 'Revert failed. Workspace may have no git stash checkpoints.';
      }
    } catch (err) {
      this._console.innerHTML = `Revert error: ${err.message}`;
    }
  },

  async haltCurrentLoop() {
    try {
      const result = await invoke('python:rpc', { method: 'task.list', params: {} });
      if (result && result.tasks && result.tasks.length > 0) {
        const active = result.tasks[0];
        const haltRes = await invoke('python:rpc', { method: 'task.control', params: { task_id: active.id, action: 'abort' } });
        if (haltRes && haltRes.success) {
          alert('Task queue loop halted and emergency lockdown enabled.');
          await this.refreshAll();
        } else {
          alert('Failed to halt task.');
        }
      } else {
        alert('No active task queue running.');
      }
    } catch (err) {
      alert(`Halt loop error: ${err.message}`);
    }
  }
};

/* ═══════════════════════════════════════════════════════════════════
   ContactsManager — Blockchain wallets unique contact directory
   ═══════════════════════════════════════════════════════════════════ */
const ContactsManager = {
  _initialized: false,
  _contacts: [],
  _selectedContact: null,
  _sharePermissionKeys: ['identity', 'about', 'rp_persona', 'roles', 'skills', 'projects', 'connections', 'nodes', 'pii_vault'],

  _firewallDefaultCategories() {
    return ['rp', 'coding', 'identity', 'workflows'];
  },

  _parseFirewallNodeNames(raw) {
    if (!raw) return [];
    return raw
      .split(',')
      .map(item => item.trim())
      .filter(Boolean);
  },

  _formatFirewallNodeNames(categories) {
    const fixed = new Set(this._firewallDefaultCategories());
    return (categories || [])
      .filter(cat => !fixed.has(cat))
      .join(', ');
  },

  init() {
    if (this._initialized) return;

    // Elements
    this._listContainer = qs('#contacts-list-container');
    this._activeWorkspace = qs('#contacts-active-workspace');
    this._noSelection = qs('#contacts-no-selection');
    
    this._activeName = qs('#contacts-active-name');
    this._activeWallet = qs('#contacts-active-wallet');
    this._activeEndpoint = qs('#contacts-active-endpoint');
    
    this._searchInput = qs('#contacts-search-input');
    this._btnAdd = qs('#btn-contacts-add');
    this._btnSave = qs('#btn-contacts-save');
    this._btnConnect = qs('#btn-contacts-connect');
    this._btnDelete = qs('#btn-contacts-delete');

    // Add Modal Elements
    this._modal = qs('#modal-add-contact');
    this._modalClose = qs('#btn-modal-add-contact-close');
    this._modalCancel = qs('#btn-modal-add-contact-cancel');
    this._modalSave = qs('#btn-modal-add-contact-save');
    this._modalUsername = qs('#input-add-contact-username');
    this._modalWallet = qs('#input-add-contact-wallet');
    this._modalEndpoint = qs('#input-add-contact-endpoint');
    this._modalSecret = qs('#input-add-contact-secret');

    // Bind event listeners
    if (this._btnAdd) this._btnAdd.addEventListener('click', () => this.openAddModal());
    if (this._modalClose) this._modalClose.addEventListener('click', () => this.closeAddModal());
    if (this._modalCancel) this._modalCancel.addEventListener('click', () => this.closeAddModal());
    if (this._modalSave) this._modalSave.addEventListener('click', () => this.saveNewContact());
    if (this._btnSave) this._btnSave.addEventListener('click', () => this.savePermissions());
    if (this._btnConnect) this._btnConnect.addEventListener('click', () => this.connectContact());
    if (this._btnDelete) this._btnDelete.addEventListener('click', () => this.deleteContact());
    
    if (this._searchInput) {
      this._searchInput.addEventListener('input', () => this.filterList());
    }

    // Modal backdrop click
    if (this._modal) {
      this._modal.addEventListener('click', (e) => {
        if (e.target === this._modal) this.closeAddModal();
      });
    }

    this._initialized = true;
  },

  async load() {
    this.init();
    await this.fetchContacts();
    this.filterList();
  },

  async fetchContacts() {
    try {
      const resp = await invoke('python:rpc', {
        method: 'contacts.list',
        params: {}
      });
      if (resp && Array.isArray(resp.contacts)) {
        this._contacts = resp.contacts;
      }
    } catch (err) {
      console.error('[ContactsManager] Error loading contacts list:', err);
      IPCLog.append('error', `Failed to retrieve contacts: ${err.message || err}`);
    }
  },

  renderList(filtered = null) {
    if (!this._listContainer) return;
    const items = filtered || this._contacts;

    this._listContainer.innerHTML = '';
    if (items.length === 0) {
      this._listContainer.innerHTML = '<div class="empty-state">No contacts found.</div>';
      return;
    }

    items.forEach(c => {
      const card = document.createElement('div');
      card.className = `agent-card ${this._selectedContact && this._selectedContact.username === c.username ? 'active' : ''}`;
      card.style.cursor = 'pointer';
      card.style.padding = 'var(--space-3)';
      card.style.background = 'rgba(255, 255, 255, 0.01)';
      card.style.border = '1px solid var(--glass-border)';
      card.style.borderRadius = 'var(--radius-md)';
      card.style.marginBottom = 'var(--space-2)';
      card.innerHTML = `
        <div style="display: flex; align-items: center; justify-content: space-between;">
          <strong style="color: var(--accent-blue);">${escapeHtml(c.username)}</strong>
          <span class="badge badge-identity" style="font-size: 0.65rem;">PEER</span>
        </div>
        <div style="font-family: var(--font-mono); font-size: 0.72rem; color: var(--text-muted); text-overflow: ellipsis; overflow: hidden; white-space: nowrap; margin-top: 4px;">
          ${escapeHtml(c.wallet_address)}
        </div>
      `;

      card.addEventListener('click', () => this.selectContact(c));
      this._listContainer.appendChild(card);
    });
  },

  filterList() {
    const q = this._searchInput ? this._searchInput.value.toLowerCase().trim() : '';
    if (!q) {
      this.renderList();
      return;
    }

    const filtered = this._contacts.filter(c => 
      c.username.toLowerCase().includes(q) || 
      c.wallet_address.toLowerCase().includes(q)
    );
    this.renderList(filtered);
  },

  selectContact(contact) {
    this._selectedContact = contact;
    this.renderList();

    if (this._noSelection) this._noSelection.style.display = 'none';
    if (this._activeWorkspace) this._activeWorkspace.style.display = 'flex';

    if (this._activeName) this._activeName.textContent = contact.username;
    if (this._activeWallet) this._activeWallet.value = contact.wallet_address;
    if (this._activeEndpoint) this._activeEndpoint.value = contact.endpoint_url || '';

    // Checkbox defaults to off if permissions empty, else map database fields
    const perms = contact.permissions || {};
    const categories = this._sharePermissionKeys;
    
    categories.forEach(cat => {
      const checkbox = qs(`#share-${cat}`);
      if (checkbox) {
        checkbox.checked = !!perms[cat];
      }
    });
  },

  openAddModal() {
    if (this._modalUsername) this._modalUsername.value = '';
    if (this._modalWallet) this._modalWallet.value = '';
    if (this._modalEndpoint) this._modalEndpoint.value = '';
    if (this._modalSecret) this._modalSecret.value = '';
    if (this._modal) this._modal.hidden = false;
    if (this._modalUsername) this._modalUsername.focus();
  },

  closeAddModal() {
    if (this._modal) this._modal.hidden = true;
  },

  async saveNewContact() {
    let username = this._modalUsername ? this._modalUsername.value.trim() : '';
    const wallet = this._modalWallet ? this._modalWallet.value.trim() : '';
    const endpoint = this._modalEndpoint ? this._modalEndpoint.value.trim() : '';
    const sharedSecret = this._modalSecret ? this._modalSecret.value.trim() : '';

    if (!username) {
      alert('Username is required.');
      return;
    }
    if (!wallet) {
      alert('Wallet address is required.');
      return;
    }

    // Auto prepend @ if not present
    if (!username.startsWith('@')) {
      username = '@' + username;
    }

    // Check local duplicate first
    const dupUser = this._contacts.find(c => c.username.toLowerCase() === username.toLowerCase());
    if (dupUser) {
      alert(`Contact with username ${username} already exists.`);
      return;
    }

    const dupWallet = this._contacts.find(c => c.wallet_address.toLowerCase() === wallet.toLowerCase());
    if (dupWallet) {
      alert(`Contact with wallet address ${wallet} already exists.`);
      return;
    }

    // Default zero-share permissions
    const permissions = {
      identity: false,
      about: false,
      rp_persona: false,
      roles: false,
      skills: false,
      projects: false,
      connections: false,
      nodes: false,
      pii_vault: false
    };

    try {
      const resp = await invoke('python:rpc', {
        method: 'contacts.save',
        params: {
          username,
          wallet_address: wallet,
          endpoint_url: endpoint,
          shared_secret: sharedSecret,
          permissions
        }
      });

      if (resp && resp.success) {
        IPCLog.append('ok', `Successfully added contact ${username}`);
        await this.load();
        
        // Auto-select the newly added contact
        const newlyAdded = this._contacts.find(c => c.username === username);
        if (newlyAdded) {
          this.selectContact(newlyAdded);
        }
        
        this.closeAddModal();
      } else {
        alert(`Failed to save contact: ${JSON.stringify(resp)}`);
      }
    } catch (err) {
      console.error('[ContactsManager] Error saving contact:', err);
      alert(`Failed to save contact: ${err.message || err}`);
    }
  },

  async savePermissions() {
    if (!this._selectedContact) return;

    const username = this._selectedContact.username;
    const wallet = this._selectedContact.wallet_address;
    const endpoint = this._activeEndpoint ? this._activeEndpoint.value.trim() : (this._selectedContact.endpoint_url || '');
    
    // Read checkbox states
    const permissions = {};
    const categories = this._sharePermissionKeys;
    
    categories.forEach(cat => {
      const checkbox = qs(`#share-${cat}`);
      permissions[cat] = checkbox ? checkbox.checked : false;
    });

    try {
      const resp = await invoke('python:rpc', {
        method: 'contacts.save',
        params: {
          username,
          wallet_address: wallet,
          endpoint_url: endpoint,
          permissions
        }
      });

      if (resp && resp.success) {
        IPCLog.append('ok', `Updated permissions for ${username}`);
        
        // Update local object
        this._selectedContact.permissions = permissions;
        this._selectedContact.endpoint_url = endpoint;
        const local = this._contacts.find(c => c.username === username);
        if (local) {
          local.permissions = permissions;
          local.endpoint_url = endpoint;
        }
      } else {
        alert(`Failed to update permissions: ${JSON.stringify(resp)}`);
      }
    } catch (err) {
      console.error('[ContactsManager] Error saving permissions:', err);
      alert(`Failed to save permissions: ${err.message || err}`);
    }
  },

  async connectContact() {
    if (!this._selectedContact) return;
    const username = this._selectedContact.username;
    const endpoint = this._activeEndpoint ? this._activeEndpoint.value.trim() : (this._selectedContact.endpoint_url || '');
    if (!endpoint) {
      alert('Tala peer endpoint is required before connecting.');
      return;
    }

    try {
      await this.savePermissions();
      const resp = await invoke('python:rpc', {
        method: 'p2p.handshake',
        params: {
          username,
          endpoint_url: endpoint
        }
      });
      if (resp && resp.success) {
        IPCLog.append('ok', `P2P handshake with ${username}: ${JSON.stringify(resp.response)}`);
      } else {
        alert(`Handshake failed: ${JSON.stringify(resp)}`);
      }
    } catch (err) {
      console.error('[ContactsManager] Error connecting contact:', err);
      alert(`Failed to connect: ${err.message || err}`);
    }
  },

  async deleteContact() {
    if (!this._selectedContact) return;

    const username = this._selectedContact.username;
    if (!confirm(`Are you sure you want to delete contact ${username}?`)) {
      return;
    }

    try {
      const resp = await invoke('python:rpc', {
        method: 'contacts.delete',
        params: { username }
      });

      if (resp && resp.success) {
        IPCLog.append('info', `Deleted contact ${username}`);
        this._selectedContact = null;
        
        if (this._noSelection) this._noSelection.style.display = 'flex';
        if (this._activeWorkspace) this._activeWorkspace.style.display = 'none';
        
        await this.load();
      } else {
        alert(`Failed to delete contact: ${JSON.stringify(resp)}`);
      }
    } catch (err) {
      console.error('[ContactsManager] Error deleting contact:', err);
      alert(`Failed to delete contact: ${err.message || err}`);
    }
  }
};

/* ═══════════════════════════════════════════════════════════════════
   ProfileManager — Facebook + LinkedIn Scale User Profiles
   ═══════════════════════════════════════════════════════════════════ */
const ProfileManager = {
  _initialized: false,
  _activeProfileId: 'master-user',
  _isEditing: false,
  _chainAutoConnectAttempted: false,
  _chainPromptActive: false,

  init() {
    if (this._initialized) return;

    // Action buttons
    const btnEdit = qs('#btn-profile-edit');
    const btnSave = qs('#btn-profile-save');
    const btnChainUpdate = qs('#btn-profile-chain-update');
    const btnChainDisconnect = qs('#btn-profile-chain-disconnect');

    if (btnEdit) btnEdit.addEventListener('click', () => this.toggleEditMode(true));
    if (btnSave) btnSave.addEventListener('click', () => this.saveActiveProfile());
    if (btnChainUpdate) btnChainUpdate.addEventListener('click', () => this.updateChainConnection());
    if (btnChainDisconnect) btnChainDisconnect.addEventListener('click', () => this.disconnectChain());

    // Sub-tabs navigation inside profiles
    const tabBtns = document.querySelectorAll('#profile-tabs .profile-tab-btn');
    tabBtns.forEach(btn => {
      btn.addEventListener('click', () => {
        tabBtns.forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        const tabName = btn.dataset.tab;
        
        // Hide all panel children
        document.querySelectorAll('#profile-tab-content .profile-tab-panel').forEach(p => {
          p.hidden = true;
          p.classList.remove('active');
        });
        const targetPanel = qs(`#profile-panel-${tabName}`);
        if (targetPanel) {
          targetPanel.hidden = false;
          targetPanel.classList.add('active');
        }
      });
    });

    // List item builders
    const btnAddSkill = qs('#btn-prof-add-skill');
    if (btnAddSkill) btnAddSkill.addEventListener('click', () => this.addSkillRow());

    const btnAddProject = qs('#btn-prof-add-project');
    if (btnAddProject) btnAddProject.addEventListener('click', () => this.addProjectRow());

    const btnAddConn = qs('#btn-prof-add-conn');
    if (btnAddConn) btnAddConn.addEventListener('click', () => this.addConnectionRow());

    const btnAddPii = qs('#btn-prof-add-pii');
    if (btnAddPii) btnAddPii.addEventListener('click', () => this.addPiiRow());

    this._initialized = true;
    this.loadProfile();
  },

  async loadProfile() {
    try {
      const response = await invoke('python:rpc', {
        method: 'profiles.get',
        params: { id: 'master-user' }
      });

      if (response && response.profile) {
        this.renderProfileDetails(response.profile);
      } else {
        await this.loadFromNeuralNodeFallback();
      }
    } catch (err) {
      console.warn('Failed to load profiles.get for master-user, falling back to neural node:', err);
      await this.loadFromNeuralNodeFallback();
    }
  },

  async loadFromNeuralNodeFallback() {
    try {
      const response = await invoke('python:rpc', {
        method: 'identity.get_user_profile',
        params: {}
      });

      if (response && response.success && response.profile) {
        const p = response.profile;
        const name = p.name || 'Steve';
        const username = p.username || 'steve';
        const config = {
          identity: {
            name,
            username,
            type: 'human',
            avatar_url: '',
            cover_url: '',
            headline: '',
            location: '',
            timezone: '',
            language: '',
            pronouns: '',
            nickname: '',
            motto: ''
          },
          about: {
            bio: p.biography || '',
            story: '',
            status: '',
            goals: '',
            values: ''
          },
          roles: {
            title: '',
            company: '',
            department: '',
            education: '',
            work_history: ''
          },
          rp_persona: {
            name: p.rp_name || '',
            class: p.rp_class || '',
            faction: p.rp_faction || '',
            backstory: p.rp_backstory || ''
          },
          pii_vault: {
            legal_name: p.pii_legal_name || p.name || 'Steve Pollard',
            dob: p.pii_dob || p.birthdate || '1974-09-19',
            email: p.pii_email || '',
            phone: p.pii_phone || '',
            registries: p.pii_registries || []
          },
          skills: [],
          projects: [],
          connections: [],
          preferences: {
            tone: p.tone || 'warm_professional',
            notifications: 'all'
          },
          privacy: {}
        };

        // Save fallback profile in database
        const saveRes = await invoke('python:rpc', {
          method: 'profiles.save',
          params: {
            id: 'master-user',
            type: 'human',
            name,
            username,
            config
          }
        });

        if (saveRes && saveRes.success) {
          const profile = {
            id: 'master-user',
            type: 'human',
            name,
            username,
            config
          };
          this.renderProfileDetails(profile);
        }
      }
    } catch (err) {
      console.error('Failed to load profile fallback from neural node:', err);
      IPCLog.append('error', `Failed to load user profile: ${err.message || err}`);
    }
  },

  renderProfileDetails(profile) {
    const ws = qs('#profile-active-workspace');
    if (!ws) return;

    ws.style.display = 'block';

    // Header info
    qs('#profile-display-name').textContent = profile.name;
    qs('#profile-handle-badge').textContent = profile.username ? `@${profile.username}` : '';

    const typeBadge = qs('#profile-type-badge');
    if (typeBadge) {
      typeBadge.textContent = profile.type.toUpperCase();
      typeBadge.className = `badge badge-${profile.type}`;
    }

    const avatar = qs('#profile-avatar-img');
    if (avatar) {
      avatar.src = profile.config.identity?.avatar_url || 'data:image/svg+xml;utf8,<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20" fill="%239ca3af"><path fill-rule="evenodd" d="M10 9a3 3 0 100-6 3 3 0 000 6zm-7 9a7 7 0 1114 0H3z" clip-rule="evenodd" /></svg>';
    }

    // Toggle fields based on type
    const humanSec = qs('#prof-human-roles-section');
    const agentSec = qs('#prof-agent-roles-section');
    const tabRoles = qs('#tab-profile-roles');

    if (profile.type === 'agent') {
      if (humanSec) humanSec.style.display = 'none';
      if (agentSec) agentSec.style.display = 'block';
      if (tabRoles) tabRoles.textContent = 'Operational Persona';
    } else {
      if (humanSec) humanSec.style.display = 'block';
      if (agentSec) agentSec.style.display = 'none';
      if (tabRoles) tabRoles.textContent = 'Roles & Work';
    }

    // Populate Identity Panel
    const config = profile.config || {};
    const idObj = config.identity || {};

    qs('#prof-input-name').value = profile.name || '';
    qs('#prof-input-username').value = profile.username || '';
    qs('#prof-input-avatar').value = idObj.avatar_url || '';
    qs('#prof-input-cover').value = idObj.cover_url || '';
    qs('#prof-input-headline').value = idObj.headline || '';
    qs('#prof-input-location').value = idObj.location || '';
    qs('#prof-input-timezone').value = idObj.timezone || '';
    qs('#prof-input-language').value = idObj.language || '';
    qs('#prof-input-pronouns').value = idObj.pronouns || '';
    qs('#prof-input-nickname').value = idObj.nickname || '';
    qs('#prof-input-motto').value = idObj.motto || '';

    // About Panel
    const about = config.about || {};
    qs('#prof-input-bio').value = about.bio || '';
    qs('#prof-input-story').value = about.story || '';
    qs('#prof-input-status').value = about.status || '';
    qs('#prof-input-goals').value = about.goals || '';
    qs('#prof-input-values').value = about.values || '';

    // Roleplay Persona Panel
    const rpObj = config.rp_persona || {};
    const rpName = qs('#prof-input-rp-name');
    const rpClass = qs('#prof-input-rp-class');
    const rpFaction = qs('#prof-input-rp-faction');
    const rpBackstory = qs('#prof-input-rp-backstory');

    if (rpName) rpName.value = rpObj.name || '';
    if (rpClass) rpClass.value = rpObj.class || '';
    if (rpFaction) rpFaction.value = rpObj.faction || '';
    if (rpBackstory) rpBackstory.value = rpObj.backstory || '';

    // PII Vault Panel
    const piiObj = config.pii_vault || {};
    const piiLegalName = qs('#prof-input-pii-legal-name');
    const piiDob = qs('#prof-input-pii-dob');
    const piiEmail = qs('#prof-input-pii-email');
    const piiPhone = qs('#prof-input-pii-phone');

    if (piiLegalName) piiLegalName.value = piiObj.legal_name || '';
    if (piiDob) piiDob.value = piiObj.dob || '';
    if (piiEmail) piiEmail.value = piiObj.email || '';
    if (piiPhone) piiPhone.value = piiObj.phone || '';

    // Roles Panel (Human / Orgs)
    const roles = config.roles || {};
    qs('#prof-input-role-title').value = roles.title || '';
    qs('#prof-input-role-company').value = roles.company || '';
    qs('#prof-input-role-dept').value = roles.department || '';
    qs('#prof-input-education').value = roles.education || '';
    qs('#prof-input-workhistory').value = roles.work_history || '';

    // Render lists (Skills, Projects, Connections, PII)
    this.renderSkillsList(config.skills || []);
    this.renderProjectsList(config.projects || []);
    this.renderConnectionsList(config.connections || []);
    this.renderPiiList(piiObj.registries || []);

    // Preferences & Visibilities
    const pref = config.preferences || {};
    qs('#prof-input-pref-tone').value = pref.tone || '';
    qs('#prof-input-pref-notif').value = pref.notifications || 'all';

    const priv = config.privacy || {};
    document.querySelectorAll('#profile-panel-preferences .visibility-select').forEach(sel => {
      const section = sel.dataset.section;
      sel.value = priv[section] || 'public';
    });

    // Calculate completeness
    this.calculateCompleteness(profile);
    this.refreshChainStatus();
    this.autoConnectChainIfConfigured(profile);
  },

  renderSkillsList(skills) {
    const container = qs('#prof-skills-container');
    if (!container) return;

    if (skills.length === 0) {
      container.innerHTML = '<div class="empty-state">No structured skills added. Click Add Skill below.</div>';
      return;
    }

    container.innerHTML = skills.map((s, idx) => {
      const disabledAttr = this._isEditing ? '' : 'disabled';
      return `
        <div class="skill-row-card" data-idx="${idx}">
          <input type="text" class="form-control skill-field-name" style="flex: 2;" value="${escapeHtml(s.name || '')}" placeholder="Skill Name" ${disabledAttr}>
          <input type="text" class="form-control skill-field-category" style="flex: 1.5;" value="${escapeHtml(s.category || '')}" placeholder="Category" ${disabledAttr}>
          <select class="form-control skill-field-level" style="flex: 1.5;" ${disabledAttr}>
            <option value="Beginner" ${s.level === 'Beginner' ? 'selected' : ''}>Beginner</option>
            <option value="Intermediate" ${s.level === 'Intermediate' ? 'selected' : ''}>Intermediate</option>
            <option value="Advanced" ${s.level === 'Advanced' ? 'selected' : ''}>Advanced</option>
            <option value="Expert" ${s.level === 'Expert' ? 'selected' : ''}>Expert</option>
          </select>
          <input type="number" min="0" max="100" class="form-control skill-field-confidence" style="width: 70px;" value="${s.confidence || 80}" placeholder="Conf %" ${disabledAttr}>
          ${this._isEditing ? `<button class="skill-row-delete">✕</button>` : ''}
        </div>
      `;
    }).join('');

    if (this._isEditing) {
      container.querySelectorAll('.skill-row-delete').forEach(btn => {
        const row = btn.closest('.skill-row-card');
        const idx = parseInt(row.dataset.idx, 10);
        btn.addEventListener('click', () => {
          this.removeSkillRow(idx);
        });
      });
    }
  },

  renderProjectsList(projects) {
    const container = qs('#prof-projects-container');
    if (!container) return;

    if (projects.length === 0) {
      container.innerHTML = '<div class="empty-state">No projects added yet. Click Add Project.</div>';
      return;
    }

    container.innerHTML = projects.map((p, idx) => {
      const disabledAttr = this._isEditing ? '' : 'disabled';
      return `
        <div class="project-row-card" data-idx="${idx}">
          <div style="display:flex; width:100%; gap:var(--space-2); margin-bottom: 4px;">
            <input type="text" class="form-control proj-field-name" style="flex:2;" value="${escapeHtml(p.name || '')}" placeholder="Project Name" ${disabledAttr}>
            <input type="text" class="form-control proj-field-status" style="flex:1;" value="${escapeHtml(p.status || '')}" placeholder="Status (e.g. Ongoing)" ${disabledAttr}>
          </div>
          <textarea class="form-control proj-field-desc" style="width:100%; margin-bottom: 4px;" rows="2" placeholder="Project Description" ${disabledAttr}>${escapeHtml(p.description || '')}</textarea>
          <div style="display:flex; width:100%; gap:var(--space-2);">
            <input type="text" class="form-control proj-field-role" style="flex:1;" value="${escapeHtml(p.role || '')}" placeholder="Your Role" ${disabledAttr}>
            <input type="text" class="form-control proj-field-skills" style="flex:1.5;" value="${escapeHtml(p.skills_used || '')}" placeholder="Skills Used (comma-separated)" ${disabledAttr}>
          </div>
          ${this._isEditing ? `<button class="project-row-delete">✕</button>` : ''}
        </div>
      `;
    }).join('');

    if (this._isEditing) {
      container.querySelectorAll('.project-row-delete').forEach(btn => {
        const row = btn.closest('.project-row-card');
        const idx = parseInt(row.dataset.idx, 10);
        btn.addEventListener('click', () => {
          this.removeProjectRow(idx);
        });
      });
    }
  },

  renderConnectionsList(conns) {
    const container = qs('#prof-connections-container');
    if (!container) return;

    if (conns.length === 0) {
      container.innerHTML = '<div class="empty-state">No network connections added yet.</div>';
      return;
    }

    container.innerHTML = conns.map((c, idx) => {
      const disabledAttr = this._isEditing ? '' : 'disabled';
      return `
        <div class="conn-row-card" data-idx="${idx}">
          <input type="text" class="form-control conn-field-name" style="flex:2;" value="${escapeHtml(c.name || '')}" placeholder="Connection Name / Handle" ${disabledAttr}>
          <select class="form-control conn-field-type" style="flex:1.5;" ${disabledAttr}>
            <option value="Friend" ${c.type === 'Friend' ? 'selected' : ''}>Friend</option>
            <option value="Coworker" ${c.type === 'Coworker' ? 'selected' : ''}>Coworker</option>
            <option value="Manager" ${c.type === 'Manager' ? 'selected' : ''}>Manager</option>
            <option value="Collaborator" ${c.type === 'Collaborator' ? 'selected' : ''}>Collaborator</option>
            <option value="AI Companion" ${c.type === 'AI Companion' ? 'selected' : ''}>AI Companion</option>
            <option value="Project Agent" ${c.type === 'Project Agent' ? 'selected' : ''}>Project Agent</option>
          </select>
          <input type="number" min="0" max="10" class="form-control conn-field-trust" style="width:75px;" value="${c.trust_level || 5}" placeholder="Trust (0-10)" ${disabledAttr}>
          ${this._isEditing ? `<button class="conn-row-delete">✕</button>` : ''}
        </div>
      `;
    }).join('');

    if (this._isEditing) {
      container.querySelectorAll('.conn-row-delete').forEach(btn => {
        const row = btn.closest('.conn-row-card');
        const idx = parseInt(row.dataset.idx, 10);
        btn.addEventListener('click', () => {
          this.removeConnectionRow(idx);
        });
      });
    }
  },

  renderPiiList(pii) {
    const container = qs('#prof-pii-container');
    if (!container) return;

    if (pii.length === 0) {
      container.innerHTML = '<div class="empty-state">No secure location registries added yet. Click Add Registry Item.</div>';
      return;
    }

    container.innerHTML = pii.map((p, idx) => {
      const disabledAttr = this._isEditing ? '' : 'disabled';
      return `
        <div class="pii-row-card skill-row-card" style="display: flex; gap: var(--space-2); width:100%;" data-idx="${idx}">
          <input type="text" class="form-control pii-field-label" style="flex: 2;" value="${escapeHtml(p.label || '')}" placeholder="Label / Description" ${disabledAttr}>
          <select class="form-control pii-field-type" style="flex: 1.5;" ${disabledAttr}>
            <option value="File Path" ${p.type === 'File Path' ? 'selected' : ''}>File Path</option>
            <option value="Directory Path" ${p.type === 'Directory Path' ? 'selected' : ''}>Directory Path</option>
            <option value="Text Value" ${p.type === 'Text Value' ? 'selected' : ''}>Text Value</option>
            <option value="Key / Token" ${p.type === 'Key / Token' ? 'selected' : ''}>Key / Token</option>
          </select>
          <input type="text" class="form-control pii-field-location" style="flex: 3;" value="${escapeHtml(p.location || '')}" placeholder="Location / Value" ${disabledAttr}>
          ${this._isEditing ? `<button class="pii-row-delete skill-row-delete">✕</button>` : ''}
        </div>
      `;
    }).join('');

    if (this._isEditing) {
      container.querySelectorAll('.pii-row-delete').forEach(btn => {
        const row = btn.closest('.pii-row-card');
        const idx = parseInt(row.dataset.idx, 10);
        btn.addEventListener('click', () => {
          this.removePiiRow(idx);
        });
      });
    }
  },

  toggleEditMode(editing) {
    this._isEditing = editing;

    const btnEdit = qs('#btn-profile-edit');
    const btnSave = qs('#btn-profile-save');

    if (btnEdit) btnEdit.style.display = editing ? 'none' : 'inline-block';
    if (btnSave) btnSave.style.display = editing ? 'inline-block' : 'none';

    // Enable/disable all form controls inside workspace
    document.querySelectorAll('#profile-active-workspace input, #profile-active-workspace textarea, #profile-active-workspace select').forEach(ctrl => {
      if (ctrl.id !== 'prof-input-name' && ctrl.id !== 'prof-input-username') {
        ctrl.disabled = !editing;
      } else {
        if (ctrl.id === 'prof-input-name') ctrl.disabled = !editing;
        else ctrl.disabled = true; // handle locked
      }
    });

    // Also control panel list add buttons
    const btnAddSkill = qs('#btn-prof-add-skill');
    const btnAddProject = qs('#btn-prof-add-project');
    const btnAddConn = qs('#btn-prof-add-conn');
    const btnAddPii = qs('#btn-prof-add-pii');

    if (btnAddSkill) btnAddSkill.disabled = !editing;
    if (btnAddProject) btnAddProject.disabled = !editing;
    if (btnAddConn) btnAddConn.disabled = !editing;
    if (btnAddPii) btnAddPii.disabled = !editing;

    // Re-render sublists so delete buttons show/hide
    if (this._activeProfileId) {
      this.renderSkillsList(this.collectSkillsState());
      this.renderProjectsList(this.collectProjectsState());
      this.renderConnectionsList(this.collectConnectionsState());
      this.renderPiiList(this.collectPiiState());
    }
  },

  addSkillRow() {
    const list = this.collectSkillsState();
    list.push({ name: '', category: '', level: 'Intermediate', confidence: 80 });
    this.renderSkillsList(list);
  },

  removeSkillRow(idx) {
    const list = this.collectSkillsState();
    list.splice(idx, 1);
    this.renderSkillsList(list);
  },

  addProjectRow() {
    const list = this.collectProjectsState();
    list.push({ name: '', status: 'Ongoing', description: '', role: '', skills_used: '' });
    this.renderProjectsList(list);
  },

  removeProjectRow(idx) {
    const list = this.collectProjectsState();
    list.splice(idx, 1);
    this.renderProjectsList(list);
  },

  addConnectionRow() {
    const list = this.collectConnectionsState();
    list.push({ name: '', type: 'Friend', trust_level: 5 });
    this.renderConnectionsList(list);
  },

  removeConnectionRow(idx) {
    const list = this.collectConnectionsState();
    list.splice(idx, 1);
    this.renderConnectionsList(list);
  },

  addPiiRow() {
    const list = this.collectPiiState();
    list.push({ label: '', type: 'Directory Path', location: '' });
    this.renderPiiList(list);
  },

  removePiiRow(idx) {
    const list = this.collectPiiState();
    list.splice(idx, 1);
    this.renderPiiList(list);
  },

  collectSkillsState() {
    const list = [];
    document.querySelectorAll('#prof-skills-container .skill-row-card').forEach(row => {
      const name = row.querySelector('.skill-field-name')?.value.trim() || '';
      const category = row.querySelector('.skill-field-category')?.value.trim() || '';
      const level = row.querySelector('.skill-field-level')?.value || 'Intermediate';
      const confidence = parseInt(row.querySelector('.skill-field-confidence')?.value || '80', 10);
      if (name) {
        list.push({ name, category, level, confidence });
      }
    });
    return list;
  },

  collectProjectsState() {
    const list = [];
    document.querySelectorAll('#prof-projects-container .project-row-card').forEach(row => {
      const name = row.querySelector('.proj-field-name')?.value.trim() || '';
      const status = row.querySelector('.proj-field-status')?.value.trim() || '';
      const description = row.querySelector('.proj-field-desc')?.value.trim() || '';
      const role = row.querySelector('.proj-field-role')?.value.trim() || '';
      const skills_used = row.querySelector('.proj-field-skills')?.value.trim() || '';
      if (name) {
        list.push({ name, status, description, role, skills_used });
      }
    });
    return list;
  },

  collectConnectionsState() {
    const list = [];
    document.querySelectorAll('#prof-connections-container .conn-row-card').forEach(row => {
      const name = row.querySelector('.conn-field-name')?.value.trim() || '';
      const type = row.querySelector('.conn-field-type')?.value || 'Friend';
      const trust_level = parseInt(row.querySelector('.conn-field-trust')?.value || '5', 10);
      if (name) {
        list.push({ name, type, trust_level });
      }
    });
    return list;
  },

  collectPiiState() {
    const list = [];
    document.querySelectorAll('#prof-pii-container .pii-row-card').forEach(row => {
      const label = row.querySelector('.pii-field-label')?.value.trim() || '';
      const type = row.querySelector('.pii-field-type')?.value || 'Directory Path';
      const location = row.querySelector('.pii-field-location')?.value.trim() || '';
      if (label || location) {
        list.push({ label, type, location });
      }
    });
    return list;
  },

  async saveActiveProfile() {
    const name = qs('#prof-input-name').value.trim();
    const username = qs('#prof-input-username').value.trim();

    if (!name) {
      alert('Display Name is required.');
      return;
    }

    const config = {
      identity: {
        name,
        username,
        type: 'human',
        avatar_url: qs('#prof-input-avatar').value.trim(),
        cover_url: qs('#prof-input-cover').value.trim(),
        headline: qs('#prof-input-headline').value.trim(),
        location: qs('#prof-input-location').value.trim(),
        timezone: qs('#prof-input-timezone').value.trim(),
        language: qs('#prof-input-language').value.trim(),
        pronouns: qs('#prof-input-pronouns').value.trim(),
        nickname: qs('#prof-input-nickname').value.trim(),
        motto: qs('#prof-input-motto').value.trim()
      },
      about: {
        bio: qs('#prof-input-bio').value.trim(),
        story: qs('#prof-input-story').value.trim(),
        status: qs('#prof-input-status').value.trim(),
        goals: qs('#prof-input-goals').value.trim(),
        values: qs('#prof-input-values').value.trim()
      },
      rp_persona: {
        name: qs('#prof-input-rp-name') ? qs('#prof-input-rp-name').value.trim() : '',
        class: qs('#prof-input-rp-class') ? qs('#prof-input-rp-class').value.trim() : '',
        faction: qs('#prof-input-rp-faction') ? qs('#prof-input-rp-faction').value.trim() : '',
        backstory: qs('#prof-input-rp-backstory') ? qs('#prof-input-rp-backstory').value.trim() : ''
      },
      pii_vault: {
        legal_name: qs('#prof-input-pii-legal-name') ? qs('#prof-input-pii-legal-name').value.trim() : '',
        dob: qs('#prof-input-pii-dob') ? qs('#prof-input-pii-dob').value : '',
        email: qs('#prof-input-pii-email') ? qs('#prof-input-pii-email').value.trim() : '',
        phone: qs('#prof-input-pii-phone') ? qs('#prof-input-pii-phone').value.trim() : '',
        registries: this.collectPiiState()
      },
      roles: {
        title: qs('#prof-input-role-title').value.trim(),
        company: qs('#prof-input-role-company').value.trim(),
        department: qs('#prof-input-role-dept').value.trim(),
        education: qs('#prof-input-education').value.trim(),
        work_history: qs('#prof-input-workhistory').value.trim(),
        
        personality: qs('#prof-input-personality').value.trim(),
        memory_authority: qs('#prof-input-mem-authority').value,
        dev_phase: qs('#prof-input-dev-phase').value.trim(),
        operating_principles: qs('#prof-input-operating-principles').value.trim(),
        boundaries: qs('#prof-input-boundaries').value.trim()
      },
      skills: this.collectSkillsState(),
      projects: this.collectProjectsState(),
      connections: this.collectConnectionsState(),
      preferences: {
        tone: qs('#prof-input-pref-tone').value.trim(),
        notifications: qs('#prof-input-pref-notif').value
      },
      privacy: {}
    };

    document.querySelectorAll('#profile-panel-preferences .visibility-select').forEach(sel => {
      config.privacy[sel.dataset.section] = sel.value;
    });

    try {
      const response = await invoke('python:rpc', {
        method: 'profiles.save',
        params: {
          id: 'master-user',
          type: 'human',
          name,
          username,
          config
        }
      });

      if (response && response.success) {
        IPCLog.append('ok', `Saved changes to user profile: ${name}`);
        this.toggleEditMode(false);
        await this.loadProfile();
        await this.maybeOfferChainConnection({ id: 'master-user', type: 'human', name, username, config });
      } else {
        alert(response.error || 'Failed to save changes.');
      }
    } catch (err) {
      console.error(err);
      IPCLog.append('error', `Error saving user profile: ${err.message || err}`);
    }
  },

  async refreshChainStatus() {
    const statusEl = qs('#profile-chain-status');
    const detailsEl = qs('#profile-chain-details');
    const disconnectBtn = qs('#btn-profile-chain-disconnect');
    if (!statusEl || !detailsEl) return;

    try {
      const status = await invoke('python:rpc', {
        method: 'chain.connection_get',
        params: {}
      });
      const connection = status?.connection || {};
      if (connection.mode === 'vps') {
        statusEl.textContent = 'VPS Connected';
        detailsEl.textContent = `Automatic Tala VPS pairing is enabled for ${connection.endpoint_url || connection.username || '@tala-vps'}.`;
        if (disconnectBtn) disconnectBtn.disabled = false;
      } else if (connection.mode === 'local') {
        statusEl.textContent = 'Local Only';
        detailsEl.textContent = 'This Tala client is disconnected from the VPS chain and stores locally.';
        if (disconnectBtn) disconnectBtn.disabled = true;
      } else {
        statusEl.textContent = 'Not Set';
        detailsEl.textContent = 'Save your profile, then choose Tala VPS chain or local-only.';
        if (disconnectBtn) disconnectBtn.disabled = true;
      }
    } catch (err) {
      statusEl.textContent = 'Unavailable';
      detailsEl.textContent = `Could not read chain connection: ${err.message || err}`;
      if (disconnectBtn) disconnectBtn.disabled = false;
    }
  },

  async updateChainConnection() {
    const endpoint = prompt('Tala VPS endpoint:', 'wss://tala.example.com')?.trim();
    if (!endpoint) return;
    const wallet = prompt('Tala VPS wallet/address:', 'tala-vps-main')?.trim();
    if (!wallet) return;
    const secret = prompt('Shared secret for this Tala client ↔ Tala VPS pairing:')?.trim();
    if (!secret) return;

    try {
      const resp = await invoke('python:rpc', {
        method: 'chain.connect_vps',
        params: {
          username: '@tala-vps',
          endpoint_url: endpoint,
          wallet_address: wallet,
          shared_secret: secret
        }
      });
      if (resp && resp.success) {
        IPCLog.append('ok', `Updated Tala VPS chain connection: ${endpoint}`);
      } else {
        alert(`Tala VPS update failed: ${JSON.stringify(resp)}`);
      }
    } catch (err) {
      IPCLog.append('error', `Tala VPS update error: ${err.message || err}`);
    } finally {
      await this.refreshChainStatus();
    }
  },

  async disconnectChain() {
    const removeContact = confirm(
      'Disconnect from the Tala VPS chain and stop automatic reconnect?\n\nChoose OK to also remove the @tala-vps contact. Choose Cancel to keep the contact but disable auto-connect.'
    );
    try {
      const resp = await invoke('python:rpc', {
        method: 'chain.disconnect',
        params: { remove_contact: removeContact }
      });
      if (resp && resp.success) {
        this._chainAutoConnectAttempted = true;
        IPCLog.append('info', 'Disconnected from Tala VPS chain. Client is now local-only.');
      } else {
        alert(`Disconnect failed: ${JSON.stringify(resp)}`);
      }
    } catch (err) {
      IPCLog.append('error', `Tala chain disconnect error: ${err.message || err}`);
    } finally {
      await this.refreshChainStatus();
    }
  },

  async maybeOfferChainConnection(profile) {
    if (this._chainPromptActive || !profile || profile.id !== 'master-user') return;
    this._chainPromptActive = true;

    try {
      const status = await invoke('python:rpc', {
        method: 'chain.connection_get',
        params: {}
      });
      const connection = status?.connection || {};
      if (connection.mode && connection.mode !== 'undecided') return;

      const connect = confirm(
        'Your Tala profile is saved.\n\nConnect this Tala client permanently to a Tala VPS chain?\n\nChoose OK to connect to Tala VPS, or Cancel to stay local-only.'
      );

      if (!connect) {
        const localResp = await invoke('python:rpc', {
          method: 'chain.stay_local',
          params: {}
        });
        if (localResp && localResp.success) {
          IPCLog.append('info', 'Tala chain mode set to local-only.');
        }
        return;
      }

      const endpoint = prompt('Tala VPS endpoint:', 'wss://tala.example.com')?.trim();
      if (!endpoint) return;
      const wallet = prompt('Tala VPS wallet/address:', 'tala-vps-main')?.trim();
      if (!wallet) return;
      const secret = prompt('Shared secret for this Tala client ↔ Tala VPS pairing:')?.trim();
      if (!secret) return;

      const resp = await invoke('python:rpc', {
        method: 'chain.connect_vps',
        params: {
          username: '@tala-vps',
          endpoint_url: endpoint,
          wallet_address: wallet,
          shared_secret: secret
        }
      });

      if (resp && resp.success) {
        IPCLog.append('ok', `Tala client permanently connected to VPS chain at ${endpoint}`);
      } else {
        alert(`Tala VPS connection failed: ${JSON.stringify(resp)}`);
      }
    } catch (err) {
      IPCLog.append('error', `Tala chain connection setup failed: ${err.message || err}`);
    } finally {
      this._chainPromptActive = false;
    }
  },

  calculateCompleteness(profile) {
    const config = profile.config || {};
    const identity = config.identity || {};
    const about = config.about || {};
    const rp_persona = config.rp_persona || {};
    const roles = config.roles || {};
    const skills = config.skills || [];
    const projects = config.projects || [];
    const conns = config.connections || [];

    // Fields to check
    const checklist = [
      { key: 'photo', filled: !!identity.avatar_url, label: 'Profile Photo' },
      { key: 'headline', filled: !!identity.headline, label: 'Bio Headline' },
      { key: 'location', filled: !!identity.location, label: 'Location' },
      { key: 'bio', filled: !!about.bio, label: 'Full Biography' },
      { key: 'rp_persona', filled: !!rp_persona.name, label: 'Roleplay Persona Name' },
      { key: 'skills', filled: skills.length > 0, label: 'Add Skills' },
      { key: 'projects', filled: projects.length > 0, label: 'Add Projects' },
      { key: 'connections', filled: conns.length > 0, label: 'Network Connections' }
    ];

    if (profile.type === 'agent') {
      checklist.push({ key: 'personality', filled: !!roles.personality, label: 'AI Personality' });
      checklist.push({ key: 'principles', filled: !!roles.operating_principles, label: 'Operating Principles' });
    } else {
      checklist.push({ key: 'title', filled: !!roles.title, label: 'Job Title' });
      checklist.push({ key: 'work_history', filled: !!roles.work_history, label: 'Work History' });
    }

    const filledCount = checklist.filter(c => c.filled).length;
    const percent = Math.round((filledCount / checklist.length) * 100);

    const scoreText = qs('#profile-completeness-score');
    if (scoreText) scoreText.textContent = `${percent}%`;

    const bar = qs('#profile-completeness-bar');
    if (bar) bar.style.width = `${percent}%`;

    const tips = qs('#profile-completeness-tips');
    if (tips) {
      const missing = checklist.filter(c => !c.filled).map(c => c.label);
      if (missing.length === 0) {
        tips.textContent = 'Profile complete! Maximum reputation score reached. 🌟';
      } else {
        tips.textContent = `Tips to reach 100%: Fill in ${missing.slice(0, 3).join(', ')}${missing.length > 3 ? '...' : ''}.`;
      }
    }
  }
};

/* ═══════════════════════════════════════════════════════════════════
   AGENT BUILDER
   ═══════════════════════════════════════════════════════════════════ */
const AgentBuilder = {
  activeTab: 'workers',
  pollInterval: null,
  activeWorkers: [],

  init() {
    this.bindEvents();
  },

  bindEvents() {
    const tabBtns = document.querySelectorAll('[data-builder-tab]');
    tabBtns.forEach(btn => {
      btn.addEventListener('click', (e) => {
        tabBtns.forEach(b => b.classList.remove('active'));
        e.target.classList.add('active');
        this.switchTab(e.target.dataset.builderTab);
      });
    });

    const observer = new MutationObserver((mutations) => {
      for (const m of mutations) {
        if (m.target.id === 'view-agent-builder') {
          if (!m.target.hidden) {
            this.onShow();
          } else {
            this.onHide();
          }
        }
      }
    });
    const view = qs('#view-agent-builder');
    if (view) observer.observe(view, { attributes: true, attributeFilter: ['hidden'] });
  },

  switchTab(tabId) {
    this.activeTab = tabId;
    document.querySelectorAll('.builder-tab-pane').forEach(el => {
      el.style.display = 'none';
    });
    const activePane = qs(`#builder-tab-${tabId}`);
    if (activePane) {
      // Use original display type if available, else block
      activePane.style.display = tabId === 'topologies' ? 'block' : 'block';
    }

    if (tabId === 'taskboard') this.refreshTaskBoard();
    else if (tabId === 'workers') this.refreshWorkers();
    else if (tabId === 'topologies') this.refreshTopologies();
  },

  onShow() {
    this.refreshWorkers();
    if (this.activeTab === 'taskboard') this.refreshTaskBoard();
    if (this.activeTab === 'topologies') this.refreshTopologies();
    if (this.activeTab === 'runs' && this.monitoredTask) this.pollRunsData();
    if (!this.pollInterval) {
      this.pollInterval = setInterval(() => {
        if (this.activeTab === 'taskboard') this.refreshTaskBoard();
        if (this.activeTab === 'workers') this.refreshWorkers();
        if (this.activeTab === 'runs' && this.monitoredTask) this.pollRunsData();
      }, 5000);
    }
  },

  onHide() {
    if (this.pollInterval) {
      clearInterval(this.pollInterval);
      this.pollInterval = null;
    }
  },

  async refreshWorkers() {
    try {
      const res = await window.tala.invoke('swarm.workers.active', {});
      this.activeWorkers = res.workers || [];
      const listEl = qs('#builder-workers-list');
      if (!listEl) return;
      if (this.activeWorkers.length === 0) {
        listEl.innerHTML = '<div class="empty-state" style="padding: 16px;">No active workers found. Enable them in Settings > Roles.</div>';
        return;
      }
      listEl.innerHTML = this.activeWorkers.map(w => `
        <div class="agent-card" style="padding: var(--space-3); background: rgba(0,0,0,0.2); border: 1px solid rgba(255,255,255,0.1); border-radius: 6px;">
          <div style="display:flex; justify-content: space-between; align-items: baseline;">
            <h3 style="margin: 0 0 4px 0; font-size: 0.9rem;">${w.name} <span style="color:var(--text-muted); font-size: 0.7rem; font-weight: normal; margin-left: 8px;">(${w.id})</span></h3>
            <span style="font-size: 0.7rem; color: var(--status-success);">Active</span>
          </div>
          <div style="font-size: 0.75rem; color: var(--text-muted);">Role: ${w.role || 'Standalone'}</div>
        </div>
      `).join('');
    } catch (e) {
      console.error('Failed to load workers', e);
    }
  },

  async refreshTopologies() {
    try {
      const res = await window.tala.invoke('swarm.topology.list', {});
      const topologies = res.topologies || [];
      const listEl = qs('#topology-list');
      if (!listEl) return;
      
      if (topologies.length === 0) {
        listEl.innerHTML = '<div class="empty-state">No topologies registered.</div>';
        return;
      }
      
      listEl.innerHTML = topologies.map(t => `
        <div class="topology-list-item" data-id="${t.topology_id}" style="padding: var(--space-2); background: rgba(255,255,255,0.05); border-radius: 4px; cursor: pointer; border: 1px solid transparent;">
          <div style="font-weight: bold; font-size: 0.9rem;">${t.name}</div>
          <div style="font-size: 0.75rem; color: var(--text-muted);">${t.topology_id}</div>
        </div>
      `).join('');
      
      listEl.querySelectorAll('.topology-list-item').forEach(item => {
        item.addEventListener('click', () => {
          listEl.querySelectorAll('.topology-list-item').forEach(el => el.style.border = '1px solid transparent');
          item.style.border = '1px solid var(--accent-blue)';
          const t = topologies.find(x => x.topology_id === item.dataset.id);
          this.renderTopologyGraph(t);
        });
      });
      
    } catch (e) {
      console.error('Failed to load topologies', e);
    }
  },

  renderTopologyGraph(topology) {
    const titleEl = qs('#topology-title');
    const startBtn = qs('#btn-topology-start');
    const nodesContainer = qs('#topology-nodes-container');
    const svgOverlay = qs('#topology-edges-svg');
    
    if (!titleEl || !nodesContainer || !svgOverlay) return;
    
    titleEl.textContent = topology.name;
    startBtn.style.display = 'block';
    
    startBtn.onclick = async () => {
      const goal = prompt("Enter the Swarm Goal:");
      if (!goal) return;
      try {
        const res = await window.tala.invoke('swarm.start', { topology_id: topology.topology_id, goal });
        if (res.success) {
          alert('Swarm launched! Parent Task ID: ' + res.task_id);
          this.switchTab('taskboard');
        } else {
          alert('Failed to start swarm: ' + JSON.stringify(res.error));
        }
      } catch (err) {
        alert('RPC Error: ' + err);
      }
    };
    
    // Render Nodes (simple left-to-right flex layout for now)
    nodesContainer.innerHTML = '';
    topology.nodes.forEach(node => {
      const nodeEl = document.createElement('div');
      nodeEl.className = 'topology-node';
      nodeEl.id = `topo-node-${node.id}`;
      nodeEl.style.cssText = 'background: rgba(20,20,30,0.8); border: 1px solid var(--glass-border); border-radius: 8px; padding: var(--space-3); width: 180px; text-align: center; display: flex; flex-direction: column; gap: 4px; box-shadow: 0 4px 6px rgba(0,0,0,0.3); z-index: 2;';
      nodeEl.innerHTML = `
        <div style="font-size: 0.7rem; color: var(--accent-blue); text-transform: uppercase; font-weight: bold;">${node.role}</div>
        <div style="font-size: 0.85rem; font-weight: bold;">${node.id}</div>
        <div style="font-size: 0.7rem; color: var(--text-muted);">${node.description}</div>
      `;
      nodesContainer.appendChild(nodeEl);
    });
    
    // We defer drawing edges to allow DOM to layout the nodes
    setTimeout(() => this.drawTopologyEdges(topology), 50);
    // Redraw on window resize
    window.addEventListener('resize', () => this.drawTopologyEdges(topology));
  },
  
  drawTopologyEdges(topology) {
    const svgOverlay = qs('#topology-edges-svg');
    const graphContainer = qs('#topology-graph');
    if (!svgOverlay || !graphContainer) return;
    
    svgOverlay.innerHTML = '';
    
    const containerRect = graphContainer.getBoundingClientRect();
    
    // Safety check in case edges are undefined
    const edges = topology.edges || [];
    
    edges.forEach(edge => {
      const fromEl = qs(`#topo-node-${edge.from_node}`);
      const toEl = qs(`#topo-node-${edge.to_node}`);
      
      if (!fromEl || !toEl) return;
      
      const fromRect = fromEl.getBoundingClientRect();
      const toRect = toEl.getBoundingClientRect();
      
      // Calculate coordinates relative to the graph container
      const startX = fromRect.right - containerRect.left;
      const startY = fromRect.top + (fromRect.height / 2) - containerRect.top;
      
      const endX = toRect.left - containerRect.left;
      const endY = toRect.top + (toRect.height / 2) - containerRect.top;
      
      // Control points for a smooth cubic bezier curve
      const cpX1 = startX + 50;
      const cpY1 = startY;
      const cpX2 = endX - 50;
      const cpY2 = endY;
      
      const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
      path.setAttribute('d', `M ${startX} ${startY} C ${cpX1} ${cpY1}, ${cpX2} ${cpY2}, ${endX} ${endY}`);
      path.setAttribute('fill', 'none');
      path.setAttribute('stroke', 'rgba(255,255,255,0.2)');
      path.setAttribute('stroke-width', '2');
      path.setAttribute('marker-end', 'url(#arrowhead)');
      
      // Add label for condition
      if (edge.condition && edge.condition !== 'success') {
        const text = document.createElementNS('http://www.w3.org/2000/svg', 'text');
        text.setAttribute('x', (startX + endX) / 2);
        text.setAttribute('y', (startY + endY) / 2 - 10);
        text.setAttribute('fill', 'var(--accent-orange)');
        text.setAttribute('font-size', '10px');
        text.setAttribute('text-anchor', 'middle');
        text.textContent = edge.condition;
        svgOverlay.appendChild(text);
      }
      
      svgOverlay.appendChild(path);
    });
    
    // Add arrowhead defs if not present
    if (!svgOverlay.querySelector('defs')) {
      const defs = document.createElementNS('http://www.w3.org/2000/svg', 'defs');
      defs.innerHTML = `
        <marker id="arrowhead" markerWidth="10" markerHeight="7" refX="9" refY="3.5" orient="auto">
          <polygon points="0 0, 10 3.5, 0 7" fill="rgba(255,255,255,0.2)" />
        </marker>
      `;
      svgOverlay.appendChild(defs);
    }
  },

  async refreshTaskBoard() {
    try {
      const res = await window.tala.invoke('task.list', {});
      const tasks = res.tasks || [];
      
      const cols = ['pending', 'planned', 'ready', 'running', 'review', 'blocked', 'done'];
      cols.forEach(status => {
        const colEl = qs(`#kanban-${status}`);
        if (colEl) colEl.innerHTML = ''; 
      });

      tasks.forEach(t => {
        let status = t.status || 'pending';
        if (!cols.includes(status)) status = 'pending';
        
        const colEl = qs(`#kanban-${status}`);
        if (!colEl) return;

        const card = document.createElement('div');
        card.className = 'kanban-card';
        card.style.cssText = 'background: rgba(255,255,255,0.03); border: 1px solid rgba(255,255,255,0.1); border-radius: 4px; padding: var(--space-2); cursor: pointer; display: flex; flex-direction: column; gap: 8px;';
        
        const shortDesc = t.description.length > 60 ? t.description.substring(0, 60) + '...' : t.description;
        const workerName = t.assigned_agent_id ? t.assigned_agent_id : 'Unassigned';
        
        card.innerHTML = `
          <div style="font-size: 0.7rem; color: var(--text-muted); font-family: var(--font-mono);">${t.id.split('-').pop()}</div>
          <div style="font-size: 0.85rem; line-height: 1.3;">${shortDesc}</div>
          <div style="font-size: 0.7rem; color: var(--accent-blue);">Worker: ${workerName}</div>
          ${t.worktree_path ? `<div style="font-size: 0.65rem; color: var(--status-success);">🗂️ ${t.branch_name}</div>` : ''}
          <div class="card-actions" style="display: flex; gap: 4px; margin-top: 4px;">
            <button class="btn-assign" style="font-size: 0.65rem; padding: 2px 6px; background: rgba(255,255,255,0.1); border: none; border-radius: 2px; color: white; cursor: pointer;">Assign</button>
            ${!t.worktree_path ? `<button class="btn-worktree" style="font-size: 0.65rem; padding: 2px 6px; background: rgba(255,255,255,0.1); border: none; border-radius: 2px; color: white; cursor: pointer;">+ Worktree</button>` : ''}
          </div>
        `;

        const btnAssign = card.querySelector('.btn-assign');
        if (btnAssign) {
          btnAssign.addEventListener('click', (e) => {
            e.stopPropagation();
            const workerId = prompt('Enter Worker ID to assign (e.g. from Workers tab):', t.assigned_agent_id || '');
            if (workerId) {
              window.tala.invoke('task.assign_worker', { task_id: t.id, agent_id: workerId })
                .then(() => this.refreshTaskBoard());
            }
          });
        }

        const btnWorktree = card.querySelector('.btn-worktree');
        if (btnWorktree) {
          btnWorktree.addEventListener('click', (e) => {
            e.stopPropagation();
            window.tala.invoke('task.create_worktree', { task_id: t.id }).then(res => {
              if (res.error) alert(res.error.message);
              else this.refreshTaskBoard();
            });
          });
        }

        colEl.appendChild(card);
        
        // Phase 4: Allow clicking root Swarm tasks to monitor
        if (t.payload && t.payload.topology_id && !t.parent_id) {
          card.style.border = '1px solid var(--accent-blue)';
          card.title = "Click to monitor this Swarm execution";
          card.addEventListener('click', () => {
            this.monitorSwarmRun(t.id, t.payload.topology_id);
          });
        }
      });
    } catch (e) {
      console.error('Failed to load tasks', e);
    }
  },

  async monitorSwarmRun(parentTaskId, topologyId) {
    this.monitoredTask = parentTaskId;
    this.monitoredTopology = topologyId;
    
    // Switch UI
    qs('#runs-empty-state').style.display = 'none';
    qs('#runs-active-state').style.display = 'flex';
    qs('#runs-task-id').textContent = parentTaskId;
    
    // Fetch topology schema
    try {
      const res = await window.tala.invoke('swarm.topology.list', {});
      const topologies = res.topologies || [];
      const topology = topologies.find(t => t.topology_id === topologyId);
      
      if (topology) {
        qs('#runs-topology-title').textContent = `Monitoring: ${topology.name}`;
        
        // Render Nodes (reuse logic from Topologies tab, but into Runs tab containers)
        const nodesContainer = qs('#runs-topology-nodes-container');
        nodesContainer.innerHTML = '';
        topology.nodes.forEach(node => {
          const nodeEl = document.createElement('div');
          nodeEl.className = 'topology-node';
          nodeEl.id = `runs-topo-node-${node.id}`;
          nodeEl.dataset.nodeId = node.id;
          nodeEl.style.cssText = 'background: rgba(20,20,30,0.8); border: 1px solid var(--glass-border); border-radius: 8px; padding: var(--space-3); width: 180px; text-align: center; display: flex; flex-direction: column; gap: 4px; box-shadow: 0 4px 6px rgba(0,0,0,0.3); z-index: 2; transition: all 0.3s ease;';
          nodeEl.innerHTML = `
            <div style="font-size: 0.7rem; color: var(--accent-blue); text-transform: uppercase; font-weight: bold;">${node.role}</div>
            <div style="font-size: 0.85rem; font-weight: bold;">${node.id}</div>
            <div class="node-status" style="font-size: 0.7rem; color: var(--text-muted); margin-top: 4px;">Waiting</div>
          `;
          nodesContainer.appendChild(nodeEl);
        });
        
        // Draw edges slightly delayed
        setTimeout(() => {
          this.drawTopologyEdgesRuns(topology);
        }, 100);
      }
    } catch(e) {
      console.error(e);
    }
    
    // Switch to tab
    this.switchTab('runs');
    this.pollRunsData();
  },
  
  drawTopologyEdgesRuns(topology) {
    const svgOverlay = qs('#runs-topology-edges-svg');
    const graphContainer = qs('#runs-topology-graph');
    if (!svgOverlay || !graphContainer) return;
    
    svgOverlay.innerHTML = '';
    const containerRect = graphContainer.getBoundingClientRect();
    const edges = topology.edges || [];
    
    // Add arrowhead defs
    const defs = document.createElementNS('http://www.w3.org/2000/svg', 'defs');
    defs.innerHTML = `
      <marker id="runs-arrowhead" markerWidth="10" markerHeight="7" refX="9" refY="3.5" orient="auto">
        <polygon points="0 0, 10 3.5, 0 7" fill="rgba(255,255,255,0.2)" />
      </marker>
    `;
    svgOverlay.appendChild(defs);
    
    edges.forEach(edge => {
      const fromEl = qs(`#runs-topo-node-${edge.from_node}`);
      const toEl = qs(`#runs-topo-node-${edge.to_node}`);
      if (!fromEl || !toEl) return;
      
      const fromRect = fromEl.getBoundingClientRect();
      const toRect = toEl.getBoundingClientRect();
      
      const startX = fromRect.right - containerRect.left;
      const startY = fromRect.top + (fromRect.height / 2) - containerRect.top;
      const endX = toRect.left - containerRect.left;
      const endY = toRect.top + (toRect.height / 2) - containerRect.top;
      
      const cpX1 = startX + 50, cpY1 = startY;
      const cpX2 = endX - 50, cpY2 = endY;
      
      const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
      path.setAttribute('d', `M ${startX} ${startY} C ${cpX1} ${cpY1}, ${cpX2} ${cpY2}, ${endX} ${endY}`);
      path.setAttribute('fill', 'none');
      path.setAttribute('stroke', 'rgba(255,255,255,0.2)');
      path.setAttribute('stroke-width', '2');
      path.setAttribute('marker-end', 'url(#runs-arrowhead)');
      svgOverlay.appendChild(path);
      
      if (edge.condition && edge.condition !== 'success') {
        const text = document.createElementNS('http://www.w3.org/2000/svg', 'text');
        text.setAttribute('x', (startX + endX) / 2);
        text.setAttribute('y', (startY + endY) / 2 - 10);
        text.setAttribute('fill', 'var(--accent-orange)');
        text.setAttribute('font-size', '10px');
        text.setAttribute('text-anchor', 'middle');
        text.textContent = edge.condition;
        svgOverlay.appendChild(text);
      }
    });
  },

  async pollRunsData() {
    if (!this.monitoredTask) return;
    
    try {
      // Fetch Tasks to see active child tasks
      const res = await window.tala.invoke('task.list', {});
      const tasks = res.tasks || [];
      
      // Find children of monitored task
      const children = tasks.filter(t => t.parent_id === this.monitoredTask);
      
      // Reset all nodes
      document.querySelectorAll('#runs-topology-nodes-container .topology-node').forEach(el => {
        el.style.border = '1px solid var(--glass-border)';
        el.style.boxShadow = '0 4px 6px rgba(0,0,0,0.3)';
        el.querySelector('.node-status').textContent = 'Inactive';
        el.querySelector('.node-status').style.color = 'var(--text-muted)';
      });
      
      // Highlight active child nodes
      children.forEach(child => {
        if (child.payload && child.payload.node_id) {
          const el = qs(`#runs-topo-node-${child.payload.node_id}`);
          if (el) {
            el.querySelector('.node-status').textContent = child.status.toUpperCase();
            if (['running', 'review'].includes(child.status)) {
              el.style.border = '1px solid var(--status-success)';
              el.style.boxShadow = '0 0 15px rgba(40, 167, 69, 0.4)';
              el.querySelector('.node-status').style.color = 'var(--status-success)';
            } else if (child.status === 'blocked') {
              el.style.border = '1px solid var(--status-error)';
              el.querySelector('.node-status').style.color = 'var(--status-error)';
            }
          }
        }
      });
      
      // Fetch Telemetry (simulated for now since get_telemetry RPC might not be implemented exactly this way,
      // but let's try calling task.get_telemetry)
      const telRes = await window.tala.invoke('task.get_telemetry', { task_id: this.monitoredTask, include_children: true });
      const events = telRes.events || [];
      
      const consoleEl = qs('#runs-telemetry-console');
      if (consoleEl && events.length > 0) {
        consoleEl.innerHTML = events.map(e => {
          const time = new Date(e.timestamp).toLocaleTimeString();
          let color = 'var(--text-secondary)';
          if (e.event_type.includes('failed') || e.event_type.includes('error')) color = 'var(--status-error)';
          if (e.event_type.includes('success')) color = 'var(--status-success)';
          
          return `
            <div style="display: flex; gap: 8px;">
              <span style="color: var(--text-muted);">[${time}]</span>
              <span style="color: ${color};">[${e.event_type}]</span>
              <span>${e.message || JSON.stringify(e.payload)}</span>
            </div>
          `;
        }).join('');
        consoleEl.scrollTop = consoleEl.scrollHeight;
      }
      
    } catch(e) {
      console.error("Poll Runs Error", e);
    }
  }
};
const GatewayStatusManager = {
  _panel: document.querySelector('#degraded-state-panel'),
  _msgs: document.querySelector('#degraded-state-messages'),
  _btnRetry: document.querySelector('#btn-degraded-retry'),
  _btnLogs: document.querySelector('#btn-degraded-logs'),
  _pollingInterval: null,

  init() {
    if (this._btnRetry) this._btnRetry.addEventListener('click', () => this.checkStatus());
    if (this._btnLogs) this._btnLogs.addEventListener('click', () => { 
        if(window.tala && window.tala.openLogs) window.tala.openLogs(); 
    });
    this.startPolling();
  },

  startPolling() {
    if(this._pollingInterval) clearInterval(this._pollingInterval);
    this._pollingInterval = setInterval(() => this.checkStatus(), 10000);
    // Initial check
    setTimeout(() => this.checkStatus(), 2000);
  },

  async checkStatus() {
    try {
      if(!window.tala || !window.tala.request) return;
      const res = await window.tala.request('gateway.status', {});
      this.renderAuthPosture(res);
      if (res && res.status === 'online') {
        this.hidePanel();
      } else {
        this.showPanel("Gateway reported offline or degraded status.");
      }
    } catch (err) {
      this.renderAuthPosture(null, err);
      this.showPanel("Connection failed: " + err.message);
    }
  },

  renderAuthPosture(status, err = null) {
    const card = document.querySelector('#gateway-auth-posture');
    if (!card) return;
    const title = document.querySelector('#gateway-auth-title');
    const copy = document.querySelector('#gateway-auth-copy');
    const credentials = document.querySelector('#gateway-auth-credentials');
    const remotes = document.querySelector('#gateway-auth-remotes');
    const contacts = document.querySelector('#gateway-auth-contacts');

    const authCredentials = Number(status?.auth_credentials_count || 0);
    const authedRemotes = Number(status?.authenticated_remote_instances || 0);
    const trustedContacts = Number(status?.trusted_contacts_count || 0);
    const ready = Boolean(status?.auth_ready);

    card.classList.toggle('ready', ready);
    card.classList.toggle('degraded', Boolean(err) || !ready);
    if (title) title.textContent = err ? 'Gateway unreachable' : (ready ? 'Auth boundary configured' : 'Auth boundary needs credentials');
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

  showPanel(msg) {
    if (!this._panel) {
      this._panel = document.querySelector('#degraded-state-panel');
      this._msgs = document.querySelector('#degraded-state-messages');
    }
    if (!this._panel) return;
    this._panel.style.display = 'flex';
    if (this._msgs) this._msgs.textContent = msg;
  },

  hidePanel() {
    if (!this._panel) return;
    this._panel.style.display = 'none';
  }
};

document.addEventListener('DOMContentLoaded', () => {
    GatewayStatusManager.init();
});
const PluginRegistryManager = {
  _btnReload: document.querySelector('#btn-plugins-reload'),
  _list: document.querySelector('#plugins-list'),
  _loaded: false,

  init() {
    this._btnReload = document.querySelector('#btn-plugins-reload');
    this._list = document.querySelector('#plugins-list');
    if (this._btnReload) {
      this._btnReload.addEventListener('click', () => this.reloadPlugins());
    }
    
    // Hook into settings tab click
    const btn = document.querySelector('[data-panel="plugins"]');
    if (btn) {
      btn.addEventListener('click', () => {
        if (!this._loaded) this.loadPlugins();
      });
    }
  },

  async loadPlugins() {
    if (!this._list) return;
    this._list.innerHTML = '<div class="admin-empty-state">Loading plugin manifests...</div>';
    try {
      if(!window.tala || !window.tala.request) return;
      const res = await window.tala.request('plugins.list', {});
      if (res && res.plugins) {
        this.renderPlugins(res.plugins);
        this._loaded = true;
      } else {
        this._list.innerHTML = '<div class="admin-empty-state admin-empty-error">Failed to load plugin manifests.</div>';
      }
    } catch (e) {
      this._list.innerHTML = `<div class="admin-empty-state admin-empty-error">Error loading plugins: ${escapeHtml(e.message)}</div>`;
    }
  },

  async reloadPlugins() {
    if (!this._list) return;
    this._list.innerHTML = '<div class="admin-empty-state">Reloading plugin manifests...</div>';
    try {
      const res = await window.tala.request('plugins.reload', {});
      if (res && res.plugins) {
        this.renderPlugins(res.plugins);
        this._loaded = true;
      } else {
        this._list.innerHTML = '<div class="admin-empty-state admin-empty-error">Reload returned no plugin list.</div>';
      }
    } catch (e) {
      this._list.innerHTML = `<div class="admin-empty-state admin-empty-error">Error reloading plugins: ${escapeHtml(e.message)}</div>`;
    }
  },

  async validatePlugin(pluginPath) {
    try {
      const res = await window.tala.request('plugins.validate', { path: pluginPath });
      if (res.valid) {
        alert("Plugin manifest is valid!");
      } else {
        alert("Plugin manifest invalid: " + res.error);
      }
    } catch (e) {
      alert("Error validating plugin: " + e.message);
    }
  },

  renderPlugins(plugins) {
    if (!this._list) return;
    this._list.innerHTML = '';
    
    if (plugins.length === 0) {
        this._list.innerHTML = '<div class="admin-empty-state">No plugins installed.</div>';
        return;
    }

    plugins.forEach(p => {
      const card = document.createElement('div');
      const permissions = (p.permissions || []);
      const tools = (p.tools || []);
      const mcpServers = (p.mcp_servers || []);
      const runtimeLabel = p.runtime_status === 'loaded' ? 'manifest loaded' : (p.runtime_status || 'unknown');
      const permissionHtml = permissions.length
        ? permissions.map(perm => `<span class="admin-tag${String(perm).includes('shell') ? ' admin-tag-risk' : ''}">${escapeHtml(perm)}</span>`).join('')
        : '<span class="admin-tag admin-tag-muted">no permissions</span>';
      const manifestStats = [
        `${tools.length} tools`,
        `${mcpServers.length} MCP`,
        `v${p.version || 'unknown'}`
      ];

      card.className = `admin-plugin-card${p.valid ? '' : ' admin-plugin-card-error'}`;
      card.innerHTML = `
        <div class="admin-card-header">
          <div class="admin-card-title-wrap">
            <h4 class="admin-card-title">${escapeHtml(p.name || p.id || 'Unnamed Plugin')}</h4>
            <div class="admin-card-meta">${escapeHtml(p.id || 'no-id')}</div>
          </div>
          <div class="admin-status-stack">
            <span class="admin-status-chip ${p.valid ? 'status-ok' : 'status-error'}">${p.valid ? 'valid' : 'invalid'}</span>
            <span class="admin-status-chip ${p.runtime_status === 'crashed' ? 'status-error' : 'status-info'}" title="Manifest parse state, not process liveness">${escapeHtml(runtimeLabel)}</span>
          </div>
        </div>
        <div class="admin-card-meta-row">${manifestStats.map(escapeHtml).join(' / ')}</div>
        <div class="admin-tag-row">${permissionHtml}</div>
        ${p.error ? `<div class="admin-card-error-text">${escapeHtml(p.error)}</div>` : ''}
        <div class="admin-card-actions">
          <button class="btn btn-secondary btn-sm admin-plugin-validate">Validate</button>
        </div>
      `;

      const btnValidate = card.querySelector('.admin-plugin-validate');
      if (btnValidate) btnValidate.addEventListener('click', () => this.validatePlugin(p.path));
      this._list.appendChild(card);
    });
  }
};

const MCPServerRegistryManager = {
  _form: null,
  _list: null,
  _transport: null,
  _btnRefresh: null,
  _loaded: false,

  init() {
    this._form = document.querySelector('#mcp-server-form');
    this._list = document.querySelector('#mcp-server-list');
    this._transport = document.querySelector('#mcp-server-transport');
    this._btnRefresh = document.querySelector('#btn-mcp-refresh');

    if (this._transport) {
      this._transport.addEventListener('change', () => this.updateTransportFields());
    }
    if (this._form) {
      this._form.addEventListener('submit', (event) => {
        event.preventDefault();
        this.connectServer();
      });
    }
    if (this._btnRefresh) {
      this._btnRefresh.addEventListener('click', () => this.loadServers());
    }

    const btn = document.querySelector('[data-panel="plugins"]');
    if (btn) {
      btn.addEventListener('click', () => {
        if (!this._loaded) this.loadServers();
      });
    }

    this.updateTransportFields();
  },

  updateTransportFields() {
    const transport = this._transport?.value || 'stdio';
    const stdioFields = document.querySelector('#mcp-stdio-fields');
    const sseFields = document.querySelector('#mcp-sse-fields');
    if (stdioFields) stdioFields.hidden = transport !== 'stdio';
    if (sseFields) sseFields.hidden = transport !== 'sse';
  },

  parseArgs(raw) {
    if (!raw || !raw.trim()) return [];
    const parts = raw.match(/(?:[^\s"]+|"[^"]*")+/g) || [];
    return parts.map(part => part.replace(/^"|"$/g, ''));
  },

  buildConfig() {
    const transport = this._transport?.value || 'stdio';
    if (transport === 'sse') {
      const url = document.querySelector('#mcp-server-url')?.value?.trim();
      if (!url) throw new Error('SSE URL is required.');
      return { transport, url };
    }

    const command = document.querySelector('#mcp-server-command')?.value?.trim();
    if (!command) throw new Error('Command is required.');
    const args = this.parseArgs(document.querySelector('#mcp-server-args')?.value || '');
    const cwd = document.querySelector('#mcp-server-cwd')?.value?.trim();
    const config = { transport, command, args };
    if (cwd) config.cwd = cwd;
    return config;
  },

  async connectServer() {
    const name = document.querySelector('#mcp-server-name')?.value?.trim();
    if (!name) {
      IPCLog.append('warn', 'MCP server name is required.');
      return;
    }

    try {
      const config = this.buildConfig();
      await window.tala.request('mcp.connect_server', { name, config, persist: true });
      IPCLog.append('ok', `MCP server registered: ${name}`);
      if (this._form) this._form.reset();
      this.updateTransportFields();
      await this.loadServers();
    } catch (err) {
      IPCLog.append('error', `Failed to register MCP server: ${err.message ?? err}`);
    }
  },

  async loadServers() {
    if (!this._list) return;
    this._list.innerHTML = '<div class="admin-empty-state">Loading MCP servers...</div>';
    try {
      const res = await window.tala.request('mcp.list_servers', {});
      this.renderServers(res?.servers || []);
      this._loaded = true;
    } catch (err) {
      this._list.innerHTML = `<div class="admin-empty-state admin-empty-error">Error loading MCP servers: ${escapeHtml(err.message ?? err)}</div>`;
    }
  },

  async removeServer(name) {
    try {
      await window.tala.request('mcp.remove_server', { name });
      IPCLog.append('info', `MCP server removed: ${name}`);
      await this.loadServers();
    } catch (err) {
      IPCLog.append('error', `Failed to remove MCP server: ${err.message ?? err}`);
    }
  },

  async inspectTools(name, card) {
    const target = card.querySelector('.admin-mcp-tools');
    if (!target) return;
    target.innerHTML = '<span class="admin-tag admin-tag-muted">loading tools...</span>';
    try {
      const res = await window.tala.request('mcp.list_tools', { server_name: name });
      const tools = res?.tools || [];
      target.innerHTML = tools.length
        ? tools.slice(0, 12).map(tool => `<span class="admin-tag">${escapeHtml(tool.name || tool.qualified_name || 'tool')}</span>`).join('')
        : '<span class="admin-tag admin-tag-muted">no tools reported</span>';
    } catch (err) {
      target.innerHTML = `<span class="admin-tag admin-tag-risk">${escapeHtml(err.message ?? err)}</span>`;
    }
  },

  renderServers(servers) {
    if (!this._list) return;
    this._list.innerHTML = '';

    if (!servers.length) {
      this._list.innerHTML = '<div class="admin-empty-state">No MCP servers registered. Add a stdio or SSE server above.</div>';
      return;
    }

    servers.forEach(server => {
      const config = server.config || {};
      const name = server.name || 'unnamed';
      const transport = config.transport || (config.command ? 'stdio' : 'sse');
      const endpoint = transport === 'stdio'
        ? [config.command, ...(config.args || [])].filter(Boolean).join(' ')
        : config.url || 'no-url';
      const card = document.createElement('div');
      card.className = 'admin-mcp-card';
      card.innerHTML = `
        <div class="admin-card-header">
          <div class="admin-card-title-wrap">
            <h4 class="admin-card-title">${escapeHtml(name)}</h4>
            <div class="admin-card-meta">${escapeHtml(endpoint)}</div>
          </div>
          <span class="admin-status-chip status-info">${escapeHtml(String(transport).toUpperCase())}</span>
        </div>
        <div class="admin-card-meta-row">${escapeHtml(config.cwd ? `cwd: ${config.cwd}` : 'opened on demand')}</div>
        <div class="admin-tag-row admin-mcp-tools"><span class="admin-tag admin-tag-muted">tools not loaded</span></div>
        <div class="admin-card-actions">
          <button class="btn btn-secondary btn-sm admin-mcp-tools-btn">List Tools</button>
          <button class="btn btn-secondary btn-sm admin-mcp-remove-btn">Remove</button>
        </div>
      `;
      card.querySelector('.admin-mcp-tools-btn')?.addEventListener('click', () => this.inspectTools(name, card));
      card.querySelector('.admin-mcp-remove-btn')?.addEventListener('click', () => this.removeServer(name));
      this._list.appendChild(card);
    });
  }
};

document.addEventListener('DOMContentLoaded', () => {
    MCPServerRegistryManager.init();
    PluginRegistryManager.init();
    
    // Listen for server updates
    try {
        window.tala.on('python:notification', async (msg) => {
            if (msg && msg.method === 'task.event') {
                const p = msg.params || {};
                const evt = p.event_type;
                const payload = p.payload || {};
                let statusText = '';
                if (evt === 'phase_start') {
                    statusText = `[Phase Started] ${payload.phase || ''}`;
                } else if (evt === 'state_transition') {
                    statusText = `[Task State] ${payload.from_status || 'init'} -> ${payload.to_status || ''}${payload.reason ? ' (' + payload.reason + ')' : ''}`;
                } else if (evt === 'task_init') {
                    statusText = `[Task Initialized] ${payload.description || ''}`;
                }
                if (statusText) {
                    console.log(`[TaskEvent] ${statusText}`);
                }
            } else if (msg && msg.method === 'system.update_available') {
                const confirmed = confirm('A server update is available from the VPS. Do you want to download and apply it now?');
                if (confirmed) {
                    try {
                        console.log('Downloading update payload from VPS...');
                        const response = await window.tala.invoke('system.download_update');
                        if (response.status === 'success' && response.zipBase64) {
                            console.log('Applying update payload locally...');
                            await window.tala.invoke('system:apply_update', { zipBase64: response.zipBase64 });
                        } else {
                            alert('Failed to download update payload from VPS.');
                        }
                    } catch (err) {
                        alert('Error during OTA update: ' + err.message);
                    }
                }
            }
        });
    } catch (err) {
        console.warn('Failed to register system.update_available listener', err);
    }

    // Global Network Toggle Button Logic
    const bottomToggleBtn = document.getElementById('btn-bottom-network-toggle');
    const bottomNetworkDot = document.getElementById('bottom-network-dot');
    const bottomNetworkText = document.getElementById('bottom-network-text');

    async function updateNetworkToggleUI() {
        if (!bottomToggleBtn) return;
        try {
            const status = await invoke('python:rpc', { method: 'chain.connection_get', params: {} });
            const connection = status?.connection || {};
            if (connection.mode === 'vps') {
                bottomNetworkDot.style.background = '#00f0ff';
                bottomNetworkDot.style.boxShadow = '0 0 8px #00f0ff';
                bottomNetworkText.textContent = 'VPS Online';
                bottomNetworkText.style.color = '#00f0ff';
                bottomToggleBtn.textContent = 'Disconnect';
            } else {
                bottomNetworkDot.style.background = '#444';
                bottomNetworkDot.style.boxShadow = '0 0 5px rgba(0,0,0,0.5)';
                bottomNetworkText.textContent = 'Local Only';
                bottomNetworkText.style.color = '#aaa';
                bottomToggleBtn.textContent = 'Connect VPS';
            }
        } catch (err) {
            bottomNetworkText.textContent = 'Offline';
            bottomToggleBtn.textContent = 'Connect VPS';
        }
    }

    if (bottomToggleBtn) {
        bottomToggleBtn.addEventListener('click', async () => {
            bottomToggleBtn.disabled = true;
            bottomToggleBtn.textContent = 'Wait...';
            try {
                const status = await invoke('python:rpc', { method: 'chain.connection_get', params: {} });
                const connection = status?.connection || {};
                if (connection.mode === 'vps') {
                    // Disconnect
                    await invoke('python:rpc', { method: 'chain.disconnect', params: { remove_contact: false } });
                    IPCLog.append('info', 'Manually disconnected from Tala VPS chain.');
                } else {
                    // Connect
                    const profileResp = await invoke('python:rpc', { method: 'identity.get_user_profile', params: {} });
                    let profileUsername = 'local-user';
                    if (profileResp && profileResp.success && profileResp.profile && profileResp.profile.username) {
                        profileUsername = profileResp.profile.username;
                    }
                    if (profileUsername === 'local-user' || profileUsername.trim() === '') {
                        IPCLog.append('warning', 'Please configure your username in Settings > User Profile before connecting to the VPS.');
                        if (typeof UI !== 'undefined' && UI.switchView) {
                            UI.switchView('settings');
                            const userTab = document.querySelector('.settings-sub-tab[data-panel="identity"]');
                            if (userTab) userTab.click();
                        }
                        return;
                    }
                    
                    // We assume talacore.app is the default public directory
                    await invoke('python:rpc', { method: 'chain.connect_vps', params: { username: profileUsername, endpoint_url: '', shared_secret: '' } });
                    IPCLog.append('ok', 'Manually connected to Tala VPS chain as @' + profileUsername + '.');
                }
            } catch (err) {
                console.error(err);
                IPCLog.append('error', `Network toggle error: ${err.message || err}`);
            } finally {
                bottomToggleBtn.disabled = false;
                await updateNetworkToggleUI();
                if (typeof ProfileManager !== 'undefined') ProfileManager.refreshChainStatus();
            }
        });
        
        // Initial state update
        updateNetworkToggleUI();
        setInterval(updateNetworkToggleUI, 15000);
    }

    // Auto-Update Checker
    async function checkForUpdates() {
        try {
            // Get local version
            const localResp = await invoke('python:rpc', { method: 'app.version' });
            if (!localResp || !localResp.version) return;
            const localVersion = localResp.version;


            // Get remote version
            const remoteResp = await fetch('https://talacore.app/version.json', { cache: 'no-store' });
            if (!remoteResp.ok) return;
            const remoteData = await remoteResp.json();
            if (!remoteData || !remoteData.version) return;
            const remoteVersion = remoteData.version;

            // Simple semver compare (assuming x.y.z)
            const localParts = localVersion.split('.').map(Number);
            const remoteParts = remoteVersion.split('.').map(Number);
            
            let isNewer = false;
            for (let i = 0; i < 3; i++) {
                const l = localParts[i] || 0;
                const r = remoteParts[i] || 0;
                if (r > l) { isNewer = true; break; }
                if (r < l) { break; }
            }

            if (isNewer) {
                const banner = document.getElementById('upgrade-banner');
                const vSpan = document.getElementById('upgrade-banner-version');
                if (banner && vSpan) {
                    vSpan.textContent = 'v' + remoteVersion;
                    banner.style.display = 'block';
                    banner.onclick = async () => {
                        vSpan.textContent = 'Downloading update... please wait';
                        banner.style.pointerEvents = 'none';
                        let url = remoteData.downloads.windows;
                        if (navigator.userAgent.indexOf('Mac') !== -1) url = remoteData.downloads.macos;
                        if (navigator.userAgent.indexOf('Linux') !== -1) url = remoteData.downloads.linux;
                        
                        try {
                            await invoke('app.update', { url: url });
                        } catch (err) {
                            console.error('Update failed:', err);
                            vSpan.textContent = 'Update failed. Check console.';
                        }
                    };
                }
            }
        } catch (err) {
            console.warn('Update check failed:', err);
        }
    }

    // Check immediately and then every 2 hours
    setTimeout(checkForUpdates, 3000);
    setInterval(checkForUpdates, 2 * 60 * 60 * 1000);
});

window.InferenceManager = InferenceManager;
