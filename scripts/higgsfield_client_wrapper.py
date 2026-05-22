"""
Higgsfield SDK wrapper.

The official higgsfield_client SDK:
- Reads credentials from env vars: HF_KEY (single), or HF_API_KEY + HF_API_SECRET.
  Credentials cannot be passed programmatically.
- Exposes exactly two exception types: HiggsfieldClientError (everything except
  missing creds) and CredentialsMissedError (no creds configured).
- Provides a sync convenience function `subscribe(model, arguments=...)` that
  submits a job and waits for completion in one call.
- Returns a dict containing 'images' (list of {'url': ...}) plus other metadata.

This wrapper:
- Verifies env vars are set before calling the SDK
- Calls subscribe() with bounded retries on transient failures
- Returns a typed GenerationResult with the URL and cost (if present)

Reference: https://github.com/higgsfield-ai/higgsfield-client
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Optional


log = logging.getLogger("higgsfield-wrapper")


@dataclass
class GenerationResult:
    image_url: str
    cost: Optional[float]
    raw: dict


def credentials_present() -> bool:
    """True if either auth format is set in the environment."""
    if os.environ.get("HF_KEY"):
        return True
    if os.environ.get("HF_API_KEY") and os.environ.get("HF_API_SECRET"):
        return True
    return False


class HiggsfieldClient:
    """Thin wrapper around higgsfield_client.subscribe() with retries.

    The SDK uses env-var auth, so this class takes no credential arguments.
    Set HF_KEY (or HF_API_KEY + HF_API_SECRET) before instantiating.
    """

    def __init__(self, max_retries: int = 3, retry_backoff: float = 2.0):
        if not credentials_present():
            raise RuntimeError(
                "Higgsfield credentials not found in environment. "
                "Set HF_KEY=KEY_ID:KEY_SECRET, or HF_API_KEY and HF_API_SECRET separately."
            )
        self.max_retries = max_retries
        self.retry_backoff = retry_backoff

    def generate_image(
        self,
        prompt: str,
        model: str,
        aspect_ratio: str = "1:1",
        resolution: str = "1K",
    ) -> GenerationResult:
        """Submit a generation, wait for completion, return the URL + cost.

        Raises:
            RuntimeError: bad credentials or no credits — fatal, do not retry
            Exception: transient failures get retried up to max_retries times
        """
        # Lazy import — keeps dry-run and parse-only paths free of SDK dependency
        import higgsfield_client
        from higgsfield_client import HiggsfieldClientError, CredentialsMissedError

        arguments = {
            "prompt": prompt,
            "aspect_ratio": aspect_ratio,
            "resolution": resolution,
        }

        last_error: Optional[Exception] = None
        for attempt in range(1, self.max_retries + 1):
            try:
                result = higgsfield_client.subscribe(model, arguments=arguments)
                images = result.get("images", [])
                if not images:
                    raise RuntimeError("Higgsfield returned no images in result")
                return GenerationResult(
                    image_url=images[0]["url"],
                    cost=result.get("cost"),
                    raw=result,
                )

            except CredentialsMissedError as e:
                # Bad creds — fatal, don't retry
                raise RuntimeError(f"Higgsfield credentials rejected: {e}") from e

            except HiggsfieldClientError as e:
                # API errors. The message is the only signal we have for what
                # kind of failure it was — we surface fatal-looking ones early
                # rather than retrying through a known-dead state.
                msg = str(e).lower()
                if any(s in msg for s in ("credit", "quota", "insufficient")):
                    raise RuntimeError(f"Higgsfield out of credits: {e}") from e
                if any(s in msg for s in ("auth", "permission", "forbidden", "401", "403")):
                    raise RuntimeError(f"Higgsfield auth error: {e}") from e

                # Otherwise treat as transient and retry
                last_error = e
                wait = self.retry_backoff ** attempt
                log.warning(
                    f"Higgsfield error (attempt {attempt}/{self.max_retries}): "
                    f"{e}. Retrying in {wait:.1f}s."
                )
                time.sleep(wait)

        raise RuntimeError(
            f"Higgsfield failed after {self.max_retries} attempts: {last_error}"
        )
