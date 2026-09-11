#!/bin/sh
# Forced command for the fleetdeck console key on pfSense. The key line in
# System > User Manager > admin > Authorized SSH Keys carries
# command="/root/fleetdeck-read.sh", so this script is all the key can run:
# three read-only subcommands, anything else is refused.

case "$SSH_ORIGINAL_COMMAND" in
dhcp)
	# leases and static mappings the way status_dhcp_leases.php reads
	# them, without the reverse DNS lookups (false) that the page does
	exec /usr/local/bin/php -q <<'PHP'
<?php
require_once("config.inc");
require_once("functions.inc");
echo json_encode(array(
	"lease" => system_get_dhcpleases(false)["lease"],
	"arp" => system_get_arp_table(),
));
PHP
	;;
status)
	wan=$(route -n get default 2>/dev/null | awk '/interface:/ {print $2}')
	echo "== version"
	cat /etc/version
	echo "== boottime"
	sysctl -n kern.boottime
	echo "== temp"
	sysctl -n dev.cpu.0.temperature
	echo "== load"
	sysctl -n vm.loadavg
	echo "== mem"
	sysctl -n hw.physmem hw.pagesize vm.stats.vm.v_free_count vm.stats.vm.v_inactive_count
	echo "== states"
	pfctl -si | grep 'current entries'
	pfctl -sm | grep '^states'
	echo "== wan $wan"
	netstat -ibn -I "$wan"
	echo "== unbound"
	ps -o etimes= -p "$(cat /var/run/unbound.pid 2>/dev/null)" 2>/dev/null
	;;
tailscale)
	exec /usr/local/bin/tailscale status --json
	;;
*)
	echo "refused: $SSH_ORIGINAL_COMMAND" >&2
	exit 1
	;;
esac
