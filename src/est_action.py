#!/usr/bin/env python3
"""Email Spam Tester as a GitHub Action step.

Reserve a one-hour test address, send the real email to it through the
caller's SMTP relay, wait for the checks, then pass or fail the step:
fail when an authentication check fails or the score is under a threshold.
Standard library only, so it runs on any runner that has Python 3.9+.

Settings come from EST_* environment variables (see action.yml); outputs go
to $GITHUB_OUTPUT and a summary to $GITHUB_STEP_SUMMARY.
"""
import html
import json
import os
import secrets
import smtplib
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import format_datetime, make_msgid, parseaddr

VERSION = "1.0.0"
USER_AGENT = "email-spam-tester-action/" + VERSION
#: Headers a message picks up on the way to a mailbox. A message exported from
#: one carries them, and sent again they would describe an old delivery: a
#: DKIM signature over the old To fails, a Received chain from last month
#: confuses the infrastructure checks.
TRACE_HEADERS = ("DKIM-Signature", "ARC-Seal", "ARC-Message-Signature",
                 "ARC-Authentication-Results", "Authentication-Results", "Received",
                 "Received-SPF", "Return-Path", "Delivered-To", "X-Original-To",
                 "Resent-Date", "Resent-From", "Resent-Sender", "Resent-To",
                 "Resent-Cc", "Resent-Bcc", "Resent-Message-ID", "Date", "Message-ID")
#: The address lives an hour; waiting longer can only end in "expired".
MAX_WAIT_MINUTES = 60
#: How long the status endpoint is asked to hold a request (it allows 50 s).
LONG_POLL = 25


class Failure(Exception):
    """Something that ends the step with an error message and no report."""


# --- settings --------------------------------------------------------------

def env(name, default="", strip=True):
    value = os.environ.get("EST_" + name) or default
    return value.strip() if strip else value


def flag(name, default=False):
    value = env(name, "true" if default else "false").lower()
    if value in ("true", "1", "yes", "on"):
        return True
    if value in ("false", "0", "no", "off", ""):
        return False
    raise Failure("%s must be true or false, got %r" % (name.lower().replace("_", "-"), value))


def number(name, default, cast=int):
    raw = env(name, str(default))
    try:
        return cast(raw)
    except ValueError:
        raise Failure("%s must be a number, got %r" % (name.lower().replace("_", "-"), raw))


# --- GitHub plumbing -------------------------------------------------------

#: Text from the report (titles, summaries, the email's own subject quoted in
#: them) goes to the log. The runner would read a line starting with "::" as a
#: command, so commands stay off while that text is printed and only our own
#: annotations switch them back on for a moment.
COMMANDS_OFF = secrets.token_hex(16)


def output(name, value):
    path = os.environ.get("GITHUB_OUTPUT")
    value = "" if value is None else str(value)
    if "\n" in value or "\r" in value:
        raise Failure("unexpected line break in output %s" % name)
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("%s=%s\n" % (name, value))


def summary(markdown):
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(markdown + "\n")


def log(text):
    print(" ".join(str(text).splitlines()), flush=True)


def annotate(kind, text):
    text = str(text).replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    print("::%s::" % COMMANDS_OFF, flush=True)
    print("::%s::%s" % (kind, text), flush=True)
    print("::stop-commands::%s" % COMMANDS_OFF, flush=True)


def mask(secret):
    if secret:
        print("::%s::" % COMMANDS_OFF, flush=True)
        print("::add-mask::%s" % secret, flush=True)
        print("::stop-commands::%s" % COMMANDS_OFF, flush=True)


def cell(text, limit=300):
    """Server text in the Markdown summary: no markup, no breaks, no pipes."""
    text = " ".join(str(text if text is not None else "").split())[:limit]
    return html.escape(text).replace("|", "\\|")


# --- API -------------------------------------------------------------------

