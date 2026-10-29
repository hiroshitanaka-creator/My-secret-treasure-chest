"""World Engine: world state, rules, and the only code path that changes state.

All quantities are integers so that a turn is fully deterministic and its
result can be hashed and replayed (reproducibility class A).
"""

from __future__ import annotations

import copy
import hashlib
import json
import random
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field, fields
from typing import Any

from civlab.actions import RESOURCES, Action, ActionType

MEMORY_LIMIT = 5


class WorldInvariantError(RuntimeError):
    """Raised when a state violates a world invariant (engine bug or corrupt data)."""


@dataclass(frozen=True)
class WorldRules:
    gather_yield: int = 3
    pool_regen: int = 4
    pool_capacity: int = 40
    consume_relief: int = 3
    need_threshold: int = 4
    need_damage: int = 1
    rest_heal: int = 1
    max_health: int = 5
    max_share: int = 5
    initial_pool: int = 20

    def __post_init__(self) -> None:
        for f in fields(self):
            value = getattr(self, f.name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"rule {f.name} must be a non-negative integer")
        if self.max_health < 1 or self.max_share < 1 or self.need_threshold < 1:
            raise ValueError("max_health, max_share and need_threshold must be >= 1")
        if self.initial_pool > self.pool_capacity:
            raise ValueError("initial_pool must not exceed pool_capacity")

    def to_dict(self) -> dict[str, int]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> WorldRules:
        known = {f.name for f in fields(cls)}
        unknown = set(data) - known
        if unknown:
            raise ValueError(f"unknown world rules: {sorted(unknown)}")
        return cls(**dict(data))


@dataclass
class Resident:
    resident_id: str
    name: str
    role: str
    personality: str
    food: int
    water: int
    hunger: int
    thirst: int
    health: int
    alive: bool = True
    memory: list[str] = field(default_factory=list)

    def inventory(self, resource: str) -> int:
        return self.food if resource == "food" else self.water

    def add(self, resource: str, delta: int) -> None:
        if resource == "food":
            self.food += delta
        else:
            self.water += delta

    def remember(self, event: str) -> None:
        self.memory.append(event)
        del self.memory[:-MEMORY_LIMIT]


