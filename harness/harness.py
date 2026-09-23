#!/usr/bin/env python3
"""Synthetic-corpus generator for the azzurra-services flatfile migration tests.

Drives N synthetic users through REAL services commands against the
azzurra-testnet (bahamut hub + leaves + azzurra/services), journaling every
issued command and snapshotting the resulting database.

Two waves per run (bahamut class Y:1 caps 512 local clients/server, and CS
ACCESS ADD requires already-registered targets):
  wave 1 — every user connects, NICKSERV REGISTERs (+ SET flips), quits;
  wave 2 — every user reconnects, joins its assigned channels, founders run
           CHANSERV REGISTER/ACCESS/AKICK/TOPIC, ambient PRIVMSG/memos.

Per run (seeded):
  out/seed-<seed>/journal.ndjson   raw lines: {ts,nick,dir,line}
  out/seed-<seed>/passwords.json   nick->pass, #chan->pass, founders
  out/seed-<seed>/manifest.json    counts, timings, errors

stdlib-only (asyncio); targets a python:3.12-alpine runner attached to the
testnet's svc-net/leaf4-net/leaf6-net. Requires SVC_AKILL_CLONES=0 on the
services container (1000 clients share one runner IP — CLONEKILL default 5
would autokill the whole run).
"""

import argparse
import asyncio
import json
import random
import socket
import time
from pathlib import Path

HOSTS = [
    ("hub.azzurra.chat", 6667),    # spread across hub + both leaves so the
    ("leaf4.azzurra.chat", 6667),  # per-server 512-client class cap is never
    ("leaf6.azzurra.chat", 6667),  # hit and the corpus spans cross-server
]                                   # joins.

ADJ = ["nero", "verde", "rosso", "blu", "calmo", "fermo", "alto", "basso",
       "dolce", "scuro", "chiaro", "antico", "nuovo", "rapido", "lento"]
NOUN = ["lupo", "orso", "aquila", "gatto", "cervo", "volpe", "gufo",
        "martello", "chiave", "ponte", "torre", "fiume",
        "monte", "isola", "stella", "vento", "pietra"]
CHAN = ["azzurra", "aiuto", "chat", "musica", "calcio", "vino", "cucina",
        "viaggi", "libri", "film", "scuola", "lavoro", "game", "notte"]

# azzurra ChanServ is XOP-model (SOP/AOP/HOP/VOP); numeric ACCESS is not a
# services command on this tree.
# NS SET keys safe to flip without email/oper prerequisites.
NS_SETS = ["KILL", "SECURE", "PRIVATE", "HIDE", "MSG"]


