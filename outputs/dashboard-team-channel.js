(() => {
  const views = document.getElementById('manager-views');
  const channelNav = document.querySelector('[data-page="channels"]');
  const state = {selectedSales: 'all', selectedType: 'all', searchQuery: '', sortKey: 'gross', sortDirection: -1, selectedKey: null};

  const escapeHtml = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[char]));
  const money = value => '$' + Number(value || 0).toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2});
  const number = value => Number(value || 0).toLocaleString('en-US');
  const asOfLabel = value => String(value || '').replace('T', ' ').replace('Z', ' UTC').slice(0, 23);
  const businessKey = row => [row.month, row.sales, row.type, row.channelId].join('|');

  function dataset() {
    return window.LARK_DASHBOARD_DATA?.x5ChannelMonthly;
  }

  function scopedRows(data) {
    const query = state.searchQuery.trim().toLowerCase();
    return data.records
      .filter(row => state.selectedSales === 'all' || row.sales === state.selectedSales)
      .filter(row => state.selectedType === 'all' || row.type === state.selectedType)
      .filter(row => !query || `${row.accountName} ${row.channelId}`.toLowerCase().includes(query))
      .sort((left, right) => ((Number(left[state.sortKey]) || 0) - (Number(right[state.sortKey]) || 0)) * state.sortDirection);
  }

  function summary(rows) {
    return rows.reduce((total, row) => ({
      channels: total.channels + 1,
      ib: total.ib + (row.type === 'IB' ? 1 : 0),
      cpa: total.cpa + (row.type === 'CPA' ? 1 : 0),
      registration: total.registration + Number(row.registration || 0),
      ftd: total.ftd + Number(row.ftd || 0),
      gross: total.gross + Number(row.gross || 0),
      withdrawal: total.withdrawal + Number(row.withdrawal || 0),
      net: total.net + Number(row.net || 0),
    }), {channels: 0, ib: 0, cpa: 0, registration: 0, ftd: 0, gross: 0, withdrawal: 0, net: 0});
  }

  function closeDrawer() {
    document.querySelector('.channel-phase1-drawer')?.remove();
    state.selectedKey = null;
  }

  function openDrawer(key) {
    const row = dataset()?.records.find(item => businessKey(item) === key);
    if (!row) return;
    closeDrawer();
    state.selectedKey = key;
    const fields = [
      ['月份', row.month], ['销售', row.sales], ['类型', row.type], ['渠道 ID', row.channelId],
      ['IB/CPA 账户名称', row.accountName || '未提供'], ['Registration', number(row.registration)],
      ['FTD', number(row.ftd)], ['Gross Deposit', money(row.gross)],
      ['Withdrawal', money(row.withdrawal)], ['Net', money(row.net)], ['Lark 更新时间', asOfLabel(row.updatedAt)],
    ];
    const overlay = document.createElement('div');
    overlay.className = 'channel-phase1-drawer';
    overlay.innerHTML = '<aside class="channel-phase1-drawer-panel" role="dialog" aria-modal="true" aria-label="渠道详情">' +
      '<button class="channel-phase1-drawer-close" type="button" aria-label="关闭渠道详情">关闭</button>' +
      '<h2 class="manager-title">' + escapeHtml(row.accountName || row.channelId) + '</h2>' +
      '<p class="manager-sub">X5 月度渠道表现</p><dl class="channel-phase1-detail">' +
      fields.map(([label, value]) => '<dt>' + escapeHtml(label) + '</dt><dd>' + escapeHtml(value) + '</dd>').join('') +
      '</dl></aside>';
    overlay.addEventListener('click', event => {
      if (event.target === overlay || event.target.closest('.channel-phase1-drawer-close')) closeDrawer();
    });
    document.body.append(overlay);
  }

  function sortHead(label, key) {
    const arrow = state.sortKey === key ? (state.sortDirection > 0 ? ' ↑' : ' ↓') : '';
    return '<th data-channel-sort="' + key + '" role="button" tabindex="0">' + label + arrow + '</th>';
  }

  function kpi(value, label) {
    return '<div class="kpi-card"><b>' + escapeHtml(value) + '</b><span>' + escapeHtml(label) + '</span><span>X5 月度渠道</span></div>';
  }

  function renderChannels() {
    const data = dataset();
    if (!data?.month || !Array.isArray(data.records)) {
      views.innerHTML = '<div class="manager-view active"><h1 class="manager-title">渠道分析</h1><p class="manager-sub">X5 月度渠道快照未加载；页面不会回退到旧 CRM 数据。</p></div>';
      return;
    }
    const all = data.records;
    const rankedRows = scopedRows(data);
    const rows = rankedRows.slice(0, 10);
    const totals = summary(all);
    const sales = [...new Set(all.map(row => row.sales))].sort();
    const qa = data.qa || {};

    views.innerHTML = '<div class="manager-view active">' +
      '<h1 class="manager-title">X5 月度渠道表现</h1>' +
      '<p class="manager-sub">月份 ' + escapeHtml(data.month) + ' · ' + escapeHtml(data.source || 'Lark') + '</p>' +
      '<div class="channel-phase1-note"><b>业务键 QA：</b>' + escapeHtml(qa.validBusinessKeys ?? all.length) +
        ' 条有效记录，重复键 ' + escapeHtml(qa.duplicateBusinessKeys ?? '待确认') +
        '。无效或占位记录不参与展示；缺少月度 CPA 的渠道不会被补零。</div>' +
      '<div class="kpi-row channel-phase1-kpis">' +
        kpi(totals.channels, '渠道数') + kpi(totals.ib, 'IB') + kpi(totals.cpa, 'CPA') +
        kpi(number(totals.registration), 'Registration') + kpi(number(totals.ftd), 'FTD') +
        kpi(money(totals.gross), 'Gross Deposit') + kpi(money(totals.withdrawal), 'Withdrawal') + kpi(money(totals.net), 'Net') +
      '</div><div class="manager-card"><div class="filters">' +
        '<select id="channel-sales" aria-label="选择销售"><option value="all">全部销售</option>' +
        sales.map(salesName => '<option value="' + escapeHtml(salesName) + '">' + escapeHtml(salesName) + '</option>').join('') +
        '</select><select id="channel-type" aria-label="选择渠道类型"><option value="all">IB + CPA</option><option value="IB">IB</option><option value="CPA">CPA</option></select>' +
        '<input id="channel-search" value="' + escapeHtml(state.searchQuery) + '" placeholder="搜索账户名称或渠道 ID" aria-label="搜索账户名称或渠道 ID">' +
      '</div><p class="quiet">当前展示筛选及排序结果的前 ' + rows.length + ' 条（共 ' + rankedRows.length + ' 条有效渠道记录）。筛选和排序不改变上方团队汇总。</p>' +
      (rows.length ? '<table class="manager-table"><thead><tr><th>销售</th><th>类型</th><th>账户名称</th><th>渠道 ID</th>' +
        sortHead('Registration', 'registration') + sortHead('FTD', 'ftd') + sortHead('Gross Deposit', 'gross') +
        sortHead('Withdrawal', 'withdrawal') + sortHead('Net', 'net') + '<th>更新时间</th></tr></thead><tbody>' +
        rows.map(row => '<tr><td>' + escapeHtml(row.sales) + '</td><td>' + escapeHtml(row.type) +
          '</td><td><button class="link-button" data-channel-key="' + escapeHtml(businessKey(row)) + '">' +
          escapeHtml(row.accountName || '未提供') + '</button></td><td>' + escapeHtml(row.channelId) +
          '</td><td>' + number(row.registration) + '</td><td>' + number(row.ftd) + '</td><td>' + money(row.gross) +
          '</td><td>' + money(row.withdrawal) + '</td><td>' + money(row.net) + '</td><td>' + escapeHtml(asOfLabel(row.updatedAt)) +
          '</td></tr>').join('') + '</tbody></table>' : '<p class="quiet">没有符合当前筛选条件的渠道。</p>') +
      '</div></div>';

    const salesSelect = document.getElementById('channel-sales');
    const typeSelect = document.getElementById('channel-type');
    salesSelect.value = state.selectedSales;
    typeSelect.value = state.selectedType;
    salesSelect.addEventListener('change', event => { state.selectedSales = event.target.value; closeDrawer(); renderChannels(); });
    typeSelect.addEventListener('change', event => { state.selectedType = event.target.value; closeDrawer(); renderChannels(); });
    document.getElementById('channel-search').addEventListener('input', event => { state.searchQuery = event.target.value; renderChannels(); });
    document.querySelectorAll('[data-channel-sort]').forEach(header => {
      const sort = () => { const key = header.dataset.channelSort; state.sortDirection = state.sortKey === key ? -state.sortDirection : -1; state.sortKey = key; renderChannels(); };
      header.addEventListener('click', sort);
      header.addEventListener('keydown', event => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); sort(); } });
    });
    document.querySelectorAll('[data-channel-key]').forEach(button => button.addEventListener('click', () => openDrawer(button.dataset.channelKey)));
  }

  document.addEventListener('keydown', event => { if (event.key === 'Escape' && state.selectedKey) closeDrawer(); });
  channelNav?.addEventListener('click', renderChannels);
})();
