"""Helpers shared by the test files."""

from inferenceinquire.rules import evaluate_alerts
from inferenceinquire.store import Store


def make_store(tmp_path):
    return Store(str(tmp_path / "t.db"))


def healthy_snap():
    return {
        "agent_ok": True,
        "gpu_available": True,
        "llama": {"reachable": True, "healthy": True, "url": "http://x"},
        "model": {"file": "m.gguf"},
        "gpus": [
            {
                "idx": 0,
                "serial": "SN1",
                "temp": 40,
                "hbm": 45,
                "pwr": 90,
                "pwr_limit": 250,
                "throttle": [],
                "pcie_gen": 3,
                "pcie_gen_max": 3,
                "util": 30,
                "ecc_uncorrected": 0,
                "remap_correctable": 0,
                "remap_uncorrectable": 0,
                "remap_pending": "No",
                "remap_failure": "No",
                "limits": {
                    "temp_mem_warn": 90,
                    "temp_mem_crit": 100,
                    "temp_core_warn": 85,
                    "temp_slowdown": 95,
                    "temp_shutdown": 105,
                    "power_limit": 250,
                },
            }
        ],
        "units": {"llama-server": {"active": "active", "restarts": 0}},
        "ipmi_sensors": [],
        "disks": [],
        "xid": {"count": 0},
        "llm_ct": {"vmid": "100", "reachable": True},
    }


def keys(alerts):
    return {a["key"] for a in alerts}


def evaluate(store, snap, point):
    return keys(evaluate_alerts(snap, point, store))
