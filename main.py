#!/usr/bin/env python3
"""Werewolf agent for make.fun werewolf-agent-01 (agent/v1, Python 3.13, stdlib only).

A purely rule-based player: no model calls, no network except the env at
MAKEFUN_ENV_URL. It loops POST /observe, answers every decision waiting on
its seat, and exits once the trial is done.

How it plays
------------
Per game it rebuilds everything from the view the env serves (so every
decision is a pure function of that view plus the trial seed: the trial's
replay sees the same requests).

- Suspicion model (village side): every living seat gets a score from the
  public record. Speeches are parsed for role claims (who claims seer, and
  what checks they claim), accusations and defences; votes are tracked day
  by day; revealed roles (from deaths and exiles) score the people who
  pushed for or against them; a night victim's accusations count against
  their targets; vote blocs that move together count against both. The
  seer's own inspection results override everything (a found wolf is a
  certain wolf, a cleared seat is cleared).
- As a villager / doctor it votes the most suspicious seat and says why,
  citing the evidence (a contradicted seer claim, votes for a revealed
  villager, defending a revealed wolf).
- As the seer it inspects the most suspicious seat, and the moment it finds
  a wolf it announces itself and the result and votes that wolf.
- As the doctor it protects the most credible seer claimant, else the
  villager whose accusations have proved right, else the loudest accuser.
- As a werewolf it votes and talks like a villager using the same model,
  but never votes or accuses its partner; at night it matches its partner's
  pick when the partner voted first, else names the most dangerous villager
  (the seer claimant first, then the most accurate accuser), the target a
  partner is likeliest to name too.

Randomness comes only from MAKEFUN_TRIAL_SEED (one stream per game and
purpose, via sha256), never from the wall clock: a trial replays exactly.
"""

import hashlib
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.request

# ------------------------------------------------------------------ env client


def _post(url, body, timeout=120):
    """One POST of a JSON object; returns (status, parsed JSON or None).

    Retries a few times when no answer came back at all (a request the env
    never saw has no replay record, so retrying it is safe). An answered
    request is never re-sent: /act answers 409 when the decision is gone.
    """
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    last = None
    for _ in range(4):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as res:
                raw = res.read()
                try:
                    return res.status, json.loads(raw.decode("utf-8"))
                except ValueError:
                    return res.status, None
        except urllib.error.HTTPError as err:
            try:
                raw = err.read()
            except Exception:
                raw = b""
            try:
                return err.code, json.loads(raw.decode("utf-8"))
            except ValueError:
                return err.code, None
        except (urllib.error.URLError, OSError) as err:
            last = err
            time.sleep(1)
    raise IOError("env unreachable after retries: %s" % (last,))


def _rng(seed, *labels):
    """Deterministic RNG: sha256(seed | label ...) -> random.Random."""
    key = "|".join([str(seed)] + [str(x) for x in labels])
    n = int.from_bytes(hashlib.sha256(key.encode("utf-8")).digest()[:8], "big")
    return random.Random(n)


# ------------------------------------------------------------------ view parsing

NAME_RE = re.compile(r"[A-Za-z][A-Za-z\-']*")