@dataclass
class WorldState:
    turn: int
    pools: dict[str, int]
    residents: dict[str, Resident]

    def to_dict(self) -> dict[str, Any]:
        return {
            "turn": self.turn,
            "pools": {r: self.pools[r] for r in RESOURCES},
            "residents": [asdict(self.residents[rid]) for rid in sorted(self.residents)],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> WorldState:
        residents: dict[str, Resident] = {}
        for item in data["residents"]:
            resident = Resident(**item)
            if resident.resident_id in residents:
                raise WorldInvariantError(f"duplicate resident id: {resident.resident_id}")
            residents[resident.resident_id] = resident
        pools = dict(data["pools"])
        if set(pools) != set(RESOURCES):
            raise WorldInvariantError("pools must contain exactly the known resources")
        return cls(turn=int(data["turn"]), pools=pools, residents=residents)

    def clone(self) -> WorldState:
        return copy.deepcopy(self)

    def living_ids(self) -> list[str]:
        return [rid for rid in sorted(self.residents) if self.residents[rid].alive]


def canonical_json(data: Any) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def state_hash(state: WorldState) -> str:
    return hashlib.sha256(canonical_json(state.to_dict()).encode("ascii")).hexdigest()


def check_invariants(state: WorldState, rules: WorldRules) -> None:
    for resource, amount in state.pools.items():
        if amount < 0 or amount > rules.pool_capacity:
            raise WorldInvariantError(f"pool {resource} out of range: {amount}")
    for r in state.residents.values():
        for name in ("food", "water", "hunger", "thirst", "health"):
            if getattr(r, name) < 0:
                raise WorldInvariantError(f"{r.resident_id}.{name} is negative")
        if r.health > rules.max_health:
            raise WorldInvariantError(f"{r.resident_id}.health above max")
        if r.alive != (r.health > 0):
            raise WorldInvariantError(f"{r.resident_id}.alive inconsistent with health")


@dataclass(frozen=True)
class ActionOutcome:
    resident_id: str
    status: str  # "applied" | "rejected_rule" | "no_action"
    reason: str
    effects: dict[str, int]

    def to_dict(self) -> dict[str, Any]:
        return {
            "resident_id": self.resident_id,
            "status": self.status,
            "reason": self.reason,
            "effects": dict(sorted(self.effects.items())),
        }


@dataclass(frozen=True)
class StepResult:
    state: WorldState
    outcomes: dict[str, ActionOutcome]
    deaths: list[str]


_NAMES = ("Aoi", "Ren", "Mio", "Sora", "Haru", "Yui", "Kai", "Rin", "Nao", "Emi", "Taro", "Hana")
_ROLES = ("gatherer", "water_carrier", "caretaker", "elder")
_PERSONALITIES = ("generous", "cautious", "balanced", "self_reliant")


def make_initial_state(num_residents: int, seed: int, rules: WorldRules) -> WorldState:
    """Create a deterministic initial world from a seed."""
    if not 1 <= num_residents <= 99:
        raise ValueError("num_residents must be between 1 and 99")
    rng = random.Random(seed)
    residents: dict[str, Resident] = {}
    for i in range(num_residents):
        rid = f"r{i + 1:02d}"
        residents[rid] = Resident(
            resident_id=rid,
            name=_NAMES[i % len(_NAMES)] + ("" if i < len(_NAMES) else str(i)),
            role=_ROLES[i % len(_ROLES)],
            personality=rng.choice(_PERSONALITIES),
            food=rng.randint(1, 4),
            water=rng.randint(1, 4),
            hunger=rng.randint(0, 2),
            thirst=rng.randint(0, 2),
            health=rules.max_health,
        )
    state = WorldState(
        turn=0,
        pools={r: rules.initial_pool for r in RESOURCES},
        residents=residents,
    )
    check_invariants(state, rules)
    return state


def turn_order(state: WorldState) -> list[str]:
    """Living residents in id order, rotated each turn so no one always acts first."""
    ids = state.living_ids()
    if not ids:
        return ids
    k = state.turn % len(ids)
    return ids[k:] + ids[:k]


class WorldEngine:
    """Applies validated actions to a state according to ``WorldRules``."""

    def __init__(self, rules: WorldRules) -> None:
        self.rules = rules

    def step(self, state: WorldState, actions: Mapping[str, Action | None]) -> StepResult:
        """Return the next state. ``state`` is not modified.

        ``actions`` maps resident id to a validated action, or ``None`` when the
        resident produced no usable decision. Residents missing from the mapping
        are treated as ``None``.
        """
        check_invariants(state, self.rules)
        unknown = set(actions) - set(state.residents)
        if unknown:
            raise WorldInvariantError(f"actions for unknown residents: {sorted(unknown)}")

        nxt = state.clone()
        outcomes: dict[str, ActionOutcome] = {}
        for rid in sorted(nxt.residents):
            if not nxt.residents[rid].alive and actions.get(rid) is not None:
                outcomes[rid] = ActionOutcome(rid, "rejected_rule", "actor_dead", {})
        for rid in turn_order(nxt):
            outcome = self._apply(nxt, rid, actions.get(rid))
            outcomes[rid] = outcome
            nxt.residents[rid].remember(f"t{state.turn}:{outcome.status}:{outcome.reason}")

        deaths = self._upkeep(nxt)
        for resource in RESOURCES:
            nxt.pools[resource] = min(
                self.rules.pool_capacity, nxt.pools[resource] + self.rules.pool_regen
            )
        nxt.turn = state.turn + 1
        check_invariants(nxt, self.rules)
        return StepResult(nxt, dict(sorted(outcomes.items())), deaths)

    def _apply(self, s: WorldState, rid: str, action: Action | None) -> ActionOutcome:
        me = s.residents[rid]
        rules = self.rules
        if action is None:
            return ActionOutcome(rid, "no_action", "no_valid_decision", {})

        def reject(reason: str) -> ActionOutcome:
            return ActionOutcome(rid, "rejected_rule", reason, {})

        t = action.type
        if t in (ActionType.GATHER_FOOD, ActionType.GATHER_WATER):
            resource = "food" if t is ActionType.GATHER_FOOD else "water"
            taken = min(rules.gather_yield, s.pools[resource])
            if taken == 0:
                return reject("pool_empty")
            s.pools[resource] -= taken
            me.add(resource, taken)
            return ActionOutcome(
                rid, "applied", "gathered", {f"pool.{resource}": -taken, f"{rid}.{resource}": taken}
            )
        if t in (ActionType.EAT, ActionType.DRINK):
            resource, need = ("food", "hunger") if t is ActionType.EAT else ("water", "thirst")
            if me.inventory(resource) < 1:
                return reject(f"no_{resource}")
            before = getattr(me, need)
            after = max(0, before - rules.consume_relief)
            me.add(resource, -1)
            setattr(me, need, after)
            return ActionOutcome(
                rid,
                "applied",
                "consumed",
                {f"{rid}.{resource}": -1, f"{rid}.{need}": after - before},
            )
        if t is ActionType.SHARE:
            if action.target is None or action.amount is None or action.resource not in RESOURCES:
                return reject("malformed_share")
            target = s.residents.get(action.target)
            if target is None:
                return reject("unknown_target")
            if target.resident_id == rid:
                return reject("self_target")
            if not target.alive:
                return reject("target_dead")
            if not 1 <= action.amount <= rules.max_share:
                return reject("amount_out_of_range")
            if me.inventory(action.resource) < action.amount:
                return reject(f"insufficient_{action.resource}")
            me.add(action.resource, -action.amount)
            target.add(action.resource, action.amount)
            target.remember(f"t{s.turn}:received:{action.resource}:{action.amount}:from:{rid}")
            return ActionOutcome(
                rid,
                "applied",
                "shared",
                {
                    f"{rid}.{action.resource}": -action.amount,
                    f"{target.resident_id}.{action.resource}": action.amount,
                },
            )
        if t is ActionType.REST:
            if me.hunger >= rules.need_threshold or me.thirst >= rules.need_threshold:
                return ActionOutcome(rid, "applied", "rested_no_heal", {})
            healed = min(rules.max_health, me.health + rules.rest_heal) - me.health
            me.health += healed
            return ActionOutcome(rid, "applied", "rested", {f"{rid}.health": healed})
        raise WorldInvariantError(f"unhandled action type: {t}")  # pragma: no cover

    def _upkeep(self, s: WorldState) -> list[str]:
        deaths: list[str] = []
        for rid in s.living_ids():
            r = s.residents[rid]
            r.hunger += 1
            r.thirst += 1
            damage = 0
            if r.hunger >= self.rules.need_threshold:
                damage += self.rules.need_damage
            if r.thirst >= self.rules.need_threshold:
                damage += self.rules.need_damage
            r.health = max(0, r.health - damage)
            if r.health == 0:
                r.alive = False
                deaths.append(rid)
        return deaths
