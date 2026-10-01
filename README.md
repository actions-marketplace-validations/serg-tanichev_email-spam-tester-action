# Email Spam Tester GitHub Action

Sends your real email template through the SMTP relay production uses, waits for [Email Spam Tester](https://email-spam-tester.com/) to check it, and fails the job when SPF, DKIM or DMARC fails or the score drops under your threshold. Run it nightly and it catches DNS drift the morning after it happens. Typical cases are a new `include:` that pushes SPF past ten lookups, or a relay that starts signing DKIM with its own domain after a provider switch, so DMARC alignment quietly stops holding.

The service is free to use right now and needs no key. Each run checks one email against 42 checks: authentication and alignment, reverse DNS, TLS, blocklists, links, HTML, the Gmail and Yahoo bulk-sender rules, SpamAssassin and Rspamd.

## Usage

```yaml
name: email-deliverability

on:
  schedule:
    - cron: "17 6 * * *"
  push:
    paths: ["templates/**"]
  workflow_dispatch:

jobs:
  spam-test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: serg-tanichev/email-spam-tester-action@v1
        with:
          smtp-host: ${{ secrets.SMTP_HOST }}
          smtp-port: 587
          smtp-username: ${{ secrets.SMTP_USERNAME }}
          smtp-password: ${{ secrets.SMTP_PASSWORD }}
          from: "Acme Billing <billing@example.com>"
          subject: Your invoice for September
          html-file: templates/invoice.html
          text-file: templates/invoice.txt
          min-score: 80
```

The job log lists the failed and warned checks, and the run's summary page gets a table of them with a link to the full report. The report quotes the standard behind each finding and ends with a fix plan.

Outgoing port 25 is usually closed on cloud runners, so use your relay's submission port, 587 with STARTTLS or 465 with TLS. Point it at the production relay with a sender account that can only send: a staging relay usually signs with a different key and sends from a different IP, and the checks would be about the wrong setup.

### Sending with your own code

When the email comes out of your application rather than a template file, reserve the address first, let your code send to it, then check it:

```yaml
      - id: address
        uses: serg-tanichev/email-spam-tester-action@v1
        with:
          reserve-only: true

      - run: ./bin/send-welcome-email --to "${{ steps.address.outputs.address }}"

      - uses: serg-tanichev/email-spam-tester-action@v1
        with:
          slug: ${{ steps.address.outputs.slug }}
          min-score: 80
```

A complete message exported from your mail tooling can go through the relay as it is with `eml-file`. Its own To and Cc are dropped and the test address goes in their place. Anything left over from an earlier delivery (Received lines, DKIM and ARC signatures, Authentication-Results, the old Date and Message-ID) is removed too, because the relay has to sign and stamp it afresh for the checks to mean anything.

### Folder at email providers

With `placement: true` the same email also goes to about 30 test mailboxes at 14 providers, Gmail, Outlook, Yahoo, GMX and Proton among them, and the summary says how many copies landed in the inbox, in spam or in promotions. Folder results are informational and never fail the job. The Gmail mailboxes are public test mailboxes. Anyone there can see the subject, the sender name and the folder, so use this only for content that is not confidential.

## Inputs

| Input | Default | |
|---|---|---|
| `smtp-host` | | SMTP relay to send through |
| `smtp-port` | `587` | |
| `smtp-security` | `ssl` on 465, otherwise `starttls` | `starttls`, `ssl` or `none` |
| `smtp-username`, `smtp-password` | | Keep both in secrets |
| `from` | | From address, with a display name if production uses one |
| `subject` | `Deliverability check` | Use the real subject; content checks read it |
| `html-file`, `text-file` | | The bodies, one or both |
| `eml-file` | | A complete message to send instead |
| `placement` | `false` | Also check the folder at email providers |
| `placement-wait-minutes` | `10` | How long to wait for folder results |
| `min-score` | `70` | Fail under this score (0 to 100); `0` turns it off |
| `fail-on-auth` | `true` | Fail on any failed SPF, DKIM, DMARC or alignment check |
| `reserve-only` | `false` | Only reserve an address, for your own code to send to |
| `slug` | | Check an address reserved earlier |
| `timeout-minutes` | `10` | How long to wait for the email to arrive and be checked |
| `lang` | `en` | Language of the report and fix plan, any of 31 |
| `api-url` | `https://email-spam-tester.com` | Only for testing the action itself |

## Outputs

`slug`, `address`, `report-url`, `score` (0 to 100), `score-classic` (0 to 10), `failed-checks` (JSON list of check ids), `passed` (`true` or `false`).

## What to know

- A test address takes one email and expires after an hour. Each run reserves a new one, and only after the inputs and the template files have been checked, so a typo does not use up a test.
- The step runs on Python 3.9 or newer, which every GitHub-hosted runner has. On a self-hosted runner without it, add `actions/setup-python` first.
- A report is public to anyone who has its link, subject and sender included, the way it is on the site. The service deletes the email itself two days after it arrives.
- A check marked `skip` did not apply and counts neither way. When a check could not run on the service's side (a blocklist that timed out), the summary says so and the job is judged on the rest.
- Free tests are counted per IP, and hosted runners come from shared cloud address ranges. If the service ever answers that the daily limit is used up, the step fails with that message.

The same checks are available from a script, from an AI agent over MCP, and from hosting panels: see [email-spam-tester](https://github.com/serg-tanichev/email-spam-tester).

Questions and bug reports: [hi@email-spam-tester.com](mailto:hi@email-spam-tester.com) or an issue here. Built by [Serhii Tanichev](https://email-spam-tester.com/about/). MIT licensed; the license covers this code, not the Email Spam Tester service.
