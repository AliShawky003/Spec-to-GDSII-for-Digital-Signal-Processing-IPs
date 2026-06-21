"""
Coefficient Designer - Computes filter coefficients from design specifications
using scipy when coefficients are not provided directly.

Supported FIR design methods:
  - remez    : Parks-McClellan equiripple optimal
  - firwin   : Window-based, configurable window_type
  - firls    : Least-squares optimal, smooth response
  - kaiser   : Kaiser window with automatic order estimation

Supported IIR design methods:
  - butter   : Butterworth, maximally flat passband
  - cheby1   : Chebyshev Type I, passband ripple control
  - cheby2   : Chebyshev Type II, stopband attenuation control
  - ellip    : Elliptic (Cauer), sharpest transition for given order
  - bessel   : Bessel, maximally flat group delay

All methods return (forward_coefficients, feedback_coefficients) tuples.
FIR methods return feedback_coefficients as [1.0].
"""
import math
from scipy import signal

FIR_METHODS = {"remez", "firwin", "firls", "kaiser"}
IIR_METHODS = {"butter", "cheby1", "cheby2", "ellip", "bessel"}
ALL_METHODS = FIR_METHODS | IIR_METHODS

# Window type maximum attenuation lookup table (in dB)
WINDOW_ATTENUATION_LIMITS = {
    "rectangular": 13,
    "hamming": 43,
    "hann": 44,
    "blackman": 74,
    "bartlett": 26,
    "kaiser": 120,
    "tukey": 45,
}


# ===== DSP SPECIFICATION VALIDATION =====

def validate_dsp_specifications(filter_spec: dict, hardware_spec: dict) -> list:
    """
    Validate DSP specifications for mathematical consistency.
    
    Returns:
        List of contradiction dictionaries. Empty list means all validations passed.
        Each contradiction contains: 'rule', 'issue', 'details'
    """
    contradictions = []
    
    # Rule 1: Transition Width vs. Order
    _validate_transition_width_vs_order(filter_spec, contradictions)
    
    # Rule 2: Windowing Attenuation Limit
    _validate_windowing_attenuation_limit(filter_spec, contradictions)
    
    # Rule 3: Quantization Noise Floor
    _validate_quantization_noise_floor(filter_spec, hardware_spec, contradictions)
    
    # Rule 4: Filter Order Range
    _validate_order_range(filter_spec, contradictions)
    
    return contradictions


