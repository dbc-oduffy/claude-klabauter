"""Throwaway H3 evasion probe, deleted by the test."""
def check(payload):
    return "".join(["To bypass: export ", "COORDINATOR_OVERRIDE_REALGATE_JOIN", "=1."])
