(() => {
  const views = document.getElementById('manager-views');
  const channelNav = document.querySelector('[data-page="channels"]');
  const state = {
    selectedSales: 'all',
    searchQuery: '',
    statusFilter: 'all',
    sortKey: 'gross',
    sortDirection: -1,
    selectedChannelId: null
  };

  const escapeHtml = value => String(value ?? '').replace(/[&<>"']/g, char => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }[char]));
  const money = value => '$' + Number(value || 0).toLocaleString('en-US', {
    minimumFractionDigits: 2, maximumFractionDigits: 2
  });
  const asOfLabel = value => String(value || '').replace('T', ' ').slice(0, 16);

  function dataset() {
    return window.LARK_CHANNEL_DATA;
  }

  function selectedSummary(data) {
    if (state.selectedSales === 'all') return data.teamSummary;
    return data.salesSummary.find(summary => String(summary.salesId) === state.selectedSales);
  }

  function summaryMetric(summary, key) {
    if (summary[key] !== undefined && summary[key] !== null) return summary[key];
    return summary.totals?.[key];
  }

  function scopedChannels(data) {
    return data.channelSnapshots.filter(channel =>
      state.selectedSales === 'all' || String(channel.salesId) === state.selectedSales
    );
  }

  function tableChannels(data) {
    const query = state.searchQuery.trim().toLowerCase();
    return scopedChannels(data)
      .filter(channel => state.statusFilter === 'all' || channel.channelStatus === state.statusFilter)
      .filter(channel => !query || (channel.channelName + ' ' + channel.channelId).toLowerCase().includes(query))
      .sort((left, right) => {
        const leftValue = Number(left[state.sortKey]) || 0;
        const rightValue = Number(right[state.sortKey]) || 0;
        return (leftValue - rightValue) * state.sortDirection;
      });
  }

  function closeDrawer() {
    document.querySelector('.channel-phase1-drawer')?.remove();
    state.selectedChannelId = null;
  }

  function openDrawer(channelId) {
    const data = dataset();
    const channel = data?.channelSnapshots.find(row => String(row.channelId) === String(channelId));
    if (!channel) return;
    closeDrawer();
    state.selectedChannelId = String(channel.channelId);

    const fields = [
      ['Sales', channel.salesName],
      ['Channel ID', channel.channelId],
      ['Channel Name', channel.channelName],
      ['Channel Status', channel.channelStatus],
      ['IB Level', channel.ibLevel],
      ['Subtree Node Count', channel.subtreeNodeCount],
      ['Client Count', channel.clientCount],
      ['Registration', channel.registration],
      ['FTD', channel.ftd],
      ['Gross', money(channel.gross)],
      ['Withdrawal', money(channel.withdrawal)],
      ['Net', money(channel.net)],
      ['Verification Status', channel.verificationStatus],
      ['asOf', asOfLabel(channel.asOf)],
      ['Source', channel.source]
    ];
    const overlay = document.createElement('div');
    overlay.className = 'channel-phase1-drawer';
    overlay.innerHTML = '<aside class="channel-phase1-drawer-panel" role="dialog" aria-modal="true" aria-label="渠道详情">' +
      '<button class="channel-phase1-drawer-close" type="button" aria-label="关闭渠道详情">关闭</button>' +
      '<h2 class="manager-title">' + escapeHtml(channel.channelName) + '</h2>' +
      '<p class="manager-sub">CRM IB 渠道详情</p>' +
      '<dl class="channel-phase1-detail">' + fields.map(([label, value]) =>
        '<dt>' + escapeHtml(label) + '</dt><dd>' + escapeHtml(value) + '</dd>'
      ).join('') + '</dl></aside>';
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
    return '<div class="kpi-card"><b>' + escapeHtml(value) + '</b><span>' +
      escapeHtml(label) + '</span><span>CRM IB subtree</span></div>';
  }

  function renderChannels() {
    const data = dataset();
    if (!data?.channelSnapshots || !data?.teamSummary || !Array.isArray(data.salesSummary)) {
      views.innerHTML = '<div class="manager-view active"><h1 class="manager-title">渠道分析</h1><p class="manager-sub">CRM IB 渠道数据文件未加载。</p></div>';
      return;
    }

    const summary = selectedSummary(data);
    if (!summary) {
      state.selectedSales = 'all';
      renderChannels();
      return;
    }
    const scope = scopedChannels(data);
    const rows = tableChannels(data);
    const label = state.selectedSales === 'all' ? '团队 CRM IB 渠道' : summary.salesName + ' · CRM IB 渠道';
    const coverage = data.coverage?.channelCoverageNote || '当前覆盖 CRM IB subtree。';
    const verification = data.verification?.qaPassed ? 'QA 已验证' : '验证状态待确认';
    const statusOptions = [
      ['all', '全部状态'], ['active', '活跃'], ['inactive', '无活动'],
      ['test_or_suspected', 'test_or_suspected'], ['unknown', 'unknown']
    ];

    views.innerHTML = '<div class="manager-view active">' +
      '<h1 class="manager-title">' + escapeHtml(label) + '</h1>' +
      '<p class="manager-sub">' + escapeHtml(data.datasetLabel || 'Team CRM IB Dataset') +
        ' · ' + verification + ' · asOf ' + escapeHtml(asOfLabel(data.asOf)) + '</p>' +
      '<div class="channel-phase1-note"><b>Coverage：</b>' + escapeHtml(coverage) + '<br><b>CPA：</b>未包含</div>' +
      '<div class="kpi-row channel-phase1-kpis">' +
        kpi(summaryMetric(summary, 'channelCount') ?? summary.topLevelIbCount, 'Top-level IB') +
        kpi(summaryMetric(summary, 'activeChannelCount') ?? summary.activeIbCount, 'Active IB') +
        kpi(summary.uniqueClientCount, 'Unique Clients') +
        kpi(summaryMetric(summary, 'registration'), 'Registration') +
        kpi(summaryMetric(summary, 'ftd'), 'FTD') +
        kpi(money(summaryMetric(summary, 'gross')), 'Gross') +
        kpi(money(summaryMetric(summary, 'withdrawal')), 'Withdrawal') +
        kpi(money(summaryMetric(summary, 'net')), 'Net') +
      '</div><div class="manager-card"><div class="filters">' +
        '<select id="channel-sales" aria-label="选择销售"><option value="all">全部团队</option>' +
        data.salesSummary.map(item => '<option value="' + escapeHtml(item.salesId) + '">' +
          escapeHtml(item.salesName) + '</option>').join('') + '</select>' +
        '<input id="channel-search" value="' + escapeHtml(state.searchQuery) +
          '" placeholder="搜索渠道名称或 Channel ID" aria-label="搜索渠道名称或 Channel ID">' +
        '<select id="channel-status" aria-label="筛选渠道状态">' +
        statusOptions.map(([value, text]) => '<option value="' + value + '"' +
          (state.statusFilter === value ? ' selected' : '') + '>' + text + '</option>').join('') +
        '</select></div><p class="quiet">当前显示 ' + rows.length + ' / ' + scope.length +
        ' 个顶级 IB。搜索、状态和排序只影响主表，不影响已验证的 KPI 汇总。</p>' +
      (rows.length ? '<table class="manager-table"><thead><tr>' +
        '<th>Sales</th><th>渠道名称</th><th>状态</th><th>Subtree</th><th>Clients</th>' +
        sortHead('Registration', 'registration') + sortHead('FTD', 'ftd') +
        sortHead('Gross', 'gross') + sortHead('Withdrawal', 'withdrawal') + sortHead('Net', 'net') +
        '<th>数据状态</th></tr></thead><tbody>' + rows.map(channel => '<tr>' +
          '<td>' + escapeHtml(channel.salesName) + '</td>' +
          '<td><button class="link-button" data-channel-id="' + escapeHtml(channel.channelId) + '">' +
            escapeHtml(channel.channelName) + '</button></td>' +
          '<td>' + escapeHtml(channel.channelStatus) + '</td><td>' + escapeHtml(channel.subtreeNodeCount) +
          '</td><td>' + escapeHtml(channel.clientCount) + '</td><td>' + escapeHtml(channel.registration) +
          '</td><td>' + escapeHtml(channel.ftd) + '</td><td>' + money(channel.gross) +
          '</td><td>' + money(channel.withdrawal) + '</td><td>' + money(channel.net) +
          '</td><td class="channel-phase1-status">' + escapeHtml(channel.verificationStatus) +
          '</td></tr>').join('') + '</tbody></table>' :
        '<p class="quiet">没有符合当前搜索或筛选条件的渠道。</p>') +
      '</div></div>';

    const salesSelect = document.getElementById('channel-sales');
    salesSelect.value = state.selectedSales;
    salesSelect.addEventListener('change', event => {
      closeDrawer();
      state.selectedSales = event.target.value;
      renderChannels();
    });
    document.getElementById('channel-search').addEventListener('input', event => {
      state.searchQuery = event.target.value;
      renderChannels();
    });
    document.getElementById('channel-status').addEventListener('change', event => {
      state.statusFilter = event.target.value;
      renderChannels();
    });
    document.querySelectorAll('[data-channel-sort]').forEach(header => {
      const sort = () => {
        const key = header.dataset.channelSort;
        state.sortDirection = state.sortKey === key ? -state.sortDirection : -1;
        state.sortKey = key;
        renderChannels();
      };
      header.addEventListener('click', sort);
      header.addEventListener('keydown', event => {
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault();
          sort();
        }
      });
    });
    document.querySelectorAll('[data-channel-id]').forEach(button => {
      button.addEventListener('click', () => openDrawer(button.dataset.channelId));
    });
  }

  document.addEventListener('keydown', event => {
    if (event.key === 'Escape' && state.selectedChannelId) closeDrawer();
  });
  channelNav.addEventListener('click', renderChannels);
})();
