from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional, TypedDict

try:
    from langgraph.graph import END, StateGraph
except Exception:
    END = None
    StateGraph = None

SHELL_NIX = os.getenv("LIBRELANE_SHELL_NIX", "~/librelane/shell.nix")
FLOW_NAME = "synthesisexploration"
RUN_TAG_SUFFIX = "synexp"
FULL_FLOW_RUN_TAG = os.getenv("LIBRELANE_FULL_RUN_TAG", "fir_filter_ASIC")
NIX_WARN_DIRTY = "false"
SUMMARY_REPORT = "summary.rpt"
BOX_MARKERS = ("\u2500", "\u2502", "\u250c", "\u2510", "\u2514", "\u2518", "\u251c", "\u2524", "\u252c", "\u2534", "\u253c")
CONFIG_FILE_NAME = "config.json"
ENV_FILE_NAME = ".env"
DEFAULT_LLM_MODEL = "deepseek-coder"
DEFAULT_API_BASE_URL = "https://api.deepseek.com/chat/completions"
ANSI_RED = "\033[91m"
ANSI_RESET = "\033[0m"
CONFIG_BASE_KEYS = (
    "DESIGN_NAME",
    "PDN_MULTILAYER",
    "CLOCK_PORT",
    "CLOCK_PERIOD",
    "VERILOG_FILES",
    "FP_CORE_UTIL",
    "RT_MAX_LAYER",
)
CONFIG_SDC_KEYS = (
    "PNR_SDC_FILE",
    "SIGNOFF_SDC_FILE",
)


class AsicState(TypedDict, total=False):
    project_root: str
    config_path: str
    filter_spec_path: str
    env_path: str
    run_tag: str
    config: dict
    filter_spec: dict
    summary_rows: list[dict]
    selected_strategy: dict
    strategy_warning: str
    strategy_error: str
    config_generated: bool
    pnr_sdc_generated: bool
    pnr_sdc_path: str
    pnr_clock_period_ns: float
    pnr_input_ports: list[str]
    signoff_sdc_generated: bool
    signoff_sdc_path: str
    signoff_clock_period_ns: float
    full_flow_run_tag: str
    min_required_clock_period_ns: float
    max_achievable_frequency_mhz: float
    timing_limit_strategy: str
    asic_status: str
    asic_report: str
    asic_return_code: int
    status: str


def print_failure(message: str) -> None:
    print(f"{ANSI_RED}{message}{ANSI_RESET}")


def resolve_project_root(state: AsicState) -> Path:
    root = state.get("project_root")
    if root:
        return Path(root).resolve()
    return Path(__file__).resolve().parents[1]


def to_wsl_path(path: Path) -> str:
    resolved = path.resolve()
    drive = resolved.drive.rstrip(":").lower()
    tail = resolved.as_posix().split(":", 1)[-1]
    return f"/mnt/{drive}{tail}"


def load_config(config_path: Path) -> dict:
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")
    with config_path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError("config.json root must be a JSON object")
    return data


def load_filter_spec(spec_path: Path) -> dict:
    if not spec_path.exists():
        raise FileNotFoundError(f"Filter spec file not found: {spec_path}")
    with spec_path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError("filter_spec.json root must be a JSON object")
    return data


def load_dotenv(env_path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not env_path.exists():
        return values
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("\"'")
    return values


def derive_run_tag(config: dict) -> str:
    design_name = str(config.get("DESIGN_NAME", "design")).strip() or "design"
    sanitized = re.sub(r"[^A-Za-z0-9_.-]+", "_", design_name).strip("_.-") or "design"
    return f"{sanitized}_{RUN_TAG_SUFFIX}"


def user_provided_sdc_files(project_root: Path) -> bool:
    return (project_root / "pnr.sdc").exists() and (project_root / "signoff.sdc").exists()


def derive_expected_config(filter_spec: dict, include_sdc_paths: bool) -> dict:
    project_settings = filter_spec.get("project_settings", {})
    if not isinstance(project_settings, dict):
        raise ValueError("filter_spec.json is missing project_settings")

    output_file = str(project_settings.get("output_file", "")).strip()
    if not output_file:
        raise ValueError("filter_spec.json is missing project_settings.output_file")

    output_rel_path = Path(output_file)
    design_name = output_rel_path.stem
    if not design_name:
        raise ValueError("Could not derive DESIGN_NAME from project_settings.output_file")

    target_clock_freq_mhz = project_settings.get("target_clock_freq_mhz")
    try:
        freq_mhz = float(target_clock_freq_mhz)
    except (TypeError, ValueError) as exc:
        raise ValueError("filter_spec.json is missing valid project_settings.target_clock_freq_mhz") from exc
    if freq_mhz <= 0:
        raise ValueError("project_settings.target_clock_freq_mhz must be greater than zero")

    clock_period_ns = get_original_clock_period_ns(filter_spec)

    expected = {
        "DESIGN_NAME": design_name,
        "PDN_MULTILAYER": False,
        "CLOCK_PORT": "clk",
        "CLOCK_PERIOD": clock_period_ns,
        "VERILOG_FILES": [f"dir::{output_rel_path.as_posix()}"],
        "FP_CORE_UTIL": 40,
        "RT_MAX_LAYER": "met4",
    }
    if include_sdc_paths:
        expected["PNR_SDC_FILE"] = "dir::pnr.sdc"
        expected["SIGNOFF_SDC_FILE"] = "dir::signoff.sdc"
    return expected


def resolve_dir_style_path(project_root: Path, raw_path: str, default_name: str) -> Path:
    value = (raw_path or default_name).strip()
    if value.startswith("dir::"):
        value = value[5:]
    path = Path(value)
    if not str(path):
        path = Path(default_name)
    if path.is_absolute():
        return path
    return project_root / path


def resolve_rtl_path(project_root: Path, filter_spec: dict, config: dict) -> Path:
    project_settings = filter_spec.get("project_settings", {})
    output_file = str(project_settings.get("output_file", "")).strip()
    candidates: list[Path] = []
    if output_file:
        rel = Path(output_file)
        candidates.append(project_root / rel)
        candidates.append(project_root / rel.name)

    for item in config.get("VERILOG_FILES", []):
        if not isinstance(item, str):
            continue
        candidate = item[5:] if item.startswith("dir::") else item
        if "*" in candidate or "?" in candidate:
            candidates.extend(sorted(project_root.glob(candidate)))
        else:
            candidates.append(project_root / candidate)

    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate.resolve()) if candidate.exists() else str(candidate)
        if key in seen:
            continue
        seen.add(key)
        if candidate.exists() and candidate.is_file() and candidate.suffix in {".sv", ".v"}:
            return candidate

    raise FileNotFoundError("Could not resolve the RTL file for PnR SDC generation.")


