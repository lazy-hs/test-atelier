# Security Policy

## Supported Deployment Baseline

Production deployments should use:

- PostgreSQL with a dedicated account and encrypted transport where available
- HTTPS at the public edge with `COOKIE_SECURE=1` and HSTS
- a strong `AI_STUDIO_SESSION_SECRET`, administrator password, and `METRICS_TOKEN`
- `AI_STUDIO_EXTERNAL_WORKER=1` with a durable worker and persistent volumes
- immutable API/worker container roots with bounded private tmpfs mounts for
  parser/runtime scratch space
- a read-only skill repository mount and private data/backup directories
- JSON logs validated by `scripts/validate_log_stream.py`

The Linux production dependency set and Apple Silicon macOS environments
select the patched `cryptography` 50+ series. Intel macOS currently has no
49+/50+ binary wheel on the supported Python versions, so its development
marker stays on the latest universal2 wheel available without a local
OpenSSL/Rust toolchain. `48.0.1` is affected by advisories reported by
`pip-audit`; never use that Intel macOS environment as a production image.
Prefer the Linux production image (or build a patched cryptography release
from source) and run dependency auditing in the Linux CI job.

Do not expose the API or Prometheus metrics directly to the public network. The
metrics token must be sent in `X-Metrics-Token` or an Authorization Bearer
header, never in a URL or log message.

## Sensitive Data Boundaries

Application logs intentionally exclude request bodies, uploaded documents,
prompts, model responses, cookies, authorization headers, and provider keys.
Provider credentials are loaded only by the process that executes the provider
request. CLI and skill subprocesses run without shell interpolation and with
application secrets removed from their environment.

Uploaded Word OOXML archives are checked for path traversal, excessive entry
counts, and decompressed-size limits before `python-docx` parses them; timeout
or malformed-document failures return a bounded user-facing error instead of
an unstructured server traceback.

Before sharing diagnostics, run the log validator and remove raw documents or
unredacted exception output. Treat backup archives and the `data/` directory as
confidential project material.

## Reporting a Vulnerability

Do not open a public issue containing credentials, private documents, or an
exploitable proof of concept. Report the issue privately to the repository
maintainer with the affected version, deployment mode, reproduction steps, and
whether any sensitive data may have been exposed. Rotate affected credentials
immediately and preserve only sanitized logs for investigation.

## Operational Response

1. Disable public access or the affected provider integration.
2. Rotate session, database, metrics, provider, and administrator credentials
   as applicable.
3. Preserve sanitized structured logs and the relevant request/job IDs.
4. Verify backups with `scripts/backup_data.py verify` before any restore.
5. Re-run production preflight, smoke checks, and the log contract validator
   after remediation.
