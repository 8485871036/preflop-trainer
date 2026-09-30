import json
d = json.load(open("tools/postflop/work/a72r_output.json", encoding="utf-8"))
node = d["childrens"]["CHECK"]  # BB checks, BTN (hero) to act
st = node["strategy"]["strategy"]
actions = node["actions"]
print("Board: As 7d 2c  (dry ace-high). BB checked. BTN to act.")
print("Canonical hands and their GTO frequencies:\n")
for h in ["AhAs", "AhKh", "AhAd", "2h2d", "Ac7c", "AcKc", "QhJh", "9h8h", "6h5h", "KdQs"]:
    f = st.get(h)
    if not f:
        print(f"  {h:6s} -> not in BTN range")
        continue
    s = ", ".join(f"{a}: {p:.0%}" for a, p in zip(actions, f))
    print(f"  {h:6s} -> {s}")
