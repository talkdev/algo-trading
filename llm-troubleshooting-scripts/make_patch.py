#!/usr/bin/env python3
"""Generate the hunk table for patch_v14.py.

Reads /home/user/patchgen/pristine (git HEAD, i.e. the v13 tree) and
/home/user/patchgen/modified (the measured v14 tree) and emits a line-based
hunk payload plus per-file SHA-256 metadata. Every hunk set is simulated here
before it is written out, so the payload cannot describe a patch that does not
reproduce the measured tree byte for byte.
"""
import difflib
import hashlib
import pathlib
import sys

BASE = pathlib.Path('/home/user/patchgen')
PRIS, MOD = BASE / 'pristine', BASE / 'modified'
FILES = ['core.py', 'data_engine.py', 'regime_engine.py', 'strategy_engine.py',
         'execution_engine.py', 'backtest_engine.py', 'verify_all.py']


def sha(t):
    return hashlib.sha256(t.encode('utf-8')).hexdigest()


def make_hunks(a, b, ctx=3):
    """Group the diff opcodes into hunks with `ctx` lines of context.

    opcode is (tag, i1, i2, j1, j2): a[i1:i2] becomes b[j1:j2]. Groups are
    merged when EITHER side's intervening gap is small - a gap of 7 unchanged
    lines in the old file can be a gap of 2 in the new one, and splitting there
    would emit the new file's lines twice.
    """
    sm = difflib.SequenceMatcher(a=a, b=b, autojunk=False)
    ops = [op for op in sm.get_opcodes() if op[0] != 'equal']
    if not ops:
        return []
    groups, cur = [], [ops[0]]
    for op in ops[1:]:
        gap_a = op[1] - cur[-1][2]
        gap_b = op[3] - cur[-1][4]
        if min(gap_a, gap_b) <= 2 * ctx:
            cur.append(op)
        else:
            groups.append(cur)
            cur = [op]
    groups.append(cur)
    out = []
    for g in groups:
        a1 = max(0, g[0][1] - ctx)
        a2 = min(len(a), g[-1][2] + ctx)
        b1 = max(0, g[0][3] - ctx)
        b2 = min(len(b), g[-1][4] + ctx)
        # context must be identical on both sides or the hunk is malformed
        assert a[a1:a1 + ctx] == b[b1:b1 + ctx], 'head context mismatch'
        assert a[a2 - ctx:a2] == b[b2 - ctx:b2], 'tail context mismatch'
        out.append((a1, a2, b1, b2))
    for prev, nxt in zip(out, out[1:]):
        assert prev[1] <= nxt[0], 'a-side overlap'
        assert prev[3] <= nxt[2], 'b-side overlap'
    return out


def locate(lines, old, hint):
    """Find `old` in `lines`, preferring the recorded position."""
    n = len(old)
    if n == 0:
        return hint
    if lines[hint:hint + n] == old:
        return hint
    hits = [i for i in range(max(0, len(lines) - n + 1))
            if lines[i:i + n] == old]
    if not hits:
        raise AssertionError('hunk not found anywhere in the file')
    return min(hits, key=lambda i: abs(i - hint))


def apply(lines, hunks):
    lines = list(lines)
    for (_idx, old, new, hint) in sorted(hunks, key=lambda h: -h[3]):
        at = locate(lines, old, hint)
        lines[at:at + len(old)] = new
    return lines


payload, meta = [], {}
for f in FILES:
    o = (PRIS / f).read_text(encoding='utf-8')
    n = (MOD / f).read_text(encoding='utf-8')
    a = o.splitlines(keepends=True)
    b = n.splitlines(keepends=True)
    hunks = []
    for (a1, a2, b1, b2) in make_hunks(a, b, 3):
        hunks.append((len(hunks) + 1, a[a1:a2], b[b1:b2], a1))
    got = ''.join(apply(a, hunks))
    if got != n:
        print(f'{f}: SIMULATION FAILED ({len(got)} vs {len(n)} bytes)',
              file=sys.stderr)
        sys.exit(1)
    meta[f] = {'pristine_sha': sha(o), 'target_sha': sha(n),
               'hunks': len(hunks), 'lines_before': len(a),
               'lines_after': len(b)}
    payload.append((f, hunks))
    print(f'{f}: {len(hunks):>2} hunks  {len(a)}->{len(b)} lines  '
          f'{sha(o)[:12]}->{sha(n)[:12]}  simulation OK')

(BASE / 'payload.py').write_text(repr(payload), encoding='utf-8')
(BASE / 'meta.py').write_text(repr(meta), encoding='utf-8')
print(f'\ntotal hunks: {sum(m["hunks"] for m in meta.values())}   '
      f'payload: {(BASE / "payload.py").stat().st_size:,} bytes')