class Game:
    """Everything the agent knows about one game, rebuilt from one view."""

    def __init__(self, view, seed):
        self.view = view
        self.day = view.get("day", 1)
        self.phase = view.get("phase", "")
        you = view.get("you") or {}
        self.me = you.get("seat")
        self.my_name = you.get("name", "")
        self.role = you.get("role", "")
        self.alive = bool(you.get("alive", True))
        partner = you.get("partner") or {}
        self.partner = partner.get("seat") if partner else None
        self.partner_alive = bool(partner.get("alive")) if partner else False
        self.players = {}
        for p in view.get("players") or []:
            self.players[p["seat"]] = {
                "name": p.get("name", "seat %d" % p["seat"]),
                "alive": bool(p.get("alive", True)),
                "role": p.get("role"),
            }
        self.log = view.get("log") or []
        self.seer_known = {}  # seat -> True if werewolf, False if not
        for r in view.get("seerResults") or []:
            self.seer_known[r["seat"]] = bool(r.get("werewolf"))
        last = view.get("lastProtect") or {}
        self.last_protect = last.get("seat")
        self.wolf_votes = view.get("wolfVotes") or []
        self.seed = seed
        self._parse_log()

    # -- derived sets ------------------------------------------------
    def living(self):
        return [s for s, p in self.players.items() if p["alive"]]

    def living_others(self):
        return [s for s in self.living() if s != self.me]

    def revealed(self):
        """seat -> role for players whose role the game has revealed."""
        return {s: p["role"] for s, p in self.players.items() if p["role"]}

    def revealed_wolves(self):
        return {s for s, r in self.revealed().items() if r == "werewolf"}

    def revealed_villagers(self):
        return {s for s, r in self.revealed().items() if r in ("villager", "seer", "doctor")}

    def name(self, seat):
        p = self.players.get(seat)
        return p["name"] if p else "seat %d" % seat

    def rng(self, *labels):
        return _rng(self.seed, self.view.get("game", 0), *labels)

    # -- log parsing --------------------------------------------------
    def _parse_log(self):
        self.speeches = []   # (seat, day, round, text)
        self.votes = {}      # day -> [(voter, target)]
        self.exiles = []     # (day, seat, role)
        self.deaths = []     # (day, seat, role)
        self.accusations = []  # (accuser, target, day)
        self.defences = []     # (defender, target, day)
        self.seer_claims = set()
        self.doctor_claims = set()
        self.claimed_checks = {}  # claimant -> {seat: is_wolf}
        for e in self.log:
            kind = e.get("kind")
            day = e.get("day", 1)
            if kind == "speech":
                text = e.get("text", "")
                self.speeches.append((e.get("seat"), day, e.get("round", 1), text))
                self._parse_speech(e.get("seat"), day, text)
            elif kind == "vote":
                self.votes.setdefault(day, []).append((e.get("seat"), e.get("target")))
            elif kind == "exile":
                self.exiles.append((day, e.get("seat"), e.get("role")))
            elif kind == "dawn":
                died = e.get("died")
                if died is not None:
                    self.deaths.append((day, died, e.get("role")))

    def _seat_from(self, text, m):
        """A seat number from a regex match, or a name matched to a seat."""
        if m is None:
            return None
        g = m.group(1) if m.groups() else None
        if g is None:
            return None
        if g.isdigit():
            seat = int(g)
            return seat if seat in self.players else None
        low = g.lower()
        for s, p in self.players.items():
            if p["name"].lower() == low:
                return s
        return None

    def _parse_speech(self, speaker, day, text):
        if not text or speaker is None:
            return
        t = text.lower()
        # role claims
        if re.search(r"\bi(?:'m| am)\s+(?:the\s+)?seer\b", t) or re.search(r"\bas\s+seer\b.{0,20}\bi\b", t):
            self.seer_claims.add(speaker)
        if re.search(r"\bi(?:'m| am)\s+(?:the\s+)?doctor\b", t):
            self.doctor_claims.add(speaker)
        # claimed checks: "checked seat 4", "inspected 4", "seat 4 is a werewolf"
        for m in re.finditer(r"(?:checked|inspected|looked at|scanned)\s+(?:seat\s+)?(\d+)", t):
            seat = self._seat_from(text, m)
            if seat is None or seat == speaker:
                continue
            is_wolf = None
            tail = t[m.end():m.end() + 40]
            if re.search(r"\b(werewolf|wolf)\b", tail) and "not" not in tail[:12]:
                is_wolf = True
            elif re.search(r"\b(not a (?:were)?wolf|innocent|clear|villager)\b", tail):
                is_wolf = False
            if is_wolf is not None:
                self.claimed_checks.setdefault(speaker, {})[seat] = is_wolf
        for m in re.finditer(r"(?:seat\s+)?(\d+)\s+is\s+(?:a\s+)?(werewolf|wolf)\b", t):
            seat = self._seat_from(text, m)
            if seat is not None and seat != speaker:
                self.accusations.append((speaker, seat, day))
        for m in re.finditer(r"(?:seat\s+)?(\d+)\s+is\s+not\s+(?:a\s+)?(?:werewolf|wolf)\b", t):
            seat = self._seat_from(text, m)
            if seat is not None and seat != speaker:
                self.defences.append((speaker, seat, day))
        for m in re.finditer(r"(?:seat\s+)?(\d+)\s+is\s+innocent\b", t):
            seat = self._seat_from(text, m)
            if seat is not None and seat != speaker:
                self.defences.append((speaker, seat, day))
        for m in re.finditer(r"\bvote\s+(?:for\s+)?(?:seat\s+)?(\d+)\b", t):
            seat = self._seat_from(text, m)
            if seat is not None and seat != speaker:
                self.accusations.append((speaker, seat, day))
        # name-based accusations: "Tariq is a wolf"
        for m in re.finditer(r"\b([A-Za-z][A-Za-z\-']*)\s+is\s+(?:a\s+)?(?:werewolf|wolf)\b", text):
            seat = self._seat_from(text, m)
            if seat is not None and seat != speaker and (speaker, seat, day) not in self.accusations:
                self.accusations.append((speaker, seat, day))

    # -- evidence ------------------------------------------------------
    def claim_trust(self):
        """seat -> trust of a seer claimant: 1 trusted, -1 contradicted, 0 unproven."""
        trust = {}
        revealed = self.revealed()
        for c in self.seer_claims:
            t = 0
            for seat, is_wolf in (self.claimed_checks.get(c) or {}).items():
                if seat in revealed:
                    if (revealed[seat] == "werewolf") == is_wolf:
                        t = 1
                    else:
                        t = -1
                        break
            trust[c] = t
        return trust

    def liar_claimants(self):
        return {c for c, t in self.claim_trust().items() if t == -1}

    def trusted_claimants(self):
        return {c for c, t in self.claim_trust().items() if t == 1}

    def accusation_count(self, seat):
        """How many distinct accusers named this seat (excluding my own)."""
        return len({a for (a, t, _d) in self.accusations if t == seat and a != self.me})

    def speech_count(self, seat):
        return sum(1 for (s, _d, _r, _t) in self.speeches if s == seat)


