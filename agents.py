from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Literal

from langchain_anthropic import ChatAnthropic
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_openai import ChatOpenAI

try:
    from .config import Provider, clear_settings_cache, get_settings
    from .state import IdeaDiscussionState
    from .text_utils import normalize_content
except ImportError:
    from config import Provider, clear_settings_cache, get_settings
    from state import IdeaDiscussionState
    from text_utils import normalize_content

SPEAKER_ORDER: list[str] = ["PM", "Tech Lead", "Skeptic"]
LOGGER = logging.getLogger(__name__)
SPEAKER_NAME_TOKEN: dict[str, str] = {
    "PM": "pm",
    "Tech Lead": "tech_lead",
    "Skeptic": "skeptic",
}
TOKEN_TO_SPEAKER: dict[str, str] = {v: k for k, v in SPEAKER_NAME_TOKEN.items()}

SHARED_DISCUSSION_RULES = (
    "You are in a live panel with two other AIs. The human message is the anchor idea, "
    "and the remaining messages are prior panel turns.\n\n"
    "How to participate:\n"
    "- Read the whole thread and directly reference at least one concrete claim from another panelist.\n"
    "- Prioritize this order in every turn: (1) functionality and user workflows, (2) technical implementation, "
    "(3) business model/GTM.\n"
    "- Cover the full arc when relevant: problem and user workflow -> scope/MVP -> build/ops -> validation/launch "
    "-> business model/pricing -> risks.\n"
    "- Improve the idea with concrete tradeoffs, metrics, and what to cut.\n"
    "- Use concise Markdown with short sections and bullets.\n"
    "- Keep each turn compact: target 200-250 words, max 7 bullets total.\n"
    "- Avoid generic essays and avoid repeating the idea verbatim.\n"
    "- Keep outputs RAG-friendly: use explicit claims, assumptions, and decisions as bullet points.\n"
)

PM_SYSTEM = (
    "You are PM in an idea lab: product strategist and market shaper.\n"
    + SHARED_DISCUSSION_RULES
    + "\nYour angle: core user journeys, MVP functionality, feature prioritization, and implementation-ready scope. "
    "After that, add business model implications only if they affect product decisions. "
    "Each turn, expose 1-2 risky assumptions and suggest 2-3 practical improvements.\n"
    "End with **Functionality recommendation** and **Implementation-scoped next test**.\n"
)

TECH_LEAD_SYSTEM = (
    "You are Tech Lead in an idea lab: pragmatic architect and delivery realist.\n"
    + SHARED_DISCUSSION_RULES
    + "\nYour angle: architecture choices, stack constraints, integration complexity, operational reliability, "
    "and build sequencing for the highest-priority product functionality. "
    "Address business model only after feasibility and implementation are clear. "
    "React to PM/Skeptic claims with feasibility notes and hidden technical risk.\n"
    "End with **Tech / build notes** and **Validation steps**.\n"
)

SKEPTIC_SYSTEM = (
    "You are Skeptic in an idea lab: critical analyst focused on failure modes.\n"
    + SHARED_DISCUSSION_RULES
    + "\nYour angle: weak assumptions in functionality, technical design gaps, edge cases, adoption friction, "
    "and legal/compliance concerns. Raise business-model concerns after product and implementation risks.\n"
    "Challenge overconfident claims and force evidence-backed decisions.\n"
    "End with **Pushback on functionality/implementation** and **Evidence needed next**.\n"
)

