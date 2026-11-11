# Werewolf agent — make.fun werewolf-agent-01

A purely rule-based Werewolf player (Python 3.13, standard library only).
No model calls, no network except the env at `MAKEFUN_ENV_URL`.

## How it plays

- Loops `POST /observe`, answers every decision waiting on its seat, exits
  once the trial is done.
- Suspicion model: parses speeches for seer claims, claimed checks,
  accusations and defences; tracks votes day by day; scores players against
  revealed roles (deaths and exiles); a night victim's accusations count
  against their targets; vote blocs that move together count against both.
  The seer's own inspection results override everything.
- Villager/doctor: votes the most suspicious seat, with the evidence in
  words. Seer: inspects the top suspect and announces a found wolf at once.
  Doctor: protects the most credible seer claimant, else the most accurate
  accuser. Werewolf: votes and talks like a villager (never its partner),
  matches its partner's night pick when the partner voted first, else kills
  the most dangerous villager (seer claimant first).
- Every decision is a pure function of the game's view plus
  `MAKEFUN_TRIAL_SEED` (one seeded stream per game and purpose): no wall
  clock, no unseeded randomness, so a trial replays exactly.

## Run

```sh
MAKEFUN_ENV_URL=http://127.0.0.1:18080 \
MAKEFUN_PROMPT_FILE=/input/prompt.txt \
MAKEFUN_TRIAL_SEED=<trial seed> \
python3 -P -s main.py
```

## Local test (practice trial 0, stub house players)

The kit's env server needs a house lane; `/tmp/house_stub.py` is a small
stub that answers it with weak but valid heuristic play, and
`/tmp/run_trial.sh <trial> <seed>` runs the env + stub + this agent and
scores the result with the kit's scorer. Practice results (stub house):
trial 0 score 500 (wolf 600, village 400), trial 1 score 433, trial 2
score 300; 30/30 games finished every time, 0 house errors, 0 fallbacks.
Two runs of trial 0 with the same seed produce byte-identical state.json.
