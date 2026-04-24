"""
core/retry.py — Retry decorator with exponential backoff for InstaAgent.

Usage:
    from core.retry import retry
    from requests import RequestException

    @retry(max_attempts=3, backoff_factor=2, exceptions=(RequestException,))
    def call_api():
        ...
"""

import functools
import logging
import time
from typing import Callable, Optional, Tuple, Type

logger = logging.getLogger("retry")


def retry(
    max_attempts: int = 3,
    backoff_factor: float = 2.0,
    initial_wait: float = 1.0,
    max_wait: float = 60.0,
    exceptions: Tuple[Type[Exception], ...] = (Exception,),
    on_failure: Optional[Callable] = None,
):
    """
    Decorator factory — retry a function with exponential backoff.

    Args:
        max_attempts:   Total number of attempts (including the first try).
        backoff_factor: Multiplier applied to wait time after each failure.
        initial_wait:   Wait time (seconds) before the first retry.
        max_wait:       Cap on wait time between retries.
        exceptions:     Tuple of exception types to catch and retry on.
        on_failure:     Optional callback(attempt: int, exc: Exception) called
                        after each failed attempt (before sleeping).

    Raises:
        The last caught exception if all attempts are exhausted.

    Example:
        @retry(max_attempts=3, backoff_factor=2, exceptions=(RequestException,))
        def fetch():
            return requests.get(url, timeout=10)
    """

    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            wait = initial_wait
            last_exc: Optional[Exception] = None

            for attempt in range(1, max_attempts + 1):
                try:
                    return func(*args, **kwargs)
                except exceptions as exc:
                    last_exc = exc

                    if attempt == max_attempts:
                        logger.error(
                            f"[{func.__qualname__}] All {max_attempts} attempts failed. "
                            f"Last error: {exc}"
                        )
                        raise

                    if on_failure is not None:
                        try:
                            on_failure(attempt, exc)
                        except Exception:
                            pass  # never let the callback crash the retry loop

                    sleep_time = min(wait, max_wait)
                    logger.warning(
                        f"[{func.__qualname__}] Attempt {attempt}/{max_attempts} failed: "
                        f"{exc}. Retrying in {sleep_time:.1f}s..."
                    )
                    time.sleep(sleep_time)
                    wait *= backoff_factor

            # Should never reach here, but satisfy type checker
            raise last_exc  # type: ignore

        return wrapper

    return decorator