class Client:
    def __init__(self, nick, password, host, port, out, rng):
        self.nick = nick
        self.password = password
        self.host = host
        self.port = port
        self.out = out
        self.rng = rng
        self.reader = None
        self.writer = None
        self.registered = False
        self.chan_ok = 0
        self.errors = []
        self.sent = 0

    # --- plumbing --------------------------------------------------------
    def journal(self, direction, line):
        self.out.write(json.dumps({
            "ts": round(time.time(), 3), "nick": self.nick,
            "dir": direction, "line": line}) + "\n")

    async def send(self, line):
        self.writer.write((line + "\r\n").encode())
        await self.writer.drain()
        self.sent += 1
        self.journal("send", line)

    async def connect(self, want001=True):
        # bahamut is compiled IPv4-only (options.h #undef INET6) but the
        # runner is multi-homed on nets that carry hub AAAA/ULAs — pin
        # AF_INET or sockets die on the unreachable v6 address.
        self.reader, self.writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port,
                                    family=socket.AF_INET), timeout=15)
        if want001:
            await self.send(f"NICK {self.nick}")
            await self.send(f"USER {self.nick} 0 * :{self.nick}")
            # Block on the ircd 001 before ANY service traffic — commands
            # sent pre-registration get dropped with 451 and services never
            # sees them. 45s: bahamut resolves ident/rDNS per connection and
            # the resolver queue backs up under bursts.
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline:
                line = await asyncio.wait_for(self.reader.readline(),
                                              timeout=15)
                if not line:
                    raise ConnectionError("eof")
                text = line.decode("utf-8", "replace").rstrip("\r\n")
                self.journal("recv", text)
                if " 001 " in text.upper():
                    return
                if " 433 " in text.upper() or " 432 " in text.upper():
                    raise ConnectionError("nick held/erroneous")

    async def connect_retry(self, attempts=3):
        """Transient zero-byte/433 connects happen under bursts (bahamut's
        per-connection ident/rDNS path queues). CLOSE the previous socket on
        retry — a leaked socket keeps the NICK held server-side and the next
        attempt gets 433 forever."""
        last = None
        for a in range(attempts):
            try:
                await self.connect()
                return
            except Exception as exc:  # noqa: BLE001
                last = exc
                if self.writer is not None:
                    try:
                        self.writer.close()
                    except Exception:  # noqa: BLE001
                        pass
                await asyncio.sleep(3 * (a + 1))
        raise last

    async def quit(self):
        try:
            await self.send("QUIT :corpus")
            await asyncio.wait_for(self.reader.read(), timeout=5)
        except Exception:  # noqa: BLE001 — quit best-effort
            pass
        if self.writer:
            self.writer.close()

    async def wait_notice(self, who, needle, timeout=15):
        """Read until a NOTICE from `who` contains `needle` (case-insens).

        Returns the matched line, or None on timeout/no-match. Never raises
        on read timeouts — stray mid-window timeouts must not poison the
        per-client error count.
        """
        self.last_notice = None
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                line = await asyncio.wait_for(self.reader.readline(),
                                              timeout=5)
            except asyncio.TimeoutError:
                continue
            if not line:
                raise ConnectionError("eof")
            text = line.decode("utf-8", "replace").rstrip("\r\n")
            up = text.upper()
            if ("001 " in up or "376 " in up or "NOTICE" in up
                    or " JOIN " in up):
                self.journal("recv", text)
            if who.upper() in up and "NOTICE" in up:
                self.last_notice = text
                if needle.upper() in up:
                    return text
            if " 433 " in up or " 432 " in up:
                return None
        return None

    # --- waves -----------------------------------------------------------
    async def wave1(self, ramp):
        """Register the nickname (inline — EMAIL off, no AUTH round-trip)."""
        await asyncio.sleep(ramp)
        try:
            await self.connect_retry()
            # azzurra NickServ: REGISTER <password> <email> — email is
            # REQUIRED and format-validated (nickserv.c do_register).
            # Success notice = NS_REGISTER_REG_OK_1 ("Your password is %s…")
            # — the word "registered" never appears.
            await self.send(f"PRIVMSG NickServ :REGISTER {self.password} "
                            f"{self.nick}@example.com")
            notice = await self.wait_notice("NickServ", "password is", 20)
            if notice is None and 'already registered' in (
                    self.last_notice or '').lower():
                # Our own earlier attempt registered server-side but the
                # client gave up waiting — IDENTIFY instead of re-REGISTER.
                await self.send(f"PRIVMSG NickServ :IDENTIFY {self.password}")
                notice = await self.wait_notice("NickServ", "identified", 15)
            self.registered = notice is not None
            if self.registered:
                for key in self.rng.sample(
                        NS_SETS, self.rng.randint(0, len(NS_SETS))):
                    await self.send(
                        f"PRIVMSG NickServ :SET {key} "
                        f"{self.rng.choice(['ON', 'OFF'])}")
                    await asyncio.sleep(0.05)
                if self.rng.random() < 0.3:
                    await self.send(
                        f"PRIVMSG NickServ :SET EMAIL "
                        f"{self.nick}@example.test")
            await self.quit()
        except Exception as exc:  # noqa: BLE001 — per-client isolation
            self.errors.append(f"w1 {type(exc).__name__}: {exc}")

    async def wave2(self, chans_for_me, members_of, ramp):
        """Founder phase: JOIN (gets @ as creator), IDENTIFY, CS REGISTER,
        XOP/TOPIC/AKICK. Members run wave2_member AFTER all founders, so a
        founder is always channel operator when CS REGISTER checks."""
        await asyncio.sleep(ramp)
        try:
            await self.connect_retry()
            # New session = unidentified; CS REGISTER/XOP require an
            # identified founder ("Type /NS IDENTIFY nick password").
            await self.send(f"PRIVMSG NickServ :IDENTIFY {self.password}")
            await self.wait_notice("NickServ", "identified", 15)
            for chan, role in chans_for_me:
                if role != "founder":
                    continue  # member joins happen in wave2_member only —
                    # an early member JOIN would create the channel and
                    # leave the founder without @op for CS REGISTER.
                await self.send(f"JOIN {chan}")
                # CS_REGISTER_REG_OK_1: "Channel %s has been successfully
                # registered to nickname %s." Founder nick + nick pass
                # must differ from channel pass (English.lang:385).
                await self.send(
                    f"PRIVMSG ChanServ :REGISTER {chan} "
                    f"{self.password}-chan {chan.lstrip('#')} "
                    f"auto-generated")
                if await self.wait_notice(
                        "ChanServ", "successfully registered", 15):
                    self.chan_ok += 1
                await self.send(f"TOPIC {chan} :Corpus topic for {chan}")
                # azzurra ChanServ is XOP-model: SOP/AOP/HOP/VOP (numeric
                # ACCESS replies "Unknown command").
                for tier in self.rng.sample(("SOP", "AOP", "HOP", "VOP"), 2):
                    target = self.rng.choice(members_of[chan])
                    await self.send(
                        f"PRIVMSG ChanServ :{tier} {chan} ADD {target}")
                    await asyncio.sleep(0.05)
                if self.rng.random() < 0.5:
                    await self.send(
                        f"PRIVMSG ChanServ :AKICK {chan} ADD "
                        f"*!ghost@*.example")
                # S2S propagation headroom between channel registrations.
                await asyncio.sleep(0.3)
            await self.quit()
        except Exception as exc:  # noqa: BLE001 — per-client isolation
            self.errors.append(f"w2f {type(exc).__name__}: {exc}")

    async def wave2_member(self, chans_for_me, ramp):
        """Member phase: JOIN assigned channels + ambient traffic."""
        if not chans_for_me:
            return
        await asyncio.sleep(ramp)
        try:
            await self.connect_retry()
            for chan, _role in chans_for_me:
                await self.send(f"JOIN {chan}")
                await asyncio.sleep(0.1)
            for _ in range(self.rng.randint(1, 4)):
                await self.send(
                    f"PRIVMSG {chans_for_me[0][0]} :hello from {self.nick}")
                await asyncio.sleep(self.rng.uniform(0.1, 0.4))
            if self.rng.random() < 0.15:  # MEMO_DELAY:20-throttled
                await self.send(f"PRIVMSG MemoServ :SEND {self.nick} ping")
            await self.quit()
        except Exception as exc:  # noqa: BLE001 — per-client isolation
            self.errors.append(f"w2m {type(exc).__name__}: {exc}")


