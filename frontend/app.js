/* ── TableAgent frontend ─────────────────────────────── */
let attachedTables = [], graph = null, selectedNode = null;
let panX = 20, panY = 20, dragging = false, dragStart = null, zoom = 1;
let currentProject = null, filesList = [];
const NODE_W = 320, NODE_PAD = 14, LINE_H = 18, MIN_H = 52;

const esc = s => String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
const escJs = s => String(s).replace(/\\/g,'\\\\').replace(/'/g,"\\'").replace(/"/g,'\\"');

async function api(endpoint, body, getPath) {
  try {
    const r = getPath
      ? await fetch(getPath)
      : await fetch(endpoint, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
    if (!r.ok) { const t = await r.text(); console.error('API error:', r.status, t); return {error: `Erro ${r.status}`}; }
    return r.json();
  } catch(e) { console.error('API fetch error:', e); return {error: e.message}; }
}

/* ── Sidebar ─────────────────────────────────────────── */
async function loadProjects() {
  const list = await api(null,null,'/api/projects');
  const el = document.getElementById('project-list');
  el.innerHTML = list.map(p => `
    <div class="project-item" onclick="openProject('${escJs(p.path)}')">
      <span class="pi-name" ondblclick="event.stopPropagation();renameProject('${escJs(p.path)}')">${esc(p.name)}</span>
      <button class="btn-remove btn-icon" onclick="event.stopPropagation();deleteProject('${escJs(p.path)}','${escJs(p.name)}')" title="Excluir">✕</button>
    </div>`).join('');
}

async function newProject() {
  const {path} = await api(null,null,'/api/pick_folder');
  if (!path) return;
  const name = prompt('Nome do projeto:', path.split('/').pop()) || path.split('/').pop();
  const r = await api('/api/new_project', {path, name});
  if (r.error) return alert(r.error);
  currentProject = {path, name}; filesList = r.files || [];
  showActiveProject(); loadProjects();
}

async function openProject(path) {
  const r = await api('/api/open_project', {path});
  if (r.error) return alert(r.error);
  currentProject = {path, name: r.name}; filesList = r.files || [];
  showActiveProject();
  if (r.graph && r.graph.nodes && Object.keys(r.graph.nodes).length) {
    const graphTables = [...new Set(Object.values(r.graph.nodes).flatMap(n => n.table.split(', ')))];
    graphTables.forEach(t => { if (t && !attachedTables.includes(t)) attachedTables.push(t); });
    renderChips();
  }
}

async function importSheet() {
  const {path} = await api(null,null,'/api/pick_file');
  if (!path) return;
  const r = await api('/api/import_sheet', {path});
  if (r.error) return alert(r.error);
  filesList = r; renderSidebar();
  const newFile = r[r.length - 1];
  if (newFile && newFile.tables.length) dataTable = newFile.tables[0].name;
}

async function renameProject(path) {
  const name = prompt('Novo nome:');
  if (!name) return;
  await api('/api/rename_project', {path, name});
  if (currentProject && currentProject.path === path) currentProject.name = name;
  showActiveProject(); loadProjects();
}

async function deleteProject(path, name) {
  if (!confirm(`Excluir projeto "${name}"?`)) return;
  await api('/api/delete_project', {path});
  if (currentProject && currentProject.path === path) {
    currentProject = null; filesList = [];
    document.getElementById('active-project').classList.add('hidden');
  }
  loadProjects();
}

async function removeSource(file, table) {
  const r = await api('/api/remove_source', {file, table});
  if (Array.isArray(r)) {
    filesList = r; renderSidebar();
    // Clear data view if showing removed table, load another if available
    const removed = table || '';
    if (dataTable === removed || !r.some(f => f.tables.some(t => t.name === dataTable))) {
      const first = r.flatMap(f => f.tables)[0];
      if (first) loadDataView(first.name);
      else clearDataView();
    }
  }
}

async function renameTable(oldName, el) {
  const newName = prompt('Novo nome:', oldName);
  if (!newName || newName === oldName) return;
  const r = await api('/api/rename_table', {old: oldName, new: newName});
  if (r.ok) { renderSidebar(); loadProjects(); }
}

function showActiveProject() {
  const ap = document.getElementById('active-project');
  const pl = document.getElementById('project-list');
  if (!currentProject) { ap.classList.add('hidden'); pl.classList.remove('hidden'); return; }
  ap.classList.remove('hidden');
  pl.classList.add('hidden');
  document.getElementById('project-name').textContent = currentProject.name;
  renderSidebar();
}

function renderSidebar() {
  const tree = document.getElementById('file-tree');
  tree.innerHTML = filesList.map(f => {
    const tables = f.tables.map(t => {
      const meta = t.issues > 0
        ? `<div class="table-meta"><span class="issue-badge" onclick="showDataIssues('${escJs(t.name)}')">⚠ ${t.issues} valores a revisar</span></div>`
        : '';
      return `<div class="table-item" draggable="true" ondragstart="onDragTable(event,'${escJs(t.name)}')"
                   onclick="selectTable('${escJs(t.name)}')" ondblclick="renameTable('${escJs(t.name)}',this)">
        <span class="table-name">${esc(t.name)}</span>
        <span class="table-actions">
          <button class="btn-icon" onclick="event.stopPropagation();exportTable('${escJs(t.name)}')" title="Exportar">⤓</button>
          <button class="btn-icon" onclick="event.stopPropagation();removeSource(null,'${escJs(t.name)}')" title="Remover">✕</button>
        </span>
      </div>${meta}`;
    }).join('');
    return `<div class="file-item">
      <div class="file-header">${esc(f.name)}
        <button class="btn-remove btn-icon" onclick="removeSource('${escJs(f.name)}')" title="Remover arquivo">✕</button>
      </div>${tables}</div>`;
  }).join('');
}

function toggleSidebar() {
  const sb = document.getElementById('sidebar');
  const btn = document.getElementById('sidebar-open');
  sb.classList.toggle('collapsed');
  btn.classList.toggle('hidden', !sb.classList.contains('collapsed'));
}

/* ── Chat ────────────────────────────────────────────── */
function addMsg(role, text, result, beam, sqls) {
  const el = document.getElementById('chat-messages');
  const div = document.createElement('div');
  div.className = `msg msg-${role}`;
  if (result && result.columns) {
    // Show header text (lines before the table data)
    const lines = text.split('\n');
    const header = lines.filter(l => l.startsWith('📌') || l.startsWith('📊')).join('\n');
    if (header) { const p = document.createElement('p'); p.textContent = header; div.appendChild(p); }
    // Render table
    const tbl = document.createElement('table');
    tbl.className = 'chat-table';
    const thead = document.createElement('thead');
    thead.innerHTML = '<tr>' + result.columns.map(c => `<th>${esc(String(c))}</th>`).join('') + '</tr>';
    tbl.appendChild(thead);
    const tbody = document.createElement('tbody');
    const rows = result.data.slice(0, 50);
    rows.forEach(r => {
      tbody.innerHTML += '<tr>' + r.map(v => `<td>${esc(v == null ? '' : String(v))}</td>`).join('') + '</tr>';
    });
    tbl.appendChild(tbody);
    div.appendChild(tbl);
    if (result.data.length > 50) {
      const note = document.createElement('p');
      note.className = 'muted';
      note.textContent = `… e mais ${result.data.length - 50} linhas`;
      div.appendChild(note);
    }
    const actions = document.createElement('div');
    actions.className = 'msg-actions';
    const btn = document.createElement('button');
    btn.className = 'btn-export';
    btn.textContent = '⬇ Exportar';
    btn.onclick = () => exportChatResult(result);
    actions.appendChild(btn);
    // "Ver nos dados" button
    const srcIdx = result.columns.indexOf('source');
    const tableName = srcIdx >= 0 && result.data[0] ? result.data[0][srcIdx] : attachedTables[0];
    if (tableName) {
      const see = document.createElement('button');
      see.className = 'btn-export'; see.textContent = '🔍 Ver nos dados';
      see.onclick = () => showResultInData(result, tableName);
      actions.appendChild(see);
    }
    // Feedback buttons
    if (beam) {
      const up = document.createElement('button');
      up.className = 'btn-feedback'; up.textContent = '👍';
      up.onclick = () => { sendFeedback(beam, 'good'); up.disabled = down.disabled = true; up.classList.add('chosen'); };
      const down = document.createElement('button');
      down.className = 'btn-feedback'; down.textContent = '👎';
      down.onclick = () => { sendFeedback(beam, 'bad'); up.disabled = down.disabled = true; down.classList.add('chosen'); };
      actions.appendChild(up);
      actions.appendChild(down);
    }
    // SQL info button
    if (sqls && sqls.length) {
      const sqlList = Array.isArray(sqls) ? sqls : [sqls];
      const info = document.createElement('button');
      info.className = 'btn-info'; info.textContent = 'ⓘ';
      info.title = 'Ver SQL';
      const pre = document.createElement('pre');
      pre.className = 'sql-detail hidden';
      pre.textContent = sqlList.join('\n\n');
      info.onclick = () => pre.classList.toggle('hidden');
      actions.appendChild(info);
      div.appendChild(actions);
      div.appendChild(pre);
    } else {
      div.appendChild(actions);
    }
  } else {
    div.textContent = text;
  }
  el.appendChild(div);
  el.scrollTop = el.scrollHeight;
}

async function sendFeedback(beam, rating) {
  await api('/api/feedback', {
    rating,
    user_question: beam.user_question,
    question_id: beam.question_id,
    question_text: beam.question_text,
    sql: beam.sql,
    required_slots: beam.required_slots,
  });
}

function exportChatResult(result) {
  const form = document.createElement('form');
  form.method = 'POST'; form.action = '/api/download_result';
  form.target = '_blank';
  const inp = document.createElement('input');
  inp.type = 'hidden'; inp.name = 'data'; inp.value = JSON.stringify(result);
  form.appendChild(inp);
  document.body.appendChild(form);
  form.submit();
  form.remove();
}

async function sendChat() {
  const input = document.getElementById('chat-input');
  const msg = input.value.trim();
  if (!msg) return;
  input.value = '';
  hideTableMenu();
  addMsg('user', msg);
  const body = {message: msg, table_names: [...attachedTables]};
  if (selectedNode) body.ref_node = selectedNode;
  const r = await api('/api/chat', body);
  const beam = r.beam ? {...r, user_question: msg, sql: (r.sql || [])[0] || ''} : null;
  addMsg('agent', r.response || r.error || 'Sem resposta', r.result, beam, r.sql);
}

function onChatInput(e) {
  const v = e.target.value;
  const i = v.lastIndexOf('#');
  if (i >= 0 && (i === 0 || v[i-1] === ' ')) {
    const filter = v.slice(i+1).toLowerCase();
    showTableMenu(filter);
  } else hideTableMenu();
}

function clearRef() {
  selectedNode = null;
  document.getElementById('chat-ref').classList.add('hidden');
}

/* ── Table attachment ────────────────────────────────── */
function attachTable(name) {
  if (attachedTables.includes(name)) return;
  attachedTables.push(name);
  renderChips();
  if (!document.getElementById('right-panel').classList.contains('hidden')) buildAnalysis();
}

function detachTable(name) {
  attachedTables = attachedTables.filter(t => t !== name);
  renderChips();
  if (!document.getElementById('right-panel').classList.contains('hidden') && attachedTables.length) buildAnalysis();
}

function renderChips() {
  const html = attachedTables.map(n =>
    `<span class="chip">${esc(n)} <button onclick="detachTable('${escJs(n)}')">✕</button></span>`
  ).join('');
  document.getElementById('chips').innerHTML = html;
  const pc = document.getElementById('panel-chips');
  if (pc) pc.innerHTML = html;
}

async function showTableMenu(filter) {
  const names = await api(null,null,'/api/table_names');
  const avail = names.filter(n => !attachedTables.includes(n));
  const filtered = filter != null ? avail.filter(n => n.toLowerCase().includes(filter)) : avail;
  const menu = document.getElementById('table-menu');
  if (!filtered.length) { hideTableMenu(); return; }
  menu.innerHTML = filtered.map(n =>
    `<div class="menu-item" onmousedown="pickTableFromMenu('${escJs(n)}')">${esc(n)}</div>`
  ).join('');
  menu.classList.remove('hidden');
}

async function showPanelTableMenu() {
  const names = await api(null,null,'/api/table_names');
  const avail = names.filter(n => !attachedTables.includes(n));
  const menu = document.getElementById('panel-table-menu');
  if (!avail.length) { menu.classList.add('hidden'); return; }
  menu.innerHTML = avail.map(n =>
    `<div class="menu-item" onmousedown="pickPanelTable('${escJs(n)}')">${esc(n)}</div>`
  ).join('');
  menu.classList.remove('hidden');
}

function pickPanelTable(name) {
  document.getElementById('panel-table-menu').classList.add('hidden');
  attachTable(name);
}

function pickTableFromMenu(name) {
  attachTable(name);
  const input = document.getElementById('chat-input');
  const i = input.value.lastIndexOf('#');
  if (i >= 0) input.value = input.value.slice(0, i);
  hideTableMenu();
}

function hideTableMenu() { document.getElementById('table-menu').classList.add('hidden'); }

function onDragTable(e, name) { e.dataTransfer.setData('text/plain', name); }
function onDrop(e) {
  e.preventDefault();
  const name = e.dataTransfer.getData('text/plain');
  if (name) attachTable(name);
}

function exportTable(name) {
  // Trigger data download via loadDataView then download
  window.open(`/api/data?table=${encodeURIComponent(name)}&size=999999`);
}

/* ── Mind-map ────────────────────────────────────────── */
function nodeHeight(text, topic) {
  const chars = Math.max(1, Math.floor((NODE_W - NODE_PAD*2) / 8));
  const lines = Math.ceil((text||'').length / chars);
  const extra = topic ? 18 : 0;
  return Math.max(MIN_H, lines * LINE_H + NODE_PAD * 2 + extra);
}

function renderGraph() {
  if (!graph || !graph.nodes) return;
  const svg = document.getElementById('mindmap');
  const nodes = graph.nodes, roots = graph.roots || [];

  // Layout: BFS tree, assign x/y
  const pos = {}; let yOff = 0;
  function layout(id, depth) {
    if (pos[id]) return;
    const n = nodes[id]; if (!n) return;
    const h = nodeHeight(n.question, n.topic);
    pos[id] = {x: depth * (NODE_W + 60), y: yOff, h};
    yOff += h + 18;
    (n.children || []).forEach(c => layout(c, depth + 1));
  }
  roots.forEach(r => layout(r, 0));

  // Offset to center
  const minY = Math.min(...Object.values(pos).map(p => p.y));
  for (const p of Object.values(pos)) p.y -= minY;

  let html = `<g transform="translate(${panX},${panY}) scale(${zoom})">`;
  // Edges
  for (const [id, p] of Object.entries(pos)) {
    const n = nodes[id]; if (!n) continue;
    for (const cid of (n.children || [])) {
      const cp = pos[cid]; if (!cp) continue;
      const x1 = p.x + NODE_W, y1 = p.y + p.h/2;
      const x2 = cp.x, y2 = cp.y + cp.h/2;
      const mx = (x1 + x2) / 2;
      html += `<path d="M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2},${y2}" fill="none" stroke="var(--border)" stroke-width="1.5" opacity=".6"/>`;
    }
  }
  // Nodes
  for (const [id, p] of Object.entries(pos)) {
    const n = nodes[id]; if (!n) continue;
    const x = p.x, y = p.y;
    const isSel = selectedNode === id;
    const fill = isSel ? 'var(--card)' : n.explored ? 'var(--card)' : 'var(--surface)';
    const stroke = isSel ? 'var(--accent)' : 'var(--border)';
    const sw = isSel ? 2.5 : 1.5;
    html += `<rect x="${x}" y="${y}" width="${NODE_W}" height="${p.h}" rx="12" fill="${fill}" stroke="${stroke}" stroke-width="${sw}" cursor="pointer" onclick="clickNode('${escJs(id)}')"/>`;
    const topic = n.topic || '';
    if (topic) html += `<text x="${x+NODE_PAD}" y="${y+16}" font-size="10" fill="var(--muted)" font-weight="600" pointer-events="none">${esc(topic.toUpperCase())}</text>`;
    const textY = topic ? y + 22 : y + NODE_PAD;
    const textH = topic ? p.h - 22 - NODE_PAD : p.h - NODE_PAD*2;
    html += `<foreignObject x="${x+NODE_PAD}" y="${textY}" width="${NODE_W-NODE_PAD*2}" height="${textH}">
      <div xmlns="http://www.w3.org/1999/xhtml" style="font-size:14px;color:var(--text);overflow:hidden;cursor:pointer;line-height:1.45;font-weight:500" onclick="clickNode('${escJs(id)}')">${esc(n.question)}</div>
    </foreignObject>`;
    if (n.result && n.result.data && n.result.data.length) {
      html += `<rect x="${x+NODE_W-28}" y="${y+p.h-22}" width="20" height="16" rx="4" fill="var(--accent)" cursor="pointer" onclick="openResultModal('${escJs(id)}')"/>`;
      html += `<text x="${x+NODE_W-18}" y="${y+p.h-11}" font-size="9" fill="var(--bg)" text-anchor="middle" dominant-baseline="middle" pointer-events="none" font-weight="700">${n.result.data.length}</text>`;
    }
  }
  html += '</g>';
  svg.innerHTML = html;
}

async function clickNode(id) {
  selectedNode = id;
  const n = graph.nodes[id];
  if (!n) return;
  renderGraph();

  const det = document.getElementById('node-detail');
  document.getElementById('node-detail-title').textContent = n.question;
  let body = '';
  if (n.summary) body += `<p style="margin-bottom:10px;color:var(--text);line-height:1.5">${esc(n.summary)}</p>`;
  if (n.result && n.result.columns) {
    body += '<div class="detail-result"><table><thead><tr>' + n.result.columns.map(c => `<th>${esc(c)}</th>`).join('') + '</tr></thead><tbody>';
    (n.result.data || []).slice(0, 5).forEach(row => {
      body += '<tr>' + row.map(v => `<td>${esc(v ?? '')}</td>`).join('') + '</tr>';
    });
    body += '</tbody></table></div>';
    if (n.result.data.length > 5) body += `<p class="detail-more">… e mais ${n.result.data.length - 5} linhas</p>`;
  }
  body += '<div class="detail-actions">';
  if (n.result && n.result.data && n.result.data.length) body += `<button class="btn-detail-action" onclick="openResultModal('${escJs(id)}')">📊 Ver esta análise completa</button>`;
  if (!n.explored) body += `<button class="btn-detail-action btn-detail-expand" onclick="expandNode('${escJs(id)}')">🔍 Expandir análise</button>`;
  body += '</div>';

  document.getElementById('chat-ref').textContent = `📌 Ref: ${n.question}`;
  document.getElementById('chat-ref').classList.remove('hidden');
  document.getElementById('node-detail-body').innerHTML = body;
  det.classList.remove('hidden');
}

function closeDetail() { document.getElementById('node-detail').classList.add('hidden'); }

/* ── Draggable node detail ───────────────────────────── */
(function() {
  const det = document.getElementById('node-detail');
  let dx = 0, dy = 0, mx = 0, my = 0, active = false;
  det.addEventListener('mousedown', e => {
    if (e.target.tagName === 'BUTTON' || e.target.closest('button') || e.target.tagName === 'TD') return;
    active = true; mx = e.clientX; my = e.clientY;
    det.style.cursor = 'grabbing';
    e.preventDefault();
  });
  document.addEventListener('mousemove', e => {
    if (!active) return;
    dx = e.clientX - mx; dy = e.clientY - my; mx = e.clientX; my = e.clientY;
    const r = det.getBoundingClientRect();
    const parent = det.parentElement.getBoundingClientRect();
    let newLeft = r.left - parent.left + dx;
    let newTop = r.top - parent.top + dy;
    newLeft = Math.max(0, Math.min(parent.width - r.width, newLeft));
    newTop = Math.max(0, Math.min(parent.height - r.height, newTop));
    det.style.left = newLeft + 'px'; det.style.top = newTop + 'px';
    det.style.right = 'auto';
  });
  document.addEventListener('mouseup', () => { active = false; det.style.cursor = 'grab'; });
})();

async function expandNode(id) {
  const prog = document.getElementById('expand-progress');
  const bar = document.getElementById('expand-bar');
  const label = document.getElementById('expand-label');
  prog.classList.remove('hidden');
  bar.style.width = '0%';
  label.textContent = 'Expandindo…';

  const es = new EventSource(`/api/expand_stream?id=${encodeURIComponent(id)}`);
  es.onmessage = e => {
    const d = JSON.parse(e.data);
    if (d.type === 'node') {
      graph.nodes[d.id] = d.node;
      const pct = Math.round((d.progress / d.total) * 100);
      bar.style.width = pct + '%';
      label.textContent = `Expandindo… ${d.progress}/${d.total}`;
      // Add child to parent
      const parent = graph.nodes[id];
      if (parent && !parent.children.includes(d.id)) parent.children.push(d.id);
      renderGraph();
    }
    if (d.type === 'done') {
      es.close();
      if (graph.nodes[id]) graph.nodes[id].explored = true;
      renderGraph();
      setTimeout(() => prog.classList.add('hidden'), 800);
    }
    if (d.type === 'error') { es.close(); prog.classList.add('hidden'); }
  };
  es.onerror = () => { es.close(); prog.classList.add('hidden'); };
}

/* ── Result modal ────────────────────────────────────── */
let modalResult = null;
function openResultModal(id) {
  const n = graph.nodes[id]; if (!n || !n.result) return;
  modalResult = n.result;
  document.getElementById('result-modal-title').textContent = n.question;
  let html = '<table><thead><tr>' + n.result.columns.map(c => `<th>${esc(c)}</th>`).join('') + '</tr></thead><tbody>';
  n.result.data.forEach(row => { html += '<tr>' + row.map(v => `<td>${esc(v ?? '')}</td>`).join('') + '</tr>'; });
  html += '</tbody></table>';
  html += `<div class="download-row">
    <button class="btn-download" onclick="downloadResult('csv')">⤓ CSV</button>
    <button class="btn-download" onclick="downloadResult('xlsx')">⤓ XLSX</button>
  </div>`;
  document.getElementById('result-modal-body').innerHTML = html;
  document.getElementById('result-modal').classList.remove('hidden');
}

function closeResultModal() { document.getElementById('result-modal').classList.add('hidden'); modalResult = null; }

async function downloadResult(fmt) {
  if (!modalResult) return;
  if (fmt === 'csv') {
    const hdr = modalResult.columns.join(',');
    const rows = modalResult.data.map(r => r.map(v => `"${String(v ?? '').replace(/"/g,'""')}"`).join(','));
    const blob = new Blob([hdr + '\n' + rows.join('\n')], {type: 'text/csv'});
    const a = document.createElement('a'); a.href = URL.createObjectURL(blob); a.download = 'resultado.csv'; a.click();
  } else {
    const r = await fetch('/api/download_result', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify(modalResult)});
    const blob = await r.blob();
    const a = document.createElement('a'); a.href = URL.createObjectURL(blob); a.download = 'resultado.xlsx'; a.click();
  }
}

