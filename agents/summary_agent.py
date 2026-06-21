"""
Summary Agent - Runs RTL frequency response characterization and prints final summary.
Runs as the last node regardless of workflow success or failure.

Characterizes the actual hardware by running a dedicated cocotb testbench that:
  1. Feeds an impulse into the RTL and captures the output (-> FFT for freq response)
  2. Feeds sine waves at multiple frequencies and measures output amplitude (-> sweep)

Both curves are plotted overlaid and saved to output/frequency_response.png.
"""
import re
import os
import csv
import json
import subprocess
import numpy as np
from termcolor import colored
from models.state import AgentState

try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from scipy import signal as sig
    HAS_PLOT_DEPS = True
except ImportError:
    HAS_PLOT_DEPS = False


# ---------------------------------------------------------------------------
# Helpers (kept from original)
# ---------------------------------------------------------------------------

def _parse_coeff_line(plan: str, key: str) -> list:
    """Extract a list of numbers from a PLAN line like 'KEY=[1, 2, 3]'."""
    pattern = rf'^{key}=\[([^\]]*)\]'
    match = re.search(pattern, plan, re.MULTILINE)
    if not match:
        return []
    raw = match.group(1).strip()
    if not raw:
        return []
    try:
        return [float(x.strip()) for x in raw.split(",")]
    except ValueError:
        return []


def _parse_plan_value(plan: str, key: str) -> str:
    """Extract a single value from a PLAN line like 'KEY=value'."""
    pattern = rf'^{key}=(.+)$'
    match = re.search(pattern, plan, re.MULTILINE)
    return match.group(1).strip() if match else ""


def _parse_sim_summary(sim_report: str) -> dict:
    """Extract test result summary from simulation output."""
    info = {"total": 0, "errors": 0, "passed": False}

    total_match = re.search(r'Total samples tested:\s*(\d+)', sim_report)
    if total_match:
        info["total"] = int(total_match.group(1))

    error_match = re.search(r'Total errors:\s*(\d+)', sim_report)
    if error_match:
        info["errors"] = int(error_match.group(1))

    info["passed"] = "PASS" in sim_report.upper() and info["errors"] == 0
    return info


# ---------------------------------------------------------------------------
# Per-run config writer (testbench itself lives in utils/summary_tb.py)
# ---------------------------------------------------------------------------

def _write_summary_config(
    sim_dir: str,
    data_width: int,
    latency: int,
    fs: float,
    n_impulse: int,
    sweep_freqs: list,
    warmup_cycles: int,
    measurement_min_samples: int,
    fold_factor: int = 1,
    taps: int = 1,
    topology: str = "direct_form",
    impulse_amplitude: int = 1,
):
    """Write summary_config.json with per-run parameters for the static testbench."""
    config = {
        "data_width": data_width,
        "latency": latency,
        "fs": fs,
        "n_impulse": n_impulse,
        "sweep_freqs": sweep_freqs,
        "warmup_cycles": warmup_cycles,
        "measurement_min_samples": measurement_min_samples,
        "fold_factor": fold_factor,
        "taps": taps,
        "topology": topology,
        "impulse_amplitude": impulse_amplitude,
    }
    os.makedirs(sim_dir, exist_ok=True)
    config_path = os.path.join(sim_dir, "summary_config.json")
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)


# ---------------------------------------------------------------------------
# Makefile generation
# ---------------------------------------------------------------------------

def _generate_summary_makefile(rtl_file: str, module_name: str) -> str:
    """Return a cocotb Makefile string for the summary testbench.

    The testbench module lives at utils/summary_tb.py (one level up from
    utils/summary_sim/ where make runs).  PYTHONPATH is set so cocotb can
    import it.
    """
    rtl_abs = os.path.abspath(rtl_file)
    sim_abs = os.path.abspath(os.path.join("utils", "summary_sim"))
    rtl_rel = os.path.relpath(rtl_abs, sim_abs).replace("\\", "/")

    return (
        "# Makefile_summary - frequency response characterization\n"
        "SIM ?= icarus\n"
        "TOPLEVEL_LANG ?= verilog\n"
        f"VERILOG_SOURCES += {rtl_rel}\n"
        f"TOPLEVEL = {module_name}\n"
        "COCOTB_TEST_MODULES = summary_tb\n"
        "export PYTHONPATH := $(shell cd .. && pwd):$(PYTHONPATH)\n"
        "include $(shell cocotb-config --makefiles)/Makefile.sim\n"
    )


# ---------------------------------------------------------------------------
# Simulation runner
# ---------------------------------------------------------------------------

