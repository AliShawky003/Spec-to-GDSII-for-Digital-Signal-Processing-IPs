"""
State definition for the LangGraph workflow
"""
from typing import TypedDict

class AgentState(TypedDict, total=False):
    """
    The state object passed between nodes in the graph.
    Fields are optional (total=False) so nodes can selectively update.
    """
    project_root: str            # Absolute project root path for file-based agents
    specs: dict                  # The JSON input specs for RTL
    verif_specs: dict            # The JSON input specs for Verification
    design_plan: str             # The Architect's strategy
    verif_plan: str              # The Architect's verification plan
    verilog_code: str            # The generated RTL
    tb_code: str                 # The generated Testbench (SV or Python for cocotb)
    makefile: str                # Makefile for cocotb (optional)
    error_report: str            # Output from Verilator (RTL) or Iverilog (TB)
    sim_report: str              # Output from Simulation execution
    attempt_count: int           # RTL generation retries
    tb_attempt_count: int        # TB generation retries
    rtl_fix_count: int           # RTL fixer retries
    tb_fix_count: int            # TB fixer retries
    status: str                  # "processing", "success", "failed", "rtl_verified", "tb_error", "sim_failed", "rtl_compile_error"

    # Pre-fix snapshots used by the error-memory harvest hook.
    # Fixer nodes populate these before calling the LLM so that, after a
    # successful downstream check, the workflow can pair (error, before, after)
    # and record the fix.
    pre_fix_rtl_code: str        # RTL snapshot taken just before rtl_fixer runs
    pre_fix_rtl_error: str       # Raw linter error that triggered the RTL fix
    pre_fix_tb_code: str         # TB snapshot taken just before tb_fixer runs
    pre_fix_tb_error: str        # Raw TB/compile error that triggered the TB fix
    # ASIC flow outputs
    asic_status: str             # ASIC stage status
    asic_report: str             # ASIC flow combined log/output
    asic_return_code: int        # LibreLane process return code
    selected_strategy: dict      # Selected synthesis strategy summary
    pnr_sdc_path: str            # Resolved PnR SDC path
    signoff_sdc_path: str        # Resolved signoff SDC path
    full_flow_run_tag: str       # Full ASIC flow run tag
    min_required_clock_period_ns: float   # Estimated min clock after synthesis exploration
    max_achievable_frequency_mhz: float   # Estimated max frequency after synthesis exploration
    timing_limit_strategy: str   # Strategy used for min clock estimate

