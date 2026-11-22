"""On a console, a site that failed one request is used again by itself (#499).

Under the supervisor the health check runs in the background worker, with its own registry. In the web process nothing ever tried a site
that was down, so one failed request hid the site's live video and playback from everyone until an administrator pressed "check"."""
from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

import httpx

from app import console


class Site:
    """A site's answers: ``mode`` is "ok", "dead" (no connection), "slow" (no answer in time) or "busy" (HTTP 503)."""

    def __init__(self) -> None:
        self.mode = "ok"
        self.requests = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests += 1
        if self.mode == "dead":
            raise httpx.ConnectError("refused")
        if self.mode == "slow":
            raise httpx.ReadTimeout("no answer in time")
        if self.mode == "busy":
            return httpx.Response(503)
        return httpx.Response(200, json={"cameras": []})


def link_to(site: Site) -> console.SiteLink:
    client = httpx.AsyncClient(base_url="https://site.example.test", transport=httpx.MockTransport(site.handler))
    return console.SiteLink("site-1", "Site", "https://site.example.test", "token", True, ("https://site.example.test", b"x", True), client)


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        for name, value in (("RECHECK_SECONDS", 0.05), ("check_url", lambda url, allow_private: url)):
            patcher = patch.object(console, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.site = Site()

    def run_scenario(self, scenario):
        async def run():
            link = link_to(self.site)
            try:
                return await scenario(link)
            finally:
                await asyncio.gather(*console._probes, return_exceptions=True)
                await link.client.aclose()

        return asyncio.run(run())

    async def ask(self, link: console.SiteLink) -> str:
        try:
            await console.call(link, "GET", "/api/cameras")
            return "answered"
        except console.SiteDown as error:
            return str(error)

    def test_a_site_that_failed_once_is_used_again_once_it_answers_with_nobody_pressing_check(self):
        async def scenario(link):
            self.site.mode = "dead"
            first = await self.ask(link)
            refused_at_once = self.site.requests
            again = await self.ask(link)  # Refused without trying: a dead site costs a request nothing
            self.assertEqual(self.site.requests, refused_at_once)
            self.site.mode = "ok"  # The site is back
            await asyncio.sleep(console.RECHECK_SECONDS * 2)
            waiting = await self.ask(link)  # Still refused at once, but it sets the site being asked again in the background
            await asyncio.sleep(0.05)
            return first, again, waiting, await self.ask(link), link.state

        first, again, waiting, after, state = self.run_scenario(scenario)
        self.assertEqual((first, again, waiting), ("ConnectError",) * 3)
        self.assertEqual((after, state), ("answered", "ok"))

    def test_a_site_that_is_still_dead_is_asked_once_per_wait_not_once_per_request(self):
        async def scenario(link):
            self.site.mode = "dead"
            await self.ask(link)
            await asyncio.sleep(console.RECHECK_SECONDS * 2)
            for _ in range(20):
                await self.ask(link)
            await asyncio.sleep(0.02)
            return self.site.requests, link.state

        requests, state = self.run_scenario(scenario)
        self.assertEqual(requests, 2, "the request that found it down, and one background try")
        self.assertEqual(state, "unreachable")

    def test_one_slow_answer_fails_that_request_only(self):
        async def scenario(link):
            self.site.mode = "slow"
            slow = await self.ask(link)
            self.site.mode = "ok"
            return slow, link.available, await self.ask(link), link.slow

        slow, available, after, count = self.run_scenario(scenario)
        self.assertEqual(slow, "The site took too long to answer")
        self.assertTrue(available, "the site is still there for everyone else")
        self.assertEqual((after, count), ("answered", 0))

    def test_a_site_that_never_answers_is_taken_as_down_after_a_few_tries(self):
        async def scenario(link):
            self.site.mode = "slow"
            results = [await self.ask(link) for _ in range(console.SLOW_IN_A_ROW + 2)]
            return results, self.site.requests, link.state

        results, requests, state = self.run_scenario(scenario)
        self.assertEqual(requests, console.SLOW_IN_A_ROW, "after that, requests are refused without waiting")
        self.assertEqual(state, "unreachable")
        self.assertEqual(results[-1], "ReadTimeout")

    def test_a_busy_site_is_still_taken_as_down_at_once(self):
        async def scenario(link):
            self.site.mode = "busy"
            return await self.ask(link), link.state

        self.assertEqual(self.run_scenario(scenario), ("HTTP 503", "unreachable"))


class TwoProcessTests(unittest.TestCase):
    """The web process's registry and the background worker's, which share only the database rows."""

    def test_what_the_health_check_wrote_clears_a_site_marked_down_in_the_web_process(self):
        site = Site()
        row = {"id": "site-1", "name": "Site", "base_url": "https://site.example.test", "token_ciphertext": b"x", "enabled": 1, "allow_private": 0,
               "last_contact_at": None, "last_checked_at": None, "last_error": None, "camera_count": None, "open_alarms": None}

        async def scenario():
            web = console.Registry()
            link = link_to(site)
            web.links = {"site-1": link}
            with patch.object(console, "_read", lambda: (True, [dict(row)])):
                await web.load(max_age=0)
                self.assertIs(web.links["site-1"], link)
                link.failed("ReadTimeout")
                await web.load(max_age=0)
                still_down = link.state
                # The background worker's check reaches the site and writes its row.
                row.update(last_contact_at=console._now_iso(), last_checked_at=console._now_iso(), last_error=None)
                await asyncio.sleep(0.002)
                await web.load(max_age=0)
            await link.client.aclose()
            return still_down, link.state, link.available

        self.assertEqual(asyncio.run(scenario()), ("unreachable", "ok", True))

    def test_a_row_that_still_says_unreachable_clears_nothing(self):
        site = Site()
        row = {"id": "site-1", "name": "Site", "base_url": "https://site.example.test", "token_ciphertext": b"x", "enabled": 1, "allow_private": 0,
               "last_contact_at": "2026-11-01T00:00:00.000Z", "last_checked_at": console._now_iso(), "last_error": "ConnectError", "camera_count": None, "open_alarms": None}

        async def scenario():
            web = console.Registry()
            link = link_to(site)
            web.links = {"site-1": link}
            link.failed("ConnectError")
            with patch.object(console, "_read", lambda: (True, [dict(row)])):
                await web.load(max_age=0)
            await link.client.aclose()
            return link.state

        self.assertEqual(asyncio.run(scenario()), "unreachable")


if __name__ == "__main__":
    unittest.main()