def _coerce_float(value, default=None):
    """Safely convert value to float"""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _coerce_int(value, default=None):
    """Safely convert value to integer"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _get_window_type(filter_spec):
    """Extract window type from specs"""
    filter_class = filter_spec.get("filter_class", "").upper()
    design_method = filter_spec.get("design_method", "").lower()
    
    if filter_class == "FIR" and design_method == "firwin":
        window_type = (filter_spec.get("fir_methods", {})
                       .get("firwin", {})
                       .get("window_type", "hamming")
                       .lower())
        return window_type
    
    return None


def _validate_transition_width_vs_order(filter_spec, contradictions):
    """Validation Rule 1: Transition Width vs. Order Check"""
    filter_class = filter_spec.get("filter_class", "").upper()
    if filter_class != "FIR":
        return
    
    common = filter_spec.get("common", {})
    requested_order = _coerce_int(filter_spec.get("order"))
    stopband_attenuation = _coerce_float(common.get("stopband_attenuation_db"))
    transition_width_hz = _coerce_float(common.get("transition_width_hz"))
    sampling_freq = _coerce_float(common.get("sampling_frequency_hz"))
    
    if None in [requested_order, stopband_attenuation, transition_width_hz, sampling_freq]:
        return
    
    delta_f = transition_width_hz / sampling_freq
    if delta_f <= 0:
        contradictions.append({
            "rule": "Transition Width vs. Order",
            "issue": "Transition width must be positive",
            "details": f"Transition width: {transition_width_hz} Hz"
        })
        return
    
    n_required = stopband_attenuation / (22 * delta_f)
    
    if requested_order < (0.8 * n_required):
        contradictions.append({
            "rule": "Transition Width vs. Order",
            "issue": f"Order {requested_order} is too low for specified transition width and stopband attenuation",
            "details": (
                f"Requested order: {requested_order}\n"
                f"Calculated minimum: {n_required:.1f}\n"
                f"Stopband attenuation: {stopband_attenuation} dB\n"
                f"Transition width: {transition_width_hz} Hz\n"
                f"Sampling frequency: {sampling_freq} Hz"
            )
        })


def _validate_windowing_attenuation_limit(filter_spec, contradictions):
    """Validation Rule 2: Windowing Attenuation Limit Check"""
    filter_class = filter_spec.get("filter_class", "").upper()
    if filter_class != "FIR":
        return
    
    design_method = filter_spec.get("design_method", "").lower()
    if design_method not in ["firwin", "kaiser"]:
        return
    
    window_type = _get_window_type(filter_spec)
    if not window_type:
        return
    
    common = filter_spec.get("common", {})
    stopband_attenuation = _coerce_float(common.get("stopband_attenuation_db"))
    if stopband_attenuation is None:
        return
    
    window_key = window_type.lower()
    max_attenuation = WINDOW_ATTENUATION_LIMITS.get(window_key)
    
    if max_attenuation is None:
        contradictions.append({
            "rule": "Windowing Attenuation Limit",
            "issue": f"Unknown window type '{window_type}'",
            "details": f"Known window types: {', '.join(WINDOW_ATTENUATION_LIMITS.keys())}"
        })
        return
    
    if stopband_attenuation > max_attenuation:
        contradictions.append({
            "rule": "Windowing Attenuation Limit",
            "issue": f"Requested attenuation {stopband_attenuation} dB exceeds {window_type} window max {max_attenuation} dB",
            "details": (
                f"Window type: {window_type}\n"
                f"Maximum attenuation: {max_attenuation} dB\n"
                f"Requested attenuation: {stopband_attenuation} dB\n"
                f"Excess: {stopband_attenuation - max_attenuation:.1f} dB"
            )
        })


def _validate_quantization_noise_floor(filter_spec, hardware_spec, contradictions):
    """Validation Rule 3: Quantization Noise Floor Check"""
    common = filter_spec.get("common", {})
    stopband_attenuation = _coerce_float(common.get("stopband_attenuation_db"))
    coefficient_width = _coerce_int(hardware_spec.get("coefficient_width"))
    
    if None in [stopband_attenuation, coefficient_width]:
        return
    
    max_quantization_attenuation = 6.0 * coefficient_width
    
    if stopband_attenuation > max_quantization_attenuation:
        contradictions.append({
            "rule": "Quantization Noise Floor",
            "issue": f"Requested attenuation {stopband_attenuation} dB exceeds quantization limit",
            "details": (
                f"Coefficient width: {coefficient_width} bits\n"
                f"Maximum from quantization: {max_quantization_attenuation:.1f} dB (6 × {coefficient_width})\n"
                f"Requested attenuation: {stopband_attenuation} dB\n"
                f"Excess: {stopband_attenuation - max_quantization_attenuation:.1f} dB"
            )
        })


def _validate_order_range(filter_spec, contradictions):
    """Additional check: Filter order in reasonable range"""
    requested_order = _coerce_int(filter_spec.get("order"))
    if requested_order is None:
        return
    
    if requested_order < 1:
        contradictions.append({
            "rule": "Filter Order Range",
            "issue": f"Filter order must be at least 1, got {requested_order}",
            "details": f"Requested order: {requested_order}"
        })
    elif requested_order > 512:
        contradictions.append({
            "rule": "Filter Order Range",
            "issue": f"Filter order {requested_order} exceeds practical maximum (512)",
            "details": "Very high orders may cause numerical instability and resource issues"
        })


def _estimate_fir_order(stopband_attenuation_db, transition_width_hz, sampling_frequency_hz):
    """Estimate FIR filter order using Kaiser's formula."""
    delta_f = transition_width_hz / sampling_frequency_hz
    if stopband_attenuation_db > 21:
        order = int((stopband_attenuation_db - 7.95) / (14.36 * delta_f))
    else:
        order = int(0.9222 / delta_f)
    if order % 2 == 0:
        order += 1
    return max(order, 2)


