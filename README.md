# Solar-Farm-Automation-Script
Python 3 program that regulates Antminer S19-series rigs running VNish firmware
The S19 Solar Farm Automation Script is a Python 3 program that regulates Antminer S19-series rigs running VNish firmware in an off-grid or hybrid solar battery installation. It saves power by pausing or resuming hashing according to the live state of charge (SoC) of the battery, which it retrieves from Home Assistant (HA). All communication is done through HTTP APIs, so no additional libraries are required beyond requests.
Main Goals

    Prevent battery deep-discharge by stopping miners when SoC falls below a per-miner stop threshold.

    Maximize uptime by automatically restarting each miner when SoC climbs above its resume threshold.

    Maintain secure, unattended operation using VNish’s built-in authentication tokens and a Home Assistant long-lived token.

Architecture and Files
| File                          | Purpose                                                                                                  |
| ----------------------------- | -------------------------------------------------------------------------------------------------------- |
| **`vnish_soc_controller.py`** | Core script (≈ 300 lines) containing CLI parsing, the polling loop, miner abstractions and SoC handling. |
| **`miners.json`**             | User editable array describing each rig: IP address, Web-GUI password, stop\_soc, resume\_soc.           |

| Argument     | Meaning                                                       |
| ------------ | ------------------------------------------------------------- |
| `--ha-url`   | Base URL of the Home Assistant instance.                      |
| `--sensor`   | Entity ID of the battery SoC sensor in HA.                    |
| `--config`   | Path to `miners.json`.                                        |
| `--poll`     | Polling interval in seconds (default 60).                     |
| `--ha-token` | HA token, optional if environment variable `HA_TOKEN` is set. |

Internal Classes and Functions
Miner

    Wraps all API interactions for a single device:

        refresh_token() unlocks the Web-GUI and caches the session token.

        is_hashing() checks whether the unit is actively mining. It tries /api/v1/status and falls back to /api/v1/summary, then inspects flags or real-time hashrate metrics.

        set_hashing(start: bool) issues /mining/start or /mining/stop with automatic token refresh on HTTP 401.

get_soc()

Queries Home Assistant’s REST endpoint /api/states/<entity> and returns the battery SoC as float.
control_cycle()

Runs once per poll:

    Calls is_hashing() for each miner.

    Compares SoC with the miner’s thresholds.

    Decides whether to start or stop hashing.

    Logs the action or any error in one concise line.

Decision Logic (Hysteresis)

    Stop rule: SoC < stop_soc and miner currently hashing.

    Resume rule: SoC > resume_soc and miner currently idle.

    Independent thresholds allow priority rigs with different values.

Robustness Features

    Token management: Automatic unlock at startup and retry on token expiry.

    Endpoint fallback: Works with both /status and /summary firmware trees.

    Hashrate-based detection: Considers the miner active if any real-time hashrate key reports a value above zero, covering all VNish 1.2 builds.

    Graceful HTTP handling:

        Retries unlock once on 401.

        Treats 500 on start/stop as “already in desired state”.

        Catches unexpected exceptions and keeps the main loop running.

Logging Behavior

    The current battery SoC is printed in place every poll using carriage-return updates.

    A new line is printed only when a miner changes state or an error occurs, for clear and compact logs.

    Example output:
    Battery SoC 78.4%
[192.168.88.20] SOC 78.0% → mining stopped

Sample Configuration (miners.json)
[
  { "ip": "192.168.88.20", "password": "admin", "stop_soc": 73, "resume_soc": 75 },
  { "ip": "192.168.88.21", "password": "admin", "stop_soc": 95, "resume_soc": 99 },
  { "ip": "192.168.88.22", "password": "admin", "stop_soc": 95, "resume_soc": 99 }
]Each miner can have distinct thresholds, letting you assign priority to certain units.


Typical Usage
export HA_TOKEN="your-long-lived-ha-token"
python3 vnish_soc_controller.py \
    --ha-url http://192.168.88.239:8123 \
    --sensor sensor.solis_s6_eh1p_battery_soc \
    --config ./miners.json \
    --poll 30
Run inside tmux, screen, or a small systemd service for continuous 24 × 7 operation.

Deployment Checklist

    Update VNish firmware on each S19 to a build that exposes the Web-API.

    Create a long-lived token in Home Assistant.

    Populate miners.json with correct IPs, passwords and thresholds.

    Place the script and config on a host reachable by both HA and the miners.

    Enable Python 3 and install requests if it is not already present.

    Launch the script with the desired polling interval and monitor the log.

Extensibility Points

    Additional metrics: integrate PV output or grid availability to refine control logic.

    E-mail or Telegram alerts: hook into the control_cycle() branch where state changes are logged.

    GUI front-end: expose current status and thresholds through a simple Flask dashboard.

    Graceful shutdown tasks: extend set_hashing() to verify temperature cooldown before power-off.

Summary

The script provides a lightweight, dependable way to align S19-series mining activity with real-time solar battery conditions. It requires only REST access to Home Assistant and the VNish API, is resilient to firmware variations, and can be customized with trivial Python edits. The result is improved battery health, optimized energy usage, and hands-free operation of a small or large fleet of Antminers in a renewable energy environment.
