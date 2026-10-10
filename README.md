# Spotifast

Une table de copie privée pour toi et tes amis : on colle un lien Spotify
(titre, album ou playlist), le serveur prépare les fichiers audio, le
navigateur les télécharge, puis tout est effacé du serveur.

Le moteur est **[Sunnify](https://github.com/sunnypatell/sunnify-spotify-downloader)**
de Sunny Patel, **utilisé sans aucune modification** : sa licence interdit de
le modifier. Spotifast est une application séparée qui l'appelle (sa commande
`sunnify download --json`) et ajoute tout le reste : comptes, sécurité,
interface web et livraison des fichiers dans le navigateur.

> ⚖️ Télécharger de la musique protégée depuis YouTube n'entre pas dans la
> copie privée en droit français, et ce service le rend possible pour d'autres
> personnes que toi. Garde-le dans un cercle restreint et lis le
> [DISCLAIMER de Sunnify](https://github.com/sunnypatell/sunnify-spotify-downloader/blob/main/DISCLAIMER.md).

## Comment ça marche

```
navigateur ──HTTPS──▶ Caddy (443) ──réseau Docker privé──▶ Spotifast ──▶ sunnify (yt-dlp + ffmpeg)
     ▲                                                         │
     └──────────── .mp3 ou .zip envoyé au fil de l'eau ◀───────┘   puis dossier effacé
```

1. On colle un lien → aperçu (pochette, titres) lu chez Spotify.
2. On choisit le format (MP3 / M4A / Opus) et le débit (128 → 320 kbps).
3. Sunnify cherche chaque titre sur YouTube et le convertit ; la progression
   s'affiche en direct.
4. Bouton « Télécharger » : un titre seul arrive en fichier audio, un album ou
   une playlist en `.zip` (un dossier, pistes numérotées, tags et pochettes).
5. Les fichiers sont supprimés du serveur **2 min après le téléchargement**, ou
   **20 min après la fin** au plus tard. Rien ne s'accumule.

Le serveur doit forcément traiter l'audio : un navigateur ne peut pas
récupérer l'audio de YouTube lui-même (YouTube bloque les requêtes venant d'autres sites).
Les fichiers ne font que transiter par le VPS.

## Sécurité

- **Tout est derrière le login**, sauf la page de connexion. Pas de
  documentation d'API exposée, `noindex` partout.
- **Comptes sur invitation** : tu crées les comptes depuis la page `/admin`.
  Chaque nouveau compte reçoit un mot de passe temporaire, affiché une seule
  fois, qu'il faut changer à la première connexion.
- **Mots de passe** : hachés en argon2id (12 caractères minimum). Le temps de
  réponse est le même pour un identifiant inconnu et pour un mauvais mot de
  passe, donc on ne peut pas deviner quels comptes existent.
- **Anti force brute** : 5 échecs → blocage de 15 min pour ce compte depuis
  cette IP. Une IP qui essaie beaucoup de comptes est bloquée, et un plafond
  global protège chaque compte. Un inconnu ne peut pas bloquer ton ami chez lui.
- **Sessions** : jeton aléatoire stocké haché côté serveur, cookie
  `__Host-` `Secure` `HttpOnly` `SameSite=Lax`. Expiration après 7 jours
  d'inactivité ou 30 jours au total. Changer de mot de passe déconnecte les
  autres appareils, et désactiver un compte le déconnecte immédiatement.
- **CSRF** : vérification de l'en-tête `Origin` et jeton par session sur
  toute action.
- **En-têtes** : CSP stricte (aucun script inline), HSTS, `nosniff`,
  `frame-ancestors 'none'`, `Referrer-Policy: same-origin`.
- **Entrées** : seuls les liens Spotify sont acceptés (vérifiés par
  expression régulière). Le lien passé à Sunnify est reconstruit par
  Spotifast, jamais le texte collé. La commande est lancée sans shell.
- **Isolation** : chacun ne voit que ses propres téléchargements. Chaque
  téléchargement tourne dans son propre dossier, avec un environnement
  système minimal.
- **Conteneurs** : utilisateur non-root, système de fichiers en lecture
  seule, aucune capacité Linux, `no-new-privileges`, limites mémoire, CPU et
  nombre de processus. L'application n'a **aucun port exposé** : seul Caddy
  écoute (80/443).
- **VPS** : `scripts/bootstrap-vps.sh` configure le pare-feu (SSH + 80 + 443
  seulement), fail2ban et les mises à jour de sécurité automatiques.

L'admin voit l'historique (qui a téléchargé quoi, quand), et les journaux
Docker gardent les connexions. Dis-le à tes amis.

## Limites (anti-abus, et surtout anti-blocage YouTube)

