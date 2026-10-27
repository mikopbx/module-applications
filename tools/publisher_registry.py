# Copyright 2026 the module-applications authors
# SPDX-License-Identifier: Apache-2.0
"""What the licence server knows about publishers and module ids (rule APP-011).

The checks need two answers only the server has: is support_email the address on the confirmed publisher's card, and
is module_id free or already this publisher's. The server API is LIC-613 and will be provided later (owner's decision of
2026-10-27); until then this is an explicit STUB: `default()` returns StubRegistry, which answers "not_checked". A "not_checked" verdict never fails validation and never counts as
passed: whoever registers the module after merge (LIC-614) must ask the server itself.

A registry implements one method:

    check(module_id, publisher_id, support_email) -> Verdict

    Verdict.status   "ok"           the card's address matches and the id is free or the publisher's
                     "mismatch"     the address differs, or the id belongs to someone else (detail says which)
                     "not_checked"  the server was not asked (detail says why)
"""
import dataclasses


@dataclasses.dataclass(frozen=True)
class Verdict:
    status: str
    detail: str
    source: str


class StubRegistry:
    """Stand-in until the licence server has an API for it (LIC-613)."""

    def check(self, module_id, publisher_id, support_email):
        return Verdict(
            status="not_checked",
            detail="the licence server API (LIC-613) does not exist yet: the publisher card and module_id ownership are checked at registration",
            source="stub",
        )


class StaticRegistry:
    """A registry from a fixed table, for tests: {publisher_id: {"support_email": ..., "modules": [...]}}, plus
    "owners": {module_id: publisher_id} for ids registered to anyone."""

    def __init__(self, publishers, owners=None):
        self.publishers = publishers
        self.owners = owners or {}

    def check(self, module_id, publisher_id, support_email):
        card = self.publishers.get(publisher_id)
        if card is None:
            return Verdict("mismatch", f"publisher_id {publisher_id} is not a confirmed publisher", "static")
        if card["support_email"].lower() != support_email.lower():
            return Verdict("mismatch", f"support_email {support_email!r} is not the address on publisher {publisher_id}'s card", "static")
        owner = self.owners.get(module_id)
        if owner is not None and owner != publisher_id:
            return Verdict("mismatch", f"module_id {module_id!r} is registered to another publisher", "static")
        return Verdict("ok", "the card's address matches and module_id is free or the publisher's", "static")


def default():
    return StubRegistry()
