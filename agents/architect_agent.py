"""
Architect Agent - Hybrid design and verification plan builder.
Uses deterministic math (scipy) for known topologies and LLM reasoning for novel ones.
"""
import os
import re
import sys
import math
from termcolor import colored
from models.state import AgentState
from config.settings import MODEL_NAME, API_KEY
from utils.coeff_designer import validate_dsp_specifications, validate_iir_specifications
from utils.api_utils import call_llm_with_retry


# ---------------------------------------------------------------------------
# Known topology registry — deterministic path handles these
# ---------------------------------------------------------------------------
KNOWN_FIR_TOPOLOGIES = {
    "direct_form", "symmetric", "symmetric_direct_form",
     "transposed", "transposed_direct_form",
    "folded",
}
KNOWN_IIR_TOPOLOGIES = {
    "biquad_df2t", "biquad_df1", "cascaded",
}


def _coerce_int(value, default=None):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _coerce_float(value, default=None):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _format_float_list(values):
    if not values:
        return "[]"
    return "[" + ", ".join(f"{v:.6g}" for v in values) + "]"


def _format_int_list(values):
    if not values:
        return "[]"
    return "[" + ", ".join(str(v) for v in values) + "]"


def _quantize_coeffs(coeffs, coeff_width, frac_bits=None):
    """Convert float coefficients to fixed-point integers dynamically.

    Args:
        coeffs: list of floats
        coeff_width: total bit-width for coefficients
        frac_bits: Optional explicit fractional bit count. If provided, the
            calculation in steps 1-3 is skipped and this value is used directly.
    Returns:
        (fixed_list, used_frac_bits)
    """
    if not coeffs:
        return [], 0

    # determine fractional bits
    if frac_bits is None:
        # 1. Find the maximum absolute coefficient
        max_abs = max(abs(c) for c in coeffs)

        # 2. Calculate required integer bits
        if max_abs < 1.0:
            int_bits = 0
        else:
            # math.floor(math.log2(x)) + 1 gives the number of bits needed left of the decimal
            int_bits = math.floor(math.log2(max_abs)) + 1

        # 3. Calculate fractional bits (Total Width - Sign Bit - Integer Bits)
        coeff_frac_bits = max(coeff_width - 1 - int_bits, 0)
    else:
        coeff_frac_bits = frac_bits

    coeff_scale = 1 << coeff_frac_bits
    
    # 4. Define hardware clipping boundaries
    max_clip = (1 << (coeff_width - 1)) - 1 if coeff_width > 0 else 0
    min_clip = -(1 << (coeff_width - 1)) if coeff_width > 0 else 0
    
    # 5. Quantize
    fixed = []
    for c in coeffs:
        val = int(round(c * coeff_scale))
        val = max(min_clip, min(max_clip, val))
        fixed.append(val)
        
    # Return both the array AND the fractional bits used
    return fixed, coeff_frac_bits


# --- area dataset helpers --------------------------------------------------
_area_data_cache = None

def _load_area_dataset():
    """Lazy-load the CSV containing area results.

    Returns a list of dict rows with keys: topology, taps, data_width,
    total_area_um2 (float), cell_count (int) and optional symmetry (string).
    Empty area entries are ignored.
    """
    global _area_data_cache
    if _area_data_cache is not None:
        return _area_data_cache

    root = os.path.dirname(os.path.dirname(__file__))  # project root
    path = os.path.join(root, "dataset", "dataset_gp.csv")
    data = []
    try:
        # use utf-8-sig so that any BOM at start of file is removed automatically
        with open(path, newline='', encoding='utf-8-sig') as csvfile:
            import csv
            reader = csv.DictReader(csvfile)
            # strip any surrounding whitespace from fieldnames
            if reader.fieldnames:
                reader.fieldnames = [fn.strip() for fn in reader.fieldnames]
            for row in reader:
                # clean whitespace on each key/value pair
                row = {k.strip(): v for k, v in row.items()}
                try:
                    taps = int(row.get('taps', 0))
                    dw = int(row.get('data_width', 0))
                except ValueError:
                    continue
                area_str = row.get('total_area_um2', '').strip()
                if area_str == '':
                    # skip rows with missing area numbers
                    continue
                try:
                    area = float(area_str)
                except ValueError:
                    continue
                cells = None
                try:
                    cells = int(row.get('cell_count', 0))
                except ValueError:
                    cells = None
                # optional timing column — may be missing (older CSVs) or empty
                # (row not yet measured). None means "unknown, skip slack filter".
                cp_str = (row.get('critical_path_ns', '') or '').strip()
                if cp_str == '':
                    cp_ns = None
                else:
                    try:
                        cp_ns = float(cp_str)
                    except ValueError:
                        cp_ns = None
                data.append({
                    'topology': row.get('topology', '').strip(),
                    'taps': taps,
                    'data_width': dw,
                    'total_area_um2': area,
                    'cell_count': cells,
                    'symmetry': row.get('symmetry', '').strip().lower(),
                    'critical_path_ns': cp_ns,
                })
    except FileNotFoundError:
        # dataset not available
        data = []
    _area_data_cache = data
    return data


def _estimate_area_for_topology(topology: str, taps: int, data_width: int):
    """Estimate the total area for a given topology and tap count.

    The dataset only contains measurements for a few tap counts.  We handle
    three cases:
      * exact match: return the measured area.
      * interpolation: if the requested taps lie between two measured points,
        perform linear interpolation.
      * extrapolation: if taps fall outside the measured range, extrapolate
        using the slope between the two nearest points.
      * single data point: scale linearly by taps.

    Returns a tuple ``(area, matched_taps, was_estimate)`` or ``(None, None,
    False)`` if no suitable data exists for that topology/data_width.
    """
    rows = [r for r in _load_area_dataset()
            if r['topology'] == topology and r['data_width'] == data_width]
    if not rows:
        return (None, None, False)

    # sort by taps
    rows_sorted = sorted(rows, key=lambda r: r['taps'])
    # look for exact match
    for r in rows_sorted:
        if r['taps'] == taps:
            return (r['total_area_um2'], taps, False)

    # we need to estimate
    if len(rows_sorted) == 1:
        # only one point, scale linearly
        base = rows_sorted[0]
        area = base['total_area_um2'] * taps / base['taps']
        return (area, base['taps'], True)

    # find neighbours
    lower = None
    upper = None
    for r in rows_sorted:
        if r['taps'] < taps:
            lower = r
        elif r['taps'] > taps and upper is None:
            upper = r
    if lower and upper:
        # interpolation
        slope = ((upper['total_area_um2'] - lower['total_area_um2']) /
                 (upper['taps'] - lower['taps']))
        area = lower['total_area_um2'] + slope * (taps - lower['taps'])
        return (area, lower['taps'], True)
    elif lower:
        # extrapolate upward using last two points
        r1, r2 = rows_sorted[-2], rows_sorted[-1]
        slope = ((r2['total_area_um2'] - r1['total_area_um2']) /
                 (r2['taps'] - r1['taps']))
        area = r2['total_area_um2'] + slope * (taps - r2['taps'])
        return (area, r2['taps'], True)
    elif upper:
        # extrapolate downward using first two points
        r1, r2 = rows_sorted[0], rows_sorted[1]
        slope = ((r2['total_area_um2'] - r1['total_area_um2']) /
                 (r2['taps'] - r1['taps']))
        area = r1['total_area_um2'] + slope * (taps - r1['taps'])
        return (area, r1['taps'], True)
    return (None, None, False)


def _estimate_critical_path_for_topology(topology: str, taps: int, data_width: int):
    """Estimate the critical-path delay (ns) for a topology at a given tap count.

    Mirrors _estimate_area_for_topology: exact match → measured value;
    interpolate between neighbours; extrapolate using the slope of the two
    nearest points; single point → return it unchanged (critical path for
    pipelined/transposed topologies is independent of taps).

    Returns ``(cp_ns, matched_taps, was_estimate)`` or ``(None, None, False)``
    when no row has a usable critical_path_ns for that topology/data_width.
    """
    rows = [r for r in _load_area_dataset()
            if r['topology'] == topology
            and r['data_width'] == data_width
            and r.get('critical_path_ns') is not None]
    if not rows:
        return (None, None, False)

    rows_sorted = sorted(rows, key=lambda r: r['taps'])
    for r in rows_sorted:
        if r['taps'] == taps:
            return (r['critical_path_ns'], taps, False)

    if len(rows_sorted) == 1:
        base = rows_sorted[0]
        return (base['critical_path_ns'], base['taps'], True)

    lower = None
    upper = None
    for r in rows_sorted:
        if r['taps'] < taps:
            lower = r
        elif r['taps'] > taps and upper is None:
            upper = r
    if lower and upper:
        slope = ((upper['critical_path_ns'] - lower['critical_path_ns']) /
                 (upper['taps'] - lower['taps']))
        cp = lower['critical_path_ns'] + slope * (taps - lower['taps'])
        return (cp, lower['taps'], True)
    elif lower:
        r1, r2 = rows_sorted[-2], rows_sorted[-1]
        slope = ((r2['critical_path_ns'] - r1['critical_path_ns']) /
                 (r2['taps'] - r1['taps']))
        cp = r2['critical_path_ns'] + slope * (taps - r2['taps'])
        return (cp, r2['taps'], True)
    elif upper:
        r1, r2 = rows_sorted[0], rows_sorted[1]
        slope = ((r2['critical_path_ns'] - r1['critical_path_ns']) /
                 (r2['taps'] - r1['taps']))
        cp = r1['critical_path_ns'] + slope * (taps - r1['taps'])
        return (cp, r1['taps'], True)
    return (None, None, False)


