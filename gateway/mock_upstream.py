"""Stand-in for the Claude Messages API.

Returns responses shaped like POST /v1/messages, including the `usage` block
the gateway meters on. Token counts are simulated: input is ~4 chars per token,
output is a random share of max_tokens. Seeded so every demo run is identical.
"""
import asyncio
import os
import random
import uuid

_rng = random.Random(int(os.environ.get("MOCK_SEED", "7")))


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    return " ".join(b.get("text", "") for b in content if isinstance(b, dict))


async def create_message(body: dict) -> dict:
    await asyncio.sleep(_rng.uniform(0.02, 0.08))

    system = body.get("system") or ""
    prompt = _text_of(system) + " ".join(_text_of(m.get("content", "")) for m in body["messages"])
    input_tokens = max(1, len(prompt) // 4)

    max_tokens = body["max_tokens"]
    output_tokens = max(1, int(max_tokens * _rng.uniform(0.5, 1.0)))
    first_user = _text_of(body["messages"][-1].get("content", ""))[:50]

    return {
        "id": "msg_mock_" + uuid.uuid4().hex[:16],
        "type": "message",
        "role": "assistant",
        "model": body["model"],
        "content": [{"type": "text", "text": f"[mock {body['model']}] Simulated reply to: {first_user!r}"}],
        "stop_reason": "max_tokens" if output_tokens == max_tokens else "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
    }
