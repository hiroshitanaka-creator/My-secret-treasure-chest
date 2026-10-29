from __future__ import annotations

import random

import pytest

from civlab.actions import RESOURCES, Action, ActionType
from civlab.world import (
    Resident,
    WorldEngine,
    WorldInvariantError,
    WorldRules,
    WorldState,
    check_invariants,
    make_initial_state,
    state_hash,
    turn_order,
)

RULES = WorldRules()


def resident(rid: str, **kw: object) -> Resident:
    base: dict[str, object] = dict(
        resident_id=rid,
        name=rid,
        role="gatherer",
        personality="balanced",
        food=2,
        water=2,
        hunger=0,
        thirst=0,
        health=5,
    )
    base.update(kw)
    return Resident(**base)  # type: ignore[arg-type]


def world(*residents: Resident, food: int = 10, water: int = 10) -> WorldState:
    return WorldState(0, {"food": food, "water": water}, {r.resident_id: r for r in residents})


def step(state: WorldState, **actions: Action | None):
    return WorldEngine(RULES).step(state, actions)


def test_initial_state_has_unique_ids_and_is_deterministic() -> None:
    a = make_initial_state(6, 42, RULES)
    b = make_initial_state(6, 42, RULES)
    assert sorted(a.residents) == ["r01", "r02", "r03", "r04", "r05", "r06"]
    assert len({r.resident_id for r in a.residents.values()}) == 6
    assert state_hash(a) == state_hash(b)
    assert state_hash(a) != state_hash(make_initial_state(6, 43, RULES))


def test_duplicate_resident_ids_are_rejected() -> None:
    data = world(resident("r01")).to_dict()
    data["residents"].append(dict(data["residents"][0]))
    with pytest.raises(WorldInvariantError, match="duplicate resident id"):
        WorldState.from_dict(data)


def test_roundtrip_preserves_hash() -> None:
    s = make_initial_state(6, 1, RULES)
    assert state_hash(WorldState.from_dict(s.to_dict())) == state_hash(s)


def test_gather_moves_resource_from_pool() -> None:
    r = step(world(resident("r01")), r01=Action(ActionType.GATHER_FOOD))
    assert r.outcomes["r01"].status == "applied"
    assert r.state.residents["r01"].food == 2 + RULES.gather_yield
    # pool: 10 - yield, then regen (capped)
    assert r.state.pools["food"] == min(
        RULES.pool_capacity, 10 - RULES.gather_yield + RULES.pool_regen
    )


def test_gather_takes_at_most_what_the_pool_has() -> None:
    r = step(world(resident("r01"), food=1), r01=Action(ActionType.GATHER_FOOD))
    assert r.state.residents["r01"].food == 3
    assert r.outcomes["r01"].effects == {"pool.food": -1, "r01.food": 1}


def test_gather_from_empty_pool_is_rejected() -> None:
    r = step(world(resident("r01"), water=0), r01=Action(ActionType.GATHER_WATER))
    assert (r.outcomes["r01"].status, r.outcomes["r01"].reason) == ("rejected_rule", "pool_empty")
    assert r.state.residents["r01"].water == 2


def test_eat_consumes_food_and_relieves_hunger() -> None:
    r = step(world(resident("r01", hunger=3)), r01=Action(ActionType.EAT))
    me = r.state.residents["r01"]
    assert me.food == 1
    assert me.hunger == 0 + 1  # relieved to 0, then +1 upkeep


def test_eat_without_food_is_rejected_and_nothing_goes_negative() -> None:
    r = step(world(resident("r01", food=0)), r01=Action(ActionType.EAT))
    assert r.outcomes["r01"].reason == "no_food"
    assert r.state.residents["r01"].food == 0


def test_share_transfers_exact_amount() -> None:
    s = world(resident("r01", water=4), resident("r02", water=0))
    r = step(s, r01=Action(ActionType.SHARE, target="r02", resource="water", amount=3))
    assert r.state.residents["r01"].water == 1
    assert r.state.residents["r02"].water == 3


