"""TableAgent desktop app — Flask dev server, optional pywebview wrapper."""
import json, os, webbrowser, threading, queue
from pathlib import Path
from tkinter import Tk, filedialog

from flask import Flask, send_from_directory, request, jsonify, Response
import duckdb, pandas as pd

from tableagent.cleaner import clean_spreadsheet, CleanTable, _dedup_strings
from tableagent.catalog import load_catalog
from tableagent.column_mapper import map_columns
from tableagent.embedder import Embedder
from tableagent.metric_classifier import MetricClassifier
from tableagent.tree import KGraph, build_graph, expand_node, _execute_sql, _fill_template
from tableagent import slm

# ── Globals ───────────────────────────────────────────────
FRONTEND = Path(__file__).parent / "frontend"
RECENT_FILE = Path.home() / ".tableagent" / "recent.json"

conn = duckdb.connect()
db_lock = threading.Lock()
tables: list[CleanTable] = []
files_list: list[dict] = []
graph: KGraph | None = None
current_project: dict | None = None
embedder = Embedder()
catalog = load_catalog()
classifier = MetricClassifier(embedder)
classifier.fit(catalog.questions)


# ── Recent projects ───────────────────────────────────────
def _load_recent() -> list[dict]:
    if RECENT_FILE.exists():
        items = json.loads(RECENT_FILE.read_text("utf-8"))
        valid = [r for r in items if (Path(r["path"]) / "project.json").exists()]
        if len(valid) != len(items):
            _save_recent(valid)
        return valid
    return []

def _save_recent(projects: list[dict]):
    RECENT_FILE.parent.mkdir(parents=True, exist_ok=True)
    RECENT_FILE.write_text(json.dumps(projects, ensure_ascii=False, indent=2), "utf-8")

def _touch_recent(proj_path: str, name: str):
    recent = [p for p in _load_recent() if p["path"] != proj_path]
    recent.insert(0, {"path": proj_path, "name": name})
    _save_recent(recent[:20])