# ------------------------------------------------------------------ suspicion model

def suspicion(game):
    """seat -> score (higher = more likely a wolf), village's view.

    Built only from the public record plus my own seer results. My partner
    (when I am a wolf) is scored -999 so I never vote or accuse them.
    """
    scores = {s: 0.0 for s in game.living_others()}
    if not scores:
        return scores
    revealed_wolves = game.revealed_wolves()
    revealed_villagers = game.revealed_villagers()
    trust = game.claim_trust()
    liars = game.liar_claimants()
    trusted = game.trusted_claimants()

    # my own seer results override everything
    for seat, is_wolf in game.seer_known.items():
        if seat in scores:
            scores[seat] = 100.0 if is_wolf else -100.0

    # seer claims: a contradicted claimant is almost certainly a wolf;
    # two claimants means one lies; a trusted claimant's found wolf is a wolf.
    for c in game.seer_claims:
        if c not in scores and c != game.me:
            continue
        if c in liars and c in scores:
            scores[c] += 45.0
        if c in game.seer_claims and len(game.seer_claims) > 1 and c in scores:
            scores[c] += 6.0
    for c in trusted:
        for seat, is_wolf in (game.claimed_checks.get(c) or {}).items():
            if is_wolf and seat in scores and game.seer_known.get(seat) is not True:
                scores[seat] += 35.0
            if not is_wolf and seat in scores and game.seer_known.get(seat) is not False:
                scores[seat] -= 25.0

    # accusations in speeches
    for (a, t, _d) in game.accusations:
        if t not in scores or a == game.me:
            continue
        if a in revealed_wolves:
            scores[t] -= 4.0
        elif a in liars:
            scores[t] -= 6.0
        elif a in trusted:
            scores[t] += 30.0
        else:
            scores[t] += 3.0

    # defences in speeches
    for (a, t, _d) in game.defences:
        if t not in scores or a == game.me:
            continue
        if a in revealed_wolves:
            scores[t] += 6.0
        elif a in liars:
            scores[t] += 4.0
        else:
            scores[t] -= 3.0

    # votes, day by day
    for _day, votes in game.votes.items():
        for (v, t) in votes:
            if t is None:
                continue
            if t in revealed_wolves and v in scores:
                scores[v] -= 6.0
            elif t in revealed_villagers and v in scores:
                scores[v] += 4.0
            if v in revealed_wolves and t in scores:
                scores[t] -= 2.0
    # vote blocs: pairs voting the same target on 2+ days
    pair_days = {}
    for day, votes in game.votes.items():
        by_target = {}
        for (v, t) in votes:
            if t is not None:
                by_target.setdefault(t, []).append(v)
        for _t, vs in by_target.items():
            for i in range(len(vs)):
                for j in range(i + 1, len(vs)):
                    key = (min(vs[i], vs[j]), max(vs[i], vs[j]))
                    pair_days.setdefault(key, set()).add(day)
    for (a, b), days in pair_days.items():
        if len(days) < 2:
            continue
        if a in revealed_wolves and b in scores:
            scores[b] += 12.0
        if b in revealed_wolves and a in scores:
            scores[a] += 12.0

    # night deaths: the victim's accusations count against their targets
    for (_day, victim, _role) in game.deaths:
        for (a, t, _d) in game.accusations:
            if a == victim and t in scores:
                scores[t] += 10.0
        for (a, t, _d) in game.defences:
            if a == victim and t in scores:
                scores[t] -= 6.0

    if game.role == "werewolf" and game.partner in scores:
        scores[game.partner] = -999.0
    return scores


