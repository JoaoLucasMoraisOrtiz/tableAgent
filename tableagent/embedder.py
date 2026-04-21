import numpy as np
from sentence_transformers import SentenceTransformer, util


class Embedder:
    def __init__(self, model_name='paraphrase-multilingual-MiniLM-L12-v2'):
        self.model_name = model_name
        self._model = None
    
    @property
    def model(self):
        if self._model is None:
            self._model = SentenceTransformer(self.model_name)
        return self._model
    
    def encode(self, texts):
        return self.model.encode(texts, convert_to_numpy=True)
    
    def similarity(self, a, b):
        emb_a = self.model.encode(a, convert_to_tensor=True)
        emb_b = self.model.encode(b, convert_to_tensor=True)
        return float(util.cos_sim(emb_a, emb_b))
    
    def find_matches(self, query, candidates, top_k=5):
        query_emb = self.model.encode(query, convert_to_tensor=True)
        cand_embs = self.model.encode(candidates, convert_to_tensor=True)
        scores = util.cos_sim(query_emb, cand_embs)[0]
        top_results = scores.topk(min(top_k, len(candidates)))
        return [(candidates[idx], float(score)) for score, idx in zip(top_results.values, top_results.indices)]