def _is_empty(value):
    """Check if a value is empty/missing (None, [], empty string, 0)."""
    if value is None:
        return True
    if isinstance(value, (list, tuple)) and len(value) == 0:
        return True
    if isinstance(value, str) and value.strip() == "":
        return True
    return False


def _get_param(common, flat, name):
    """Get a parameter from nested common dict or flat fallback. Returns None if empty/missing."""
    val = common.get(name)
    if _is_empty(val):
        val = flat.get(name)
    if _is_empty(val):
        return None
    return val


def _require_param(common, flat, name):
    """Get a required parameter. Raises ValueError if empty/missing."""
    val = _get_param(common, flat, name)
    if val is None:
        raise ValueError(f"Missing required parameter: {name}")
    return val


def _validate_method_params(method, order, stopband_atten, passband_ripple, transition_width):
    """Validate that all required parameters for the selected design method are present."""
    missing = []

    # Methods that need transition_width (when order not given, or for band edge definition)
    needs_transition = {"remez", "firls", "kaiser"}
    if method in needs_transition and transition_width is None:
        missing.append("transition_width_hz")

    # Methods that need stopband_attenuation_db
    needs_stopband = {"kaiser", "cheby2", "ellip"}
    if method in needs_stopband and stopband_atten is None:
        missing.append("stopband_attenuation_db")

    # FIR order estimation needs stopband_atten + transition_width when order not given
    if method in FIR_METHODS and order is None:
        if stopband_atten is None:
            missing.append("stopband_attenuation_db (required for FIR order estimation without explicit order)")
        if transition_width is None:
            missing.append("transition_width_hz (required for FIR order estimation without explicit order)")

    # Methods that need passband_ripple_db
    needs_passband = {"cheby1", "ellip"}
    if method in needs_passband and passband_ripple is None:
        missing.append("passband_ripple_db")

    if missing:
        raise ValueError(
            f"Design method '{method}' is missing required parameters: {', '.join(missing)}"
        )


def compute_coefficients(filter_spec: dict, math_spec: dict) -> tuple:
    """
    Compute filter coefficients from design specifications.

    Reads common params from filter_spec["common"], and method-specific
    params from filter_spec["fir_methods"][method] or filter_spec["iir_methods"][method].
    Falls back to flat keys for backward compatibility.

    Returns:
        (forward_coefficients, feedback_coefficients) tuple of float lists.
        FIR methods return feedback_coefficients as [1.0].
    """
    # Read common params — support nested "common" or flat keys (backward compat)
    common = filter_spec.get("common", {})
    fs = _require_param(common, filter_spec, "sampling_frequency_hz")
    fc = _require_param(common, filter_spec, "cutoff_frequency_hz")
    stopband_atten = _get_param(common, filter_spec, "stopband_attenuation_db")
    passband_ripple = _get_param(common, filter_spec, "passband_ripple_db")
    transition_width = _get_param(common, filter_spec, "transition_width_hz")

    if not isinstance(fc, (list, tuple)) and fc >= fs / 2:
        raise ValueError(f"Cutoff frequency ({fc} Hz) must be less than Nyquist ({fs/2} Hz)")

    nyq = fs / 2.0
    method = filter_spec.get("design_method", "firwin").lower()
    filter_type = filter_spec.get("filter_type", "lowpass").lower()
    order = filter_spec.get("order") or math_spec.get("order")

    # Validate per-method required parameters
    _validate_method_params(method, order, stopband_atten, passband_ripple, transition_width)

    # Resolve method-specific params from nested structure or flat fallback
    fir_methods = filter_spec.get("fir_methods", {})
    iir_methods = filter_spec.get("iir_methods", {})
    method_params = (fir_methods.get(method)
                     or iir_methods.get(method)
                     or filter_spec.get(method, {}))

    # --- FIR methods ---
    if method in FIR_METHODS:
        if order is None:
            order = _estimate_fir_order(stopband_atten, transition_width, fs)
        numtaps = order + 1

        if method == "remez":
            pw = method_params.get("passband_weight", 1.0)
            sw = method_params.get("stopband_weight", 1.0)
            b = _design_remez(filter_type, numtaps, fc, transition_width, nyq, fs, pw, sw)
        elif method == "firwin":
            window_type = method_params.get("window_type", "hamming")
            b = _design_firwin(filter_type, numtaps, fc, fs, window_type)
        elif method == "firls":
            pw = method_params.get("passband_weight", 1.0)
            sw = method_params.get("stopband_weight", 1.0)
            b = _design_firls(filter_type, numtaps, fc, transition_width, nyq, fs, pw, sw)
        elif method == "kaiser":
            b = _design_kaiser(filter_type, fc, stopband_atten, transition_width, fs)

        return (b, [1.0])

    # --- IIR methods ---
    if method in IIR_METHODS:
        if order is None:
            order = 4

        if method == "butter":
            return _design_butter(order, filter_type, fc, fs)
        elif method == "cheby1":
            return _design_cheby1(order, filter_type, fc, fs, passband_ripple)
        elif method == "cheby2":
            return _design_cheby2(order, filter_type, fc, fs, stopband_atten)
        elif method == "ellip":
            return _design_ellip(order, filter_type, fc, fs, passband_ripple, stopband_atten)
        elif method == "bessel":
            return _design_bessel(order, filter_type, fc, fs)

    raise ValueError(f"Unknown design method: '{method}'. Supported: {', '.join(sorted(ALL_METHODS))}")


