"""Pricing every model call from the catalog (with negotiated-price overrides) and the implement gate's dollar range."""

from pathlib import Path

import pytest

from idea_to_mvp.config import Settings
from idea_to_mvp.implementation.ledger import LedgerRow, append_row
from idea_to_mvp.model_catalog import get_catalog
from idea_to_mvp.pricing import (
    PricingError,
    call_cost,
    estimate_run_cost,
    load_price_overrides,
    task_cost_history,
)

SONNET = "claude-sonnet-5-5"


def settings(**overrides: object) -> Settings:
    return Settings(_env_file=None, **overrides)  # type: ignore[arg-type]


def prices_file(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "prices.toml"
    path.write_text(text, encoding="utf-8")
    return path


def ledger_row(
    task_id: str, cost: float, model: str, *, kind: str = "task", outcome: str = "success"
) -> LedgerRow:
    return {
        "kind": kind, "label": task_id, "task_id": task_id, "iteration": 1, "attempt": 1, "model": model,
        "cost_usd": cost, "turns": 3, "outcome": outcome, "session_id": None,
        "trajectory": f".idea-to-mvp/trajectories/{task_id}.md", "started_at": 1.0, "ended_at": 2.0,
    }  # type: ignore[typeddict-item]  # fmt: skip


# ------------------------------------------------------------------ one call


def test_a_call_is_priced_from_the_catalog_per_million_tokens() -> None:
    entry = get_catalog().find(SONNET)
    assert entry and entry.input_per_mtok == 2.0 and entry.output_per_mtok == 10.0
    assert call_cost(SONNET, 1_000_000, 100_000) == pytest.approx(3.0)


def test_cached_input_is_charged_at_the_cached_rate() -> None:
    entry = get_catalog().find(SONNET)
    assert entry and entry.cached_input_per_mtok is not None
    expected = (200_000 * 2.0 + 800_000 * entry.cached_input_per_mtok) / 1_000_000
    assert call_cost(SONNET, 1_000_000, 0, cached_tokens=800_000) == pytest.approx(expected)


def test_a_dated_snapshot_of_a_listed_model_is_priced_like_it() -> None:
    assert call_cost(f"{SONNET}-20260301", 1000, 1000) == call_cost(SONNET, 1000, 1000) is not None


def test_a_model_the_catalog_does_not_know_stays_unpriced() -> None:
    assert call_cost("totally-new-model", 1000, 1000) is None
    assert call_cost("", 1000, 1000) is None


# ---------------------------------------------------------------- overrides


def test_a_prices_file_replaces_the_catalog_price_of_a_listed_model(tmp_path: Path) -> None:
    path = prices_file(
        tmp_path,
        f'[prices."{SONNET}"]\ninput_per_mtok = 1.0\noutput_per_mtok = 5.0\ncached_input_per_mtok = 0.1\n',
    )
    overrides = load_price_overrides(path)
    assert call_cost(SONNET, 1_000_000, 1_000_000, overrides=overrides) == pytest.approx(6.0)
    assert call_cost(
        SONNET, 1_000_000, 0, cached_tokens=1_000_000, overrides=overrides
    ) == pytest.approx(0.1)
    assert call_cost("claude-opus-5-5", 1_000_000, 0, overrides=overrides) == call_cost(
        "claude-opus-5-5", 1_000_000, 0
    )


def test_a_prices_file_can_price_a_model_the_catalog_does_not_list(tmp_path: Path) -> None:
    overrides = load_price_overrides(
        prices_file(
            tmp_path, '[prices."in-house-model"]\ninput_per_mtok = 1.0\noutput_per_mtok = 2.0\n'
        )
    )
    assert call_cost("in-house-model", 1_000_000, 1_000_000, overrides=overrides) == pytest.approx(
        3.0
    )
    assert call_cost("in-house-model-20260101", 1_000_000, 0, overrides=overrides) == pytest.approx(
        1.0
    )  # snapshots too


def test_an_override_without_a_cached_rate_charges_cached_tokens_as_input(tmp_path: Path) -> None:
    overrides = load_price_overrides(
        prices_file(tmp_path, '[prices."m"]\ninput_per_mtok = 4.0\noutput_per_mtok = 8.0\n')
    )
    assert call_cost(
        "m", 1_000_000, 0, cached_tokens=500_000, overrides=overrides
    ) == pytest.approx(4.0)


@pytest.mark.parametrize(
    ("text", "complaint"),
    [
        ("this is = not toml [", "not valid TOML"),
        ('[prices."m"]\ninput_per_mtok = 1.0\n', "both input_per_mtok and output_per_mtok"),
        ('[prices."m"]\ninput_per_mtok = -1.0\noutput_per_mtok = 1.0\n', "must not be negative"),
        ('[prices."m"]\ninput_per_mtok = 1.0\noutput_per_mtok = 1.0\nbonus = 3\n', "unknown key"),
        ("[other]\nx = 1\n", "[prices"),
    ],
)
def test_a_bad_prices_file_is_refused_with_a_reason(
    tmp_path: Path, text: str, complaint: str
) -> None:
    with pytest.raises(PricingError, match=complaint.replace("[", r"\[")):
        load_price_overrides(prices_file(tmp_path, text))


def test_a_missing_prices_file_is_refused() -> None:
    with pytest.raises(PricingError, match="cannot read"):
        load_price_overrides(Path("/nonexistent/prices.toml"))


# ------------------------------------------------------------ earlier ledgers


def test_the_history_is_the_cost_of_each_earlier_task_on_the_same_model(tmp_path: Path) -> None:
    first = tmp_path / "20260101-first"
    second = tmp_path / "20260102-second"
    append_row(first, ledger_row("T01", 1.0, SONNET))
    append_row(first, ledger_row("T02", 2.0, SONNET))
    append_row(
        first, ledger_row("T02", 0.5, SONNET, outcome="failed")
    )  # a failed attempt still cost money
    append_row(first, ledger_row("T03", 9.0, "claude-opus-5-5"))  # another model: not comparable
    append_row(second, ledger_row("T01", 3.0, SONNET))
    append_row(
        second, ledger_row("T01", 4.0, SONNET, kind="resolver")
    )  # a resolver is not the task's own session
    append_row(
        second, ledger_row("T02", 0.7, SONNET, outcome="failed")
    )  # never succeeded: not a finished task
    assert sorted(task_cost_history(tmp_path, SONNET)) == [1.0, 2.5, 3.0]


def test_hidden_folders_and_corrupt_ledgers_are_ignored(tmp_path: Path) -> None:
    append_row(tmp_path / ".worktrees" / "x", ledger_row("T01", 5.0, SONNET))
    broken = tmp_path / "broken" / ".idea-to-mvp"
    broken.mkdir(parents=True)
    (broken / "ledger.json").write_text("garbage")
    assert task_cost_history(tmp_path, SONNET) == []
    assert task_cost_history(tmp_path / "missing", SONNET) == []


# ----------------------------------------------------------------- the range


def test_without_history_the_range_comes_from_the_catalogs_typical_task_session() -> None:
    s = settings(
        implementer_model=SONNET,
        verifier_model=SONNET,
        escalation_model="claude-opus-5-5",
        max_fix_attempts=2,
    )
    estimate = estimate_run_cost(s, task_count=4, history=[])
    shape = get_catalog().typical_run["implementer"]
    entry = get_catalog().find(SONNET)
    assert entry
    per_task = entry.cost(
        shape.input_tokens, shape.output_tokens, round(shape.input_tokens * shape.cached_fraction)
    )
    assert (
        estimate.basis == "catalog"
        and estimate.samples == 0
        and estimate.per_task == pytest.approx(per_task)
    )
    assert estimate.low is not None and estimate.high is not None
    assert (
        estimate.low >= 4 * per_task * 0.5 and estimate.high > estimate.low
    )  # tasks plus one verification round at least
    assert estimate.high <= s.implementer_max_total_usd


def test_enough_earlier_tasks_replace_the_default_with_their_median() -> None:
    s = settings(implementer_model=SONNET, verifier_model=SONNET)
    estimate = estimate_run_cost(s, task_count=4, history=[1.0, 3.0, 2.0, 10.0])
    assert estimate.basis == "history" and estimate.samples == 4 and estimate.per_task == 2.5
    assert estimate.low is not None and estimate.low >= 4 * 2.5 * 0.75


def test_two_earlier_tasks_are_too_few_to_trust() -> None:
    s = settings(implementer_model=SONNET, verifier_model=SONNET)
    assert estimate_run_cost(s, task_count=3, history=[1.0, 2.0]).basis == "catalog"


def test_the_high_end_never_exceeds_the_whole_run_cap() -> None:
    s = settings(implementer_model=SONNET, verifier_model=SONNET, implementer_max_total_usd=3.0)
    estimate = estimate_run_cost(s, task_count=10, history=[5.0, 5.0, 5.0])
    assert estimate.high == 3.0 and estimate.low is not None and estimate.low <= 3.0


def test_more_fix_attempts_raise_the_high_end_only() -> None:
    base = {"implementer_model": SONNET, "verifier_model": SONNET, "escalation_model": SONNET}
    none = estimate_run_cost(settings(max_fix_attempts=0, **base), task_count=3, history=[])
    two = estimate_run_cost(settings(max_fix_attempts=2, **base), task_count=3, history=[])
    assert none.low == two.low and (two.high or 0) > (none.high or 0)


def test_a_session_model_without_a_price_makes_the_whole_estimate_unpriced() -> None:
    s = settings(implementer_model="mystery-model", verifier_model=SONNET)
    estimate = estimate_run_cost(s, task_count=3, history=[])
    assert estimate.basis == "unpriced" and estimate.low is None and estimate.high is None
    assert "implementer" in estimate.unpriced


def test_a_prices_file_changes_the_estimate(tmp_path: Path) -> None:
    base = {"implementer_model": SONNET, "verifier_model": SONNET, "escalation_model": SONNET}
    plain = estimate_run_cost(settings(**base), task_count=3, history=[])
    path = prices_file(
        tmp_path, f'[prices."{SONNET}"]\ninput_per_mtok = 0.2\noutput_per_mtok = 1.0\n'
    )
    cheap = estimate_run_cost(settings(model_prices_file=path, **base), task_count=3, history=[])
    assert cheap.high is not None and plain.high is not None and cheap.high < plain.high / 3


def test_the_range_serialises_for_the_checkpoint() -> None:
    s = settings(implementer_model=SONNET, verifier_model=SONNET)
    data = estimate_run_cost(s, task_count=2, history=[]).to_dict()
    assert (
        set(data) == {"low", "high", "per_task", "basis", "samples", "tasks", "unpriced"}
        and data["tasks"] == 2
    )


def test_no_plan_is_estimated_as_one_session() -> None:
    s = settings(implementer_model=SONNET, verifier_model=SONNET)
    assert estimate_run_cost(s, task_count=0, history=[]).tasks == 1


def test_the_acceptance_lane_adds_one_lane_to_every_verification_round() -> None:
    base = {
        "implementer_model": SONNET,
        "verifier_model": SONNET,
        "escalation_model": SONNET,
        "max_fix_attempts": 0,
    }
    shape = get_catalog().typical_run["verifier"]
    entry = get_catalog().find(SONNET)
    assert entry
    lane = entry.cost(
        shape.input_tokens, shape.output_tokens, round(shape.input_tokens * shape.cached_fraction)
    )
    off = estimate_run_cost(settings(acceptance_tests="off", **base), task_count=3, history=[])
    on = estimate_run_cost(settings(acceptance_tests="on", **base), task_count=3, history=[])
    assert (on.low or 0) - (off.low or 0) == pytest.approx(lane)
