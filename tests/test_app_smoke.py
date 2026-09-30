from idea_to_mvp.config import Settings
from idea_to_mvp.ui.app import make_ui


def test_make_ui_smoke() -> None:
    demo = make_ui(settings=Settings())
    assert demo is not None


def _markdown_values(demo) -> list[str]:
    return [str(getattr(b, "value", "")) for b in demo.blocks.values() if type(b).__name__ == "Markdown"]


def test_demo_banner_only_shown_in_demo_mode() -> None:
    assert any("Demo mode" in v for v in _markdown_values(make_ui(settings=Settings(demo_mode=True))))
    assert not any("Demo mode" in v for v in _markdown_values(make_ui(settings=Settings(demo_mode=False))))


def _blocks(demo, kind: str) -> list:
    return [b for b in demo.blocks.values() if type(b).__name__ == kind]


def _ui() -> object:
    return make_ui(settings=Settings(_env_file=None))


def test_the_chat_grows_with_the_window_instead_of_a_fixed_height() -> None:
    (chat,) = _blocks(_ui(), "Chatbot")
    assert chat.height == "70vh"


def test_the_idea_box_starts_empty_and_four_examples_are_offered() -> None:
    demo = _ui()
    (idea,) = [b for b in _blocks(demo, "Textbox") if b.label == "Describe your idea"]
    assert not idea.value
    (examples,) = _blocks(demo, "Dataset")
    assert len(examples.samples) == 4
    assert not any("photographers" in str(sample) for sample in examples.samples)


def test_the_settings_accordion_holds_rounds_panel_mode_autopilot_and_the_model_profile() -> None:
    demo = _ui()
    (settings_box,) = [b for b in _blocks(demo, "Accordion") if b.label == "Settings"]

    def descendants(block) -> list:
        return [d for child in getattr(block, "children", []) for d in (child, *descendants(child))]

    inside = {type(b).__name__ + ":" + str(getattr(b, "label", "")) for b in descendants(settings_box)}
    assert "Slider:Rounds per speaker" in inside
    assert any(name.startswith("Dropdown:Panel mode") for name in inside)
    assert any(name.startswith("Checkbox:Autopilot") for name in inside)
    assert any("gpt-" in str(getattr(b, "value", "")) for b in descendants(settings_box) if type(b).__name__ == "Markdown")


def test_autopilot_is_off_and_the_panel_mode_defaults_to_the_setting() -> None:
    demo = _ui()
    (autopilot,) = [b for b in _blocks(demo, "Checkbox") if str(b.label).startswith("Autopilot")]
    assert autopilot.value is False
    (mode,) = [b for b in _blocks(demo, "Dropdown") if str(b.label).startswith("Panel mode")]
    assert mode.value == "moderated" and [c[1] for c in mode.choices] == ["moderated", "round_robin"]


def test_there_is_a_stop_button_that_cancels_the_running_event() -> None:
    demo = _ui()
    assert [b for b in _blocks(demo, "Button") if b.value == "Stop"]
    assert any(getattr(fn, "cancels", None) for fn in demo.fns.values())


def test_every_gate_has_its_own_hidden_form_and_there_is_no_shared_decision_radio() -> None:
    from idea_to_mvp.ui.gates import GATES

    demo = _ui()
    panels = [b for b in _blocks(demo, "Column") if "gate-form" in (getattr(b, "elem_classes", None) or [])]
    assert len(panels) == len(GATES) and all(panel.visible is False for panel in panels)
    assert not [b for b in _blocks(demo, "Radio") if b.label == "Decision"]
    assert [b for b in _blocks(demo, "Button") if b.value == "Use all suggestions"]
    assert [b for b in _blocks(demo, "Code")]  # the blueprint editor


def test_stop_cancels_every_way_of_starting_a_run() -> None:
    from idea_to_mvp.ui.gates import GATES

    demo = _ui()
    stop = [fn for fn in demo.fns.values() if getattr(fn, "cancels", None)]
    assert stop and len(stop[0].cancels) == 1 + sum(len(spec.actions) for spec in GATES.values())
