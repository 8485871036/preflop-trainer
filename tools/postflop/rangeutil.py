R = "AKQJT98765432"
def ri(r): return R.index(r)

def is_pair_tok(s): return len(s) == 2 and s[0] == s[1]

def parse_token(t):
    t = t.strip()
    if not t:
        return []
    out = []
    if "-" in t:
        a, b = [s.strip() for s in t.split("-")]
        if is_pair_tok(a) and is_pair_tok(b):
            i, j = ri(a[0]), ri(b[0])
            for k in range(min(i, j), max(i, j) + 1):
                out.append(R[k] + R[k])
            return out
        hi, sfx = a[0], a[2]
        i, j = ri(a[1]), ri(b[1])
        for k in range(min(i, j), max(i, j) + 1):
            out.append(hi + R[k] + sfx)
        return out
    if t.endswith("+"):
        base = t[:-1]
        if is_pair_tok(base):
            i = ri(base[0])
            for k in range(0, i + 1):
                out.append(R[k] + R[k])
            return out
        hi, sfx, hIdx = base[0], base[2], ri(base[0])
        for k in range(ri(base[1]), hIdx, -1):
            out.append(hi + R[k] + sfx)
        return out
    return [t]

def expand(s):
    out = set()
    for tok in s.split(","):
        for h in parse_token(tok):
            out.add(h)
    return out

def weighted_range_string(pure_str, border_str=None, border_weight=0.5):
    pure = expand(pure_str)
    border = expand(border_str) - pure if border_str else set()
    parts = [h for h in sorted(pure)]
    parts += [h + ":" + str(border_weight) for h in sorted(border)]
    return ",".join(parts)

if __name__ == "__main__":
    open_btn = "22+, A2s+, K2s+, Q2s+, J4s+, T6s+, 95s, 96s, 97s, 98s, 84s, 85s, 86s, 87s, 74s, 75s, 76s, 63s, 64s, 65s, 53s, 54s, 43s, A2o+, K9o+, QTo+, JTo"
    open_btn_b = "J2s, J3s, T5s, 94s, 83s, 73s, 62s, 52s, 42s, 32s, K7o, K8o, Q8o, Q9o, J8o, J9o, T8o, T9o, 98o, 87o"
    bb_call = "22-66, A6s-A8s, K2s-K9s, Q2s-Q9s, J2s-J8s, T6s-T8s, 95s, 96s, 97s, 84s, 85s, 86s, 74s, 75s, 63s, 64s, 53s, A2o-A9o, K9o-KQo, QTo, QJo, JTo"
    bb_call_b = "T5s, 94s, 83s, 73s, 62s, 52s, 43s, K8o, K7o, Q9o, J9o, T9o, 98o"
    ip = weighted_range_string(open_btn, open_btn_b)
    oop = weighted_range_string(bb_call, bb_call_b)
    print("IP len", len(ip.split(",")))
    print(ip)
    print()
    print("OOP len", len(oop.split(",")))
    print(oop)
