# Copyright 2026 the module-applications authors
# SPDX-License-Identifier: Apache-2.0
"""The model behind the AI review, behind one interface, so another provider (DeepSeek, Qwen) plugs in without
touching the workflow or the report.

A provider runs one review: it gets MIKO's policy, the first message (application, rules, test results), a ToolBox
of read-only tools over the module's source, and the JSON schema the final answer must follow. It drives its own
loop of requests and tool calls within the limits, and returns a RunResult. It never sees a credential other than
its own API key, and never has a tool that writes, runs or reaches the network.
"""
import dataclasses
import json
import time


@dataclasses.dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cost_usd_estimate: float = 0.0
    turns: int = 0
    tool_calls: int = 0
    duration_seconds: float = 0.0

    def as_dict(self):
        return {**dataclasses.asdict(self), "cost_usd_estimate": round(self.cost_usd_estimate, 4), "cost_is_estimate": True,
                "duration_seconds": round(self.duration_seconds, 1)}


@dataclasses.dataclass
class RunResult:
    """complete: `text` is the final answer. incomplete: `reason` says why, and the review does not pass."""
    status: str
    text: str = ""
    reason: str = ""
    answered_by: str = None
    usage: Usage = dataclasses.field(default_factory=Usage)


@dataclasses.dataclass
class Limits:
    max_turns: int
    max_cost_usd: float
    timeout_seconds: int
    max_output_tokens: int
    retries: int


def price(prices, model, usage_delta):
    """USD for one response's usage. An unknown model (a fallback) is priced as the dearest known one."""
    table = prices.get(model) or max(prices.values(), key=lambda p: p["output"])
    return (usage_delta["input"] * table["input"] + usage_delta["output"] * table["output"]
            + usage_delta["cache_read"] * table["cache_read"] + usage_delta["cache_write"] * table["cache_write"]) / 1_000_000


class AnthropicProvider:
    """Claude through the Messages API, with our own read-only tools and structured output.

    Not the Claude Agent SDK: that brings Claude Code's own tools (Bash, Edit, WebFetch) and its settings loading,
    which the review must not have; a plain tool loop has exactly the three tools below and loads nothing from the
    module. Prompt caching keeps the growing conversation cheap; the server-side fallback answers a policy refusal
    with another model, and the report names the model that answered.
    """

    name = "anthropic"

    def __init__(self, model, effort, prices, fallbacks="default", client=None):
        self.model, self.effort, self.prices, self.fallbacks = model, effort, prices, fallbacks
        self.client = client

    def run(self, system, first_message, toolbox, output_schema, limits):
        import anthropic  # only the review job installs it

        client = self.client or anthropic.Anthropic(max_retries=limits.retries)
        tools = [{**spec, "strict": True} for spec in toolbox.specs()]
        messages = [{"role": "user", "content": first_message}]
        usage = Usage()
        start = time.monotonic()
        answered_by = None

        def done(status, text="", reason=""):
            usage.duration_seconds = time.monotonic() - start
            return RunResult(status=status, text=text, reason=reason, answered_by=answered_by, usage=usage)

        while True:
            if usage.turns >= limits.max_turns:
                return done("incomplete", reason=f"turn limit reached ({limits.max_turns})")
            if usage.cost_usd_estimate >= limits.max_cost_usd:
                return done("incomplete", reason=f"budget reached (estimate ${usage.cost_usd_estimate:.2f} of ${limits.max_cost_usd:.2f})")
            remaining = limits.timeout_seconds - (time.monotonic() - start)
            if remaining <= 5:
                return done("incomplete", reason=f"timeout ({limits.timeout_seconds}s)")
            request = dict(
                model=self.model,
                max_tokens=limits.max_output_tokens,
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                tools=tools,
                messages=messages,
                cache_control={"type": "ephemeral"},
                output_config={"effort": self.effort, "format": {"type": "json_schema", "schema": output_schema}},
            )
            try:
                api = client.with_options(timeout=remaining)
                if self.fallbacks:
                    response = api.beta.messages.create(betas=["server-side-fallback-2026-07-01"], fallbacks=self.fallbacks, **request)
                else:
                    response = api.messages.create(**request)
            except anthropic.APIStatusError as error:
                return done("incomplete", reason=f"API error {error.status_code} ({type(error).__name__})")
            except anthropic.APIConnectionError as error:
                return done("incomplete", reason=f"API unreachable ({type(error).__name__})")

            usage.turns += 1
            answered_by = response.model
            delta = {
                "input": response.usage.input_tokens or 0,
                "output": response.usage.output_tokens or 0,
                "cache_read": getattr(response.usage, "cache_read_input_tokens", 0) or 0,
                "cache_write": getattr(response.usage, "cache_creation_input_tokens", 0) or 0,
            }
            usage.input_tokens += delta["input"]
            usage.output_tokens += delta["output"]
            usage.cache_read_input_tokens += delta["cache_read"]
            usage.cache_creation_input_tokens += delta["cache_write"]
            usage.cost_usd_estimate += price(self.prices, response.model, delta)

            if response.stop_reason == "refusal":
                category = getattr(response.stop_details, "category", None) if response.stop_details else None
                return done("incomplete", reason=f"the model declined (refusal, category {category})")
            if response.stop_reason == "max_tokens":
                return done("incomplete", reason="the answer was cut off at max_tokens")
            messages.append({"role": "assistant", "content": response.content})
            if response.stop_reason == "pause_turn":
                continue
            calls = [block for block in response.content if block.type == "tool_use"]
            if response.stop_reason == "tool_use" and calls:
                results = []
                for call in calls:
                    usage.tool_calls += 1
                    text, is_error = toolbox.execute(call.name, call.input)
                    results.append({"type": "tool_result", "tool_use_id": call.id, "content": text, "is_error": is_error})
                messages.append({"role": "user", "content": results})
                continue
            text = "".join(block.text for block in response.content if block.type == "text")
            return done("complete", text=text) if text.strip() else done("incomplete", reason="empty answer")


class ScriptedProvider:
    """For tests: plays a fixed list of steps, each either a tool call {"tool": name, "input": {...}} or a final
    answer {"answer": text}, with a fixed cost per step."""

    name = "scripted"

    def __init__(self, steps, cost_per_turn=0.01, model="scripted-model"):
        self.steps, self.cost_per_turn, self.model = steps, cost_per_turn, model
        self.tool_results = []

    def run(self, system, first_message, toolbox, output_schema, limits):
        usage = Usage()
        for step in self.steps:
            if usage.turns >= limits.max_turns:
                return RunResult("incomplete", reason=f"turn limit reached ({limits.max_turns})", usage=usage)
            if usage.cost_usd_estimate >= limits.max_cost_usd:
                return RunResult("incomplete", reason="budget reached", usage=usage)
            usage.turns += 1
            usage.cost_usd_estimate += self.cost_per_turn
            if "tool" in step:
                usage.tool_calls += 1
                self.tool_results.append(toolbox.execute(step["tool"], step["input"]))
            elif "answer" in step:
                answer = step["answer"] if isinstance(step["answer"], str) else json.dumps(step["answer"])
                return RunResult("complete", text=answer, answered_by=self.model, usage=usage)
            elif "error" in step:
                return RunResult("incomplete", reason=step["error"], usage=usage)
        return RunResult("incomplete", reason="no answer", usage=usage)


def make(config, client=None):
    if config["provider"] == "anthropic":
        return AnthropicProvider(config["model"], config["effort"], config["prices"], config.get("fallbacks"), client=client)
    raise ValueError(f"unknown provider {config['provider']!r}")