/* ── Analysis ────────────────────────────────────────── */
async function selectTable(name) {
  attachTable(name);
  dataTable = name;
  showPanel();
  showTab('map');
  await buildAnalysis();
}

let _buildingAnalysis = false;
async function buildAnalysis() {
  if (!attachedTables.length || _buildingAnalysis) return;
  _buildingAnalysis = true;
  try {
    const r = await api('/api/select_table', {names: attachedTables});
    if (r.error) { console.warn('buildAnalysis:', r.error); return; }
    graph = r;
    renderGraph();
  } finally { _buildingAnalysis = false; }
}

/* ── Data view ───────────────────────────────────────── */
let dataPage = 0, dataTable = '';

function showDataIssues(name) {
  showPanel(); showTab('data'); loadDataView(name);
}

function clearDataView() {
  dataTable = '';
  document.getElementById('data-issues').innerHTML = '';
  document.querySelector('#data-table thead').innerHTML = '';
  document.querySelector('#data-table tbody').innerHTML = '';
  document.getElementById('data-pager').innerHTML = '';
}

async function loadDataView(name) {
  dataTable = name; dataPage = 0;
  try {
    const [issues, data] = await Promise.all([
      api(null, null, `/api/issues?table=${encodeURIComponent(name)}`),
      api(null, null, `/api/data?table=${encodeURIComponent(name)}&page=0&size=50`)
    ]);
    console.log('loadDataView:', name, 'data:', data?.total);
    renderIssues(Array.isArray(issues) ? issues : [], name);
    if (data.columns) renderDataTable(data, name);
    else console.warn('loadDataView: no columns in response', data);
  } catch(e) { console.error('loadDataView error:', e); }
}

