"""SMS delivery via Twilio's REST API."""
from __future__ import annotations

from typing import List

import requests

from .config import Config

# Twilio accepts up to 1600 characters per message; leave room for a counter.
CHUNK_LIMIT = 1500
MAX_CHUNKS = 4


def split_message(text: str, limit: int = CHUNK_LIMIT) -> List[str]:
    """Split on line boundaries so a digest never breaks mid-item."""
    if len(text) <= limit:
        return [text]

    chunks: List[str] = []
    current = ""
    for line in text.split("\n"):
        # A single line longer than the limit gets hard-split as a last resort.
        while len(line) > limit:
            if current:
                chunks.append(current.rstrip())
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit:
            chunks.append(current.rstrip())
            current = line
        else:
            current = candidate
    if current.strip():
        chunks.append(current.rstrip())

    if len(chunks) > MAX_CHUNKS:
        chunks = chunks[:MAX_CHUNKS]
        chunks[-1] = chunks[-1][: limit - 20].rstrip() + "\n…(truncated)"

    total = len(chunks)
    return [f"({i}/{total}) {chunk}" for i, chunk in enumerate(chunks, 1)]


def send_sms(config: Config, text: str) -> int:
    """Send the digest. Returns the number of messages sent (0 in dry-run)."""
    chunks = split_message(text)

    if config.dry_run:
        for chunk in chunks:
            print("--- DRY RUN SMS ---")
            print(chunk)
        print(f"--- end ({len(chunks)} message(s), not sent) ---")
        return 0

    config.require_twilio()
    url = f"https://api.twilio.com/2010-04-01/Accounts/{config.twilio_sid}/Messages.json"
    for chunk in chunks:
        response = requests.post(
            url,
            data={"From": config.twilio_from, "To": config.twilio_to, "Body": chunk},
            auth=(config.twilio_sid, config.twilio_token),
            timeout=30,
        )
        if response.status_code >= 400:
            raise RuntimeError(f"Twilio error {response.status_code}: {response.text[:400]}")
    return len(chunks)
