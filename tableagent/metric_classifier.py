"""KNN classifier on MiniLM embeddings — matches user queries to catalog questions."""
import numpy as np


class MetricClassifier:
    def __init__(self, embedder):
        self.embedder = embedder
        self.questions = []
        self._idx_to_q = []  # maps each embedding row back to its Question
        self.embeddings = None

    def fit(self, questions):
        self.questions = list(questions)
        texts = []
        self._idx_to_q = []
        for q in self.questions:
            texts.append(q.text)
            self._idx_to_q.append(q)
            for alias in (q.aliases or []):
                texts.append(alias)
                self._idx_to_q.append(q)
        self.embeddings = self.embedder.encode(texts)
        norms = np.linalg.norm(self.embeddings, axis=1, keepdims=True)
        norms[norms == 0] = 1
        self.embeddings = self.embeddings / norms

    def predict(self, text, top_k=3):
        query_emb = self.embedder.encode([text])[0]
        norm = np.linalg.norm(query_emb)
        if norm > 0:
            query_emb = query_emb / norm
        scores = self.embeddings @ query_emb
        # Deduplicate: keep best score per question
        best = {}
        for i, s in enumerate(scores):
            q = self._idx_to_q[i]
            if q.question_id not in best or s > best[q.question_id][1]:
                best[q.question_id] = (q, float(s))
        ranked = sorted(best.values(), key=lambda x: x[1], reverse=True)
        return ranked[:top_k]
