import pytest
from pydantic import ValidationError

from app.commands.schema import Operation, ParsedCommand


def test_minimal_valid_command():
    cmd = ParsedCommand(operation=Operation.HELP)
    assert cmd.operation == Operation.HELP
    assert cmd.attachment is None
    assert cmd.requires_clarification is False


def test_rejects_unknown_operation():
    with pytest.raises(ValidationError):
        ParsedCommand(operation="delete_everything")


def test_rejects_extra_fields_hallucinated_by_llm():
    with pytest.raises(ValidationError):
        ParsedCommand.model_validate(
            {
                "operation": "help",
                "shell_command": "rm -rf /",
            }
        )


@pytest.mark.parametrize(
    "bad_attachment",
    [
        "../../etc/passwd",
        "..\\..\\windows\\system32\\config",
        "/etc/passwd",
        "C:\\Windows\\System32\\config",
        "foo/bar.vtt",
        "foo\\bar.vtt",
    ],
)
def test_rejects_path_like_attachment(bad_attachment):
    with pytest.raises(ValidationError):
        ParsedCommand(operation=Operation.SUBMIT_TRANSCRIPT, attachment=bad_attachment)


def test_accepts_bare_filename_attachment():
    cmd = ParsedCommand(operation=Operation.SUBMIT_TRANSCRIPT, attachment="meeting.vtt")
    assert cmd.attachment == "meeting.vtt"


def test_rejects_bad_job_id_format():
    with pytest.raises(ValidationError):
        ParsedCommand(operation=Operation.STATUS, job_id="not-a-job-id")


def test_accepts_valid_job_id_format():
    cmd = ParsedCommand(operation=Operation.STATUS, job_id="DIAR-2026-0811-0017")
    assert cmd.job_id == "DIAR-2026-0811-0017"


def test_requires_clarification_question_when_flag_set():
    with pytest.raises(ValidationError):
        ParsedCommand(operation=Operation.HELP, requires_clarification=True)


def test_clarification_with_question_is_valid():
    cmd = ParsedCommand(
        operation=Operation.SUBMIT_TRANSCRIPT,
        requires_clarification=True,
        clarification_question="Which attachment did you mean?",
    )
    assert cmd.requires_clarification is True


def test_blank_strings_normalise_to_none():
    cmd = ParsedCommand(operation=Operation.HELP, group_hint="   ")
    assert cmd.group_hint is None
