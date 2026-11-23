import { api, chip, escapeHtml } from '../api.js';

export async function renderSites(el) {
  const [data, counts, protocols, cutovers] = await Promise.all([
    api('/api/sites'),
    api('/api/site-counts'),
    api('/api/protocols'),
    api('/api/cutovers'),
  ]);
  const items = data.items || [];
  const liveProtocols = (protocols.items || []).filter((p) => p.status === '生效');
  const cutoverItems = cutovers.items || [];

  const badge = (s) => {
    if (!s.cutover_id) return chip('无落点');
    return `${chip('现行')} <strong>${escapeHtml(s.cutover_code)}@${escapeHtml(s.cutover_version)}</strong>` +
      ` <span class="muted">指纹${escapeHtml(String(s.cutover_fingerprint || '').slice(0, 8))} · r${s.cutover_revision}</span>`;
  };

  el.innerHTML = `
    <div class="card">
      <h2>试验中心目录</h2>
      <p class="muted">登记临床试验中心、启停状态与访视抓取配额。停用前须先结束采集中批次。</p>
      <div class="grid">
        <div class="stat"><div class="n">${counts.total || 0}</div><div class="l">中心总数</div></div>
        <div class="stat"><div class="n">${counts['启用'] || 0}</div><div class="l">启用</div></div>
        <div class="stat"><div class="n">${counts['观察'] || 0}</div><div class="l">观察</div></div>
        <div class="stat"><div class="n">${counts['停用'] || 0}</div><div class="l">停用</div></div>
      </div>
      <div class="row">
        <input id="sname" placeholder="中心名称" />
        <input id="sregion" placeholder="区域" />
        <input id="sprincipal" placeholder="负责人" />
        <input id="squota" type="number" value="50" style="width:100px" title="配额" />
        <input id="snote" placeholder="备注" />
        <button id="create">登记中心</button>
      </div>
      <p class="muted">切流口径：${escapeHtml(cutovers.mode || '甲·快照表')} · 读侧只读切流落点快照，方案翻牌不回刷。</p>
    </div>
    <div class="card">
      <div class="row">
        <select id="filterStatus">
          <option value="">全部状态</option>
          <option>启用</option>
          <option>观察</option>
          <option>停用</option>
        </select>
        <button id="refresh" class="ghost">刷新</button>
      </div>
      <table>
        <thead>
          <tr>
            <th>编码</th><th>名称</th><th>状态</th><th>切流徽标</th><th>配额</th>
            <th>批次</th><th>缺口合计</th><th>区域/负责人</th><th>操作</th>
          </tr>
        </thead>
        <tbody>
          ${items.map((s) => `
            <tr data-id="${s.id}">
              <td>${escapeHtml(s.site_code)}</td>
              <td>
                <div>${escapeHtml(s.name)}</div>
                <div class="muted">${escapeHtml(s.note || '')}</div>
              </td>
              <td>${chip(s.status)}</td>
              <td>${badge(s)}</td>
              <td>${s.quota}</td>
              <td>${s.batch_count}</td>
              <td>${s.gap_total}</td>
              <td>${escapeHtml(s.region || '-')} / ${escapeHtml(s.principal || '-')}</td>
              <td class="row">
                <button data-st="启用" data-id="${s.id}">启用</button>
                <button data-st="观察" data-id="${s.id}" class="warn">观察</button>
                <button data-st="停用" data-id="${s.id}" class="danger">停用</button>
                <button data-q="${s.id}">配额+10</button>
                <button data-detail="${s.id}" class="ghost">详情</button>
              </td>
            </tr>`).join('')}
        </tbody>
      </table>
      <div id="siteDetail" class="detail-box" style="margin-top:12px">选择一行查看批次明细。</div>
    </div>
    <div class="card">
      <h2>协议切流闸（${escapeHtml(cutovers.mode || '甲·快照表')}）</h2>
      <p class="muted">提交切流把中心钉到生效方案：落点入快照表并贴指纹，旧落点转封存，修订号 +1。
        读侧（开批/抓取/入库/质控/补采）只读现行落点；期望修订号不符即钉点冲突，拒双成功互冲。</p>
      <div class="row">
        <select id="coSite">
          ${items.map((s) => `<option value="${s.id}" data-rev="${s.cutover_id ? s.cutover_revision : 0}">${escapeHtml(s.site_code + ' · ' + s.name)}（现行 r${s.cutover_id ? s.cutover_revision : 0}）</option>`).join('')}
        </select>
        <select id="coProto">
          ${liveProtocols.map((p) => `<option value="${p.id}">${escapeHtml(p.protocol_code + ' · ' + p.title + ' @ ' + p.version)}</option>`).join('') || '<option value="">无生效方案</option>'}
        </select>
        <input id="coRev" type="number" style="width:110px" title="期望修订号" placeholder="期望修订" />
        <input id="coNote" placeholder="切流备注" />
        <button id="coSubmit">提交切流</button>
      </div>
      <table>
        <thead>
          <tr><th>中心</th><th>方案@版号</th><th>指纹</th><th>修订</th><th>状态</th><th>时间</th><th>备注</th><th>操作</th></tr>
        </thead>
        <tbody>
          ${cutoverItems.map((c) => `
            <tr>
              <td>${escapeHtml(c.site_code || '')} ${escapeHtml(c.site_name || '')}</td>
              <td><strong>${escapeHtml(c.protocol_code)}@${escapeHtml(c.protocol_version)}</strong></td>
              <td><code>${escapeHtml(String(c.fingerprint || '').slice(0, 12))}</code></td>
              <td>r${c.revision}</td>
              <td>${chip(c.status)}</td>
              <td class="muted">${escapeHtml((c.status === '封存' ? c.sealed_at : c.created_at) || '')}</td>
              <td class="muted">${escapeHtml(c.note || '')}</td>
              <td><button data-codetail="${c.id}" class="ghost">详情</button></td>
            </tr>`).join('') || '<tr><td colspan="8" class="muted">暂无切流落点</td></tr>'}
        </tbody>
      </table>
      <div id="cutoverDetail" class="detail-box" style="margin-top:12px">切流台账：现行与封存档在此复核，封存不回刷。</div>
    </div>
    <div class="card">
      <h2>方案版本</h2>
      <p class="muted">方案生命周期台账。生效不等于切流：各中心读哪版，以切流落点为准。</p>
      <div class="row">
        <input id="ptitle" placeholder="方案标题" />
        <input id="pver" placeholder="版本" value="1.1" style="width:90px" />
        <input id="pfields" placeholder="必填字段逗号分隔" style="min-width:260px"
          value="subject_key,visit_code,visit_date,investigator,weight_kg" />
        <input id="pvisits" placeholder="访视编码" value="V1,V2,V3,EOT" />
        <button id="pcreate">登记方案</button>
      </div>
      <table>
        <thead><tr><th>编码</th><th>标题</th><th>版本</th><th>状态</th><th>必填字段</th><th>操作</th></tr></thead>
        <tbody>
          ${(protocols.items || []).map((p) => `
            <tr>
              <td>${escapeHtml(p.protocol_code)}</td>
              <td>${escapeHtml(p.title)}</td>
              <td>${escapeHtml(p.version)}</td>
              <td>${chip(p.status)}</td>
              <td class="muted">${escapeHtml((p.required_fields_list || []).join(', '))}</td>
              <td class="row">
                <button data-pstatus="生效" data-pid="${p.id}">生效</button>
                <button data-pstatus="草稿" data-pid="${p.id}" class="ghost">草稿</button>
                <button data-pstatus="废止" data-pid="${p.id}" class="danger">废止</button>
              </td>
            </tr>`).join('')}
        </tbody>
      </table>
    </div>`;

  const errBox = (msg) => {
    const box = el.querySelector('#siteDetail');
    box.textContent = '错误：' + msg;
  };
  const cutoverBox = (msg) => {
    el.querySelector('#cutoverDetail').textContent = msg;
  };

  el.querySelector('#create').onclick = async () => {
    try {
      await api('/api/sites', {
        method: 'POST',
        body: JSON.stringify({
          name: el.querySelector('#sname').value,
          region: el.querySelector('#sregion').value,
          principal: el.querySelector('#sprincipal').value,
          quota: Number(el.querySelector('#squota').value || 50),
          note: el.querySelector('#snote').value,
        }),
      });
      await renderSites(el);
    } catch (e) { errBox(e.message); }
  };

  el.querySelector('#refresh').onclick = () => renderSites(el);
  el.querySelector('#filterStatus').onchange = async (ev) => {
    const status = ev.target.value;
    const url = status ? `/api/sites?status=${encodeURIComponent(status)}` : '/api/sites';
    const filtered = await api(url);
    const tbody = el.querySelector('tbody');
    tbody.innerHTML = (filtered.items || []).map((s) => `
      <tr data-id="${s.id}">
        <td>${escapeHtml(s.site_code)}</td>
        <td>${escapeHtml(s.name)}</td>
        <td>${chip(s.status)}</td>
        <td>${badge(s)}</td>
        <td>${s.quota}</td>
        <td>${s.batch_count}</td>
        <td>${s.gap_total}</td>
        <td>${escapeHtml(s.region || '-')} / ${escapeHtml(s.principal || '-')}</td>
        <td class="muted">请点刷新回到完整操作区</td>
      </tr>`).join('');
  };

  el.querySelectorAll('[data-st]').forEach((btn) => {
    btn.onclick = async () => {
      try {
        await api(`/api/sites/${btn.dataset.id}/status`, {
          method: 'POST',
          body: JSON.stringify({ status: btn.dataset.st }),
        });
        await renderSites(el);
      } catch (e) { errBox(e.message); }
    };
  });

  el.querySelectorAll('[data-q]').forEach((btn) => {
    btn.onclick = async () => {
      const site = items.find((x) => String(x.id) === btn.dataset.q);
      try {
        await api(`/api/sites/${btn.dataset.q}/quota`, {
          method: 'POST',
          body: JSON.stringify({ quota: (site?.quota || 50) + 10 }),
        });
        await renderSites(el);
      } catch (e) { errBox(e.message); }
    };
  });

  el.querySelectorAll('[data-detail]').forEach((btn) => {
    btn.onclick = async () => {
      try {
        const detail = await api(`/api/sites/${btn.dataset.detail}`);
        el.querySelector('#siteDetail').textContent = JSON.stringify({
          中心: detail.name,
          状态: detail.status,
          切流落点: detail.cutover_id
            ? `${detail.cutover_code}@${detail.cutover_version} · 指纹${String(detail.cutover_fingerprint || '').slice(0, 8)} · r${detail.cutover_revision}`
            : '无落点',
          开放缺口: detail.open_gaps,
          批次: (detail.batches || []).map((b) => ({
            编码: b.batch_code,
            状态: b.status,
            行数: b.item_count,
            缺口: b.gap_count,
          })),
        }, null, 2);
      } catch (e) { errBox(e.message); }
    };
  });

  const coSite = el.querySelector('#coSite');
  const coRev = el.querySelector('#coRev');
  const syncRev = () => {
    const opt = coSite.selectedOptions[0];
    coRev.value = opt ? opt.dataset.rev : 0;
  };
  coSite.onchange = syncRev;
  syncRev();

  el.querySelector('#coSubmit').onclick = async () => {
    try {
      const r = await api('/api/cutovers', {
        method: 'POST',
        body: JSON.stringify({
          site_id: Number(coSite.value),
          protocol_id: Number(el.querySelector('#coProto').value),
          expected_revision: Number(coRev.value || 0),
          note: el.querySelector('#coNote').value,
        }),
      });
      cutoverBox(`切流已落定：${r.protocol_code}@${r.protocol_version} · 指纹${String(r.fingerprint).slice(0, 8)} · r${r.revision}`);
      await renderSites(el);
    } catch (e) { cutoverBox('切流被拒：' + e.message); }
  };

  el.querySelectorAll('[data-codetail]').forEach((btn) => {
    btn.onclick = async () => {
      try {
        const c = await api(`/api/cutovers/${btn.dataset.codetail}`);
        el.querySelector('#cutoverDetail').textContent = JSON.stringify({
          中心: c.site_name,
          方案: `${c.protocol_code}@${c.protocol_version}`,
          状态: c.status,
          修订: c.revision,
          指纹: c.fingerprint,
          印记校验: c.fingerprint_ok ? '通过' : '不符',
          必填字段: c.required_fields_list,
          访视编码: c.visit_codes_list,
          封存时间: c.sealed_at || '-',
          备注: c.note || '-',
        }, null, 2);
      } catch (e) { cutoverBox('错误：' + e.message); }
    };
  });

  el.querySelector('#pcreate').onclick = async () => {
    const fields = el.querySelector('#pfields').value.split(',').map((x) => x.trim()).filter(Boolean);
    const visits = el.querySelector('#pvisits').value.split(',').map((x) => x.trim()).filter(Boolean);
    try {
      await api('/api/protocols', {
        method: 'POST',
        body: JSON.stringify({
          title: el.querySelector('#ptitle').value,
          version: el.querySelector('#pver').value,
          required_fields: fields,
          visit_codes: visits,
        }),
      });
      await renderSites(el);
    } catch (e) { errBox(e.message); }
  };

  el.querySelectorAll('[data-pstatus]').forEach((btn) => {
    btn.onclick = async () => {
      try {
        await api(`/api/protocols/${btn.dataset.pid}/status`, {
          method: 'POST',
          body: JSON.stringify({ status: btn.dataset.pstatus }),
        });
        await renderSites(el);
      } catch (e) { errBox(e.message); }
    };
  });
}