def ranked_suspicion(game):
    """Living others, most suspicious first; ties broken by the seeded RNG."""
    scores = suspicion(game)
    rng = game.rng("tiebreak", game.day)
    order = list(scores.keys())
    rng.shuffle(order)
    order.sort(key=lambda s: -scores[s])
    return order, scores

# ------------------------------------------------------------------ decisions

def fold(text):
    """Fold a statement the env's way: runs of breaks/tabs -> one space, other
    control characters dropped, ends trimmed."""
    text = re.sub(r"[\t\n\v\f\r\x85\u2028\u2029]+", " ", text)
    text = "".join(ch for ch in text if not (ord(ch) < 32 or ord(ch) == 127))
    return text.strip()


def clip(text, max_chars):
    folded = fold(text)
    chars = list(folded)
    if len(chars) <= max_chars:
        return folded
    return "".join(chars[:max_chars]).strip()


def found_wolf(game):
    """A living wolf my seer checks found, else None."""
    for seat, is_wolf in game.seer_known.items():
        if is_wolf and seat in game.living() and seat != game.me:
            return seat
    return None


def best_reason(game, target, scores):
    """One short evidence phrase for suspecting `target`, from the record."""
    revealed = game.revealed()
    liars = game.liar_claimants()
    if target in liars:
        return "they claimed to be the seer, but their check was wrong"
    for (a, t, _d) in game.accusations:
        if a == target:
            pass
    for (v, t) in [(v, t) for _d, vs in game.votes.items() for (v, t) in vs]:
        if v == target and t in revealed and revealed[t] in ("villager", "seer", "doctor"):
            return "they voted to exile %s, who turned out to be a %s" % (
                game.name(t), revealed[t])
    for (a, t, _d) in game.defences:
        if a == target and t in revealed and revealed[t] == "werewolf":
            return "they defended %s, who turned out to be a werewolf" % game.name(t)
    for (_d, victim, _r) in game.deaths:
        for (a, t, _dd) in game.accusations:
            if a == victim and t == target:
                return "%s accused them before dying last night" % game.name(victim)
    n = game.accusation_count(target)
    if n >= 2:
        return "%d players have pointed at them" % n
    if game.speech_count(target) == 0 and game.day >= 2:
        return "they have stayed quiet while others talked"
    return "their story does not add up"


