from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any, Literal

from langchain_anthropic import ChatAnthropic
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_openai import ChatOpenAI
from langgraph.types import interrupt

try:
    from .blueprints import build_bundle_context, create_project_bundle
    from .config import Provider, clear_settings_cache, get_settings
    from .roles import (
        DISCUSSION_ROLE_KEYS,
        QUESTIONS_SYSTEM,
        ROLES,
        SPEAKER_NAME_TOKEN,
        SPEAKER_ORDER,
        SUMMARY_SYSTEM,
        TOKEN_TO_SPEAKER,
    )
    from .state import ArchChoice, ExecutionStrategy, IdeaDiscussionState, PlanDecision
    from .text_utils import normalize_content
except ImportError:
    from blueprints import build_bundle_context, create_project_bundle
    from config import Provider, clear_settings_cache, get_settings
    from roles import (
        DISCUSSION_ROLE_KEYS,
        QUESTIONS_SYSTEM,
        ROLES,
        SPEAKER_NAME_TOKEN,
        SPEAKER_ORDER,
        SUMMARY_SYSTEM,
        TOKEN_TO_SPEAKER,
    )
    from state import ArchChoice, ExecutionStrategy, IdeaDiscussionState, PlanDecision
    from text_utils import normalize_content

LOGGER = logging.getLogger(__name__)

@dataclass(frozen=True)
class LlmRuntime:
    llm: BaseChatModel
    provider: Provider
    model: str
    max_tokens: int


@dataclass(frozen=True)
class AgentRuntime(LlmRuntime):
    system_prompt: str


def _infer_provider_from_model(model: str) -> Provider | None:
    m = (model or "").strip().lower()
    if not m:
        return None
    if "gemini" in m:
        return "google"
    if "claude" in m:
        return "anthropic"
    if m.startswith("gpt-") or m.startswith("o"):
        return "openai"
    return None


def _resolve_provider(configured_provider: Provider, model: str) -> Provider:
    inferred = _infer_provider_from_model(model)
    if inferred and inferred != configured_provider:
        LOGGER.warning(
            "Provider/model mismatch fixed automatically: provider=%s model=%s -> provider=%s",
            configured_provider,
            model,
            inferred,
        )
        return inferred
    return configured_provider


def _build_llm(provider: Provider, model: str, max_tokens: int) -> BaseChatModel:
    settings = get_settings()
    resolved_provider = _resolve_provider(provider, model)
    if resolved_provider == "openai":
        return ChatOpenAI(
            model=model,
            api_key=settings.openai_api_key,
            temperature=settings.temperature,
            max_tokens=max_tokens,
        )
    if resolved_provider == "anthropic":
        return ChatAnthropic(
            model=model,
            api_key=settings.anthropic_api_key,
            temperature=settings.temperature,
            max_tokens=max_tokens,
        )
    return ChatGoogleGenerativeAI(
        model=model,
        google_api_key=settings.google_api_key,
        temperature=settings.temperature,
    )


def _invoke_kwargs(runtime: LlmRuntime, *, max_tokens: int | None = None) -> dict[str, Any]:
    requested_tokens = max_tokens or runtime.max_tokens
    if runtime.provider == "google":
        # LangChain's Google wrapper can ignore init-time generation kwargs, so pass
        # the output cap on each request to make Gemini honor the configured limit.
        return {"max_output_tokens": requested_tokens}
    return {}


def _response_finish_reason(message: AIMessage) -> str:
    metadata = getattr(message, "response_metadata", None) or {}
    for key in ("finish_reason", "finishReason"):
        value = metadata.get(key)
        if value is not None:
            return str(value).strip().upper()
    return ""


