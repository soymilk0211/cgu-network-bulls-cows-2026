#!/usr/bin/env python3
"""AI experiment (issue #11): compare the v1 and v2 guessing strategies of bc.py.

The games are simulated in-process with the same code the real AI uses
(bc.score, bc.filter_candidates, bc.choose_guess_v1/v2, bc.compute_ranking),
so thousands of turns take minutes instead of hours.  The game rules follow
the server: turn order is random, every alive opponent answers every guess
honestly, a 4A0B answer eliminates that player, and the game ends when one
player is left or after --max-turns turns.

Setups (one "hero" plays the strategy under test):
  solo  the hero breaks one secret alone (the opponent never guesses):
        guesses needed.
  2p    hero against 1 opponent playing v1.
  4p    hero against 3 opponents playing v1.
With the hero on v1 every player is equal, so v1's rows are the control:
about 50 % wins in 2p and 25 % in 4p.  Game i uses the same secrets and turn
order for both strategies (paired comparison).

    python3 tools/ai_experiment.py                       # 100 games per setup and strategy
    python3 tools/ai_experiment.py --games 300 --out ai_experiment_out

Writes results.csv and results.md into --out (default ai_experiment_out/,
ignored by git) and prints the Markdown table.  Standard library only.
"""

import argparse
import csv
import os
import platform
import random
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import bc  # noqa: E402

SETUPS = {"solo": 1, "2p": 2, "4p": 4}  # setup -> number of players (solo: no opponent guesses)
COLUMNS = ["setup", "players", "strategy", "games", "wins", "win_rate", "avg_guesses", "max_guesses",
           "avg_turns", "avg_decision_ms", "max_decision_ms", "decisions", "v1_fallbacks"]


def decide(strategy, cands, budget, rng, record):
    """One decision of a player, timed.  `record` is None for the opponents."""
    stats = {}
    start = time.perf_counter()
    if strategy == "v2":
        guess = bc.choose_guess_v2(cands, budget, rng, stats=stats)
    else:
        guess = bc.choose_guess_v1(cands, budget, rng)
    if record is not None:
        record["times"].append(time.perf_counter() - start)
        record["fallbacks"] += 1 if stats.get("fallback") else 0
    return guess


def play_solo(strategy, game_rng, play_rng, budget, record, max_guesses=20):
    """The hero breaks one random secret.  Returns the number of guesses."""
    secret = game_rng.choice(bc.ALL_NUMBERS)
    cands = list(bc.ALL_NUMBERS)
    for n in range(1, max_guesses + 1):
        guess = decide(strategy, {"p2": cands}, budget, play_rng, record)
        a, b = bc.score(guess, secret)
        if (a, b) == (4, 0):
            return n
        cands = bc.filter_candidates(cands, guess, a, b)
    return max_guesses


def play_game(strategies, game_rng, play_rng, budget, record, max_turns):
    """One game; strategies[0] is the hero.  Returns (hero rank, hero guesses, turns)."""
    pids = [f"p{i + 1}" for i in range(len(strategies))]
    strategy = dict(zip(pids, strategies))
    secret = {pid: game_rng.choice(bc.ALL_NUMBERS) for pid in pids}
    order = list(pids)
    game_rng.shuffle(order)
    cands = {pid: list(bc.ALL_NUMBERS) for pid in pids}  # public: the same for every observer
    alive, out_turn = list(order), {}
    guesses, turn, last = 0, 0, None
    while len(alive) > 1 and turn < max_turns:
        turn += 1
        start = 0 if last is None else order.index(last) + 1  # next alive player (§12.3.1)
        guesser = next(order[(start + i) % len(order)] for i in range(len(order))
                       if order[(start + i) % len(order)] in alive)
        last = guesser
        view = {pid: cands[pid] for pid in alive if pid != guesser}  # like PlayerSession.cands
        hero = guesser == "p1"
        guess = decide(strategy[guesser], view, budget, play_rng, record if hero else None)
        guesses += 1 if hero else 0
        eliminated = []
        for pid in [p for p in alive if p != guesser]:
            a, b = bc.score(guess, secret[pid])
            cands[pid] = bc.filter_candidates(cands[pid], guess, a, b)
            if (a, b) == (4, 0):
                eliminated.append(pid)
        for pid in eliminated:
            alive.remove(pid)
            out_turn[pid] = turn
    entries = [(pid, "SURVIVOR" if pid in alive else "GUESSED", out_turn.get(pid)) for pid in order]
    rank = {row["player"]: row["rank"] for row in bc.compute_ranking(entries)}
    return rank["p1"], guesses, turn


