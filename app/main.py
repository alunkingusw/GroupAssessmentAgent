"""Entry point: wires all components together and runs the mail-polling pipeline and the job
worker as two threads sharing the same SQLite-backed stores, until interrupted."""
from __future__ import annotations

import logging
import signal
import threading
import time

from app.admin.notifier import AdminNotifier
from app.auth.authorisation import SenderAuthoriser
from app.diarisation.client import DiarisationClient
from app.jobs.store import JobStore, Outbox, ProcessedMessageStore
from app.jobs.worker import JobWorker
from app.llm.command_parser import EmailCommandParser
from app.llm.ollama_client import OllamaClient
from app.logging_config import configure_logging
from app.mail.base import MailClient
from app.mail.fake_client import FakeMailClient
from app.mail.graph_client import GraphMailClient
from app.mail.thread_matcher import ThreadMatcher
from app.pipeline import EmailProcessingPipeline
from app.settings import Settings, load_settings
from app.storage.db import init_db

logger = logging.getLogger(__name__)


def build_mail_client(settings: Settings) -> MailClient:
    if settings.mail.provider == "graph":
        if not (settings.graph_tenant_id and settings.graph_client_id and settings.graph_client_secret):
            raise RuntimeError(
                "mail.provider is 'graph' but GRAPH_TENANT_ID/GRAPH_CLIENT_ID/GRAPH_CLIENT_SECRET "
                "are not all set in .env"
            )
        return GraphMailClient(
            settings.graph_tenant_id,
            settings.graph_client_id,
            settings.graph_client_secret,
            settings.mail.mailbox_upn,
        )
    if settings.mail.provider == "fake":
        return FakeMailClient()
    raise RuntimeError(
        f"Unsupported mail.provider {settings.mail.provider!r} - only 'graph' and 'fake' are "
        "currently implemented (see Specification.md S5/S7 for extending this to IMAP)."
    )


def run(settings: Settings) -> None:
    settings.ensure_storage_dirs()
    configure_logging(settings)
    init_db(settings.storage.db_path)

    mail_client = build_mail_client(settings)
    job_store = JobStore(settings.storage.db_path)
    processed_store = ProcessedMessageStore(settings.storage.db_path)
    outbox = Outbox(settings.storage.db_path)
    admin_notifier = AdminNotifier(
        settings.storage.db_path, outbox, settings.admin_email, settings.admin.alert_cooldown_minutes
    )
    authoriser = SenderAuthoriser(
        settings.authorisation.group_owners,
        settings.authorised_email_domains,
        settings.authorisation.require_auth_pass,
    )
    ollama_client = OllamaClient(
        settings.llm.host, settings.llm.model, settings.llm.request_timeout_seconds
    )
    command_parser = EmailCommandParser(
        ollama_client, settings.llm.max_parse_retries, settings.limits.max_email_body_chars
    )
    thread_matcher = ThreadMatcher(job_store)
    diarisation_client = DiarisationClient(
        settings.backend.base_url,
        settings.backend.request_timeout_seconds,
        settings.backend.max_retry_attempts,
        settings.backend.retry_backoff_seconds,
    )

    pipeline = EmailProcessingPipeline(
        mail_client,
        authoriser,
        ollama_client,
        command_parser,
        job_store,
        processed_store,
        outbox,
        admin_notifier,
        thread_matcher,
        settings.storage,
        settings.limits,
        settings.admin_email,
    )
    worker = JobWorker(job_store, diarisation_client, outbox, admin_notifier, settings.storage)

    stop_event = threading.Event()

    def _handle_signal(signum, frame):
        logger.info("Received signal %s, shutting down...", signum)
        stop_event.set()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    def _mail_loop():
        while not stop_event.is_set():
            try:
                processed = pipeline.poll_once()
                sent = pipeline.flush_outbox()
                if processed or sent:
                    logger.info("Poll cycle: processed=%s sent=%s", processed, sent)
            except Exception:
                logger.exception("Error in mail polling loop")
            stop_event.wait(settings.mail.poll_interval_seconds)

    worker_thread = threading.Thread(
        target=worker.run_forever,
        args=(settings.mail.poll_interval_seconds, stop_event),
        name="job-worker",
        daemon=True,
    )
    mail_thread = threading.Thread(target=_mail_loop, name="mail-pipeline", daemon=True)

    logger.info("GroupAssessmentAgent starting (mail.provider=%s, llm.model=%s)", settings.mail.provider, settings.llm.model)
    worker_thread.start()
    mail_thread.start()

    try:
        while not stop_event.is_set():
            time.sleep(0.5)
    except KeyboardInterrupt:
        stop_event.set()
    finally:
        worker_thread.join(timeout=5.0)
        mail_thread.join(timeout=5.0)
        ollama_client.close()
        diarisation_client.close()
        logger.info("GroupAssessmentAgent stopped.")


def main() -> None:
    settings = load_settings()
    run(settings)


if __name__ == "__main__":
    main()