def _looks_complete(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return False
    return stripped.endswith((".", "!", "?", ")", "]", "}", "`", '"'))


def _invoke_with_runtime(
    runtime: LlmRuntime,
    messages: list[BaseMessage],
    *,
    max_tokens: int | None = None,
) -> AIMessage:
    requested_tokens = max_tokens or runtime.max_tokens
    response = runtime.llm.invoke(messages, **_invoke_kwargs(runtime, max_tokens=requested_tokens))
    finish_reason = _response_finish_reason(response)
    normalized = _extract_text_from_message(response)
    if runtime.provider != "google" or finish_reason != "MAX_TOKENS" or _looks_complete(normalized):
        return response

    retry_max_tokens = min(max(requested_tokens * 2, requested_tokens + 512), 8192)
    if retry_max_tokens <= requested_tokens:
        return response

    LOGGER.warning(
        "[Google] model=%s hit MAX_TOKENS with incomplete output; retrying with max_output_tokens=%s",
        runtime.model,
        retry_max_tokens,
    )
    return runtime.llm.invoke(messages, **_invoke_kwargs(runtime, max_tokens=retry_max_tokens))


@cache
def get_runtime(role_key: str) -> AgentRuntime:
    settings = get_settings()
    spec = ROLES[role_key]
    configured_provider = getattr(settings, spec.provider_setting)
    model = getattr(settings, spec.model_setting)
    max_tokens = getattr(settings, spec.max_tokens_setting)
    return AgentRuntime(
        llm=_build_llm(configured_provider, model, max_tokens),
        provider=_resolve_provider(configured_provider, model),
        model=model,
        max_tokens=max_tokens,
        system_prompt=spec.system_prompt,
    )


def get_discussion_runtimes() -> dict[str, AgentRuntime]:
    return {ROLES[key].display_name: get_runtime(key) for key in DISCUSSION_ROLE_KEYS}


def _next_speaker(current_speaker: str) -> str:
    idx = SPEAKER_ORDER.index(current_speaker)
    return SPEAKER_ORDER[(idx + 1) % len(SPEAKER_ORDER)]


def display_speaker_name(name: str | None) -> str:
    if not name:
        return "Panelist"
    return TOKEN_TO_SPEAKER.get(name, name)


def _extract_text_from_message(message: AIMessage) -> str:
    content_text = normalize_content(message.content).strip()
    if content_text:
        return content_text
    # Some providers may place text outside `content`.
    extra = getattr(message, "additional_kwargs", {}) or {}
    for key in ("text", "output_text", "completion"):
        val = extra.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    # Some OpenAI responses can contain blocks in `refusal` even when content is empty.
    refusal_val = extra.get("refusal")
    if isinstance(refusal_val, str) and refusal_val.strip():
        return refusal_val.strip()
    if isinstance(refusal_val, list):
        bits: list[str] = []
        for item in refusal_val:
            if isinstance(item, str) and item.strip():
                bits.append(item.strip())
            elif isinstance(item, dict):
                txt = item.get("text")
                if isinstance(txt, str) and txt.strip():
                    bits.append(txt.strip())
        if bits:
            return "\n".join(bits)
    return ""


def _snip(text: str, limit: int = 450) -> str:
    t = text.strip()
    if len(t) <= limit:
        return t
    return t[:limit] + "...[truncated]"


def _log_openai_response(tag: str, model: str, message: AIMessage) -> None:
    normalized = _extract_text_from_message(message)
    extra = getattr(message, "additional_kwargs", {}) or {}
    usage = getattr(message, "usage_metadata", None)
    LOGGER.info(
        "[OpenAI:%s] model=%s content_len=%s normalized_len=%s usage=%s extra_keys=%s",
        tag,
        model,
        len(normalize_content(message.content)),
        len(normalized),
        usage,
        sorted(list(extra.keys())),
    )
    if normalized:
        LOGGER.info("[OpenAI:%s] normalized_preview=%s", tag, _snip(normalized))


def _history_markdown(messages: list[BaseMessage]) -> str:
    rows: list[str] = []
    turn_num = 0
    for msg in messages:
        if isinstance(msg, HumanMessage):
            rows.append(f"**[Human / anchor idea]**\n{normalize_content(msg.content)}")
        elif isinstance(msg, AIMessage):
            turn_num += 1
            name = display_speaker_name(msg.name)
            rows.append(f"**[{turn_num}. {name}]**\n{normalize_content(msg.content)}")
    return "\n\n".join(rows)


def _extract_questions(summary_text: str) -> list[str]:
    raw_lines = re.findall(r"^\s*(?:\d+[\.\)]|[-*])\s+(.+?)\s*$", summary_text, flags=re.MULTILINE)
    cleaned: list[str] = []
    for line in raw_lines:
        q = re.sub(r"\s+", " ", line).strip()
        if not q:
            continue
        if not q.endswith("?"):
            q = q.rstrip(".") + "?"
        cleaned.append(q)
    if cleaned:
        return cleaned[:5]
    return []


def _ensure_questions(questions: list[str]) -> list[str]:
    if questions:
        return [f"{i}. {q}" for i, q in enumerate(questions[:5], start=1)]
    # Deterministic fallback: guarantee decisions are asked before MVP planning.
    return [
        "1. What are the top 1-2 user workflows the MVP must complete end-to-end without manual workarounds?",
        "2. Which capabilities are mandatory in v0, and which must be deferred to avoid scope creep?",
        "3. What technical constraints (latency, data model, auth, integrations) must shape MVP architecture from day one?",
        "4. Which implementation milestones and validation checks define build readiness for launch?",
        "5. Which business model assumption matters most initially, and how will the MVP test it with minimal added scope?",
    ]


def _default_plan_offer_question() -> str:
    return (
        "Should I generate an agent-ready project pack next with scoped `AGENTS.md` files and a full `plan.md` "
        "covering task order, data contracts, and test expectations for the MVP?"
    )


def _project_bundle_root() -> Path:
    return Path(__file__).resolve().parent / "project_blueprints"


def _generate_project_doc(
    *,
    runtime: AgentRuntime,
    system_prompt: str,
    user_prompt: str,
    fallback: str,
    log_tag: str,
) -> str:
    response = _invoke_with_runtime(
        runtime,
        [
            SystemMessage(content=system_prompt),
            HumanMessage(content=user_prompt),
        ],
    )
    if isinstance(runtime.llm, ChatOpenAI):
        _log_openai_response(log_tag, runtime.model, response)
    text = _extract_text_from_message(response) or normalize_content(response.content).strip()
    return text or fallback


def discussion_node(state: IdeaDiscussionState) -> dict[str, Any]:
    speaker = state["next_speaker"]
    runtimes = get_discussion_runtimes()
    runtime = runtimes[speaker]

    thread_md = _history_markdown(state["discussion_history"])
    speakers_count = len(SPEAKER_ORDER)
    total_rounds = max(1, state["max_rounds"] // speakers_count)
    current_round = min(total_rounds, state["turn_count"] // speakers_count + 1)
    round_rules = f"- This is your round {current_round} of {total_rounds} in this panel.\n"
    if current_round >= total_rounds:
        round_rules += (
            "- This is your FINAL round: converge instead of expanding. State your position on the open "
            "disagreements, what you would commit to building, and what you would cut. Do not open new threads.\n"
        )
    turn_prompt = (
        f"Anchor idea:\n{state['user_idea']}\n\n"
        "Panel transcript so far (chronological):\n"
        f"{thread_md}\n\n"
        f"You are **{speaker}**. Write the next panel turn.\n"
        "Rules for this turn:\n"
        f"{round_rules}"
        "- Explicitly reference at least one prior panelist by role name (PM, Tech Lead, Skeptic).\n"
        "- Build on or challenge a concrete claim from the transcript.\n"
        "- Add net-new decisions/assumptions/tradeoffs rather than repeating prior text.\n"
        "- Keep it concise, structured, and decision-oriented.\n"
        "- Use at most 2 short sections and at most 6 bullets total.\n"
    )
    response = _invoke_with_runtime(
        runtime,
        [
            SystemMessage(content=runtime.system_prompt),
            HumanMessage(content=turn_prompt + "\n\nReturn the final answer text explicitly."),
        ]
    )
    if isinstance(runtime.llm, ChatOpenAI):
        _log_openai_response("primary", runtime.model, response)
    normalized = _extract_text_from_message(response)
    if not normalized and isinstance(runtime.llm, ChatOpenAI):
        LOGGER.warning(
            "[OpenAI] Primary model returned reasoning-only output (no visible text). Retrying same model with strict final-answer instruction."
        )
        same_model_retry = _invoke_with_runtime(
            runtime,
            [
                SystemMessage(content=runtime.system_prompt + "\nAlways provide visible final answer text."),
                HumanMessage(content=turn_prompt + "\n\nDo not return hidden reasoning; output only final answer markdown."),
            ]
        )
        _log_openai_response("primary_retry", runtime.model, same_model_retry)
        normalized = _extract_text_from_message(same_model_retry)
    if not normalized and isinstance(runtime.llm, ChatOpenAI):
        settings = get_settings()
        fallback_model = settings.openai_fallback_model
        LOGGER.warning(
            "[OpenAI] Empty output from primary model=%s speaker=%s; retrying with fallback model=%s",
            runtime.model,
            speaker,
            fallback_model,
        )
        # Retry once with a stable OpenAI model if the primary model emitted empty text.
        fallback_llm = ChatOpenAI(
            model=fallback_model,
            api_key=settings.openai_api_key,
            temperature=settings.temperature,
            max_tokens=settings.discussion_max_tokens,
        )
        retry = fallback_llm.invoke(
            [
                SystemMessage(content=runtime.system_prompt),
                HumanMessage(content=turn_prompt + "\n\nReturn plain Markdown text only."),
            ]
        )
        _log_openai_response("fallback", fallback_model, retry)
        normalized = _extract_text_from_message(retry)

    ai_message = AIMessage(
        content=normalized or "I could not produce a usable response for this turn.",
        name=SPEAKER_NAME_TOKEN.get(speaker, speaker.lower().replace(" ", "_")),
    )

    return {
        "discussion_history": [ai_message],
        "next_speaker": _next_speaker(speaker),
        "turn_count": state["turn_count"] + 1,
        "stage": "discussion",
    }


def summarizer_node(state: IdeaDiscussionState) -> dict[str, Any]:
    summarizer = get_runtime("summarizer")
    thread_md = _history_markdown(state["discussion_history"])
    summary_response = _invoke_with_runtime(
        summarizer,
        [
            SystemMessage(content=SUMMARY_SYSTEM),
            HumanMessage(
                content=(
                    f"Original idea:\n{state['user_idea']}\n\n"
                    f"Full discussion thread:\n\n{thread_md}\n\n"
                    "Produce the brief in the requested format. Do not include question lists."
                )
            ),
        ]
    )
    if isinstance(summarizer.llm, ChatOpenAI):
        _log_openai_response("summary", summarizer.model, summary_response)
    summary_text = _extract_text_from_message(summary_response) or normalize_content(summary_response.content)
    questions_response = _invoke_with_runtime(
        summarizer,
        [
            SystemMessage(content=QUESTIONS_SYSTEM),
            HumanMessage(
                content=(
                    f"Idea:\n{state['user_idea']}\n\n"
                    f"Discussion highlights:\n{thread_md}\n\n"
                    f"Current synthesized summary:\n{summary_text}\n\n"
                    "Generate the 5 MVP-preparation questions now."
                )
            ),
        ]
    )
    questions_text = _extract_text_from_message(questions_response) or normalize_content(questions_response.content)
    questions = _ensure_questions(_extract_questions(questions_text))
    return {"summary": summary_text, "generated_questions": questions, "stage": "summary"}


def _run_architect(
    *,
    user_idea: str,
    summary: str,
    questions: list[str],
    user_answers: str,
) -> str:
    architect = get_runtime("architect")
    normalized_questions = _ensure_questions(questions)
    question_block = "\n".join(normalized_questions) if normalized_questions else "No explicit questions provided."
    response = _invoke_with_runtime(
        architect,
        [
            SystemMessage(content=architect.system_prompt),
            HumanMessage(
                content=(
                    f"Initial user idea:\n{user_idea.strip()}\n\n"
                    f"Discussion summary:\n{summary.strip()}\n\n"
                    f"Questions asked to user:\n{question_block}\n\n"
                    f"User responses:\n{user_answers.strip()}\n\n"
                    "Create the two architecture options now. Keep this at high level for MVP planning."
                )
            ),
        ]
    )
    if isinstance(architect.llm, ChatOpenAI):
        _log_openai_response("architect", architect.model, response)
    architecture_text = _extract_text_from_message(response)
    if architecture_text:
        return architecture_text
    return normalize_content(response.content).strip() or (
        "Architect could not generate a usable architecture proposal."
    )


def route_after_discussion(state: IdeaDiscussionState) -> Literal["discussion", "summarizer"]:
    if state["turn_count"] < state["max_rounds"]:
        return "discussion"
    return "summarizer"


def collect_answers_node(state: IdeaDiscussionState) -> dict[str, Any]:
    answers = interrupt(
        {
            "kind": "answers",
            "summary": state.get("summary", ""),
            "questions": state.get("generated_questions", []),
        }
    )
    return {"user_answers": str(answers or "").strip(), "stage": "answers"}


def arch_choice_node(state: IdeaDiscussionState) -> dict[str, Any]:
    decision = interrupt(
        {
            "kind": "arch_choice",
            "question": (
                "Which architecture option should the blueprint and implementation target: "
                "Option A (fast and maintainable) or Option B (performance and scale)?"
            ),
            "architecture": state.get("architecture", ""),
        }
    )
    if isinstance(decision, dict):
        option = str(decision.get("option") or "A").strip().upper()
        notes = str(decision.get("notes") or "").strip()
    else:
        option = str(decision or "A").strip().upper()
        notes = ""
    if option not in ("A", "B"):
        option = "A"
    arch_choice: ArchChoice = {"option": option, "notes": notes}
    return {"arch_choice": arch_choice, "stage": "arch_choice"}


_FALLBACK_STRATEGY: ExecutionStrategy = {
    "mode": "subagents",
    "reasoning": (
        "Strategy output could not be parsed; defaulting to a single lead session with specialized "
        "subagents, which is the safest mode for a small MVP."
    ),
    "workstreams": [
        {
            "name": "core-product",
            "focus": "Implement the MVP end to end following plan.md.",
            "deliverables": "Working application code with tests.",
        },
        {
            "name": "quality",
            "focus": "Test coverage, fixtures, and verification of every task in plan.md.",
            "deliverables": "Passing unit/integration test suite.",
        },
    ],
}


def _strip_json_fences(text: str) -> str:
    stripped = text.strip()
    match = re.match(r"^```[a-zA-Z]*\s*\n(.*?)\n?```$", stripped, flags=re.DOTALL)
    if match:
        return match.group(1).strip()
    return stripped


def _parse_strategy(text: str) -> ExecutionStrategy | None:
    try:
        data = json.loads(_strip_json_fences(text))
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    mode = str(data.get("mode") or "").strip()
    if mode not in ("subagents", "agent_team"):
        return None
    raw_workstreams = data.get("workstreams")
    workstreams = []
    if isinstance(raw_workstreams, list):
        for item in raw_workstreams:
            if not isinstance(item, dict):
                continue
            name = re.sub(r"[^a-z0-9-]+", "-", str(item.get("name") or "").strip().lower()).strip("-")
            if not name:
                continue
            workstreams.append(
                {
                    "name": name,
                    "focus": str(item.get("focus") or "").strip(),
                    "deliverables": str(item.get("deliverables") or "").strip(),
                }
            )
    if not workstreams:
        return None
    return {
        "mode": mode,
        "reasoning": str(data.get("reasoning") or "").strip(),
        "workstreams": workstreams[:5],
    }


def strategy_node(state: IdeaDiscussionState) -> dict[str, Any]:
    runtime = get_runtime("strategy")
    arch_choice = state.get("arch_choice", {})
    response = _invoke_with_runtime(
        runtime,
        [
            SystemMessage(content=runtime.system_prompt),
            HumanMessage(
                content=(
                    f"Idea:\n{state.get('user_idea', '').strip()}\n\n"
                    f"Discussion summary:\n{state.get('summary', '').strip() or 'No summary.'}\n\n"
                    f"User answers:\n{state.get('user_answers', '').strip() or 'No answers.'}\n\n"
                    f"Architecture options:\n{state.get('architecture', '').strip() or 'No architecture.'}\n\n"
                    f"User chose option: {arch_choice.get('option') or 'A'}\n"
                    f"User notes: {arch_choice.get('notes') or 'None.'}\n\n"
                    "Decide the execution mode and workstreams. Output the strict JSON now."
                )
            ),
        ],
    )
    text = _extract_text_from_message(response) or normalize_content(response.content)
    strategy = _parse_strategy(text)
    if strategy is None:
        LOGGER.warning("Strategy output was not valid JSON; using fallback strategy.")
        strategy = _FALLBACK_STRATEGY
    return {"execution_strategy": strategy, "stage": "strategy"}


def plan_gate_node(state: IdeaDiscussionState) -> dict[str, Any]:
    decision = interrupt(
        {
            "kind": "plan_gate",
            "question": state.get("plan_offer_question") or _default_plan_offer_question(),
            "architecture": state.get("architecture", ""),
        }
    )
    if isinstance(decision, dict):
        generate = bool(decision.get("generate"))
        notes = str(decision.get("notes") or "").strip()
    else:
        generate = bool(decision)
        notes = ""
    plan_decision: PlanDecision = {"generate": generate, "notes": notes}
    return {"plan_decision": plan_decision, "stage": "plan_bundle" if generate else "done"}


def route_after_plan_gate(state: IdeaDiscussionState) -> Literal["plan_bundle", "__end__"]:
    if state.get("plan_decision", {}).get("generate"):
        return "plan_bundle"
    return "__end__"


def architect_node(state: IdeaDiscussionState) -> dict[str, Any]:
    architecture = _run_architect(
        user_idea=state["user_idea"],
        summary=state.get("summary", ""),
        questions=state.get("generated_questions", []),
        user_answers=state.get("user_answers", ""),
    )
    return {"architecture": architecture, "stage": "architecture"}


def planner_offer_node(state: IdeaDiscussionState) -> dict[str, Any]:
    planner = get_runtime("planner")
    response = _invoke_with_runtime(
        planner,
        [
            SystemMessage(content=planner.system_prompt),
            HumanMessage(
                content=(
                    f"Initial idea:\n{state['user_idea'].strip()}\n\n"
                    f"Summary:\n{state.get('summary', '').strip() or 'No summary.'}\n\n"
                    f"User answers:\n{state.get('user_answers', '').strip() or 'No answers.'}\n\n"
                    f"Architecture:\n{state.get('architecture', '').strip() or 'No architecture.'}\n\n"
                    "Ask the yes/no follow-up question now."
                )
            ),
        ],
    )
    if isinstance(planner.llm, ChatOpenAI):
        _log_openai_response("planner_offer", planner.model, response)
    question = _extract_text_from_message(response) or normalize_content(response.content).strip()
    return {"plan_offer_question": question or _default_plan_offer_question(), "stage": "plan_gate"}


def plan_bundle_node(state: IdeaDiscussionState) -> dict[str, Any]:
    runtime = get_runtime("architect")
    strategy = state.get("execution_strategy") or None
    context_block = build_bundle_context(
        user_idea=state["user_idea"],
        summary=state.get("summary", ""),
        questions=_ensure_questions(state.get("generated_questions", [])),
        user_answers=state.get("user_answers", ""),
        architecture=state.get("architecture", ""),
        arch_choice=state.get("arch_choice"),
        strategy=strategy,
        planning_request=state.get("plan_decision", {}).get("notes", ""),
    )

    def generate_doc(system_prompt: str, user_prompt: str) -> str:
        return _generate_project_doc(
            runtime=runtime,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            fallback="",
            log_tag="project_bundle",
        )

    bundle_dir, created_files, summary = create_project_bundle(
        root=_project_bundle_root(),
        user_idea=state["user_idea"],
        context_block=context_block,
        strategy=strategy,
        generate_doc=generate_doc,
    )
    return {
        "project_bundle_dir": str(bundle_dir),
        "project_bundle_files": created_files,
        "project_bundle_summary": summary,
        "stage": "plan_bundle",
    }


def clear_runtime_caches() -> None:
    clear_settings_cache()
    get_runtime.cache_clear()
