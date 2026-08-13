"""Thin, deterministic wrapper over the real backend's REST API.

One method per endpoint the email interface actually needs - never a generic "call any
endpoint" interface, mirroring the same "explicit finite operations" principle used for the
LLM's command schema (spec S17). See
D:\\Documents\\Documents\\VS Projects\\group_meeting_transcripts\\backend\\routes for the
endpoints these wrap. There are deliberately no get_status/cancel_job methods - those backend
endpoints don't exist.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import httpx
from tenacity import Retrying, retry_if_exception_type, stop_after_attempt, wait_exponential


class DiarisationApiError(Exception):
    """Base class for all errors raised by DiarisationClient."""


class AuthError(DiarisationApiError):
    """401/403 - the JWT was rejected or expired. Not retried."""


class NotFoundError(DiarisationApiError):
    """404 - the group/meeting/member referenced doesn't exist. Not retried."""


class ConflictError(DiarisationApiError):
    """409 - e.g. an alias already registered. Not retried."""


class ClientError(DiarisationApiError):
    """Any other 4xx - malformed request. Not retried."""


class TransientError(DiarisationApiError):
    """Timeouts and 5xx - retried internally with backoff."""


@dataclass
class GroupSummary:
    id: int
    name: str


@dataclass
class MemberSummary:
    id: int
    name: str


@dataclass
class GroupDetail:
    id: int
    name: str
    members: list[MemberSummary] = field(default_factory=list)


@dataclass
class MeetingSummary:
    id: int
    group_id: int
    date: str


@dataclass
class RawFileSummary:
    id: int
    file_name: str
    human_name: str
    type: str


@dataclass
class AttendeeSummary:
    id: int
    name: str


@dataclass
class TranscriptChunk:
    chunk_id: str
    meeting_id: str
    meeting_title: str
    meeting_date: str
    speaker: str
    text: str
    start_ts: str
    end_ts: str

    def citation(self) -> str:
        return f"[{self.meeting_title}, {self.meeting_date}, {self.start_ts}-{self.end_ts}, {self.speaker}]"


class DiarisationClient:
    def __init__(
        self,
        base_url: str,
        timeout: float = 10.0,
        max_retry_attempts: int = 3,
        retry_backoff_seconds: float = 1.0,
    ):
        self._client = httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout)
        self._retryer = Retrying(
            reraise=True,
            retry=retry_if_exception_type(TransientError),
            stop=stop_after_attempt(max_retry_attempts),
            wait=wait_exponential(multiplier=retry_backoff_seconds, min=retry_backoff_seconds),
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "DiarisationClient":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    # --- endpoint methods -------------------------------------------------

    def login(self, user_id: int) -> str:
        """POST /users/login - trades a backend user_id for a JWT."""
        resp = self._request("POST", "/users/login", data={"username": str(user_id)})
        return resp.json()["access_token"]

    def list_groups(self, token: str) -> list[GroupSummary]:
        """GET /groups/ - only the groups the JWT's user belongs to."""
        resp = self._request("GET", "/groups/", headers=_auth(token))
        return [GroupSummary(id=g["id"], name=g["name"]) for g in resp.json()]

    def get_group(self, token: str, group_id: int) -> GroupDetail:
        """GET /groups/{id}"""
        resp = self._request("GET", f"/groups/{group_id}", headers=_auth(token))
        data = resp.json()
        return GroupDetail(
            id=data["id"],
            name=data["name"],
            members=[MemberSummary(id=m["id"], name=m["name"]) for m in data.get("members", [])],
        )

    def create_meeting(self, token: str, group_id: int, date: datetime) -> MeetingSummary:
        """POST /groups/{id}/meetings/"""
        resp = self._request(
            "POST",
            f"/groups/{group_id}/meetings/",
            headers=_auth(token),
            json={"date": date.isoformat()},
        )
        data = resp.json()
        return MeetingSummary(id=data["id"], group_id=data["group_id"], date=data["date"])

    def upload_file(
        self, token: str, group_id: int, meeting_id: int, filename: str, content: bytes
    ) -> RawFileSummary:
        """POST /groups/{id}/meetings/{id}/upload/ - multipart file upload."""
        resp = self._request(
            "POST",
            f"/groups/{group_id}/meetings/{meeting_id}/upload/",
            headers=_auth(token),
            files={"file": (filename, content)},
        )
        data = resp.json()
        return RawFileSummary(
            id=data["id"],
            file_name=data["file_name"],
            human_name=data["human_name"],
            type=data["type"],
        )

    def add_attendee(
        self, token: str, group_id: int, meeting_id: int, member_id: int
    ) -> AttendeeSummary:
        """POST /groups/{id}/meetings/{id}/attendees"""
        resp = self._request(
            "POST",
            f"/groups/{group_id}/meetings/{meeting_id}/attendees",
            headers=_auth(token),
            json={"member_id": member_id},
        )
        data = resp.json()
        return AttendeeSummary(id=data["id"], name=data["name"])

    def resolve_aliases(
        self,
        token: str,
        group_id: int,
        names: list[str],
        source: Optional[str] = "transcript_name",
    ) -> dict[str, Optional[int]]:
        """POST /groups/{id}/aliases/resolve - VTT speaker labels -> known member_id (or null)."""
        body: dict = {"names": names}
        if source is not None:
            body["source"] = source
        resp = self._request(
            "POST", f"/groups/{group_id}/aliases/resolve", headers=_auth(token), json=body
        )
        return resp.json()

    def search_transcripts(
        self, token: str, group_id: int, query: str, meeting_id: Optional[int] = None
    ) -> list[TranscriptChunk]:
        """POST /groups/{id}/transcripts/search - semantic search over this group's already
        submitted/transcribed meetings. Retrieval only, no LLM involved on the backend side."""
        body: dict = {"query": query}
        if meeting_id is not None:
            body["meeting_id"] = meeting_id
        resp = self._request(
            "POST", f"/groups/{group_id}/transcripts/search", headers=_auth(token), json=body
        )
        return [
            TranscriptChunk(
                chunk_id=r["chunk_id"],
                meeting_id=r["meeting_id"],
                meeting_title=r["meeting_title"],
                meeting_date=r["meeting_date"],
                speaker=r["speaker"],
                text=r["text"],
                start_ts=r["start_ts"],
                end_ts=r["end_ts"],
            )
            for r in resp.json().get("results", [])
        ]

    # --- request plumbing --------------------------------------------------

    def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        return self._retryer(self._do_request, method, path, **kwargs)

    def _do_request(self, method: str, path: str, **kwargs) -> httpx.Response:
        try:
            resp = self._client.request(method, path, **kwargs)
        except httpx.TimeoutException as e:
            raise TransientError(f"Timeout calling {method} {path}: {e}") from e
        except httpx.TransportError as e:
            raise TransientError(f"Transport error calling {method} {path}: {e}") from e

        if resp.status_code >= 500:
            raise TransientError(f"{method} {path} returned {resp.status_code}: {resp.text[:200]}")
        if resp.status_code in (401, 403):
            raise AuthError(f"{method} {path} returned {resp.status_code}: {resp.text[:200]}")
        if resp.status_code == 404:
            raise NotFoundError(f"{method} {path} returned 404: {resp.text[:200]}")
        if resp.status_code == 409:
            raise ConflictError(f"{method} {path} returned 409: {resp.text[:200]}")
        if resp.status_code >= 400:
            raise ClientError(f"{method} {path} returned {resp.status_code}: {resp.text[:200]}")
        return resp


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}