def extract_input_ports(rtl_path: Path) -> list[str]:
    ports: list[str] = []
    for raw_line in rtl_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.split("//", 1)[0].strip()
        if not line.startswith("input"):
            continue
        has_vector = "[" in line and "]" in line
        line = re.sub(r"\binput\b", "", line)
        line = re.sub(r"\b(?:wire|reg|logic|signed|unsigned)\b", "", line)
        line = re.sub(r"\[[^\]]+\]", "", line)
        line = line.replace(");", "").replace(")", "").replace(";", "")
        for token in line.split(","):
            name = token.strip()
            if not name:
                continue
            pieces = name.split()
            if not pieces:
                continue
            port_name = pieces[-1]
            if port_name in ("clk", "rst", "rst_n"):
                continue
            formatted = f"{port_name}[*]" if has_vector else port_name
            if formatted not in ports:
                ports.append(formatted)
    if not ports:
        raise ValueError(f"Could not derive non-clock input ports from RTL: {rtl_path}")
    return ports


def get_original_clock_period_ns(filter_spec: dict) -> float:
    project_settings = filter_spec.get("project_settings", {})
    if not isinstance(project_settings, dict):
        raise ValueError("filter_spec.json is missing project_settings")
    target_clock_freq_mhz = project_settings.get("target_clock_freq_mhz")
    try:
        freq_mhz = float(target_clock_freq_mhz)
    except (TypeError, ValueError) as exc:
        raise ValueError("filter_spec.json is missing valid project_settings.target_clock_freq_mhz") from exc
    if freq_mhz <= 0:
        raise ValueError("project_settings.target_clock_freq_mhz must be greater than zero")
    return round(1000.0 / freq_mhz, 6)


def get_max_area_constraint(filter_spec: dict) -> Optional[float]:
    hardware = filter_spec.get("hardware_specifications", {})
    if not isinstance(hardware, dict):
        return None

    value = hardware.get("max_area_um2")
    if value in (None, "", []):
        return None

    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _is_table_like_line(line: str) -> bool:
    return line.count("|") >= 2 or any(marker in line for marker in BOX_MARKERS)


def find_summary_report(workdir: Path, run_tag: str) -> Path:
    return workdir / "runs" / run_tag / SUMMARY_REPORT


def read_summary_text(summary_path: Path) -> str:
    return summary_path.read_text(encoding="utf-8")


def parse_summary_rows(summary_text: str) -> list[dict]:
    rows: list[dict] = []
    for raw_line in summary_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        normalized = line.replace("\u2502", "|").replace("\ufeff", "")
        ascii_friendly = re.sub(r"[^\x20-\x7E]", " ", normalized)
        if "SYNTH_STRATEGY" in normalized or "SYNTH_STRATEGY" in ascii_friendly:
            continue

        if normalized.count("|") >= 5:
            parts = [part.strip() for part in normalized.strip("|").split("|")]
            if len(parts) >= 6:
                parts = parts[:6]
                try:
                    rows.append(
                        {
                            "strategy": parts[0],
                            "gates": int(parts[1]),
                            "area_um2": float(parts[2]),
                            "worst_r2r_setup_slack_ns": float(parts[3]),
                            "worst_setup_slack_ns": float(parts[4]),
                            "total_negative_setup_slack_ns": float(parts[5]),
                        }
                    )
                    continue
                except ValueError:
                    pass

        fallback_match = re.search(
            r"(AREA\s+\d+|DELAY\s+\d+)\s+(\d+)\s+([-+]?\d*\.?\d+)\s+([-+]?\d*\.?\d+)\s+([-+]?\d*\.?\d+)\s+([-+]?\d*\.?\d+)",
            ascii_friendly,
        )
        if fallback_match:
            rows.append(
                {
                    "strategy": fallback_match.group(1),
                    "gates": int(fallback_match.group(2)),
                    "area_um2": float(fallback_match.group(3)),
                    "worst_r2r_setup_slack_ns": float(fallback_match.group(4)),
                    "worst_setup_slack_ns": float(fallback_match.group(5)),
                    "total_negative_setup_slack_ns": float(fallback_match.group(6)),
                }
            )

    if not rows:
        raise ValueError("Could not parse any synthesis strategies from summary.rpt")
    return rows


def get_clock_period_ns(config: dict) -> float:
    value = config.get("CLOCK_PERIOD")
    if value is None:
        raise ValueError("config.json must define CLOCK_PERIOD for synthesis strategy selection")
    return float(value)


def row_has_positive_timing(row: dict) -> bool:
    return row["worst_setup_slack_ns"] > 0.0 and row["worst_r2r_setup_slack_ns"] > 0.0


def compute_min_required_clock(selected_row: dict, current_clock_period_ns: float) -> tuple[float, Optional[float]]:
    min_required_clock_period_ns = max(current_clock_period_ns - selected_row["worst_r2r_setup_slack_ns"], 0.0)
    max_achievable_frequency_mhz = (1000.0 / min_required_clock_period_ns) if min_required_clock_period_ns > 0 else None
    return min_required_clock_period_ns, max_achievable_frequency_mhz


