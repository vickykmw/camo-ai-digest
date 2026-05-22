"""
Higgsfield SDK wrapper — clean interface, retries, cost tracking.

The Higgsfield SDK is fine but its surface area is large. This wrapper:
- Exposes one method, generate_image()
- Handles transient errors with bounded retries
- Returns a typed result with the URL and the actual credit cost
- Raises cleanly on auth/credit failures so the caller can surface them
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Optional


log = logging.getLogger("higgsfield-wrapper")


@dataclass
class GenerationResult:
    image_url: str
    cost: Optional[float]
    job_id: Optional[str]


class HiggsfieldClient:
    """Thin wrapper around higgsfield_client with retries + typed return."""

    def __init__(
        self,
        credentials: str,
        max_retries: int = 3,
        retry_backoff: float = 2.0,
    ):
        # SDK reads credentials from env vars — the orchestrator sets HF_CREDENTIALS
        # before instantiating us, so this is mostly a sanity check.
        if not credentials:
            raise ValueError("Empty Higgsfield credentials")
        self.max_retries = max_retries
        self.retry_backoff = retry_backoff

    def generate_image(
        self,
        prompt: str,
        model: str,
        aspect_ratio: str = "1:1",
        resolution: str = "1K",
    ) -> GenerationResult:
        """Submit a generation, poll to completion, return URL + cost.

        Raises:
            AuthenticationError: bad credentials — fatal, do not retry the run
            NotEnoughCreditsError: out of credits — fatal, do not retry the run
            APIError: transient API failure — retried up to max_retries
        """
        # Lazy import — only loaded when we actually want to generate, so
        # the dry-run path and the parse-only tests don't require the SDK.
        import higgsfield_client
        from higgsfield_client import (
            AuthenticationError,
            NotEnoughCreditsError,
            BadInputError,
            ValidationError,
            APIError,
        )

        arguments = {
            "prompt": prompt,
            "aspect_ratio": aspect_ratio,
            "resolution": resolution,
        }

        last_error: Optional[Exception] = None
        for attempt in range(1, self.max_retries + 1):
            try:
                result = higgsfield_client.subscribe(model, arguments=arguments)
                # SDK returns {'images': [{'url': ...}], 'cost': ..., 'job_id': ...}
                images = result.get("images", [])
                if not images:
                    raise APIError("Higgsfield returned no images in result")
                return GenerationResult(
                    image_url=images[0]["url"],
                    cost=result.get("cost"),
                    job_id=result.get("job_id"),
                )

            except (AuthenticationError, NotEnoughCreditsError) as e:
                # Fatal — let it bubble up immediately
                log.error(f"Fatal Higgsfield error: {type(e).__name__}: {e}")
                raise

            except (BadInputError, ValidationError) as e:
                # Bad prompt or args — retrying won't help, but we shouldn't
                # tank the whole run for one bad paper. Surface to caller.
                log.error(f"Higgsfield rejected input: {e}")
                raise

            except APIError as e:
                last_error = e
                wait = self.retry_backoff ** attempt
                log.warning(
                    f"Higgsfield API error (attempt {attempt}/{self.max_retries}): "
                    f"{e}. Retrying in {wait:.1f}s."
                )
                time.sleep(wait)

        # Exhausted retries
        raise APIError(f"Higgsfield failed after {self.max_retries} attempts: {last_error}")
