from pathlib import Path

from app.commands.schema import Operation
from app.commands.validator import ValidatedCommand
from app.handlers import assess_query
from app.jobs.store import Outbox


def _cmd(transcript_focus=None, github_focus=None, trello_focus=None):
    return ValidatedCommand(
        operation=Operation.ASSESS_QUERY,
        transcript_focus=transcript_focus,
        github_focus=github_focus,
        trello_focus=trello_focus,
    )


def test_assess_query_reply_lists_only_populated_sources(db_path: Path):
    outbox = Outbox(db_path)
    cmd = _cmd(transcript_focus="mentions of the API redesign", github_focus="open API issues")

    outcome = assess_query.handle(cmd, "alice@uni.ac.uk", outbox)

    assert outcome.outcome_type == "assess_query_reply"
    body = outbox.pending()[0].body_text
    assert "mentions of the API redesign" in body
    assert "open API issues" in body
    assert "Trello board:" not in body


def test_assess_query_reply_includes_dry_run_disclaimer(db_path: Path):
    outbox = Outbox(db_path)
    cmd = _cmd(trello_focus="open cards tagged API")

    assess_query.handle(cmd, "alice@uni.ac.uk", outbox)

    body = outbox.pending()[0].body_text.lower()
    assert "dry run" in body or "isn't built yet" in body
    assert "i found" not in body
    assert "i checked" not in body
