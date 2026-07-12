(function(){
  'use strict';

  const get = id => document.getElementById(id);
  const workspaces = {
    dashboard: {title:'Dashboard', description:'Market chart, account state, performance and alerts.'},
    backtest: {title:'Backtest Workspace', description:'Validate a strategy with the same candle range shown on the chart.', action:'backtest', actionLabel:'Run backtest'},
    paper: {title:'Paper Trading', description:'Simulated fills use live market data without sending real orders.', action:'paper', actionLabel:'Activate paper mode'},
    demo: {title:'Demo Trading', description:'Sandbox account access is shown here when the demo credentials are available.'},
    live: {title:'Live Trading', description:'Live orders remain locked until account, consent, risk, and safety checks pass.'},
    strategy: {title:'Strategy Management', description:'Save and reuse strategy parameters for backtests and paper execution.', action:'strategy', actionLabel:'Save strategy'},
    trades: {title:'Trade History', description:'Review the currently loaded backtest or connected execution records.'},
    positions: {title:'Positions', description:'Inspect open positions and the latest account risk state.'},
    notifications: {title:'Notification Center', description:'Connection, data, order, and risk events are collected here.'},
    risk: {title:'Risk Management', description:'Configure limits in Settings and monitor actual usage in the right rail.', action:'settings', actionLabel:'Open risk settings'},
    settings: {title:'Settings', description:'Persist risk controls, connection status, and execution safety preferences.'},
  };
  let activeWorkspace = 'dashboard';

  function escapeHtml(value){
    return String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
  }

  function ensureContext(){
    const chartWrap = document.querySelector('.dashboardShell .chartWrap');
    if (!chartWrap || get('stage5WorkspaceContext')) return;
    const context = document.createElement('section');
    context.id = 'stage5WorkspaceContext';
    context.className = 'workspaceContext';
    context.innerHTML = '<div class="workspaceContextCopy"><span class="workspaceContextEyebrow">Workspace</span><strong class="workspaceContextTitle" id="stage5WorkspaceTitle">Dashboard</strong><span class="workspaceContextDescription" id="stage5WorkspaceDescription"></span></div><div class="workspaceContextMeta"><span class="workspaceContextMode" id="stage5WorkspaceMode">PAPER</span><span class="workspaceContextState" id="stage5WorkspaceState">READY</span></div>';
    const kpi = chartWrap.querySelector('.kpiStrip');
    if (kpi) kpi.before(context); else chartWrap.prepend(context);
  }

  function actualWorkspaceNote(page){
    if (page === 'trades') {
      const count = document.querySelectorAll('#tradeRows tr').length;
      return count ? `${count} visible rows from the active result.` : 'No trade rows are loaded yet.';
    }
    if (page === 'positions') return get('shellPositionEmpty')?.textContent?.trim() || 'Open positions appear in the right rail when account data is connected.';
    if (page === 'notifications') return get('shellNotificationCount')?.textContent?.trim() ? `${get('shellNotificationCount').textContent.trim()} unread or recent events.` : 'No notification count is available yet.';
    if (page === 'demo') return document.querySelector('.modeButton[data-mode="DEMO"]')?.disabled ? 'Demo mode is locked until a demo account is configured.' : 'Demo mode is available for the connected account.';
    if (page === 'live') return document.querySelector('.modeButton[data-mode="LIVE"]')?.disabled ? 'Live mode is locked by the existing safety checks.' : 'Live mode is available after the final confirmation step.';
    if (page === 'risk') return 'The right rail shows actual usage; Settings stores the configured limits.';
    if (page === 'settings') return get('stage4SettingsStatus')?.textContent?.trim() || 'Settings status is loading.';
    if (page === 'backtest') return get('status')?.textContent?.includes('백테스트') ? 'A backtest result is loaded on the chart.' : 'No backtest result is loaded yet.';
    return 'Paper mode is simulation-only and does not submit exchange orders.';
  }

  function ensureWorkspacePanel(){
    const drawer = get('controlDrawer');
    if (!drawer || get('stage5WorkspacePanel')) return;
    const panel = document.createElement('section');
    panel.id = 'stage5WorkspacePanel';
    panel.className = 'panel';
    panel.addEventListener('click', event => {
      const button = event.target.closest('[data-stage5-action]');
      if (!button) return;
      const action = button.dataset.stage5Action;
      if (action === 'backtest') (get('shellRunBacktest') || get('runBtn'))?.click();
      if (action === 'strategy') get('shellSaveStrategy')?.click();
      if (action === 'paper') document.querySelector('.modeButton[data-mode="PAPER"]')?.click();
      if (action === 'settings') document.querySelector('.sideNavItem[data-page="settings"]')?.click();
    });
    drawer.prepend(panel);
  }

  function renderWorkspace(page){
    activeWorkspace = workspaces[page] ? page : 'dashboard';
    const info = workspaces[activeWorkspace];
    const app = document.querySelector('.dashboardShell');
    const drawer = get('controlDrawer');
    if (app) app.dataset.workspace = activeWorkspace;
    if (drawer) drawer.classList.toggle('drawer-open', activeWorkspace !== 'dashboard');
    ensureContext();
    ensureWorkspacePanel();
    const title = get('stage5WorkspaceTitle');
    const description = get('stage5WorkspaceDescription');
    if (title) title.textContent = info.title;
    if (description) description.textContent = info.description;
    const mode = get('shellModeBadge')?.textContent?.trim() || 'PAPER';
    if (get('stage5WorkspaceMode')) get('stage5WorkspaceMode').textContent = mode;
    if (get('stage5WorkspaceState')) get('stage5WorkspaceState').textContent = get('netBadge')?.textContent?.trim() || 'READY';
    const panel = get('stage5WorkspacePanel');
    if (!panel) return;
    const action = info.action ? `<button type="button" class="btn blue stage5WorkspaceAction" data-stage5-action="${info.action}">${info.actionLabel}</button>` : '';
    panel.innerHTML = `<div class="stage5PanelHeader"><h3>${escapeHtml(info.title)}</h3><small>${escapeHtml(activeWorkspace)}</small></div><div class="box stage5PanelBody"><p>${escapeHtml(info.description)}</p><div class="stage5WorkspaceNote"><strong>Current state</strong>${escapeHtml(actualWorkspaceNote(activeWorkspace))}</div>${action}</div>`;
  }

  function syncContextState(){
    if (get('stage5WorkspaceMode')) get('stage5WorkspaceMode').textContent = get('shellModeBadge')?.textContent?.trim() || 'PAPER';
    if (get('stage5WorkspaceState')) get('stage5WorkspaceState').textContent = get('netBadge')?.textContent?.trim() || 'READY';
  }

  function installNavigation(){
    const original = window.setDashboardShellNav;
    if (typeof original === 'function' && !original.__stage5Wrapped){
      const wrapped = function(page){ const result = original.apply(this, arguments); renderWorkspace(page); return result; };
      wrapped.__stage5Wrapped = true;
      window.setDashboardShellNav = wrapped;
    }
    document.querySelectorAll('.sideNavItem, .shellTab').forEach(node => {
      if (node.dataset.stage5Bound === '1') return;
      node.dataset.stage5Bound = '1';
      node.addEventListener('click', () => renderWorkspace(node.dataset.page));
    });
  }

  function installWorkspaceStatusAlias(){
    if (typeof window.setWorkspaceStatus === 'function') return;
    window.setWorkspaceStatus = function(message, kind){
      const node = get('shellStrategyStatus');
      if (!node) return;
      node.textContent = message;
      node.className = 'workspaceStatus' + (kind ? ' ' + kind : '');
    };
  }

  function installChartPolicy(){
    if (typeof window.setTradeLevels === 'function' && !window.setTradeLevels.__stage5Policy){
      const clear = window.clearTradeLevels;
      const noLevels = function(){ if (typeof clear === 'function') clear(); };
      noLevels.__stage5Policy = true;
      window.setTradeLevels = noLevels;
    }
    if (typeof window.renderResult === 'function' && !window.renderResult.__stage5Policy){
      const original = window.renderResult;
      const wrapped = function(data){ const result = original.apply(this, arguments); if (typeof window.clearTradeLevels === 'function') window.clearTradeLevels(); return result; };
      wrapped.__stage5Policy = true;
      window.renderResult = wrapped;
    }
  }

  function boot(){
    ensureContext();
    ensureWorkspacePanel();
    installWorkspaceStatusAlias();
    installNavigation();
    installChartPolicy();
    const badge = get('netBadge');
    if (badge && window.MutationObserver) new MutationObserver(syncContextState).observe(badge, {childList:true,characterData:true,subtree:true});
    const status = get('status');
    if (status && window.MutationObserver) new MutationObserver(() => { if (activeWorkspace === 'backtest') renderWorkspace(activeWorkspace); }).observe(status, {childList:true,characterData:true,subtree:true});
    renderWorkspace(document.querySelector('.sideNavItem.is-active')?.dataset.page || 'dashboard');
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot, {once:true});
  else window.setTimeout(boot, 0);
})();
