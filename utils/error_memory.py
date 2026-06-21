"""
Error Memory — schema and normalization for the past-errors RAG.

This module holds the shared data layer for a retrieval system over past
RTL bugs (linter failures, simulation mismatches, debug findings). It does
NOT contain a retriever yet — that lands in a follow-up step.

=============================================================================
SCHEMA (one JSON file per entry, stored in error_memory/seed or /harvested)
=============================================================================

{
    "id":                 "<short slug, unique>",
    "source":             "linter" | "simulator" | "debug_agent",
    "topology":           "<topology name, comma-separated list, or 'any'>",
    "filter_class":       "FIR" | "IIR" | "any",
    "error_signature":    "<normalized, searchable text — BM25 index key>",
    "raw_error_example":  "<representative raw error line(s)>",
    "root_cause":         "<one-line explanation of the bug>",
    "fix_rule":           "<one-to-two-line rule for fixing it>",
    "fix_snippet":        "<short code snippet showing the fix>",
    "tags":               ["keyword", "keyword", ...],
    "source_notes":       "seed" | "harvested",
    "date":               "YYYY-MM-DD"
}

Design points:
  * `error_signature` is the field indexed by BM25 — it MUST be normalized
    (see normalize_error below) so that two occurrences of the same bug
    collide regardless of specific identifiers, line numbers, or file paths.
  * `raw_error_example` is kept as-is for human readability and as a
    fallback for substring matching if BM25 misses.
  * `fix_rule` is what gets injected into the fixer LLM prompt — keep it
    terse and imperative.
  * `topology` and `filter_class` are pre-filters applied before scoring.

=============================================================================
NORMALIZATION RULES  (applied before indexing AND at query time)
=============================================================================

Goal: two instances of the same bug should produce the same normalized
signature regardless of file paths, line numbers, or specific signal names.

 1. Strip ANSI color codes
 2. Strip file-path prefixes before :LINE:COL: markers
        "/tmp/test.v:42:12: ..." -> "<FILE>:<N>: ..."
 3. Replace bracketed bit ranges with "<BITS>"
        "[31:0]" -> "<BITS>"
 4. Replace single-quoted identifiers with "<IDENT>"
        "Signal 'accum_reg_3' ..." -> "Signal <IDENT> ..."
 5. Replace standalone numbers with "<N>"
 6. Lowercase
 7. Collapse whitespace; strip leading/trailing punctuation

Lexical tokens DELIBERATELY preserved (high value for BM25 matching):
  - Verilator/Icarus error codes:
      %error, %warning, unoptflat, width, undriven, latch, pinmissing,
      syncasyncnet, multidriven, blkandnblk, caseincomplete
  - SystemVerilog construct keywords:
      always_ff, always_comb, logic, wire, reg, assign, localparam,
      parameter, generate, for
  - Topology / structure vocabulary:
      transposed, symmetric, direct, biquad, cascaded, pipeline, pre_add,
      accumulator, state, feedback
"""

import re

_ANSI_RE  = re.compile(r"\x1b\[[0-9;]*m")
_PATH_RE  = re.compile(r"[A-Za-z]?:?[\w./\\-]*\.(?:v|sv|vh|svh):\d+(?::\d+)?:?")
_BITS_RE  = re.compile(r"\[[^\]]*:[^\]]*\]")
_IDENT_RE = re.compile(r"'[A-Za-z_][\w\.]*'")
_NUM_RE   = re.compile(r"\b\d+\b")
_SPACE_RE = re.compile(r"\s+")


def normalize_error(raw: str) -> str:
    """Normalize a raw compiler/simulator error string into a signature
    suitable for BM25 matching. See module docstring for the rule set."""
    if not raw:
        return ""
    s = _ANSI_RE.sub("", raw)
    s = _PATH_RE.sub("<FILE>:<N>:", s)
    s = _BITS_RE.sub("<BITS>", s)
    s = _IDENT_RE.sub("<IDENT>", s)
    s = _NUM_RE.sub("<N>", s)
    s = s.lower()
    s = _SPACE_RE.sub(" ", s).strip(" \t\n.,;:")
    return s