def _clock_period_ns(target_clock_mhz):
    """Return T_clk in ns, or None when the target clock is missing/invalid."""
    mhz = _coerce_float(target_clock_mhz, None)
    if mhz is None or mhz <= 0:
        return None
    return 1000.0 / mhz


def _compute_slack_ns(critical_path_ns, target_clock_mhz):
    """slack = T_clk − T_cp. Returns None if either input is missing."""
    if critical_path_ns is None:
        return None
    t_clk = _clock_period_ns(target_clock_mhz)
    if t_clk is None:
        return None
    return t_clk - float(critical_path_ns)


def _topology_is_symmetric(topology: str) -> bool:
    """Return True only for topologies that implement symmetric processing."""
    name = (topology or "").strip().lower()
    if not name:
        return False
    if name.startswith("non_symmetric") or "asymmetric" in name:
        return False
    return (
        name == "symmetric"
        or name.startswith("symmetric_")
        or name.endswith("_symmetric")
        or "_symmetric_" in name
    )


def _select_topology_by_area(max_area: float, taps: int, data_width: int, require_symmetric: bool | None = None):
    """Pick topology meeting area constraint using dataset patterns.

    A new ``symmetry`` column in the CSV may be used to indicate whether the
    topology is symmetric.  When ``require_symmetric`` is True or False, rows
    not matching that symmetry value are ignored.  ``None`` means ignore
    symmetry.

    The algorithm estimates area for every topology available at the given
    data width (interpolating/extrapolating when necessary), then selects the
    smallest estimate under the budget.  If none satisfy the budget the
    absolutely smallest topology is returned.  The returned dictionary contains
    ``topology`` and ``total_area_um2`` (which may be estimated) plus an
    ``estimated`` flag.
    """
    estimates = []
    # gather unique topologies for this data width
    for row in _load_area_dataset():
        if row['data_width'] != data_width:
            continue
        # enforce symmetry requirement if provided
        if require_symmetric is not None:
            symflag = str(row.get('symmetry', 'no')).strip().lower()
            is_sym = symflag in ['yes', 'true', '1']
            if require_symmetric != is_sym:
                continue
        topo = row['topology']
        if any(e['topology'] == topo for e in estimates):
            continue
        area, matched_taps, is_est = _estimate_area_for_topology(topo, taps, data_width)
        if area is not None:
            estimates.append({'topology': topo,
                              'total_area_um2': area,
                              'matched_taps': matched_taps,
                              'estimated': is_est})
    if not estimates:
        return None
    # sort by area
    estimates.sort(key=lambda e: e['total_area_um2'])
    for e in estimates:
        if e['total_area_um2'] <= max_area:
            return e
    return estimates[0]


# ---------------------------------------------------------------------------
# Topology routing — known vs novel
# ---------------------------------------------------------------------------

def _is_known_topology(structure: str, filter_class: str) -> bool:
    """Returns True if structure is in the known set or is blank/auto."""
    s = structure.strip().lower()
    if not s or s in ("auto", "autosel", ""):
        return True
    known = KNOWN_IIR_TOPOLOGIES if filter_class.upper() == "IIR" else KNOWN_FIR_TOPOLOGIES
    return s in known


# ---------------------------------------------------------------------------
# LLM-powered topology reasoning (for novel/unknown topologies)
# ---------------------------------------------------------------------------

_LLM_TOPOLOGY_SYSTEM = """You are a digital signal processing hardware architect.
Given filter specifications and a requested topology, determine the hardware
characteristics for that topology.

You MUST respond ONLY in KEY=VALUE format, one per line. No markdown, no explanation outside the format.

Required output:
TOPOLOGY=<final topology name>
LATENCY=<integer number of clock cycles from input valid to output valid>
LATENCY_RULE=<brief formula or explanation>
MULTIPLIER_COUNT=<integer number of multipliers required>
PIPELINE_STAGES=<integer number of pipeline register stages>
REASONING=<one line explaining why this topology suits the specs>
"""


def _parse_llm_topology_response(text: str) -> dict:
    """Parse KEY=VALUE lines from LLM response into a dict."""
    result = {}
    for match in re.finditer(r'^(\w+)=(.+)$', text, re.MULTILINE):
        key = match.group(1).strip().upper()
        val = match.group(2).strip()
        result[key] = val
    # Coerce numeric fields
    for int_key in ("LATENCY", "MULTIPLIER_COUNT", "PIPELINE_STAGES"):
        if int_key in result:
            try:
                result[int_key] = int(result[int_key])
            except (ValueError, TypeError):
                pass
    return result


def _llm_select_topology(specs: dict, taps: int, data_width: int, coeff_width: int,
                         is_symmetric: bool, is_iir: bool) -> dict:
    """Use LLM to reason about a novel topology's hardware characteristics.

    Returns dict with keys: TOPOLOGY, LATENCY, LATENCY_RULE, MULTIPLIER_COUNT,
    PIPELINE_STAGES, REASONING.

    Falls back to conservative defaults if LLM fails.
    """
    filter_spec = specs.get("filter_design_specification", {})
    structure = filter_spec.get("structure", "")
    filter_class = "IIR" if is_iir else "FIR"
    filter_type = filter_spec.get("filter_type", "lowpass")
    hw_spec = specs.get("hardware_specifications", {})
    area_budget = hw_spec.get("max_area_um2", "not specified")

    user_prompt = f"""Design a hardware implementation for this filter:

FILTER_CLASS={filter_class}
FILTER_TYPE={filter_type}
REQUESTED_TOPOLOGY={structure}
ORDER={filter_spec.get('order', taps - 1 if taps > 0 else 0)}
TAPS={taps}
DATA_WIDTH={data_width}
COEFF_WIDTH={coeff_width}
SYMMETRIC={'YES' if is_symmetric else 'NO'}
AREA_BUDGET={area_budget}

Determine the hardware characteristics for the "{structure}" topology.
If this topology is not feasible for the given specs, suggest the closest
alternative and explain why.

Respond ONLY in KEY=VALUE format:
TOPOLOGY=<name>
LATENCY=<integer cycles>
LATENCY_RULE=<explanation>
MULTIPLIER_COUNT=<integer>
PIPELINE_STAGES=<integer>
REASONING=<one line>
"""

    # Default fallback values
    default_latency = max(1, math.ceil(math.log2(max(taps, 2))) + 1)
    fallback = {
        "TOPOLOGY": structure or "direct_form",
        "LATENCY": default_latency,
        "LATENCY_RULE": f"LLM_FALLBACK: generic ceil(log2({taps}))+1 = {default_latency}",
        "MULTIPLIER_COUNT": taps,
        "PIPELINE_STAGES": default_latency,
        "REASONING": "LLM unavailable — using conservative defaults",
    }

    if not API_KEY:
        print(colored("[Architect] No API key — using deterministic fallback for novel topology", "yellow"))
        return fallback

    try:
        messages = [
            {"role": "system", "content": _LLM_TOPOLOGY_SYSTEM},
            {"role": "user", "content": user_prompt},
        ]
        response = call_llm_with_retry(model=MODEL_NAME, messages=messages, api_key=API_KEY)
        raw = response.choices[0].message.content
        parsed = _parse_llm_topology_response(raw)

        # Validate required fields
        if "TOPOLOGY" not in parsed or "LATENCY" not in parsed:
            print(colored("[Architect] LLM response missing required fields — using fallback", "yellow"))
            return fallback

        # Sanity-clamp latency
        lat = parsed["LATENCY"]
        if not isinstance(lat, int) or lat < 1:
            parsed["LATENCY"] = fallback["LATENCY"]
            parsed["LATENCY_RULE"] = fallback["LATENCY_RULE"]

        print(colored(f"[Architect] LLM topology: {parsed['TOPOLOGY']}, "
                      f"latency={parsed['LATENCY']}, "
                      f"reason: {parsed.get('REASONING', 'N/A')}", "cyan"))
        return parsed

    except Exception as e:
        print(colored(f"[Architect] LLM topology call failed ({e}) — using fallback", "yellow"))
        return fallback


# ---------------------------------------------------------------------------
# LLM advisors that augment the deterministic path. Each one is strictly
# opt-in: it only edits specific fields and falls back to the existing
# deterministic behaviour on any failure. The DESIGN_PLAN / VERIFICATION_PLAN
# key set stays identical so downstream parsers (designer, TB generator,
# debug agent, fixers, summary) are untouched.
# ---------------------------------------------------------------------------

_LLM_RESOLVE_SYSTEM = """You are a DSP architect helping fix spec contradictions.
Given a list of contradictions, propose the SMALLEST set of spec edits that
resolves them. Respond ONLY in KEY=VALUE lines, no prose, no markdown.
Allowed keys (edit only the ones needed):
  order=<int>
  window_type=<hamming|hanning|blackman|kaiser>
  transition_width_hz=<number>
  stopband_attenuation_db=<number>
  coefficient_width=<int>
  data_width=<int>
  accumulator_width=<int>
REASON=<one sentence>"""


