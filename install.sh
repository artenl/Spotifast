#!/usr/bin/env bash
# Spotifast one-line installer / updater for a Debian or Ubuntu VPS.
#
#   curl -fsSL https://raw.githubusercontent.com/artenl/Spotifast/main/install.sh | sudo bash
#
# or, without questions:
#
#   curl -fsSL https://raw.githubusercontent.com/artenl/Spotifast/main/install.sh \
#     | sudo bash -s -- --domain musique.example.fr --email toi@example.fr --admin pseudo
#
# Running it again updates Spotifast (git pull + rebuild with a fresh yt-dlp)
# and keeps your accounts and settings.
#
# Everything lives in main(), called on the last line, so a truncated
# download can never run half a script.
set -euo pipefail

REPO_URL="https://github.com/artenl/Spotifast.git"
INSTALL_DIR="${SPOTIFAST_DIR:-/opt/spotifast}"

DOMAIN=""
EMAIL=""
ADMIN=""
SKIP_BOOTSTRAP=0

say()  { printf '\033[1;33m==>\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m ✓\033[0m %s\n' "$*"; }
warn() { printf '\033[1;31m !\033[0m %s\n' "$*" >&2; }
die()  { warn "$*"; exit 1; }

usage() {
  cat <<'EOF'
Usage: install.sh [--domain DOMAIN] [--email EMAIL] [--admin USERNAME] [--skip-bootstrap]

  --domain          sub-domain pointing at this server (e.g. musique.example.fr)
  --email           contact address for Let's Encrypt certificates
  --admin           create this admin account (a temporary password is printed)
  --skip-bootstrap  don't touch the system (firewall, fail2ban, Docker install)

Missing values are asked interactively. Re-run to update.
EOF
}

valid_domain()   { [[ "$1" =~ ^([A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}$ ]]; }
valid_email()    { [[ "$1" =~ ^[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+\.)+[A-Za-z]{2,63}$ ]]; }
valid_username() { [[ "$1" =~ ^[A-Za-z0-9_.-]{3,32}$ ]]; }

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --domain) DOMAIN="${2:-}"; shift 2 ;;
      --email) EMAIL="${2:-}"; shift 2 ;;
      --admin) ADMIN="${2:-}"; shift 2 ;;
      --skip-bootstrap) SKIP_BOOTSTRAP=1; shift ;;
      -h|--help) usage; exit 0 ;;
      *) usage >&2; die "unknown option: $1" ;;
    esac
  done
}

# With `curl | bash`, stdin is the script itself: questions go to the terminal.
have_tty() { { true < /dev/tty; } 2>/dev/null; }

# Must be called outside $(...): `exit` in a command substitution only
# leaves the subshell, which would turn the question loops into busy loops.
need_tty() {
  have_tty || die "valeur manquante ($1) et pas de terminal pour la demander : passe-la en option (voir --help)"
}

ask() {
  local prompt="$1" default="${2:-}" answer=""
  if [[ -n "$default" ]]; then
    read -r -p "$prompt [$default] " answer < /dev/tty || true
    printf '%s' "${answer:-$default}"
  else
    read -r -p "$prompt " answer < /dev/tty || true
    printf '%s' "$answer"
  fi
}

env_get() {  # env_get KEY FILE -> value of KEY=... (empty if absent)
  [[ -f "$2" ]] || return 0
  sed -n "s/^$1=//p" "$2" | tail -n 1
}

env_set() {  # env_set KEY VALUE FILE (values are validated beforehand)
  local key="$1" value="$2" file="$3"
  if grep -q "^${key}=" "$file"; then
    sed -i "s|^${key}=.*|${key}=${value}|" "$file"
  else
    printf '%s=%s\n' "$key" "$value" >> "$file"
  fi
}

