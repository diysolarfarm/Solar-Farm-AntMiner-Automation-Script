#!/usr/bin/env python3
"""
 vnish_soc_controller.py ▸ Antminer / VNish – start‑stop on battery SoC
 ----------------------------------------------------------------------
 * Reads the current battery State‑of‑Charge from Home‑Assistant.
 * Starts or stops each listed miner according to per‑rig stop / resume
   thresholds (hysteresis).
 * Works with **all** VNish firmware builds tested so far (≥ v1.1):
     ‑ Authenticates once via /api/v1/unlock and keeps the session token.
     ‑ Detects whether the rig is hashing by looking at any realtime
       hashrate key if explicit flags are absent.
 * Robust to token expiry (re‑auth 1× on HTTP 401) and to firmware trees
   that moved the status endpoint from /status → /summary.

 Example one‑off run:
   export HA_TOKEN="<home‑assistant long‑lived token>"
   ./vnish_soc_controller.py \
       --ha-url  http://192.168.88.239:8123 \
       --sensor  sensor.solis_s6_eh1p_battery_soc \
       --config  ./miners.json               \
       --poll    60

 For 24 × 7 operation wrap it in tmux or a small systemd service.
"""

from __future__ import annotations
import argparse, json, os, sys, time, requests
from pathlib import Path
from ipaddress import ip_address
from typing import Dict, List

# ────────────────────── CLI ──────────────────────

def parse_args():
    p = argparse.ArgumentParser(description="Start / stop VNish miners based on battery SoC")
    p.add_argument("--ha-url",   required=True, help="Base URL of Home Assistant")
    p.add_argument("--sensor",   required=True, help="Entity ID of the SoC sensor")
    p.add_argument("--config",   required=True, help="Path to miners.json")
    p.add_argument("--poll",     type=int, default=60, help="Polling interval in seconds")
    p.add_argument("--ha-token", help="HA long‑lived token (else env HA_TOKEN)")
    return p.parse_args()

# ────────────────────── Home Assistant ──────────────────────

def get_soc(ha_url: str, token: str, entity: str) -> float:
    """Return the battery SoC percentage as float."""
    url = f"{ha_url.rstrip('/')}/api/states/{entity}"
    r = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=5)
    r.raise_for_status()
    return float(r.json()["state"])

# ────────────────────── Miner helper ──────────────────────

class Miner:
    PATH_UNLOCK = "/api/v1/unlock"     # POST {"pw": "…"}
    PATH_START  = "/api/v1/mining/start"
    PATH_STOP   = "/api/v1/mining/stop"

    def __init__(self, d: Dict):
        self.ip           = str(ip_address(d["ip"]))
        self.pw           = d.get("password", "admin")
        self.stop_soc     = d["stop_soc"]
        self.resume_soc   = d["resume_soc"]
        self.token: str | None = None

    # ── internal helpers ──
    def url(self, path: str) -> str:
        return f"http://{self.ip}{path}"

    # ── authentication ──
    def refresh_token(self):
        """Unlock web GUI and capture the session token."""
        r = requests.post(self.url(self.PATH_UNLOCK), json={"pw": self.pw}, timeout=5)
        r.raise_for_status()
        js = r.json()
        self.token = js.get("token") or js.get("access_token")
        if not self.token:
            raise RuntimeError(f"{self.ip}: unlock response lacked token field")

    def auth_header(self, *, bearer: bool = False) -> Dict[str, str]:
        """Return proper Authorization header for this firmware build."""
        if not self.token:
            self.refresh_token()
        return {"Authorization": f"Bearer {self.token}" if bearer else self.token}

    # ── status helpers ──
    def _stats(self) -> dict:
        """Return miner status JSON – tries /status then /summary."""
        for path in ("/api/v1/status", "/api/v1/summary"):
            r = requests.get(self.url(path), headers=self.auth_header(), timeout=5)
            if r.status_code == 401:   # token expired – refresh once then retry
                self.refresh_token()
                r = requests.get(self.url(path), headers=self.auth_header(), timeout=5)
            if r.ok:
                return r.json()
            if r.status_code != 404:   # a genuine error, not just the wrong path
                r.raise_for_status()
        raise RuntimeError("no status endpoint found on miner")

    def is_hashing(self) -> bool:
        """True if the rig is currently hashing (detected robustly)."""
        js = self._stats()

        # 1) explicit flags (older builds)
        for flag in ("is_mining", "mining"):
            if flag in js:
                try:
                    return bool(int(js[flag]))  # handles 0/1 or False/True
                except (TypeError, ValueError):
                    return bool(js[flag])

        # 2) miner_state string (mixed builds)
        state = str(js.get("miner_state", "")).lower()
        if state in {"running", "hashing", "mining"}:
            return True

        # 3) any realtime hashrate key (current builds)
        for k in ("hr_realtime", "instant_hashrate", "hashrate"):
            try:
                if float(js.get(k, 0)) > 0:
                    return True
            except (TypeError, ValueError):
                pass
        return False

    # ── start / stop ──
    def set_hashing(self, start: bool):
        """Send POST /mining/start or /mining/stop. Ignores 500 = already there."""
        path = self.PATH_START if start else self.PATH_STOP
        hdrs = {"Content-Type": "application/json", **self.auth_header()}  # raw token
        r = requests.post(self.url(path), headers=hdrs, json={}, timeout=5)
        if r.status_code == 401:
            self.refresh_token()
            hdrs = {"Content-Type": "application/json", **self.auth_header()}
            r = requests.post(self.url(path), headers=hdrs, json={}, timeout=5)
        if r.status_code in (200, 204, 500):   # 500 = already in requested state
            return
        r.raise_for_status()

# ────────────────────── utilities ──────────────────────

def load_miners(path: Path) -> List[Miner]:
    """Read miners.json and return a list of Miner objects."""
    return [Miner(m) for m in json.loads(path.read_text())]

# ────────────────────── control loop ──────────────────────

def control_cycle(miners: List[Miner], soc: float):
    """One pass over all miners – enforce desired state from SoC thresholds."""
    for m in miners:
        try:
            active = m.is_hashing()
        except Exception as e:
            print(f"[{m.ip}] ERROR status → {e}")
            continue

        # decide what we *want* given the current SoC
        if soc < m.stop_soc:
            desired = False   # stop hashing
        elif soc > m.resume_soc:
            desired = True    # start hashing
        else:
            desired = None    # leave it as‑is (inside hysteresis band)

        if desired is None or desired == active:
            # nothing to do
            continue

        try:
            m.set_hashing(desired)
            verb = "started" if desired else "stopped"
            print(f"[{m.ip}] SOC {soc:.1f}% → mining {verb}")
        except Exception as e:
            print(f"[{m.ip}] ERROR set_hashing → {e}")

# ────────────────────── entry point ──────────────────────

def main():
    args = parse_args()
    ha_token = args.ha_token or os.getenv("HA_TOKEN")
    if not ha_token:
        sys.exit("Missing Home‑Assistant token (use --ha-token or HA_TOKEN env)")

    miners = load_miners(Path(args.config))
    print(f"Loaded {len(miners)} miners from {args.config}. Poll {args.poll}s")

    while True:
        try:
            soc = get_soc(args.ha_url, ha_token, args.sensor)
            print(f"Battery SoC {soc:.1f}%", end="\r", flush=True)
        except Exception as e:
            print(f"HA ERROR → {e}")
            time.sleep(args.poll)
            continue

        control_cycle(miners, soc)
        time.sleep(args.poll)

if __name__ == "__main__":
    main()

