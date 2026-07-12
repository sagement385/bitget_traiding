(function(){
  'use strict';

  const get = id => document.getElementById(id);
  const notify = (message, bad) => {
    if (typeof window.toast === 'function') window.toast(message, Boolean(bad));
    else console[bad ? 'error' : 'log'](message);
  };
  const setStatus = (message, kind) => {
    const node = get('stage4SettingsStatus');
    if (!node) return;
    node.textContent = message;
    node.className = 'settingsStatus' + (kind ? ' ' + kind : '');
  };
  const riskFields = [
    ['daily_loss_limit', 'settingsDailyLossLimit'],
    ['max_consecutive_losses', 'settingsConsecutiveLosses'],
    ['max_leverage', 'settingsMaxLeverage'],
    ['max_total_exposure', 'settingsMaxExposure'],
    ['max_symbol_exposure', 'settingsMaxSymbolExposure'],
    ['risk_per_trade_pct', 'settingsRiskPerTrade'],
  ];
  const riskChecks = [
    ['kill_switch', 'settingsKillSwitch'],
    ['halt_on_connection_issue', 'settingsHaltConnection'],
    ['halt_on_data_delay', 'settingsHaltDataDelay'],
  ];
  let settingsLoaded = false;
  let confirmationArmed = false;

  function ensureSettingsWorkspace(){
    const drawer = get('controlDrawer');
    if (!drawer || get('stage4SettingsWorkspace')) return;
    const section = document.createElement('section');
    section.id = 'stage4SettingsWorkspace';
    section.className = 'panel settingsWorkspace';
    section.innerHTML = '<h3>리스크 및 연결 설정</h3>' +
      '<div class="box"><div class="settingsGrid">' +
      '<div class="field"><label for="settingsDailyLossLimit">일일 최대 손실 (USDT)</label><input id="settingsDailyLossLimit" type="number" min="0" step="10"></div>' +
      '<div class="field"><label for="settingsConsecutiveLosses">연속 손실 허용 횟수</label><input id="settingsConsecutiveLosses" type="number" min="1" max="100" step="1"></div>' +
      '<div class="field"><label for="settingsMaxLeverage">최대 레버리지</label><input id="settingsMaxLeverage" type="number" min="0.1" max="125" step="0.1"></div>' +
      '<div class="field"><label for="settingsRiskPerTrade">거래당 손실 허용 (%)</label><input id="settingsRiskPerTrade" type="number" min="0" max="100" step="0.1"></div>' +
      '<div class="field"><label for="settingsMaxExposure">최대 총 노출 (USDT)</label><input id="settingsMaxExposure" type="number" min="0" step="10"></div>' +
      '<div class="field"><label for="settingsMaxSymbolExposure">종목당 최대 노출 (USDT)</label><input id="settingsMaxSymbolExposure" type="number" min="0" step="10"></div>' +
      '<label class="check wide"><span>킬 스위치</span><input id="settingsKillSwitch" type="checkbox"></label>' +
      '<label class="check wide"><span>연결 이상 시 신규 주문 중단</span><input id="settingsHaltConnection" type="checkbox"></label>' +
      '<label class="check wide"><span>데이터 지연 시 신규 진입 차단</span><input id="settingsHaltDataDelay" type="checkbox"></label>' +
      '</div><div class="settingsMeta"><span>API 상태 <b id="settingsApiStatus">확인 중</b></span><span>거래 모드 <b id="settingsModeStatus">PAPER 사용 가능</b></span><span>민감한 API 키 값은 이 화면에 표시하지 않습니다.</span></div>' +
      '<div class="settingsActions"><button class="btn gray" id="settingsRefresh" type="button">새로고침</button><button class="btn blue" id="settingsSave" type="button">설정 저장</button></div>' +
      '<div class="settingsStatus" id="stage4SettingsStatus">설정값을 불러오는 중입니다.</div></div>';
    drawer.prepend(section);
    get('settingsRefresh').addEventListener('click', loadSettings);
    get('settingsSave').addEventListener('click', saveSettings);
  }

  function showSettings(visible){
    const panel = get('stage4SettingsWorkspace');
    if (panel) panel.classList.toggle('is-visible', Boolean(visible));
    if (visible && !settingsLoaded) loadSettings();
  }

  function renderSettings(payload){
    const risk = payload.risk || {};
    riskFields.forEach(([key, id]) => { if (get(id)) get(id).value = risk[key] ?? ''; });
    riskChecks.forEach(([key, id]) => { if (get(id)) get(id).checked = Boolean(risk[key]); });
    const connected = payload.credentials_configured ? 'API 키 설정됨' : 'API 키 미설정';
    if (get('settingsApiStatus')) get('settingsApiStatus').textContent = connected;
    const flags = payload.mode_flags || {};
    if (get('settingsModeStatus')) get('settingsModeStatus').textContent = `PAPER ${flags.paper ? '가능' : '중단'} · DEMO ${flags.demo ? '가능' : '잠금'} · LIVE ${flags.live ? '가능' : '잠금'}`;
  }

  async function loadSettings(){
    ensureSettingsWorkspace();
    setStatus('설정과 연결 상태를 확인하는 중입니다.');
    try{
      const response = await fetch('/api/settings/status', {cache:'no-store'});
      const payload = await response.json();
      if (!response.ok || !payload.ok) throw new Error(payload.error || 'settings status failed');
      renderSettings(payload);
      settingsLoaded = true;
      confirmationArmed = false;
      setStatus('설정값을 불러왔습니다.', 'ok');
    }catch(error){ setStatus('설정 상태를 읽지 못했습니다: ' + error.message, 'bad'); }
  }

  function collectSettings(){
    const payload = {};
    riskFields.forEach(([key, id]) => payload[key] = Number(get(id)?.value));
    riskChecks.forEach(([key, id]) => payload[key] = Boolean(get(id)?.checked));
    return payload;
  }

  async function saveSettings(){
    const payload = collectSettings();
    const invalid = riskFields.find(([key]) => !Number.isFinite(payload[key]) || payload[key] < 0);
    if (invalid || payload.max_consecutive_losses < 1 || payload.max_leverage <= 0 || payload.risk_per_trade_pct > 100){
      setStatus('리스크 숫자 범위를 확인해 주세요.', 'bad');
      return;
    }
    const mode = (get('shellModeBadge')?.textContent || '').trim();
    if (mode === 'LIVE' && !confirmationArmed){
      confirmationArmed = true;
      setStatus('LIVE 모드의 리스크 설정을 저장하려면 설정 저장을 한 번 더 눌러 확인해 주세요.', 'bad');
      return;
    }
    try{
      const response = await fetch('/api/settings/risk', {method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(payload)});
      const result = await response.json();
      if (!response.ok || !result.ok) throw new Error(result.error || 'risk settings save failed');
      renderSettings(result);
      settingsLoaded = true;
      confirmationArmed = false;
      setStatus('리스크 설정을 저장했습니다. 계좌 리스크 카드에 반영됩니다.', 'ok');
      notify('리스크 설정 저장 완료');
      if (typeof window.refreshDashboardAccountState === 'function') window.refreshDashboardAccountState();
    }catch(error){ setStatus('리스크 설정을 저장하지 못했습니다: ' + error.message, 'bad'); notify(error.message, true); }
  }

  function ensureLiveModal(){
    if (get('liveConfirmModal')) return;
    const modal = document.createElement('div');
    modal.id = 'liveConfirmModal';
    modal.className = 'liveModalBackdrop';
    modal.hidden = true;
    modal.innerHTML = '<div class="liveModal" role="dialog" aria-modal="true" aria-labelledby="liveModalTitle"><h3 id="liveModalTitle">LIVE 모드 전환 확인</h3><p>LIVE 모드는 실제 Bitget 계좌 상태를 조회할 수 있습니다. 현재 UI에서는 주문 실행이 안전 잠금 상태지만, 모드 전환 자체는 계좌 인식에 영향을 줍니다.</p><div class="warning">API 키, 계좌 조회, 리스크 설정을 모두 확인한 뒤에만 계속하세요. 준비되지 않은 값은 서버에서 다시 차단됩니다.</div><label class="confirmCheck"><input id="liveConfirmCheckbox" type="checkbox"><span>실계좌 모드임을 이해했고, 리스크 설정과 연결 상태를 확인했습니다.</span></label><div class="liveModalActions"><button class="btn gray" id="liveCancel" type="button">취소</button><button class="btn liveConfirm" id="liveConfirm" type="button" disabled>LIVE 모드 전환</button></div></div>';
    document.body.appendChild(modal);
    get('liveCancel').addEventListener('click', closeLiveModal);
    get('liveConfirmCheckbox').addEventListener('change', event => { get('liveConfirm').disabled = !event.target.checked; });
    get('liveConfirm').addEventListener('click', confirmLiveMode);
    modal.addEventListener('click', event => { if (event.target === modal) closeLiveModal(); });
  }
  function closeLiveModal(){ const modal = get('liveConfirmModal'); if (modal) modal.hidden = true; }
  function openLiveModal(){ ensureLiveModal(); get('liveConfirmCheckbox').checked = false; get('liveConfirm').disabled = true; get('liveConfirmModal').hidden = false; get('liveConfirmCheckbox').focus(); }
  function confirmLiveMode(){
    closeLiveModal();
    document.dispatchEvent(new Event('stage4-live-confirmed'));
  }

  function installLiveGuard(){
    if (document.body.dataset.stage4LiveGuard === 'on') return;
    document.body.dataset.stage4LiveGuard = 'on';
    document.addEventListener('stage4-live-request', openLiveModal);
    document.addEventListener('click', event => {
      const button = event.target?.closest?.('.modeButton[data-mode="LIVE"]');
      if (!button || button.disabled) return;
      event.preventDefault();
      event.stopImmediatePropagation();
      openLiveModal();
    }, true);
  }

  function installRightRailToggle(){
    const top = document.querySelector('.dashboardShell .top');
    const app = document.querySelector('.dashboardShell');
    if (!top || !app || get('rightRailToggle')) return;
    const button = document.createElement('button');
    button.id = 'rightRailToggle';
    button.type = 'button';
    button.className = 'shellIcon stage4CollapseButton';
    button.setAttribute('aria-label', '거래 패널 접기');
    button.textContent = '◧';
    top.appendChild(button);
    button.addEventListener('click', () => { const collapsed = app.classList.toggle('right-collapsed'); button.setAttribute('aria-label', collapsed ? '거래 패널 펼치기' : '거래 패널 접기'); });
  }

  function installSettingsNav(){
    document.querySelectorAll('.sideNavItem[data-page="settings"], .shellTab[data-page="settings"]').forEach(node => node.addEventListener('click', () => showSettings(true)));
    document.querySelectorAll('.sideNavItem:not([data-page="settings"]), .shellTab:not([data-page="settings"])').forEach(node => node.addEventListener('click', () => showSettings(false)));
  }

  function boot(){
    ensureSettingsWorkspace();
    ensureLiveModal();
    installLiveGuard();
    installRightRailToggle();
    installSettingsNav();
    const page = document.querySelector('.sideNavItem.is-active')?.dataset.page;
    showSettings(page === 'settings');
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot, {once:true});
  else window.setTimeout(boot, 0);
})();
