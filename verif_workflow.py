import json
import re
import datetime
from langgraph.graph import StateGraph, END
from termcolor import colored
import os

# Import configuration
from config.settings import API_KEY, MAX_RTL_ATTEMPTS, MODEL_NAME

# Import state model
from models.state import AgentState

# Import agents
from agents.architect_agent import architect_node
from agents.designer_agent import generate_rtl_node
from agents.linter_agent import lint_node
from agents.rtl_fixer_agent import rtl_fixer_node
from agents.tb_generator_agent import generate_tb_node
from agents.tb_fixer_agent import tb_fixer_node
from agents.simulator_agent import simulation_node
from agents.debug_agent import debug_node
from agents.summary_agent import summary_node
from agents.asic_agent import asic_node

# Error-memory harvest
from utils.error_retriever import save_harvested_error
from utils.api_utils import call_llm_with_retry


# ===== ERROR MEMORY HARVEST =====
#
# When a fix succeeds, we pair (pre_fix_error, pre_fix_code, post_fix_code)
# into a new harvested entry in error_memory/harvested/. An LLM call
# summarizes the fix; if the LLM call fails, we fall back to a mechanical
# line-diff so the entry is still useful.

_HARVEST_SYSTEM_PROMPT = (
    "You analyze hardware-design fix attempts and produce concise rules for "
    "a fix-memory database. Output MUST follow this exact format with no extra "
    "text:\n"
    "ROOT_CAUSE: <one sentence explaining the underlying bug>\n"
    "FIX_RULE: <one-to-two imperative sentences describing how to fix it>\n"
    "FIX_SNIPPET:\n"
    "<up to 15 lines of code showing the correction, no markdown fences>\n"
)

_HARVEST_USER_TEMPLATE = """An earlier {kind} code had this error:

ERROR:
{error_text}

BEFORE (broken):
{before_code}

AFTER (working):
{after_code}

Summarize the fix for a reusable rule database. Follow the format strictly.
"""


def _line_diff(before: str, after: str, max_lines: int = 15) -> str:
    """Mechanical fallback: return up to max_lines that are in `after`
    but not in `before` (a crude 'what changed' view)."""
    if not after:
        return ""
    before_set = set(l.strip() for l in before.splitlines() if l.strip())
    added = []
    for line in after.splitlines():
        stripped = line.strip()
        if stripped and stripped not in before_set:
            added.append(line)
            if len(added) >= max_lines:
                break
    return "\n".join(added)


def _parse_harvest_response(text: str) -> tuple:
    """Parse ROOT_CAUSE / FIX_RULE / FIX_SNIPPET from LLM output."""
    root_cause = ""
    fix_rule = ""
    fix_snippet = ""
    rc_match = re.search(r"ROOT_CAUSE:\s*(.+?)(?=\n(?:FIX_RULE|FIX_SNIPPET)|\Z)",
                         text, re.DOTALL)
    if rc_match:
        root_cause = rc_match.group(1).strip()
    fr_match = re.search(r"FIX_RULE:\s*(.+?)(?=\n(?:FIX_SNIPPET|ROOT_CAUSE)|\Z)",
                         text, re.DOTALL)
    if fr_match:
        fix_rule = fr_match.group(1).strip()
    fs_match = re.search(r"FIX_SNIPPET:\s*\n?(.+)\Z", text, re.DOTALL)
    if fs_match:
        fix_snippet = fs_match.group(1).strip()
    return root_cause, fix_rule, fix_snippet


