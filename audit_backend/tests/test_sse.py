from __future__ import annotations

import asyncio
from types import SimpleNamespace

from app.api.v1.projects import stream_events


def test_sse_resumes_after_last_event_id_and_emits_event_id() -> None:
    class Request:
        headers = {"last-event-id": "41"}

        async def is_disconnected(self) -> bool:
            return False

    class Query:
        def filter(self, *_args):
            return self

        def order_by(self, *_args):
            return self

        def all(self):
            return [SimpleNamespace(id=42, rule_key="TM-01", event_type="rule_done", payload={"verdict": "pass"})]

    class Session:
        def query(self, *_args):
            return Query()

    async def read_first_event() -> str:
        response = await stream_events("project-1", Request(), Session())  # type: ignore[arg-type]
        chunk = await anext(response.body_iterator)
        await response.body_iterator.aclose()
        return chunk

    assert asyncio.run(read_first_event()).startswith(
        'id: 42\nevent: rule_done\ndata: {"rule_key": "TM-01", "verdict": "pass"}\n\n'
    )
