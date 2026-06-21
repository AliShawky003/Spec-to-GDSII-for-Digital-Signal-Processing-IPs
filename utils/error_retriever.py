"""
Error Retriever — BM25 retrieval over past RTL/TB errors.

Indexes JSON entries from two directories:
  1. error_memory/seed/       (hand-written canonical bugs)
  2. error_memory/harvested/  (auto-captured from successful fixes)

Public API:
  retrieve_errors(raw_error, source, topology, filter_class, top_k)
      -> list of matching entry dicts (may be empty)

  format_error_context(entries)
      -> compact string block for injection into a fixer LLM prompt

  save_harvested_error(...)
      -> write a new JSON entry to the harvested directory + rebuild index

The schema and normalization rules live in utils/error_memory.py.
"""

import os
import re
import json
import hashlib
from typing import Optional
from rank_bm25 import BM25Okapi

from utils.error_memory import normalize_error


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
_SCRIPT_DIR   = os.path.dirname(os.path.abspath(__file__))
_PROJECT_DIR  = os.path.dirname(_SCRIPT_DIR)
SEED_DIR      = os.path.join(_PROJECT_DIR, "error_memory", "seed")
HARVESTED_DIR = os.path.join(_PROJECT_DIR, "error_memory", "harvested")


# ---------------------------------------------------------------------------
# Index
# ---------------------------------------------------------------------------
class _ErrorEntry:
    """One indexed error entry with its JSON data and searchable text."""

    def __init__(self, filepath: str, data: dict, source_dir: str):
        self.filepath = filepath
        self.data = data
        self.source_dir = source_dir  # "seed" or "harvested"

        # Build BM25-indexable text: normalized error_signature + tags + topology
        sig = data.get("error_signature", "")
        tags = " ".join(data.get("tags", []) or [])
        topo = data.get("topology", "")
        fclass = data.get("filter_class", "")
        self.search_text = f"{sig} {tags} {topo} {fclass}".lower()

    def tokens(self) -> list:
        return re.findall(r"[a-z0-9_<>]+", self.search_text)


class _ErrorIndex:
    """BM25 index over all available error entries."""

    def __init__(self):
        self.entries: list = []
        self.bm25: Optional[BM25Okapi] = None
        self._build()

    def _load_dir(self, directory: str, tag: str):
        if not os.path.isdir(directory):
            return
        for fname in sorted(os.listdir(directory)):
            if not fname.endswith(".json"):
                continue
            fpath = os.path.join(directory, fname)
            try:
                with open(fpath, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except (OSError, json.JSONDecodeError):
                continue
            self.entries.append(_ErrorEntry(fpath, data, tag))

    def _build(self):
        self.entries = []
        self._load_dir(SEED_DIR, "seed")
        self._load_dir(HARVESTED_DIR, "harvested")
        if self.entries:
            corpus = [e.tokens() for e in self.entries]
            self.bm25 = BM25Okapi(corpus)
        else:
            self.bm25 = None

    def rebuild(self):
        self._build()

    def retrieve(
        self,
        query_text: str,
        top_k: int = 2,
        source: Optional[str] = None,
        topology: Optional[str] = None,
        filter_class: Optional[str] = None,
        min_score: float = 0.5,
    ) -> list:
        """Return up to top_k entry dicts scoring above min_score, pre-filtered
        by source/topology/filter_class when provided."""
        if not self.bm25 or not self.entries:
            return []

        tokens = re.findall(r"[a-z0-9_<>]+", query_text.lower())
        if not tokens:
            return []

        scores = self.bm25.get_scores(tokens)
        scored = list(zip(scores, self.entries))

        def _match(val: str, candidates: str) -> bool:
            """True if val is 'any' or matches any comma-separated candidate.
            Matching is bidirectional substring so 'transposed' matches a stored
            'transposed_direct_form' and vice versa."""
            if not val or not candidates:
                return True
            val_l = val.strip().lower()
            if val_l in ("", "any"):
                return True
            cands = [c.strip().lower() for c in candidates.split(",")]
            if "any" in cands:
                return True
            for c in cands:
                if val_l == c or val_l in c or c in val_l:
                    return True
            return False

        filtered = []
        for score, entry in scored:
            if score < min_score:
                continue
            d = entry.data
            if source and not _match(source, d.get("source", "")):
                continue
            if topology and not _match(topology, d.get("topology", "")):
                continue
            if filter_class and not _match(filter_class, d.get("filter_class", "")):
                continue
            filtered.append((score, entry))

        # Seed entries win ties over harvested ones
        filtered.sort(
            key=lambda x: (x[0], 1 if x[1].source_dir == "seed" else 0),
            reverse=True,
        )

        out = []
        for score, entry in filtered[:top_k]:
            result = dict(entry.data)
            result["_score"] = round(float(score), 2)
            result["_source_dir"] = entry.source_dir
            out.append(result)
        return out


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------
_index: Optional[_ErrorIndex] = None


def _get_index() -> _ErrorIndex:
    global _index
    if _index is None:
        _index = _ErrorIndex()
    return _index


# ---------------------------------------------------------------------------
# Public: retrieve
# ---------------------------------------------------------------------------
def retrieve_errors(
    raw_error: str,
    source: Optional[str] = None,
    topology: Optional[str] = None,
    filter_class: Optional[str] = None,
    top_k: int = 2,
) -> list:
    """Retrieve past error entries matching the given raw error text.

    Args:
        raw_error:    Raw compiler/simulator error text (will be normalized).
        source:       "linter" | "simulator" | "debug_agent" (optional filter)
        topology:     e.g. "symmetric", "transposed_direct_form" (optional)
        filter_class: "FIR" | "IIR" (optional)
        top_k:        Max number of entries to return (default 2)

    Returns:
        List of dicts, each a full entry with added _score and _source_dir.
    """
    query = normalize_error(raw_error)
    if not query:
        return []
    return _get_index().retrieve(
        query_text=query,
        top_k=top_k,
        source=source,
        topology=topology,
        filter_class=filter_class,
    )


# ---------------------------------------------------------------------------
# Public: format for prompt injection
# ---------------------------------------------------------------------------
def format_error_context(entries: list) -> str:
    """Format retrieved entries into a compact block for a fixer LLM prompt.

    Keeps each entry small (root_cause + fix_rule + short snippet).
    Returns an empty string when there are no entries so callers can
    conditionally append.
    """
    if not entries:
        return ""
    lines = ["========== SIMILAR PAST ISSUES (from error memory) =========="]
    for i, e in enumerate(entries, start=1):
        lines.append(f"[{i}] id={e.get('id','?')}  (score={e.get('_score','?')})")
        rc = e.get("root_cause", "").strip()
        if rc:
            lines.append(f"    ROOT_CAUSE: {rc}")
        fr = e.get("fix_rule", "").strip()
        if fr:
            lines.append(f"    FIX_RULE:   {fr}")
        snippet = (e.get("fix_snippet") or "").strip()
        if snippet:
            snippet_lines = snippet.splitlines()
            if len(snippet_lines) > 12:
                snippet_lines = snippet_lines[:12] + ["    // ... (truncated)"]
            lines.append("    FIX_SNIPPET:")
            for sl in snippet_lines:
                lines.append(f"      {sl}")
    lines.append("==========  END SIMILAR PAST ISSUES  ==========")
    lines.append("")
    lines.append("Use the rules above as guidance. Do NOT copy snippets verbatim —")
    lines.append("adapt to the current code's signal names, widths, and topology.")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Public: harvest on success
# ---------------------------------------------------------------------------
def _short_hash(text: str, n: int = 10) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:n]