def speak_text(game, max_chars):
    """What I say on my debate turn (or None to pass)."""
    rng = game.rng("speak", game.day, len(game.speeches))
    order, scores = ranked_suspicion(game)
    top = order[0] if order else None
    top_score = scores.get(top, 0.0) if top is not None else 0.0
    second = order[1] if len(order) > 1 else None
    wolf = found_wolf(game)
    my_seat = game.me

    # seer with a found wolf: announce it, every round until they are gone.
    if game.role == "seer" and wolf is not None:
        variants = [
            "I am the seer. I checked seat %d (%s) last night and they are a werewolf. Vote seat %d today." % (wolf, game.name(wolf), wolf),
            "I checked seat %d (%s) last night: a werewolf. I am the seer, trust me on this one and vote seat %d." % (wolf, game.name(wolf), wolf),
        ]
        return clip(rng.choice(variants), max_chars)

    # a liar claims to be the seer while I am the seer: call it out.
    if game.role == "seer":
        liars = [c for c in game.seer_claims if c != my_seat and c in game.living()]
        if liars:
            liar = liars[0]
            return clip(
                "Seat %d (%s) claims to be the seer, but I am the seer. Their claim is a lie, do not follow them." % (liar, game.name(liar)),
                max_chars)

    # I am being pushed: defend myself, and deflect to my best suspect.
    my_accusers = {a for (a, t, _d) in game.accusations if t == my_seat and a != my_seat}
    if len(my_accusers) >= 2:
        accuser = sorted(my_accusers, key=lambda s: -scores.get(s, 0.0))[0]
        if top is not None and top_score >= 3.0 and top != game.partner:
            return clip(
                "I am not a wolf. Seat %d (%s) is pushing me with no evidence, and seat %d (%s) looks far worse: %s." % (
                    accuser, game.name(accuser), top, game.name(top), best_reason(game, top, scores)),
                max_chars)
        return clip(
            "I am not a wolf. Seat %d (%s) is pushing me with no evidence at all, ask them why they are so sure." % (
                accuser, game.name(accuser)),
            max_chars)

    # strong suspect: accuse with the evidence.
    if top is not None and top_score >= 10.0:
        reason = best_reason(game, top, scores)
        if game.role == "werewolf":
            variants = [
                "I agree with the case on seat %d (%s): %s. Let us vote seat %d." % (top, game.name(top), reason, top),
                "Seat %d (%s) looks worst to me: %s. I am voting seat %d." % (top, game.name(top), reason, top),
            ]
        else:
            variants = [
                "Seat %d (%s) is my pick: %s. Vote seat %d." % (top, game.name(top), reason, top),
                "Look at seat %d (%s): %s. We should exile seat %d today." % (top, game.name(top), reason, top),
            ]
        return clip(rng.choice(variants), max_chars)

    # mild suspicion: raise it as a question, do not hard-push.
    if top is not None and top_score >= 3.0:
        if game.role == "werewolf" and top == game.partner:
            top = second
        if top is not None:
            variants = [
                "I am not sure about seat %d (%s) yet. %s, what do the rest of you think?" % (top, game.name(top), best_reason(game, top, scores).capitalize()),
                "Seat %d (%s) caught my eye: %s. Worth watching today." % (top, game.name(top), best_reason(game, top, scores)),
            ]
            return clip(rng.choice(variants), max_chars)

    # day 1, nothing to go on: a light observation, sometimes a pass.
    # (never name my partner as the one to watch: that only helps the village)
    if game.day <= 1:
        if rng.random() < 0.25:
            return None
        others = [s for s in game.living_others() if s != game.partner]
        if others:
            quiet = [s for s in others if game.speech_count(s) == 0]
            pick = rng.choice(quiet) if quiet else rng.choice(others)
            variants = [
                "No strong read yet. Seat %d (%s) has been quiet, I want to hear from them before we vote." % (pick, game.name(pick)),
                "Too early to point fingers. I am watching seat %d (%s), they seemed nervous." % (pick, game.name(pick)),
                "Let us talk first. Nothing solid yet, but I will be listening to who pushes whom.",
            ]
            return clip(rng.choice(variants), max_chars)
        return None

    # later days, no lead: agree with the room or note the quiet ones.
    if rng.random() < 0.2:
        return None
    if top is not None:
        return clip(
            "I have no firm read. Seat %d (%s) is the one I trust least right now, but I am open to other views." % (top, game.name(top)),
            max_chars)
    return None


def decide_vote(game, options):
    """My exile vote (a seat, or None to abstain)."""
    if not options:
        return None
    wolf = found_wolf(game)
    if wolf is not None and wolf in options:
        return wolf
    order, scores = ranked_suspicion(game)
    for seat in order:
        if seat in options:
            return seat
    rng = game.rng("vote", game.day)
    return rng.choice(list(options))


def decide_kill(game, options):
    """Tonight's victim (a seat, or None). Match my partner's pick if they
    already voted; otherwise name the most dangerous villager."""
    if not options:
        return None
    night = game.day
    for v in game.wolf_votes:
        if v.get("night") == night and v.get("seat") == game.partner and v.get("target") in options:
            return v["target"]
    # most dangerous villager: credible seer claimant first
    trust = game.claim_trust()
    claimants = [c for c in game.seer_claims if c in options and trust.get(c, 0) >= 0]
    if claimants:
        claimants.sort(key=lambda s: (-game.accusation_count(s), s))
        return claimants[0]
    # the most accurate accuser: someone whose accusations hit revealed wolves,
    # or who accused me or my partner
    best = None
    best_score = -1
    revealed_wolves = game.revealed_wolves()
    for s in options:
        score = 0
        for (a, t, _d) in game.accusations:
            if a == s and t in revealed_wolves:
                score += 3
            if a == s and t in (game.me, game.partner):
                score += 2
        score += game.speech_count(s)
        if score > best_score:
            best_score = score
            best = s
    if best is not None and best_score > 0:
        return best
    rng = game.rng("kill", night)
    return rng.choice(list(options))


