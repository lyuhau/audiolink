"""Wi-Fi terms and prompts shared by the host's `audiolink wifi` and network changes."""
import os

SEC_LABEL = {"open": "open", "owe": "open (encrypted)", "wpa2": "WPA2", "wpa3": "WPA3",
             "wpa2/wpa3": "WPA2/3", "wep": "WEP", "enterprise": "enterprise"}


def bars(dbm):
    """Signal strength the way phones show it."""
    level = 4 if dbm >= -55 else 3 if dbm >= -67 else 2 if dbm >= -75 else 1
    return "▂▄▆█"[:level].ljust(4, "·")


def wifi_password(ask, ssid, security, retry=False):
    """Returns (password, can_retry): from --password-stdin, $AUDIOLINK_WIFI_PASSWORD
    (first try only), or a prompt that enforces WPA2's 8-63 characters."""
    if ask.stdin_pw is not None:
        return ask.stdin_pw, False
    env = os.environ.get("AUDIOLINK_WIFI_PASSWORD")
    if env and not retry:
        return env, False
    while True:
        pw = ask.secret("Password for %s: " % ssid)
        if security in ("wpa2", "wpa2/wpa3") and not 8 <= len(pw) <= 63:
            print("Wi-Fi passwords are 8 to 63 characters long. Try again.")
            continue
        if pw:
            return pw, True