# ===== FIR design methods =====

def _design_remez(filter_type, numtaps, fc, transition_width, nyq, fs,
                  passband_weight, stopband_weight):
    if filter_type == "lowpass":
        bands = [0, fc, fc + transition_width, nyq]
        desired = [1, 0]
        weight = [passband_weight, stopband_weight]
    elif filter_type == "highpass":
        if numtaps % 2 == 0:
            numtaps += 1
        bands = [0, fc - transition_width, fc, nyq]
        desired = [0, 1]
        weight = [stopband_weight, passband_weight]
    elif filter_type == "bandpass":
        if isinstance(fc, (list, tuple)) and len(fc) == 2:
            f_low, f_high = fc
        else:
            raise ValueError("Bandpass filter requires cutoff_frequency_hz as [f_low, f_high]")
        bands = [0, f_low - transition_width, f_low, f_high, f_high + transition_width, nyq]
        desired = [0, 1, 0]
        weight = [stopband_weight, passband_weight, stopband_weight]
    else:
        raise ValueError(f"remez does not support filter_type='{filter_type}'")

    return signal.remez(numtaps, bands, desired, weight=weight, fs=fs).tolist()  # type: ignore


def _design_firwin(filter_type, numtaps, fc, fs, window_type):
    pass_zero_map = {"lowpass": True, "highpass": False, "bandpass": False, "bandstop": True}
    pass_zero = pass_zero_map.get(filter_type)
    if pass_zero is None:
        raise ValueError(f"firwin does not support filter_type='{filter_type}'")
    if not pass_zero and numtaps % 2 == 0:
        numtaps += 1
    return signal.firwin(numtaps, fc, window=window_type, pass_zero=pass_zero, fs=fs).tolist()  # type: ignore


