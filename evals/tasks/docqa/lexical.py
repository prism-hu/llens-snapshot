"""Deterministic lexical retrieval baseline for the frozen docqa corpus.

The index deliberately has no dependency on the benchmark questions or gold
fields.  It indexes every ``.md`` and ``.yaml`` file below ``corpus_root`` and
uses the same simple tokenizer for documents and queries: CJK character
bigrams plus normalized ASCII/number tokens.  It is a reproducible lexical
baseline, not a dense or hybrid retrieval system.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
import hashlib
import math
from pathlib import Path
import re
from typing import Any
import unicodedata

import yaml


_CJK_RUN = re.compile(r"[\u3040-\u30ff\u3400-\u9fff\uf900-\ufaff]+")
_ASCII_OR_NUMBER = re.compile(r"[a-z][a-z0-9_-]*|\d+(?:[.,]\d+)?")
_FRONTMATTER_DELIMITER = re.compile(r"^(---|\.\.\.)\s*$")


def _tokens(value: str) -> list[str]:
    """Return CJK bigrams and ASCII/number tokens after NFKC normalization."""
    normalized = unicodedata.normalize("NFKC", value or "").lower()
    out: list[str] = []
    for run in _CJK_RUN.findall(normalized):
        if len(run) == 1:
            out.append(run)
        else:
            out.extend(run[i:i + 2] for i in range(len(run) - 1))
    out.extend(_ASCII_OR_NUMBER.findall(normalized))
    return out


def _canonical_path(corpus_root: Path, source: Path) -> str:
    """Map a corpus file to the document-tool path used for citations."""
    relative = source.relative_to(corpus_root).with_suffix("")
    parts = list(relative.parts)
    if parts and parts[-1] == "_index":
        parts.pop()
    return "/" + "/".join(parts) if parts else "/"


def _frontmatter_and_body(raw: str) -> tuple[str, str]:
    """Split simple YAML frontmatter without a frontmatter package."""
    lines = raw.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return "", raw
    for index in range(1, len(lines)):
        if _FRONTMATTER_DELIMITER.match(lines[index].strip()):
            return "".join(lines[1:index]), "".join(lines[index + 1:])
    return "", raw


def _yaml_citation_metadata(raw: str) -> str:
    """Extract compact date/source-style metadata from a YAML document."""
    try:
        parsed = yaml.safe_load(raw)
    except yaml.YAMLError:
        return ""
    if not isinstance(parsed, dict):
        return ""

    def select(value: Any, depth: int = 0) -> dict[str, Any]:
        selected: dict[str, Any] = {}
        for key in sorted(value, key=lambda item: str(item)):
            key_text = str(key)
            lowered = key_text.lower()
            child = value[key]
            if (any(term in lowered for term in ("date", "source", "url", "origin", "version", "revision", "title"))
                    or lowered in {"metadata", "meta", "header"}):
                selected[key_text] = child
            elif depth < 2 and isinstance(child, dict):
                nested = select(child, depth + 1)
                if nested:
                    selected[key_text] = nested
        return selected

    selected = select(parsed)
    return yaml.safe_dump(selected, allow_unicode=True, default_flow_style=False, sort_keys=True).strip()


@dataclass(frozen=True)
class _Chunk:
    path: str
    chunk_id: str
    text: str
    ordinal: int
    term_counts: Counter[str]
    length: int


class LexicalIndex:
    """Sparse in-memory BM25 index over a frozen Markdown/YAML corpus.

    ``search`` returns dictionaries so that a runner can inject static context
    directly and preserve a canonical source path for citation evaluation.
    Construction reads the corpus once and performs no logging.
    """

    k1 = 1.2
    b = 0.75
    tokenizer = "nfkc-lower; cjk-bigram; ascii-number"
    schema_version = 1

    def __init__(self, corpus_root: Path, chunk_chars: int = 2000, overlap_chars: int = 200) -> None:
        if chunk_chars <= 0:
            raise ValueError("chunk_chars must be positive")
        if overlap_chars < 0 or overlap_chars >= chunk_chars:
            raise ValueError("overlap_chars must be nonnegative and less than chunk_chars")

        self.corpus_root = Path(corpus_root).resolve()
        if not self.corpus_root.is_dir():
            raise ValueError("corpus_root must be an existing directory")
        self.chunk_chars = chunk_chars
        self.overlap_chars = overlap_chars
        self._chunks: list[_Chunk] = []
        self._postings: dict[str, list[tuple[int, int]]] = defaultdict(list)
        self._document_frequency: dict[str, int] = {}
        self._source_file_count = 0
        self._corpus_hash = self._build()
        self._avg_length = (
            sum(chunk.length for chunk in self._chunks) / len(self._chunks)
            if self._chunks else 0.0
        )

    def _source_files(self) -> list[Path]:
        return sorted(
            (
                path for path in self.corpus_root.rglob("*")
                if path.is_file() and path.suffix.lower() in {".md", ".yaml"}
            ),
            key=lambda path: path.relative_to(self.corpus_root).as_posix(),
        )

    def _source_parts(self, source: Path, raw: str) -> tuple[str, str]:
        if source.suffix.lower() == ".md":
            metadata, body = _frontmatter_and_body(raw)
            # Markdown frontmatter is normally compact and is preserved whole;
            # date/source and other provenance fields remain visible per chunk.
            header = metadata.strip()
        else:
            body = raw
            header = _yaml_citation_metadata(raw)
        return header, body

    def _chunk_body(self, body: str) -> list[str]:
        if not body:
            return [""]
        step = self.chunk_chars - self.overlap_chars
        chunks: list[str] = []
        start = 0
        while start < len(body):
            end = min(start + self.chunk_chars, len(body))
            chunks.append(body[start:end])
            if end == len(body):
                break
            start += step
        return chunks

    def _build(self) -> str:
        digest = hashlib.sha256(b"llens-docqa-lexical-corpus-v1\0")
        files = self._source_files()
        self._source_file_count = len(files)
        for source in files:
            relative = source.relative_to(self.corpus_root).as_posix()
            raw_bytes = source.read_bytes()
            digest.update(relative.encode("utf-8"))
            digest.update(b"\0")
            digest.update(raw_bytes)
            digest.update(b"\0")

            raw = raw_bytes.decode("utf-8", errors="replace")
            header, body = self._source_parts(source, raw)
            path = _canonical_path(self.corpus_root, source)
            for ordinal, body_chunk in enumerate(self._chunk_body(body)):
                prefix = f"path: {path}"
                if header:
                    prefix = f"{prefix}\n{header}"
                text = f"{prefix}\n\n{body_chunk}".rstrip()
                counts = Counter(_tokens(text))
                chunk = _Chunk(
                    path=path,
                    chunk_id=f"{path}#{ordinal:04d}",
                    text=text,
                    ordinal=ordinal,
                    term_counts=counts,
                    length=sum(counts.values()),
                )
                self._chunks.append(chunk)

        for index, chunk in enumerate(self._chunks):
            for term, frequency in chunk.term_counts.items():
                self._postings[term].append((index, frequency))
        self._document_frequency = {term: len(postings) for term, postings in self._postings.items()}
        return digest.hexdigest()

    def manifest(self) -> dict[str, Any]:
        """Return aggregate, non-content provenance for this deterministic index."""
        return {
            "schema_version": self.schema_version,
            "tokenizer": self.tokenizer,
            "bm25": {"k1": self.k1, "b": self.b},
            "chunk_chars": self.chunk_chars,
            "overlap_chars": self.overlap_chars,
            "source_file_count": self._source_file_count,
            "chunk_count": len(self._chunks),
            "corpus_sha256": self._corpus_hash,
        }

    def search(self, question: str, k: int = 10) -> list[dict[str, Any]]:
        """Return up to ``k`` positive-BM25 chunks in deterministic order."""
        if k <= 0 or not self._chunks:
            return []
        terms = set(_tokens(question))
        if not terms:
            return []

        n_chunks = len(self._chunks)
        scores: dict[int, float] = defaultdict(float)
        average_length = self._avg_length or 1.0
        for term in terms:
            postings = self._postings.get(term)
            if not postings:
                continue
            df = self._document_frequency[term]
            idf = math.log(1.0 + (n_chunks - df + 0.5) / (df + 0.5))
            for index, frequency in postings:
                chunk = self._chunks[index]
                normalizer = self.k1 * (1.0 - self.b + self.b * chunk.length / average_length)
                scores[index] += idf * frequency * (self.k1 + 1.0) / (frequency + normalizer)

        ranked = sorted(
            scores,
            key=lambda index: (-scores[index], self._chunks[index].path, self._chunks[index].ordinal),
        )[:k]
        return [
            {
                "path": self._chunks[index].path,
                "chunk_id": self._chunks[index].chunk_id,
                "text": self._chunks[index].text,
                "score": scores[index],
            }
            for index in ranked
        ]
