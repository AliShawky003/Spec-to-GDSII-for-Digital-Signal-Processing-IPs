"""
Reusable cocotb testbench for RTL frequency response characterization.

Performs two measurements on the DUT:
  Phase 1 — Impulse response: feeds a single impulse, captures output → CSV
  Phase 2 — Sine sweep: feeds sine waves at multiple frequencies, measures RMS → CSV

Parameters are read from utils/summary_sim/summary_config.json, which is
written by summary_agent.py before each run.

Supports folded architectures via fold_factor from config:
  - sample_valid pulses for 1 clock, then stays low for fold_factor-1 clocks
  - result_valid is only checked (not assumed every clock)
  - sine samples are generated at the effective sample rate (fs), not clock rate
"""
import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge
import math
import csv
import json
import os

# ---------- Load per-run parameters from config ----------
_cfg_path = os.path.join(os.path.dirname(__file__), "summary_sim", "summary_config.json")
with open(_cfg_path, "r") as _f:
    _CFG = json.load(_f)

DATA_WIDTH  = _CFG["data_width"]
LATENCY     = _CFG["latency"]
FS          = _CFG["fs"]
N_IMPULSE   = _CFG["n_impulse"]
SWEEP_FREQS = _CFG["sweep_freqs"]
WARMUP      = _CFG["warmup_cycles"]
MEAS_MIN    = _CFG["measurement_min_samples"]
FOLD_FACTOR = _CFG.get("fold_factor", 1)
TAPS        = _CFG.get("taps", 1)
TOPOLOGY    = _CFG.get("topology", "direct_form")
IMPULSE_AMP = _CFG.get("impulse_amplitude", 2 ** (DATA_WIDTH - 2))

IS_FOLDED   = (TOPOLOGY == "folded" and FOLD_FACTOR > 1)


async def reset_dut(dut):
    """Assert active-low reset for 2 cycles, then release."""
    dut.rst_n.value = 0
    dut.sample_in.value = 0
    dut.sample_valid.value = 0
    for _ in range(2):
        await RisingEdge(dut.clk)
    dut.rst_n.value = 1
    await RisingEdge(dut.clk)


def encode_signed(value, width):
    """Clamp to signed range and return unsigned 2s-complement representation."""
    max_val = (1 << (width - 1)) - 1
    min_val = -(1 << (width - 1))
    value = max(min_val, min(max_val, int(round(value))))
    if value < 0:
        value += (1 << width)
    return value


async def drive_sample(dut, value, outputs_list):
    """Drive one sample into the DUT and wait fold_factor clocks.

    For folded architectures:
      - Clock 0: assert sample_valid=1 with sample_in=value
      - Clocks 1 to fold_factor-1: deassert sample_valid, check result_valid

    For non-folded architectures (fold_factor=1):
      - Clock 0: assert sample_valid=1 with sample_in=value
      - Check result_valid on this edge

    Any outputs captured are appended to outputs_list.
    """
    # Drive the sample
    dut.sample_in.value = encode_signed(value, DATA_WIDTH)
    dut.sample_valid.value = 1
    await RisingEdge(dut.clk)

    # Check for output on this edge
    if dut.result_valid.value == 1:
        outputs_list.append(dut.result_out.value.signed_integer)

    # For folded: wait remaining fold_factor-1 clocks with sample_valid=0
    if IS_FOLDED:
        dut.sample_valid.value = 0
        dut.sample_in.value = 0
        for _ in range(FOLD_FACTOR - 1):
            await RisingEdge(dut.clk)
            if dut.result_valid.value == 1:
                outputs_list.append(dut.result_out.value.signed_integer)
    else:
        # Non-folded: sample_valid stays high (will be set again next call)
        pass


async def drain_outputs(dut, outputs_list, n_clocks):
    """Stop driving inputs, capture any remaining outputs for n_clocks."""
    dut.sample_valid.value = 0
    dut.sample_in.value = 0
    for _ in range(n_clocks):
        await RisingEdge(dut.clk)
        if dut.result_valid.value == 1:
            outputs_list.append(dut.result_out.value.signed_integer)


