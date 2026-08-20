from __future__ import annotations

import io
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch
from uuid import uuid4

from koawa_agent_v2.execution.loop import AgentLoopCancelled, CancellationToken
from koawa_agent_v2.model.protocol import ModelRequest, UserMessage
from koawa_agent_v2.model import openai_client as client_module
from koawa_agent_v2.model.openai_client import (
    OpenAICompatibleChatClient,
    OpenAICompatibleClientError,
)


def _request() -> ModelRequest:
    return ModelRequest(
        uuid4(),
        "openai_compatible",
        "gpt-test",
        (UserMessage("input-1", "hello"),),
    )


class _HeartbeatResponse:
    def __init__(self, *, on_read=None) -> None:
        self.status = 200
        self.headers = {"Content-Type": "text/event-stream"}
        self.closed = False
        self._on_read = on_read

    def readline(self, _size=-1) -> bytes:
        if self._on_read is not None:
            self._on_read()
        return b": keep-alive\n"

    def close(self) -> None:
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()


class _Open:
    def __init__(self, response) -> None:
        self.response = response
        self.calls = 0

    def __call__(self, request, *, timeout):
        self.calls += 1
        return self.response


class D2TransportHardeningTest(unittest.TestCase):
    def test_bearer_key_requires_https(self) -> None:
        with self.assertRaisesRegex(ValueError, "requires an HTTPS"):
            OpenAICompatibleChatClient(
                "http://provider.example/v1",
                api_key="secret-key",
            )

    def test_default_redirect_handler_refuses_to_build_cross_origin_request(self) -> None:
        request = urllib.request.Request(
            "https://provider.example/v1/chat/completions",
            data=b"{}",
            headers={"Authorization": "Bearer secret-key"},
            method="POST",
        )
        handler = client_module._RejectRedirectHandler()

        with self.assertRaises(urllib.error.HTTPError) as raised:
            handler.redirect_request(
                request,
                io.BytesIO(),
                302,
                "redirect",
                {},
                "https://attacker.example/steal",
            )

        self.assertEqual(request.full_url, raised.exception.filename)
        self.assertNotIn("attacker.example", raised.exception.filename)
        raised.exception.close()

    def test_heartbeat_stream_observes_cooperative_cancellation(self) -> None:
        token = CancellationToken()
        response = _HeartbeatResponse(on_read=token.cancel)
        opener = _Open(response)
        client = OpenAICompatibleChatClient(
            "https://provider.example/v1",
            urlopen=opener,
        )

        with self.assertRaises(AgentLoopCancelled):
            tuple(
                client.stream_controlled(
                    _request(),
                    progress_guard=token.raise_if_cancelled,
                )
            )

        self.assertEqual(1, opener.calls)
        self.assertTrue(response.closed)

    def test_heartbeat_stream_has_a_total_deadline(self) -> None:
        response = _HeartbeatResponse()
        opener = _Open(response)
        client = OpenAICompatibleChatClient(
            "https://provider.example/v1",
            max_stream_seconds=1.0,
            urlopen=opener,
        )
        clock = iter((0.0, 0.0, 0.0, 0.0, 2.0))

        with patch.object(client_module, "monotonic", side_effect=lambda: next(clock)):
            with self.assertRaises(OpenAICompatibleClientError) as raised:
                tuple(client.stream(_request()))

        self.assertEqual("openai.stream_deadline_exceeded", raised.exception.code)
        self.assertTrue(response.closed)


if __name__ == "__main__":
    unittest.main()