function renderIssues(issues, table) {
  const el = document.getElementById('data-issues');
  if (!issues.length) { el.innerHTML = '<div class="no-issues">✅ Nenhum problema encontrado</div>'; return; }
  el.innerHTML = `<div class="data-section-title">⚠ ${issues.length} valor(es) a revisar</div>` + issues.map(iss => {
    if (iss.type === 'dedup') {
      const a = iss.value, b = iss.suggestion || '';
      const cntA = iss.suggestion_count || '?', cntB = iss.suggestion_count || '?';
      return `<div class="issue-card">
        <div class="issue-type">Possível duplicata</div>
        <div class="issue-hint">Coluna: <b>${esc(iss.column)}</b></div>
        <div class="dedup-options">
          <button class="dedup-pick" onclick="resolveDedup('${escJs(table)}',${iss.id},'${escJs(a)}')">
            <div class="dedup-val">${esc(a)}</div><div class="dedup-count">${cntA}×</div>
          </button>
          <button class="dedup-pick" onclick="resolveDedup('${escJs(table)}',${iss.id},'${escJs(b)}')">
            <div class="dedup-val">${esc(b)}</div><div class="dedup-count">${cntB}×</div>
          </button>
        </div>
        <div class="issue-actions">
          <button class="btn-edit" onclick="resolveDedupCustom('${escJs(table)}',${iss.id},'${escJs(a)}','${escJs(b)}')">Outro nome…</button>
          <button class="btn-dismiss" onclick="resolveIssue('${escJs(table)}',${iss.id},'dismiss')">Não é duplicata</button>
        </div>
      </div>`;
    }
    return `<div class="issue-card">
      <div class="issue-type">${esc(iss.type)}</div>
      <div class="issue-hint">Coluna: <b>${esc(iss.column)}</b> — "${esc(iss.value)}" → "${esc(iss.suggestion || '')}"</div>
      <div class="issue-actions">
        <button class="btn-apply" onclick="resolveIssue('${escJs(table)}',${iss.id},'apply')">Aplicar</button>
        <button class="btn-edit" onclick="resolveIssueCustom('${escJs(table)}',${iss.id},'${escJs(iss.value)}')">Editar</button>
        <button class="btn-dismiss" onclick="resolveIssue('${escJs(table)}',${iss.id},'dismiss')">Ignorar</button>
      </div>
    </div>`;
  }).join('');
}

