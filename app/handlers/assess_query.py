from __future__ import annotations

from app.commands.validator import ValidatedCommand
from app.email_templates.render import render_assess_plan
from app.handlers.base import HandlerOutcome
from app.jobs.store import Outbox


def handle(validated_cmd: ValidatedCommand, sender_email: str, outbox: Outbox) -> HandlerOutcome:
    subject, body = render_assess_plan(
        transcript_focus=validated_cmd.transcript_focus,
        github_focus=validated_cmd.github_focus,
        trello_focus=validated_cmd.trello_focus,
    )
    outbox.enqueue(to_email=sender_email, subject=subject, body_text=body)
    return HandlerOutcome("assess_query_reply")