def save_harvested_error(
    raw_error: str,
    source: str,
    topology: str,
    filter_class: str,
    root_cause: str = "",
    fix_rule: str = "",
    fix_snippet: str = "",
    tags: Optional[list] = None,
    date: str = "",
) -> str:
    """Save a new error entry to error_memory/harvested/ and rebuild the index.

    De-duplicates on normalized signature: if an existing seed or harvested
    entry has the same signature + source + filter_class, harvest is skipped.

    Returns the filepath written (empty string if skipped).
    """
    if not raw_error:
        return ""

    signature = normalize_error(raw_error)
    if not signature:
        return ""

    # De-dupe: skip if an equivalent entry already exists
    index = _get_index()
    for entry in index.entries:
        d = entry.data
        existing_sig = (d.get("error_signature") or "").lower().strip()
        if existing_sig and existing_sig == signature:
            same_source = (d.get("source", "").lower() == source.lower())
            same_class  = (d.get("filter_class", "").lower() == filter_class.lower())
            if same_source and same_class:
                return ""

    os.makedirs(HARVESTED_DIR, exist_ok=True)
    slug_source = re.sub(r"[^a-z0-9_]+", "_", source.lower()) or "unknown"
    slug_topo   = re.sub(r"[^a-z0-9_]+", "_", (topology or "any").lower())
    entry_id = f"harv_{slug_source}_{slug_topo}_{_short_hash(signature)}"
    fpath = os.path.join(HARVESTED_DIR, f"{entry_id}.json")

    entry = {
        "id": entry_id,
        "source": source,
        "topology": topology or "any",
        "filter_class": filter_class or "any",
        "error_signature": signature,
        "raw_error_example": raw_error.strip()[:2000],
        "root_cause": root_cause or "(auto-harvested; no root-cause summary)",
        "fix_rule": fix_rule or "(auto-harvested; see fix_snippet)",
        "fix_snippet": (fix_snippet or "").strip()[:2000],
        "tags": tags or [],
        "source_notes": "harvested",
        "date": date or "",
    }

    with open(fpath, "w", encoding="utf-8") as f:
        json.dump(entry, f, indent=2)

    # Rebuild index so the new entry is immediately retrievable
    index.rebuild()
    return fpath


# ---------------------------------------------------------------------------
# Convenience: pretty-print matches to the terminal (for logging in nodes)
# ---------------------------------------------------------------------------
def describe_matches(entries: list) -> str:
    """One-line-per-entry human summary (for stdout logging, not prompts)."""
    if not entries:
        return "no matches"
    return "; ".join(
        f"{e.get('id','?')} (score={e.get('_score','?')}, src={e.get('_source_dir','?')})"
        for e in entries
    )