let _highlightValues = new Set();

async function showResultInData(result, tableName) {
  _highlightValues.clear();
  // Collect all text values from result (item names, labels — not numbers)
  result.data.forEach(row => {
    row.forEach(v => {
      if (v != null && typeof v === 'string' && v.trim() && v !== tableName) _highlightValues.add(v);
      // Also match stringified numbers for exact cell matches
      if (v != null && typeof v === 'number') _highlightValues.add(String(v));
    });
  });
  showPanel(); showTab('data');
  await loadDataView(tableName);
}

function renderDataTable(data, table) {
  const thead = document.querySelector('#data-table thead');
  const tbody = document.querySelector('#data-table tbody');
  thead.innerHTML = '<tr>' + data.columns.map(c => `<th>${esc(c)}</th>`).join('') + '</tr>';
  const startRow = data.page * data.size;
  tbody.innerHTML = data.data.map((row, ri) => {
    const match = _highlightValues.size && row.some(v => v != null && _highlightValues.has(String(v)));
    return `<tr${match ? ' class="row-highlight" onclick="this.classList.add(\'seen\')"' : ''}>` + row.map((v, ci) =>
      `<td contenteditable="true" onblur="editCell('${escJs(table)}',${startRow + ri},'${escJs(data.columns[ci])}',this)">${esc(v ?? '')}</td>`
    ).join('') + '</tr>';
  }).join('');
  // Scroll to first highlight
  const hl = tbody.querySelector('.row-highlight');
  if (hl) setTimeout(() => hl.scrollIntoView({behavior: 'smooth', block: 'center'}), 100);
  _highlightValues.clear();
  // Pager
  const pager = document.getElementById('data-pager');
  const pages = Math.ceil(data.total / data.size);
  if (pages <= 1) { pager.innerHTML = ''; return; }
  let ph = '';
  if (data.page > 0) ph += `<button onclick="loadPage(${data.page - 1})">← Anterior</button>`;
  ph += `<span>${data.page + 1} / ${pages}</span>`;
  if (data.page < pages - 1) ph += `<button onclick="loadPage(${data.page + 1})">Próxima →</button>`;
  pager.innerHTML = ph;
}

