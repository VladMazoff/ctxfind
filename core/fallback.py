from __future__ import annotations

"""TF-IDF векторизатор и косинусное сходство (чистый Python).

Контракты:
  - TFIDFVectorizer: fit(texts) -> IDF-словарь, transform(query) -> вектор.
  - search(query, top_k) -> List[TextMatch] с косинусным сходством.
  - Не требует numpy/scikit-learn (опционально использует numpy если доступен).
  - Интеграция со ScorerEngine: fallback-матчи получают score_breakdown.
"""

import math
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from config import (
    DEFAULT_SNIPPET_LINES,
    FallbackError,
    SCORING_RULES,
    TFIDF_MAX_DF_RATIO,
    TFIDF_MIN_DF,
    TFIDF_TOKEN_PATTERN,
)
from core.scorer import ScoredMatch, ScoreBreakdown


@dataclass
class TextMatch:
    """Результат текстового поиска."""
    file_path: str
    start_line: int
    end_line: int
    score: float
    snippet: str


class TFIDFVectorizer:
    """TF-IDF векторизатор на чистом Python.

    Pipeline:
      1. fit(texts) -- строит IDF-словарь.
      2. transform(query) -- возвращает TF-IDF вектор запроса.
      3. search(query, top_k) -- косинусное сходство с корпусом.
    """

    def __init__(self):
        self.idf: Dict[str, float] = {}
        self.vocab: Dict[str, int] = {}
        self.doc_vectors: List[Dict[int, float]] = []
        self.documents: List[Dict] = []
        self._has_numpy = self._check_numpy()

    def _check_numpy(self) -> bool:
        """Проверяет доступность numpy."""
        try:
            import numpy as np
            return True
        except ImportError:
            return False

    def _tokenize(self, text: str) -> List[str]:
        """Нормализует и токенизирует текст."""
        text = text.lower()
        tokens = re.findall(TFIDF_TOKEN_PATTERN, text)
        return tokens

    def fit(self, texts: List[str]) -> None:
        """Строит IDF-словарь по корпусу.

        Args:
            texts: список текстов (по одному на чанк).
        """
        if not texts:
            raise FallbackError("Пустой корпус для TF-IDF")

        n_docs = len(texts)
        doc_freq: Dict[str, int] = {}
        tokenized_docs: List[List[str]] = []

        for text in texts:
            tokens = self._tokenize(text)
            tokenized_docs.append(tokens)
            unique_tokens = set(tokens)
            for token in unique_tokens:
                doc_freq[token] = doc_freq.get(token, 0) + 1

        # Фильтруем по min/max DF
        max_df = max(1, int(n_docs * TFIDF_MAX_DF_RATIO))
        valid_tokens = {
            token for token, df in doc_freq.items()
            if TFIDF_MIN_DF <= df <= max_df
        }

        # Строим vocab и IDF
        self.vocab = {}
        self.idf = {}
        for token in sorted(valid_tokens):
            idx = len(self.vocab)
            self.vocab[token] = idx
            idf_val = math.log(n_docs / doc_freq[token]) + 1.0
            self.idf[token] = idf_val

        # [BUGFIX v0] Строим вектора документов ПОСЛЕ построения vocab
        self.doc_vectors = []
        for tokens in tokenized_docs:
            vec = self._build_vector(tokens)
            self.doc_vectors.append(vec)

    def _build_vector(self, tokens: List[str]) -> Dict[int, float]:
        """Строит разреженный TF-IDF вектор из токенов."""
        tf: Dict[str, int] = {}
        for token in tokens:
            if token in self.vocab:
                tf[token] = tf.get(token, 0) + 1

        vec: Dict[int, float] = {}
        for token, count in tf.items():
            idx = self.vocab[token]
            tf_val = 1 + math.log(count) if count > 0 else 0
            vec[idx] = tf_val * self.idf[token]

        # Нормализуем (L2)
        norm = math.sqrt(sum(v ** 2 for v in vec.values()))
        if norm > 0:
            vec = {k: v / norm for k, v in vec.items()}

        return vec

    def transform(self, query: str) -> Dict[int, float]:
        """Преобразует запрос в TF-IDF вектор."""
        tokens = self._tokenize(query)
        return self._build_vector(tokens)

    def _cosine_similarity(self, vec_a: Dict[int, float], vec_b: Dict[int, float]) -> float:
        """Косинусное сходство между двумя разреженными векторами."""
        dot = 0.0
        for idx, val_a in vec_a.items():
            if idx in vec_b:
                dot += val_a * vec_b[idx]
        return dot

    def search(
        self,
        query: str,
        top_k: int = 10,
    ) -> List[Tuple[int, float]]:
        """Ищет top-k похожих документов.

        Returns:
            Список (doc_index, score), отсортированный по score desc.
        """
        query_vec = self.transform(query)
        if not query_vec:
            return []

        scores: List[Tuple[int, float]] = []
        for idx, doc_vec in enumerate(self.doc_vectors):
            score = self._cosine_similarity(query_vec, doc_vec)
            if score > 0:
                scores.append((idx, score))

        scores.sort(key=lambda x: x[1], reverse=True)
        return scores[:top_k]