SUMMARY_SYSTEM = (
    "You are a neutral session synthesizer. You receive the user's idea and the full multi-agent discussion.\n"
    "Produce a concise Markdown brief the user can immediately execute:\n\n"
    "## Executive summary\n"
    "(3-4 concise sentences)\n\n"
    "## Functional specification snapshot\n"
    "(2-4 bullets: primary user workflows, core capabilities, clear MVP boundaries)\n\n"
    "## Technical implementation snapshot\n"
    "(2-4 bullets: architecture shape, key integrations, sequencing, operational constraints)\n\n"
    "## Refined idea\n"
    "(2-3 bullets: how the concept evolved after functionality + technical discussion)\n\n"
    "## SWOT snapshot\n"
    "- Strengths\n- Weaknesses\n- Opportunities\n- Threats\n\n"
    "## Business model notes\n"
    "(2-3 bullets only; add only notes that affect MVP decisions)\n\n"
    "## Risks & mitigations\n"
    "(3-5 bullets, include disagreements where relevant)\n\n"
    "## Recommended next steps\n"
    "(4-6 bullets; prioritize functionality then implementation then business tests)\n\n"
    "Hard limits: keep total output under 600 words and avoid repeating similar points."
)

QUESTIONS_SYSTEM = (
    "You are an MVP planning interviewer.\n"
    "Generate exactly 5 high-leverage questions that must be answered before implementing the MVP.\n"
    "Questions must be:\n"
    "- directly actionable for scoping and architecture decisions\n"
    "- specific to this idea (user workflows, scope, data model, integrations, constraints, then business model)\n"
    "- answerable in free text\n"
    "- one line each, max 24 words\n"
    "Output format (strict):\n"
    "1. ...\n2. ...\n3. ...\n4. ...\n5. ...\n"
    "Do not output any extra sections or commentary."
)

ARCHITECT_SYSTEM = (
    "You are Architect, a principal system architect focused on turning validated MVP ideas into practical implementation plans.\n"
    "You receive: (1) original idea, (2) discussion summary, (3) clarification questions, (4) user answers.\n"
    "Your job is to produce exactly two architecture options at the right level for MVP planning.\n\n"
    "Output format (strict):\n"
    "## Option A - Fast implementation and maintainability\n"
    "- Proposed architecture style and key services/components\n"
    "- Recommended languages/frameworks/tools\n"
    "- State and persistence approach (high-level only)\n"
    "- Integrations and infrastructure\n"
    "- Security/reliability baseline\n"
    "- Tradeoffs and expected limits\n\n"
    "## Option B - Maximum performance and scale\n"
    "- Proposed architecture style and key services/components\n"
    "- Recommended languages/frameworks/tools\n"
    "- State and persistence approach (high-level only)\n"
    "- Integrations and infrastructure\n"
    "- Security/reliability baseline\n"
    "- Tradeoffs and expected limits\n\n"
    "## Shared components (if applicable)\n"
    "(List overlaps if the two options have common parts. Similarity is acceptable.)\n\n"
    "## Architect recommendation\n"
    "- Select one architecture as the ideal target for high performance and high load.\n"
    "- Explain why it is still suitable for quick MVP launch.\n"
    "- Provide a phased rollout: MVP phase -> scale-up phase.\n\n"
    "Constraints:\n"
    "- Keep output concise and practical (target 600 words total).\n"
    "- Do NOT include database table schemas, ERDs, field-by-field models, or code/type definitions.\n"
    "- Focus on system shape and implementation decisions only."
)

@dataclass(frozen=True)
class AgentRuntime:
    llm: BaseChatModel
    system_prompt: str
    provider: Provider
    model: str


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
        max_output_tokens=max_tokens,
    )


@lru_cache(maxsize=1)
def get_discussion_runtimes() -> dict[str, AgentRuntime]:
    settings = get_settings()
    return {
        "PM": AgentRuntime(
            llm=_build_llm(settings.pm_provider, settings.pm_model, settings.discussion_max_tokens),
            system_prompt=PM_SYSTEM,
            provider=_resolve_provider(settings.pm_provider, settings.pm_model),
            model=settings.pm_model,
        ),
        "Tech Lead": AgentRuntime(
            llm=_build_llm(
                settings.tech_lead_provider,
                settings.tech_lead_model,
                settings.discussion_max_tokens,
            ),
            system_prompt=TECH_LEAD_SYSTEM,
            provider=_resolve_provider(settings.tech_lead_provider, settings.tech_lead_model),
            model=settings.tech_lead_model,
        ),
        "Skeptic": AgentRuntime(
            llm=_build_llm(
                settings.skeptic_provider,
                settings.skeptic_model,
                settings.skeptic_max_tokens,
            ),
            system_prompt=SKEPTIC_SYSTEM,
            provider=_resolve_provider(settings.skeptic_provider, settings.skeptic_model),
            model=settings.skeptic_model,
        ),
    }