def _llm_resolve_contradictions(filter_spec: dict, hw_spec: dict,
                                  contradictions: list) -> dict:
    """Ask the LLM for minimal spec edits that would resolve contradictions.

    Returns a dict like {"order": 66, "window_type": "blackman", "REASON": "..."}
    or an empty dict if the LLM call fails or returns nothing usable.
    """
    if not API_KEY or not contradictions:
        return {}

    bullets = "\n".join(
        f"- {c.get('rule', '?')}: {c.get('issue', '?')}"
        + (f" | details: {c.get('details', '')}" if c.get('details') else "")
        for c in contradictions
    )
    fs_common = filter_spec.get("common", {}) or {}
    fir_methods = filter_spec.get("fir_methods", {}) or {}
    window_type = (fir_methods.get("firwin", {}) or {}).get("window_type", "")
    user_prompt = f"""FILTER_CLASS={filter_spec.get('filter_class', 'FIR')}
FILTER_TYPE={filter_spec.get('filter_type', 'lowpass')}
ORDER={filter_spec.get('order', 0)}
SAMPLING_FREQUENCY_HZ={fs_common.get('sampling_frequency_hz', '?')}
CUTOFF_FREQUENCY_HZ={fs_common.get('cutoff_frequency_hz', '?')}
TRANSITION_WIDTH_HZ={fs_common.get('transition_width_hz', '?')}
STOPBAND_ATTENUATION_DB={fs_common.get('stopband_attenuation_db', '?')}
PASSBAND_RIPPLE_DB={fs_common.get('passband_ripple_db', '?')}
WINDOW_TYPE={window_type}
DATA_WIDTH={hw_spec.get('data_width', '?')}
COEFF_WIDTH={hw_spec.get('coefficient_width', '?')}
ACC_WIDTH={hw_spec.get('accumulator_width', '?')}

CONTRADICTIONS:
{bullets}

Propose minimal edits. Respond only in KEY=VALUE lines.
"""
    try:
        resp = call_llm_with_retry(
            model=MODEL_NAME,
            messages=[{"role": "system", "content": _LLM_RESOLVE_SYSTEM},
                      {"role": "user", "content": user_prompt}],
            api_key=API_KEY,
        )
        raw = resp.choices[0].message.content or ""
    except Exception as e:
        print(colored(f"[Architect] Resolver LLM failed: {e}", "yellow"))
        return {}

    edits = {}
    for m in re.finditer(r'^(\w+)=(.+)$', raw, re.MULTILINE):
        key = m.group(1).strip().lower()
        val = m.group(2).strip()
        if key == "reason":
            edits["REASON"] = val
            continue
        for int_key in ("order", "coefficient_width", "data_width", "accumulator_width"):
            if key == int_key:
                try:
                    edits[key] = int(val)
                except ValueError:
                    pass
                break
        else:
            for num_key in ("transition_width_hz", "stopband_attenuation_db"):
                if key == num_key:
                    try:
                        edits[key] = float(val)
                    except ValueError:
                        pass
                    break
            else:
                if key == "window_type":
                    edits[key] = val.lower()
    return edits


def _apply_resolver_edits(specs: dict, edits: dict) -> list:
    """Mutate `specs` in place with the resolver's suggested edits.
    Returns a list of human-readable changes that were actually applied."""
    applied = []
    if not edits:
        return applied
    fs = specs.setdefault("filter_design_specification", {})
    common = fs.setdefault("common", {})
    hw = specs.setdefault("hardware_specifications", {})

    if "order" in edits:
        fs["order"] = edits["order"]
        applied.append(f"order -> {edits['order']}")
    if "window_type" in edits:
        fir_methods = fs.setdefault("fir_methods", {})
        firwin = fir_methods.setdefault("firwin", {})
        firwin["window_type"] = edits["window_type"]
        applied.append(f"window_type -> {edits['window_type']}")
    if "transition_width_hz" in edits:
        common["transition_width_hz"] = edits["transition_width_hz"]
        applied.append(f"transition_width_hz -> {edits['transition_width_hz']}")
    if "stopband_attenuation_db" in edits:
        common["stopband_attenuation_db"] = edits["stopband_attenuation_db"]
        applied.append(f"stopband_attenuation_db -> {edits['stopband_attenuation_db']}")
    if "coefficient_width" in edits:
        hw["coefficient_width"] = edits["coefficient_width"]
        applied.append(f"coefficient_width -> {edits['coefficient_width']}")
    if "data_width" in edits:
        hw["data_width"] = edits["data_width"]
        applied.append(f"data_width -> {edits['data_width']}")
    if "accumulator_width" in edits:
        hw["accumulator_width"] = edits["accumulator_width"]
        applied.append(f"accumulator_width -> {edits['accumulator_width']}")
    return applied


_LLM_TOPO_ADVISOR_SYSTEM = """You are a DSP hardware architect picking a
concrete topology for a known filter class. You MUST pick exactly one name
from the provided CANDIDATES list. Respond ONLY in KEY=VALUE lines.
Required:
TOPOLOGY=<one of the candidate names verbatim>
REASONING=<one sentence explaining why>"""


def _llm_pick_topology(filter_class: str, filter_type: str,
                        taps: int, data_width: int, is_symmetric: bool,
                        area_budget, candidates: list) -> dict:
    """Pick a topology from the KNOWN set using LLM reasoning over candidates.

    `candidates` is a list of dicts from _select_topology_by_area with
    'topology' and 'total_area_um2' fields. Returns a dict with 'topology',
    'reasoning', 'total_area_um2' (from candidates) on success; empty dict
    otherwise — caller should fall back to deterministic pick."""
    if not API_KEY or not candidates:
        return {}
    cand_lines = "\n".join(
        f"- {c['topology']}: est_area={c.get('total_area_um2', '?'):.0f} um^2"
        for c in candidates
    )
    budget = f"{area_budget:.0f}" if area_budget is not None else "unspecified"
    user_prompt = f"""FILTER_CLASS={filter_class}
FILTER_TYPE={filter_type}
TAPS={taps}
DATA_WIDTH={data_width}
SYMMETRIC={'YES' if is_symmetric else 'NO'}
AREA_BUDGET={budget}

CANDIDATES:
{cand_lines}

Pick the best candidate. Prefer symmetric variants when SYMMETRIC=YES.
Prefer lower-latency when budget allows. Respond only in KEY=VALUE lines.
"""
    try:
        resp = call_llm_with_retry(
            model=MODEL_NAME,
            messages=[{"role": "system", "content": _LLM_TOPO_ADVISOR_SYSTEM},
                      {"role": "user", "content": user_prompt}],
            api_key=API_KEY,
        )
        raw = resp.choices[0].message.content or ""
    except Exception as e:
        print(colored(f"[Architect] Topology advisor failed: {e}", "yellow"))
        return {}

    parsed = {}
    for m in re.finditer(r'^(\w+)=(.+)$', raw, re.MULTILINE):
        parsed[m.group(1).strip().upper()] = m.group(2).strip()
    name = parsed.get("TOPOLOGY", "").strip().lower()
    match = next((c for c in candidates if c['topology'].lower() == name), None)
    if not match:
        print(colored(f"[Architect] Topology advisor picked unknown '{name}' — falling back", "yellow"))
        return {}
    return {
        "topology": match['topology'],
        "total_area_um2": match.get('total_area_um2'),
        "estimated": match.get('estimated', False),
        "reasoning": parsed.get("REASONING", ""),
    }


_LLM_TOPO_EXPAND_SYSTEM = """You are a DSP hardware architect. The area-model
dataset has very few topologies. Propose additional topology names that could
meet these filter specs, with rough area estimates based on multiplier count,
tap count, and data width.

Respond ONLY in KEY=VALUE lines, no markdown. Up to 4 proposals.
For each proposal N (1..4):
TOPOLOGY_N=<short_snake_case_name>
EST_AREA_N=<integer um^2 estimate>
MULTIPLIERS_N=<integer>
REASONING_N=<one short sentence>
End with:
END=OK"""


def _llm_expand_topologies(filter_class: str, filter_type: str,
                             taps: int, data_width: int, coeff_width: int,
                             is_symmetric: bool, area_budget,
                             known_candidates: list) -> list:
    """Ask LLM for additional topology candidates beyond what the area table knows.

    Returns a list of dicts shaped like _select_topology_by_area entries:
    {'topology', 'total_area_um2', 'estimated': True, 'multipliers', 'reasoning'}.
    Empty list on LLM failure — caller proceeds with known_candidates only.
    """
    if not API_KEY:
        return []
    known_names = ", ".join(sorted({c['topology'] for c in known_candidates})) or "(none)"
    budget = f"{area_budget:.0f}" if area_budget is not None else "unspecified"
    user_prompt = f"""FILTER_CLASS={filter_class}
FILTER_TYPE={filter_type}
TAPS={taps}
DATA_WIDTH={data_width}
COEFF_WIDTH={coeff_width}
SYMMETRIC={'YES' if is_symmetric else 'NO'}
AREA_BUDGET={budget}
KNOWN_TOPOLOGIES=[{known_names}]

Propose up to 4 additional topologies (do NOT repeat names from KNOWN_TOPOLOGIES).
Area estimates should roughly match: multipliers * ~4000 + taps * ~800 um^2
for a 16-bit baseline, scaling with data_width. Respond only in KEY=VALUE lines."""
    try:
        resp = call_llm_with_retry(
            model=MODEL_NAME,
            messages=[{"role": "system", "content": _LLM_TOPO_EXPAND_SYSTEM},
                      {"role": "user", "content": user_prompt}],
            api_key=API_KEY,
        )
        raw = resp.choices[0].message.content or ""
    except Exception as e:
        print(colored(f"[Architect] Topology expansion failed: {e}", "yellow"))
        return []

    parsed = {}
    for m in re.finditer(r'^(\w+)=(.+)$', raw, re.MULTILINE):
        parsed[m.group(1).strip().upper()] = m.group(2).strip()

    known_lower = {c['topology'].lower() for c in known_candidates}
    proposals = []
    for n in range(1, 5):
        name = parsed.get(f"TOPOLOGY_{n}", "").strip().lower()
        if not name or name in known_lower:
            continue
        area = _coerce_float(parsed.get(f"EST_AREA_{n}", ""), None)
        mults = _coerce_int(parsed.get(f"MULTIPLIERS_{n}", ""), None)
        if area is None or area <= 0:
            continue
        proposals.append({
            'topology': name,
            'total_area_um2': area,
            'estimated': True,
            'multipliers': mults,
            'reasoning': parsed.get(f"REASONING_{n}", ""),
        })
        known_lower.add(name)
    return proposals


