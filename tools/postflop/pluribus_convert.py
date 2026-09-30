"""
Pluribus ke 10,000 hands (Brown & Sandholm 2019, Science) -> GG jaisa hand history text,
taaki app ka Hand Review parser + stats unhe waise hi padh le. Pluribus = "hero".

Source: https://github.com/uoftcprg/phh-dataset (data/pluribus, PHH format)
  git clone --depth 1 --filter=blob:none --sparse https://github.com/uoftcprg/phh-dataset.git work/phh
  cd work/phh && git sparse-checkout set data/pluribus

Run:   python pluribus_convert.py
Out:   work/pluribus_gg.txt   (GTO server /api/pluribus se app ko deta hai)

PHH: p1=SB, p2=BB, p3..p6 (p6 = button). Actions: 'pN f' fold, 'pN cc' check/call,
'pN cbr X' bet/raise TO X (street total), 'pN sm CARDS' showdown, 'd db CARDS' board.
Blinds 50/100, stacks 10000 (=100bb).
"""
import tomllib
import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE / "work" / "phh" / "data" / "pluribus"
OUT = HERE / "work" / "pluribus_gg.txt"
HERO = "Pluribus"


def load_phh(path):
    return tomllib.loads(path.read_text(encoding="utf-8"))    # PHH = TOML


def convert(h, hand_id, when):
    names = h["players"]
    n = len(names)
    stacks = h["starting_stacks"]
    blinds = h["blinds_or_straddles"]
    sb, bb = blinds[0], blinds[1]
    L = [f"Poker Hand #PL{hand_id}: Hold'em No Limit (${sb}/${bb}) - {when:%Y/%m/%d %H:%M:%S}",
         f"Table 'Pluribus' 6-max Seat #{n} is the button"]
    for i, nm in enumerate(names):
        L.append(f"Seat {i + 1}: {nm} (${stacks[i]} in chips)")
    put = [0] * n                 # is street pe daala
    inv = [0] * n                 # poore hand me daala
    L.append(f"{names[0]}: posts small blind ${blinds[0]}")
    L.append(f"{names[1]}: posts big blind ${blinds[1]}")
    for i in (0, 1):
        put[i] = inv[i] = blinds[i]
    holes, folded, shown = {}, set(), {}
    board, hole_done = [], False
    last_agg = None

    for act in h["actions"]:
        t = act.split()
        if t[0] == "d" and t[1] == "dh":
            holes[int(t[2][1:]) - 1] = [t[3][i:i + 2] for i in range(0, len(t[3]), 2)]
            continue
        if not hole_done:
            L.append("*** HOLE CARDS ***")
            hero_i = names.index(HERO)
            L.append(f"Dealt to {HERO} [{' '.join(holes[hero_i])}]")
            hole_done = True
        if t[0] == "d" and t[1] == "db":
            new = [t[2][i:i + 2] for i in range(0, len(t[2]), 2)]
            prev = board[:]
            board += new
            street = {3: "FLOP", 4: "TURN", 5: "RIVER"}[len(board)]
            L.append(f"*** {street} *** [{' '.join(board)}]" if street == "FLOP"
                     else f"*** {street} *** [{' '.join(prev)}] [{new[0]}]")
            put = [0] * n
            last_agg = None
            continue
        i = int(t[0][1:]) - 1
        nm = names[i]
        mx = max(put)
        if t[1] == "f":
            L.append(f"{nm}: folds")
            folded.add(i)
        elif t[1] == "cc":
            if put[i] >= mx:
                L.append(f"{nm}: checks")
            else:
                amt = min(mx - put[i], stacks[i] - inv[i])
                allin = inv[i] + amt >= stacks[i]
                L.append(f"{nm}: calls ${amt}" + (" and is all-in" if allin else ""))
                put[i] += amt
                inv[i] += amt
        elif t[1] == "cbr":
            to = int(t[2])
            add = to - put[i]
            allin = inv[i] + add >= stacks[i]
            if mx == 0:
                L.append(f"{nm}: bets ${add}" + (" and is all-in" if allin else ""))
            else:
                L.append(f"{nm}: raises ${to - mx} to ${to}" + (" and is all-in" if allin else ""))
            put[i] = to
            inv[i] += add
            last_agg = i
        elif t[1] == "sm":
            shown[i] = [t[2][k:k + 2] for k in range(0, len(t[2]), 2)] if len(t) > 2 else None

    # uncalled bet wapas (baaki sab fold)
    live = [i for i in range(n) if i not in folded]
    if last_agg is not None and len(live) == 1:
        others = max(p for j, p in enumerate(put) if j != last_agg)
        if put[last_agg] > others:
            back = put[last_agg] - others
            L.append(f"Uncalled bet (${back}) returned to {names[last_agg]}")
            inv[last_agg] -= back
    L.append("*** SHOWDOWN ***")
    if len(live) >= 2:                                     # sirf jo showdown tak pahunche
        for i in live:
            if names[i] != HERO:
                L.append(f"{names[i]}: shows [{' '.join(shown.get(i) or holes[i])}]")
    for i in range(n):
        won = h["finishing_stacks"][i] - stacks[i] + inv[i]
        if won > 0:
            L.append(f"{names[i]} collected ${won} from pot")
    L.append("*** SUMMARY ***")
    return "\n".join(L)


def main():
    folders = sorted((d for d in SRC.iterdir() if d.is_dir()),
                     key=lambda d: (int("".join(c for c in d.name if c.isdigit())), d.name))
    base = datetime.datetime(2019, 7, 11)
    out, k, skipped = [], 0, 0
    for d in folders:
        for f in sorted(d.glob("*.phh"), key=lambda p: int(p.stem)):
            h = load_phh(f)
            if HERO not in h.get("players", []):
                skipped += 1
                continue
            out.append(convert(h, f"{d.name}-{f.stem}", base + datetime.timedelta(seconds=k)))
            k += 1
    OUT.write_text("\n\n".join(out) + "\n", encoding="utf-8")
    print(f"{k} hands -> {OUT} ({OUT.stat().st_size // 1024} KB), skipped {skipped}")


if __name__ == "__main__":
    main()
