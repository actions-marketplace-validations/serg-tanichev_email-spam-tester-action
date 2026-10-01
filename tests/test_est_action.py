"""End to end against a fake API and a fake SMTP relay, standard library only.

    python3 -m unittest discover -s tests
"""
import json
import os
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import unittest
from email import message_from_bytes
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "..", "src", "est_action.py")
sys.path.insert(0, os.path.join(HERE, "..", "src"))
import est_action  # noqa: E402


def check(cid, category, status, title="t", summary="s", weight=1):
    return {"id": cid, "category": category, "status": status, "title": title,
            "summary": summary, "weight_ours": weight}


GOOD = {"slug": "s" * 20, "score_ours": 92.5, "score_compat": 9.6, "complete": True,
        "report_url": "https://example.test/t/" + "s" * 20,
        "checks": [check("auth.spf", "auth", "pass"), check("auth.dkim", "auth", "pass"),
                   check("content.links", "content", "warn", "Links", "one link is http")]}


class FakeApi:
    """Answers like the real API; records what it was asked."""

    def __init__(self, rep, placement=None, inbox_status=200, placement_status=201):
        self.rep, self.placement, self.inbox_status = rep, placement, inbox_status
        self.placement_status = placement_status
        self.requests = []
        self.status_calls = 0
        api = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def answer(self, code, body):
                raw = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}") if length else None
                api.requests.append(("POST", self.path, body))
                if self.path.startswith("/api/v1/inbox"):
                    if api.inbox_status != 200:
                        return self.answer(api.inbox_status, {"detail": {"message": "limit"}})
                    return self.answer(200, {"slug": "s" * 20,
                                             "address": "test-%s@t.example.test" % ("s" * 20)})
                if self.path == "/api/v1/placement":
                    if api.placement_status != 201:
                        return self.answer(api.placement_status, {"detail": "no free inboxes"})
                    return self.answer(201, api.placement)
                self.answer(404, {"detail": "no"})

            def do_GET(self):
                api.requests.append(("GET", self.path, None))
                path = self.path.split("?", 1)[0]
                if path.endswith("/status"):
                    api.status_calls += 1
                    if api.status_calls == 1:
                        return self.answer(202, {"detail": "nothing yet"})
                    return self.answer(200, {"analysis_status": "checks_ready",
                                             "checks_done": 42, "checks_total": 42})
                if path.startswith("/api/v1/placement/by-score/"):
                    return self.answer(200, dict(api.placement, status="complete"))
                if path.startswith("/api/v1/tests/"):
                    return self.answer(200, api.rep)
                self.answer(404, {"detail": "no"})

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:%d" % self.server.server_port
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class FakeSmtp:
    """A relay without TLS that keeps what it was given."""

    def __init__(self):
        self.messages = []
        relay = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                w = lambda line: self.wfile.write((line + "\r\n").encode())
                w("220 fake")
                envelope = {"from": None, "to": [], "data": b""}
                while True:
                    line = self.rfile.readline()
                    if not line:
                        return
                    cmd = line.decode().strip()
                    upper = cmd.upper()
                    if upper.startswith(("EHLO", "HELO")):
                        w("250-fake")
                        w("250 AUTH PLAIN LOGIN")
                    elif upper.startswith("AUTH"):
                        w("235 ok")
                    elif upper.startswith("MAIL FROM:"):
                        envelope["from"] = cmd[10:].strip().strip("<>").split(">")[0]
                        w("250 ok")
                    elif upper.startswith("RCPT TO:"):
                        envelope["to"].append(cmd[8:].strip().strip("<>"))
                        w("250 ok")
                    elif upper == "DATA":
                        w("354 go")
                        data = b""
                        while True:
                            chunk = self.rfile.readline()
                            if chunk == b".\r\n":
                                break
                            data += chunk
                        envelope["data"] = data
                        relay.messages.append(dict(envelope))
                        w("250 queued")
                    elif upper == "QUIT":
                        w("221 bye")
                        return
                    else:
                        w("250 ok")

        self.server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def run(env, process_env=None):
    out = tempfile.NamedTemporaryFile(delete=False)
    summ = tempfile.NamedTemporaryFile(delete=False)
    out.close()
    summ.close()
    full = {k: v for k, v in os.environ.items() if not k.startswith("EST_")}
    full.update(GITHUB_OUTPUT=out.name, GITHUB_STEP_SUMMARY=summ.name)
    full.update({"EST_" + k: v for k, v in env.items()})
    full.update(process_env or {})
    proc = subprocess.run([sys.executable, SCRIPT], env=full, capture_output=True,
                          encoding="utf-8", timeout=120)
    with open(out.name, encoding="utf-8") as fh:
        outputs = dict(line.rstrip("\n").split("=", 1) for line in fh if "=" in line)
    with open(summ.name, encoding="utf-8") as fh:
        text = fh.read()
    os.unlink(out.name)
    os.unlink(summ.name)
    return proc, outputs, text


class EndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.html = os.path.join(self.tmp, "invoice.html")
        with open(self.html, "w") as fh:
            fh.write("<p>Your invoice</p>")
        self.smtp = FakeSmtp()

    def tearDown(self):
        self.smtp.close()

    def base(self, api, **extra):
        env = {"API_URL": api.url, "SMTP_HOST": "127.0.0.1", "SMTP_PORT": str(self.smtp.port),
               "SMTP_SECURITY": "none", "FROM": "Billing <billing@example.com>",
               "SUBJECT": "Your invoice", "HTML_FILE": self.html, "TIMEOUT_MINUTES": "1"}
        env.update(extra)
        return env

    def test_passes_and_sends_the_real_email(self):
        api = FakeApi(GOOD)
        try:
            proc, outputs, text = run(self.base(api))
        finally:
            api.close()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(outputs["passed"], "true")
        self.assertEqual(outputs["score"], "92.5")
        self.assertEqual(json.loads(outputs["failed-checks"]), [])
        self.assertIn("**passed**", text)
        self.assertIn("Links", text)
        [sent] = self.smtp.messages
        self.assertEqual(sent["from"], "billing@example.com")
        self.assertEqual(sent["to"], ["test-%s@t.example.test" % ("s" * 20)])
        msg = message_from_bytes(sent["data"])
        self.assertEqual(msg["Subject"], "Your invoice")
        self.assertTrue(msg["Message-ID"].endswith("@example.com>"))

    def test_fails_on_auth_even_with_a_good_score(self):
        rep = dict(GOOD, checks=GOOD["checks"] + [check("auth.dkim_alignment", "auth", "fail",
                                                        "DKIM alignment", "d= is the platform's")])
        api = FakeApi(rep)
        try:
            proc, outputs, text = run(self.base(api))
        finally:
            api.close()
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(outputs["passed"], "false")
        self.assertEqual(json.loads(outputs["failed-checks"]), ["auth.dkim_alignment"])
        self.assertIn("::error::DKIM alignment: d= is the platform's", proc.stdout)

    def test_auth_rule_can_be_switched_off(self):
        rep = dict(GOOD, checks=[check("auth.spf", "auth", "fail")])
        api = FakeApi(rep)
        try:
            proc, outputs, _ = run(self.base(api, FAIL_ON_AUTH="false"))
        finally:
            api.close()
        self.assertEqual(proc.returncode, 0, proc.stdout)

    def test_fails_under_the_threshold(self):
        api = FakeApi(dict(GOOD, score_ours=55.0))
        try:
            proc, outputs, _ = run(self.base(api, MIN_SCORE="70"))
        finally:
            api.close()
        self.assertEqual(proc.returncode, 1)
        self.assertIn("score 55.0 is below 70", proc.stdout)

    def test_placement_sends_to_every_mailbox_with_the_marker(self):
        placement = {"marker": "est-abc", "status": "waiting", "poll_after_seconds": 20,
                     "results": [{"address": "a@gmail.test", "placement": "inbox"}],
                     "fleet": {"results": [{"address": "b@gmx.test", "placement": "spam"},
                                           {"address": "c@x.test", "available": False}]}}
        api = FakeApi(GOOD, placement)
        try:
            proc, outputs, text = run(self.base(api, PLACEMENT="true", PLACEMENT_WAIT_MINUTES="0"))
        finally:
            api.close()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        [sent] = self.smtp.messages
        self.assertEqual(sent["to"][1:], ["a@gmail.test", "b@gmx.test"])
        msg = message_from_bytes(sent["data"])
        self.assertEqual(msg["Subject"], "Your invoice est-abc")
        self.assertEqual(msg["To"], sent["to"][0])
        self.assertIn("(final): inbox 1, spam 1", text)
        self.assertIn(("POST", "/api/v1/placement", {"public_results_accepted": True,
                                                     "score_slug": "s" * 20}), api.requests)

    def test_eml_is_sent_as_it_is_with_the_test_address(self):
        eml = os.path.join(self.tmp, "m.eml")
        with open(eml, "wb") as fh:
            fh.write(b"From: Shop <shop@example.org>\r\nTo: someone@example.net\r\n"
                     b"Subject: Order 42\r\nX-Template: order\r\n\r\nThanks for the order.\r\n")
        api = FakeApi(GOOD)
        try:
            proc, _, _ = run(self.base(api, EML_FILE=eml, HTML_FILE="", FROM=""))
        finally:
            api.close()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        msg = message_from_bytes(self.smtp.messages[0]["data"])
        self.assertEqual(msg["To"], "test-%s@t.example.test" % ("s" * 20))
        self.assertEqual(msg["X-Template"], "order")
        self.assertEqual(self.smtp.messages[0]["from"], "shop@example.org")

    def test_reserve_only_then_slug(self):
        api = FakeApi(GOOD)
        try:
            proc, outputs, _ = run({"API_URL": api.url, "RESERVE_ONLY": "true"})
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertEqual(outputs["address"], "test-%s@t.example.test" % ("s" * 20))
            proc, outputs, _ = run({"API_URL": api.url, "SLUG": outputs["slug"], "TIMEOUT_MINUTES": "1"})
        finally:
            api.close()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(outputs["passed"], "true")
        self.assertEqual(self.smtp.messages, [])

    def test_quota_answer_is_explained(self):
        api = FakeApi(GOOD, inbox_status=429)
        try:
            proc, _, _ = run(self.base(api))
        finally:
            api.close()
        self.assertEqual(proc.returncode, 1)
        self.assertIn("daily limit of free tests", proc.stdout)

    def test_input_mistakes_do_not_cost_a_test(self):
        for extra, message in (({"HTML_FILE": ""}, "give html-file, text-file or eml-file"),
                               ({"HTML_FILE": "missing.html"}, "cannot read html-file"),
                               ({"FROM": "nobody"}, "cannot read an address in From"),
                               ({"SMTP_USERNAME": "u", "SMTP_PASSWORD": "p"}, "clear text")):
            api = FakeApi(GOOD)
            try:
                proc, _, _ = run(self.base(api, **extra))
            finally:
                api.close()
            self.assertEqual(proc.returncode, 1, extra)
            self.assertIn(message, proc.stdout)
            self.assertEqual(api.requests, [], extra)
        self.assertEqual(self.smtp.messages, [])

    def test_report_text_cannot_run_workflow_commands(self):
        evil = "fine\n::add-mask::secret\n::error::fake | <img src=x> # Heading"
        rep = dict(GOOD, checks=[check("content.x", "content", "warn", "T|itle", evil)])
        api = FakeApi(rep)
        try:
            proc, _, text = run(self.base(api))
        finally:
            api.close()
        self.assertEqual(proc.returncode, 0, proc.stdout)
        lines = proc.stdout.splitlines()
        token = lines[0].split("::stop-commands::", 1)[1]
        self.assertTrue(token)
        self.assertEqual(lines[-1], "::%s::" % token)
        self.assertFalse([l for l in lines if l.lstrip().startswith("::add-mask::secret")])
        self.assertFalse([l for l in lines if l.lstrip().startswith("::error::fake")])
        self.assertNotIn("<img", text)
        self.assertIn("T\\|itle", text)
        self.assertIn("&lt;img src=x&gt;", text)

    def test_eml_loses_the_traces_of_its_old_delivery(self):
        eml = os.path.join(self.tmp, "old.eml")
        with open(eml, "wb") as fh:
            fh.write(b"Received: from old by mx\r\nDKIM-Signature: v=1; d=old.test; b=xx\r\n"
                     b"Authentication-Results: mx; dkim=pass\r\nDate: Mon, 1 Jan 2024 00:00:00 +0000\r\n"
                     b"Message-ID: <old@old.test>\r\nFrom: Shop <shop@example.org>\r\n"
                     b"To: a@b.test\r\nSubject: Order\r\n\r\nbody\r\n")
        api = FakeApi(GOOD)
        try:
            proc, _, _ = run(self.base(api, EML_FILE=eml, HTML_FILE=""))
        finally:
            api.close()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        msg = message_from_bytes(self.smtp.messages[0]["data"])
        for header in ("Received", "DKIM-Signature", "Authentication-Results"):
            self.assertIsNone(msg[header], header)
        self.assertNotEqual(msg["Message-ID"], "<old@old.test>")
        self.assertNotIn("2024", msg["Date"])

    def test_folder_check_trouble_is_only_a_warning(self):
        api = FakeApi(GOOD, placement_status=503)
        try:
            proc, outputs, _ = run(self.base(api, PLACEMENT="true"))
        finally:
            api.close()
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertIn("::warning::folder check not started (503)", proc.stdout)
        self.assertEqual(len(self.smtp.messages[0]["to"]), 1)

    def test_reserve_only_does_not_take_placement(self):
        api = FakeApi(GOOD)
        try:
            proc, _, _ = run({"API_URL": api.url, "RESERVE_ONLY": "true", "PLACEMENT": "true"})
        finally:
            api.close()
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(api.requests, [])

    def test_unicode_report_on_a_cp1252_console(self):
        rep = dict(GOOD, checks=[check("content.x", "content", "warn", "Заголовок", "Текст ⚠️")])
        api = FakeApi(rep)
        try:
            proc, _, text = run(self.base(api, LANG="ru"), {"PYTHONIOENCODING": "cp1252",
                                                            "PYTHONUTF8": "0"})
        finally:
            api.close()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("Заголовок", text)


class Pieces(unittest.TestCase):
    def test_api_url_must_be_https(self):
        with self.assertRaises(est_action.Failure):
            est_action.Api("http://example.com")

    def test_problems_put_failures_first_and_skip_engines_and_advice(self):
        rep = {"checks": [check("content.a", "content", "warn", weight=5),
                          check("auth.b", "auth", "fail", weight=1),
                          check("spam.rspamd", "spam", "fail"),
                          check("advisory.x", "advisory", "warn")]}
        self.assertEqual([c["id"] for c in est_action.problems(rep)], ["auth.b", "content.a"])


if __name__ == "__main__":
    unittest.main()
