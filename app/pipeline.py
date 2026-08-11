"""Orchestrates one mail-poll cycle: fetch, authorise, parse via the LLM, validate, and dispatch
to the appropriate handler via an explicit if/elif over the finite operation set (spec S17) -
never a generic tool-call interface. Also flushes the Outbox (all outbound mail, including admin
alerts) through the MailClient.

This is the file that embodies the core architectural boundary from the spec:

    UNTRUSTED (email + LLM) -> Strict Validator -> TRUSTED (deterministic handlers)
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional, Protocol

from app.admin.notifier import AdminCategory, AdminNotifier
from app.auth.authorisation import AuthResultReason, SenderAuthoriser
from app.commands.schema import CommandParsingFailed, Operation
from app.commands.validator import (
    AttachmentMeta,
    ClarificationRequired,
    Rejected,
    ValidationContext,
    validate_command,
)
from app.email_templates.render import (
    render_clarification,
    render_failure,
    render_unrecognised_sender,
)
from app.handlers import cancel as cancel_handler
from app.handlers import help as help_handler
from app.handlers import results as results_handler
from app.handlers import status as status_handler
from app.handlers import submit_transcript
from app.jobs.store import JobStore, Outbox, ProcessedMessageStore
from app.llm.command_parser import EmailCommandParser
from app.mail.base import EmailMessage, MailClient
from app.mail.thread_matcher import ThreadMatcher
from app.settings import LimitsSettings, StorageSettings

logger = logging.getLogger(__name__)


class HasIsAvailable(Protocol):
    def is_available(self) -> bool: ...


class EmailProcessingPipeline:
    def __init__(
        self,
        mail_client: MailClient,
        authoriser: SenderAuthoriser,
        ollama_client: HasIsAvailable,
        command_parser: EmailCommandParser,
        job_store: JobStore,
        processed_store: ProcessedMessageStore,
        outbox: Outbox,
        admin_notifier: AdminNotifier,
        thread_matcher: ThreadMatcher,
        storage: StorageSettings,
        limits: LimitsSettings,
        admin_email: Optional[str],
    ):
        self._mail = mail_client
        self._authoriser = authoriser
        self._ollama = ollama_client
        self._parser = command_parser
        self._job_store = job_store
        self._processed = processed_store
        self._outbox = outbox
        self._admin = admin_notifier
        self._thread_matcher = thread_matcher
        self._storage = storage
        self._limits = limits
        self._admin_email = admin_email

    # --- inbound: fetch, authorise, parse, validate, dispatch ---------------------------------

    def poll_once(self) -> int:
        messages = self._mail.fetch_new_messages()
        for msg in messages:
            try:
                self._process_message(msg)
            except Exception:
                # A single malformed/unexpected message must never take down polling for
                # every other message in the batch.
                logger.exception("Unhandled error processing message %s", msg.message_id)
        return len(messages)

    def _process_message(self, msg: EmailMessage) -> None:
        auth_result = self._authoriser.authorise(msg.from_address, msg.auth_signals)
        dedup_sender = auth_result.sender_email or msg.from_address

        if not self._processed.begin(msg.message_id, dedup_sender, auth_result.reason.value):
            # Either already fully handled before (true duplicate) or its crash-recovery
            # retry budget is exhausted (see ProcessedMessageStore) - either way we're
            # definitively done with it, so it shouldn't keep resurfacing on future polls.
            logger.info("Skipping duplicate or exhausted message %s", msg.message_id)
            self._mail.mark_processed(msg.provider_ref)
            return

        if auth_result.reason == AuthResultReason.UNRECOGNISED_IN_DOMAIN:
            subject, body = render_unrecognised_sender(self._admin_email)
            self._outbox.enqueue(to_email=auth_result.sender_email, subject=subject, body_text=body)
            self._admin.alert(
                AdminCategory.UNAUTHORISED_SENDER,
                f"Unrecognised in-domain sender attempted to use the system: {auth_result.sender_email}",
            )
            self._processed.finalize(msg.message_id, outcome="unrecognised_sender_replied")
            self._mail.mark_processed(msg.provider_ref)
            return

        if auth_result.reason != AuthResultReason.AUTHORISED:
            # unauthorised_external / unauthenticated / malformed_sender - silent drop, no
            # reply (spec decision: replying would confirm a monitored mailbox exists).
            self._admin.alert(
                AdminCategory.UNAUTHORISED_SENDER,
                f"Rejected sender ({auth_result.reason.value}): {msg.from_address}",
            )
            self._processed.finalize(msg.message_id, outcome="rejected_silently")
            self._mail.mark_processed(msg.provider_ref)
            return

        sender_email = auth_result.sender_email
        sender_user_id = auth_result.user_id

        if not self._ollama.is_available():
            # Deliberately do NOT finalize or mark_processed: the message stays 'in_progress'
            # in our ledger and unread on the provider, so it's retried on a later poll once
            # Ollama comes back (spec S20: "do not process the command; leave the email
            # available for later retry").
            logger.warning("Ollama unavailable - deferring message %s", msg.message_id)
            self._admin.alert(AdminCategory.INFRASTRUCTURE, "Ollama is unreachable")
            return

        attachment_filenames = [a.filename for a in msg.attachments]
        thread_job_id = self._thread_matcher.match(msg, sender_email)

        try:
            parsed_cmd = self._parser.parse_email(msg.body_text, attachment_filenames, thread_job_id)
        except CommandParsingFailed as e:
            self._reply_and_finalize(
                msg,
                *render_clarification(
                    "I couldn't understand your request. Could you rephrase it, mentioning "
                    "clearly what you'd like me to do (submit a transcript, check status, see "
                    "results, or cancel a request)?"
                ),
                outcome="llm_parse_failed",
            )
            self._admin.alert(
                AdminCategory.LLM_PARSE_FAILURE, f"Could not parse email from {sender_email}: {e}"
            )
            return

        attachments_meta = [
            AttachmentMeta(a.filename, a.size_bytes, a.content_type) for a in msg.attachments
        ]
        ctx = ValidationContext(
            attachments=attachments_meta,
            thread_job_id=thread_job_id,
            sender_email=sender_email,
            job_store=self._job_store,
            limits=self._limits,
        )

        try:
            validated = validate_command(parsed_cmd, ctx)
        except ClarificationRequired as e:
            self._reply_and_finalize(
                msg, *render_clarification(e.question), outcome="clarification_sent",
                operation=parsed_cmd.operation.value,
            )
            return
        except Rejected as e:
            self._reply_and_finalize(
                msg, *render_failure(str(e)), outcome="rejected", operation=parsed_cmd.operation.value,
            )
            return

        try:
            job_id = self._dispatch(validated, msg, sender_email, sender_user_id)
        except ClarificationRequired as e:
            self._reply_and_finalize(
                msg, *render_clarification(e.question), outcome="clarification_sent",
                operation=validated.operation.value,
            )
            return
        except Rejected as e:
            self._reply_and_finalize(
                msg, *render_failure(str(e)), outcome="rejected", operation=validated.operation.value,
            )
            return

        self._processed.finalize(
            msg.message_id, outcome="handled", job_id=job_id, operation=validated.operation.value
        )
        self._mail.mark_processed(msg.provider_ref)

    def _dispatch(self, validated, msg: EmailMessage, sender_email: str, sender_user_id: int) -> Optional[str]:
        """The explicit, finite dispatch (spec S17) - deliberately not a generic
        operation-name-to-callable lookup, so the full set of possible actions is visible here."""
        if validated.operation == Operation.SUBMIT_TRANSCRIPT:
            outcome = submit_transcript.accept(
                msg.attachments,
                validated,
                sender_email,
                sender_user_id,
                msg.message_id,
                msg.received_at,
                self._job_store,
                self._outbox,
                self._storage,
            )
        elif validated.operation == Operation.STATUS:
            outcome = status_handler.handle(validated, sender_email, self._job_store, self._outbox)
        elif validated.operation == Operation.RESULTS:
            outcome = results_handler.handle(validated, sender_email, self._job_store, self._outbox)
        elif validated.operation == Operation.CANCEL:
            outcome = cancel_handler.handle(validated, sender_email, self._job_store, self._outbox)
        elif validated.operation == Operation.HELP:
            outcome = help_handler.handle(sender_email, self._outbox)
        else:
            raise Rejected(f"Unsupported operation: {validated.operation!r}")  # unreachable
        return outcome.job_id

    def _reply_and_finalize(
        self, msg: EmailMessage, subject: str, body: str, outcome: str, operation: Optional[str] = None
    ) -> None:
        self._outbox.enqueue(
            to_email=msg.from_address,
            subject=subject,
            body_text=body,
            in_reply_to=msg.message_id,
        )
        self._processed.finalize(msg.message_id, outcome=outcome, operation=operation)
        self._mail.mark_processed(msg.provider_ref)

    # --- outbound: flush queued mail through the provider --------------------------------------

    def flush_outbox(self) -> int:
        sent = 0
        for message in self._outbox.pending():
            try:
                attachments = (
                    [(Path(p).name, Path(p).read_bytes()) for p in message.attachments]
                    if message.attachments
                    else None
                )
                provider_message_id = self._mail.send_email(
                    to=message.to_email,
                    subject=message.subject,
                    body_text=message.body_text,
                    attachments=attachments,
                    in_reply_to=message.in_reply_to_message_id,
                    references=message.references_header,
                )
                self._outbox.mark_sent(message.id)
                if message.job_id:
                    self._job_store.update(message.job_id, last_response_message_id=provider_message_id)
                sent += 1
            except Exception as e:
                logger.exception("Failed to send outbox message %s", message.id)
                self._outbox.mark_failed(message.id, str(e))
        return sent
