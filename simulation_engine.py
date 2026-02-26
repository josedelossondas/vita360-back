"""
Simulation engine for Vitacura Fleet Routes.
Loads vitacura_fleet_routes.json and advances vehicle state on each tick.
"""
import json
import os
import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)

# ─── Load routes file ───────────────────────────────────────────────────────

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_ROUTES_FILE = os.path.join(_BASE_DIR, "data", "vitacura_fleet_routes.json")

with open(_ROUTES_FILE, "r", encoding="utf-8") as _f:
    _DATA: dict = json.load(_f)

META: dict = _DATA["meta"]
SCHEDULE: dict = _DATA["schedule"]
TICK_MS: int = META["tick_ms"]           # 800 ms
VEHICLES_DEF: list = _DATA["vehicles"]

SUSPECT_SPAWN_TICK: int = SCHEDULE["suspect_spawn_tick"]       # 40
INTERCEPT_START_TICK: int = SCHEDULE["intercept_start_tick"]   # 90
CAPTURE_TICK: int = SCHEDULE["capture_tick"]                   # 130
RESET_TICK: int = SCHEDULE["reset_tick"]                       # 240


# ─── State ───────────────────────────────────────────────────────────────────

tick: int = 0
vehicles_state: dict[str, dict] = {}   # id → state dict
_ws_clients: set = set()               # active WebSocket connections
_task: asyncio.Task | None = None


def _init_vehicle_state(vdef: dict) -> dict:
    mode = vdef.get("mode", "fixed")
    state: dict[str, Any] = {
        "id": vdef["id"],
        "type": vdef["type"],
        "mode": mode,
        "area": vdef.get("area", ""),
        "speed_kmh": vdef.get("speed_kmh", 0),
        "route_index": 0,
        "phase": "patrol",        # patrol | intercept | hold | hidden | moving
        "visible": True,
    }

    if mode == "fixed":
        pos = vdef["hold_position"]
        state["lat"] = pos[0]
        state["lng"] = pos[1]
        state["phase"] = "hold"

    elif mode == "loop":
        route = vdef["patrol_route"]
        state["patrol_route"] = route
        state["lat"] = route[0][0]
        state["lng"] = route[0][1]
        state["phase"] = "patrol"

    elif mode == "loop_then_intercept_then_hold":
        state["patrol_route"] = vdef["patrol_route"]
        state["intercept_route"] = vdef["intercept_route"]
        state["hold_position"] = vdef["hold_position"]
        state["state_labels"] = vdef.get("state_labels", {})
        r = vdef["patrol_route"]
        state["lat"] = r[0][0]
        state["lng"] = r[0][1]
        state["phase"] = "patrol"

    elif mode == "spawn_then_route_then_hold":
        state["route"] = vdef["route"]
        state["hold_position"] = vdef["hold_position"]
        state["state_labels"] = vdef.get("state_labels", {})
        state["spawn_tick"] = vdef.get("spawn_tick", SUSPECT_SPAWN_TICK)
        state["visible"] = False
        state["phase"] = "hidden"
        state["lat"] = vdef["route"][0][0]
        state["lng"] = vdef["route"][0][1]

    return state


def _reset():
    global tick, vehicles_state
    tick = 0
    vehicles_state = {v["id"]: _init_vehicle_state(v) for v in VEHICLES_DEF}


_reset()


# ─── Tick logic ──────────────────────────────────────────────────────────────