def build_timing_failure_message(rows: list[dict], current_clock_period_ns: float, max_area_um2: Optional[float]) -> str:
    best_slack_row = max(
        rows,
        key=lambda row: (
            min(row["worst_setup_slack_ns"], row["worst_r2r_setup_slack_ns"]),
            row["worst_r2r_setup_slack_ns"],
            row["worst_setup_slack_ns"],
            -row["area_um2"],
            -row["gates"],
        ),
    )
    limiting_slack_ns = min(best_slack_row["worst_setup_slack_ns"], best_slack_row["worst_r2r_setup_slack_ns"])
    slack_margin_ns = abs(limiting_slack_ns)
    suggested_clock_period_ns = current_clock_period_ns + slack_margin_ns
    suggested_frequency_mhz = 1000.0 / suggested_clock_period_ns
    limiting_metric = (
        "worst_setup_slack_ns"
        if best_slack_row["worst_setup_slack_ns"] <= best_slack_row["worst_r2r_setup_slack_ns"]
        else "worst_r2r_setup_slack_ns"
    )

    area_prefix = ""
    if max_area_um2 is not None:
        area_prefix = f"For max_area_um2={max_area_um2}, "

    return (
        f"{area_prefix}no explored synthesis strategy achieved both positive setup slack and positive R2R setup slack "
        f"at CLOCK_PERIOD={current_clock_period_ns:.6f} ns. Best available strategy is {best_slack_row['strategy']} "
        f"with worst_r2r_setup_slack={best_slack_row['worst_r2r_setup_slack_ns']:.6f} ns and "
        f"worst_setup_slack={best_slack_row['worst_setup_slack_ns']:.6f} ns. "
        f"The limiting metric is {limiting_metric}={limiting_slack_ns:.6f} ns. "
        f"Increase CLOCK_PERIOD to at least {suggested_clock_period_ns:.6f} ns "
        f"(~{suggested_frequency_mhz:.3f} MHz) and rerun synthesis exploration."
    )


def choose_strategy(rows: list[dict], max_area_um2: Optional[float], current_clock_period_ns: float) -> tuple[Optional[dict], Optional[str], Optional[str]]:
    warning = None
    error = None
    timing_clean_rows = [row for row in rows if row_has_positive_timing(row)]

    if max_area_um2 is None:
        if timing_clean_rows:
            selected = max(
                timing_clean_rows,
                key=lambda row: (
                    row["worst_r2r_setup_slack_ns"],
                    row["worst_setup_slack_ns"],
                    -row["area_um2"],
                    -row["gates"],
                ),
            )
            return selected, warning, error

        error = build_timing_failure_message(rows, current_clock_period_ns, max_area_um2)
        return None, warning, error

    candidates = [row for row in timing_clean_rows if (3.0 * row["area_um2"]) <= max_area_um2]
    if candidates:
        selected = max(
            candidates,
            key=lambda row: (
                row["worst_r2r_setup_slack_ns"],
                row["worst_setup_slack_ns"],
                -row["area_um2"],
                -row["gates"],
            ),
        )
        return selected, warning, error

    if timing_clean_rows:
        selected = min(
            timing_clean_rows,
            key=lambda row: (
                row["area_um2"],
                -row["worst_r2r_setup_slack_ns"],
                -row["worst_setup_slack_ns"],
                row["gates"],
            ),
        )
        warning = (
            f"max_area_um2={max_area_um2} cannot be met by any timing-clean strategy after applying the 3x area guard band. "
            f"Using least-area timing-clean strategy {selected['strategy']} instead."
        )
        return selected, warning, error

    error = build_timing_failure_message(rows, current_clock_period_ns, max_area_um2)
    return None, warning, error


def to_dir_style_path(project_root: Path, file_path: Path) -> str:
    try:
        relative = file_path.resolve().relative_to(project_root.resolve())
    except ValueError:
        return str(file_path)
    return f"dir::{relative.as_posix()}"


def write_config_updates(config_path: Path, project_root: Path, strategy: str, pnr_path: Path, signoff_path: Path) -> None:
    config = load_config(config_path)
    config["SYNTH_STRATEGY"] = strategy
    config["PNR_SDC_FILE"] = to_dir_style_path(project_root, pnr_path)
    config["SIGNOFF_SDC_FILE"] = to_dir_style_path(project_root, signoff_path)
    config_path.write_text(json.dumps(config, indent=4) + "\n", encoding="utf-8")


def build_command(workdir: Path, run_tag: str, config_name: str) -> list[str]:
    wsl_dir = to_wsl_path(workdir)
    shell_cmd = (
        f"cd '{wsl_dir}' && "
        f"nix-shell --option warn-dirty {NIX_WARN_DIRTY} {SHELL_NIX} --run \"stdbuf -oL -eL librelane ./{config_name} --flow {FLOW_NAME} --run-tag {run_tag} --overwrite\""
    )
    return ["wsl", "bash", "-lc", shell_cmd]


def build_full_flow_command(workdir: Path, run_tag: str, config_name: str) -> list[str]:
    wsl_dir = to_wsl_path(workdir)
    shell_cmd = (
        f"cd '{wsl_dir}' && "
        f"nix-shell --option warn-dirty {NIX_WARN_DIRTY} {SHELL_NIX} --run \"stdbuf -oL -eL librelane ./{config_name} --run-tag {run_tag} --overwrite\""
    )
    return ["wsl", "bash", "-lc", shell_cmd]


def _failure_state(message: str, report: Optional[str] = None) -> AsicState:
    return {
        "status": "failed",
        "asic_status": "failed",
        "asic_report": report or message,
    }


def _strip_code_fences(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json|tcl|sdc)?\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)
    return stripped.strip()


def _normalize_model_name(model_name: str) -> str:
    return model_name.split("/", 1)[1] if "/" in model_name else model_name