@lru_cache(maxsize=1)
def get_summarizer_runtime() -> BaseChatModel:
    settings = get_settings()
    return _build_llm(
        settings.summarizer_provider,
        settings.summarizer_model,
        settings.summary_max_tokens,
    )


@lru_cache(maxsize=1)
def get_architect_runtime() -> AgentRuntime:
    settings = get_settings()
    return AgentRuntime(
        llm=_build_llm(
            settings.architect_provider,
            settings.architect_model,
            settings.summary_max_tokens,
        ),
        system_prompt=ARCHITECT_SYSTEM,
        provider=_resolve_provider(settings.architect_provider, settings.architect_model),
        model=settings.architect_model,
    )


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


def discussion_node(state: IdeaDiscussionState) -> dict[str, Any]:
    speaker = state["next_speaker"]
    runtimes = get_discussion_runtimes()
    runtime = runtimes[speaker]

    thread_md = _history_markdown(state["discussion_history"])
    turn_prompt = (
        f"Anchor idea:\n{state['user_idea']}\n\n"
        "Panel transcript so far (chronological):\n"
        f"{thread_md}\n\n"
        f"You are **{speaker}**. Write the next panel turn.\n"
        "Rules for this turn:\n"
        "- Explicitly reference at least one prior panelist by role name (PM, Tech Lead, Skeptic).\n"
        "- Build on or challenge a concrete claim from the transcript.\n"
        "- Add net-new decisions/assumptions/tradeoffs rather than repeating prior text.\n"
        "- Keep it concise, structured, and decision-oriented.\n"
        "- Use at most 2 short sections and at most 6 bullets total.\n"
    )
    response = runtime.llm.invoke(
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
        same_model_retry = runtime.llm.invoke(
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
    }


def summarizer_node(state: IdeaDiscussionState) -> dict[str, Any]:
    summarizer = get_summarizer_runtime()
    thread_md = _history_markdown(state["discussion_history"])
    summary_response = summarizer.invoke(
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
    if isinstance(summarizer, ChatOpenAI):
        _log_openai_response("summary", get_settings().summarizer_model, summary_response)
    summary_text = normalize_content(summary_response.content)
    questions_response = summarizer.invoke(
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
    questions_text = normalize_content(questions_response.content)
    questions = _ensure_questions(_extract_questions(questions_text))
    return {"summary": summary_text, "generated_questions": questions}


def _run_architect(
    *,
    user_idea: str,
    summary: str,
    questions: list[str],
    user_answers: str,
) -> str:
    architect = get_architect_runtime()
    normalized_questions = _ensure_questions(questions)
    question_block = "\n".join(normalized_questions) if normalized_questions else "No explicit questions provided."
    response = architect.llm.invoke(
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


def clear_runtime_caches() -> None:
    clear_settings_cache()
    get_discussion_runtimes.cache_clear()
    get_summarizer_runtime.cache_clear()
    get_architect_runtime.cache_clear()


def route_after_discussion(state: IdeaDiscussionState) -> Literal["discussion", "summarizer"]:
    if state["turn_count"] < state["max_rounds"]:
        return "discussion"
    return "summarizer"


def route_from_start(state: IdeaDiscussionState) -> Literal["discussion", "architect"]:
    if state.get("phase") == "answers":
        return "architect"
    return "discussion"


def architect_node(state: IdeaDiscussionState) -> dict[str, Any]:
    architecture = _run_architect(
        user_idea=state["user_idea"],
        summary=state.get("summary", ""),
        questions=state.get("generated_questions", []),
        user_answers=state.get("user_answers", ""),
    )
    return {"architecture": architecture}
