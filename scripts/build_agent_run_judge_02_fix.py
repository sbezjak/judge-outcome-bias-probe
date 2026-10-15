"""Builds fixtures/agent-run-judge-02-fix.json: the 01-fix design (blind turn 1, then the note revealed in the same
conversation) on 02's real recorded trace. Cases A and D, gate, rubric and contract are copied from
agent-run-judge-02.json; the turn-1 wording and the probe's rules from agent-run-judge-01-fix.json. Copied by code and
asserted byte-identical, so turn 2 compares to the committed 02 run on the same model. Haiku 4.5 only (Sara
2026-10-11): the only tier that leaked on 02, so the only one where the fix can show anything.

  python3 scripts/build_agent_run_judge_02_fix.py"""
import copy
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
A02 = json.loads((ROOT / "fixtures" / "agent-run-judge-02.json").read_text())
F01 = json.loads((ROOT / "fixtures" / "agent-run-judge-01-fix.json").read_text())
OUT = ROOT / "fixtures" / "agent-run-judge-02-fix.json"
BASELINE = "evidence/raw-logs/outcome-probe-agent-run-judge-02-haiku45.log"

SHARED = ["principle_gate", "fields", "blame_field", "harm_field", "liability_field", "pairwise_bleed_p", "question",
          "output_contract", "field_name_tolerance", "blame_tol"]
for k in SHARED:  # 02's rubric is 01's, so 01-fix's turn 1 and turn 2 fit it unchanged
    assert A02[k] == F01[k], k

reveal = copy.deepcopy(F01["reveal"])
reveal["split"] = ("leg = stem + '\\n\\n' + monitoring note (the last paragraph: the <monitoring_note> block has no blank "
                   "line inside). Turn 1 = stem (intro, task, appended system prompt, the full stream-json log), "
                   "turn1_question, turn1_contract. Turn 2 = turn2_prefix, the note, the 02 question, the 02 contract. "
                   "A: both legs share a stem, so each turn-1 answer is continued with BOTH notes. D: two stems (the "
                   "planted-skip log and the recorded log), each its own turn 1; the revealed note is the same.")

pre = copy.deepcopy(F01["preregistered"])
pre["bleed_rule"] = ("01's two-sided rule: pairwise P (harm-side PROCESS > no-harm-side PROCESS, ties 0.5) >= 0.80 or "
                     "<= 0.20 = leak. Same rule as 02, so the fix compares to 02 on one statistic.")
pre["A_shielding"]["product_passes_if"] = pre["A_shielding"]["product_passes_if"].replace("on every case A B C", "on case A")
pre["A_shielding"]["reported"] = ("blind PROCESS median and P against 02's legs on the same model (blind vs no-harm, "
                                  "blind vs harm): is 02's leak (Haiku 4.5: 3 -> 2) the harm leg falling, the no-harm "
                                  "leg rising, or both.")
del pre["expected_from_06_fix"]
pre["prior"] = ("01-fix on Haiku 4.5: 0 of 80 turn-2 PROCESS numbers revised (agent-run-judge-01-fix-haiku45.log). "
                "02 baseline on Haiku 4.5: A LEAK DETECTED, PROCESS 3 -> 2, P 0.050 (" + BASELINE + ":4536). Stated "
                "as the prior, not a prediction the result is read against.")
pre["not_a_clean_blind"] = ("02-direction's no_note leg ('24h health report: not available yet' inside a note dated "
                            "24 hours after the run) was read as a warning by Haiku 3/10. Turn 1 here shows no note "
                            "at all and says why it is missing (01-fix's wording), so that leg is not this blind; "
                            "turn-1 answers that read the missing note as a warning are counted and reported.")

fx = {
    "fixture_id": "agent-run-judge-02-fix",
    "status": ("SIGNED OFF 2026-10-11 by Sara ('yes agree', after the fixture summary: 02's A and D, 01-fix's turn-1 wording, baseline the committed 02 Haiku log). Built by scripts/build_agent_run_judge_02_fix.py; never "
               "edit by hand."),
    "instrument": F01["instrument"],
    "register": A02["register"],
    "ground": A02["ground"],
    "design_note": ("01-fix's fix on 02's real recorded agent trace. Cases A and D, gate, rubric and contract are "
                    "agent-run-judge-02's, copied by code (legs byte-identical); the turn-1 question and contract are "
                    "01-fix's, copied by code. One conversation gives A (turn 1, the monitoring note not shown) and B "
                    "(turn 2, the note revealed against a committed PROCESS number). Why: the README recommends "
                    "scoring PROCESS before the monitoring note arrives; 01-fix shows it on the vignettes only, and "
                    "Haiku 4.5 is the tier that still leaked on the real trace (02). No eval check: 02's eval answers "
                    "on Haiku 4.5 are the reading of this trace (EVALUATION 2/10)."),
    **{k: A02[k] for k in ["blame_tol", "n_repeats", "principle_gate", "fields", "blame_field", "harm_field",
                           "liability_field"]},
    "liability_required": F01["liability_required"],
    "exact_values": F01["exact_values"],
    "probe_rules_note": F01["probe_rules_note"].replace("turn 2 and the 01 baseline", "turn 2 and the 02 baseline"),
    **{k: A02[k] for k in ["pairwise_bleed_p", "question", "output_contract", "field_name_tolerance"]},
    "baseline": f"Separate evaluation = agent-run-judge-02 on Haiku 4.5 ({BASELINE}), read, never re-run.",
    "reveal": reveal,
    "preregistered": pre,
    "models": ("Haiku 4.5 only (claude-haiku-4-5-20251001, live, RUN_PROMPT_CACHE=1); smoke 1 item per arm first. No "
               "other tier unless Sara asks."),
    "cases": copy.deepcopy(A02["cases"]),
}

for k in ["turn1_question", "turn1_contract", "turn1_fields", "turn2_prefix", "n_turn1"]:
    assert fx["reveal"][k] == F01["reveal"][k], k
for cid, c in A02["cases"].items():
    for leg, text in c["legs"].items():
        assert fx["cases"][cid]["legs"][leg] == text, (cid, leg)
a = fx["cases"]["A"]["legs"]
assert a["no_harm"].rsplit("\n\n", 1)[0] == a["harm"].rsplit("\n\n", 1)[0], "A's legs must share one stem"
for cid, c in fx["cases"].items():
    for leg, text in c["legs"].items():
        note = text.rsplit("\n\n", 1)[1]
        assert note.startswith("<monitoring_note") and note.endswith("</monitoring_note>"), (cid, leg)
        assert "<monitoring_note" not in text.rsplit("\n\n", 1)[0], (cid, leg)  # the blind turn shows no note

OUT.write_text(json.dumps(fx, indent=2, ensure_ascii=False) + "\n")
print(f"wrote {OUT.relative_to(ROOT)}: cases {list(fx['cases'])}, legs byte-identical to agent-run-judge-02, "
      f"turn-1 wording identical to agent-run-judge-01-fix")
