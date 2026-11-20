"""Wishlist news also goes into an Ari chat, "Updates from Argus", so it can be read later on the phone."""

from __future__ import annotations

import time

from argus.ari import ari as ari_mod
from argus.outbox import add_message, phone_message


def test_wishlist_news_lands_in_the_updates_chat(store):
    msg = phone_message("Watcher: Wishlist: Dune book", "Wishlist: Dune book is back in stock at LKR 4,495.5.",
                        priority="high", tags=["eyes", "wishlist"])
    store.write_sync(lambda c: add_message(c, time.time(), "phone", msg))
    chats = {c["conv"]: c for c in store.read_sync(ari_mod.chats)}
    assert chats["updates"]["title"] == "Updates from Argus"
    (turn,) = store.read_sync(lambda c: ari_mod.turns(c, "updates"))
    assert turn["role"] == "ari" and turn["text"] == "Dune book is back in stock at LKR 4,495.5."


def test_other_messages_stay_out_of_the_chat(store):
    msg = phone_message("Job failed: backup", "disk full", priority="high")
    store.write_sync(lambda c: add_message(c, time.time(), "phone", msg))
    assert store.read_sync(ari_mod.chats) == []