def decide_protect(game, options):
    """Who the doctor protects tonight (a seat, or None)."""
    if not options:
        return None
    trust = game.claim_trust()
    claimants = [c for c in game.seer_claims if c in options and trust.get(c, 0) >= 0]
    if claimants:
        return claimants[0]
    # the villager whose accusations proved right, or the loudest accuser
    best = None
    best_score = -1
    revealed_wolves = game.revealed_wolves()
    for s in options:
        score = 0
        for (a, t, _d) in game.accusations:
            if a == s and t in revealed_wolves:
                score += 3
        score += game.speech_count(s)
        if score > best_score:
            best_score = score
            best = s
    if best is not None and best_score > 0:
        return best
    rng = game.rng("protect", game.day)
    return rng.choice(list(options))


def decide_inspect(game, options):
    """Who the seer inspects tonight (a seat, or None)."""
    if not options:
        return None
    # a seer claimant is the best check: I am the seer, so they are lying
    # (and most likely a wolf); otherwise inspect the top suspect.
    claimants = [c for c in game.seer_claims if c in options and c != game.me]
    if claimants:
        return claimants[0]
    order, _scores = ranked_suspicion(game)
    for seat in order:
        if seat in options and seat not in game.seer_known:
            return seat
    rng = game.rng("inspect", game.day)
    return rng.choice(list(options))


def decide(game_num, pending, seed):
    """One decision -> the /act body for it."""
    view = pending.get("view") or {}
    game = Game(view, seed)
    kind = pending.get("decision")
    options = pending.get("options") or []
    if kind == "speak":
        text = speak_text(game, pending.get("max_chars") or 500)
        if text is None or text == "":
            return {"game": game_num, "decision": "speak", "pass": True}
        return {"game": game_num, "decision": "speak", "text": text}
    if kind == "vote":
        return {"game": game_num, "decision": "vote", "target": decide_vote(game, options)}
    if kind == "kill":
        return {"game": game_num, "decision": "kill", "target": decide_kill(game, options)}
    if kind == "protect":
        return {"game": game_num, "decision": "protect", "target": decide_protect(game, options)}
    if kind == "inspect":
        return {"game": game_num, "decision": "inspect", "target": decide_inspect(game, options)}
    return {"game": game_num, "decision": kind, "target": None}


def fallback(pending):
    if pending.get("decision") == "speak":
        return {"game": pending.get("game"), "decision": "speak", "pass": True}
    return {"game": pending.get("game"), "decision": pending.get("decision"), "target": None}


# ------------------------------------------------------------------ main loop

def main():
    env_url = (os.environ.get("MAKEFUN_ENV_URL") or "").rstrip("/")
    if not env_url:
        sys.stderr.write("MAKEFUN_ENV_URL is not set\n")
        return 1
    seed = os.environ.get("MAKEFUN_TRIAL_SEED") or "werewolf-agent-01"
    prompt_file = os.environ.get("MAKEFUN_PROMPT_FILE") or ""
    if prompt_file:
        try:
            with open(prompt_file, "r", encoding="utf-8") as f:
                f.read()
        except OSError:
            pass
    decided = 0
    while True:
        try:
            _status, obs = _post(env_url + "/observe", {})
        except IOError as err:
            sys.stderr.write("observe failed: %s\n" % (err,))
            time.sleep(2)
            continue
        if not isinstance(obs, dict):
            time.sleep(1)
            continue
        if obs.get("done"):
            break
        for pending in obs.get("pending") or []:
            game_num = pending.get("game")
            try:
                act = decide(game_num, pending, seed)
            except Exception as err:
                sys.stderr.write("decide failed game %s: %s\n" % (game_num, err))
                act = fallback(pending)
            try:
                status, _body = _post(env_url + "/act", act)
            except IOError:
                status = 0
            if status == 409:
                continue  # already answered; the game moved on
            if status != 200:
                try:
                    _post(env_url + "/act", fallback(pending))
                except IOError:
                    pass
            decided += 1
    sys.stdout.write("werewolf agent: %d decisions answered, trial done\n" % decided)
    return 0


if __name__ == "__main__":
    sys.exit(main())
