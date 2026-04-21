# FRONTEND SPEC — reference for rebuild

## COLOR SCHEME
--bg: #0f1117; --surface: #181b24; --card: #222636; --border: #2e3348;
--accent: #3ecfb2; --warm: #f0883e; --text: #e8eaf0; --muted: #7b829a;
--danger: #e05555; --radius: 12px; --radius-sm: 8px; --radius-lg: 16px;

## LAYOUT (index.html)
3-column: sidebar(left) | chat(center) | right-panel(collapsible)
Resize handles between columns.

### Sidebar (#sidebar)
- Logo+hamburger INSIDE sidebar, collapses together
- Floating ⊞ button (#sidebar-open) appears when collapsed
- Content: "＋ Novo projeto" pill, #project-list (recent), #active-project (hidden until opened)
- Active project: #project-name, "＋ Importar planilha" button, #file-tree
- File tree: file-items (📄 name ✕) expand to show table-items
- Table items: draggable, dblclick rename, hover ✕ remove + ⤓ export
- Issue badge: .table-meta with ⚠ N valores a revisar
- Project items: .pi-name (dblclick rename) + .btn-remove (hover ✕)

### Chat center (#chat-center)
- #chat-messages: bubble messages (user=accent right, agent=card left with "Agent" label, system=centered italic)
- #chat-ref: clickable reference to selected node (hidden by default)
- #chat-input-area: #chips (attached tables), #input-row (+ button, input, Enviar button), #table-menu (autocomplete)
- Max-width 800px centered for messages

### Right panel (#right-panel, hidden by default)
- Tabs: 🗺 ANÁLISES | 📋 DADOS
- #view-map: SVG #mindmap, #node-detail (draggable card), #expand-progress (bar)
- #view-data: #data-issues, #data-table-wrap (table with editable cells), #data-pager
- #view-map.hidden { display: none } (prevents overlap)

### Topbar (#topbar)
- Just floating right-side: "⊞ Painel" toggle button

### Result modal (#result-modal)
- Fixed overlay, #result-modal-inner (90vw max 900px, flex column)
- Header with title + ✕ close
- Body with full table + download buttons (⤓ CSV, ⤓ XLSX)

## APP.JS FUNCTIONS

### API
api(endpoint, body, getPath) — POST with JSON or GET via getPath

### Sidebar
renderSidebar(files) — builds file tree with tables, issues badges, drag
loadProjects() — fetches /api/projects, renders project-list with dblclick rename + ✕ delete
newProject() — /api/pick_folder → prompt name → /api/new_project
openProject(path) — /api/open_project → renderSidebar + restore graph
importSheet() — /api/pick_file → /api/import_sheet
renameProject(path), deleteProject(path, name)
removeSource(file, table), renameTable(oldName, el)
toggleSidebar() — toggle .collapsed class, show/hide #sidebar-open

### Chat
addMsg(role, text) — role: user|agent|system, different styling each
sendChat() — sends message + attachedTables + selectedNode ref
onChatInput(e) — detects # trigger, calls showTableMenu(filter)

### Table attachment
attachedTables = []
attachTable(name), detachTable(name) — add/remove + renderChips + rebuild analysis if panel open
renderChips() — chips above input (📋 name ✕)
showTableMenu(filter) — autocomplete popup from # or + button
Drop on #chat-input-area attaches tables (file-item drag gives all tables, table-item gives one)

### Mind-map
NODE_W=260, NODE_PAD=10, LINE_H=16, MIN_H=40
nodeHeight(text) — dynamic height: Math.max(MIN_H, lines*LINE_H + NODE_PAD*2)
renderGraph() — SVG nodes with foreignObject for HTML word-wrap, edges, badges
clickNode(id) — auto-expand if !explored + show detail card with table preview + "Expandir tabela ↗" button
closeDetail(), showPanel(), hidePanel(), togglePanel()
Pan: mousedown/mousemove/mouseup on SVG for panX/panY

### Result modal
openResultModal(id) — shows full table from node result
closeResultModal()
downloadResult(id, fmt) — CSV client-side blob, XLSX via /api/download_result

### Expand
expandNode(id) — SSE /api/expand_stream, progress bar, adds nodes to graph

### Analysis
selectTable(name, el) — attaches table + calls buildAnalysis()
buildAnalysis() — POST /api/select_table with {names: attachedTables}

### Data review
loadDataView(tableName) — parallel fetch issues + data
renderIssues(issues, tableName):
  - DEDUP: .issue-card with .dedup-options (two .dedup-pick buttons showing value+count), "Outro nome…", "Não é duplicata"
  - OTHER: .issue-card with Aplicar/Editar/Ignorar
renderDataTable(data, tableName) — editable cells via contenteditable + onblur
resolveIssue, resolveDedup, resolveDedupCustom, resolveIssueCustom — all call updateIssueBadge + loadDataView
updateIssueBadge(table, remaining) — updates .table-meta in sidebar
editCell(table, row, col, el)

### Resize handles
mousedown on #resize-left/#resize-right, mousemove adjusts widths with min/max constraints

### Init
restoreState() — GET /api/state → if active, renderSidebar + restore graph
loadProjects()
restoreState()

## KEY CSS CLASSES
.msg-user, .msg-agent, .msg-system — bubble styles
.issue-card, .issue-type, .issue-hint, .issue-actions — issue cards
.dedup-options, .dedup-pick, .dedup-val, .dedup-count — dedup value buttons
.project-item, .pi-name — project list items
.table-item, .table-actions, .table-meta, .issue-badge — file tree
.chip — attached table chip
#table-menu — autocomplete dropdown
.resize-handle — vertical divider
.btn-remove, .btn-export, .btn-apply, .btn-dismiss, .btn-edit — action buttons
.btn-download — modal download buttons
.no-issues — green checkmark message
.bar, .bar-fill — progress bar
.tab, .tab.active — panel tabs