| Réglage `.env` | Défaut | Rôle |
|---|---|---|
| `SPOTIFAST_DAILY_TRACK_QUOTA` | 400 | titres obtenus par personne sur 24 h glissantes |
| `SPOTIFAST_MAX_TRACKS_PER_JOB` | 150 | taille max d'une playlist |
| `SPOTIFAST_MAX_ACTIVE_JOBS_PER_USER` | 1 | copies en cours par personne |
| `SPOTIFAST_MAX_CONCURRENT_JOBS` | 2 | copies simultanées sur le serveur |
| `SPOTIFAST_RESULT_TTL_MIN` | 20 | effacement après la fin |
| `SPOTIFAST_AFTER_DOWNLOAD_TTL_S` | 120 | effacement après téléchargement |

Toutes les options sont listées dans [.env.example](.env.example).

## Déploiement sur le VPS

Prérequis : un VPS Debian 12/13 ou Ubuntu 22.04/24.04 (x86_64 ou ARM), 2 Go
de RAM minimum, et un sous-domaine (ex. `musique.mondomaine.fr`) avec un
enregistrement DNS **A** (et **AAAA** si IPv6) vers l'IP du VPS.

**1. Récupérer le projet sur le VPS** (Sunnify arrive en sous-module, figé
sur un commit précis du dépôt officiel) :

```bash
ssh user@ton-vps
sudo apt-get install -y git
git clone --recurse-submodules https://github.com/artenl/Spotifast.git ~/spotifast
cd ~/spotifast
```

**2. Préparer le VPS** (une seule fois) :

```bash
sudo bash scripts/bootstrap-vps.sh
```

**3. Configurer** :

```bash
cp .env.example .env
nano .env        # DOMAIN=musique.mondomaine.fr, ACME_EMAIL=ton@email
```

**4. Lancer** (le premier build prend quelques minutes) :

```bash
sudo docker compose up -d --build
sudo docker compose ps
```

Caddy obtient le certificat HTTPS tout seul au premier accès.

**5. Créer ton compte admin** :

```bash
sudo docker compose exec app python -m spotifast.cli create-user ton-pseudo --admin
```

Le mot de passe temporaire s'affiche. Va sur `https://musique.mondomaine.fr`,
connecte-toi et choisis ton mot de passe. Crée ensuite les comptes de tes amis
depuis **Admin**.

**Recommandé : SSH par clé uniquement.** Si tu te connectes déjà au VPS avec
une clé SSH, désactive les mots de passe SSH. Dans
`/etc/ssh/sshd_config.d/hardening.conf`, mets
`PasswordAuthentication no` et `PermitRootLogin prohibit-password`, puis lance
`sudo systemctl reload ssh`. Teste une nouvelle connexion **avant** de fermer
la session en cours.

## Exploitation

| Besoin | Commande (dans `~/spotifast`) |
|---|---|
| Journaux | `sudo docker compose logs -f app` |
| Mettre yt-dlp à jour (si YouTube casse) | `sudo docker compose build --build-arg YTDLP_REFRESH=$(date +%s) app && sudo docker compose up -d` |
| Mettre Spotifast à jour | `git pull --recurse-submodules && sudo docker compose up -d --build` |
| Passer à une nouvelle version de Sunnify | `bash scripts/update-sunnify.sh` (sur ton Mac), commit + push, puis la ligne ci-dessus sur le VPS |
| Lister les comptes | `sudo docker compose exec app python -m spotifast.cli list-users` |
| Réinitialiser un mot de passe | `sudo docker compose exec app python -m spotifast.cli reset-password pseudo` |
| Sauvegarder les comptes | `sudo docker run --rm -v spotifast_data:/d -v $PWD:/b alpine cp /d/spotifast.sqlite3 /b/` |
| Arrêter | `sudo docker compose down` |

## Dépannage

**« YouTube demande au serveur de prouver qu'il n'est pas un robot »** :
YouTube applique souvent cette vérification aux IP d'hébergeurs. Ce n'est ni
un bug de Spotifast ni un bug de Sunnify. Options, de la plus simple à la plus
lourde :
attendre quelques heures, baisser `SPOTIFAST_MAX_CONCURRENT_JOBS` à 1, mettre
yt-dlp à jour (commande ci-dessus), ou faire passer le trafic par un proxy
résidentiel (`HTTPS_PROXY` dans `.env`).

**Un titre est « introuvable »** : Sunnify n'a pas trouvé de vidéo YouTube
qui corresponde (titre, artiste et durée). Le reste de la playlist est
quand même livré.

**Erreur 403 « origine inconnue »** : `DOMAIN` dans `.env` ne correspond pas
à l'adresse tapée dans le navigateur.

## Développement local

```bash
git clone --recurse-submodules https://github.com/artenl/Spotifast.git && cd Spotifast
uv venv -p 3.12 .venv && source .venv/bin/activate
uv pip install -e '.[test]' ./vendor/sunnify deno
python -m pytest            # 49 tests, avec un faux Sunnify (sans réseau)
./scripts/dev.sh admin create-user moi --admin
./scripts/dev.sh            # http://127.0.0.1:8000 (nécessite ffmpeg)
```

`scripts/lock.sh` régénère `requirements.lock` (versions figées et vérifiées
par empreinte, pour Linux x86_64 et arm64).
