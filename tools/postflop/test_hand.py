import sys, json
sys.path.insert(0, "tools/postflop")
import gto_server as g

for trial in range(3):
    h = g.Hand(g.SPOTS[0])
    print(f"\n=== TRIAL {trial} hero={h.hero} board={h.board} ===")
    steps = 0
    while not h.hand_over and steps < 30:
        steps += 1
        acts = h.hero_actions()
        if not acts:
            print("  (no actions) toAct=", h.to_act)
            break
        strat = h.hero_strategy()
        keys = {a["key"] for a in acts}
        if "CHECK" in keys:
            key = "CHECK"
        elif "CALL" in keys:
            key = "CALL"
        else:
            key = max(strat, key=strat.get)
        h.hero_act(key)
        print(f"  step{steps} street={h.street} pot={h.pot} chose={key} -> correct={h.last_decision['correctLabel']} (correct={h.last_decision['correct']})")
    print("  RESULT:", json.dumps(h.result))
    for x in h.history:
        print("     ", x["street"], x["actor"], x["label"])
