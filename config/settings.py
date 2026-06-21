"""
Configuration settings for the RTL Verification Workflow
"""
import os
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# ===== PROMPT SELECTION =====
USE_COMPACT_PROMPTS = True  # Set to False for detailed prompts (uses more tokens)

# ===== API CONFIGURATION =====
# The API key variable name can be reused for different providers
# litellm will use the same key for the model specified in MODEL_NAME
API_KEY = os.getenv("API_KEY")  # Can be Gemini, DeepSeek, GLM-4, etc.
MODEL_NAME = "deepseek/deepseek-coder"  # Using DeepSeek API 

# API key validation
if not API_KEY:
    print("\033[91mError: API Key not found.\033[0m")
    print("Please add this to your .env file: API_KEY=your_key_here")

# ===== RETRY CONFIGURATION =====
MAX_RTL_ATTEMPTS = 3       # Max full RTL regenerations
MAX_TB_ATTEMPTS = 3        # Max full TB regenerations
MAX_RTL_FIX_ATTEMPTS = 2   # Max RTL fix attempts before regenerating
MAX_TB_FIX_ATTEMPTS = 2    # Max TB fix attempts before regenerating

# ===== FILE PATHS =====
RTL_OUTPUT_DIR = "."
TB_OUTPUT_DIR = "."

# ===== SIMULATION SETTINGS =====
DEFAULT_SIM_TIMEOUT = 300  # seconds (increased for complex testbenches)
COCOTB_SIM_TIMEOUT = 180   # seconds (increased from 60 for cocotb simulations)

# ===== VERILATOR SETTINGS =====
VERILATOR_LINT_FLAGS = [
    "--lint-only", 
    "-Wall", 
    "-Wno-fatal",          # Critical: Don't stop the pipeline on warnings
    "-Wno-DECLFILENAME",   # Safe: OpenLane uses file lists; module names don't need to match filenames
    "-Wno-UNUSED",         # Safe: Yosys performs "Dead Logic Elimination" and removes unused signals automatically
    "-Wno-PINMISSING",     # Safe: Synthesis treats unconnected pins as floating (Z) or 0
    "-Wno-EOFNEWLINE",     # Safe: Text formatting issue irrelevant to hardware
    "-Wno-WIDTH",          # Safe-ish: Yosys automatically handles implicit truncation/padding (e.g. 32->16 bits)
    "-Wno-TIMESCALEMOD"    # Safe: Synthesis tools ignore simulation timescales
]

# ===== MODEL-SPECIFIC CONFIGURATIONS =====
MODEL_CONFIGS = {
    "zhipuai/glm-4": {
        "temperature": 0.5,
        "max_tokens": 4000
    },
    "gemini/gemini-2.5-pro": {
        "temperature": 0.5,
        "max_tokens": 8000
    },
    "deepseek/deepseek-chat": {
        "temperature": 0.5,
        "max_tokens": 8000
    }
}

# Get current model config
CURRENT_MODEL_CONFIG = MODEL_CONFIGS.get(MODEL_NAME, {"temperature": 0.5, "max_tokens": 8000})

# ===== API RETRY CONFIGURATION =====
API_MAX_RETRIES = 3          # Max retries for API calls
API_RETRY_DELAY = 2          # Seconds to wait between retries
API_TIMEOUT = 120            # API call timeout in seconds