@cocotb.test()
async def test_frequency_response(dut):
    """Impulse response + sine sweep for hardware frequency characterization."""
    clock = Clock(dut.clk, 10, unit="ns")
    cocotb.start_soon(clock.start())

    dut._log.info(f"Config: DATA_WIDTH={DATA_WIDTH} LATENCY={LATENCY} "
                  f"FOLD_FACTOR={FOLD_FACTOR} TAPS={TAPS} TOPOLOGY={TOPOLOGY} "
                  f"IMPULSE_AMP={IMPULSE_AMP} FS={FS}")

    # ======================== PHASE 1: IMPULSE ========================
    dut._log.info("=== Phase 1: Impulse Response ===")
    await reset_dut(dut)

    impulse_outputs = []

    # Drive impulse as first sample
    await drive_sample(dut, IMPULSE_AMP, impulse_outputs)

    # Drive N_IMPULSE-1 zero samples to propagate impulse through shift register
    for _ in range(N_IMPULSE - 1):
        await drive_sample(dut, 0, impulse_outputs)

    # Drain: wait for any remaining pipeline outputs
    drain_clocks = (LATENCY + 10) * (FOLD_FACTOR if IS_FOLDED else 1)
    await drain_outputs(dut, impulse_outputs, drain_clocks)

    # Write impulse CSV
    with open("impulse_data.csv", "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["sample_index", "output_value"])
        for i, val in enumerate(impulse_outputs):
            writer.writerow([i, val])

    dut._log.info(f"Impulse: captured {len(impulse_outputs)} output samples")

    # ======================== PHASE 2: SINE SWEEP ========================
    dut._log.info("=== Phase 2: Sine Sweep ===")
    sweep_results = []

    for freq_idx, freq in enumerate(SWEEP_FREQS):
        if freq <= 0:
            continue

        await reset_dut(dut)

        # Calculate measurement length in OUTPUT SAMPLES
        period_samples = FS / freq
        n_meas_samples = max(MEAS_MIN, int(math.ceil(2.0 * period_samples)))

        # Total warmup is already in clock cycles from summary_agent
        # Convert to output samples for our drive loop
        if IS_FOLDED:
            warmup_output_samples = WARMUP // FOLD_FACTOR
        else:
            warmup_output_samples = WARMUP

        warmup_output_samples = max(warmup_output_samples, TAPS + LATENCY)

        # --- Warmup: drive sine at sample rate, ignore outputs ---
        warmup_outputs = []
        for k in range(warmup_output_samples):
            val = IMPULSE_AMP * math.sin(2.0 * math.pi * freq * k / FS)
            await drive_sample(dut, val, warmup_outputs)

        # --- Measurement: drive sine at sample rate, record outputs ---
        input_sq_sum = 0.0
        output_sq_sum = 0.0
        output_count = 0
        meas_outputs = []

        for k in range(n_meas_samples):
            t_sample = warmup_output_samples + k
            val = IMPULSE_AMP * math.sin(2.0 * math.pi * freq * t_sample / FS)
            input_sq_sum += val * val
            await drive_sample(dut, val, meas_outputs)

        # Drain remaining outputs from measurement phase
        drain_clocks = (LATENCY + 10) * (FOLD_FACTOR if IS_FOLDED else 1)
        await drain_outputs(dut, meas_outputs, drain_clocks)

        # Compute RMS from captured outputs
        for raw in meas_outputs:
            output_sq_sum += float(raw) * float(raw)
            output_count += 1

        rms_in = math.sqrt(input_sq_sum / n_meas_samples) if n_meas_samples > 0 else 1e-12
        rms_out = math.sqrt(output_sq_sum / output_count) if output_count > 0 else 0.0
        sweep_results.append((freq, rms_in, rms_out))

        if (freq_idx + 1) % 10 == 0:
            dut._log.info(f"  Sweep progress: {freq_idx + 1}/{len(SWEEP_FREQS)} frequencies")

    # Write sweep CSV
    with open("sweep_data.csv", "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["frequency_hz", "rms_input", "rms_output"])
        for freq, rms_in, rms_out in sweep_results:
            writer.writerow([freq, rms_in, rms_out])

    dut._log.info(f"Sweep: measured {len(sweep_results)} frequencies")
    dut._log.info("TEST PASSED")