_LLM_PLAN_VALIDATOR_SYSTEM = """You validate a deterministically-generated DSP
filter DESIGN_PLAN + VERIFICATION_PLAN against the underlying specs. Flag
inconsistencies in numeric fields only. Do NOT rewrite the plan.

Respond ONLY in KEY=VALUE lines:
VALID=YES or VALID=NO
ISSUES=<comma-separated short issue tags, or NONE>

If VALID=NO, you MAY propose numeric edits on this exact whitelist only:
LATENCY=<int>
ACC_WIDTH=<int>
COEFF_FRAC_BITS=<int>
CLOCK_PERIOD_NS=<int>
(Omit any field you don't want to edit.)"""


_VALIDATOR_EDIT_KEYS = ("LATENCY", "ACC_WIDTH", "COEFF_FRAC_BITS", "CLOCK_PERIOD_NS")


def _llm_validate_plan(design_plan: str, verif_plan: str, specs: dict) -> dict:
    """Advisory plan validator. Returns {'valid': bool, 'issues': str, 'edits': {key: int}}."""
    if not API_KEY:
        return {'valid': True, 'issues': '', 'edits': {}}
    filter_spec = specs.get("filter_design_specification", {})
    hw_spec = specs.get("hardware_specification", {})
    user_prompt = f"""FILTER_SPEC_SNIPPET={filter_spec}
HARDWARE_SPEC_SNIPPET={hw_spec}

DESIGN_PLAN:
{design_plan}

VERIFICATION_PLAN:
{verif_plan}

Check: LATENCY match between plans, ACC_WIDTH sufficient for taps*data_width*coeff_width,
COEFF_FRAC_BITS consistent with COEFF_WIDTH, CLOCK_PERIOD_NS matches CLOCK_MHZ.
Respond only in KEY=VALUE lines."""
    try:
        resp = call_llm_with_retry(
            model=MODEL_NAME,
            messages=[{"role": "system", "content": _LLM_PLAN_VALIDATOR_SYSTEM},
                      {"role": "user", "content": user_prompt}],
            api_key=API_KEY,
        )
        raw = resp.choices[0].message.content or ""
    except Exception as e:
        print(colored(f"[Architect] Plan validator failed: {e}", "yellow"))
        return {'valid': True, 'issues': '', 'edits': {}}

    parsed = {}
    for m in re.finditer(r'^(\w+)=(.+)$', raw, re.MULTILINE):
        parsed[m.group(1).strip().upper()] = m.group(2).strip()
    valid = parsed.get("VALID", "YES").strip().upper() == "YES"
    issues = parsed.get("ISSUES", "").strip()
    edits = {}
    for key in _VALIDATOR_EDIT_KEYS:
        if key in parsed:
            val = _coerce_int(parsed[key], None)
            if val is not None and val > 0:
                edits[key] = val
    return {'valid': valid, 'issues': issues, 'edits': edits}


def _apply_validator_edits(plan_text: str, edits: dict) -> tuple[str, list]:
    """Apply whitelisted numeric edits to plan text via regex. Returns (new_text, applied_list)."""
    applied = []
    for key, new_val in edits.items():
        pattern = re.compile(rf'^({re.escape(key)})=.+$', re.MULTILINE)
        new_text, count = pattern.subn(f"{key}={new_val}", plan_text)
        if count > 0:
            plan_text = new_text
            applied.append(f"{key}={new_val}")
    return plan_text, applied


def _has_freq_params(specs: dict) -> bool:
    """Check if frequency design parameters are present (non-empty)."""
    filter_spec = specs.get("filter_design_specification", {})
    common = filter_spec.get("common", {})
    fs = common.get("sampling_frequency_hz") or filter_spec.get("sampling_frequency_hz")
    fc = common.get("cutoff_frequency_hz") or filter_spec.get("cutoff_frequency_hz")
    if isinstance(fs, (list, tuple)) and len(fs) == 0:
        fs = None
    if isinstance(fc, (list, tuple)) and len(fc) == 0:
        fc = None
    return bool(fs and fc)


def _warn_conflict(source: str, specs: dict, provided_fwd: list, provided_fbk: list, mismatches: list):
    """Check if frequency params exist alongside directly provided coefficients.
    If so, compute coefficients from design params and compare against provided ones.
    Warns only if they actually differ; prints info if they match."""
    if provided_fbk is None:
        provided_fbk = [1.0]
    
    if not _has_freq_params(specs):
        return

    filter_spec = specs.get("filter_design_specification", {})
    math_spec = specs.get("mathematical_model", {})
    common = filter_spec.get("common", {})
    fs = common.get("sampling_frequency_hz") or filter_spec.get("sampling_frequency_hz")
    fc = common.get("cutoff_frequency_hz") or filter_spec.get("cutoff_frequency_hz")
    method = filter_spec.get("design_method", "unknown")

    # Try to compute coefficients from design params for comparison
    try:
        from utils.coeff_designer import compute_coefficients
        computed_fwd, computed_fbk = compute_coefficients(filter_spec, math_spec)
    except Exception:
        # Can't compute — fall back to always warning
        print(colored(
            f"[Architect] WARNING: Cannot verify if {source} matches design params "
            f"(Fs={fs}, Fc={fc}, method={method}). "
            f"Could not compute coefficients for comparison. Proceeding with {source}.",
            "yellow", attrs=['bold']
        ))
        return

    # Compare quantized versions (what actually ends up in RTL)
    hw_spec = specs.get("hardware_specifications", {})
    coeff_width = _coerce_int(hw_spec.get("coefficient_width"), 8)
    
    provided_fwd_fixed = _quantize_coeffs(provided_fwd, coeff_width)
    computed_fwd_fixed = _quantize_coeffs(computed_fwd, coeff_width)
    provided_fbk_fixed = _quantize_coeffs(provided_fbk, coeff_width)
    computed_fbk_fixed = _quantize_coeffs(computed_fbk, coeff_width)

    fwd_match = provided_fwd_fixed == computed_fwd_fixed
    fbk_match = provided_fbk_fixed == computed_fbk_fixed

    if fwd_match and fbk_match:
        print(colored(
            f"[Architect] INFO: {source} verified — matches design params "
            f"(Fs={fs}, Fc={fc}, method={method}). Using provided coefficients.",
            "green"
        ))
    else:
        mismatch_parts = []
        if not fwd_match:
            mismatch_parts.append(f"forward (provided: {provided_fwd_fixed}, computed: {computed_fwd_fixed})")
        if not fbk_match:
            mismatch_parts.append(f"feedback (provided: {provided_fbk_fixed}, computed: {computed_fbk_fixed})")
        detail_msg = (
            f"Conflict detected between provided coefficients and those computed "
            f"from design params (Fs={fs}, Fc={fc}, method={method}).\n"
            f"Mismatch in: {', '.join(mismatch_parts)}"
        )
        print(colored(
            f"[Architect] WARNING: {detail_msg}\n  Proceeding with {source}.",
            "yellow", attrs=['bold']
        ))
        # record mismatch dictionary for architect_node
        mismatches.append({
            "rule": "Coefficient Mismatch",
            "issue": "Provided coefficients differ from computed design parameters",
            "details": detail_msg
        })

def _parse_ztransform(expr: str) -> tuple:
    """Extract forward and feedback coefficients from a z-transform expression.
    Format: H(z) = (b0 + b1*z^-1 + ...) / (a0 + a1*z^-1 + ...)
    Returns: (forward_coeffs, feedback_coeffs) tuple of lists.
    FIR filters (denominator = 1) return feedback as [1.0]."""
    if not expr:
        return ([], [])
    
    # Remove "H(z) = " prefix if present
    cleaned = re.sub(r'^H\(z\)\s*=\s*', '', expr.strip())
    
    # Split by "/" to get numerator and denominator
    if "/" not in cleaned:
        # No denominator specified, treat as FIR (all coefficients are forward)
        cleaned_num = re.sub(r'z\^?\{?-?\d+\}?', '', cleaned)
        numbers = re.findall(r'[-+]?\d*\.?\d+(?:e[-+]?\d+)?', cleaned_num, flags=re.IGNORECASE)
        forward = [_coerce_float(n, 0.0) for n in numbers] if numbers else []
        return (forward, [1.0])
    
    parts = cleaned.split("/")
    if len(parts) != 2:
        return ([], [])
    
    numerator, denominator = parts
    
    # Extract coefficients from numerator (forward path)
    num_cleaned = re.sub(r'z\^?\{?-?\d+\}?', '', numerator)
    num_numbers = re.findall(r'[-+]?\d*\.?\d+(?:e[-+]?\d+)?', num_cleaned, flags=re.IGNORECASE)
    forward = [_coerce_float(n, 0.0) for n in num_numbers] if num_numbers else []
    
    # Extract coefficients from denominator (feedback path)
    denom_cleaned = re.sub(r'z\^?\{?-?\d+\}?', '', denominator)
    denom_numbers = re.findall(r'[-+]?\d*\.?\d+(?:e[-+]?\d+)?', denom_cleaned, flags=re.IGNORECASE)
    feedback = [_coerce_float(n, 0.0) for n in denom_numbers] if denom_numbers else [1.0]
    
    return (forward, feedback)