class FallbackEngine:
    """Движок fallback-поиска по тексту.

    Контракт:
      - Индексирует файлы как чанки строк.
      - При поиске: TF-IDF по чанкам -> возвращает TextMatch с сниппетами.
      - Интеграция: конвертирует TextMatch в ScoredMatch с fallback_tfidf весом.
    """

    def __init__(self, snippet_lines: int = DEFAULT_SNIPPET_LINES):
        self.snippet_lines = snippet_lines
        self.vectorizer = TFIDFVectorizer()
        self.chunks: List[Dict] = []
        self._is_fitted = False

    def fit_files(self, file_entries: List) -> None:
        """Индексирует файлы для fallback-поиска.

        Args:
            file_entries: список FileEntry (или любых объектов с .path).
        """
        texts = []
        self.chunks = []

        for entry in file_entries:
            try:
                with open(entry.path, "r", encoding="utf-8") as f:
                    lines = f.readlines()
            except Exception:
                continue

            # Разбиваем на чанки по N строк
            chunk_size = max(1, self.snippet_lines * 2)
            for start in range(0, len(lines), chunk_size):
                end = min(start + chunk_size, len(lines))
                chunk_lines = lines[start:end]
                text = "".join(chunk_lines)

                texts.append(text)
                self.chunks.append({
                    "file_path": entry.path,
                    "start_line": start + 1,
                    "end_line": end,
                    "lines": chunk_lines,
                })

        if texts:
            self.vectorizer.fit(texts)
            self._is_fitted = True

    def search(
        self,
        query: str,
        top_k: int = 10,
    ) -> List[TextMatch]:
        """Выполняет fallback-поиск.

        Returns:
            Список TextMatch с сниппетами +/-N строк.
        """
        if not self._is_fitted:
            return []

        results = self.vectorizer.search(query, top_k)
        matches: List[TextMatch] = []

        for doc_idx, score in results:
            chunk = self.chunks[doc_idx]

            # Расширяем сниппет на +/-snippet_lines
            start_line = max(1, chunk["start_line"] - self.snippet_lines)
            end_line = chunk["end_line"] + self.snippet_lines

            try:
                with open(chunk["file_path"], "r", encoding="utf-8") as f:
                    all_lines = f.readlines()
                end_line = min(end_line, len(all_lines))
                snippet_lines = all_lines[start_line - 1:end_line]
                snippet = "".join(snippet_lines)
            except Exception:
                snippet = "".join(chunk["lines"])

            matches.append(TextMatch(
                file_path=chunk["file_path"],
                start_line=start_line,
                end_line=end_line,
                score=score,
                snippet=snippet,
            ))

        return matches

    def search_as_scored(
        self,
        query: str,
        top_k: int = 10,
        lang: Optional[str] = None,
    ) -> List[ScoredMatch]:
        """Выполняет fallback-поиск и возвращает ScoredMatch с breakdown.

        Args:
            query: поисковый запрос.
            top_k: количество результатов.
            lang: язык файла (для метаданных).

        Returns:
            Список ScoredMatch с fallback_tfidf весом.
        """
        text_matches = self.search(query, top_k)
        scored_matches: List[ScoredMatch] = []

        for tm in text_matches:
            # Нормализуем TF-IDF score (0..1) -> вес
            # fallback_tfidf = 12 по умолчанию, масштабируем пропорционально
            normalized_score = min(tm.score, 1.0)
            tfidf_weight = int(SCORING_RULES["fallback_tfidf"] * normalized_score)

            breakdown = ScoreBreakdown()
            breakdown.fallback_tfidf = tfidf_weight

            scored = ScoredMatch(
                node_id=f"fallback_{tm.file_path}:{tm.start_line}",
                text=tm.snippet[:200],
                file_path=tm.file_path,
                line=tm.start_line,
                col=0,
                end_line=tm.end_line,
                score=min(tfidf_weight, 100),  # Cap
                score_breakdown=breakdown,
                match_type="fallback_tfidf",
                semantic_role=None,
                meta={
                    "tfidf_raw_score": tm.score,
                    "lang": lang,
                    "mode": "vector",
                },
            )
            scored_matches.append(scored)

        return scored_matches