"""Check a running test site (CHAT_AUTH_PROVIDER=invite, CHAT_WEB_DIR set) end to end, as staff and as a tester.
Standard library only. Run it after deploying, with a staff link from `python -m chat_service.invites create`:

  python3 ops/check_test_site.py --link "https://your-site.onrender.com/?invite=aos1...."

Checks:
1. The site's page loads, with its own security policy; the API refuses requests without a link.
2. The staff link signs in as staff, and creates a tester link (which switches the assistant on for that org).
3. The tester signs in, and a chat message streams an answer from the model, with tokens counted.
Creates one test tester ("Site check") in the organisation "Site check". Prints one line per check; exits 1 on failure.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


def request(method: str, url: str, headers: dict | None = None, body: dict | None = None, timeout: float = 120):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=dict(headers or {}))
    if data is not None:
        req.add_header("content-type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


def ok(msg: str) -> None:
    print(f"ok    {msg}", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--link", required=True, help="a staff invite link")
    ap.add_argument("--timeout", type=float, default=180, help="seconds to wait for the site to come up")
    args = ap.parse_args()
    parsed = urllib.parse.urlparse(args.link)
    site = f"{parsed.scheme}://{parsed.netloc}"
    staff = {"Authorization": "Bearer " + urllib.parse.parse_qs(parsed.query)["invite"][0]}
    try:
        deadline = time.monotonic() + args.timeout
        while True:
            try:
                status, headers, body = request("GET", f"{site}/", timeout=10)
                if status == 200:
                    break
            except OSError:
                pass
            if time.monotonic() > deadline:
                raise AssertionError(f"{site}/ didn't answer within {args.timeout:.0f}s")
            time.sleep(3)
        assert "text/html" in headers.get("Content-Type", headers.get("content-type", "")), "page isn't HTML"
        csp = {k.lower(): v for k, v in headers.items()}.get("content-security-policy", "")
        assert "script-src 'self'" in csp, f"page security policy: {csp!r}"
        status, _, _ = request("GET", f"{site}/chat/me")
        assert status == 401, f"/chat/me without a link should be 401, got {status}"
        ok("site page loads with its security policy; API refuses requests without a link")

        status, _, body = request("GET", f"{site}/chat/me", staff)
        assert status == 200, f"staff link: {status} {body[:200]!r}"
        assert "agencyos_staff" in json.loads(body)["roles"], "the link isn't a staff link"
        status, _, body = request(
            "POST", f"{site}/chat/admin/invites", staff, {"name": "Site check", "org_name": "Site check", "days": 1}
        )
        assert status == 201, f"create tester link: {status} {body[:200]!r}"
        tester = {"Authorization": "Bearer " + json.loads(body)["path"].split("=", 1)[1]}
        ok("staff link signs in and creates a tester link")

        status, _, body = request("GET", f"{site}/chat/me", tester)
        assert status == 200 and json.loads(body)["display_name"] == "Site check", f"tester link: {status}"
        status, _, body = request("POST", f"{site}/chat/conversations", tester, {})
        assert status == 201, f"create conversation: {status} {body[:200]!r}"
        conv = json.loads(body)["id"]
        status, headers, body = request(
            "POST", f"{site}/chat/conversations/{conv}/messages", tester, {"text": "What can you help me with?"}
        )
        assert status == 200, f"message: {status} {body[:300]!r}"
        events, event = [], None
        for line in body.decode().splitlines():
            if line.startswith("event: "):
                event = line[7:]
            elif line.startswith("data: ") and event:
                events.append((event, json.loads(line[6:])))
        names = [e for e, _ in events]
        if "error" in names:
            raise AssertionError(f"the assistant returned an error: {dict(events).get('error')}")
        assert names and names[0] == "message.start" and names[-1] == "message.end", f"events: {names}"
        text = "".join(d.get("text", "") for e, d in events if e == "token")
        end = events[-1][1]
        assert text.strip() and end["billable_output_tokens"] > 0, f"empty answer: {end}"
        ok(f"tester's message answered ({end['billable_output_tokens']} tokens): {text.strip()[:100]!r}")
    except AssertionError as exc:
        print(f"FAIL  {exc}", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
