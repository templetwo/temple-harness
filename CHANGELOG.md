# Changelog

## unreleased

### mcp-shim 0.3.1

- Fail closed on HTTP 3xx. `urllib.request.urlopen` followed redirects and
  copied `Authorization` onto the next hop; anything answering on the bridge
  port could exfiltrate the token. `_http_json` now refuses redirects as a
  `BridgeError`.

### log-distill 0.1.1

- `--list` paths and `DistillError` stderr now pass through `_redact`. A
  session directory named with a key-shaped string no longer leaves unmasked.
- Id-less tool/result matching no longer overwrites the first `callId=None`
  call with every subsequent id-less result.
- Redaction patterns gain `github_pat_`, Slack `xox[baprs]-`, and Google
  `AIza` shapes. Floor stated next to the pattern list: whitespace-splayed,
  base64-wrapped, and homoglyph secrets are out of scope.

### repo

- GitHub Actions workflow and `run-tests.sh` so law enforcement is not a
  one-time manual act.