def _run_summary_simulation(makefile_content: str):
    """Write Makefile into utils/summary_sim/ and run cocotb via WSL.

    The testbench itself is the static utils/summary_tb.py (not regenerated).
    Per-run parameters are in utils/summary_sim/summary_config.json (already
    written by _write_summary_config before this is called).
    Returns (success, log, sim_dir).
    """
    sim_dir = os.path.join("utils", "summary_sim")
    os.makedirs(sim_dir, exist_ok=True)

    makefile_path = os.path.join(sim_dir, "Makefile_summary")
    with open(makefile_path, "w", encoding="utf-8") as f:
        f.write(makefile_content)

    env = os.environ.copy()
    home_dir = os.path.expanduser("~")
    local_bin = os.path.join(home_dir, ".local", "bin")
    env["PATH"] = local_bin + os.pathsep + env.get("PATH", "")
    env["COCOTB_REDUCED_LOG_FMT"] = "0"
    env["COCOTB_LOG_LEVEL"] = "DEBUG"

    # Convert Windows backslashes to forward slashes for WSL bash
    sim_dir_posix = sim_dir.replace(os.sep, "/")

    try:
        result = subprocess.run(
            ["wsl", "bash", "-lc",
             f"cd {sim_dir_posix} && rm -rf sim_build && make -f Makefile_summary"],
            capture_output=True,
            text=True,
            timeout=180,
            env=env,
        )
        full_output = result.stdout + "\n" + result.stderr
        success = ("TEST PASSED" in full_output
                   or "TESTS=1 PASS=1 FAIL=0" in full_output)
        return success, full_output, sim_dir
    except subprocess.TimeoutExpired:
        return False, "Summary simulation timed out (180 s)", sim_dir
    except FileNotFoundError:
        return False, "WSL not available on this system", sim_dir
    except Exception as e:
        return False, f"Simulation error: {e}", sim_dir


# ---------------------------------------------------------------------------
# CSV parsers
# ---------------------------------------------------------------------------

def _parse_impulse_csv(filepath: str) -> list:
    """Read impulse_data.csv → list of output sample integers."""
    samples = []
    try:
        with open(filepath, "r") as f:
            reader = csv.reader(f)
            next(reader)  # skip header
            for row in reader:
                if len(row) >= 2:
                    samples.append(int(row[1]))
    except (FileNotFoundError, ValueError, StopIteration):
        pass
    return samples


def _parse_sweep_csv(filepath: str):
    """Read sweep_data.csv → (freqs, rms_inputs, rms_outputs) as float lists."""
    freqs, rms_ins, rms_outs = [], [], []
    try:
        with open(filepath, "r") as f:
            reader = csv.reader(f)
            next(reader)  # skip header
            for row in reader:
                if len(row) >= 3:
                    freqs.append(float(row[0]))
                    rms_ins.append(float(row[1]))
                    rms_outs.append(float(row[2]))
    except (FileNotFoundError, ValueError, StopIteration):
        pass
    return freqs, rms_ins, rms_outs


# ---------------------------------------------------------------------------
# Plotting
# ---------------------------------------------------------------------------