class Api:
    def __init__(self, base):
        local = base.startswith(("http://localhost", "http://127.0.0.1"))
        if not base.startswith("https://") and not local:
            raise Failure("api-url must be https")
        self.base = base.rstrip("/")

    def call(self, method, path, body=None, timeout=60):
        """GETs are retried on 5xx and network errors; POSTs are not, since a
        reservation that half-happened must not be made twice."""
        data = None
        headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        elif method == "POST":
            data = b""
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        attempts = 4 if method == "GET" else 1
        for attempt in range(attempts):
            last = attempt == attempts - 1
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    return resp.status, parse(resp.read())
            except urllib.error.HTTPError as err:
                if err.code >= 500 and not last:
                    time.sleep(5 * (attempt + 1))
                    continue
                return err.code, parse(err.read())
            except (urllib.error.URLError, OSError) as err:
                if not last:
                    time.sleep(5 * (attempt + 1))
                    continue
                raise Failure("cannot reach %s: %s" % (self.base, getattr(err, "reason", err)))
        raise Failure("no answer from %s" % self.base)


def parse(raw):
    try:
        return json.loads(raw or b"{}")
    except ValueError:
        return {"detail": raw[:200].decode("utf-8", "replace")}


def detail(body):
    value = body.get("detail") if isinstance(body, dict) else body
    if isinstance(value, dict):
        return value.get("message") or value.get("error") or json.dumps(value)
    return str(value)


def reserve(api, lang):
    status, body = api.call("POST", "/api/v1/inbox?lang=" + urllib.parse.quote(lang))
    if status == 429:
        raise Failure("the daily limit of free tests is used up for this runner's IP: " + detail(body))
    if status != 200 or not isinstance(body, dict) or "slug" not in body:
        raise Failure("could not reserve a test address (%s): %s" % (status, detail(body)))
    return body


def start_placement(api, slug):
    """The folder check never fails the step: any trouble is a warning."""
    try:
        status, body = api.call("POST", "/api/v1/placement",
                                {"public_results_accepted": True, "score_slug": slug})
    except Failure as err:
        status, body = 0, {"detail": str(err)}
    if status not in (200, 201) or not isinstance(body, dict):
        annotate("warning", "folder check not started (%s): %s; the email is still checked"
                 % (status, detail(body)))
        return None
    return body


def placement_recipients(placement):
    out = [row["address"] for row in placement.get("results") or [] if row.get("address")]
    fleet = placement.get("fleet") or {}
    out += [row["address"] for row in fleet.get("results") or []
            if row.get("address") and row.get("available", True)]
    seen = set()
    return [a for a in out if not (a.lower() in seen or seen.add(a.lower()))]


