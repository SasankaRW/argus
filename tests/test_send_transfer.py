from __future__ import annotations

import asyncio
import os

import uvicorn

from _send_kit import Spy, free_port, make_agent, make_file, pair, tree, wait_for, wait_state

CH = 16 * 1024


def run_two(tmp_path, body, a_kw=None, b_kw=None, start_b=True):
    """Two agents (pc sends, calypso receives) around `body(a, b)`."""

    async def go():
        a = make_agent(tmp_path, "pc", "PC", **(a_kw or {}))
        b = make_agent(tmp_path, "calypso", "Calypso", **(b_kw or {}))
        pair(a, b)
        await a.start()
        if start_b:
            await b.start()
        try:
            return await body(a, b)
        finally:
            await a.stop()
            if start_b:
                await b.stop()

    return asyncio.run(go())


def test_send_file_folder_names_and_mtime(tmp_path):
    src = tmp_path / "src"
    data = make_file(src / "hello.txt", 50_000)
    os.utime(src / "hello.txt", (1_600_000_000, 1_600_000_000))
    make_file(src / "album" / "a.bin", 3 * CH + 5, 2)
    make_file(src / "album" / "deep" / "b.bin", 0, 3)

    async def body(a, b):
        out = b.root / "From PC"
        oid = a.enqueue("calypso", [src / "hello.txt", src / "album"])
        await wait_state(a, oid, "done")
        got = tree(out)
        assert got["hello.txt"] == data and got["album/a.bin"] == (src / "album" / "a.bin").read_bytes()
        assert got["album/deep/b.bin"] == b""
        assert int((out / "hello.txt").stat().st_mtime) == 1_600_000_000  # mtimes are kept
        assert not list((b.root / ".incoming").iterdir())  # nothing left half-done
        # same name, other content -> "name (2).ext"; same bytes again -> not saved again
        (src / "hello.txt").write_bytes(b"different")
        oid = a.enqueue("calypso", [src / "hello.txt"])
        await wait_state(a, oid, "done")
        assert (out / "hello (2).txt").read_bytes() == b"different"
        oid = a.enqueue("calypso", [src / "hello.txt"])
        await wait_state(a, oid, "done")
        assert sorted(p.name for p in out.glob("hello*")) == ["hello (2).txt", "hello.txt"]
        assert (out / "hello.txt").read_bytes() == data  # the user's file was never touched
        kinds = [h["kind"] for h in b.store.history()]
        assert kinds.count("received") == 3 and [h["kind"] for h in a.store.history()].count("sent") == 3

    run_two(tmp_path, body)


def test_same_name_twice_in_a_row(tmp_path):
    src = tmp_path / "s"
    make_file(src / "one" / "x.txt", 100, 1)
    make_file(src / "two" / "x.txt", 100, 2)

    async def body(a, b):
        for d in ("one", "two"):
            await wait_state(a, a.enqueue("calypso", [src / d / "x.txt"]), "done")
        assert sorted(tree(b.root / "From PC")) == ["x (2).txt", "x.txt"]

    run_two(tmp_path, body)


def test_resume_after_killed_connection(tmp_path):
    src = tmp_path / "big.bin"
    data = make_file(src, 20 * CH)
    spy = Spy()
    spy.kill_after = 6

    async def body(a, b):
        oid = a.enqueue("calypso", [src])
        item = await wait_for(lambda: (i := a.store.get(oid))["attempts"] >= 1 and i, what="the break")
        assert item["state"] == "queued" and "did not get through" in item["reason"]
        stored = sum(len(set(p.read_text().split())) for p in (b.root / ".incoming" / oid).glob("*.chunks"))
        assert 0 < stored < 20 and not (b.root / "From PC").exists()
        before = len(spy.puts)
        spy.heal()
        a.wake()
        await wait_state(a, oid, "done")
        assert len(spy.puts) - before == 20 - stored  # only what was missing went again
        assert (b.root / "From PC" / "big.bin").read_bytes() == data

    run_two(tmp_path, body, a_kw={"transport": spy})


