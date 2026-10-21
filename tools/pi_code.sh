# Sourced by tools/hot.sh, tools/deploy.sh and tools/build_card.sh (the Pi's: needs $SSH and $PI).

# pi_reachable: fail with a plain message when the Pi doesn't answer SSH.
pi_reachable() {
    $SSH $PI true 2>/dev/null && return
    echo "can't reach the Pi at $PI over SSH (a dev card on USB; over Wi-Fi: PI=root@ADDRESS)" >&2
    exit 1
}

# pi_wait_up [SECONDS] [BOOT_ID]: wait for " pi up " in the daemon's log (in the
# boot other than BOOT_ID, if given).
pi_wait_up() {
    end=$(( $(date +%s) + ${1:-60} ))
    while [ "$(date +%s)" -lt "$end" ]; do
        sleep 2
        $SSH -o ConnectTimeout=2 $PI "${2:+[ \"\$(cat /proc/sys/kernel/random/boot_id)\" != $2 ] &&}
            grep -q ' pi up ' /var/log/audiolink.log" 2>/dev/null && return 0
    done
    return 1
}

# pi_code_check: is the daemon running the code just installed? An update on the
# card (`audiolink update --code`: /var/lib/audiolink/audiolink.pyz) runs instead of the
# built-in copy. Offers to remove it (and restart); returns 1 if it stays.
pi_code_check() {
    U=/var/lib/audiolink/audiolink.pyz
    $SSH $PI "ps | awk '\$4==\"python3\" && \$5==\"$U\" {f=1} END {exit !f}'" 2>/dev/null || return 0
    echo "warning: the Pi runs the update on its card ($U, from \`audiolink update\`), not this code" >&2
    if [ -t 0 ]; then
        printf "Remove the update and restart on this code? [y/N] " >&2
        read -r ans
        case "$ans" in [yY]*)
            $SSH $PI "rm -f $U $U.bad $U.new && (/opt/audiolink/start-audiolink.sh \$(grep -v '^#' /etc/audiolink.args)) >/dev/null 2>&1 &"
            pi_wait_up 60 && echo "pi up, on this code" && return 0
            echo "daemon did not come up:" >&2; $SSH $PI 'tail -20 /var/log/audiolink.log' >&2
            exit 1 ;;
        esac
    fi
    echo "to run this code: audiolink update --code --back" >&2
    return 1
}

# card_initrd CARD: a built card's initramfs (in its slot, a/; boot/ before slots),
# or fail saying so: a check that can't find it mustn't pass.
card_initrd() {
    for f in "$1/a/boot/initrd.img" "$1/boot/initrd.img"; do
        [ -f "$f" ] && { echo "$f"; return 0; }
    done
    echo "no initramfs in $1 (a/boot/initrd.img)" >&2
    return 1
}