def _check_ztransform_conflict(source: str, provided_fwd: list, provided_fbk: list, math_model: dict, coeff_width: int):
    """Cross-validate provided coefficients against z_transform_expression if present."""
    expr = math_model.get("z_transform_expression", "")
    ztrans_fwd, ztrans_fbk = _parse_ztransform(expr)
    if not ztrans_fwd and not ztrans_fbk:
        return

    provided_fwd_fixed = _quantize_coeffs(provided_fwd, coeff_width)
    provided_fbk_fixed = _quantize_coeffs(provided_fbk, coeff_width)
    ztrans_fwd_fixed = _quantize_coeffs(ztrans_fwd, coeff_width)
    ztrans_fbk_fixed = _quantize_coeffs(ztrans_fbk, coeff_width)

    fwd_match = provided_fwd_fixed == ztrans_fwd_fixed
    fbk_match = provided_fbk_fixed == ztrans_fbk_fixed

    if fwd_match and fbk_match:
        print(colored(
            f"[Architect] INFO: {source} verified — matches z_transform_expression.",
            "green"
        ))
    else:
        mismatch_parts = []
        if not fwd_match:
            mismatch_parts.append(f"forward (provided: {provided_fwd_fixed}, z-transform: {ztrans_fwd_fixed})")
        if not fbk_match:
            mismatch_parts.append(f"feedback (provided: {provided_fbk_fixed}, z-transform: {ztrans_fbk_fixed})")
        
        print(colored(
            f"[Architect] WARNING: {source} does NOT match z_transform_expression.\n"
            f"  Mismatch in: {', '.join(mismatch_parts)}\n"
            f"  Proceeding with {source}.",
            "yellow", attrs=['bold']
        ))


def _extract_coeffs(specs: dict, mismatches: list):
    """
    Extract forward and feedback coefficients from specs.
    Returns (forward_coeffs, feedback_coeffs) tuple of float lists.
    FIR filters have feedback_coeffs = [1.0].
    Raises ValueError if coefficients cannot be determined from any source.
    Appends to mismatches list if conflicts are detected.
    """
    math_model = specs.get("mathematical_model", {})
    hw_spec = specs.get("hardware_specifications", {})
    coeff_width = _coerce_int(hw_spec.get("coefficient_width"), 8)

    # Priority 1: Direct forward/feedback coefficient arrays
    fwd = math_model.get("forward_coefficients") or []
    fbk = math_model.get("feedback_coefficients") or []
    if fwd:
        fwd = [_coerce_float(c, 0.0) for c in fwd]
        fbk = [_coerce_float(c, 0.0) for c in fbk] if fbk else [1.0]
        _warn_conflict("forward_coefficients from mathematical_model", specs, fwd, fbk, mismatches)
        _check_ztransform_conflict("forward_coefficients", fwd, fbk, math_model, coeff_width)
        return (fwd, fbk)

    # Priority 1b: Legacy single "coefficients" key (backward compat, FIR only)
    legacy = math_model.get("coefficients") or []
    if legacy:
        legacy_floats = [_coerce_float(c, 0.0) for c in legacy]
        _warn_conflict("coefficients from mathematical_model", specs, legacy_floats, [1.0], mismatches)
        _check_ztransform_conflict("coefficients", legacy_floats, [1.0], math_model, coeff_width)
        return (legacy_floats, [1.0])

    # Priority 2: Extract from z-transform expression
    ztrans_fwd, ztrans_fbk = _parse_ztransform(math_model.get("z_transform_expression", ""))
    if ztrans_fwd:
        _warn_conflict("z_transform_expression from mathematical_model", specs, ztrans_fwd, ztrans_fbk, mismatches)
        return (ztrans_fwd, ztrans_fbk)

    # Priority 3: Compute from filter design parameters using scipy
    filter_spec = specs.get("filter_design_specification", {})
    common = filter_spec.get("common", {})

    # Validate required frequency parameters
    fs = common.get("sampling_frequency_hz") or filter_spec.get("sampling_frequency_hz")
    fc = common.get("cutoff_frequency_hz") or filter_spec.get("cutoff_frequency_hz")

    missing_params = []
    if not fs or (isinstance(fs, (list, tuple)) and len(fs) == 0):
        missing_params.append("sampling_frequency_hz")
    if not fc or (isinstance(fc, (list, tuple)) and len(fc) == 0):
        missing_params.append("cutoff_frequency_hz")

    if missing_params:
        raise ValueError(
            f"Cannot determine filter coefficients. "
            f"Missing required parameters: {', '.join(missing_params)}. "
            f"Provide either: (1) forward_coefficients in mathematical_model, "
            f"(2) a z_transform_expression, or "
            f"(3) sampling_frequency_hz and cutoff_frequency_hz in filter_design_specification.common"
        )

    try:
        from utils.coeff_designer import compute_coefficients
        fwd, fbk = compute_coefficients(filter_spec, math_model)
        method = filter_spec.get("design_method", "firwin")
        print(colored(f"[Architect] Computed coefficients using '{method}' method "
                      f"(forward: {len(fwd)} taps, feedback: {len(fbk)} taps)", "yellow"))
        return (fwd, fbk)
    except Exception as e:
        raise ValueError(f"Coefficient computation failed: {e}") from e


def _resolve_module_name(specs: dict) -> str:
    module_name = specs.get("module_definition", {}).get("module_name")
    if module_name:
        return module_name

    output_file = specs.get("project_settings", {}).get("output_file", "")
    if output_file:
        return os.path.splitext(os.path.basename(output_file))[0]

    return specs.get("project_settings", {}).get("project_name", "dut")


def _normalize_tb_language(value: str) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in ["systemverilog", "sv", "verilog", "iverilog", "verilator"]:
        return "systemverilog"
    if normalized in ["cocotb", "python", "pyuvm"]:
        return "cocotb"
    return "systemverilog"


def _normalize_simulator(value: str) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in ["iverilog", "icarus", "ivl"]:
        return "iverilog"
    if normalized in ["verilator"]:
        return "verilator"
    if normalized in ["vcs", "questa", "xcelium"]:
        return normalized
    return "iverilog"


