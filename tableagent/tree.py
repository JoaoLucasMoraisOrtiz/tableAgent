"""KGraph: knowledge graph with build/expand, on_node callback, save/load JSON."""
from __future__ import annotations
import json, math
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Callable

import duckdb, pandas as pd

from .catalog import load_catalog, Catalog, Question
from .column_mapper import map_columns
from .embedder import Embedder
from .metric_classifier import MetricClassifier


@dataclass
class KNode:
    id: str
    question_id: str
    question: str
    topic: str
    table: str
    explored: bool = False
    children: list[str] = field(default_factory=list)
    summary: str | None = None
    result: dict | None = None  # {columns, data}


class KGraph:
    def __init__(self):
        self.nodes: dict[str, KNode] = {}
        self.roots: list[str] = []
        self._next_id = 0

    def _gen_id(self) -> str:
        self._next_id += 1
        return f"n{self._next_id}"

    def add_node(self, question_id: str, question: str, topic: str, table: str,
                 parent_id: str | None = None) -> KNode:
        nid = self._gen_id()
        node = KNode(id=nid, question_id=question_id, question=question,
                     topic=topic, table=table)
        self.nodes[nid] = node
        if parent_id and parent_id in self.nodes:
            self.nodes[parent_id].children.append(nid)
        else:
            self.roots.append(nid)
        return node

    def save(self, path: str):
        data = {
            "nodes": {nid: asdict(n) for nid, n in self.nodes.items()},
            "roots": self.roots,
            "_next_id": self._next_id,
        }
        Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")

    @classmethod
    def load(cls, path: str) -> KGraph:
        data = json.loads(Path(path).read_text("utf-8"))
        g = cls()
        g.roots = data["roots"]
        g._next_id = data.get("_next_id", 0)
        for nid, nd in data["nodes"].items():
            g.nodes[nid] = KNode(**nd)
        return g


def _execute_sql(sql: str, conn: duckdb.DuckDBPyConnection) -> dict | None:
    try:
        result = conn.execute(sql).fetchdf()
        for col in result.select_dtypes(include=["datetime", "datetimetz"]).columns:
            result[col] = result[col].dt.strftime("%Y-%m-%d").where(result[col].notna(), None)
        result = result.where(result.notna(), None)
        return {
            "columns": list(result.columns),
            "data": result.values.tolist(),
        }
    except Exception as e:
        print(f"[SQL ERROR] {e}\n  SQL: {sql[:200]}")
        return None


def _fill_template(sql_template: str, table_name: str, mapping: dict[str, str]) -> str | None:
    sql = sql_template.replace("{table}", f'"{table_name}"')
    for slot, col in mapping.items():
        sql = sql.replace(f"{{{slot}}}", f'"{col}"')
    # Check no unfilled placeholders remain
    if "{" in sql:
        return None
    return sql


def _strip_trailing_order(sql: str) -> str:
    """Remove only top-level trailing ORDER BY (not inside parens like WITHIN GROUP)."""
    depth, last_top_order = 0, -1
    upper = sql.upper()
    for i in range(len(sql)):
        if sql[i] == '(': depth += 1
        elif sql[i] == ')': depth -= 1
        elif depth == 0 and upper[i:i+8] == 'ORDER BY':
            last_top_order = i
    return sql[:last_top_order].rstrip() if last_top_order > 0 else sql


def _multi_table_sql(sql_template: str, tables_mappings: list[tuple]) -> str | None:
    """Build UNION ALL query across multiple tables. Returns None if any table fails."""
    import re
    # Queries with JOINs/subqueries referencing {table} multiple times can't be safely UNION'd
    if len(tables_mappings) > 1 and sql_template.count('{table}') > 1:
        return _fill_template(sql_template, tables_mappings[0][0].name, tables_mappings[0][1])
    parts = []
    for t, mapping in tables_mappings:
        sql = _fill_template(sql_template, t.name, mapping)
        if not sql:
            return None
        parts.append(_strip_trailing_order(sql))
    if len(parts) == 1:
        return _fill_template(sql_template, tables_mappings[0][0].name, tables_mappings[0][1])
    combined = " UNION ALL ".join(f"({p})" for p in parts)
    full = _fill_template(sql_template, tables_mappings[0][0].name, tables_mappings[0][1]) or ''
    stripped = _strip_trailing_order(full)
    trailing_order = full[len(stripped):].strip() if len(stripped) < len(full) else ''
    if trailing_order:
        return f"SELECT * FROM ({combined}) _u {trailing_order}"
    return f"SELECT * FROM ({combined}) _u"


def build_graph(tables: list, conn: duckdb.DuckDBPyConnection,
                initial_nodes: int = 8,
                on_node: Callable[[KNode], None] | None = None) -> KGraph:
    catalog = load_catalog()
    graph = KGraph()

    # Collect all (question, matching tables+mappings) pairs
    candidates = []
    mappings = {t.name: map_columns(t.df, catalog) for t in tables}
    for q in catalog.questions:
        matched = [(t, mappings[t.name]) for t in tables
                   if all(s in mappings[t.name] for s in q.required_slots)]
        if matched:
            candidates.append((q, matched))

    added = 0
    for q, matched in candidates:
        if added >= initial_nodes:
            break
        sql = _multi_table_sql(q.sql, matched)
        if not sql:
            continue
        result = _execute_sql(sql, conn)
        table_label = matched[0][0].name if len(matched) == 1 else ", ".join(t.name for t, _ in matched)
        node = graph.add_node(q.question_id, q.text, q.topic, table_label)
        node.result = result
        if on_node:
            on_node(node)
        added += 1
    return graph


def expand_node(graph: KGraph, node_id: str, tables: list,
                conn: duckdb.DuckDBPyConnection,
                on_node: Callable[[KNode, int, int], None] | None = None) -> list[KNode]:
    parent = graph.nodes.get(node_id)
    if not parent or parent.explored:
        return []

    catalog = load_catalog()
    mappings = {t.name: map_columns(t.df, catalog) for t in tables}

    related = [q for q in catalog.questions
               if q.topic == parent.topic and q.question_id != parent.question_id]
    related += [q for q in catalog.questions if q.topic != parent.topic][:5]

    new_nodes = []
    total = len(related)
    for i, q in enumerate(related):
        if any(n.question_id == q.question_id for n in graph.nodes.values()):
            continue
        matched = [(t, mappings[t.name]) for t in tables
                   if all(s in mappings[t.name] for s in q.required_slots)]
        if not matched:
            continue
        sql = _multi_table_sql(q.sql, matched)
        if not sql:
            continue
        result = _execute_sql(sql, conn)
        if result and result.get("data"):
            table_label = matched[0][0].name if len(matched) == 1 else ", ".join(t.name for t, _ in matched)
            node = graph.add_node(q.question_id, q.text, q.topic, table_label, parent_id=node_id)
            node.result = result
            new_nodes.append(node)
            if on_node:
                on_node(node, len(new_nodes), total)
            if len(new_nodes) >= 8:
                break

    parent.explored = True
    return new_nodes
