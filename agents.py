from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime
from functools import cache
from pathlib import Path
from typing import Any, Literal

from langchain_anthropic import ChatAnthropic
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_openai import ChatOpenAI

try:
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
    from .state import IdeaDiscussionState
    from .text_utils import normalize_content
except ImportError:
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
    from state import IdeaDiscussionState
    from text_utils import normalize_content

LOGGER = logging.getLogger(__name__)

ROOT_AGENTS_SYSTEM = (
    "You are writing a root AGENTS.md file for a brand-new MVP project folder.\n"
    "This file will be read by coding agents before they start implementation.\n"
    "Write concise, actionable instructions with short headings and bullets.\n"
    "Focus on: project goal, target users, MVP boundaries, chosen architecture direction, delivery workflow, "
    "definition of done, testing bar, contract discipline, and handoff expectations.\n"
    "Best practices:\n"
    "- be concrete and directive\n"
    "- avoid fluff and repeated background\n"
    "- include a short execution checklist agents can follow\n"
    "- mention which deeper docs to read next (`contracts/AGENTS.md`, `application/AGENTS.md`, `quality/AGENTS.md`, `plan.md`)\n"
    "- keep it usable as an operating manual, not a product essay"
)

CONTRACTS_AGENTS_SYSTEM = (
    "You are writing `contracts/AGENTS.md` for a multi-agent MVP project.\n"
    "This file defines how agents create, use, and evolve data contracts safely.\n"
    "Write concise operational guidance covering: contract ownership, API/schema/event boundaries, versioning, "
    "compatibility rules, migration protocol, review checklist, and required contract artifacts.\n"
    "Include a small example contract template in markdown.\n"
    "Be strict, practical, and optimized for parallel agent work."
)

APPLICATION_AGENTS_SYSTEM = (
    "You are writing `application/AGENTS.md` for the main product implementation workstream.\n"
    "This file should guide agents building features across UI, backend, integrations, and business logic.\n"
    "Cover: how to slice work, how to consume contracts, how to avoid cross-task conflicts, acceptance criteria, "
    "migration discipline, observability expectations, and what must be updated before handoff.\n"
    "Use direct instructions and short checklists."
)

QUALITY_AGENTS_SYSTEM = (
    "You are writing `quality/AGENTS.md` for a multi-agent MVP delivery process.\n"
    "This file should define testing and verification expectations for every task.\n"
    "Cover: agent-run verification, required unit/integration tests, coverage goals, contract tests, "
    "fixture guidance, regression checks, release-readiness checks, and how to document residual risk.\n"
    "Make the testing standard explicit: each task must include unit/integration tests and target at least 80% coverage "
    "for the changed scope."
)

PLAN_SYSTEM = (
    "You are creating an execution-ready `plan.md` for a greenfield MVP project.\n"
    "The plan will be consumed by autonomous coding agents, one task at a time.\n"
    "Produce a complete start-to-finish MVP task plan with sequencing, dependencies, data contracts, and tests.\n"
    "Requirements:\n"
    "- include all major tasks needed to reach an MVP release, not just engineering implementation\n"
    "- each task is owned by one agent and should be agent-sized\n"
    "- do not collapse unrelated work into giant tasks\n"
    "- if one task can affect another, reference the shared data contract IDs explicitly\n"
    "- keep `plan.md` task-only: do not include project overview, table of contents, milestone narrative, or repeated guidance already covered by AGENTS files\n"
    "- start with a compact contract registry near the top with stable IDs like `C1`, `C2`, ...\n"
    "- after the contract registry, output only a numbered task list in execution order\n"
    "- each task must include only these fields: Goal, Depends on, Contracts in, Contracts out, Implementation scope, Test and verify, Required unit/integration tests, Coverage target, Handoff artifacts\n"
    "- keep the writing dense, structured, and implementation-oriented"
)

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


def _project_slug(text: str) -> str:
    ascii_text = (text or "").encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", ascii_text.lower()).strip("-")
    return slug[:64].strip("-") or "mvp-project"


def _project_bundle_root() -> Path:
    return Path(__file__).resolve().parent / "project_blueprints"


def _build_bundle_context(
    *,
    user_idea: str,
    summary: str,
    questions: list[str],
    user_answers: str,
    architecture: str,
    planning_request: str,
) -> str:
    normalized_questions = _ensure_questions(questions)
    extra_guidance = (planning_request or "").strip()
    if extra_guidance.lower() in {"yes", "y", "yes.", "yes please", "sure", "ok", "okay"}:
        extra_guidance = ""
    return (
        f"Original idea:\n{user_idea.strip()}\n\n"
        f"Discussion summary:\n{summary.strip() or 'No summary available.'}\n\n"
        f"Questions asked:\n{chr(10).join(normalized_questions) or 'No explicit questions.'}\n\n"
        f"User answers:\n{user_answers.strip() or 'No user answers provided.'}\n\n"
        f"Recommended architecture context:\n{architecture.strip() or 'No architecture provided.'}\n\n"
        f"Extra planning guidance from user:\n{extra_guidance or 'No extra guidance beyond generating the execution pack.'}"
    )


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


