from src.adapters.devin import attention_message
from src.gateway.main import strip_mention

BOT = "BasaniteInternBot"


def test_mention_required_in_groups():
    assert strip_mention("what's in the pipeline?", BOT) is None
    assert strip_mention("", BOT) is None


def test_mention_stripped_case_insensitive():
    assert strip_mention("@BasaniteInternBot add a todo", BOT) == "add a todo"
    assert strip_mention("@basaniteinternbot add a todo", BOT) == "add a todo"
    assert strip_mention("hey @BasaniteInternBot, status?", BOT) == "hey , status?"


def test_attention_on_halts_and_questions():
    assert "waiting for your input" in attention_message(
        "running", "waiting_for_user", "fix tests", "https://x")
    assert "action approval" in attention_message(
        "running", "waiting_for_approval", "fix tests", "https://x")
    assert "NOT making progress" in attention_message(
        "suspended", "out_of_credits", "fix tests", "https://x")
    assert "error" in attention_message("error", "", "fix tests", "https://x")
    assert "finished" in attention_message("exit", "finished", "fix tests", "https://x")


def test_no_noise_for_routine_states():
    assert attention_message("running", "working", "fix tests", "https://x") is None
    assert attention_message("new", "", "fix tests", "https://x") is None
    assert attention_message("claimed", "", "fix tests", "https://x") is None
    # user asked for the suspension themselves; no alert needed
    assert attention_message("suspended", "user_request", "fix tests", "https://x") is None