collect_settings() {
  local env_file="$INSTALL_DIR/.env"
  # On an update, the existing .env provides the defaults.
  [[ -n "$DOMAIN" ]] || DOMAIN="$(env_get DOMAIN "$env_file")"
  [[ -n "$EMAIL" ]] || EMAIL="$(env_get ACME_EMAIL "$env_file")"
  [[ "$DOMAIN" == "musique.example.fr" ]] && DOMAIN=""
  [[ "$EMAIL" == "toi@example.fr" ]] && EMAIL=""

  until valid_domain "$DOMAIN"; do
    [[ -z "$DOMAIN" ]] || warn "domaine invalide : $DOMAIN"
    need_tty --domain
    DOMAIN="$(ask "Domaine du site (ex. musique.mondomaine.fr) :")"
  done
  until valid_email "$EMAIL"; do
    [[ -z "$EMAIL" ]] || warn "e-mail invalide : $EMAIL"
    need_tty --email
    EMAIL="$(ask "E-mail pour les certificats HTTPS (Let's Encrypt) :")"
  done
  # First install: offer to create the admin account.
  if [[ -z "$ADMIN" && ! -f "$env_file" ]] && have_tty; then
    ADMIN="$(ask "Pseudo de ton compte admin (vide pour plus tard) :")"
    until [[ -z "$ADMIN" ]] || valid_username "$ADMIN"; do
      warn "pseudo invalide : 3 à 32 caractères parmi lettres, chiffres, . - _"
      ADMIN="$(ask "Pseudo de ton compte admin (vide pour plus tard) :")"
    done
  fi
  if [[ -n "$ADMIN" ]] && ! valid_username "$ADMIN"; then
    die "pseudo admin invalide : 3 à 32 caractères parmi lettres, chiffres, . - _"
  fi
}

check_system() {
  [[ $EUID -eq 0 ]] || die "à lancer en root : curl -fsSL … | sudo bash"
  [[ -r /etc/os-release ]] || die "système non pris en charge (pas de /etc/os-release)"
  # shellcheck disable=SC1091
  . /etc/os-release
  case "${ID:-}" in
    debian|ubuntu) ;;
    *) die "seuls Debian et Ubuntu sont pris en charge (trouvé : ${ID:-inconnu})" ;;
  esac
  case "$(uname -m)" in
    x86_64|aarch64) ;;
    *) die "architecture non prise en charge : $(uname -m) (x86_64 ou arm64 requis)" ;;
  esac
  local mem_mb
  mem_mb=$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo)
  if (( mem_mb < 1800 )); then
    warn "seulement ${mem_mb} Mo de RAM : 2 Go recommandés (mets SPOTIFAST_MAX_CONCURRENT_JOBS=1 dans .env)"
  fi
}

fetch_code() {
  export DEBIAN_FRONTEND=noninteractive
  if ! command -v git >/dev/null; then
    say "installation de git"
    apt-get update -qq < /dev/null
    apt-get install -y -qq git ca-certificates curl < /dev/null
  fi
  if [[ -d "$INSTALL_DIR/.git" ]]; then
    say "mise à jour de $INSTALL_DIR"
    git -C "$INSTALL_DIR" pull --ff-only --recurse-submodules
    git -C "$INSTALL_DIR" submodule update --init --recursive
  else
    say "téléchargement de Spotifast dans $INSTALL_DIR"
    git clone --recurse-submodules "$REPO_URL" "$INSTALL_DIR"
  fi
  if [[ -n "$(git -C "$INSTALL_DIR/vendor/sunnify" status --porcelain)" ]]; then
    die "vendor/sunnify a été modifié : Sunnify doit rester intact (sa licence l'impose)"
  fi
}

write_env() {
  local env_file="$INSTALL_DIR/.env"
  if [[ ! -f "$env_file" ]]; then
    cp "$INSTALL_DIR/.env.example" "$env_file"
  fi
  env_set DOMAIN "$DOMAIN" "$env_file"
  env_set ACME_EMAIL "$EMAIL" "$env_file"
  chmod 600 "$env_file"
  ok "réglages dans $env_file"
}