def wait_for_checks(api, slug, timeout_s):
    deadline = time.monotonic() + timeout_s
    last = None
    while time.monotonic() < deadline:
        wait = max(0, min(LONG_POLL, int(deadline - time.monotonic())))
        status, body = api.call("GET", "/api/v1/tests/%s/status?wait=%d" % (slug, wait),
                                timeout=wait + 30)
        if status == 202:
            if last != "waiting":
                log("waiting for the email to arrive...")
                last = "waiting"
            if not wait:
                time.sleep(5)
            continue
        if status == 404:
            raise Failure("unknown test %s" % slug)
        if status == 410:
            raise Failure("the test address expired before an email arrived")
        if status == 429:
            time.sleep(10)
            continue
        if status != 200:
            raise Failure("unexpected answer from the status endpoint (%s): %s" % (status, detail(body)))
        state = "%s %s/%s" % (body.get("analysis_status"), body.get("checks_done"), body.get("checks_total"))
        if state != last:
            log("checks: " + state)
            last = state
        if body.get("analysis_status") == "failed":
            raise Failure("the analysis failed on the server; see %s/t/%s" % (api.base, slug))
        if body.get("analysis_status") == "checks_ready":
            return
        if not wait:
            time.sleep(3)
    raise Failure("no report within %d minutes; the email may still be queued at your relay"
                  % (timeout_s // 60))


def report(api, slug, lang):
    path = "/api/v1/tests/%s?lang=%s" % (slug, urllib.parse.quote(lang))
    status, body = api.call("GET", path)
    if status != 200 or not isinstance(body, dict):
        raise Failure("could not read the report (%s): %s" % (status, detail(body)))
    # Findings in other languages follow the checks by a few seconds.
    deadline = time.monotonic() + 60
    while lang != "en" and body.get("translating") and time.monotonic() < deadline:
        time.sleep(5)
        status, again = api.call("GET", path)
        if status == 200 and isinstance(again, dict):
            body = again
    return body


def wait_for_placement(api, slug, timeout_s):
    deadline = time.monotonic() + timeout_s
    body = None
    try:
        while True:
            status, latest = api.call("GET", "/api/v1/placement/by-score/%s" % slug)
            if status != 200 or not isinstance(latest, dict):
                return body
            body = latest
            if placement_done(body) or time.monotonic() >= deadline:
                return body
            time.sleep(max(20, min(int(body.get("poll_after_seconds") or 30), 60)))
    except Failure as err:
        annotate("warning", "folder results not read: %s" % err)
        return body


# --- the email -------------------------------------------------------------

def read_file(path, what):
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except OSError as err:
        raise Failure("cannot read %s %s: %s" % (what, path, err.strerror))


def read_text(path, what):
    try:
        return read_file(path, what).decode("utf-8-sig")
    except UnicodeDecodeError:
        raise Failure("%s %s is not UTF-8" % (what, path))


def compose():
    """The message to send, before it has a recipient: a ready .eml, or
    From + Subject + bodies. Built before an address is reserved, so that a
    wrong path does not cost a test."""
    eml = env("EML_FILE")
    if eml:
        msg = BytesParser(policy=policy.SMTP).parsebytes(read_file(eml, "eml-file"))
        for header in ("To", "Cc", "Bcc") + TRACE_HEADERS:
            del msg[header]
        if not msg["From"]:
            if not env("FROM"):
                raise Failure("the .eml has no From header and the from input is empty")
            msg["From"] = env("FROM")
    else:
        if not env("FROM"):
            raise Failure("from is required to send the test email")
        html_path, text_path = env("HTML_FILE"), env("TEXT_FILE")
        if not html_path and not text_path:
            raise Failure("give html-file, text-file or eml-file: the email to test")
        msg = EmailMessage(policy=policy.SMTP)
        msg["From"] = env("FROM")
        msg["Subject"] = env("SUBJECT", "Deliverability check")
        text = read_text(text_path, "text-file") if text_path else None
        body = read_text(html_path, "html-file") if html_path else None
        if text is not None:
            msg.set_content(text)
            if body is not None:
                msg.add_alternative(body, subtype="html")
        else:
            msg.set_content(body, subtype="html")
    sender = parseaddr(str(msg["From"]))[1]
    if "@" not in sender:
        raise Failure("cannot read an address in From: %r" % str(msg["From"]))
    msg["Date"] = format_datetime(datetime.now(timezone.utc))
    msg["Message-ID"] = make_msgid(domain=sender.rpartition("@")[2])
    return msg


def address(msg, to, marker):
    msg["To"] = to
    if marker:
        # The folder check finds the copies at the providers by this marker.
        subject = str(msg["Subject"] or "")
        del msg["Subject"]
        msg["Subject"] = (subject + " " + marker).strip()


def smtp_settings():
    host = env("SMTP_HOST")
    port = number("SMTP_PORT", 587)
    security = env("SMTP_SECURITY").lower() or ("ssl" if port == 465 else "starttls")
    if security not in ("starttls", "ssl", "none"):
        raise Failure("smtp-security must be starttls, ssl or none")
    user, password = env("SMTP_USERNAME"), env("SMTP_PASSWORD", strip=False)
    mask(password)
    if security == "none" and user:
        raise Failure("smtp-security none would send the password in clear text; use starttls or ssl")
    return host, port, security, user, password


def send(msg, recipients, settings):
    host, port, security, user, password = settings
    sender = parseaddr(str(msg["From"]))[1]
    context = ssl.create_default_context()
    try:
        if security == "ssl":
            smtp = smtplib.SMTP_SSL(host, port, timeout=60, context=context)
        else:
            smtp = smtplib.SMTP(host, port, timeout=60)
        with smtp:
            smtp.ehlo()
            if security == "starttls":
                smtp.starttls(context=context)
                smtp.ehlo()
            if user:
                smtp.login(user, password)
            refused = smtp.send_message(msg, from_addr=sender, to_addrs=recipients)
    except smtplib.SMTPAuthenticationError as err:
        raise Failure("the relay refused the login: %s %s"
                      % (err.smtp_code, err.smtp_error.decode("utf-8", "replace")))
    except smtplib.SMTPRecipientsRefused as err:
        code, reply = next(iter(err.recipients.values()), (0, b""))
        raise Failure("the relay refused the recipients: %s %s%s" % (
            code, reply.decode("utf-8", "replace"),
            "" if user else " (no smtp-username given; most relays need one)"))
    except (smtplib.SMTPException, OSError, ValueError) as err:
        raise Failure("sending through %s:%s failed: %s" % (host, port, err))
    if recipients[0] in refused:
        raise Failure("the relay refused the test address: %s" % (refused[recipients[0]],))
    if refused:
        annotate("warning", "the relay refused %d of the provider mailboxes" % len(refused))


# --- judging ---------------------------------------------------------------

def judge(rep, min_score, fail_on_auth):
    """Reasons to fail, empty when the email passes."""
    reasons = []
    if fail_on_auth:
        for check in rep.get("checks") or []:
            if check.get("category") == "auth" and check.get("status") == "fail":
                reasons.append("%s: %s" % (check.get("title"), check.get("summary_localised")
                                           or check.get("summary")))
    score = rep.get("score_ours")
    if min_score > 0:
        if score is None:
            reasons.append("the report has no score")
        elif score < min_score:
            reasons.append("score %s is below %s" % (score, "%g" % min_score))
    return reasons


def problems(rep):
    rows = [c for c in rep.get("checks") or [] if c.get("status") in ("fail", "warn")
            and c.get("category") != "advisory" and not str(c.get("id", "")).startswith("spam.")]
    rows.sort(key=lambda c: (c.get("status") != "fail", -(c.get("weight_ours") or 0)))
    return rows


FOLDERS = ("inbox", "promotions", "updates", "social", "forums", "spam", "multiple",
           "unknown", "rejected", "not_observed", "pending")


def placement_done(placement):
    """Done when no mailbox is still waiting for its copy."""
    return placement.get("status") != "waiting" and not folder_counts(placement).get("pending")


def folder_counts(placement):
    counts = {}
    rows = list(placement.get("results") or [])
    rows += [r for r in (placement.get("fleet") or {}).get("results") or [] if r.get("available", True)]
    for row in rows:
        where = row.get("placement") or "pending"
        counts[where] = counts.get(where, 0) + 1
    return counts


def render_summary(rep, reasons, placement, min_score):
    lines = ["## Email Spam Tester", ""]
    verdict = "passed" if not reasons else "failed"
    lines.append("**%s** · score **%s**/100 · classic %s/10 · [full report](%s)"
                 % (verdict, cell(rep.get("score_ours")), cell(rep.get("score_compat")),
                    cell(rep.get("report_url"))))
    lines.append("")
    if reasons:
        lines += ["Why it failed:", ""] + ["- " + cell(r, 500) for r in reasons] + [""]
    if not rep.get("complete"):
        errored = ", ".join(str(c) for c in rep.get("errored_checks") or []) or "some"
        lines += ["Checks that could not run (%s), so the score may be optimistic." % cell(errored), ""]
    rows = problems(rep)
    if rows:
        lines += ["| | Check | What was found |", "|---|---|---|"]
        for c in rows[:25]:
            lines.append("| %s | %s | %s |" % ("❌" if c.get("status") == "fail" else "⚠️",
                                             cell(c.get("title"), 120),
                                             cell(c.get("summary_localised") or c.get("summary"))))
        lines.append("")
    if placement:
        counts = folder_counts(placement)
        known = [k for k in FOLDERS if counts.get(k)] + sorted(k for k in counts if k not in FOLDERS)
        parts = ["%s %d" % (cell(k.replace("_", " ")), counts[k]) for k in known]
        state = "final" if placement_done(placement) else "so far; the report keeps filling in"
        lines += ["Folder at email providers (%s): %s" % (state, ", ".join(parts) or "no results yet"), ""]
    if min_score > 0:
        lines.append("Threshold: %s/100." % ("%g" % min_score))
    return "\n".join(lines)


# --- main ------------------------------------------------------------------

def main():
    api = Api(env("API_URL", "https://email-spam-tester.com"))
    lang = env("LANG", "en")
    timeout_s = min(max(1, number("TIMEOUT_MINUTES", 10)), MAX_WAIT_MINUTES) * 60
    min_score = number("MIN_SCORE", 70, float)
    fail_on_auth = flag("FAIL_ON_AUTH", True)
    want_placement = flag("PLACEMENT")
    placement_wait = max(0, number("PLACEMENT_WAIT_MINUTES", 10))
    slug = env("SLUG")
    placement = None

    if flag("RESERVE_ONLY"):
        if slug:
            raise Failure("reserve-only and slug do not go together")
        if want_placement:
            raise Failure("placement needs the action to send the email; it does not go with reserve-only")
        res = reserve(api, lang)
        output("slug", res["slug"])
        output("address", res["address"])
        output("report-url", "%s/t/%s" % (api.base, res["slug"]))
        log("send one email to %s, then check it with slug: %s" % (res["address"], res["slug"]))
        return 0

    if not slug:
        if not env("SMTP_HOST"):
            raise Failure("set smtp-host to send the test email, or use reserve-only and slug")
        settings = smtp_settings()
        msg = compose()
        res = reserve(api, lang)
        slug = res["slug"]
        output("slug", slug)
        output("address", res["address"])
        recipients = [res["address"]]
        marker = None
        if want_placement:
            placement = start_placement(api, slug)
            extra = placement_recipients(placement) if placement else []
            if extra:
                marker = placement.get("marker")
                recipients += extra
        address(msg, res["address"], marker)
        log("sending to the test address%s through %s"
            % (" and %d provider mailboxes" % (len(recipients) - 1) if len(recipients) > 1 else "",
               settings[0]))
        send(msg, recipients, settings)
    else:
        output("slug", slug)

    output("report-url", "%s/t/%s" % (api.base, slug))
    wait_for_checks(api, slug, timeout_s)
    rep = report(api, slug, lang)

    if placement is not None:
        log("waiting up to %d minutes for the folder at email providers..." % placement_wait)
        placement = wait_for_placement(api, slug, placement_wait * 60) or placement

    reasons = judge(rep, min_score, fail_on_auth)
    failed = [c.get("id") for c in rep.get("checks") or [] if c.get("status") == "fail"]
    output("score", rep.get("score_ours"))
    output("score-classic", rep.get("score_compat"))
    output("failed-checks", json.dumps(failed))
    output("passed", "false" if reasons else "true")
    summary(render_summary(rep, reasons, placement, min_score))

    log("score %s/100, classic %s/10" % (rep.get("score_ours"), rep.get("score_compat")))
    for c in problems(rep):
        log("[%s] %s: %s" % (c.get("status"), c.get("title"), c.get("summary_localised") or c.get("summary")))
    log("report: %s" % rep.get("report_url"))
    if reasons:
        for r in reasons:
            annotate("error", r)
        return 1
    return 0


def run():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    print("::stop-commands::%s" % COMMANDS_OFF, flush=True)
    try:
        code = main()
    except Failure as err:
        annotate("error", str(err))
        code = 1
    print("::%s::" % COMMANDS_OFF, flush=True)
    return code


if __name__ == "__main__":
    sys.exit(run())
