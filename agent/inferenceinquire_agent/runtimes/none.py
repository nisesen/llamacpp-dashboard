"""No inference server on this machine: host and GPU telemetry only."""

NAME = "none"


def available():
    return True


def collect():
    return {
        "guests": [],
        "llm_ct": {
            "vmid": None,
            "reachable": False,
            "units": {},
            "discovered": True,
            "runtime": NAME,
            "label": None,
        },
    }


def follow(target, unit):
    return None


def read_head(target, unit, path, size):
    return None
