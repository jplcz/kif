# SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
#
# SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception

"""An independent model of tests/c/kvdb.kif: a plain dict instead of the hash table, the same generator and commands.
Prints what the driver kvdb.c prints (the slots column comes from `Shape`, a port of the table's probing and growth rule: only keys and states, no values)."""
M32, M64 = 0xFFFFFFFF, (1 << 64) - 1


def lines(n):
    state = 12345
    for _ in range(n):
        state = (state * 1664525 + 1013904223) & M32
        r = state
        pick, key, arg = (r >> 8) % 16, (r >> 12) % 48, (r >> 4) % 1000
        if pick == 15:
            yield "BOGUS"
        elif pick == 14:
            yield "SUM"
        else:
            w = ["SET"] * 5 + ["GET"] * 4 + ["DEL"] * 2 + ["INCR"] * 3
            s = f"{w[pick]} k{key}"
            yield s + (f" {arg}" if pick <= 4 or 11 <= pick <= 13 else "")


def fnv32(key):
    h = 2166136261
    for b in key.encode():
        h = ((h ^ b) * 16777619) & M32
    return h


class Shape:
    """The slot array of kvdb.kif's `table`: linear probing, tombstones, rehash when live + deleted would pass 3/4 of the slots."""

    def __init__(self, n):
        self.state, self.key, self.used, self.tombs = [0] * n, [None] * n, 0, 0

    def find(self, key):
        mask = len(self.state) - 1
        i = fnv32(key) & mask
        for _ in range(mask + 1):
            if self.state[i] == 0:
                return None
            if self.state[i] == 1 and self.key[i] == key:
                return i
            i = (i + 1) & mask
        return None

    def rehash(self, n):
        old = list(zip(self.state, self.key))
        self.state, self.key, self.tombs = [0] * n, [None] * n, 0
        for st, k in old:
            if st == 1:
                i = fnv32(k) & (n - 1)
                while self.state[i]:
                    i = (i + 1) & (n - 1)
                self.state[i], self.key[i] = 1, k

    def set(self, key):
        if self.find(key) is not None:
            return
        n = len(self.state)
        if (self.used + self.tombs + 1) * 4 > n * 3:
            self.rehash(n * 2 if (self.used + 1) * 2 > n else n)
        mask = len(self.state) - 1
        i = fnv32(key) & mask
        while self.state[i] == 1:
            i = (i + 1) & mask
        if self.state[i] == 2:
            self.tombs -= 1
        self.state[i], self.key[i] = 1, key
        self.used += 1

    def remove(self, key):
        i = self.find(key)
        if i is not None:
            self.state[i] = 2
            self.used -= 1
            self.tombs += 1


def run(n, slots0=8):
    db, h, errors, shape = {}, 14695981039346656037, 0, Shape(slots0)

    def exec_(t):
        if t[0] == "SET":
            shape.set(t[1]); db[t[1]] = int(t[2]); return (0, int(t[2]))
        if t[0] == "GET":
            return (1, db[t[1]]) if t[1] in db else (2, 0)
        if t[0] == "DEL":
            shape.remove(t[1])
            return (1, 0) if db.pop(t[1], None) is not None else (2, 0)
        if t[0] == "INCR":
            shape.set(t[1]); db[t[1]] = db.get(t[1], 0) + int(t[2]); return (3, db[t[1]])
        return (3, sum(db.values()))  # SUM

    for rnd, line in enumerate(lines(n)):
        t = line.split()
        if t[0] == "BOGUS":
            errors += 1
            continue
        for x in exec_(t):
            h = ((h ^ x) * 1099511628211) & M64
        if rnd % 3 == 0:
            exec_(t)  # the driver answers every third line a second time, into the tally: the database changes again
    return h, errors, len(db), sum(db.values()), len(shape.state)


if __name__ == "__main__":
    for n in (0, 1, 50, 2000, 20000):
        h, e, live, s, slots = run(n)
        print(f"n={n} checksum={h} errors={e} live={live} slots={slots} sum={s} rc=0")
    s = 1
    while s <= 1024:  # the starting size changes the table's shape, never the answers
        print(f"start={s} checksum={run(2000)[0]} rc=0")
        s *= 4