def test_corrupted_chunk_is_resent(tmp_path):
    src = tmp_path / "c.bin"
    data = make_file(src, 4 * CH)
    spy = Spy()
    spy.corrupt_once = True

    async def body(a, b):
        oid = a.enqueue("calypso", [src])
        await wait_state(a, oid, "done")
        assert len(spy.puts) == 5  # four chunks and one resend
        assert (b.root / "From PC" / "c.bin").read_bytes() == data

    run_two(tmp_path, body, a_kw={"transport": spy})


def test_receiver_off_then_on(tmp_path):
    src = tmp_path / "late.bin"
    data = make_file(src, 3 * CH)

    async def body(a, b):
        oid = a.enqueue("calypso", [src])
        item = await wait_for(lambda: (i := a.store.get(oid))["attempts"] >= 2 and i, what="waiting")
        assert item["state"] == "queued" and "not reachable" in item["reason"]
        await b.start()  # Calypso comes up
        try:
            await wait_state(a, oid, "done")
            assert (b.root / "From PC" / "late.bin").read_bytes() == data
        finally:
            await b.stop()

    run_two(tmp_path, body, start_b=False)


def test_disk_full_507_keeps_it_queued(tmp_path):
    src = tmp_path / "huge.bin"
    data = make_file(src, 5 * CH)

    async def body(a, b):
        rx = b.app.state.receiver
        rx.free_bytes = lambda: 1000
        oid = a.enqueue("calypso", [src])
        item = await wait_for(lambda: (i := a.store.get(oid))["attempts"] >= 1 and i, what="the refusal")
        assert item["state"] == "queued" and "not enough room" in item["reason"]
        assert not (b.root / "From PC").exists()
        del rx.free_bytes  # space freed
        await wait_state(a, oid, "done")
        assert (b.root / "From PC" / "huge.bin").read_bytes() == data

    run_two(tmp_path, body)


def test_folder_of_2000_small_files(tmp_path):
    count = 2000 if os.environ.get("ARGUS_ALL_TESTS") == "1" else 300  # the full 2000 takes about 20 s
    src = tmp_path / "many"
    for i in range(count):
        make_file(src / f"d{i % 20}" / f"f{i}.txt", 40 + i % 90, i % 200)

    async def body(a, b):
        oid = a.enqueue("calypso", [src])
        await wait_state(a, oid, "done", timeout=100)
        got, want = tree(b.root / "From PC" / "many"), tree(src)
        assert len(got) == count and got == want

    run_two(tmp_path, body)


def test_path_switch_moves_remaining_chunks(tmp_path):
    src = tmp_path / "long.bin"
    data = make_file(src, 240 * CH)
    lan_port = free_port()
    spy = Spy()
    spy.delay = 0.03

    async def body(a, b):
        # calypso: the LAN address does not answer yet, the Tailscale name (here a second port) does
        dev = dict(b.me(), lan=[f"127.0.0.1:{lan_port}"], tailscale=f"127.0.0.1:{b.cfg.port}")
        a.devices.put(dev)
        lan = uvicorn.Server(uvicorn.Config(b.app, host="127.0.0.1", port=lan_port, log_level="warning",
                                            lifespan="off"))
        tasks = []
        spy.on_put = lambda n: tasks.append(asyncio.create_task(lan.serve())) if n == 8 else None
        oid = a.enqueue("calypso", [src])
        item = await wait_state(a, oid, "done", timeout=60)
        lan.should_exit = True
        await asyncio.gather(*tasks)
        ports = [p for p, _q in spy.puts]
        assert ports[0] == b.cfg.port and ports[-1] == lan_port  # started on Tailscale, finished on the LAN
        assert ports.count(lan_port) > 40 and item["route"] == "lan"
        assert (b.root / "From PC" / "long.bin").read_bytes() == data

    run_two(tmp_path, body, a_kw={"transport": spy})