def run_cell(setup, strategy, games, seed, budget, max_turns):
    """All games of one (setup, strategy) cell.  Returns one result row."""
    record = {"times": [], "fallbacks": 0}
    wins, guesses, turns = 0, [], []
    for i in range(games):
        game_rng = random.Random(f"{seed}-{setup}-{i}")  # same secrets/order for v1 and v2
        play_rng = random.Random(f"{seed}-{setup}-{i}-play")
        if setup == "solo":
            n = play_solo(strategy, game_rng, play_rng, budget, record)
            guesses.append(n)
            turns.append(n)
            wins += 1
        else:
            others = ["v1"] * (SETUPS[setup] - 1)
            rank, n, t = play_game([strategy] + others, game_rng, play_rng, budget, record, max_turns)
            wins += rank == 1
            guesses.append(n)
            turns.append(t)
    times = record["times"] or [0.0]
    return {"setup": setup, "players": SETUPS[setup], "strategy": strategy, "games": games, "wins": wins,
            "win_rate": round(wins / games, 4), "avg_guesses": round(sum(guesses) / games, 3),
            "max_guesses": max(guesses), "avg_turns": round(sum(turns) / games, 2),
            "avg_decision_ms": round(1000 * sum(times) / len(times), 1),
            "max_decision_ms": round(1000 * max(times), 1), "decisions": len(record["times"]),
            "v1_fallbacks": record["fallbacks"]}


def markdown(rows, args):
    lines = ["| setup | strategy | games | win rate | avg guesses (hero) | max guesses | avg turns "
             "| avg decision | max decision | v1 fallbacks |",
             "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for r in rows:
        win = "-" if r["setup"] == "solo" else f"{100 * r['win_rate']:.1f}% ({r['wins']})"
        lines.append(f"| {r['setup']} | {r['strategy']} | {r['games']} | {win} | {r['avg_guesses']:.2f} "
                     f"| {r['max_guesses']} | {r['avg_turns']:.1f} | {r['avg_decision_ms']:.1f} ms "
                     f"| {r['max_decision_ms']:.1f} ms | {r['v1_fallbacks']} |")
    lines += ["", f"Setup: in-process simulation with bc.py's own code; seed {args.seed}; time budget "
                  f"{args.budget:g} s per decision; max {args.max_turns} turns; Python "
                  f"{platform.python_version()} on {platform.system()}.",
              "solo = the hero breaks one secret alone (guesses needed). 2p / 4p = the hero against 1 / 3 "
              "opponents that play v1; win = rank 1. With the hero on v1 all players are equal, so v1's "
              "rows are the control (about 50 % / 25 %). Game i has the same secrets and turn order for "
              "both strategies. avg guesses (hero) = guesses the hero made per game. v1 fallbacks = v2 "
              "decisions that ran out of time and were made by v1."]
    return "\n".join(lines)


def main(argv=None):
    p = argparse.ArgumentParser(prog="ai_experiment.py", description="Compare AI strategies v1 and v2.")
    p.add_argument("--games", type=int, default=100,
                   help="games per setup and strategy (default: %(default)s)")
    p.add_argument("--setups", default="solo,2p,4p", help="comma-separated list (default: %(default)s)")
    p.add_argument("--strategies", default="v1,v2", help="comma-separated list (default: %(default)s)")
    p.add_argument("--seed", default="2026", help="seed for secrets, turn order and sampling")
    p.add_argument("--budget", type=float, default=bc.AI_TIME_BUDGET,
                   help="seconds per decision, as in the real AI (default: %(default)s)")
    p.add_argument("--max-turns", type=int, default=120, help="(default: %(default)s)")
    p.add_argument("--out", default="ai_experiment_out", help="output folder (default: %(default)s)")
    args = p.parse_args(argv)
    setups = [s for s in args.setups.split(",") if s]
    strategies = [s for s in args.strategies.split(",") if s]
    bad = [s for s in setups if s not in SETUPS] + [s for s in strategies if s not in bc.AI_STRATEGIES]
    if bad or args.games < 1:
        print(f"error: unknown setup/strategy {bad} or --games < 1")
        return 2

    rows = []
    for setup in setups:
        for strategy in strategies:
            start = time.monotonic()
            rows.append(run_cell(setup, strategy, args.games, args.seed, args.budget, args.max_turns))
            r = rows[-1]
            print(f"{setup:>4} {strategy}: {r['games']} games, win rate {r['win_rate']:.3f}, avg guesses "
                  f"{r['avg_guesses']}, max decision {r['max_decision_ms']} ms "
                  f"({time.monotonic() - start:.0f} s)", flush=True)

    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "results.csv"), "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    text = markdown(rows, args)
    with open(os.path.join(args.out, "results.md"), "w", encoding="utf-8") as f:
        f.write(text + "\n")
    print("\n" + text)
    print(f"\nwritten: {os.path.join(args.out, 'results.csv')}, {os.path.join(args.out, 'results.md')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