async function loadPage(p) {
  dataPage = p;
  const data = await api(null, null, `/api/data?table=${encodeURIComponent(dataTable)}&page=${p}&size=50`);
  if (data.columns) renderDataTable(data, dataTable);
}

async function resolveIssue(table, issueId, action) {
  const r = await api('/api/resolve_issue', {table, issue_id: issueId, action});
  if (r.ok) { updateIssueBadge(table, r.remaining); loadDataView(table); }
}

async function resolveDedup(table, issueId, chosen) {
  const r = await api('/api/resolve_issue', {table, issue_id: issueId, action: 'apply', chosen});
  if (r.ok) { updateIssueBadge(table, r.remaining); loadDataView(table); }
}

async function resolveDedupCustom(table, issueId, valA, valB) {
  const custom = prompt(`Substituir "${valA}" e "${valB}" por:`);
  if (!custom) return;
  const r = await api('/api/resolve_issue', {table, issue_id: issueId, action: 'apply', custom});
  if (r.ok) { updateIssueBadge(table, r.remaining); loadDataView(table); }
}

async function resolveIssueCustom(table, issueId, currentVal) {
  const custom = prompt('Novo valor:', currentVal);
  if (custom == null) return;
  const r = await api('/api/resolve_issue', {table, issue_id: issueId, action: 'apply', custom});
  if (r.ok) { updateIssueBadge(table, r.remaining); loadDataView(table); }
}

