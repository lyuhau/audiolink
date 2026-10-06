"""Wi-Fi terms and prompts shared by the host's `audiolink wifi` and network changes."""
import os

SEC_LABEL = {"open": "open", "owe": "open+", "wpa2": "WPA2", "wpa3": "WPA3", "wpa2/wpa3": "WPA2/3",
             "wep": "WEP", "enterprise": "802.1X"}


def wifi_password(ask, ssid, security, retry=False):
    """Returns (password, can_retry): from --password-stdin, $AUDIOLINK_WIFI_PASSWORD
    (first try only), or a prompt that enforces WPA2's 8-63 characters."""
    if ask.stdin_pw is not None:
        return ask.stdin_pw, False
    env = os.environ.get("AUDIOLINK_WIFI_PASSWORD")
    if env and not retry:
        return env, False
    while True:
        pw = ask.secret("%s password for %r: " % (SEC_LABEL.get(security, security), ssid))
        if security in ("wpa2", "wpa2/wpa3") and not 8 <= len(pw) <= 63:
            print("WPA2 passwords are 8-63 characters; try again")
            continue
        if pw:
            return pw, True
