from __future__ import annotations

from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from idea_to_mvp import llm
from idea_to_mvp.nodes.common import history_markdown, preferences_block
from idea_to_mvp.roles import QUESTIONS_SYSTEM, SUMMARY_SYSTEM
from idea_to_mvp.schemas import MvpQuestion, QuestionSet, render_questions
from idea_to_mvp.state import IdeaDiscussionState
from idea_to_mvp.usage import with_usage


def fallback_question_set() -> QuestionSet:
    """Deterministic questions, used when the model cannot produce a valid set."""
    rows = [
        (
            "What are the top 1-2 user workflows the MVP must complete end-to-end without manual workarounds?",
            "It fixes the scope every other decision hangs on.",
            "One core journey from sign-up to the first valuable result.",
        ),
        (
            "Which capabilities are mandatory in v0, and which must be deferred to avoid scope creep?",
            "It prevents building features nobody validated.",
            "Only what the core journey needs; defer sharing, integrations, and admin tools.",
        ),
        (
            "What technical constraints (latency, data model, auth, integrations) must shape the architecture from day one?",
            "Hard constraints decide the architecture options.",
            "A web app with simple auth and one relational store; no hard real-time needs.",
        ),
        (
            "Which implementation milestones and validation checks define build readiness for launch?",
            "It defines the definition of done for the agents.",
            "Core journey works end to end with automated tests passing.",
        ),
        (
            "Which business-model assumption matters most initially, and how will the MVP test it with minimal added scope?",
            "It decides what to measure in the first version.",
            "Users complete the core journey twice in a week; pricing comes later.",
        ),
    ]
    return QuestionSet(
        questions=[MvpQuestion(question=q, why_it_matters=w, suggested_answer=a) for q, w, a in rows]
    )


@with_usage(role="summarizer")
def summarizer_node(state: IdeaDiscussionState) -> dict[str, Any]:
    summarizer = llm.get_runtime("summarizer")
    thread_md = history_markdown(state["discussion_history"])
    preferences = preferences_block(state.get("preferences"))
    summary_text = llm.invoke_text(
        summarizer,
        [
            SystemMessage(content=SUMMARY_SYSTEM),
            HumanMessage(
                content=(
                    f"Original idea:\n{state['user_idea']}\n\n"
                    f"{preferences}"
                    f"Full discussion thread:\n\n{thread_md}\n\n"
                    "Produce the brief in the requested format. Do not include question lists."
                )
            ),
        ],
    )
    question_set = llm.invoke_structured(
        summarizer,
        [
            SystemMessage(content=QUESTIONS_SYSTEM),
            HumanMessage(
                content=(
                    f"Idea:\n{state['user_idea']}\n\n"
                    f"{preferences}"
                    f"Discussion highlights:\n{thread_md}\n\n"
                    f"Current synthesized summary:\n{summary_text}\n\n"
                    "Generate the 5 MVP-preparation questions now."
                )
            ),
        ],
        QuestionSet,
        fallback=fallback_question_set,
    )
    return {
        "summary": summary_text,
        "questions": [q.model_dump() for q in question_set.questions],
        "generated_questions": render_questions(question_set),
        "stage": "summary",
    }
