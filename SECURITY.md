# Security policy

## Reporting a vulnerability

Please report it privately through GitHub: open the repository's **Security**
tab and choose **Report a vulnerability**. Do not open a public issue.

Include what an attacker could do, the version (the Health tab shows it), and
how to reproduce it. You should get a first answer within a week; this is a
project maintained in spare time, so a fix may take longer, and you will hear
how it is going.

## Supported versions

Fixes go into the latest release. Before 1.0 there are no backports.

## What counts

InferenceInquire is meant to run on a trusted network or behind a proxy that
authenticates (see [docs/security.md](docs/security.md)). The page having no
login is by design. Examples of what does count:

- a way to read the agent's data without its token, or past `AGENT_ALLOW`;
- a way to make the dashboard or the agent change anything on the host, the
  GPUs or llama.cpp;
- script injection through data the page shows, such as unit, container or
  model names;
- secrets leaking into logs, the page or the API.