def _create_project_bundle(
    *,
    user_idea: str,
    summary: str,
    questions: list[str],
    user_answers: str,
    architecture: str,
    planning_request: str,
) -> tuple[str, list[str], str]:
    runtime = get_runtime("architect")
    context_block = _build_bundle_context(
        user_idea=user_idea,
        summary=summary,
        questions=questions,
        user_answers=user_answers,
        architecture=architecture,
        planning_request=planning_request,
    )

    timestamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S-%f")
    project_dir = _project_bundle_root() / f"{timestamp}-{_project_slug(user_idea)}"
    file_specs = [
        (
            "AGENTS.md",
            ROOT_AGENTS_SYSTEM,
            "Write the root `AGENTS.md` now. Keep it sharp, directive, and ready for implementation agents.",
            (
                "# Project Operating Guide\n\n"
                "- Read `plan.md` before starting any work.\n"
                "- Use stable data contracts and update them deliberately.\n"
                "- Keep changes scoped to one task at a time.\n"
                "- Add unit/integration tests and target 80% coverage for changed scope.\n"
            ),
            "project_root_agents",
        ),
        (
            "contracts/AGENTS.md",
            CONTRACTS_AGENTS_SYSTEM,
            "Write `contracts/AGENTS.md` now. Make it the contract governance guide for parallel agents.",
            (
                "# Contract Governance\n\n"
                "- Every shared interface gets a stable contract ID.\n"
                "- Breaking changes require a migration note and downstream review.\n"
                "- Contract tests are mandatory for shared boundaries.\n"
            ),
            "project_contracts_agents",
        ),
        (
            "application/AGENTS.md",
            APPLICATION_AGENTS_SYSTEM,
            "Write `application/AGENTS.md` now. Optimize it for agents implementing one task at a time.",
            (
                "# Application Delivery Guide\n\n"
                "- Start from the assigned task in `plan.md`.\n"
                "- Consume only the listed contracts and update acceptance checks.\n"
                "- Add observability, tests, and handoff notes before closing the task.\n"
            ),
            "project_application_agents",
        ),
        (
            "quality/AGENTS.md",
            QUALITY_AGENTS_SYSTEM,
            "Write `quality/AGENTS.md` now. Make the testing and verification standard explicit.",
            (
                "# Quality Standard\n\n"
                "- Every task needs agent-run verification.\n"
                "- Add unit/integration tests and target at least 80% coverage for changed scope.\n"
                "- Run contract-aware regression checks before handoff.\n"
            ),
            "project_quality_agents",
        ),
        (
            "plan.md",
            PLAN_SYSTEM,
            "Write `plan.md` now. Use markdown only. Do not include any overview or AGENTS-style instructions. Start with `## Contract Registry`, then `## Tasks`, then a numbered task list from MVP setup through launch readiness.",
            (
                "# MVP Plan\n\n"
                "## Contract Registry\n\n"
                "- C1: Core API contract\n\n"
                "## Tasks\n\n"
                "### 01. Project setup\n"
                "- Goal: establish the MVP baseline.\n"
                "- Depends on: none.\n"
                "- Contracts in: none.\n"
                "- Contracts out: C1.\n"
                "- Implementation scope: repository scaffold, tooling, CI, local run path.\n"
                "- Test and verify: confirm install, lint, test, and app boot commands work.\n"
                "- Required unit/integration tests: smoke coverage for config and startup paths.\n"
                "- Coverage target: at least 80% for changed scope.\n"
                "- Handoff artifacts: updated setup files and task notes.\n"
            ),
            "project_plan",
        ),
    ]

    created_files: list[str] = []
    for relative_path, system_prompt, instruction, fallback, log_tag in file_specs:
        absolute_path = project_dir / relative_path
        absolute_path.parent.mkdir(parents=True, exist_ok=True)
        content = _generate_project_doc(
            runtime=runtime,
            system_prompt=system_prompt,
            user_prompt=f"{context_block}\n\n{instruction}",
            fallback=fallback,
            log_tag=log_tag,
        )
        absolute_path.write_text(content.strip() + "\n", encoding="utf-8")
        created_files.append(relative_path)

    summary_lines = [
        f"Generated an agent-ready project pack in `{project_dir}`.",
        "",
        "Created files:",
        *[f"- `{relative_path}`" for relative_path in created_files],
        "",
        "Start with `plan.md`, then open the most relevant `AGENTS.md` file for the current task area before implementation.",
    ]
    return str(project_dir), created_files, "\n".join(summary_lines)


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
    return {"summary": summary_text, "generated_questions": questions}


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


def route_from_start(state: IdeaDiscussionState) -> Literal["discussion", "architect", "plan_bundle"]:
    if state.get("phase") == "plan_bundle":
        return "plan_bundle"
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
    return {"plan_offer_question": question or _default_plan_offer_question()}


def plan_bundle_node(state: IdeaDiscussionState) -> dict[str, Any]:
    project_dir, created_files, summary = _create_project_bundle(
        user_idea=state["user_idea"],
        summary=state.get("summary", ""),
        questions=state.get("generated_questions", []),
        user_answers=state.get("user_answers", ""),
        architecture=state.get("architecture", ""),
        planning_request=state.get("planning_request", ""),
    )
    return {
        "project_bundle_dir": project_dir,
        "project_bundle_files": created_files,
        "project_bundle_summary": summary,
    }


def clear_runtime_caches() -> None:
    clear_settings_cache()
    get_runtime.cache_clear()