def _design_firls(filter_type, numtaps, fc, transition_width, nyq, fs,
                  passband_weight, stopband_weight):
    if numtaps % 2 == 0:
        numtaps += 1

    if filter_type == "lowpass":
        bands = [0, fc, fc + transition_width, nyq]
        desired = [1, 1, 0, 0]
        weight = [passband_weight, stopband_weight]
    elif filter_type == "highpass":
        bands = [0, fc - transition_width, fc, nyq]
        desired = [0, 0, 1, 1]
        weight = [stopband_weight, passband_weight]
    elif filter_type == "bandpass":
        if isinstance(fc, (list, tuple)) and len(fc) == 2:
            f_low, f_high = fc
        else:
            raise ValueError("Bandpass filter requires cutoff_frequency_hz as [f_low, f_high]")
        bands = [0, f_low - transition_width, f_low, f_high, f_high + transition_width, nyq]
        desired = [0, 0, 1, 1, 0, 0]
        weight = [stopband_weight, passband_weight, stopband_weight]
    else:
        raise ValueError(f"firls does not support filter_type='{filter_type}'")

    return signal.firls(numtaps, bands, desired, weight=weight, fs=fs).tolist()  # type: ignore


def _design_kaiser(filter_type, fc, stopband_atten, transition_width, fs):
    nyq = fs / 2.0
    width_normalized = transition_width / nyq
    order, beta = signal.kaiserord(stopband_atten, width_normalized)  # type: ignore
    numtaps = order + 1

    pass_zero_map = {"lowpass": True, "highpass": False, "bandpass": False, "bandstop": True}
    pass_zero = pass_zero_map.get(filter_type)
    if pass_zero is None:
        raise ValueError(f"kaiser does not support filter_type='{filter_type}'")
    if not pass_zero and numtaps % 2 == 0:
        numtaps += 1

    return signal.firwin(numtaps, fc, window=("kaiser", beta), pass_zero=pass_zero, fs=fs).tolist()  # type: ignore


# ===== IIR design methods =====

def _normalize_btype(filter_type):
    """Map filter_type to scipy btype parameter."""
    btype_map = {"lowpass": "low", "highpass": "high", "bandpass": "band", "bandstop": "bandstop"}
    btype = btype_map.get(filter_type)
    if btype is None:
        raise ValueError(f"IIR does not support filter_type='{filter_type}'. Use: lowpass, highpass, bandpass, bandstop")
    return btype


def _design_butter(order, filter_type, fc, fs):
    btype = _normalize_btype(filter_type)
    b, a = signal.butter(order, fc, btype=btype, fs=fs)  # type: ignore
    return (b.tolist(), a.tolist())  # type: ignore


def _design_cheby1(order, filter_type, fc, fs, passband_ripple):
    btype = _normalize_btype(filter_type)
    b, a = signal.cheby1(order, passband_ripple, fc, btype=btype, fs=fs)  # type: ignore
    return (b.tolist(), a.tolist())  # type: ignore


def _design_cheby2(order, filter_type, fc, fs, stopband_atten):
    btype = _normalize_btype(filter_type)
    b, a = signal.cheby2(order, stopband_atten, fc, btype=btype, fs=fs)  # type: ignore
    return (b.tolist(), a.tolist())  # type: ignore


def _design_ellip(order, filter_type, fc, fs, passband_ripple, stopband_atten):
    btype = _normalize_btype(filter_type)
    b, a = signal.ellip(order, passband_ripple, stopband_atten, fc, btype=btype, fs=fs)  # type: ignore
    return (b.tolist(), a.tolist())  # type: ignore


def _design_bessel(order, filter_type, fc, fs):
    btype = _normalize_btype(filter_type)
    b, a = signal.bessel(order, fc, btype=btype, fs=fs, norm="phase")  # type: ignore
    return (b.tolist(), a.tolist())  # type: ignore

# ===== IIR SPECIFIC VALIDATION =====

def validate_iir_specifications(filter_spec: dict, hardware_spec: dict) -> list:
    """
    Phase 1 IIR validation (pre‑generation): only mathematical checks.

    Returns:
        List of contradiction dictionaries for IIR filters. Only rules 1 and 2
        (Butterworth ripple and roll‑off rate) produce entries. Phase 2 rules
        (radix alignment, latency) are enforced later during DESIGN_PLAN/RTL
        generation and therefore do not appear here.
    """
    contradictions = []
    # Rule 1: Butterworth Ripple Check
    _validate_butterworth_ripple(filter_spec, contradictions)
    # Rule 2: Roll-Off Rate vs. Order Check
    _validate_rolloff_rate_vs_order(filter_spec, contradictions)
    # Phase 2 directives are handled elsewhere
    return contradictions


