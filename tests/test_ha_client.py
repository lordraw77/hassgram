"""Tests for the HTTP client: error mapping, the two caches, and the templates.

Every request is served by an :class:`httpx.MockTransport`, so the suite never
opens a socket. The transport is swapped into the client *after* construction,
which keeps the real ``base_url`` and the real ``Authorization`` header in play
-- both are asserted below.
"""

from __future__ import annotations

import asyncio
import unittest

import httpx

import tests.fakes  # noqa: F401  -- puts the project root on sys.path

from ha_client import HomeAssistantClient, HomeAssistantError


def make_client(handler, **kwargs) -> HomeAssistantClient:
    """Build a client whose requests are answered by ``handler``.

    Args:
        handler: A callable taking an :class:`httpx.Request` and returning an
            :class:`httpx.Response`, as :class:`httpx.MockTransport` expects.
        **kwargs: Forwarded to :class:`HomeAssistantClient`.

    Returns:
        The client. Only its transport is replaced, so the base URL, the bearer
        header and the timeout are the real ones.
    """
    ha = HomeAssistantClient("http://ha.test:8123/api/", "TOKEN", **kwargs)
    ha._client._transport = httpx.MockTransport(handler)
    return ha


def json_handler(payload, status=200):
    def handler(request):
        return httpx.Response(status, json=payload)
    return handler


class TestRequest(unittest.IsolatedAsyncioTestCase):
    async def test_base_url_and_bearer_token(self):
        seen = {}

        def handler(request):
            seen["url"] = str(request.url)
            seen["auth"] = request.headers.get("authorization")
            return httpx.Response(200, json=[])

        ha = make_client(handler)
        await ha.states()
        self.assertEqual(seen["url"], "http://ha.test:8123/api/states")
        self.assertEqual(seen["auth"], "Bearer TOKEN")
        await ha.aclose()

    async def test_trailing_slash_is_stripped(self):
        self.assertEqual(make_client(json_handler([])).base_url, "http://ha.test:8123/api")

    async def test_transport_failure_becomes_a_network_error(self):
        def handler(request):
            raise httpx.ConnectError("connection refused")

        ha = make_client(handler)
        with self.assertRaises(HomeAssistantError) as ctx:
            await ha.states()
        self.assertEqual(ctx.exception.kind, "network")
        self.assertIsNone(ctx.exception.status)
        self.assertIn("connection refused", ctx.exception.detail)

    async def test_error_response_becomes_an_http_error(self):
        ha = make_client(json_handler({"message": "unauthorised"}, status=401))
        with self.assertRaises(HomeAssistantError) as ctx:
            await ha.states()
        self.assertEqual(ctx.exception.kind, "http")
        self.assertEqual(ctx.exception.status, 401)
        self.assertIn("unauthorised", ctx.exception.detail)

    async def test_error_detail_is_truncated(self):
        def handler(request):
            return httpx.Response(500, text="x" * 5000)

        ha = make_client(handler)
        with self.assertRaises(HomeAssistantError) as ctx:
            await ha.states()
        self.assertEqual(len(ctx.exception.detail), 200)

    async def test_error_str_is_for_the_log_not_the_user(self):
        """It must not read like a sentence: ``bot.ha_error_text`` writes those."""
        exc = HomeAssistantError("connection refused", kind="network")
        self.assertEqual(str(exc), "network: connection refused")
        self.assertEqual(str(HomeAssistantError("nope", kind="http", status=404)), "http 404: nope")

    async def test_non_json_response_comes_back_as_text(self):
        def handler(request):
            return httpx.Response(200, text="plain body")

        ha = make_client(handler)
        self.assertEqual(await ha.render_template("{{ 1 }}"), "plain body")

    async def test_ping(self):
        ha = make_client(json_handler({"message": "API running."}))
        self.assertEqual(await ha.ping(), "API running.")

    async def test_ping_tolerates_an_unexpected_shape(self):
        def handler(request):
            return httpx.Response(200, text="whatever")

        self.assertEqual(await make_client(handler).ping(), "whatever")


