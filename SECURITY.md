# Reporting a vulnerability

This is a network video recorder: a weakness in it can expose cameras, footage or the network they are on. Please report one
**privately**, never in a public issue, pull request or discussion.

## How to report

Use GitHub's private vulnerability reporting for this repository: the **Security** tab, then **Report a vulnerability**
(`https://github.com/gvsccrc-it/nvr/security/advisories/new`). Only the repository's owner sees the report.

If that button is not there, open a public issue that says only that you have a security report and asks for a private way to send it,
with no details of the problem.

## What to include

- what the weakness is, and what an attacker could do with it;
- the version or image you found it in (`docker compose images nvr`, or Settings > Updates), or the commit;
- how to reproduce it, as small as you can make it;
- anything you know about how to fix or limit it.

Do not include real camera addresses, passwords, footage or anything from a real site: use documentation addresses
(`192.0.2.0/24`, `198.51.100.0/24`, `203.0.113.0/24`) and `example.test`.

## What happens next

The report is acknowledged on the advisory, and the fix is discussed there in private. Once a fix is released, the advisory is published
with credit to you unless you would rather not be named.

## Supported versions

The latest released version (the `image:` line of `compose.yaml`). The `edge` image is a test build and gets fixes by being rebuilt.
