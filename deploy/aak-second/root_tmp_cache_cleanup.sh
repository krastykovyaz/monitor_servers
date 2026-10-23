#!/bin/bash
# aak-second, run as root:  sudo bash ~/.local/share/aak2_cleanup/root_tmp_cache_cleanup.sh
# 1. removes root-owned picture leftovers from /tmp (picturepeace2 runs as root; the alex cron that was meant to sweep them
#    cannot delete other users' files in the sticky /tmp, so they piled up);
# 2. replaces that cron with a root one in /etc/cron.d;
# 3. trims the journal to 200 MB and clears the apt package cache.
set -eu
[ "$(id -u)" = 0 ] || { echo "run as root: sudo bash $0"; exit 1; }
b=$(df -m / | awk 'NR==2{print $3}')
echo "before: $(df -h / | tail -1 | awk '{print $3" used, "$4" free, "$5}')"
n=$(find /tmp -xdev -maxdepth 1 -type f -name '*_[0-9]*.jpg' -mtime +1 | wc -l)
find /tmp -xdev -maxdepth 1 -type f -name '*_[0-9]*.jpg' -mtime +1 -delete
echo "removed $n picture files older than 1 day from /tmp"
printf '# picturepeace2 leftovers in /tmp (its bot runs as root, so the sweep has to as well)\n30 3 * * * root find /tmp -xdev -maxdepth 1 -type f -name "*_[0-9]*.jpg" -mtime +1 -delete\n' > /etc/cron.d/tmp-media-sweep
chmod 644 /etc/cron.d/tmp-media-sweep; echo "installed /etc/cron.d/tmp-media-sweep"
journalctl --vacuum-size=200M 2>&1 | tail -1
apt-get clean && echo "apt cache cleared"
a=$(df -m / | awk 'NR==2{print $3}')
echo "after:  $(df -h / | tail -1 | awk '{print $3" used, "$4" free, "$5}')   (freed $((b - a)) MB)"
