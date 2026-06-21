"""
API utility functions for handling API calls with retry logic
"""
import time
from litellm import completion
from termcolor import colored
from config.settings import API_MAX_RETRIES, API_RETRY_DELAY, API_TIMEOUT

def call_llm_with_retry(model, messages, api_key, max_retries=None, retry_delay=None, timeout=None):
    """
    Call LLM API with automatic retry on connection errors.

    Args:
        model: Model name to use
        messages: List of message dicts
        api_key: API key
        max_retries: Max retry attempts (defaults to API_MAX_RETRIES)
        retry_delay: Seconds between retries (defaults to API_RETRY_DELAY)
        timeout: Request timeout in seconds (defaults to API_TIMEOUT)

    Returns:
        Response object from litellm

    Raises:
        Exception: If all retries fail
    """
    max_retries = max_retries or API_MAX_RETRIES
    retry_delay = retry_delay or API_RETRY_DELAY
    timeout = timeout or API_TIMEOUT

    last_error = None

    for attempt in range(max_retries):
        try:
            # Add timeout to the completion call
            response = completion(
                model=model,
                messages=messages,
                api_key=api_key,
                timeout=timeout,
                stream=False  # Disable streaming to avoid chunked read issues
            )
            return response

        except Exception as e:
            last_error = e
            error_msg = str(e).lower()

            # Check if it's a retryable error
            retryable_errors = [
                "peer closed connection",
                "incomplete chunked read",
                "connection",
                "timeout",
                "internal server error"
            ]

            is_retryable = any(err in error_msg for err in retryable_errors)

            if is_retryable and attempt < max_retries - 1:
                print(colored(f"⚠️  API connection issue (attempt {attempt + 1}/{max_retries}): {e}", "yellow"))
                print(colored(f"   Retrying in {retry_delay} seconds...", "yellow"))
                time.sleep(retry_delay)
                # Exponential backoff
                retry_delay *= 1.5
            else:
                # Non-retryable error or last attempt
                raise e

    # If we get here, all retries failed
    raise last_error
