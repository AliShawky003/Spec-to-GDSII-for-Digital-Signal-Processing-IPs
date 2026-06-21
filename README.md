# Spec-to-GDSII of DSP Filters

This repository implements an AI-assisted Spec-to-GDSII flow for digital signal processing filters. It starts from JSON specifications, derives a hardware architecture plan, generates synthesizable RTL, verifies the design in simulation, and then hands the result to the ASIC flow for LibreLane/OpenLane-style implementation.

## What The Project Does

- Reads filter and verification specifications from `filter_spec.json` and `verification_spec.json`.
- Builds a deterministic design plan and verification plan through the architect agent.
- Generates SystemVerilog RTL for FIR and IIR DSP filters.
- Runs linting, simulation, debug, and repair loops through a LangGraph workflow.
- Collects and reuses past errors through an error-memory retrieval system.
- Launches the ASIC handoff flow after successful verification.

## Architecture Overview

The main orchestration is in `verif_workflow.py`. The flow is:

1. `architect` creates the design and verification plans.
2. `designer` generates RTL from the design plan.
3. `linter` checks the RTL with Verilator.
4. `rtl_fixer` repairs compile or lint issues when needed.
5. `generate_tb` creates the testbench.
6. `tb_fixer` repairs the testbench when needed.
7. `simulator` runs SystemVerilog simulation.
8. `debug` analyzes failures and routes the next action.
9. `summary` characterizes the RTL frequency response.
10. `asic` prepares the LibreLane flow and runs the ASIC stage.

## Verification Policy

SystemVerilog is the active verification path in this workspace. Cocotb is retained only for the summary frequency-response characterization testbench in `utils/summary_tb.py`.

## Error Handling And Learning

The workflow stores recurring failures in `error_memory/seed` and `error_memory/harvested`. Compiler and simulation errors are normalized, retrieved with BM25 search, and injected into fixer prompts so the agents can reuse prior successful repairs.

## RTL Topologies

The generated RTL covers several DSP hardware styles:

- Direct-form FIR
- Symmetric pre-adder FIR
- Transposed FIR
- Folded FIR
- IIR biquad DF2T
- IIR biquad DF1

## ASIC Handoff

After verification, the ASIC agent selects a synthesis strategy from the `runs/` summaries, generates or reuses `pnr.sdc` and `signoff.sdc`, updates the LibreLane config, and launches the full flow in WSL.

## Repository Layout

- `agents/` - orchestration, generation, repair, simulation, and ASIC nodes
- `config/` - prompt templates and runtime settings
- `utils/` - coefficient design, error memory, code cleanup, and summary testbench helpers
- `output/` - generated RTL, TB, reports, and plots
- `runs/` - ASIC and synthesis exploration artifacts
- `dataset/` - area and design data used by the architect
- `error_memory/` - seed and harvested bug examples

## Usage Notes

- Provide valid API credentials in the environment before running the workflow.
- Keep `filter_spec.json` and `verification_spec.json` aligned with the intended filter and verification setup.
- Generated files are written into `output/` and related run folders.