@pytest.mark.parametrize(
    ("action", "reason"),
    [
        (Action(ActionType.SHARE, target="r02", resource="water", amount=5), "insufficient_water"),
        (Action(ActionType.SHARE, target="r01", resource="water", amount=1), "self_target"),
        (Action(ActionType.SHARE, target="r09", resource="water", amount=1), "unknown_target"),
        (Action(ActionType.SHARE, target="r03", resource="water", amount=1), "target_dead"),
        (Action(ActionType.SHARE, target="r02", resource="water", amount=0), "amount_out_of_range"),
        (Action(ActionType.SHARE, target="r02", resource=None, amount=1), "malformed_share"),
    ],
)
def test_invalid_share_is_rejected_without_state_change(action: Action, reason: str) -> None:
    s = world(resident("r01"), resident("r02"), resident("r03", health=0, alive=False))
    before = s.to_dict()
    r = step(s, r01=action)
    assert (r.outcomes["r01"].status, r.outcomes["r01"].reason) == ("rejected_rule", reason)
    assert s.to_dict() == before  # input state untouched
    assert r.state.residents["r01"].water == 2
    assert r.state.residents["r02"].water == 2


def test_dead_resident_cannot_act() -> None:
    s = world(resident("r01"), resident("r02", health=0, alive=False))
    r = step(s, r02=Action(ActionType.GATHER_FOOD))
    assert r.outcomes["r02"].reason == "actor_dead"
    assert r.state.residents["r02"].food == 2


def test_unmet_needs_damage_health_and_can_kill() -> None:
    s = world(resident("r01", hunger=RULES.need_threshold, thirst=RULES.need_threshold, health=2))
    r = step(s, r01=None)
    assert r.state.residents["r01"].health == 0
    assert r.state.residents["r01"].alive is False
    assert r.deaths == ["r01"]


def test_rest_heals_only_when_needs_are_met() -> None:
    r = step(world(resident("r01", health=3)), r01=Action(ActionType.REST))
    assert r.state.residents["r01"].health == 4
    r = step(
        world(resident("r01", health=3, hunger=RULES.need_threshold)), r01=Action(ActionType.REST)
    )
    assert r.outcomes["r01"].reason == "rested_no_heal"


def test_actions_for_unknown_residents_are_an_error() -> None:
    with pytest.raises(WorldInvariantError):
        step(world(resident("r01")), r99=Action(ActionType.REST))


def test_invariant_check_detects_negative_values() -> None:
    s = world(resident("r01", food=-1))
    with pytest.raises(WorldInvariantError, match="negative"):
        check_invariants(s, RULES)


def test_turn_order_rotates() -> None:
    s = world(resident("r01"), resident("r02"), resident("r03"))
    assert turn_order(s) == ["r01", "r02", "r03"]
    s.turn = 1
    assert turn_order(s) == ["r02", "r03", "r01"]


def _random_action(rng: random.Random, ids: list[str]) -> Action | None:
    kind = rng.choice([*ActionType, None])
    if kind is ActionType.SHARE:
        return Action(
            kind, target=rng.choice(ids), resource=rng.choice(RESOURCES), amount=rng.randint(0, 6)
        )
    return None if kind is None else Action(kind)


@pytest.mark.parametrize("seed", range(40))
def test_random_sequences_keep_invariants_and_resource_ledger(seed: int) -> None:
    """Fuzz: arbitrary (even infeasible) actions never break invariants, and every
    unit of food/water is accounted for by recorded effects plus pool regeneration."""
    rng = random.Random(seed)
    engine = WorldEngine(RULES)
    state = make_initial_state(6, seed, RULES)
    for _ in range(25):
        ids = sorted(state.residents)
        actions = {rid: _random_action(rng, ids) for rid in state.living_ids()}
        result = engine.step(state, actions)
        nxt = result.state
        check_invariants(nxt, RULES)
        for res in RESOURCES:
            pool_mid = state.pools[res] + sum(
                o.effects.get(f"pool.{res}", 0) for o in result.outcomes.values()
            )
            regen = nxt.pools[res] - pool_mid
            assert 0 <= regen <= RULES.pool_regen
            for rid, r in nxt.residents.items():
                delta = sum(o.effects.get(f"{rid}.{res}", 0) for o in result.outcomes.values())
                assert r.inventory(res) == state.residents[rid].inventory(res) + delta
        for o in result.outcomes.values():
            if o.status != "applied":
                assert o.effects == {}
        state = nxt