def _summarize_fix_with_llm(
    kind: str,
    error_text: str,
    before_code: str,
    after_code: str,
) -> tuple:
    """Ask the LLM to summarize a successful fix. Returns
    (root_cause, fix_rule, fix_snippet). Falls back to a line-diff snippet
    on failure."""
    if not API_KEY:
        return "", "", _line_diff(before_code, after_code)

    # Truncate inputs to keep the harvest call cheap
    user_prompt = _HARVEST_USER_TEMPLATE.format(
        kind=kind,
        error_text=(error_text or "")[:1500],
        before_code=(before_code or "")[:2500],
        after_code=(after_code or "")[:2500],
    )
    messages = [
        {"role": "system", "content": _HARVEST_SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]
    try:
        resp = call_llm_with_retry(model=MODEL_NAME, messages=messages, api_key=API_KEY)
        text = resp.choices[0].message.content or ""
        root_cause, fix_rule, fix_snippet = _parse_harvest_response(text)
        if not fix_snippet:
            fix_snippet = _line_diff(before_code, after_code)
        return root_cause, fix_rule, fix_snippet
    except Exception as e:
        print(colored(f"[Harvest] LLM summarization failed: {e}", "yellow"))
        return "", "", _line_diff(before_code, after_code)


def _plan_field(plan: str, key: str) -> str:
    if not plan:
        return ""
    m = re.search(rf'^{key}=(.+)$', plan, re.MULTILINE)
    return m.group(1).strip() if m else ""


def _harvest_rtl_fix(state: AgentState) -> None:
    """Called after the linter passes following an RTL fix."""
    pre_code  = state.get("pre_fix_rtl_code",  "") or ""
    pre_error = state.get("pre_fix_rtl_error", "") or ""
    if not pre_error or not pre_code:
        return
    post_code = state.get("verilog_code", "") or ""
    if not post_code or post_code == pre_code:
        return

    plan = state.get("design_plan", "")
    topology     = _plan_field(plan, "TOPOLOGY") or "any"
    filter_class = _plan_field(plan, "FILTER_CLASS") or "any"

    root_cause, fix_rule, fix_snippet = _summarize_fix_with_llm(
        kind="RTL",
        error_text=pre_error,
        before_code=pre_code,
        after_code=post_code,
    )

    saved = save_harvested_error(
        raw_error=pre_error,
        source="linter",
        topology=topology,
        filter_class=filter_class,
        root_cause=root_cause,
        fix_rule=fix_rule,
        fix_snippet=fix_snippet,
        tags=["harvested", "rtl_fix"],
        date=datetime.date.today().isoformat(),
    )
    if saved:
        print(colored(f"[Harvest] New RTL error entry saved: {os.path.basename(saved)}", "green"))
    else:
        print(colored("[Harvest] RTL fix already in memory (skipped).", "cyan"))

    # Clear the snapshot so we don't re-harvest on subsequent successful lints
    state["pre_fix_rtl_code"]  = ""
    state["pre_fix_rtl_error"] = ""


def _harvest_tb_fix(state: AgentState) -> None:
    """Called after simulation passes following a TB fix."""
    pre_code  = state.get("pre_fix_tb_code",  "") or ""
    pre_error = state.get("pre_fix_tb_error", "") or ""
    if not pre_error or not pre_code:
        return
    post_code = state.get("tb_code", "") or ""
    if not post_code or post_code == pre_code:
        return

    plan = state.get("design_plan", "")
    topology     = _plan_field(plan, "TOPOLOGY") or "any"
    filter_class = _plan_field(plan, "FILTER_CLASS") or "any"

    # Source is 'linter' if the TB was fixing a compile error, 'simulator'
    # if it was fixing a sim-time error. Heuristic: compile errors show
    # "syntax error" / "error:" in the first few lines.
    src = "linter" if re.search(r"(syntax error|error:)", pre_error.lower()) else "simulator"

    root_cause, fix_rule, fix_snippet = _summarize_fix_with_llm(
        kind="testbench",
        error_text=pre_error,
        before_code=pre_code,
        after_code=post_code,
    )

    saved = save_harvested_error(
        raw_error=pre_error,
        source=src,
        topology=topology,
        filter_class=filter_class,
        root_cause=root_cause,
        fix_rule=fix_rule,
        fix_snippet=fix_snippet,
        tags=["harvested", "tb_fix"],
        date=datetime.date.today().isoformat(),
    )
    if saved:
        print(colored(f"[Harvest] New TB error entry saved: {os.path.basename(saved)}", "green"))
    else:
        print(colored("[Harvest] TB fix already in memory (skipped).", "cyan"))

    state["pre_fix_tb_code"]  = ""
    state["pre_fix_tb_error"] = ""


# ===== CONDITIONAL EDGE FUNCTIONS =====

def decide_after_lint(state: AgentState) -> str:
    """Decide whether to proceed to TB generation, fix RTL, or retry RTL"""
    status = state.get("status", "")
    if status == "rtl_verified":
        # An RTL fix just succeeded — harvest the (error, before, after) tuple
        # into error_memory/harvested/ before moving on.
        _harvest_rtl_fix(state)
        return "generate_tb"
    elif status in ["processing", "rtl_compile_error"]:
        # Try fixing first before regenerating
        return "fix_rtl"
    elif status == "max_attempts_reached":
        return "end"  # Stop if max RTL attempts reached
    else:
        return "end"

def decide_after_rtl_fix(state: AgentState) -> str:
    """Decide what to do after RTL fixer attempts"""
    status = state.get("status", "")
    if status == "rtl_fixed":
        # Re-lint the fixed code
        return "lint"
    elif status == "rtl_needs_regen":
        # Max fix attempts reached, regenerate RTL
        return "regenerate_rtl"
    else:
        return "end"


def decide_after_tb(state: AgentState) -> str:
    """Decide whether to simulate or fix TB"""
    status = state.get("status", "")
    if status == "tb_generated":
        return "simulate"
    elif status == "tb_error":
        # Try fixing TB before regenerating
        return "fix_tb"
    else:
        return "end"

def decide_after_tb_fix(state: AgentState) -> str:
    """Decide what to do after TB fixer attempts"""
    status = state.get("status", "")
    if status == "tb_fixed":
        # Re-run simulation with fixed TB
        return "simulate"
    elif status == "tb_needs_regen":
        # Max fix attempts reached, regenerate TB
        return "regenerate_tb"
    else:
        return "end"

def decide_after_sim(state: AgentState) -> str:
    """Decide whether to debug/fix or pass simulation"""
    status = state.get("status", "")
    attempt_count = state.get("attempt_count", 0)

    if status == "success":
        # Simulation just passed — if a TB fix was pending, harvest it.
        # (No-op when pre_fix_tb_* are empty.)
        _harvest_tb_fix(state)
        return "summary"
    elif status in ["rtl_sim_error", "sim_failed", "tb_error"]:
        # Run debugger to diagnose the issue
        return "debug"
    elif status == "rtl_compile_error":
        # Compile error indicates RTL needs regeneration/fix
        return "designer"
    elif status == "max_attempts_reached":
        return "end"
    else:
        return "end"

def decide_after_debug(state: AgentState) -> str:
    """Decide which node to visit after debugging.

    After the debug agent automatically attempts to fix the problem, it will
    update the `status` to one of:
        - "rtl_fixed"  : corrected RTL is available
        - "tb_fixed"   : corrected testbench is available
        - "rtl_sim_error", "tb_error" : routing fallback
    """
    status = state.get("status", "")
    recommended_action = state.get("recommended_action", "tb")
    attempt_count = state.get("attempt_count", 0)

    print(colored(f"[Workflow] decide_after_debug called. status={status}, recommended_action={recommended_action}", "magenta"))

    if status == "rtl_fixed":
        print(colored("[Workflow] RTL fixed by debugger; send to linter.", "magenta"))
        return "linter"
    if status == "tb_fixed":
        print(colored("[Workflow] TB fixed by debugger; rerun simulation.", "magenta"))
        return "simulate"

    # fallback to previous behaviour if no automatic fix
    if recommended_action == "rtl":
        if attempt_count >= 3:  # MAX_RTL_ATTEMPTS
            print(colored("[Workflow] Max RTL attempts reached, ending.", "magenta"))
            return "end"
        print(colored("[Workflow] Routing to RTL fixer.", "magenta"))
        return "fix_rtl"
    elif recommended_action == "tb":
        print(colored("[Workflow] Routing to TB fixer.", "magenta"))
        return "fix_tb"
    else:
        print(colored("[Workflow] Unknown recommended_action -> END.", "magenta"))
        return "end"



# ===== WORKFLOW CREATION =====

def create_workflow():
    """
    Create and configure the LangGraph workflow with fixer agents.

    Returns:
        Compiled StateGraph ready for execution
    """
    graph = StateGraph(AgentState)

    # Add nodes
    graph.add_node("architect", architect_node)
    graph.add_node("designer", generate_rtl_node)
    graph.add_node("linter", lint_node)
    graph.add_node("rtl_fixer", rtl_fixer_node)
    graph.add_node("generate_tb", generate_tb_node)
    graph.add_node("tb_fixer", tb_fixer_node)
    graph.add_node("simulator", simulation_node)
    graph.add_node("debug", debug_node)
    graph.add_node("asic", asic_node)
    graph.add_node("summary", summary_node)

    # Add edges
    graph.add_conditional_edges(
        "architect",
        lambda state: "end" if state.get("status") == "max_attempts_reached" else "designer",
        {"designer": "designer", "end": "summary"}
    )
    graph.add_edge("designer", "linter")

    # linter decision: pass → TB, fail → fix
    graph.add_conditional_edges(
        "linter",
        decide_after_lint,
        {
            "generate_tb": "generate_tb",
            "fix_rtl": "rtl_fixer",
            "end": "summary"
        }
    )

    # RTL Fixer decision: fixed → lint again, needs_regen → designer
    graph.add_conditional_edges(
        "rtl_fixer",
        decide_after_rtl_fix,
        {
            "lint": "linter",
            "regenerate_rtl": "designer",
            "end": "summary"
        }
    )

    # TB generation decision: success → simulate, error → fix
    graph.add_conditional_edges(
        "generate_tb",
        decide_after_tb,
        {
            "simulate": "simulator",
            "fix_tb": "tb_fixer",
            "end": "summary"
        }
    )

    # TB Fixer decision: fixed → simulate again, needs_regen → generate_tb
    graph.add_conditional_edges(
        "tb_fixer",
        decide_after_tb_fix,
        {
            "simulate": "simulator",
            "regenerate_tb": "generate_tb",
            "end": "summary"
        }
    )

    # Simulator decision: success → end, fail → debug
    graph.add_conditional_edges(
        "simulator",
        decide_after_sim,
        {
            "summary": "summary",
            "debug": "debug",
            "designer": "designer",
            "end": "summary"
        }
    )

    # Debug decision: auto-fix if possible, else route to fixer
    graph.add_conditional_edges(
        "debug",
        decide_after_debug,
        {
            "linter": "linter",           # RTL fixed by debugger, send to linter
            "simulate": "simulator",       # TB fixed by debugger, rerun simulation
            "fix_rtl": "rtl_fixer",        # RTL issue, couldn't auto-fix
            "fix_tb": "tb_fixer",          # TB issue, couldn't auto-fix
            "end": "summary"
        }
    )

    # Summary runs first; ASIC runs only on the successful path after summary.
    graph.add_conditional_edges(
        "summary",
        lambda state: "asic" if state.get("status") == "success" else "end",
        {"asic": "asic", "end": END}
    )
    graph.add_edge("asic", END)

    # Set entry point
    graph.set_entry_point("architect")

    return graph.compile()


# ===== MAIN EXECUTION =====

def main():
    """Main execution function"""
    # Check API key
    if not API_KEY:
        print(colored("❌ Error: API Key not configured in .env file", "red"))
        return

    # Load specifications
    script_dir = os.path.dirname(os.path.abspath(__file__))
    try:
        with open(os.path.join(script_dir, "filter_spec.json"), 'r') as f:
            rtl_specs = json.load(f)
        with open(os.path.join(script_dir, "verification_spec.json"), 'r') as f:
            verif_specs = json.load(f)
    except FileNotFoundError as e:
        print(colored(f"❌ Error: {e}", "red"))
        print("Make sure filter_spec.json and verification_spec.json exist.")
        return
    except json.JSONDecodeError as e:
        print(colored(f"❌ Error parsing JSON: {e}", "red"))
        return

    # Initialize state
    initial_state = {
        "project_root": script_dir,
        "specs": rtl_specs,
        "verif_specs": verif_specs,
        "design_plan": "",
        "verif_plan": "",
        "verilog_code": "",
        "tb_code": "",
        "makefile": "",
        "error_report": "",
        "sim_report": "",
        "attempt_count": 0,
        "tb_attempt_count": 0,
        "rtl_fix_count": 0,
        "tb_fix_count": 0,
        "status": "processing",
        "pre_fix_rtl_code":  "",
        "pre_fix_rtl_error": "",
        "pre_fix_tb_code":   "",
        "pre_fix_tb_error":  "",
    }

    # Create and run workflow
    print(colored("\n" + "="*60, "cyan"))
    print(colored("  RTL VERIFICATION WORKFLOW - STARTING", "cyan", attrs=['bold']))
    print(colored("="*60 + "\n", "cyan"))

    workflow = create_workflow()
    final_state = workflow.invoke(initial_state)  # type: ignore

    # Print final status
    print(colored("\n" + "="*60, "cyan"))
    if final_state["status"] == "success":
        print(colored("  ✅ WORKFLOW COMPLETED SUCCESSFULLY!", "green", attrs=['bold']))
        print(colored(f"  RTL: {rtl_specs['project_settings']['output_file']}", "green"))
        print(colored(f"  Verification: {verif_specs.get('testbench_settings', {}).get('method', 'systemverilog')}", "green"))
        print(colored(f"  ASIC outputs: runs/{final_state.get('full_flow_run_tag', 'fir_filter_ASIC')}", "green"))
    elif final_state["status"] == "max_attempts_reached":
        print(colored("  ⚠️  WORKFLOW STOPPED: Max retry attempts reached", "yellow", attrs=['bold']))
        print(colored(f"  RTL attempts: {final_state.get('attempt_count', 0)}/{MAX_RTL_ATTEMPTS}", "yellow"))
        print(colored(f"  Check the error messages above for details", "yellow"))
    else:
        print(colored(f"  ❌ WORKFLOW FAILED: {final_state['status']}", "red", attrs=['bold']))
        print(colored(f"  RTL attempts: {final_state.get('attempt_count', 0)}", "red"))
    print(colored("="*60 + "\n", "cyan"))


if __name__ == "__main__":
    main()