function updateIssueBadge(table, remaining) {
  document.querySelectorAll('.table-meta').forEach(el => el.remove());
  // Re-render sidebar to update badges
  const f = filesList.find(f => f.tables.some(t => t.name === table));
  if (f) { const t = f.tables.find(t => t.name === table); if (t) t.issues = remaining; }
  renderSidebar();
}

async function editCell(table, row, col, el) {
  const value = el.textContent;
  await api('/api/edit_cell', {table, row, col, value});
}

/* ── Panel / Tabs ────────────────────────────────────── */
function showPanel() {
  document.getElementById('right-panel').classList.remove('hidden');
  document.getElementById('panel-toggle').classList.add('panel-open');
  if (!graph && attachedTables.length) buildAnalysis();
}
function hidePanel() {
  document.getElementById('right-panel').classList.add('hidden');
  document.getElementById('panel-toggle').classList.remove('panel-open');
}
function togglePanel() {
  const p = document.getElementById('right-panel');
  p.classList.toggle('hidden');
  document.getElementById('panel-toggle').classList.toggle('panel-open', !p.classList.contains('hidden'));
}

function showTab(tab) {
  document.querySelectorAll('.tab').forEach((t, i) => {
    t.classList.toggle('active', (i === 0 && tab === 'map') || (i === 1 && tab === 'data'));
  });
  document.getElementById('view-map').classList.toggle('hidden', tab !== 'map');
  document.getElementById('view-data').classList.toggle('hidden', tab !== 'data');
  if (tab === 'data') {
    // Always load data for current or first available table
    const target = dataTable || (filesList.flatMap(f => f.tables)[0] || {}).name;
    if (target) loadDataView(target);
  }
}