def _advance():
    global tick
    tick += 1

    if tick >= RESET_TICK:
        _reset()
        return

    for vid, state in vehicles_state.items():
        mode = state["mode"]

        # ── fixed ──────────────────────────────────────────────────────────
        if mode == "fixed":
            # stays put, status = "detenido"
            state["phase"] = "hold"

        # ── loop ───────────────────────────────────────────────────────────
        elif mode == "loop":
            route = state["patrol_route"]
            idx = state["route_index"]
            state["lat"] = route[idx][0]
            state["lng"] = route[idx][1]
            state["route_index"] = (idx + 1) % len(route)
            state["phase"] = "patrol"

        # ── loop_then_intercept_then_hold ──────────────────────────────────
        elif mode == "loop_then_intercept_then_hold":
            if tick < INTERCEPT_START_TICK:
                # patrol loop
                route = state["patrol_route"]
                idx = state["route_index"]
                state["lat"] = route[idx][0]
                state["lng"] = route[idx][1]
                state["route_index"] = (idx + 1) % len(route)
                state["phase"] = "patrol"
            elif tick < CAPTURE_TICK:
                # intercept: traverse intercept_route once
                i_route = state["intercept_route"]
                if state["phase"] != "intercept":
                    # reset index when phase changes
                    state["route_index"] = 0
                    state["phase"] = "intercept"
                idx = state["route_index"]
                state["lat"] = i_route[idx][0]
                state["lng"] = i_route[idx][1]
                # clamp at end
                if idx < len(i_route) - 1:
                    state["route_index"] = idx + 1
            else:
                # hold
                hp = state["hold_position"]
                state["lat"] = hp[0]
                state["lng"] = hp[1]
                state["phase"] = "hold"

        # ── spawn_then_route_then_hold (suspect) ───────────────────────────
        elif mode == "spawn_then_route_then_hold":
            spawn_tick = state["spawn_tick"]
            if tick < spawn_tick:
                state["visible"] = False
                state["phase"] = "hidden"
            elif tick < CAPTURE_TICK:
                state["visible"] = True
                route = state["route"]
                if state["phase"] == "hidden":
                    state["route_index"] = 0
                    state["phase"] = "moving"
                idx = state["route_index"]
                state["lat"] = route[idx][0]
                state["lng"] = route[idx][1]
                if idx < len(route) - 1:
                    state["route_index"] = idx + 1
            else:
                hp = state["hold_position"]
                state["lat"] = hp[0]
                state["lng"] = hp[1]
                state["visible"] = True
                state["phase"] = "hold"


def _status_label(state: dict) -> str:
    phase = state["phase"]
    labels = state.get("state_labels", {})
    if labels and phase in labels:
        return labels[phase]
    mapping = {
        "patrol": "patrullando",
        "intercept": "acudiendo",
        "hold": "bloqueando" if state["type"] == "patrol" else "neutralizado",
        "hidden": "oculto",
        "moving": "en_movimiento",
        "detenido": "detenido",
    }
    return mapping.get(phase, phase)


def _build_payload() -> dict:
    visible_vehicles = []
    for state in vehicles_state.values():
        if not state["visible"]:
            continue
        visible_vehicles.append({
            "id": state["id"],
            "type": state["type"],
            "lat": state["lat"],
            "lng": state["lng"],
            "status": _status_label(state),
            "speed_kmh": state["speed_kmh"],
            "area": state["area"],
            "phase": state["phase"],
        })
    return {"tick": tick, "vehicles": visible_vehicles}


# ─── Background task ─────────────────────────────────────────────────────────

async def _simulation_loop():
    while True:
        await asyncio.sleep(TICK_MS / 1000)
        _advance()
        payload = _build_payload()
        payload_json = json.dumps(payload)
        dead = set()
        for ws in _ws_clients:
            try:
                await ws.send_text(payload_json)
            except Exception:
                dead.add(ws)
        _ws_clients.difference_update(dead)


def start_simulation(app_loop: asyncio.AbstractEventLoop | None = None):
    global _task
    loop = app_loop or asyncio.get_event_loop()
    if _task is None or _task.done():
        _task = loop.create_task(_simulation_loop())
        logger.info("Simulation engine started (tick_ms=%d)", TICK_MS)


def register_ws(ws):
    _ws_clients.add(ws)


def unregister_ws(ws):
    _ws_clients.discard(ws)


def get_current_state() -> dict:
    """HTTP polling fallback."""
    return _build_payload()
