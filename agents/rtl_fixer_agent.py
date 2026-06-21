"""
RTL Fixer Agent - Fixes compilation errors in RTL code
"""
import os
import re
from termcolor import colored
from config.settings import MODEL_NAME, API_KEY, MAX_RTL_FIX_ATTEMPTS, USE_COMPACT_PROMPTS
from models.state import AgentState
from utils.code_cleaners import clean_verilog_code
from utils.api_utils import call_llm_with_retry
from utils.error_retriever import retrieve_errors, format_error_context, describe_matches


from config.prompts_compact import (
        RTL_FIXER_SYSTEM_PROMPT,
        RTL_FIXER_USER_PROMPT,
        IVERILOG_CONSTRAINTS,
        VERILATOR_CONSTRAINTS
    )


def _extract_plan_field(plan: str, key: str) -> str:
    """Extract a single value from a DESIGN_PLAN line like 'KEY=value'."""
    if not plan:
        return ""
    m = re.search(rf'^{key}=(.+)$', plan, re.MULTILINE)
    return m.group(1).strip() if m else ""


def rtl_fixer_node(state: AgentState):
    """
    Attempts to fix RTL compilation errors with minimal changes.
    If fix attempts exhausted, signals to regenerate RTL.
    """
    current_code = state.get("verilog_code", "")
    error_report = state.get("error_report", "")
    specs = state["specs"]
    verif_specs = state.get("verif_specs", {})
    fix_count = state.get("rtl_fix_count", 0)

    # Check if max fix attempts reached
    if fix_count >= MAX_RTL_FIX_ATTEMPTS:
        print(colored(f"\n[RTL Fixer] Max fix attempts ({MAX_RTL_FIX_ATTEMPTS}) reached. Regenerating RTL...", "yellow"))
        return {
            "rtl_fix_count": 0,  # Reset fix counter
            "status": "rtl_needs_regen"
        }

    print(colored(f"\n[RTL Fixer] Attempting to fix RTL errors... (Fix attempt {fix_count + 1}/{MAX_RTL_FIX_ATTEMPTS})", "cyan"))

    # Select constraints based on simulation tool
    sim_tool = verif_specs.get("verification_framework", {}).get("simulator", "iverilog")
    constraints = VERILATOR_CONSTRAINTS if sim_tool == "verilator" else IVERILOG_CONSTRAINTS

    # Error-memory retrieval: look for similar past bugs to help the LLM
    design_plan = state.get("design_plan", "")
    topology     = _extract_plan_field(design_plan, "TOPOLOGY")
    filter_class = _extract_plan_field(design_plan, "FILTER_CLASS") or "any"
    past_errors = retrieve_errors(
        raw_error=error_report,
        source="linter",
        topology=topology,
        filter_class=filter_class,
        top_k=2,
    )
    if past_errors:
        print(colored(
            f"[RTL Fixer] Error memory hits: {describe_matches(past_errors)}",
            "cyan"
        ))
    else:
        print(colored("[RTL Fixer] Error memory: no past matches", "yellow"))
    past_context = format_error_context(past_errors)

    # Build the prompt
    user_content = RTL_FIXER_USER_PROMPT.format(
        error_feedback=error_report,
        current_code=current_code,
        constraints=constraints
    )
    if past_context:
        user_content = past_context + "\n" + user_content

    messages = [
        {"role": "system", "content": RTL_FIXER_SYSTEM_PROMPT},
        {"role": "user", "content": user_content}
    ]

    try:
        response = call_llm_with_retry(model=MODEL_NAME, messages=messages, api_key=API_KEY)
        raw_code = response.choices[0].message.content
        fixed_code = clean_verilog_code(raw_code)

        # Write fixed code to file
        output_file = specs["project_settings"]["output_file"]
        os.makedirs(os.path.dirname(output_file), exist_ok=True)
        with open(output_file, 'w') as f:
            f.write(fixed_code)
        print(colored(f"[RTL Fixer] Fixed code saved to {output_file}", "green"))

        return {
            "verilog_code": fixed_code,
            "rtl_fix_count": fix_count + 1,
            "status": "rtl_fixed",
            # Snapshot for the harvest hook — if the next lint succeeds,
            # the workflow pairs these with the new verilog_code.
            "pre_fix_rtl_code":  current_code,
            "pre_fix_rtl_error": error_report,
        }

    except Exception as e:
        print(colored(f"[RTL Fixer] Error: {e}", "red"))
        return {
            "rtl_fix_count": fix_count + 1,
            "status": "rtl_fix_failed"
        }