def build_plan(seed, n_users, n_chans):
    """1 channel per founder: CONF_REGISTER_DELAY (30s, per session) forbids
    a user registering several channels in one session, and founders ==
    users keeps 1000/1000 runs exact (U_ACC_MAX:5 is never the binding
    constraint this way)."""
    rng = random.Random(seed)
    nicks, passwords = set(), {}
    while len(nicks) < n_users:
        nick = (rng.choice(ADJ) + "_" + rng.choice(NOUN)
                + str(rng.randint(0, 9999)))
        if nick not in nicks:
            nicks.add(nick)
            passwords[nick] = f"pw-{seed}-{len(nicks)}-" + "".join(
                rng.choice("abcdefgh23456789") for _ in range(8))
    nicks = sorted(nicks)
    chan_names, chan_pass = set(), {}
    while len(chan_names) < n_chans:
        name = "#" + rng.choice(CHAN) + str(rng.randint(0, 99999))
        if name not in chan_names:
            chan_names.add(name)
            chan_pass[name] = "cpw-" + "".join(
                rng.choice("abcdefgh23456789") for _ in range(8))
    chans = sorted(chan_names)
    founders = dict(zip(chans, rng.sample(nicks, min(len(chans),
                                                     len(nicks)))))
    members_of = {c: [] for c in chans}
    assignments = {n: [] for n in nicks}
    for c in chans:
        f = founders[c]
        assignments[f].append((c, "founder"))
        pool = [n for n in rng.sample(nicks, min(6, len(nicks))) if n != f]
        members_of[c] = pool
        for m in pool[:3]:
            # members can sit in several channels; nick registration
            # happened in wave 1 so XOP ADD targets are valid.
            assignments[m].append((c, "member"))
    return nicks, passwords, chans, chan_pass, founders, assignments, members_of


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, required=True)
    ap.add_argument("--users", type=int, default=1000)
    ap.add_argument("--channels", type=int, default=1000)
    ap.add_argument("--out", required=True)
    ap.add_argument("--ramp", type=float, default=0.05,
                    help="per-client connect stagger seconds")
    args = ap.parse_args()

    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)
    (nicks, passwords, chans, chan_pass, founders,
     assignments, members_of) = build_plan(args.seed, args.users,
                                           args.channels)

    journal = open(outdir / "journal.ndjson", "w", encoding="utf-8")
    t0 = time.monotonic()
    clients = []
    for i, nick in enumerate(nicks):
        host, port = HOSTS[i % len(HOSTS)]
        clients.append(Client(nick, passwords[nick], host, port,
                              journal, random.Random(args.seed * 1000003 + i)))

    # Cap concurrent connects: bahamut resolves ident/rDNS per connection
    # on a serial resolver path — unbounded bursts back it up for minutes.
    sem = asyncio.Semaphore(25)

    async def paced(coro):
        async with sem:
            await coro

    ramp = lambda i: args.ramp * (i % 40)  # noqa: E731 — connect stagger
    await asyncio.gather(*(paced(c.wave1(ramp(i)))
                           for i, c in enumerate(clients)))
    # Founders first (JOIN = creator op → CS REGISTER), then members.
    await asyncio.gather(*(paced(c.wave2(assignments[c.nick], members_of,
                                         ramp(i)))
                           for i, c in enumerate(clients)
                           if assignments[c.nick]))
    await asyncio.gather(*(paced(c.wave2_member(assignments[c.nick],
                                                ramp(i)))
                           for i, c in enumerate(clients)))
    journal.close()

    manifest = {
        "seed": args.seed,
        "users_requested": args.users,
        "channels_requested": args.channels,
        "nicks_registered": sum(1 for c in clients if c.registered),
        "channels_register_cmd_ok": sum(c.chan_ok for c in clients),
        "channels_planned": len(chans),
        "commands_sent": sum(c.sent for c in clients),
        "clients_with_errors": sum(1 for c in clients if c.errors),
        "error_sample": [e for c in clients for e in c.errors][:20],
        # NS wording oracle: first captured services notices, so a needle
        # mismatch is diagnosable from the manifest alone.
        "services_notice_sample": [c.last_notice for c in clients
                                   if getattr(c, "last_notice", None)][:5],
        "duration_s": round(time.monotonic() - t0, 1),
    }
    (outdir / "manifest.json").write_text(json.dumps(manifest, indent=2))
    (outdir / "passwords.json").write_text(json.dumps({
        "nicks": passwords, "channels": chan_pass,
        "founders": founders}, indent=2))
    print(json.dumps(manifest))


if __name__ == "__main__":
    asyncio.run(main())
