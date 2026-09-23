#!/bin/sh
# Role-aware bahamut entrypoint.
# Expands the correct conf template per SERVER_ROLE and execs ircd in foreground.
set -eu

: "${SERVER_ROLE:?SERVER_ROLE is required (hub|leaf4|leaf6)}"
: "${SERVER_NAME:?}"
: "${LISTEN_PORT:?}"
: "${LISTEN_SSL_PORT:?}"
: "${OPER_NICK:?}"
: "${OPER_PASS:?}"

case "${SERVER_ROLE}" in
    hub)
        : "${SERVICES_PASSWORD:?}"
        : "${LEAF_PASSWORD:?}"
        : "${LINK_PORT_V4:?}"
        : "${LINK_PORT_V6:?}"
        : "${SERVICES_IP:?}"
        : "${LEAF4_IP:?}"
        : "${LEAF6_IP:?}"
        tmpl=/etc/bahamut/conf.hub.tmpl
        ;;
    leaf4|leaf6)
        : "${HUB:?}"
        : "${HUB_PORT:?}"
        : "${LEAF_PASSWORD:?}"
        tmpl="/etc/bahamut/conf.${SERVER_ROLE}.tmpl"
        ;;
    *)
        printf 'unknown SERVER_ROLE=%s\n' "${SERVER_ROLE}" >&2
        exit 2
        ;;
esac

# Hash the oper password with the system crypt(3) — bahamut O-lines expect a
# crypted password so leaking the running conf does not leak the plaintext.
# Uses a random 2-char DES salt; good enough for a throwaway testnet.
OPER_PASS_HASH="$(mkpasswd --method=des "${OPER_PASS}")"
export OPER_PASS_HASH

# Cap the resolver's per-query timeout/attempts BEFORE the ircd starts.
# bahamut completes client registration through its (serialized) resolver —
# a PTR for a docker-network client IP that 127.0.0.11 forwards upstream
# costs the full glibc default (2 attempts x 5s) PER CONNECTION, and
# concurrent connections queue behind it (28-40s bursts at ~20 clients,
# minutes at corpus scale). timeout:1 attempts:1 caps the worst case at
# ~1s/client; PTRs that 127.0.0.11 answers locally stay instant.
if [ -w /etc/resolv.conf ]; then
    grep -q '^options ' /etc/resolv.conf \
        && sed -e 's/^options .*/& timeout:1 attempts:1/' -i /etc/resolv.conf \
        || printf 'options timeout:1 attempts:1\n' >> /etc/resolv.conf
fi

# Second, shared testnet O-line (Sonic's request): fixed user "azzurra"
# with fixed password "azzt3st" (DES-crypted at boot), so staff poking at
# the testnet can /oper without chasing the per-boot OPER_NICK/OPER_PASS.
AZZURRA_OPER_PASS_HASH="$(mkpasswd --method=des azzt3st)"
export AZZURRA_OPER_PASS_HASH

# Generate conf from template (limit envsubst to known vars so `$foo`
# strings inside conf comments are left alone).
VARS='$SERVER_NAME $SERVER_DESC $OPER_NICK $OPER_PASS_HASH $AZZURRA_OPER_PASS_HASH'
VARS="$VARS \$LISTEN_PORT \$LISTEN_SSL_PORT"
VARS="$VARS \$LINK_PORT_V4 \$LINK_PORT_V6"
VARS="$VARS \$HUB \$HUB_PORT \$HUB_IP"
VARS="$VARS \$SERVICES_IP \$LEAF4_IP \$LEAF6_IP"
VARS="$VARS \$SERVICES_PASSWORD \$LEAF_PASSWORD"

envsubst "${VARS}" < "${tmpl}" > /etc/bahamut/bahamut.conf

# Cloak key — bahamut-azzurra needs a >=64-byte key at DPATH/ircd.cloak
# (see include/config.h MIN_CLOAK_KEY_LEN/CKPATH). MUST be identical on
# every server on the network: crypt_userhost.c hashes (user@host, key)
# to produce the +x cloak, so divergent keys produce different cloaked
# hosts for the same user depending on which server sees them. cert-init
# generates the shared key once into ./certs/ircd.cloak (mounted
# read-only at /etc/bahamut/certs here); we copy it into DPATH so the
# file stays under /var/lib/bahamut/ where bahamut expects it.
if [ -s /etc/bahamut/certs/ircd.cloak ]; then
    cp /etc/bahamut/certs/ircd.cloak /var/lib/bahamut/ircd.cloak
elif [ ! -s /var/lib/bahamut/ircd.cloak ]; then
    echo "ircd.cloak missing from /etc/bahamut/certs and DPATH — cert-init did not run?" >&2
    exit 1
fi

# SSL — bahamut-azzurra expects ircd.crt + ircd.key in DPATH
# (include/config.h: IRCDSSL_CPATH / IRCDSSL_KPATH). Our cert-init produces
# one combined <role>.pem per host (cert || key); openssl's
# use_certificate_chain_file + use_PrivateKey_file both accept combined PEM,
# so symlink the same file to both expected names.
case "${SERVER_ROLE}" in
    hub)   pem=/etc/bahamut/certs/hub.pem    ;;
    leaf4) pem=/etc/bahamut/certs/leaf4.pem  ;;
    leaf6) pem=/etc/bahamut/certs/leaf6.pem  ;;
esac
if [ -s "${pem}" ]; then
    ln -sf "${pem}" /var/lib/bahamut/ircd.crt
    ln -sf "${pem}" /var/lib/bahamut/ircd.key
fi

# Dump the conf to stderr at startup (modulo OPER hash) for debugging.
# Real tokens stay out of stdout anyway — this is a testnet image.
printf '=== %s bahamut.conf ===\n' "${SERVER_ROLE}" >&2
sed -e 's/O:\(.*\):[^:]*:\(.*\)/O:\1:<hashed>:\2/' /etc/bahamut/bahamut.conf >&2
printf '=== end bahamut.conf ===\n' >&2

exec /usr/local/sbin/ircd -t -s -d /var/lib/bahamut -f /etc/bahamut/bahamut.conf