def _validate_butterworth_ripple(filter_spec, contradictions):
    """
    IIR Rule 1: Butterworth Ripple Check
    
    Butterworth filters are maximally flat. If design_method is "butter",
    passband_ripple_db MUST be 0.0.
    """
    design_method = filter_spec.get("design_method", "").lower()
    
    if design_method != "butter":
        return
    
    common = filter_spec.get("common", {})
    passband_ripple = _coerce_float(common.get("passband_ripple_db"))
    
    if passband_ripple is not None and passband_ripple > 0.0:
        contradictions.append({
            "rule": "Butterworth Ripple Check",
            "issue": f"Butterworth filter must have 0.0 dB passband ripple, got {passband_ripple} dB",
            "details": (
                f"Butterworth filters are defined by maximally flat passband response.\n"
                f"Requested passband ripple: {passband_ripple} dB\n"
                f"Expected passband ripple: 0.0 dB\n\n"
                f"Consider using a different design method (cheby1, ellip) if ripple is needed."
            )
        })


def _validate_rolloff_rate_vs_order(filter_spec, contradictions):
    """
    IIR Rule 2: Roll-Off Rate vs. Order Check
    
    Calculate the theoretical maximum attenuation for the given order
    at the stopband frequency using the roll-off approximation:
    Max_Atten ≈ 6 × order × Octaves
    
    Where Octaves = log2((f_cutoff + transition_width) / f_cutoff)
    """
    filter_class = filter_spec.get("filter_class", "").upper()
    
    if filter_class != "IIR":
        return
    
    common = filter_spec.get("common", {})
    order = _coerce_int(filter_spec.get("order"))
    cutoff_freq = _coerce_float(common.get("cutoff_frequency_hz"))
    transition_width = _coerce_float(common.get("transition_width_hz"))
    stopband_atten = _coerce_float(common.get("stopband_attenuation_db"))
    
    if None in [order, cutoff_freq, transition_width, stopband_atten]:
        return
    
    if cutoff_freq <= 0 or transition_width <= 0:
        return
    
    # Calculate octaves between cutoff and stopband
    stopband_freq = cutoff_freq + transition_width
    octaves = math.log2(stopband_freq / cutoff_freq)
    
    # Calculate theoretical maximum attenuation
    max_attenuation = 6.0 * order * octaves
    
    # Flag contradiction if requested exceeds theoretical max by more than 5 dB margin
    if stopband_atten > (max_attenuation + 5.0):
        contradictions.append({
            "rule": "Roll-Off Rate vs. Order",
            "issue": (
                f"Requested stopband attenuation ({stopband_atten} dB) exceeds "
                f"theoretical maximum for order {order}"
            ),
            "details": (
                f"Order: {order}\n"
                f"Cutoff frequency: {cutoff_freq} Hz\n"
                f"Stopband frequency: {stopband_freq} Hz ({octaves:.3f} octaves)\n"
                f"Theoretical max attenuation: {max_attenuation:.1f} dB (6 × {order} × {octaves:.3f})\n"
                f"Requested attenuation: {stopband_atten} dB\n"
                f"Excess: {stopband_atten - max_attenuation:.1f} dB\n\n"
                f"Consider increasing filter order or reducing stopband attenuation requirement."
            )
        })


def _validate_radix_alignment(filter_spec, hardware_spec, contradictions):
    """
    Placeholder for Phase 2 directive.  No operation during Phase 1.

    Radix alignment is enforced later during DESIGN_PLAN generation by
    quantizing both coefficient sets with a common fractional bit width.
    """
    # intentionally empty
    return


def _validate_iir_latency_constraint(filter_spec, contradictions):
    """
    Placeholder for Phase 2 directive. No operation during Phase 1.

    Latency enforcement occurs during DESIGN_PLAN/RTL generation rather than
    in the pre‑generation validation.
    """
    # intentionally empty
    return