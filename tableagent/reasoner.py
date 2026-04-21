from pyDatalog import pyDatalog


class DatalogReasoner:
    def __init__(self):
        self._initialized = False
    
    def _ensure_datalog(self):
        if not self._initialized:
            pyDatalog.clear()
            exec("pyDatalog.create_terms('X, Y, Z, T, C, V')", globals())
            self._initialized = True
    
    def derive_facts(self, df, table_name):
        self._ensure_datalog()
        for _, row in df.iterrows():
            fact = f"{table_name}({', '.join(repr(v) for v in row.values)})"
            exec(f"+{fact}", globals())
    
    def query(self, rule_text):
        self._ensure_datalog()
        result = eval(f"pyDatalog.ask('{rule_text}')", globals())
        return list(result) if result else []
    
    def suggest_expansions(self, node, tables):
        self._ensure_datalog()
        suggestions = []
        for table in tables:
            suggestions.append(f"Explore {table} related to {node}")
        return suggestions