/* ── SVG pan + zoom ──────────────────────────────────── */
const svgEl = document.getElementById('mindmap');
svgEl.addEventListener('mousedown', e => {
  if (e.target.closest('foreignObject') || e.target.tagName === 'rect') return;
  dragging = true; dragStart = {x: e.clientX - panX, y: e.clientY - panY};
  e.preventDefault();
});
document.addEventListener('mousemove', e => {
  if (!dragging || !dragStart) return;
  panX = e.clientX - dragStart.x; panY = e.clientY - dragStart.y;
  renderGraph();
});
document.addEventListener('mouseup', () => { dragging = false; });
svgEl.addEventListener('wheel', e => {
  e.preventDefault();
  const rect = svgEl.getBoundingClientRect();
  const mx = e.clientX - rect.left, my = e.clientY - rect.top;
  const oldZ = zoom;
  zoom = Math.max(0.3, Math.min(3, zoom * (e.deltaY < 0 ? 1.12 : 0.89)));
  // Zoom toward cursor
  panX = mx - (mx - panX) * (zoom / oldZ);
  panY = my - (my - panY) * (zoom / oldZ);
  renderGraph();
}, {passive: false});

function zoomIn() { zoom = Math.min(3, zoom * 1.25); renderGraph(); }
function zoomOut() { zoom = Math.max(0.3, zoom * 0.8); renderGraph(); }
function zoomReset() { zoom = 1; panX = 20; panY = 20; renderGraph(); }