def _call_llm_text(api_key: str, api_base_url: str, model_name: str, system_prompt: str, user_prompt: str) -> str:
    payload = {
        "model": _normalize_model_name(model_name),
        "temperature": 0,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }
    request = urllib.request.Request(
        api_base_url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise ValueError(f"LLM request failed with HTTP {exc.code}: {detail}") from exc
    data = json.loads(body)
    return str(data["choices"][0]["message"]["content"])


def _build_config_generation_messages(expected_config: dict) -> tuple[str, str]:
    system_prompt = (
        "You write exactly one LibreLane config.json object for synthesis exploration. "
        "Return raw JSON only. Do not add markdown, comments, explanations, or extra keys. "
        f"The JSON must contain exactly these keys in any order: {', '.join(expected_config.keys())}."
    )
    user_prompt = (
        "Write config.json for LibreLane synthesis exploration using the exact values below.\n"
        "Rules:\n"
        "- Return a single JSON object only.\n"
        "- Do not add any keys beyond the required list.\n"
        "- Keep constant values exactly as provided.\n"
        "- VERILOG_FILES must stay as an array.\n"
        "- If PNR_SDC_FILE and SIGNOFF_SDC_FILE are not present in the required JSON values, do not add them.\n"
        "Required JSON values:\n"
        f"{json.dumps(expected_config, indent=2)}"
    )
    return system_prompt, user_prompt


def _validate_generated_config(candidate: dict, expected_config: dict) -> None:
    required_keys = tuple(expected_config.keys())
    if set(candidate.keys()) != set(required_keys):
        raise ValueError(
            "Generated config.json keys do not match the required exact key set: "
            f"{sorted(required_keys)}"
        )

    for key in required_keys:
        if key == "CLOCK_PERIOD":
            try:
                actual = float(candidate[key])
                expected = float(expected_config[key])
            except (TypeError, ValueError) as exc:
                raise ValueError("Generated CLOCK_PERIOD is not numeric") from exc
            if abs(actual - expected) > 1e-6:
                raise ValueError(f"Generated CLOCK_PERIOD={actual} does not match expected value {expected}")
            continue

        if candidate[key] != expected_config[key]:
            raise ValueError(
                f"Generated {key}={candidate[key]!r} does not match expected value {expected_config[key]!r}"
            )


def get_llm_settings(project_root: Path) -> tuple[str, str, str]:
    env_path = project_root / ENV_FILE_NAME
    env_values = load_dotenv(env_path)
    api_key = env_values.get("API_KEY") or os.environ.get("API_KEY")
    if not api_key:
        raise ValueError(f"Missing API_KEY in {env_path}")

    api_base_url = env_values.get("API_BASE_URL") or env_values.get("OPENAI_API_BASE") or env_values.get("DEEPSEEK_API_BASE") or DEFAULT_API_BASE_URL
    model_name = env_values.get("LLM_MODEL") or env_values.get("OPENAI_MODEL") or env_values.get("MODEL_NAME") or DEFAULT_LLM_MODEL
    return api_key, api_base_url, model_name


def generate_config_with_llm(project_root: Path, filter_spec: dict) -> dict:
    api_key, api_base_url, model_name = get_llm_settings(project_root)
    expected_config = derive_expected_config(filter_spec, include_sdc_paths=user_provided_sdc_files(project_root))
    system_prompt, user_prompt = _build_config_generation_messages(expected_config)

    last_error = ""
    for attempt in range(2):
        prompt = user_prompt
        if attempt > 0 and last_error:
            prompt += f"\n\nPrevious response was invalid: {last_error}\nReturn corrected raw JSON only."
        raw_response = _call_llm_text(api_key, api_base_url, model_name, system_prompt, prompt)
        cleaned = _strip_code_fences(raw_response)
        try:
            candidate = json.loads(cleaned)
        except json.JSONDecodeError as exc:
            last_error = f"response was not valid JSON: {exc}"
            continue
        if not isinstance(candidate, dict):
            last_error = "response root was not a JSON object"
            continue
        try:
            _validate_generated_config(candidate, expected_config)
        except ValueError as exc:
            last_error = str(exc)
            continue

        config_path = project_root / CONFIG_FILE_NAME
        config_path.write_text(json.dumps(candidate, indent=4) + "\n", encoding="utf-8")
        return candidate

    raise ValueError(f"LLM failed to generate a valid config.json. Last error: {last_error}")


def build_pnr_sdc_generation_messages(clock_period_ns: float, input_ports: list[str]) -> tuple[str, str]:
    input_ports_str = " ".join(input_ports)
    system_prompt = (
        "You are an expert in VLSI physical design and SDC constraint files for LibreLane/OpenLane PnR flows.\n\n"
        "Your task is to generate a complete, valid LibreLane PnR .sdc file.\n\n"
        "INPUT VARIABLES (provided by the agent)\n"
        f"- CLOCK_PERIOD  : {clock_period_ns:.6f}\n"
        f"- INPUT_PORTS   : {input_ports_str}\n\n"
        "GENERATION RULES — follow exactly\n"
        "RULE 1 — CLOCK PERIOD (variable)\n"
        "  • Replace every occurrence of the numeric clock period with the provided CLOCK_PERIOD.\n"
        "  • Affected lines:\n"
        "      create_clock ... -period CLOCK_PERIOD\n"
        "      set io_delay_value [expr { CLOCK_PERIOD * 0.2 }]\n\n"
        "RULE 2 — IO DELAY FORMULA (derived, never hardcoded)\n"
        "  • Always compute as: set io_delay_value [expr { CLOCK_PERIOD * 0.2 }]\n"
        "  • Never write a literal result.\n\n"
        "RULE 3 — CONSTANT VALUES (never change these)\n"
        "  • set_clock_uncertainty 0.5\n"
        "  • set_max_transition 0.75\n"
        "  • set_max_fanout 16\n"
        "  • set_timing_derate -early [expr {1 - 0.05}]\n"
        "  • set_timing_derate -late [expr {1 + 0.05}]\n"
        "  • set_input_transition 0.15 on clk only\n"
        "  • set_input_delay -min 0.0\n"
        "  • set_output_delay -min 0.0\n"
        "  • set_load 0.05\n\n"
        "RULE 4 — INPUT / OUTPUT PORTS\n"
        "  • Apply both -max and -min input_delay to: [get_ports { INPUT_PORTS }]\n"
        "  • Apply both -max and -min output_delay to: [all_outputs]\n\n"
        "RULE 5 — COMMENT HEADER\n"
        "Include a header block documenting the clock period, the 20% IO-delay formula, and that the rest are constant flow-policy constraints.\n\n"
        "RULE 6 — OUTPUT FORMAT\n"
        "  • Emit raw SDC / Tcl only.\n"
        "  • No markdown, no code fences, no prose.\n"
        "  • Preserve the section order exactly as specified in the template."
    )
    user_prompt = (
        "Generate the LibreLane PnR SDC now using this exact template structure and values.\n\n"
        "#------------------------------------------#\n"
        "# LibreLane PnR Constraints\n"
        "#------------------------------------------#\n"
        f"# Clock period : {clock_period_ns:.6f} ns\n"
        "# I/O delay    : 20% of clock period  (OpenLane formula)\n"
        "# All other values are constant flow-policy constraints\n"
        "#------------------------------------------#\n\n"
        "# Clock network\n"
        "set clk_input clk\n"
        f"create_clock [get_ports $clk_input] -name clk -period {clock_period_ns:.6f}\n\n"
        "# Clock non-idealities\n"
        "set_propagated_clock [get_clocks {clk}]\n"
        "set_clock_uncertainty 0.5 [get_clocks {clk}]\n\n"
        "# Constant implementation policy constraints\n"
        "set_max_transition 0.75 [current_design]\n"
        "set_max_fanout     16   [current_design]\n"
        "set_timing_derate -early [expr {1 - 0.05}]\n"
        "set_timing_derate -late  [expr {1 + 0.05}]\n\n"
        "# Constant clock input transition\n"
        "set_input_transition 0.15 [get_ports $clk_input]\n\n"
        "# Placeholder I/O timing — OpenLane formula: IO delay = 20% of clock period\n"
        f"set io_delay_value [expr {{ {clock_period_ns:.6f} * 0.2 }}]\n\n"
        "# Input delays (replace with interface-specific values when known)\n"
        f"set_input_delay -max $io_delay_value -clock [get_clocks {{clk}}] [get_ports {{ {input_ports_str} }}]\n"
        f"set_input_delay -min 0.0             -clock [get_clocks {{clk}}] [get_ports {{ {input_ports_str} }}]\n\n"
        "# Output delays\n"
        "set_output_delay -max $io_delay_value -clock [get_clocks {clk}] [all_outputs]\n"
        "set_output_delay -min 0.0             -clock [get_clocks {clk}] [all_outputs]\n\n"
        "# Constant output load  (0.05 pF = 50 fF)\n"
        "set_load 0.05 [all_outputs]"
    )
    return system_prompt, user_prompt


def build_signoff_sdc_generation_messages(clock_period_ns: float, input_ports: list[str]) -> tuple[str, str]:
    input_ports_str = " ".join(input_ports)
    system_prompt = (
        "You are an expert in VLSI physical design and SDC constraint files for LibreLane/OpenLane signoff STA flows.\n\n"
        "Your task is to generate a complete, valid LibreLane signoff .sdc file.\n\n"
        "INPUT VARIABLES (provided by the agent)\n"
        f"- CLOCK_PERIOD  : {clock_period_ns:.6f}\n"
        f"- INPUT_PORTS   : {input_ports_str}\n\n"
        "GENERATION RULES ? follow exactly\n"
        "RULE 1 ? CLOCK PERIOD (variable)\n"
        "  ? Replace every occurrence of the numeric clock period with the provided CLOCK_PERIOD.\n"
        "  ? Affected lines:\n"
        "      create_clock ... -period CLOCK_PERIOD\n"
        "      set io_delay_value [expr { CLOCK_PERIOD * 0.2 }]\n\n"
        "RULE 2 ? IO DELAY FORMULA (derived, never hardcoded)\n"
        "  ? Always compute as: set io_delay_value [expr { CLOCK_PERIOD * 0.2 }]\n"
        "  ? Never write a literal result.\n\n"
        "RULE 3 ? CONSTANT VALUES (never change these)\n"
        "  ? set_clock_uncertainty 0.1\n"
        "  ? set_max_transition 1.5\n"
        "  ? set_max_fanout 16\n"
        "  ? set_timing_derate -early [expr {1 - 0.02}]\n"
        "  ? set_timing_derate -late [expr {1 + 0.02}]\n"
        "  ? set_input_transition 0.15 on clk only\n"
        "  ? set_input_delay -min 0.0\n"
        "  ? set_output_delay -min 0.0\n"
        "  ? set_load 0.05\n\n"
        "RULE 4 ? INPUT / OUTPUT PORTS\n"
        "  ? Apply both -max and -min input_delay to: [get_ports { INPUT_PORTS }]\n"
        "  ? Apply both -max and -min output_delay to: [all_outputs]\n\n"
        "RULE 5 ? COMMENT HEADER\n"
        "Include a header block documenting the clock period, the 20% IO-delay formula, and that the rest are constant signoff-policy constraints.\n\n"
        "RULE 6 ? OUTPUT FORMAT\n"
        "  ? Emit raw SDC / Tcl only.\n"
        "  ? No markdown, no code fences, no prose.\n"
        "  ? Preserve the section order exactly as specified in the template."
    )
    user_prompt = (
        "Generate the LibreLane signoff SDC now using this exact template structure and values.\n\n"
        "#------------------------------------------#\n"
        "# LibreLane Signoff Constraints\n"
        "#------------------------------------------#\n"
        f"# Clock period : {clock_period_ns:.6f} ns\n"
        "# I/O delay    : 20% of clock period (OpenLane formula)\n"
        "# All other values are constant signoff-policy constraints\n"
        "#------------------------------------------#\n\n"
        "# Clock network\n"
        "set clk_input clk\n"
        f"create_clock [get_ports $clk_input] -name clk -period {clock_period_ns:.6f}\n\n"
        "# Clock non-idealities\n"
        "set_propagated_clock [get_clocks {clk}]\n"
        "set_clock_uncertainty 0.1 [get_clocks {clk}]\n\n"
        "# Constant signoff policy constraints\n"
        "set_max_transition 1.5 [current_design]\n"
        "set_max_fanout     16  [current_design]\n"
        "set_timing_derate -early [expr {1 - 0.02}]\n"
        "set_timing_derate -late  [expr {1 + 0.02}]\n\n"
        "# Constant clock input transition\n"
        "set_input_transition 0.15 [get_ports $clk_input]\n\n"
        "# Placeholder I/O timing ? OpenLane formula: IO delay = 20% of clock period\n"
        f"set io_delay_value [expr {{ {clock_period_ns:.6f} * 0.2 }}]\n\n"
        "# Input delays (replace with interface-specific values when known)\n"
        f"set_input_delay -max $io_delay_value -clock [get_clocks {{clk}}] [get_ports {{ {input_ports_str} }}]\n"
        f"set_input_delay -min 0.0             -clock [get_clocks {{clk}}] [get_ports {{ {input_ports_str} }}]\n\n"
        "# Output delays\n"
        "set_output_delay -max $io_delay_value -clock [get_clocks {clk}] [all_outputs]\n"
        "set_output_delay -min 0.0             -clock [get_clocks {clk}] [all_outputs]\n\n"
        "# Constant output load (0.05 pF = 50 fF)\n"
        "set_load 0.05 [all_outputs]"
    )
    return system_prompt, user_prompt

def generate_pnr_sdc_with_llm(project_root: Path, pnr_path: Path, clock_period_ns: float, input_ports: list[str]) -> str:
    api_key, api_base_url, model_name = get_llm_settings(project_root)
    system_prompt, user_prompt = build_pnr_sdc_generation_messages(clock_period_ns, input_ports)
    raw_response = _call_llm_text(api_key, api_base_url, model_name, system_prompt, user_prompt)
    sdc_text = _strip_code_fences(raw_response)
    if not sdc_text:
        raise ValueError("LLM returned an empty PnR SDC response.")
    pnr_path.write_text(sdc_text + "\n", encoding="utf-8")
    return sdc_text


def generate_signoff_sdc_with_llm(project_root: Path, signoff_path: Path, clock_period_ns: float, input_ports: list[str]) -> str:
    api_key, api_base_url, model_name = get_llm_settings(project_root)
    system_prompt, user_prompt = build_signoff_sdc_generation_messages(clock_period_ns, input_ports)
    raw_response = _call_llm_text(api_key, api_base_url, model_name, system_prompt, user_prompt)
    sdc_text = _strip_code_fences(raw_response)
    if not sdc_text:
        raise ValueError("LLM returned an empty signoff SDC response.")
    signoff_path.write_text(sdc_text + "\n", encoding="utf-8")
    return sdc_text


def asic_input_node(state: AsicState) -> AsicState:
    project_root = resolve_project_root(state)
    config_path = project_root / CONFIG_FILE_NAME
    filter_spec_path = project_root / "filter_spec.json"
    env_path = project_root / ENV_FILE_NAME

    print("[ASIC] Loading ASIC inputs...")
    filter_spec = state.get("filter_spec") or state.get("specs")
    if filter_spec is None:
        filter_spec = load_filter_spec(filter_spec_path)
    config_generated = False
    if not config_path.exists():
        print("[ASIC] config.json not provided. Generating it with the LLM...")
        generate_config_with_llm(project_root, filter_spec)
        config_generated = True
        print(f"[ASIC] Generated {CONFIG_FILE_NAME} in {project_root}")

    config = load_config(config_path)
    run_tag = derive_run_tag(config)

    return {
        "project_root": str(project_root),
        "config_path": str(config_path),
        "filter_spec_path": str(filter_spec_path),
        "env_path": str(env_path),
        "config": config,
        "filter_spec": filter_spec,
        "run_tag": run_tag,
        "config_generated": config_generated,
        "status": state.get("status", "inputs_ready") if state.get("status") not in (None, "") else "inputs_ready",
    }


def asic_env_node(state: AsicState) -> AsicState:
    print("[ASIC] Checking WSL / nix-shell / LibreLane environment...")
    check = subprocess.run(
        [
            "wsl",
            "bash",
            "-lc",
            f"test -f {SHELL_NIX} && command -v nix-shell >/dev/null 2>&1 && nix-shell --option warn-dirty {NIX_WARN_DIRTY} {SHELL_NIX} --run 'command -v librelane >/dev/null 2>&1'",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    if check.returncode != 0:
        message = "ASIC environment check failed."
        if check.stdout:
            print(check.stdout.rstrip())
        print_failure(f"[ASIC] ERROR: {message}")
        return _failure_state(message, check.stdout)
    print("[ASIC] Environment ready.")
    return {"status": "env_ready"}


def synth_exploration_node(state: AsicState) -> AsicState:
    workdir = Path(state["project_root"])
    config_path = Path(state["config_path"])
    run_tag = state["run_tag"]

    print("[ASIC] Starting synthesis exploration...")
    print(f"[ASIC] Working directory: {workdir}")
    print(f"[ASIC] Config file: {config_path}")
    print(f"[ASIC] Run tag: {run_tag}")

    cmd = build_command(workdir, run_tag, config_path.name)
    print("[ASIC] Launching WSL...")
    print("[ASIC] Entering nix-shell ~/librelane/shell.nix...")
    print("[ASIC] Running LibreLane flow: SynthesisExploration")
    print("[ASIC] Live LibreLane log follows:\n")

    return_code = 1
    process = None
    try:
        process = subprocess.Popen(
            cmd,
            cwd=str(workdir),
            stdout=None,
            stderr=None,
        )
        return_code = process.wait()
    finally:
        pass

    print("\n[ASIC] LibreLane finished.")
    print(f"[ASIC] Exit code: {return_code}")

    report = ""
    if return_code != 0:
        failed = _failure_state("LibreLane synthesis exploration failed.", report)
        failed["asic_return_code"] = return_code
        return failed

    summary_path = find_summary_report(workdir, run_tag)
    summary_rows: list[dict] = []
    min_required_clock_period_ns = None
    max_achievable_frequency_mhz = None
    timing_limit_strategy = None

    if not summary_path.exists():
        message = f"Summary report not found after synthesis exploration: {summary_path}"
        print_failure(f"[ASIC] ERROR: {message}")
        failed = _failure_state(message, message)
        failed["asic_return_code"] = return_code
        return failed

    summary_rows = parse_summary_rows(read_summary_text(summary_path))
    current_clock_period_ns = get_clock_period_ns(state["config"])
    max_area_um2 = get_max_area_constraint(state["filter_spec"])
    selected, warning, error = choose_strategy(summary_rows, max_area_um2, current_clock_period_ns)
    if error:
        print_failure(f"[ASIC] {error}")
        failed = _failure_state(error, error)
        failed["asic_return_code"] = return_code
        failed["summary_rows"] = summary_rows
        return failed

    assert selected is not None
    min_required_clock_period_ns, max_achievable_frequency_mhz = compute_min_required_clock(selected, current_clock_period_ns)
    timing_limit_strategy = selected["strategy"]

    if max_area_um2 is None:
        print("[ASIC] No max_area_um2 constraint was provided. Selecting the highest-R2R timing-clean strategy.")
    else:
        print(f"[ASIC] Applying max_area_um2 constraint with 3x area guard band: {max_area_um2}")

    if warning:
        print(f"[ASIC] WARNING: {warning}")

    print(
        "[ASIC] Selected synthesis strategy: "
        f"{selected['strategy']} | area={selected['area_um2']:.6f} um^2 | "
        f"worst_setup_slack={selected['worst_setup_slack_ns']:.6f} ns | "
        f"worst_r2r_setup_slack={selected['worst_r2r_setup_slack_ns']:.6f} ns"
    )
    print(
        "[ASIC] Estimated minimum synthesis-time clock period: "
        f"{min_required_clock_period_ns:.6f} ns "
        f"(~{max_achievable_frequency_mhz:.3f} MHz) using strategy {timing_limit_strategy}."
    )

    result = {
        "status": "synth_exploration_done",
        "asic_return_code": return_code,
        "asic_report": report,
        "summary_rows": summary_rows,
        "selected_strategy": selected,
        "strategy_warning": warning or "",
        "min_required_clock_period_ns": min_required_clock_period_ns,
        "timing_limit_strategy": timing_limit_strategy,
    }
    if max_achievable_frequency_mhz is not None:
        result["max_achievable_frequency_mhz"] = max_achievable_frequency_mhz
    return result


def pnr_sdc_node(state: AsicState) -> AsicState:
    workdir = Path(state["project_root"])
    config = state["config"]
    filter_spec = state["filter_spec"]
    pnr_raw_path = str(config.get("PNR_SDC_FILE", "dir::pnr.sdc"))
    pnr_path = resolve_dir_style_path(workdir, pnr_raw_path, "pnr.sdc")

    if pnr_path.exists():
        print(f"[ASIC] Using provided PnR SDC: {pnr_path}")
        return {
            "status": "pnr_sdc_ready",
            "pnr_sdc_path": str(pnr_path),
            "pnr_sdc_generated": False,
        }

    min_required_clock_period_ns = state.get("min_required_clock_period_ns")
    if min_required_clock_period_ns is None:
        return _failure_state("Cannot generate pnr.sdc because the minimum required clock period was not computed.")

    pnr_clock_period_ns = round(min_required_clock_period_ns * 0.7, 6)
    rtl_path = resolve_rtl_path(workdir, filter_spec, config)
    input_ports = extract_input_ports(rtl_path)

    print("[ASIC] pnr.sdc not provided. Generating it with the LLM...")
    print(f"[ASIC] PnR SDC generation clock period: {pnr_clock_period_ns:.6f} ns")
    generate_pnr_sdc_with_llm(workdir, pnr_path, pnr_clock_period_ns, input_ports)
    print(f"[ASIC] Generated pnr.sdc at {pnr_path}")

    return {
        "status": "pnr_sdc_ready",
        "pnr_sdc_path": str(pnr_path),
        "pnr_sdc_generated": True,
        "pnr_clock_period_ns": pnr_clock_period_ns,
        "pnr_input_ports": input_ports,
    }


def signoff_sdc_node(state: AsicState) -> AsicState:
    workdir = Path(state["project_root"])
    config = state["config"]
    filter_spec = state["filter_spec"]
    signoff_raw_path = str(config.get("SIGNOFF_SDC_FILE", "dir::signoff.sdc"))
    signoff_path = resolve_dir_style_path(workdir, signoff_raw_path, "signoff.sdc")

    if signoff_path.exists():
        print(f"[ASIC] Using provided signoff SDC: {signoff_path}")
        return {
            "status": "signoff_sdc_ready",
            "signoff_sdc_path": str(signoff_path),
            "signoff_sdc_generated": False,
        }

    signoff_clock_period_ns = get_original_clock_period_ns(filter_spec)
    input_ports = state.get("pnr_input_ports")
    if not input_ports:
        rtl_path = resolve_rtl_path(workdir, filter_spec, config)
        input_ports = extract_input_ports(rtl_path)

    print("[ASIC] signoff.sdc not provided. Generating it with the LLM...")
    print(f"[ASIC] Signoff SDC generation clock period: {signoff_clock_period_ns:.6f} ns")
    generate_signoff_sdc_with_llm(workdir, signoff_path, signoff_clock_period_ns, input_ports)
    print(f"[ASIC] Generated signoff.sdc at {signoff_path}")

    return {
        "status": "signoff_sdc_ready",
        "signoff_sdc_path": str(signoff_path),
        "signoff_sdc_generated": True,
        "signoff_clock_period_ns": signoff_clock_period_ns,
    }


def strategy_node(state: AsicState) -> AsicState:
    selected = state.get("selected_strategy")
    if not selected:
        return _failure_state("No selected synthesis strategy is available after synthesis exploration.")

    return {
        "status": "strategy_ready",
        "selected_strategy": selected,
        "strategy_warning": state.get("strategy_warning", ""),
        "summary_rows": state.get("summary_rows", []),
    }


def config_update_node(state: AsicState) -> AsicState:
    config_path = Path(state["config_path"])
    project_root = Path(state["project_root"])
    selected = state.get("selected_strategy")
    if not selected:
        return _failure_state("No selected synthesis strategy available for config update.")

    pnr_sdc_path = state.get("pnr_sdc_path")
    signoff_sdc_path = state.get("signoff_sdc_path")
    if not pnr_sdc_path or not signoff_sdc_path:
        return _failure_state("PnR/signoff SDC paths are required before updating config.json.")

    pnr_path = Path(pnr_sdc_path)
    signoff_path = Path(signoff_sdc_path)
    write_config_updates(config_path, project_root, selected["strategy"], pnr_path, signoff_path)
    print(
        f"[ASIC] Updated config.json with SYNTH_STRATEGY=\"{selected['strategy']}\", "
        f"PNR_SDC_FILE=\"{to_dir_style_path(project_root, pnr_path)}\", "
        f"SIGNOFF_SDC_FILE=\"{to_dir_style_path(project_root, signoff_path)}\""
    )
    return {"status": "config_updated"}


def full_flow_node(state: AsicState) -> AsicState:
    workdir = Path(state["project_root"])
    config_path = Path(state["config_path"])
    run_tag = FULL_FLOW_RUN_TAG
    run_dir = workdir / "runs" / run_tag

    print("[ASIC] Starting full LibreLane flow...")
    print(f"[ASIC] Full-flow run tag: {run_tag}")
    if run_dir.exists():
        print(f"[ASIC] Removing old run directory: {run_dir}")
        import shutil
        shutil.rmtree(run_dir)

    cmd = build_full_flow_command(workdir, run_tag, config_path.name)
    print("[ASIC] Launching WSL...")
    print("[ASIC] Entering nix-shell ~/librelane/shell.nix...")
    print("[ASIC] Running LibreLane full flow")
    print("[ASIC] Live LibreLane log follows:\n")

    return_code = 1
    process = None
    try:
        process = subprocess.Popen(
            cmd,
            cwd=str(workdir),
            stdout=None,
            stderr=None,
        )
        return_code = process.wait()
    finally:
        pass

    print("\n[ASIC] Full LibreLane flow finished.")
    print(f"[ASIC] Exit code: {return_code}")

    report = ""
    if return_code != 0:
        failed = _failure_state("LibreLane full flow failed.", report)
        failed["asic_return_code"] = return_code
        return failed

    print(f"[ASIC] Full ASIC flow outputs are saved at: {run_dir}")

    return {
        "status": "success",
        "asic_status": "success",
        "asic_report": report,
        "asic_return_code": return_code,
        "full_flow_run_tag": run_tag,
    }


def asic_finalize_node(state: AsicState) -> AsicState:
    result: AsicState = {
        "status": state.get("status", "success"),
        "asic_status": state.get("asic_status", "success"),
        "asic_report": state.get("asic_report", ""),
    }
    if state.get("selected_strategy"):
        result["selected_strategy"] = state["selected_strategy"]
    if state.get("pnr_sdc_path"):
        result["pnr_sdc_path"] = state["pnr_sdc_path"]
    if state.get("signoff_sdc_path"):
        result["signoff_sdc_path"] = state["signoff_sdc_path"]
    if state.get("full_flow_run_tag"):
        result["full_flow_run_tag"] = state["full_flow_run_tag"]
    return result


def _route_on_failure(state: AsicState) -> str:
    return END if state.get("status") == "failed" else "continue"


def create_workflow():
    if StateGraph is None or END is None:
        raise RuntimeError("LangGraph is not installed in this environment.")

    graph = StateGraph(AsicState)
    graph.add_node("asic_input", asic_input_node)
    graph.add_node("asic_env", asic_env_node)
    graph.add_node("synth_exploration", synth_exploration_node)
    graph.add_node("pnr_sdc", pnr_sdc_node)
    graph.add_node("signoff_sdc", signoff_sdc_node)
    graph.add_node("config_update", config_update_node)
    graph.add_node("full_flow", full_flow_node)

    graph.set_entry_point("asic_input")
    graph.add_conditional_edges("asic_input", _route_on_failure, {"continue": "asic_env", END: END})
    graph.add_conditional_edges("asic_env", _route_on_failure, {"continue": "synth_exploration", END: END})
    graph.add_conditional_edges("synth_exploration", _route_on_failure, {"continue": "pnr_sdc", END: END})
    graph.add_conditional_edges("pnr_sdc", _route_on_failure, {"continue": "signoff_sdc", END: END})
    graph.add_conditional_edges("signoff_sdc", _route_on_failure, {"continue": "config_update", END: END})
    graph.add_conditional_edges("config_update", _route_on_failure, {"continue": "full_flow", END: END})
    graph.add_conditional_edges("full_flow", _route_on_failure, {"continue": END, END: END})
    return graph.compile()


def run_pipeline(initial_state: Optional[AsicState] = None) -> AsicState:
    state: AsicState = dict(initial_state or {})
    for node in (
        asic_input_node,
        asic_env_node,
        synth_exploration_node,
        pnr_sdc_node,
        signoff_sdc_node,
        config_update_node,
        full_flow_node,
    ):
        state.update(node(state))
        if state.get("status") == "failed":
            break
    return state


def asic_node(state: dict) -> dict:
    print("=" * 60)
    print("  ASIC FLOW - STARTING")
    print("=" * 60)

    initial_state: AsicState = {
        "project_root": state.get("project_root"),
        "filter_spec": state.get("specs"),
        "status": state.get("status", "success"),
    }
    final_state = run_pipeline(initial_state)
    result = {
        "status": final_state.get("status", state.get("status", "success")),
        "asic_status": final_state.get("asic_status", "failed" if final_state.get("status") == "failed" else "success"),
        "asic_report": final_state.get("asic_report", ""),
    }
    for key in (
        "asic_return_code",
        "selected_strategy",
        "pnr_sdc_path",
        "signoff_sdc_path",
        "full_flow_run_tag",
        "min_required_clock_period_ns",
        "max_achievable_frequency_mhz",
        "timing_limit_strategy",
    ):
        if key in final_state:
            result[key] = final_state[key]
    return result


def main() -> int:
    try:
        if StateGraph is not None and END is not None:
            final_state = create_workflow().invoke({})
        else:
            final_state = run_pipeline()
    except Exception as exc:
        print_failure(f"[ASIC] ERROR: {exc}")
        return 1

    return 0 if final_state.get("status") != "failed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
