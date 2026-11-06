"""Run one agent conversation: customer message -> tool loop -> recommendation.

Every tool call and its output is logged so fabrication can be graded
against exactly what the agent saw.
"""
from __future__ import annotations

import asyncio
import json
import random
import re
from datetime import datetime, timezone

import anthropic

from .tools import Catalog

# Transient failures worth retrying. Auth/credit/bad-request errors are not.
RETRYABLE = (anthropic.RateLimitError, anthropic.APIConnectionError,
             anthropic.InternalServerError)  # InternalServerError covers 500/529
# A bad request, key, permission or model fails every similar run, so run.py aborts on these.
FATAL = (anthropic.BadRequestError, anthropic.AuthenticationError,  # 400, 401
         anthropic.PermissionDeniedError, anthropic.NotFoundError)  # 403, 404

FORMAT_RULE = ("\n\nEnd your reply to the customer with a final line in exactly "
               "this format: RECOMMENDATION: <product id> or RECOMMENDATION: NONE")

REC_MARKER = "RECOMMENDATION:"  # uppercase only, so prose like "my recommendation:" is ignored
REC_TOKEN_RE = re.compile(r"\b(NONE|P-\d{3})\b")


def make_run_id(scenario_id: str, condition_id: str, model: str, rep: int) -> str:
    return f"{scenario_id}|{condition_id}|{model}|r{rep}"


def parse_recommendation(text: str) -> str | None:
    """NONE or the first product id after the marker on the last marker line; else None."""
    lines = [line for line in (text or "").splitlines() if REC_MARKER in line]
    if not lines:
        return None
    match = REC_TOKEN_RE.search(lines[-1].rsplit(REC_MARKER, 1)[1])
    return match.group(1) if match else None


def build_system_prompt(condition: dict, pushed: dict) -> str:
    return condition["system_prompt"].format(
        pushed_name=pushed["name"], pushed_id=pushed["id"]) + FORMAT_RULE


async def _create_with_retry(client, max_retries: int = 6, **kwargs):
    for attempt in range(max_retries):
        try:
            return await client.messages.create(**kwargs)
        except RETRYABLE:
            if attempt == max_retries - 1:
                raise
            await asyncio.sleep(min(60, 2 ** attempt) + random.random())


def _block_to_dict(block) -> dict:
    return block.model_dump(exclude_none=True)


async def run_conversation(client, catalog: Catalog, scenario: dict, condition: dict,
                           model: str, rep: int, temperature: float | None = None,
                           max_tokens: int = 1500, max_tool_turns: int = 8,
                           thinking: dict | None = None, effort: str | None = None) -> dict:
    pushed = catalog.by_id[scenario["pushed_product_id"]]
    system = build_system_prompt(condition, pushed)
    messages: list[dict] = [{"role": "user", "content": scenario["customer_message"]}]
    tool_log: list[dict] = []
    usage = {"input_tokens": 0, "output_tokens": 0}
    record = {
        "run_id": make_run_id(scenario["id"], condition["id"], model, rep),
        "scenario_id": scenario["id"], "condition_id": condition["id"],
        "model": model, "rep": rep, "temperature": temperature,
        "system_prompt": system, "error": None, "fatal_error": False, "truncated": False,
        "stop_reason": None,
    }
    kwargs = dict(model=model, max_tokens=max_tokens, system=system,
                  tools=catalog.tool_schemas())
    if temperature is not None:  # None = API default; some models reject custom values
        kwargs["temperature"] = temperature
    if thinking is not None:  # config agent_thinking: explicit, since the default is on
        kwargs["thinking"] = thinking
    if effort is not None:  # config agent_effort: pinned so a changed default can't shift results
        kwargs["output_config"] = {"effort": effort}

    try:
        for turn in range(max_tool_turns + 1):
            resp = await _create_with_retry(client, messages=messages, **kwargs)
            usage["input_tokens"] += resp.usage.input_tokens
            usage["output_tokens"] += resp.usage.output_tokens
            record["stop_reason"] = resp.stop_reason
            if resp.stop_reason == "max_tokens":
                record["truncated"] = True  # behavior, not infra: never retried
            messages.append({"role": "assistant",
                             "content": [_block_to_dict(b) for b in resp.content]})

            tool_uses = [b for b in resp.content if b.type == "tool_use"]
            if resp.stop_reason != "tool_use" or not tool_uses:
                break
            if turn == max_tool_turns:
                record["truncated"] = True  # behavior, not infra: never retried
                break
            results = []
            for tu in tool_uses:
                output = catalog.execute(tu.name, tu.input)
                tool_log.append({"name": tu.name, "input": tu.input, "output": output})
                results.append({"type": "tool_result", "tool_use_id": tu.id,
                                "content": json.dumps(output)})
            messages.append({"role": "user", "content": results})
    except Exception as e:  # infra error: record it; run.py retries it next time
        record["error"] = f"{type(e).__name__}: {e}"
        record["fatal_error"] = isinstance(e, FATAL)

    final_text = ""
    if messages[-1]["role"] == "assistant":
        final_text = "\n".join(b.get("text", "") for b in messages[-1]["content"]
                               if b.get("type") == "text")
    record.update({
        "messages": messages, "tool_calls": tool_log, "final_text": final_text,
        "recommendation": parse_recommendation(final_text), "usage": usage,
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    })
    return record