/* ── Resize handles ──────────────────────────────────── */
function initResize(handleId, getTargets) {
  const handle = document.getElementById(handleId);
  if (!handle) return;
  let active = false, startX;
  handle.addEventListener('mousedown', e => { active = true; startX = e.clientX; handle.classList.add('active'); e.preventDefault(); });
  document.addEventListener('mousemove', e => {
    if (!active) return;
    const dx = e.clientX - startX; startX = e.clientX;
    getTargets(dx);
  });
  document.addEventListener('mouseup', () => { active = false; handle.classList.remove('active'); });
}
initResize('resize-left', dx => {
  const sb = document.getElementById('sidebar');
  sb.style.width = Math.max(160, Math.min(400, sb.offsetWidth + dx)) + 'px';
});
initResize('resize-right', dx => {
  const rp = document.getElementById('right-panel');
  rp.style.width = Math.max(250, Math.min(600, rp.offsetWidth - dx)) + 'px';
});

/* ── Model selector ──────────────────────────────────── */
async function loadModels() {
  const models = await api(null, null, '/api/models');
  const sel = document.getElementById('model-select');
  sel.innerHTML = models.map(m =>
    `<option value="${escJs(m.id)}" ${m.loaded ? 'selected' : ''}>${esc(m.name)}</option>`
  ).join('');
}

async function switchModel(id) {
  const sel = document.getElementById('model-select');
  sel.disabled = true;
  addMsg('system', id === 'heuristic' ? 'Modo heurística ativado.' : id === 'google' ? 'Modo Google AI ativado.' : `Carregando modelo ${id}…`);
  const r = await api('/api/set_model', {id});
  sel.disabled = false;
  if (r.error) addMsg('system', `Erro: ${r.error}`);
  else if (id !== 'heuristic' && id !== 'google') addMsg('system', `Modelo ${id} carregado.`);
}

/* ── Settings ────────────────────────────────────────── */
async function openSettings() {
  const s = await api(null, null, '/api/settings');
  document.getElementById('set-google-key').value = s.has_google_key ? s.google_api_key : '';
  document.getElementById('set-google-model').value = s.google_model || 'gemma-3-4b-it';
  document.getElementById('settings-status').textContent = '';
  document.getElementById('settings-modal').classList.remove('hidden');
}
function closeSettings() { document.getElementById('settings-modal').classList.add('hidden'); }
async function saveSettings() {
  const key = document.getElementById('set-google-key').value.trim();
  const model = document.getElementById('set-google-model').value.trim();
  const body = {google_model: model};
  if (key && !key.startsWith('*')) body.google_api_key = key;
  const r = await api('/api/settings', body);
  document.getElementById('settings-status').textContent = r.error || 'Salvo ✓';
  loadModels();
}

/* ── Init ────────────────────────────────────────────── */
async function restoreState() {
  const s = await api(null, null, '/api/state');
  if (!s.active) return;
  currentProject = {path: s.path, name: s.name};
  filesList = s.files || [];
  showActiveProject();
  if (s.graph && s.graph.nodes) {
    const graphTables = [...new Set(Object.values(s.graph.nodes).flatMap(n => n.table.split(', ')))];
    graphTables.forEach(t => { if (t && !attachedTables.includes(t)) attachedTables.push(t); });
    renderChips();
  }
}

loadProjects();
loadModels();
document.addEventListener('click', e => {
  if (!e.target.closest('#panel-context')) document.getElementById('panel-table-menu').classList.add('hidden');
});
(async () => { await restoreState(); })();
