#!/usr/bin/env python3
"""Probe: the cross-repo edge from the example STATUS.yaml still holds --
invoice reminders are handed to the mailer service, so its send API must
be reachable (replace mailer:send_api with the edge.to of your own edge).

This is a stub that always passes. Replace the body with a real check
before relying on it -- for example, calling the mailer's health endpoint
or sending a dry-run request to the send API.

Exit 0: edge holds. Exit 1: broken. Exit 77: prerequisite missing (skip).
"""
import sys


def main() -> int:
    return 0


if __name__ == "__main__":
    sys.exit(main())