check_dns() {
  local resolved local_ips
  # getent exits 2 for an unknown name: under pipefail that must not end
  # the install, a not-yet-propagated DNS record is only worth a warning.
  resolved=$( (getent ahostsv4 "$DOMAIN" 2>/dev/null || true) | awk '{print $1}' | sort -u | tr '\n' ' ')
  local_ips=$(hostname -I 2>/dev/null || true)
  if [[ -z "$resolved" ]]; then
    warn "$DOMAIN ne pointe encore nulle part : crée un enregistrement DNS A vers ce serveur,"
    warn "sinon le HTTPS ne peut pas s'activer (Caddy réessaie tout seul ensuite)."
    return
  fi
  for ip in $resolved; do
    if [[ " $local_ips " == *" $ip "* ]]; then
      ok "$DOMAIN -> $ip (ce serveur)"
      return
    fi
  done
  warn "$DOMAIN pointe vers $resolved, qui n'est pas une adresse de ce serveur ($local_ips)."
  warn "Normal derrière un NAT d'hébergeur ; sinon corrige l'enregistrement DNS A."
}

start_stack() {
  cd "$INSTALL_DIR"
  say "construction et démarrage (quelques minutes la première fois)"
  # A fresh yt-dlp on every install/update: YouTube breaks old releases.
  docker compose build --build-arg "YTDLP_REFRESH=$(date +%s)" app
  docker compose up -d --remove-orphans
  ok "conteneurs démarrés"
}

create_admin() {
  [[ -n "$ADMIN" ]] || return 0
  cd "$INSTALL_DIR"
  local out rc=0
  out=$(docker compose exec -T app python -m spotifast.cli create-user "$ADMIN" --admin 2>&1) || rc=$?
  if [[ $rc -eq 0 ]]; then
    TEMP_PASSWORD=$(printf '%s\n' "$out" | sed -n 's/^temporary password: //p')
  elif printf '%s' "$out" | grep -q "already exists"; then
    ok "le compte $ADMIN existe déjà (nouveau mot de passe : cd $INSTALL_DIR && sudo docker compose exec app python -m spotifast.cli reset-password $ADMIN)"
  else
    printf '%s\n' "$out" >&2
    die "impossible de créer le compte admin"
  fi
}

check_https() {
  local code="" i
  say "attente du certificat HTTPS"
  for i in $(seq 1 30); do
    code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "https://$DOMAIN/login" || true)
    [[ "$code" == "200" ]] && { ok "https://$DOMAIN répond"; return; }
    sleep 4
  done
  warn "https://$DOMAIN ne répond pas encore (dernier statut : ${code:-aucun})."
  warn "Vérifie le DNS et que les ports 80/443 sont ouverts chez ton hébergeur,"
  warn "puis : cd $INSTALL_DIR && sudo docker compose logs caddy"
}

main() {
  parse_args "$@"
  check_system
  # Code first (it ships bootstrap-vps.sh and .env.example), then settings.
  fetch_code
  collect_settings
  if [[ $SKIP_BOOTSTRAP -eq 0 ]]; then
    say "sécurisation du serveur (mises à jour, pare-feu, fail2ban, Docker)"
    bash "$INSTALL_DIR/scripts/bootstrap-vps.sh" < /dev/null
  elif ! command -v docker >/dev/null; then
    die "--skip-bootstrap demandé mais Docker n'est pas installé"
  fi
  write_env
  check_dns
  TEMP_PASSWORD=""
  start_stack
  create_admin
  check_https

  echo
  ok "Spotifast est installé dans $INSTALL_DIR"
  echo "   Adresse : https://$DOMAIN"
  if [[ -n "$TEMP_PASSWORD" ]]; then
    echo "   Admin   : $ADMIN"
    echo "   Mot de passe temporaire (affiché une seule fois, à changer à la 1re connexion) : $TEMP_PASSWORD"
  elif [[ -z "$ADMIN" ]]; then
    echo "   Crée ton compte admin :"
    echo "     cd $INSTALL_DIR && sudo docker compose exec app python -m spotifast.cli create-user PSEUDO --admin"
  fi
  echo "   Pour mettre à jour : relance la même commande d'installation."
}

[[ "${SPOTIFAST_INSTALL_TEST:-}" == 1 ]] || main "$@"