# ── Project persistence ───────────────────────────────────
def _save_project():
    if not current_project:
        return
    p = Path(current_project["path"])
    p.mkdir(parents=True, exist_ok=True)
    meta = {k: current_project[k] for k in ("name", "created", "sheets")}
    (p / "project.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), "utf-8")
    if graph:
        graph.save(str(p / "graph.json"))
    cache = slm.get_translate_cache()
    if cache:
        (p / "cache.json").write_text(json.dumps(cache, ensure_ascii=False), "utf-8")
    # Save cleaned dataframes + issues + dismissed pairs
    data_dir = p / "data"
    data_dir.mkdir(exist_ok=True)
    for t in tables:
        t.df.to_parquet(data_dir / f"{t.name}.parquet", index=False)
        (data_dir / f"{t.name}.issues.json").write_text(
            json.dumps(t.issues, ensure_ascii=False), "utf-8")
        dismissed = [list(p) for p in getattr(t, '_dismissed_pairs', set())]
        if dismissed:
            (data_dir / f"{t.name}.dismissed.json").write_text(
                json.dumps(dismissed, ensure_ascii=False), "utf-8")


def _load_project(proj_path: str):
    global tables, files_list, graph, current_project
    p = Path(proj_path)
    meta = json.loads((p / "project.json").read_text("utf-8"))
    current_project = {**meta, "path": proj_path}
    tables.clear(); files_list.clear(); graph = None
    data_dir = p / "data"
    for sheet_path in meta.get("sheets", []):
        sp = Path(sheet_path)
        if not sp.exists():
            continue
        new_tables = clean_spreadsheet(str(sp), conn)
        for t in new_tables:
            pq = data_dir / f"{t.name}.parquet"
            ij = data_dir / f"{t.name}.issues.json"
            if pq.exists():
                t.df = pd.read_parquet(pq)
                _refresh_duckdb(t)
                t.issues = json.loads(ij.read_text("utf-8")) if ij.exists() else []
                dj = data_dir / f"{t.name}.dismissed.json"
                if dj.exists():
                    t._dismissed_pairs = {tuple(p) for p in json.loads(dj.read_text("utf-8"))}
        tables.extend(new_tables)
        files_list.append({
            "name": sp.name,
            "tables": [{"name": t.name, "rows": len(t.df), "issues": len(t.issues)} for t in new_tables],
        })
    gf = p / "graph.json"
    if gf.exists() and tables:
        graph = KGraph.load(str(gf))
        # Re-execute queries for nodes with missing results
        for nid, node in graph.nodes.items():
            if node.result is None:
                matching = [t for t in tables if t.name == node.table]
                if matching:
                    mapping = map_columns(matching[0].df, catalog)
                    q = catalog.by_id.get(node.question_id)
                    if q:
                        sql = _fill_template(q.sql, node.table, mapping)
                        if sql:
                            node.result = _execute_sql(sql, conn)
    elif gf.exists():
        graph = KGraph.load(str(gf))
    cf = p / "cache.json"
    if cf.exists():
        slm.set_translate_cache(json.loads(cf.read_text("utf-8")))
    _touch_recent(proj_path, meta["name"])
    _load_learned()


def _refresh_duckdb(t):
    try: conn.execute(f'DROP VIEW IF EXISTS "{t.name}"')
    except: pass
    try: conn.execute(f'DROP TABLE IF EXISTS "{t.name}"')
    except: pass
    conn.register("__tmp", t.df)
    conn.execute(f'CREATE TABLE "{t.name}" AS SELECT * FROM __tmp')
    conn.unregister("__tmp")


def _tr(text):
    """Translate text for user display."""
    if not text:
        return text
    return slm.translate(text, "pt-BR")


# ── Flask app ─────────────────────────────────────────────
app = Flask(__name__, static_folder=str(FRONTEND))

@app.route("/")
def index():
    return send_from_directory(FRONTEND, "index.html")

@app.route("/<path:path>")
def static_files(path):
    return send_from_directory(FRONTEND, path)


# ── Project endpoints ─────────────────────────────────────
@app.get("/api/projects")
def api_projects():
    return jsonify(_load_recent())

@app.get("/api/state")
def api_state():
    if not current_project:
        return jsonify({"active": False})
    fresh_files = []
    for f in files_list:
        fresh_tables = []
        for ft in f["tables"]:
            t = next((t for t in tables if t.name == ft["name"]), None)
            fresh_tables.append({
                "name": ft["name"], "rows": len(t.df) if t else ft["rows"],
                "issues": len(t.issues) if t else 0,
            })
        fresh_files.append({"name": f["name"], "tables": fresh_tables})
    return jsonify({
        "active": True, "name": current_project["name"],
        "path": current_project["path"], "files": fresh_files,
        "graph": graph_json() if graph else None,
    })

@app.post("/api/new_project")
def api_new_project():
    global current_project, tables, files_list, graph
    proj_path = request.json.get("path", "")
    name = request.json.get("name", Path(proj_path).name)
    if not proj_path:
        return jsonify({"error": "Caminho não informado"}), 400
    p = Path(proj_path)
    p.mkdir(parents=True, exist_ok=True)
    tables.clear(); files_list.clear(); graph = None
    from datetime import datetime
    current_project = {"path": str(p), "name": name, "created": datetime.now().isoformat(), "sheets": []}
    _save_project()
    _touch_recent(str(p), name)
    return jsonify({"ok": True, "name": name, "files": files_list})

@app.post("/api/open_project")
def api_open_project():
    proj_path = request.json.get("path", "")
    p = Path(proj_path)
    if not (p / "project.json").exists():
        return jsonify({"error": "Projeto não encontrado"}), 404
    _load_project(proj_path)
    return jsonify({"ok": True, "name": current_project["name"], "files": files_list, "graph": graph_json()})

@app.post("/api/rename_project")
def api_rename_project():
    path = request.json.get("path", "")
    new_name = request.json.get("name", "").strip()
    if not new_name:
        return jsonify({"error": "Nome inválido"}), 400
    p = Path(path) / "project.json"
    if not p.exists():
        recent = [r for r in _load_recent() if r["path"] != path]
        _save_recent(recent)
        return jsonify({"error": "Projeto não encontrado (removido dos recentes)"}), 404
    meta = json.loads(p.read_text("utf-8"))
    meta["name"] = new_name
    p.write_text(json.dumps(meta, ensure_ascii=False, indent=2), "utf-8")
    if current_project and current_project["path"] == path:
        current_project["name"] = new_name
    _touch_recent(path, new_name)
    return jsonify({"ok": True})

@app.post("/api/delete_project")
def api_delete_project():
    """SAFE delete: only remove TableAgent files, never rmtree the whole folder."""
    global current_project, tables, files_list, graph
    path = request.json.get("path", "")
    p = Path(path)
    # Only delete files WE created, never the whole folder
    for fname in ("project.json", "graph.json", "cache.json"):
        (p / fname).unlink(missing_ok=True)
    data_dir = p / "data"
    if data_dir.exists():
        import shutil
        shutil.rmtree(data_dir, ignore_errors=True)
    # Remove from recents
    recent = [r for r in _load_recent() if r["path"] != path]
    _save_recent(recent)
    if current_project and current_project["path"] == path:
        current_project = None; tables.clear(); files_list.clear(); graph = None
    return jsonify({"ok": True})

@app.post("/api/import_sheet")
def api_import_sheet():
    global tables, files_list
    if not current_project:
        return jsonify({"error": "Nenhum projeto aberto"}), 400
    path = request.json.get("path", "")
    if not path or not Path(path).exists():
        return jsonify({"error": "Arquivo não encontrado"}), 400
    abs_path = str(Path(path).resolve())
    if abs_path not in current_project["sheets"]:
        current_project["sheets"].append(abs_path)
    new_tables = clean_spreadsheet(abs_path, conn)
    tables.extend(new_tables)
    files_list.append({
        "name": Path(path).name,
        "tables": [{"name": t.name, "rows": len(t.df), "issues": len(t.issues)} for t in new_tables],
    })
    _save_project()
    return jsonify(files_list)

@app.post("/api/remove_source")
def api_remove_source():
    global tables, files_list
    file_name = request.json.get("file", "")
    table_name = request.json.get("table")
    if table_name:
        tables[:] = [t for t in tables if t.name != table_name]
        try: conn.execute(f'DROP TABLE IF EXISTS "{table_name}"')
        except: pass
        for f in files_list:
            f["tables"] = [t for t in f["tables"] if t["name"] != table_name]
    else:
        for f in files_list:
            if f["name"] == file_name:
                for t in f["tables"]:
                    tables[:] = [tt for tt in tables if tt.name != t["name"]]
                    try: conn.execute(f'DROP TABLE IF EXISTS "{t["name"]}"')
                    except: pass
        files_list[:] = [f for f in files_list if f["name"] != file_name]
    _save_project()
    return jsonify(files_list)

@app.post("/api/rename_table")
def api_rename_table():
    old = request.json.get("old", "")
    new = request.json.get("new", "").strip()
    if not new:
        return jsonify({"error": "Nome inválido"}), 400
    t = next((t for t in tables if t.name == old), None)
    if not t:
        return jsonify({"error": "Tabela não encontrada"}), 404
    t.name = new
    _refresh_duckdb(t)
    for f in files_list:
        for ft in f["tables"]:
            if ft["name"] == old:
                ft["name"] = new
    _save_project()
    return jsonify({"ok": True})


# ── Legacy open (no project) ─────────────────────────────
@app.post("/api/open")
def api_open():
    global tables, files_list
    path = request.json.get("path", "")
    if not path or not Path(path).exists():
        return jsonify({"error": "Arquivo não encontrado"}), 400
    tables.clear(); files_list.clear()
    new_tables = clean_spreadsheet(path, conn)
    tables.extend(new_tables)
    files_list.append({
        "name": Path(path).name,
        "tables": [{"name": t.name, "rows": len(t.df), "issues": len(t.issues)} for t in new_tables],
    })
    return jsonify(files_list)


# ── Analysis endpoints ────────────────────────────────────
@app.post("/api/select_table")
def api_select_table():
    global graph
    names = request.json.get("names") or []
    name = request.json.get("name", "")
    if name and not names:
        names = [name]
    matching = [t for t in tables if t.name in names]
    if not matching:
        return jsonify({"error": "Tabela não encontrada"}), 404
    with db_lock:
        graph = build_graph(matching, conn, initial_nodes=8)
    _save_project()
    return jsonify(graph_json())

@app.post("/api/expand")
def api_expand():
    global graph
    nid = request.json.get("id", "")
    if not graph or nid not in graph.nodes:
        return jsonify({"error": "Nó não encontrado"}), 404
    new_nodes = expand_node(graph, nid, tables, conn)
    _save_project()
    return jsonify(graph_json())

@app.get("/api/expand_stream")
def api_expand_stream():
    global graph
    nid = request.args.get("id", "")
    if not graph or nid not in graph.nodes:
        return Response("data: {\"type\":\"error\"}\n\n", mimetype="text/event-stream")

    q = queue.Queue()

    def _on_node(node, progress, total):
        from dataclasses import asdict
        q.put(json.dumps({"type": "node", "id": node.id, "node": asdict(node),
                           "progress": progress, "total": total}))

    def _run():
        with db_lock:
            expand_node(graph, nid, tables, conn, on_node=_on_node)
        q.put(json.dumps({"type": "done"}))
        _save_project()

    threading.Thread(target=_run, daemon=True).start()

    def generate():
        while True:
            msg = q.get()
            yield f"data: {msg}\n\n"
            if '"type": "done"' in msg or '"type":"done"' in msg:
                break

    return Response(generate(), mimetype="text/event-stream")


# ── Data review endpoints ─────────────────────────────────
@app.get("/api/files")
def api_files():
    return jsonify(files_list)

@app.get("/api/issues")
def api_issues():
    name = request.args.get("table", "")
    t = next((t for t in tables if t.name == name), None)
    if not t:
        return jsonify({"error": "Tabela não encontrada"}), 404
    return jsonify([{**iss, "id": i} for i, iss in enumerate(t.issues)])

@app.get("/api/data")
def api_data():
    name = request.args.get("table", "")
    page = int(request.args.get("page", 0))
    size = int(request.args.get("size", 50))
    t = next((t for t in tables if t.name == name), None)
    if not t:
        return jsonify({"error": "Tabela não encontrada"}), 404
    start = page * size
    chunk = t.df.iloc[start:start + size].copy()
    # Convert datetime cols to string to avoid NaT JSON serialization error
    for col in chunk.select_dtypes(include=["datetime", "datetimetz"]).columns:
        chunk[col] = chunk[col].dt.strftime("%Y-%m-%d").where(chunk[col].notna(), None)
    return jsonify({
        "columns": list(t.df.columns),
        "data": chunk.astype(object).where(chunk.notna(), None).values.tolist(),
        "total": len(t.df), "page": page, "size": size,
    })

@app.post("/api/resolve_issue")
def api_resolve_issue():
    data = request.json
    name = data.get("table", "")
    issue_id = data.get("issue_id")
    action = data.get("action", "dismiss")
    custom = data.get("custom")
    chosen = data.get("chosen")

    t = next((t for t in tables if t.name == name), None)
    if not t or issue_id is None or issue_id >= len(t.issues):
        return jsonify({"error": "Issue não encontrada"}), 404

    iss = t.issues[issue_id]
    if action == "apply":
        col = iss["column"]
        if iss["type"] == "dedup":
            a, b = iss["value"], iss.get("suggestion", iss["value"])
            if custom:
                t.df[col] = t.df[col].replace({a: custom, b: custom})
            elif chosen:
                other = b if chosen == a else a
                t.df[col] = t.df[col].replace(other, chosen)
            else:
                t.df[col] = t.df[col].replace(a, b)
        else:
            old_val = iss["value"]
            new_val = custom or iss.get("suggestion", old_val)
            t.df[col] = t.df[col].replace(old_val, new_val)
        _refresh_duckdb(t)
    elif action == "dismiss" and iss["type"] == "dedup":
        if not hasattr(t, '_dismissed_pairs'):
            t._dismissed_pairs = set()
        a, b = iss["value"], iss.get("suggestion", iss["value"])
        t._dismissed_pairs.add((min(a, b), max(a, b)))

    t.issues.pop(issue_id)
    # Re-run dedup (transitive: A~B + B~C → checks A~C)
    t.issues = [i for i in t.issues if i["type"] != "dedup"]
    _dedup_strings(t.df, t.issues)
    dismissed = getattr(t, '_dismissed_pairs', set())
    if dismissed:
        t.issues = [i for i in t.issues if i["type"] != "dedup" or
                    (min(i["value"], i.get("suggestion", "")), max(i["value"], i.get("suggestion", ""))) not in dismissed]
    _save_project()
    return jsonify({"ok": True, "remaining": len(t.issues)})

@app.post("/api/edit_cell")
def api_edit_cell():
    data = request.json
    name = data.get("table", "")
    row = data.get("row")
    col = data.get("col", "")
    value = data.get("value")
    t = next((t for t in tables if t.name == name), None)
    if not t or col not in t.df.columns or row is None or row >= len(t.df):
        return jsonify({"error": "Célula inválida"}), 400
    t.df.at[row, col] = value
    _refresh_duckdb(t)
    _save_project()
    return jsonify({"ok": True})


# ── Entity-aware filtering ────────────────────────────────
def _match_value(entity: str, search_tables: list) -> tuple[str, str] | None:
    """Match an entity string against cell values. Returns (col, value) or None."""
    import re
    _STOP = {"de","e","o","a","os","as","do","da","dos","das","em","para","com","que","um","uma","no","na","por","se","ao","ou"}
    entity_lower = entity.lower().strip()
    if not entity_lower or entity_lower == "none":
        return None
    best, best_score, best_col = None, 0, None
    e_words = set(re.findall(r'\w+', entity_lower)) - _STOP
    for t in search_tables:
        for col in t.df.select_dtypes(include=["object"]).columns:
            for val in t.df[col].dropna().unique():
                val_str = str(val).strip()
                if len(val_str) < 3:
                    continue
                val_lower = val_str.lower()
                # Exact substring
                if entity_lower in val_lower:
                    score = len(entity_lower) / len(val_lower)
                    if score > best_score:
                        best, best_score, best_col = val_str, score, col
                elif val_lower in entity_lower:
                    score = len(val_lower) / len(entity_lower)
                    if score > best_score:
                        best, best_score, best_col = val_str, score, col
                else:
                    # Word overlap without stop words
                    v_words = set(re.findall(r'\w+', val_lower)) - _STOP
                    overlap = e_words & v_words
                    if overlap and len(overlap) >= 1:
                        # Weight by length of matching words
                        weight = sum(len(w) for w in overlap)
                        total = sum(len(w) for w in v_words) or 1
                        score = weight / total
                        if score > best_score:
                            best, best_score, best_col = val_str, score, col
    if best and best_col and best_score > 0.2:
        return best_col, best
    return None


def _detect_entity_filter(msg: str, search_tables: list) -> str | None:
    """Detect entity in user message. Returns LIKE or NOT LIKE filter."""
    # Detect exclusion intent via word-level embeddings
    is_exclusion = _is_exclusion(msg)

    if is_exclusion:
        # Match n-grams (1..4 words) against cell values
        words = [w.strip("?.,!()[]") for w in msg.lower().split() if len(w.strip("?.,!()[]")) >= 2]
        best_match = None
        best_len = 0
        for n in range(min(4, len(words)), 0, -1):
            for i in range(len(words) - n + 1):
                gram = " ".join(words[i:i+n])
                m = _match_value(gram, search_tables)
                if m:
                    col, val = m
                    if any(s in val.lower() for s in ("total", "subtotal")):
                        continue
                    if len(gram) > best_len:
                        best_len = len(gram)
                        best_match = m
            if best_match:
                break
        if best_match:
            col, val = best_match
            safe = val.replace("'", "''")
            return f'"{col}" NOT LIKE \'%{safe}%\''

    # Regular entity detection: SLM first, fallback to direct matching
    entity = slm.extract_entity(msg)
    if entity:
        match = _match_value(entity, search_tables)
        if match:
            col, val = match
            safe = val.replace("'", "''")
            return f'"{col}" LIKE \'%{safe}%\''
    match = _match_value(msg, search_tables)
    if match:
        col, val = match
        safe = val.replace("'", "''")
        return f'"{col}" LIKE \'%{safe}%\''
    return None

_exclusion_embs = None
_inclusion_embs = None
_exclusion_concepts = [
    "total sem contar um gasto", "excluindo um item do total",
    "se não pagarmos um item", "ignorando o custo de algo",
    "desconsiderando um item", "tirando do cálculo",
    "total without one expense", "not including one item",
    "e se não comprarmos algo", "descontando um item",
    "total menos o custo de um item", "subtraindo um gasto do total",
]
_inclusion_concepts = [
    "quanto custa um item", "qual o valor de algo",
    "quanto gastamos com algo", "total de todos os itens",
    "qual o preço de um produto", "how much does it cost",
    "soma de todos os valores", "quanto foi gasto",
]

def _init_exclusion():
    global _exclusion_embs, _inclusion_embs
    if _exclusion_embs is not None:
        return
    import numpy as np
    def _norm_embs(texts):
        e = embedder.encode(texts)
        n = np.linalg.norm(e, axis=1, keepdims=True); n[n==0]=1
        return e / n
    _exclusion_embs = _norm_embs(_exclusion_concepts)
    _inclusion_embs = _norm_embs(_inclusion_concepts)

def _is_exclusion(msg):
    """Contrastive: exclusion score must beat inclusion score."""
    import numpy as np
    _init_exclusion()
    q = embedder.encode([msg])[0]
    n = np.linalg.norm(q)
    if n > 0: q = q / n
    excl_score = float(np.max(_exclusion_embs @ q))
    incl_score = float(np.max(_inclusion_embs @ q))
    return excl_score > incl_score and excl_score > 0.35


# ── Compound cross-table queries ──────────────────────────
COMPOUND_DESCS = {
    "subtract": [
        "how much money is left after expenses", "quanto sobra", "quanto sobraria",
        "diferença entre receita e custo", "quanto falta para cobrir os custos",
        "restante após gastar", "how much would remain", "sobra de dinheiro",
        "quanto falta de dinheiro", "quanto ainda falta pagar",
    ],
    "ratio": [
        "what percentage of total", "quanto porcento", "proporção de",
        "qual a proporção", "percentual do total",
    ],
}
_compound_embs = None
_compound_map = []
_compound_descs_list = []

def _init_compound():
    global _compound_embs, _compound_map, _compound_descs_list
    if _compound_embs is not None:
        return
    for op_id, descs in COMPOUND_DESCS.items():
        for d in descs:
            _compound_descs_list.append(d)
            _compound_map.append(op_id)
    import numpy as np
    _compound_embs = embedder.encode(_compound_descs_list)
    norms = np.linalg.norm(_compound_embs, axis=1, keepdims=True)
    norms[norms == 0] = 1
    _compound_embs = _compound_embs / norms

def _detect_compound(msg):
    """Return (op_id, score) if compound, else None."""
    import numpy as np
    _init_compound()
    q = embedder.encode([msg])[0]
    n = np.linalg.norm(q)
    if n > 0: q = q / n
    scores = _compound_embs @ q
    idx = int(np.argmax(scores))
    if scores[idx] > 0.55:
        return _compound_map[idx], float(scores[idx])
    return None

def _try_compound(msg, search_tables):
    """Try compound cross-table query. Returns response dict or None."""
    det = _detect_compound(msg)
    if not det:
        return None
    op, score = det

    # Gemma confirms only if score is very borderline
    if slm._llm is not None and score < 0.45:
        if not slm.ask_yes_no(f"Does '{msg}' ask for a difference or remainder?"):
            return None

    # Match entity: fuzzy match message words to column names (accent-insensitive)
    import unicodedata
    def _norm(s):
        return ''.join(c for c in unicodedata.normalize('NFD', s.lower()) if unicodedata.category(c) != 'Mn')
    msg_words = [w.strip("?.,!") for w in msg.lower().split()]
    # Skip generic words that match column names but aren't entities
    entity_stop = {"total", "valor", "gasto", "custo", "item", "quantidade", "data",
                   "nome", "link", "produto", "preco", "qtd", "col", "para", "reforma",
                   "dinheiro", "materiais", "lista", "todos", "todas"}
    entity_col, entity_table = None, None
    best_ratio = 0
    for t in search_tables:
        for col in t.df.columns:
            col_n = _norm(col)
            for w in msg_words:
                if len(w) < 3 or w in entity_stop:
                    continue
                w_n = _norm(w)
                if w_n in col_n or col_n in w_n:
                    ratio = len(w_n) / max(len(col_n), 1)
                    if ratio > best_ratio:
                        best_ratio = ratio
                        entity_col = col
                        entity_table = t
    if not entity_col:
        # Fallback: use amount column from the smallest table (likely a summary)
        # But only if the question clearly implies cross-table comparison
        cross_keywords = {"sobr", "falt", "rest", "diferenç", "percentual", "proporç", "porcentag"}
        import unicodedata
        msg_n = ''.join(c for c in unicodedata.normalize('NFD', msg.lower()) if unicodedata.category(c) != 'Mn')
        if not any(kw in msg_n for kw in cross_keywords):
            return None
        smallest = min(search_tables, key=lambda t: len(t.df))
        sm_map = map_columns(smallest.df, catalog)
        if sm_map.get("col_amount"):
            entity_col = sm_map["col_amount"]
            entity_table = smallest
    if not entity_col:
        return None

    # Other table provides aggregation
    other = [t for t in search_tables if t != entity_table]
    if not other:
        return None
    agg_table = other[0]
    agg_map = map_columns(agg_table.df, catalog)
    agg_col = agg_map.get("col_amount")
    text_col = agg_map.get("col_product") or agg_map.get("col_label")
    if not agg_col:
        return None

    # Find row filter for entity table (match message words to cell values)
    row_filter = ""
    label_col = None
    for col in entity_table.df.columns:
        if entity_table.df[col].dtype == object and col != entity_col:
            label_col = col
            break
    if label_col:
        stopwords = {"o", "a", "de", "do", "da", "com", "se", "que", "e", "os", "as",
                     "um", "uma", "para", "por", "em", "no", "na", "ao", "à", "é",
                     "ser", "ter", "fazer", "fizermos", "apenas", "quanto", "sobraria"}
        for val in entity_table.df[label_col].dropna().astype(str):
            val_words = set(val.lower().split())
            msg_set = set(msg_words) - stopwords
            if val_words & msg_set:
                safe = val.replace("'", "''")
                row_filter = f' WHERE "{label_col}" = \'{safe}\''
                break

    # Build SQL
    exclude = ""
    if text_col:
        exclude = f" WHERE \"{text_col}\" NOT LIKE '%Total%' AND \"{text_col}\" NOT LIKE '%Subtotal%'"

    if op == "subtract":
        sql = (f'SELECT (SELECT CAST("{entity_col}" AS DOUBLE) FROM "{entity_table.name}"{row_filter} LIMIT 1)'
               f' - (SELECT SUM("{agg_col}") FROM "{agg_table.name}"{exclude}) AS resultado')
    elif op == "ratio":
        sql = (f'SELECT ROUND((SELECT SUM("{agg_col}") FROM "{agg_table.name}"{exclude})'
               f' * 100.0 / (SELECT CAST("{entity_col}" AS DOUBLE) FROM "{entity_table.name}"{row_filter} LIMIT 1), 2) AS percentual')
    else:
        return None

    with db_lock:
        result = _execute_sql(sql, conn)
    if not result or not result.get("data"):
        return None

    narrative = slm.narrate(msg, result["columns"], result["data"])
    # Reject repetitive/garbage narration
    if narrative and (narrative.count('\n') > 3 or len(set(narrative.split('\n'))) < len(narrative.split('\n')) / 2):
        narrative = None
    if not narrative:
        val = result['data'][0][0]
        if isinstance(val, float):
            val = f"R$ {val:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
        narrative = f"📊 Resultado: {val}"

    return {"response": narrative, "result": result, "sql": [sql], "compound": True, "operation": op}


# ── Settings ──────────────────────────────────────────────
SETTINGS_FILE = Path.home() / ".tableagent" / "settings.json"
chat_mode = "heuristic"  # current active chat mode

def _load_settings() -> dict:
    if SETTINGS_FILE.exists():
        return json.loads(SETTINGS_FILE.read_text("utf-8"))
    return {}

def _save_settings(data: dict):
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")

@app.get("/api/settings")
def api_get_settings():
    s = _load_settings()
    # Mask key for display
    key = s.get("google_api_key", "")
    return jsonify({"google_api_key": ("*" * 8 + key[-4:]) if len(key) > 4 else ("*" * len(key) if key else ""),
                    "google_model": s.get("google_model", "gemma-3-4b-it"),
                    "has_google_key": bool(key)})

@app.post("/api/settings")
def api_save_settings():
    body = request.json or {}
    s = _load_settings()
    if "google_api_key" in body and not body["google_api_key"].startswith("*"):
        s["google_api_key"] = body["google_api_key"]
    if "google_model" in body:
        s["google_model"] = body["google_model"]
    _save_settings(s)
    return jsonify({"status": "ok"})

def _google_chat(msg: str, table_context: str) -> str:
    import urllib.request
    s = _load_settings()
    api_key = s.get("google_api_key", "")
    model = s.get("google_model", "gemma-3-4b-it")
    if not api_key:
        return "Chave da API Google não configurada."
    prompt = f"Você é um assistente de análise de dados. Responda em português.\n\nContexto das tabelas disponíveis:\n{table_context}\n\nPergunta: {msg}"
    payload = json.dumps({"contents": [{"parts": [{"text": prompt}]}]}).encode()
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
        return data["candidates"][0]["content"]["parts"][0]["text"]
    except Exception as e:
        return f"Erro ao chamar Google API: {e}"

# ── Model selector ────────────────────────────────────────
MODELS_DIR = Path(__file__).parent / "models"

@app.get("/api/models")
def api_models():
    s = _load_settings()
    loaded_mode = chat_mode
    available = [{"id": "heuristic", "name": "Heurística", "loaded": loaded_mode == "heuristic"}]
    if MODELS_DIR.exists():
        for f in sorted(MODELS_DIR.glob("*.gguf")):
            available.append({"id": f.stem, "name": f.stem, "path": str(f),
                              "loaded": loaded_mode == f.stem})
    if s.get("google_api_key"):
        available.append({"id": "google", "name": "🌐 Google AI", "loaded": loaded_mode == "google"})
    return jsonify(available)

@app.post("/api/set_model")
def api_set_model():
    global chat_mode
    model_id = request.json.get("id", "heuristic")
    if model_id == "heuristic":
        slm.unload_model()
        chat_mode = "heuristic"
        return jsonify({"status": "ok", "model": "heuristic"})
    if model_id == "google":
        s = _load_settings()
        if not s.get("google_api_key"):
            return jsonify({"error": "Chave da API Google não configurada."}), 400
        slm.unload_model()
        chat_mode = "google"
        return jsonify({"status": "ok", "model": "google"})
    if MODELS_DIR.exists():
        path = MODELS_DIR / f"{model_id}.gguf"
        if path.exists():
            slm.unload_model()
            slm.load_model(str(path))
            slm._model_path = str(path)
            if slm._llm:
                chat_mode = model_id
                return jsonify({"status": "ok", "model": model_id})
            return jsonify({"error": "Falha ao carregar modelo"}), 500
    return jsonify({"error": "Modelo não encontrado"}), 404


# ── Chat endpoint ─────────────────────────────────────────
@app.post("/api/chat")
def api_chat():
    msg = request.json.get("message", "")
    table_names = request.json.get("table_names", [])
    ref_node_id = request.json.get("ref_node")

    # Google API mode — bypass heuristic/SLM pipeline
    if chat_mode == "google":
        ctx_parts = []
        for t in tables:
            if table_names and t.name not in table_names:
                continue
            cols = ", ".join(f"{c} ({t.df[c].dtype})" for c in t.df.columns)
            sample = t.df.head(3).to_string(index=False)
            ctx_parts.append(f"Tabela '{t.name}': colunas [{cols}]\nAmostra:\n{sample}")
        answer = _google_chat(msg, "\n\n".join(ctx_parts) if ctx_parts else "Nenhuma tabela carregada.")
        return jsonify({"response": answer})

    ref_node = None
    if ref_node_id and graph and ref_node_id in graph.nodes:
        ref_node = graph.nodes[ref_node_id]

    if table_names:
        scoped = [t for t in tables if t.name in table_names]
    elif ref_node:
        scoped = [t for t in tables if t.name == ref_node.table]
    else:
        scoped = []
    search_tables = scoped + [t for t in tables if t not in scoped]

    if not search_tables:
        return jsonify({"response": "Nenhuma tabela disponível. Importe uma planilha primeiro."})

    # Find matching questions
    matches = classifier.predict(msg, top_k=5)
    if not matches:
        return jsonify({"response": "Não encontrei uma análise correspondente. Tente reformular."})

    # Try compound cross-table query first
    compound = _try_compound(msg, search_tables)
    if compound:
        return jsonify(compound)

    # Detect entity reference in user's question (only against scoped tables)
    entity_filter = _detect_entity_filter(msg, scoped or search_tables)

    # SLM beam search: refine candidates with yes/no
    beam_result = None
    if slm._llm is not None and search_tables:
        # Build candidates: (Question, filled_sql, score) for first matching table
        beam_candidates = []
        t0 = search_tables[0]
        m0 = map_columns(t0.df, catalog)
        text_col = m0.get("col_product") or m0.get("col_label") or m0.get("col_name")
        amount_col = m0.get("col_amount")
        for q, score in matches:
            if all(s in m0 for s in q.required_slots):
                sql = _fill_template(q.sql, t0.name, m0)
                if sql:
                    beam_candidates.append((q, sql, score))
        if beam_candidates:
            beam_result = slm.beam_refine(msg, beam_candidates, t0.name,
                                          list(t0.df.columns), text_col, amount_col)

    # Reorder matches: beam's choice first
    if beam_result:
        bid = beam_result["question_id"]
        matches = sorted(matches, key=lambda m: (0 if m[0].question_id == bid else 1, -m[1]))

    # Try tables — if multiple attached, run on all and combine
    for q, score in matches:
        all_dfs = []
        used_sqls = []
        for t in search_tables:
            mapping = map_columns(t.df, catalog)
            if not all(s in mapping for s in q.required_slots):
                continue
            # Use beam-refined SQL if available and matches this question
            if beam_result and beam_result["question_id"] == q.question_id:
                sql = beam_result["sql"]
                # Re-fill for this specific table if multi-table
                if t != search_tables[0]:
                    sql = _fill_template(q.sql, t.name, mapping)
                    if not sql:
                        continue
            else:
                sql = _fill_template(q.sql, t.name, mapping)
                if not sql:
                    continue
            # Inject entity filter as WHERE clause (only if column exists in this table)
            if entity_filter:
                import re
                filter_col = entity_filter.split('"')[1] if '"' in entity_filter else None
                if filter_col and filter_col in t.df.columns:
                    if ' WHERE ' in sql.upper():
                        sql = re.sub(r'(?i)\bWHERE\b', f'WHERE {entity_filter} AND', sql, count=1)
                    elif ' GROUP BY ' in sql.upper():
                        sql = re.sub(r'(?i)\bGROUP BY\b', f'WHERE {entity_filter} GROUP BY', sql, count=1)
                    elif ' ORDER BY ' in sql.upper():
                        sql = re.sub(r'(?i)\bORDER BY\b', f'WHERE {entity_filter} ORDER BY', sql, count=1)
                    else:
                        sql += f' WHERE {entity_filter}'
                elif filter_col:
                    continue  # entity mentioned but table lacks the column — skip
            result = _execute_sql(sql, conn)
            if result and result.get("data"):
                used_sqls.append(sql)
                df = pd.DataFrame(result["data"], columns=result["columns"])
                if table_names and len(table_names) > 1:
                    df.insert(0, "source", t.name)
                all_dfs.append(df)
                if not table_names or len(table_names) <= 1:
                    break
        if all_dfs:
            combined = pd.concat(all_dfs, ignore_index=True)
            combined = combined.astype(object).where(combined.notna(), None)
            result_data = {"columns": list(combined.columns), "data": combined.values.tolist()}
            # SLM narration
            narrative = slm.narrate(msg, result_data["columns"], result_data["data"])
            prefix = f"📌 Ref: {_tr(ref_node.question)}\n" if ref_node else ""
            response = f"{prefix}{narrative}" if narrative else f"{prefix}📊 {_tr(q.text)}"
            # Include beam info for feedback
            meta = {}
            if beam_result and beam_result["question_id"] == q.question_id:
                meta = {"beam": True, "question_id": q.question_id,
                        "question_text": q.text,
                        "required_slots": q.required_slots,
                        "refinements": beam_result.get("refinements", [])}
            return jsonify({"response": response, "result": result_data, "sql": used_sqls, **meta})

    q, score = matches[0]
    return jsonify({"response": f"Encontrei: \"{q.text}\" (score: {score:.2f}), mas faltam colunas."})


# ── Learned queries (feedback loop) ──────────────────────
learned_queries: list[dict] = []

def _learned_path() -> Path | None:
    if current_project:
        return Path(current_project["path"]) / "learned.json"
    return None

def _save_learned():
    p = _learned_path()
    if p:
        p.write_text(json.dumps(learned_queries, ensure_ascii=False, indent=2), "utf-8")

def _load_learned():
    global learned_queries
    p = _learned_path()
    if p and p.exists():
        learned_queries = json.loads(p.read_text("utf-8"))
        # Add learned queries to classifier
        for lq in learned_queries:
            from tableagent.catalog import Question
            q = Question(lq["question_id"], lq["text"], "learned",
                         lq["sql"], lq.get("required_slots", []), "learned",
                         aliases=lq.get("aliases", []))
            if not any(eq.question_id == q.question_id for eq in catalog.questions):
                catalog.questions.append(q)
        if learned_queries:
            classifier.fit(catalog.questions)

@app.post("/api/feedback")
def api_feedback():
    data = request.json
    rating = data.get("rating")  # "good" or "bad"
    if rating != "good":
        return jsonify({"ok": True})
    # Save as learned query
    qid = f"learned_{len(learned_queries)+1:03d}"
    entry = {
        "question_id": qid,
        "text": data.get("question_text", ""),
        "sql": data.get("sql", ""),
        "required_slots": data.get("required_slots", []),
        "aliases": [data.get("user_question", "")],
    }
    learned_queries.append(entry)
    _save_learned()
    # Add to live classifier
    from tableagent.catalog import Question
    q = Question(qid, entry["text"], "learned", entry["sql"],
                 entry["required_slots"], "learned", aliases=entry["aliases"])
    catalog.questions.append(q)
    classifier.fit(catalog.questions)
    return jsonify({"ok": True, "learned": qid})


# ── Table names for frontend ─────────────────────────────
@app.get("/api/table_names")
def api_table_names():
    return jsonify([t.name for t in tables])


# ── Download result as XLSX ───────────────────────────────
@app.post("/api/download_result")
def api_download_result():
    if request.is_json:
        data = request.json
    else:
        data = json.loads(request.form.get("data", "{}"))
    df = pd.DataFrame(data["data"], columns=data["columns"])
    import io
    buf = io.BytesIO()
    df.to_excel(buf, index=False, engine="openpyxl")
    buf.seek(0)
    return Response(buf.read(), mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": "attachment; filename=resultado.xlsx"})


# ── Native file/folder picker ─────────────────────────────
def _pick_dialog(mode="file"):
    result = [None]
    def _run():
        root = Tk(); root.withdraw(); root.attributes("-topmost", True)
        if mode == "folder":
            result[0] = filedialog.askdirectory(title="Escolha a pasta do projeto")
        else:
            result[0] = filedialog.askopenfilename(
                title="Escolha a planilha",
                filetypes=[("Planilhas", "*.xlsx *.xls *.csv *.ods"), ("Todos", "*.*")])
        root.destroy()
    t = threading.Thread(target=_run); t.start(); t.join()
    return result[0] or ""

@app.get("/api/pick_folder")
def api_pick_folder():
    return jsonify({"path": _pick_dialog("folder")})

@app.get("/api/pick_file")
def api_pick_file():
    return jsonify({"path": _pick_dialog("file")})


# ── Graph JSON helper ─────────────────────────────────────
def graph_json() -> dict:
    if not graph:
        return {"nodes": {}, "roots": []}
    from dataclasses import asdict
    nodes = {}
    for nid, n in graph.nodes.items():
        nodes[nid] = {
            "question": n.question or n.question_id,
            "question_id": n.question_id,
            "topic": n.topic, "table": n.table,
            "explored": n.explored, "children": n.children,
            "summary": n.summary, "result": n.result,
        }
    return {"nodes": nodes, "roots": graph.roots}


def main():
    # Auto-restore last project
    recent = _load_recent()
    if recent:
        try: _load_project(recent[0]["path"])
        except Exception as e: print(f"Auto-restore failed: {e}")
    # Auto-load first available GGUF model
    if MODELS_DIR.exists():
        gguf = next(MODELS_DIR.glob("*.gguf"), None)
        if gguf:
            print(f"Carregando modelo {gguf.name}…")
            slm.load_model(str(gguf))
    port = 5000
    url = f"http://localhost:{port}"
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    print(f"TableAgent rodando em {url}")
    app.run(port=port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