class TestStatesCache(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.requests = 0

        def handler(request):
            self.requests += 1
            return httpx.Response(200, json=[{"entity_id": "light.x", "state": "on"}])

        self.ha = make_client(handler)

    async def test_second_read_is_served_from_the_cache(self):
        await self.ha.states()
        await self.ha.states()
        self.assertEqual(self.requests, 1)

    async def test_zero_max_age_forces_a_refresh(self):
        await self.ha.states()
        await self.ha.states(max_age=0)
        self.assertEqual(self.requests, 2)

    async def test_invalidate_forces_a_refresh(self):
        await self.ha.states()
        self.ha.invalidate_states()
        await self.ha.states()
        self.assertEqual(self.requests, 2)

    async def test_concurrent_readers_share_one_request(self):
        """The lock is what stops a burst of handlers from stampeding the API."""
        await asyncio.gather(*(self.ha.states() for _ in range(10)))
        self.assertEqual(self.requests, 1)

    async def test_call_service_invalidates_the_cache(self):
        await self.ha.states()
        await self.ha.call_service("light", "turn_on", {"entity_id": ["light.x"]})
        await self.ha.states()
        self.assertEqual(self.requests, 3)  # states, service, states

    async def test_call_service_posts_to_the_right_path(self):
        seen = {}

        def handler(request):
            seen["url"] = str(request.url)
            seen["body"] = request.read()
            return httpx.Response(200, json=[])

        ha = make_client(handler)
        await ha.call_service("switch", "turn_off", {"entity_id": ["switch.a", "switch.b"]})
        self.assertEqual(seen["url"], "http://ha.test:8123/api/services/switch/turn_off")
        self.assertIn(b"switch.b", seen["body"])


class TestAreas(unittest.IsolatedAsyncioTestCase):
    def handler_for(self, body):
        self.renders = 0

        def handler(request):
            self.renders += 1
            return httpx.Response(200, text=body)
        return handler

    async def test_parses_the_tab_separated_rendering(self):
        ha = make_client(self.handler_for("light.a\tSalone\nlight.b\tCamera da letto"))
        self.assertEqual(await ha.areas(), {"light.a": "Salone", "light.b": "Camera da letto"})

    async def test_area_names_may_contain_spaces_and_commas(self):
        ha = make_client(self.handler_for("light.a\tSala, grande"))
        self.assertEqual(await ha.areas(), {"light.a": "Sala, grande"})

    async def test_malformed_lines_are_skipped_not_fatal(self):
        ha = make_client(self.handler_for("light.a\tSalone\ngarbage\n\nlight.b\tCucina"))
        self.assertEqual(await ha.areas(), {"light.a": "Salone", "light.b": "Cucina"})

    async def test_cached_forever(self):
        ha = make_client(self.handler_for("light.a\tSalone"))
        await ha.areas()
        await ha.areas()
        self.assertEqual(self.renders, 1)

    async def test_empty_registry(self):
        ha = make_client(self.handler_for(""))
        self.assertEqual(await ha.areas(), {})


class TestStt(unittest.IsolatedAsyncioTestCase):
    async def test_lists_and_sorts_stt_entities(self):
        payload = [
            {"entity_id": "stt.zeta"}, {"entity_id": "light.x"}, {"entity_id": "stt.alfa"},
        ]
        ha = make_client(json_handler(payload))
        self.assertEqual(await ha.stt_entities(), ["stt.alfa", "stt.zeta"])

    async def test_no_engine_is_an_empty_list_not_an_error(self):
        ha = make_client(json_handler([{"entity_id": "light.x"}]))
        self.assertEqual(await ha.stt_entities(), [])

    async def test_speech_to_text_sends_audio_as_the_body_and_metadata_in_the_header(self):
        seen = {}

        def handler(request):
            seen["url"] = str(request.url)
            seen["body"] = request.read()
            seen["header"] = request.headers.get("x-speech-content")
            seen["content_type"] = request.headers.get("content-type")
            return httpx.Response(200, json={"result": "success", "text": "  accendi il salone  "})

        ha = make_client(handler)
        text = await ha.speech_to_text(b"OggS...", "stt.whisper", language="en-US")
        self.assertEqual(text, "accendi il salone")
        self.assertEqual(seen["url"], "http://ha.test:8123/api/stt/stt.whisper")
        self.assertEqual(seen["body"], b"OggS...")
        self.assertEqual(seen["content_type"], "application/octet-stream")
        self.assertIn("language=en-US", seen["header"])
        self.assertIn("format=ogg", seen["header"])
        self.assertIn("codec=opus", seen["header"])
        # 16000 is what the provider advertises; the ogg container carries the real rate
        self.assertIn("sample_rate=16000", seen["header"])

    async def test_wav_metadata(self):
        seen = {}

        def handler(request):
            seen["header"] = request.headers.get("x-speech-content")
            return httpx.Response(200, json={"result": "success", "text": "x"})

        ha = make_client(handler)
        await ha.speech_to_text(b"RIFF", "stt.w", audio_format="wav", codec="pcm")
        self.assertIn("format=wav", seen["header"])
        self.assertIn("codec=pcm", seen["header"])

    async def test_a_failed_result_is_an_stt_error(self):
        ha = make_client(json_handler({"result": "error", "text": ""}))
        with self.assertRaises(HomeAssistantError) as ctx:
            await ha.speech_to_text(b"x", "stt.w")
        self.assertEqual(ctx.exception.kind, "stt")

    async def test_an_unexpected_shape_is_an_stt_error(self):
        def handler(request):
            return httpx.Response(200, text="not json at all")

        ha = make_client(handler)
        with self.assertRaises(HomeAssistantError) as ctx:
            await ha.speech_to_text(b"x", "stt.w")
        self.assertEqual(ctx.exception.kind, "stt")

    async def test_empty_transcription_is_not_an_error(self):
        """Silence is a legitimate outcome; the caller decides what to say."""
        ha = make_client(json_handler({"result": "success", "text": "   "}))
        self.assertEqual(await ha.speech_to_text(b"x", "stt.w"), "")

    async def test_stt_options_is_a_plain_get(self):
        seen = {}

        def handler(request):
            seen["method"] = request.method
            seen["url"] = str(request.url)
            return httpx.Response(200, json={"formats": ["ogg"]})

        ha = make_client(handler)
        self.assertEqual(await ha.stt_options("stt.w"), {"formats": ["ogg"]})
        self.assertEqual(seen["method"], "GET")
        self.assertEqual(seen["url"], "http://ha.test:8123/api/stt/stt.w")


if __name__ == "__main__":
    unittest.main()