def _plot_rtl_frequency_response(
    impulse_samples: list,
    impulse_amplitude: int,
    sweep_freqs: list,
    sweep_rms_in: list,
    sweep_rms_out: list,
    fs: float,
    fc,
    filter_type: str,
    output_path: str,
    fwd_float: list | None = None,
    fbk_float: list | None = None,
):
    """Plot ideal, impulse-FFT, and sine-sweep curves overlaid, save as PNG."""
    fig, ax = plt.subplots(1, 1, figsize=(10, 6))
    fig.suptitle("Filter Frequency Response Analysis",
                 fontsize=14, fontweight="bold")

    # --- Curve 0: Ideal (theoretical from float coefficients) ---
    if fwd_float:
        b = np.array(fwd_float)
        a = np.array(fbk_float) if fbk_float else np.array([1.0])
        w_ideal, h_ideal = sig.freqz(b, a, worN=1024, fs=fs)
        mag_ideal = 20 * np.log10(np.abs(h_ideal) + 1e-12)
        ax.plot(w_ideal, mag_ideal, color="gray", linestyle="-", linewidth=2.0,
                label="Ideal (float coefficients)", alpha=0.5)

    # --- Curve 1: Impulse response FFT ---
    if impulse_samples:
        N = len(impulse_samples)
        h = np.array(impulse_samples, dtype=float) / impulse_amplitude
        H = np.fft.rfft(h, n=N)
        freqs_fft = np.fft.rfftfreq(N, d=1.0 / fs)
        mag_fft = 20 * np.log10(np.abs(H) + 1e-12)
        ax.plot(freqs_fft, mag_fft, "b-", linewidth=1.5,
                label="Impulse Response (FFT)", alpha=0.85)

    # --- Curve 2: Sine sweep ---
    if sweep_freqs and sweep_rms_out:
        gains_db = []
        for rms_in, rms_out in zip(sweep_rms_in, sweep_rms_out):
            gain = rms_out / (rms_in + 1e-12)
            gains_db.append(20 * np.log10(gain + 1e-12))
        ax.plot(sweep_freqs, gains_db, "ro-", markersize=3, linewidth=1.0,
                label="Sine Sweep (RMS)", alpha=0.85)

    # --- Spec markers ---
    if fc is not None:
        if isinstance(fc, (list, tuple)):
            for f in fc:
                ax.axvline(x=f, color="green", linestyle="-.", alpha=0.7,
                           label=f"Fc={f} Hz")
        else:
            ax.axvline(x=fc, color="green", linestyle="-.", alpha=0.7,
                       label=f"Fc={fc} Hz")

    ax.axhline(y=-3, color="gray", linestyle=":", alpha=0.5, label="-3 dB")

    ax.set_xlabel("Frequency (Hz)")
    ax.set_ylabel("Magnitude (dB)")
    ax.set_ylim(bottom=-80)
    ax.legend(loc="lower left", fontsize=8)
    ax.grid(True, alpha=0.3)
    ax.set_title(f"{filter_type.capitalize()} Filter — Hardware Frequency Response")

    plt.tight_layout()
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    fig.savefig(output_path, dpi=150)
    plt.close(fig)


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def _run_frequency_characterization(
    module_name: str,
    data_width: int,
    latency: int,
    taps: int,
    fs: float,
    fc,
    filter_type: str,
    rtl_file: str,
    output_dir: str,
    plot_path: str,
    is_iir: bool,
    fwd_float: list | None = None,
    fbk_float: list | None = None,
    topology: str = "direct_form",
) -> bool:
    """Generate testbench, run sim, parse CSV, plot. Returns True on success."""

    # Fold factor
    if topology == "folded":
        fold_factor = taps
    else:
        fold_factor = 1

    impulse_amplitude = 2 ** (data_width - 2)

    # Impulse sample count in OUTPUT SAMPLES (power-of-2 for clean FFT)
    if is_iir:
        n_impulse = 512
    else:
        n_raw = taps + latency + 16
        n_impulse = max(256, 1 << (n_raw - 1).bit_length())

    # Sweep frequencies: 64 points linearly spaced in (0, Fs/2)
    n_sweep = 64
    sweep_freqs = [
        round((fs / 2.0) * (i + 1) / (n_sweep + 1), 2)
        for i in range(n_sweep)
    ]

    # Warmup: in output samples, then scaled to clock cycles
    warmup_samples = max(taps, latency) + latency
    if is_iir:
        warmup_samples = max(64, warmup_samples)
    warmup = warmup_samples * fold_factor

    meas_min = 32

    # 1. Write per-run config JSON with ALL parameters
    sim_dir = os.path.join("utils", "summary_sim")
    _write_summary_config(
        sim_dir=sim_dir,
        data_width=data_width,
        latency=latency,
        fs=fs,
        n_impulse=n_impulse,
        sweep_freqs=sweep_freqs,
        warmup_cycles=warmup,
        measurement_min_samples=meas_min,
        fold_factor=fold_factor,
        taps=taps,
        topology=topology,
        impulse_amplitude=impulse_amplitude,
    )

    # 2. Generate Makefile
    makefile_content = _generate_summary_makefile(rtl_file, module_name)

    # 3. Run simulation
    print(colored("  Running RTL frequency characterization...", "cyan"))
    success, log_output, sim_dir = _run_summary_simulation(makefile_content)

    if not success:
        print(colored("  Characterization simulation failed.", "yellow"))
        tail = log_output[-500:] if len(log_output) > 500 else log_output
        for line in tail.strip().splitlines()[-6:]:
            print(colored(f"    {line}", "yellow"))
        return False

    # 4. Parse CSV results
    impulse_csv = os.path.join(sim_dir, "impulse_data.csv")
    sweep_csv = os.path.join(sim_dir, "sweep_data.csv")

    impulse_samples = _parse_impulse_csv(impulse_csv)
    sweep_freq_list, sweep_rms_in, sweep_rms_out = _parse_sweep_csv(sweep_csv)

    if not impulse_samples and not sweep_freq_list:
        print(colored("  No data captured — cannot plot.", "yellow"))
        return False

    print(colored(f"  Impulse: {len(impulse_samples)} samples captured", "cyan"))
    print(colored(f"  Sweep:   {len(sweep_freq_list)} frequencies measured", "cyan"))

    # 5. Plot
    _plot_rtl_frequency_response(
        impulse_samples=impulse_samples,
        impulse_amplitude=impulse_amplitude,
        sweep_freqs=sweep_freq_list,
        sweep_rms_in=sweep_rms_in,
        sweep_rms_out=sweep_rms_out,
        fs=fs,
        fc=fc,
        filter_type=filter_type,
        output_path=plot_path,
        fwd_float=fwd_float,
        fbk_float=fbk_float,
    )

    print(colored(f"\n  Frequency response plot saved to: {plot_path}",
                  "green", attrs=["bold"]))
    return True