def _build_plans(specs: dict, verif_specs: dict) -> tuple[str, str, list]:
    """Create design and verification plan strings.

    Returns a tuple (design_plan, verif_plan, mismatches) where mismatches is a
    list of dictionaries describing any conflicts between provided and computed
    coefficients.  The caller (architect_node) is responsible for treating these
    as fatal contradictions if desired.
    """
    missing = []
    mismatches = []
    
    filter_spec = specs.get("filter_design_specification", {})
    math_spec = specs.get("mathematical_model", {})
    hw_spec = specs.get("hardware_specifications", {})
    module_name = _resolve_module_name(specs)
    filter_class = filter_spec.get("filter_class", "FIR").upper()
    filter_type = filter_spec.get("filter_type", "unknown")
    structure = filter_spec.get("structure", "direct_form")
    is_iir = filter_class == "IIR"

    fwd_coeffs, fbk_coeffs = _extract_coeffs(specs, mismatches)

    # Auto-detect symmetry from forward coefficients
    if fwd_coeffs:
        is_symmetric = all(
            abs(fwd_coeffs[i] - fwd_coeffs[len(fwd_coeffs) - 1 - i]) < 1e-10
            for i in range(len(fwd_coeffs) // 2)
        )
    else:
        is_symmetric = False

    order_spec = _coerce_int(filter_spec.get("order"), None) or _coerce_int(math_spec.get("order"), None)

    if fwd_coeffs:
        if is_iir:
            order = max(len(fwd_coeffs), len(fbk_coeffs)) - 1
            taps = len(fwd_coeffs)
        else:
            taps = len(fwd_coeffs)
            order = taps - 1
        if order_spec is not None and order_spec != order:
            missing.append("order_mismatch")
    else:
        missing.append("coefficients")
        if order_spec is not None:
            order = order_spec
            taps = max(order + 1, 0)
        else:
            order = 0
            taps = 0

    data_width = _coerce_int(hw_spec.get("data_width"), 16)
    coeff_width = _coerce_int(hw_spec.get("coefficient_width"), 8)
    acc_width = _coerce_int(hw_spec.get("accumulator_width"), 24)
    if hw_spec.get("data_width") is None:
        missing.append("data_width")
    if hw_spec.get("coefficient_width") is None:
        missing.append("coefficient_width")
    if hw_spec.get("accumulator_width") is None:
        missing.append("accumulator_width")

    # Ensure accumulator width is sufficient for FIR filters
    if filter_class == "FIR" and taps > 0:
        min_acc_width = data_width + coeff_width + int(math.ceil(math.log2(taps)))
        acc_width = max(acc_width, min_acc_width)

    # initial frac bits guess (may be overridden for IIR)
    coeff_frac_bits = max(coeff_width - 1, 0)

    # Quantize coefficients
    if is_iir:
        # Phase 3 dynamic radix: compute global fractional bits based on both arrays
        all_coeffs = (fwd_coeffs or []) + (fbk_coeffs or [])
        max_val = max((abs(c) for c in all_coeffs), default=0.0)
        if max_val < 1.0:
            integer_bits = 1
        else:
            integer_bits = math.ceil(math.log2(max_val)) + 1
        coeff_frac_bits = max(coeff_width - integer_bits, 0)
        # now quantize both with same fractional bits
        fwd_fixed, _ = _quantize_coeffs(fwd_coeffs, coeff_width, frac_bits=coeff_frac_bits)
        fbk_fixed, _ = _quantize_coeffs(fbk_coeffs, coeff_width, frac_bits=coeff_frac_bits)
    else:
        fwd_fixed, coeff_frac_bits = _quantize_coeffs(fwd_coeffs, coeff_width)
        fbk_fixed = []

    clock_mhz = _coerce_int(specs.get("project_settings", {}).get("target_clock_freq_mhz"), 100)
    reset_type = hw_spec.get("reset_type", hw_spec.get("reset_polarity", "active_low"))

    status = "OK" if not missing else f"ERROR:{','.join(missing)}"

    # determine topology: either explicitly requested or auto‑select based on area
    topology = structure or "direct_form"
    folding = "YES" if topology == "folded" else "NO"
    fold_factor = taps if folding == "YES" else 0

    topology_from_llm = False
    llm_topology_result = None
    topology_reasoning = ""  # populated by LLM advisor (auto mode) or novel-topology path

    # ── ROUTING: known topology (deterministic) vs novel topology (LLM) ──
    if _is_known_topology(structure, filter_class):
        # ══════════════════════════════════════════════════════════════
        # KNOWN TOPOLOGY — existing deterministic path (unchanged)
        # ══════════════════════════════════════════════════════════════

        # area-based auto selection (RAG using dataset)
        area_budget = None
        if 'max_area_um2' in hw_spec:
            area_budget = _coerce_float(hw_spec.get('max_area_um2'))
        elif 'area_um2' in hw_spec:
            area_budget = _coerce_float(hw_spec.get('area_um2'))
        if area_budget is not None:
            print(colored(f"[Architect] AREA_BUDGET specified: {area_budget:.2f} um^2", "cyan"))
            t_clk_ns = _clock_period_ns(clock_mhz)
            if t_clk_ns is not None:
                print(colored(
                    f"[Architect] CLOCK target: {clock_mhz} MHz -> T_clk = {t_clk_ns:.2f} ns (slack filter ON)",
                    "cyan"))
            if not structure or structure.lower() in ['auto', 'autosel', 'auto_select']:
                sym_req = is_symmetric if not is_iir else None
                # Gather all candidates under the budget and let the LLM pick
                # with reasoning. The LLM must pick one of these exact names,
                # so downstream parsers still see a known topology.
                #
                # IMPORTANT: symmetry of the COEFFICIENTS is never used as a
                # hard filter here. A symmetric-coefficient filter runs fine
                # on direct_form or transposed_direct_form — it just doesn't
                # exploit the pre-adder optimization. Excluding non-symmetric
                # topologies whenever coefficients happen to be symmetric
                # wrongly threw away viable, faster topologies (e.g.
                # transposed at 7.5ns) in favor of only the symmetric-tagged
                # ones (e.g. symmetric at 15.99ns), even when the symmetric
                # ones failed timing. Symmetry is only used later as a soft
                # tiebreaker preference passed to the LLM picker.
                candidates = []
                slack_rejected = []  # topologies eliminated by negative slack
                evaluated = set()    # topology names already considered (the dataset
                                     # has multiple rows per topology — one per tap
                                     # count — but we evaluate each topology once)
                for row in _load_area_dataset():
                    if row.get('data_width') != data_width:
                        continue
                    topo_name = row['topology']
                    if topo_name in evaluated:
                        continue
                    evaluated.add(topo_name)
                    area_est, matched, is_est = _estimate_area_for_topology(topo_name, taps, data_width)
                    if area_est is None or area_est > area_budget:
                        continue
                    # critical path / slack — hard filter when both are known
                    cp_est, cp_matched, cp_is_est = _estimate_critical_path_for_topology(
                        topo_name, taps, data_width)
                    slack = _compute_slack_ns(cp_est, clock_mhz)
                    if slack is not None and slack < 0:
                        slack_rejected.append({
                            'topology': topo_name,
                            'critical_path_ns': cp_est,
                            'slack_ns': slack,
                        })
                        print(colored(
                            f"[Architect] SLACK FAIL: {topo_name} est_cp={cp_est:.2f} ns, "
                            f"slack={slack:.2f} ns (T_clk={t_clk_ns:.2f}) — rejected",
                            "yellow"))
                        continue
                    # Reject symmetric-pre-adder topologies when coefficients
                    # are NOT actually symmetric — using a pre-adder on
                    # asymmetric coefficients produces incorrect RTL.
                    if sym_req is False and _topology_is_symmetric(topo_name):
                        continue
                    candidates.append({
                        'topology': topo_name,
                        'total_area_um2': area_est,
                        'matched_taps': matched,
                        'estimated': is_est,
                        'critical_path_ns': cp_est,
                        'critical_path_estimated': cp_is_est,
                        'slack_ns': slack,
                    })
                    if slack is not None:
                        print(colored(
                            f"[Architect] SLACK PASS: {topo_name} est_cp={cp_est:.2f} ns, "
                            f"slack={slack:.2f} ns",
                            "green"))

                # Recovery: if the slack filter wiped out every candidate, fall
                # back to the topology with the smallest critical path so the
                # flow still produces a plan (with a clear warning). Designer
                # may then choose to lower the clock or add pipelining.
                if not candidates and slack_rejected:
                    best = min(slack_rejected, key=lambda r: r['critical_path_ns'])
                    print(colored(
                        f"[Architect] WARNING: no topology meets timing at {clock_mhz} MHz. "
                        f"Falling back to '{best['topology']}' (smallest critical path "
                        f"{best['critical_path_ns']:.2f} ns, slack {best['slack_ns']:.2f} ns).",
                        "red", attrs=['bold']))
                    # rebuild this one as a candidate so the area/LLM path can still pick it
                    area_est, matched, is_est = _estimate_area_for_topology(
                        best['topology'], taps, data_width)
                    if area_est is not None:
                        candidates.append({
                            'topology': best['topology'],
                            'total_area_um2': area_est,
                            'matched_taps': matched,
                            'estimated': is_est,
                            'critical_path_ns': best['critical_path_ns'],
                            'critical_path_estimated': True,
                            'slack_ns': best['slack_ns'],
                        })

                # Only invoke LLM expansion when we have NO real, dataset-
                # backed candidates at all. Any candidate here already passed
                # area AND slack filters against real (or interpolated) data
                # — that is strictly more trustworthy than an LLM inventing
                # area/timing numbers it cannot verify. Expanding when 1-2
                # real candidates already exist just dilutes the picker's
                # decision with fabricated topologies that have no RTL
                # implementation, which is exactly what produced
                # 'parallel_symmetric', 'retimed_symmetric', etc. in earlier
                # runs — none of which the RTL generator can actually build.
                if len(candidates) == 0:
                    print(colored(
                        "[Architect] No dataset-backed topology survives area+slack "
                        "filtering — asking LLM for rough estimates as a last resort. "
                        "These are UNVERIFIED guesses, not measured data.",
                        "yellow", attrs=['bold']
                    ))
                    expansions = _llm_expand_topologies(
                        filter_class, filter_type, taps, data_width,
                        coeff_width, is_symmetric, area_budget, candidates
                    )
                    for prop in expansions:
                        if area_budget is None or prop['total_area_um2'] <= area_budget:
                            candidates.append({
                                'topology': prop['topology'],
                                'total_area_um2': prop['total_area_um2'],
                                'matched_taps': None,
                                'estimated': True,
                            })
                            print(colored(
                                f"[Architect] LLM proposed novel topology: {prop['topology']} "
                                f"(est {prop['total_area_um2']:.0f} um^2) [UNVERIFIED]"
                                + (f" — {prop['reasoning']}" if prop.get('reasoning') else ""),
                                "yellow"
                            ))

                advised = _llm_pick_topology(
                    filter_class, filter_type, taps, data_width,
                    is_symmetric, area_budget, candidates
                ) if candidates else {}

                if advised:
                    picked = advised['topology']
                    if picked not in KNOWN_FIR_TOPOLOGIES and picked not in KNOWN_IIR_TOPOLOGIES:
                        print(colored(
                            f"[Architect] WARNING: LLM picked '{picked}' which has NO RTL "
                            f"implementation. Falling back to nearest real candidate instead.",
                            "red", attrs=['bold']
                        ))
                        real_candidates = [c for c in candidates
                                            if c['topology'] in KNOWN_FIR_TOPOLOGIES
                                            or c['topology'] in KNOWN_IIR_TOPOLOGIES]
                        if real_candidates:
                            choice = min(real_candidates, key=lambda c: c['total_area_um2'])
                            topology = choice['topology']
                            est_area = choice.get('total_area_um2')
                        else:
                            print(colored(
                                "[Architect] FATAL: no real topology available — "
                                "cannot proceed safely.", "red", attrs=['bold']
                            ))
                            raise ValueError(
                                f"No implementable topology found for TAPS={taps}, "
                                f"DATA_WIDTH={data_width}, AREA_BUDGET={area_budget}, "
                                f"CLOCK_MHZ={clock_mhz}. Consider relaxing area or "
                                f"clock constraints, or add dataset rows at higher tap counts."
                            )
                    else:
                        topology = picked
                        est_area = advised.get('total_area_um2')
                    topology_reasoning = advised.get('reasoning', '')
                    print(colored(
                        f"[Architect] LLM topology advisor picked '{topology}'"
                        + (f" — {topology_reasoning}" if topology_reasoning else ""),
                        "cyan"
                    ))
                else:
                    # No LLM verdict — pick from the candidates list that has
                    # already passed area+symmetry+slack filters. Only fall
                    # back to a dataset-wide query if candidates is empty
                    # (e.g. data_width has no rows at all).
                    if candidates:
                        choice = min(candidates, key=lambda c: c['total_area_um2'])
                        topology = choice['topology']
                        est_area = choice.get('total_area_um2')
                        msg = (f"Selected topology '{topology}' from filtered candidates "
                               f"(smallest area among slack-passing)")
                        if choice.get('estimated'):
                            msg += f" [area estimated from taps={choice.get('matched_taps')}]"
                        print(colored(msg, "green"))
                    else:
                        choice = _select_topology_by_area(area_budget, taps, data_width, require_symmetric=sym_req)
                        if choice:
                            topology = choice['topology']
                            msg = (f"Selected topology '{topology}' from dataset-wide query "
                                   f"(no slack-aware candidates available)")
                            if choice.get('estimated'):
                                msg += f" [area estimated from taps={choice.get('matched_taps')}]"
                            print(colored(msg, "yellow"))
                            est_area = choice.get('total_area_um2')
                        else:
                            print(colored(f"[Architect] WARNING: no dataset entries available for data_width={data_width}", "yellow"))
                            est_area = None
            else:
                if structure:
                    choice = _select_topology_by_area(float('inf'), taps, data_width)
                    if choice and choice['topology'] == structure:
                        est_area = choice.get('total_area_um2')
                    else:
                        est_area = None

        # Mirror topology to structure if it was blank/auto
        if (not structure or structure.lower() in ['auto', 'autosel', 'auto_select']) and topology:
            structure = topology

        # Pipeline latency: deterministic formulas for known topologies
        if is_iir:
            latency = _coerce_int(hw_spec.get("iir_latency"), 1)
            if latency not in (1, 2):
                latency = 1
            latency_rule = "IIR rigid (1 or 2 cycles)"
        else:
            if topology == "transposed_direct_form" or topology.startswith("transposed"):
                latency = 3
                latency_rule = "3 (transposed_direct_form fixed latency)"
            elif topology == "systolic" or topology.startswith("systolic"):
                # FIR SYSTOLIC: one always_ff stage per PE + one output register.
                latency = taps + 1 if taps > 0 else 1
                latency_rule = (
                    f"TAPS+1 = {taps}+1 = {latency} "
                    "(systolic: one FF per PE + output reg)"
                )
            
            elif topology == "folded":
                latency = taps + 2 if taps > 0 else 2
                latency_rule = (
                    f"TAPS+2 = {taps}+2 = {latency} "
                    "(folded: data_reg(1) + TAPS MAC cycles + result_out(1))"
                )

            else:
                # FIR DIRECT/SYMMETRIC: LATENCY = ceil(log2(TAPS)) + 1
                if taps > 0:
                    latency = int(math.ceil(math.log2(taps))) + 1
                else:
                    latency = 1
                latency_rule = "ceil(log2(TAPS)) + 1"

    else:
        # ══════════════════════════════════════════════════════════════
        # NOVEL TOPOLOGY — LLM reasoning path
        # ══════════════════════════════════════════════════════════════
        topology_from_llm = True
        print(colored(f"\n[Architect] Novel topology requested: '{structure}' — using LLM reasoning", "cyan", attrs=['bold']))

        llm_topology_result = _llm_select_topology(
            specs, taps, data_width, coeff_width,
            is_symmetric, is_iir,
        )
        topology = llm_topology_result["TOPOLOGY"]
        structure = topology
        latency = llm_topology_result["LATENCY"]
        latency_rule = llm_topology_result.get("LATENCY_RULE", "LLM-determined")
        area_budget = _coerce_float(hw_spec.get('max_area_um2') or hw_spec.get('area_um2'))

    # Estimate critical path and slack for the FINAL chosen topology so the
    # design plan can report them. Works for all paths (LLM-picked, area
    # fallback, explicit structure, novel topology). Returns None when the
    # dataset has no timing data for this topology (e.g. novel LLM proposal).
    est_cp_ns, _cp_matched_taps, est_cp_is_est = _estimate_critical_path_for_topology(
        topology, taps, data_width)
    est_slack_ns = _compute_slack_ns(est_cp_ns, clock_mhz)
    topology_is_symmetric = _topology_is_symmetric(topology)
    metric_is_symmetric = is_symmetric and topology_is_symmetric
    if est_cp_ns is not None and est_cp_ns > 0:
        fmax_msg = f", est_fmax={1000.0 / est_cp_ns:.1f} MHz"
        slack_msg = (
            f" -> slack={est_slack_ns:.2f} ns"
            if est_slack_ns is not None
            else " (no clock target — slack not computed)"
        )
        interp_tag = " [interpolated]" if est_cp_is_est else ""
        print(colored(
            f"[Architect] Chosen topology '{topology}': est_cp={est_cp_ns:.2f} ns{fmax_msg}{slack_msg}{interp_tag}",
            "cyan"))

    interface_str = (
        "clk:clk;"
        "rst_n:rst_n;"
        "sample_in:sample_in;"
        "sample_valid:sample_valid;"
        "result_out:result_out;"
        "result_valid:result_valid"
        "ready:ready (for folded only)"
    )

    # Build design plan
    design_lines = [
        "DESIGN_PLAN",
        f"STATUS={status}",
        f"MODULE={module_name}",
        f"FILTER_CLASS={filter_class}",
        f"FILTER_TYPE={filter_type}",
        f"STRUCTURE={structure}",
        f"TOPOLOGY={topology}",
        f"FOLDING={folding}",
        f"SYMMETRIC={'YES' if metric_is_symmetric else 'NO'}",
        f"FOLD_FACTOR={fold_factor}",
        f"TAPS={taps}",
        f"ORDER={order}",
    ]
    # append area info if we computed any above
    if area_budget is not None:
        design_lines.append(f"AREA_BUDGET={area_budget}")
    if 'est_area' in locals() and est_area is not None:
        design_lines.append(f"EST_AREA={est_area:.2f}")
    # timing budget — informational; downstream parsers ignore unknown keys
    if est_cp_ns is not None and est_cp_ns > 0:
        design_lines.append(f"EST_CRITICAL_PATH_NS={est_cp_ns:.2f}")
        design_lines.append(f"EST_FMAX_MHZ={1000.0 / est_cp_ns:.1f}")
    if est_slack_ns is not None:
        design_lines.append(f"EST_SLACK_NS={est_slack_ns:.2f}")
        design_lines.append(f"TIMING_MET={'YES' if est_slack_ns >= 0 else 'NO'}")

    if is_iir:
        design_lines += [
            f"FORWARD_COEFF_FLOAT={_format_float_list(fwd_coeffs)}",
            f"FORWARD_COEFF_FIXED={_format_int_list(fwd_fixed)}",
            f"FEEDBACK_COEFF_FLOAT={_format_float_list(fbk_coeffs)}",
            f"FEEDBACK_COEFF_FIXED={_format_int_list(fbk_fixed)}",
            f"FEEDBACK_TAPS={len(fbk_coeffs)}",
        ]
    else:
        design_lines += [
            f"COEFF_FLOAT={_format_float_list(fwd_coeffs)}",
            f"COEFF_FIXED={_format_int_list(fwd_fixed)}",
        ]

    design_lines += [
        f"COEFF_WIDTH={coeff_width}",
        f"COEFF_FRAC_BITS={coeff_frac_bits}",
        f"DATA_WIDTH={data_width}",
        f"ACC_WIDTH={acc_width}",
        f"CLOCK_MHZ={clock_mhz}",
        f"LATENCY={latency}",
        f"LATENCY_RULE={latency_rule}",
        f"RESET={reset_type}",
        f"INTERFACE={interface_str}",
        "VALID_TIMING=result_valid = sample_valid delayed by LATENCY cycles",
        "OUTPUT_SCALE=result_out = (accum >>> COEFF_FRAC_BITS) then truncate to DATA_WIDTH",
    ]

    # NOTE: LLM reasoning is intentionally NOT embedded in the plan text.
    # Downstream agents parse KEY=VALUE lines with naive regex, and free-form
    # prose (with commas/quotes/newlines) was confusing them. We log the
    # reasoning to the console instead — see _llm_pick_topology call site.

    # Build verification plan
    tb_settings = verif_specs.get("testbench_settings", {})
    tb_language = _normalize_tb_language(tb_settings.get("method", "systemverilog"))
    simulator = _normalize_simulator(verif_specs.get("verification_framework", {}).get("simulator", "iverilog"))
    tb_filename = tb_settings.get("filename", "output/test_filter")

    vector_count_spec = _coerce_int(
        verif_specs.get("test_coverage", {}).get("num_random_vectors"),
        _coerce_int(specs.get("verification_strategy", {}).get("test_vectors", {}).get("count"), None),
    )
    if vector_count_spec is None:
        vector_count_spec_str = "NA"
        vector_count = 200
    else:
        vector_count_spec_str = str(vector_count_spec)
        vector_count = min(vector_count_spec, 200)
    if vector_count <= 0:
        vector_count = 200

    # Force 0.0 tolerance for EXACT match strategy
    tolerance_percent = 0.0

    clock_period_ns = 10
    if clock_mhz > 0:
        clock_period_ns = int(round(1000.0 / clock_mhz))

    verif_lines = [
        "VERIFICATION_PLAN",
        f"STATUS={status}",
        f"TB_LANGUAGE={tb_language}",
        f"SIMULATOR={simulator}",
        f"TESTBENCH_FILE={tb_filename}",
        f"RTL_MODULE_NAME_FOR_INSTANTIATION={module_name}",
        f"VECTOR_COUNT={vector_count}",
        f"VECTOR_COUNT_SPEC={vector_count_spec_str}",
        f"TAPS={taps}",
        f"ORDER={order}",
        "GOLDEN_MODEL=SHADOW_MODEL (Bit-Accurate Integer Math)",
        f"TOLERANCE_PERCENT={tolerance_percent}",
        f"CLOCK_PERIOD_NS={clock_period_ns}",
        "RESET_SEQ=assert reset low for 2 cycles",
        "PRINT_FORMAT=Sample X: RTL=Y Golden=Z Error=E%",
        "MISMATCH_POLICY=fail if any mismatch (Exact Match Required)",
        f"LATENCY={latency}",
        f"DATA_WIDTH={data_width}",
        f"ACC_WIDTH={acc_width}",
        f"COEFF_WIDTH={coeff_width}",
        f"COEFF_FRAC_BITS={coeff_frac_bits}",
    ]

    if is_iir:
        verif_lines += [
            f"FORWARD_COEFF_FIXED={_format_int_list(fwd_fixed)}",
            f"FEEDBACK_COEFF_FIXED={_format_int_list(fbk_fixed)}",
            f"FEEDBACK_TAPS={len(fbk_coeffs)}",
        ]
    else:
        verif_lines.append(f"COEFF_FIXED={_format_int_list(fwd_fixed)}")

    verif_lines += [
        f"FILTER_CLASS={filter_class}",
        f"FILTER_TYPE={filter_type}",
        f"STRUCTURE={structure}",
        f"TOPOLOGY={topology}",
        f"FOLDING={folding}",
        f"FOLD_FACTOR={fold_factor}",
        f"SYMMETRIC={'YES' if metric_is_symmetric else 'NO'}",
        f"INTERFACE={interface_str}",
        "VALID_TIMING=result_valid = sample_valid delayed by LATENCY cycles",
    ]

    return "\n".join(design_lines) + "\n", "\n".join(verif_lines) + "\n", mismatches


def architect_node(state: AgentState):
    """
    Builds a deterministic design plan and verification plan from the JSON specs.
    First validates DSP specifications for mathematical consistency.
    """
    specs = state["specs"]
    verif_specs = state.get("verif_specs", {})
    hardware_specs = specs.get("hardware_specifications", {})
    
    # Extract filter_design_specification for validation
    filter_spec = specs.get("filter_design_specification", {})
    filter_class = filter_spec.get("filter_class", "").upper()
    
    print(colored("\n" + "="*70, "yellow"))
    print(colored("  DSP SPECIFICATION VALIDATION", "yellow", attrs=['bold']))
    print(colored("="*70 + "\n", "yellow"))
    
    # Run validation checks
    print(colored("[Architect] Validating DSP specifications...", "cyan"))
    contradictions = validate_dsp_specifications(filter_spec, hardware_specs)
    
    # Run IIR-specific validation if applicable
    if filter_class == "IIR":
        print(colored("[Architect] Running IIR-specific constraint validation...", "cyan"))
        iir_contradictions = validate_iir_specifications(filter_spec, hardware_specs)
        # Include the design-time checks along with actual contradictions
        contradictions.extend(iir_contradictions)
    
    # If contradictions found, show them and ask user
    if contradictions:
        print(colored("\n⚠️  MATHEMATICAL CONTRADICTIONS DETECTED!\n", "red", attrs=['bold']))
        
        for i, contradiction in enumerate(contradictions, 1):
            print(colored(f"\n--- Contradiction #{i}: {contradiction['rule']} ---", "red", attrs=['bold']))
            print(colored(f"Issue: {contradiction['issue']}", "red"))
            if contradiction.get('details'):
                print(colored(f"\nDetails:\n{contradiction['details']}", "red"))
        
        print(colored("\n" + "="*70, "red"))
        
        if filter_class == "IIR":
            prompt_msg = (
                "WARNING: The provided IIR DSP specifications contain mathematical or structural contradictions\n"
                "and cannot be safely realized in hardware."
            )
        else:
            prompt_msg = (
                "WARNING: The provided DSP specifications contain mathematical contradictions\n"
                "and cannot be physically realized to meet all constraints simultaneously."
            )
        
        print(colored(prompt_msg, "red", attrs=['bold']))
        print(colored("="*70 + "\n", "red"))

        # Ask the LLM for minimal edits that would resolve the contradictions.
        # This is opt-in for the user; we still fall back to force/exit if they
        # reject it or the LLM failed.
        print(colored("[Architect] Asking LLM advisor for suggested resolution...", "cyan"))
        resolver_edits = _llm_resolve_contradictions(filter_spec, hardware_specs, contradictions)
        if resolver_edits:
            reason = resolver_edits.get("REASON", "")
            preview_pairs = [(k, v) for k, v in resolver_edits.items() if k != "REASON"]
            print(colored("\n[Architect] Suggested edits:", "green", attrs=['bold']))
            for k, v in preview_pairs:
                print(colored(f"  - {k}: {v}", "green"))
            if reason:
                print(colored(f"  reason: {reason}", "green"))

        # Decide how to handle contradictions based on context
        force_mode = specs.get("project_settings", {}).get("force_on_contradiction", False)

        if force_mode:
            # Config explicitly says force — continue silently
            print(colored("\n[Architect] Auto-forcing past contradictions (force_on_contradiction=true)...\n", "yellow"))
            state["validation_contradictions"] = contradictions
        elif sys.stdin.isatty():
            # Interactive terminal — offer apply/force/exit
            options = "apply/force/exit" if resolver_edits else "force/exit"
            while True:
                user_input = input(
                    colored(
                        f"Type '{options}': ",
                        "yellow",
                        attrs=['bold']
                    )
                ).strip().lower()

                if user_input == "apply" and resolver_edits:
                    applied = _apply_resolver_edits(specs, resolver_edits)
                    print(colored("\n[Architect] Applied LLM suggestions: " + "; ".join(applied) + "\n",
                                  "green", attrs=['bold']))
                    state["validation_contradictions"] = []
                    state["resolver_reason"] = resolver_edits.get("REASON", "")
                    filter_spec = specs.get("filter_design_specification", {})  # refresh alias
                    break
                elif user_input == "force":
                    print(colored("\n[Architect] Proceeding with flawed parameters at user's discretion...\n", "yellow", attrs=['bold']))
                    state["validation_contradictions"] = contradictions
                    break
                elif user_input == "exit":
                    print(colored("\n[Architect] Exiting workflow. Please revise your specifications and try again.\n", "red", attrs=['bold']))
                    return {
                        "design_plan": "",
                        "verif_plan": "",
                        "status": "max_attempts_reached",
                        "validation_contradictions": contradictions
                    }
                else:
                    print(colored(f"Invalid input. Please type one of: {options}", "yellow"))
        else:
            # Non-interactive (piped stdin, CI, subprocess) — auto-force with warning
            print(colored("\n[Architect] Non-interactive mode detected — auto-forcing past contradictions...\n", "yellow", attrs=['bold']))
            state["validation_contradictions"] = contradictions
    else:
        print(colored("\n✅ All DSP specifications validated successfully!\n", "green", attrs=['bold']))
    
    print(colored("[Architect] Building deterministic design and verification plans...", "cyan"))

    try:
        design_plan, verif_plan, plan_mismatches = _build_plans(specs, verif_specs)
    except ValueError as e:
        print(colored(f"\n[Architect] FATAL: {e}", "red", attrs=['bold']))
        return {
            "design_plan": "",
            "verif_plan": "",
            "status": "max_attempts_reached"
        }
    
    # integrate plan mismatches into contradictions for prompting
    contradictions.extend(plan_mismatches)

    # LLM advisory validator: reads the deterministic plans, flags numeric
    # inconsistencies, and may propose whitelisted edits (LATENCY, ACC_WIDTH,
    # COEFF_FRAC_BITS, CLOCK_PERIOD_NS). Plan skeleton is never rewritten.
    print(colored("[Architect] Running LLM plan validator...", "cyan"))
    verdict = _llm_validate_plan(design_plan, verif_plan, specs)
    if not verdict['valid']:
        print(colored(
            f"[Architect] Plan validator flagged issues: {verdict['issues'] or 'unspecified'}",
            "yellow"
        ))
        if verdict['edits']:
            design_plan, d_applied = _apply_validator_edits(design_plan, verdict['edits'])
            verif_plan,  v_applied = _apply_validator_edits(verif_plan,  verdict['edits'])
            applied = sorted(set(d_applied + v_applied))
            if applied:
                print(colored(f"[Architect] Applied validator edits: {applied}", "green"))
    else:
        print(colored("[Architect] Plan validator: OK", "green"))

    print(colored(f"--- DESIGN PLAN ---\n{design_plan}------------------------", "cyan"))
    print(colored(f"--- VERIFICATION PLAN ---\n{verif_plan}------------------------", "cyan"))

    return {
        "design_plan": design_plan,
        "verif_plan": verif_plan
    }