# ---------------------------------------------------------------------------
# Graph node
# ---------------------------------------------------------------------------

def summary_node(state: AgentState):
    """Final workflow node — characterizes RTL frequency response and prints summary."""
    print(colored("\n" + "=" * 60, "magenta"))
    print(colored("  SUMMARY & FREQUENCY RESPONSE ANALYSIS", "magenta", attrs=["bold"]))
    print(colored("=" * 60, "magenta"))

    status = state.get("status", "unknown")
    design_plan = state.get("design_plan", "")
    sim_report = state.get("sim_report", "")
    specs = state.get("specs", {})

    # --- Extract parameters from design plan ---
    filter_class = _parse_plan_value(design_plan, "FILTER_CLASS") or "FIR"
    filter_type = _parse_plan_value(design_plan, "FILTER_TYPE") or "lowpass"
    is_iir = filter_class.upper() == "IIR"

    module_name = _parse_plan_value(design_plan, "MODULE") or "filter"
    data_width_str = _parse_plan_value(design_plan, "DATA_WIDTH")
    latency_str = _parse_plan_value(design_plan, "LATENCY")
    taps_str = _parse_plan_value(design_plan, "TAPS")
    topology = _parse_plan_value(design_plan, "TOPOLOGY") or "direct_form"
    fold_factor_str = _parse_plan_value(design_plan, "FOLD_FACTOR")
    fold_factor = int(fold_factor_str) if fold_factor_str else 1

    data_width = int(data_width_str) if data_width_str else 16
    latency = int(latency_str) if latency_str else 1
    taps = int(taps_str) if taps_str else 8

    # --- Get Fs and Fc from specs ---
    filter_spec = specs.get("filter_design_specification", {})
    common = filter_spec.get("common", {})
    fs = common.get("sampling_frequency_hz") or filter_spec.get("sampling_frequency_hz")
    fc = common.get("cutoff_frequency_hz") or filter_spec.get("cutoff_frequency_hz")

    if isinstance(fs, (list, tuple)) and len(fs) == 0:
        fs = None
    if isinstance(fc, (list, tuple)) and len(fc) == 0:
        fc = None

    # --- Extract float coefficients for ideal curve ---
    if is_iir:
        fwd_float = _parse_coeff_line(design_plan, "FORWARD_COEFF_FLOAT")
        fbk_float = _parse_coeff_line(design_plan, "FEEDBACK_COEFF_FLOAT")
    else:
        fwd_float = _parse_coeff_line(design_plan, "COEFF_FLOAT")
        fbk_float = [1.0]

    # --- Print text summary ---
    print(colored(f"\n  Filter: {filter_class} {filter_type}", "magenta"))
    print(colored(f"  Module: {module_name}", "magenta"))
    print(colored(f"  Taps: {taps}  Data width: {data_width}  Latency: {latency}", "magenta"))
    if fs:
        print(colored(f"  Fs: {fs} Hz", "magenta"))
    if fc:
        print(colored(f"  Fc: {fc} Hz", "magenta"))
    print(colored(f"  Workflow status: {status}", "magenta"))

    if sim_report:
        sim_info = _parse_sim_summary(sim_report)
        if sim_info["total"] > 0:
            print(colored(f"  Sim samples: {sim_info['total']}, errors: {sim_info['errors']}", "magenta"))

    # --- RTL frequency response characterization ---
    rtl_file = specs.get("project_settings", {}).get("output_file", "")
    output_dir = os.path.dirname(rtl_file) or "output"
    plot_path = os.path.join(output_dir, "frequency_response.png")

    if not HAS_PLOT_DEPS:
        print(colored("  Skipping plot — matplotlib/numpy not installed.", "yellow"))
    elif not fs:
        print(colored("  Skipping plot — sampling frequency unknown.", "yellow"))
    elif not rtl_file or not os.path.exists(rtl_file):
        print(colored("  Skipping plot — RTL file not found.", "yellow"))
    else:
        try:
            _run_frequency_characterization(
                module_name=module_name,
                data_width=data_width,
                latency=latency,
                taps=taps,
                fs=float(fs),
                fc=fc,
                filter_type=filter_type,
                rtl_file=rtl_file,
                output_dir=output_dir,
                plot_path=plot_path,
                is_iir=is_iir,
                fwd_float=fwd_float or None,
                fbk_float=fbk_float or None,
                topology=topology,
            )
        except Exception as e:
            print(colored(f"  Frequency characterization failed: {e}", "red"))

    print(colored("=" * 60 + "\n", "magenta"))
    return {}
