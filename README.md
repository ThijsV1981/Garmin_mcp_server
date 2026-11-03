# Garmin MCP server (Fly.io)

Persoonlijke, read-only MCP-server voor de Garmin Connect Personal API, gehost op Fly.io en te koppelen als custom connector in claude.ai. Werkt op telefoon, web en in geplande taken, zonder dat je Mac aan hoeft te staan.

- Basis: [`garmin-personal-mcp`](https://github.com/Mason-Levyy/garmin-personal-mcp) 0.2.0 (activiteiten, slaap, HRV, rusthartslag, stress, body battery, gewicht, profiel, HS-zones).
- Toegevoegd: OAuth met één wachtwoord (de upstream-server heeft geen eigen beveiliging), en `garmin_get_activity_streams`: hartslag, snelheid, cadans, vermogen en hoogte per interval uit het .FIT-bestand.
- Toegevoegd: `intervals_upsert_workouts` en `intervals_list_workouts`: geplande workouts (als .FIT) in je intervals.icu-kalender zetten en terugkijken. intervals.icu stuurt ze door naar Garmin Connect. Upsert op `external_id` (prefix `lr-`), dus opnieuw sturen overschrijft; met `prune_from`/`prune_to` verdwijnen `lr-`-workouts die niet meer in het plan staan. Workouts die je zelf in intervals.icu maakt, blijven altijd staan.
- Verwijderd: `garmin_download_activity` (sloeg het bestand alleen op de server op).

## Eenmalige installatie (Mac)

### 1. Fly-CLI en account

```bash
brew install flyctl
fly auth signup   # of: fly auth login
```

### 2. Garmin-key

Garmin Connect (web) → Instellingen → Personal API → *Create Key*. Kies een looptijd (max. 1 jaar) en kopieer de key; Garmin toont hem één keer.

### 3. App aanmaken

```bash
git clone https://github.com/ThijsV1981/Garmin_mcp_server.git
cd Garmin_mcp_server
fly apps create garmin-mcp-thijs
```

Is de naam bezet, kies een andere en pas `app = ...` in `fly.toml` aan.

### 4. Secrets zetten

Plak de waarden bij de verborgen prompt, zo komen ze niet in je shell-historie:

```bash
printf "Garmin-key: "; read -s GK; echo
printf "Inlogwachtwoord (min. 12 tekens): "; read -s PW; echo
fly secrets set GARMIN_PERSONAL_API_KEY="$GK" LOGIN_PASSWORD="$PW" OAUTH_SIGNING_KEY="$(openssl rand -hex 32)"
unset GK PW
```

Het inlogwachtwoord is wat je straks één keer intypt bij het koppelen in claude.ai. Bewaar het in je wachtwoordmanager.

### 5. Deployen en checken

```bash
fly deploy --ha=false
curl https://garmin-mcp-thijs.fly.dev/healthz    # verwacht {"ok":true}
```

`fly deploy` maakt de machine zelf aan op basis van `fly.toml`; in het Fly-dashboard hoef je niets aan te maken. `--ha=false` zorgt dat het bij één machine blijft (standaard zet Fly er twee neer voor redundantie, die heb je hier niet nodig).

### 6. Koppelen in claude.ai

Voeg een custom connector toe met URL `https://garmin-mcp-thijs.fly.dev/mcp`. Je komt op een donkere inlogpagina: wachtwoord invullen, klaar. Daarna blijft de koppeling 90 dagen geldig en ververst hij zichzelf.

### 7. intervals.icu (optioneel)

intervals.icu → Settings → Developer Settings → API key kopiëren. Daarna:

```bash
printf "intervals.icu-key: "; read -s IK; echo
fly secrets set INTERVALS_API_KEY="$IK"
unset IK
```

Zet in intervals.icu bij Settings → Connections → Garmin het uploaden van geplande workouts aan, dan komen ze op je horloge.

## Onderhoud

| Wat | Hoe |
|---|---|
| Garmin-key verloopt (Garmin mailt een week vooraf) | Nieuwe key maken → `fly secrets set GARMIN_PERSONAL_API_KEY="..."` (via `read -s` zoals hierboven) |
| Alle koppelingen intrekken | `fly secrets set OAUTH_SIGNING_KEY="$(openssl rand -hex 32)"` en daarna opnieuw koppelen |
| Wachtwoord wijzigen | `fly secrets set LOGIN_PASSWORD="..."` |
| intervals.icu-key vervangen | `fly secrets set INTERVALS_API_KEY="..."` (via `read -s`) |
| Logs bekijken | `fly logs` |
| Update van upstream | Versie in `requirements.txt` ophogen → `fly deploy` |

## Beveiliging in het kort

- Alles behalve `/healthz` en de OAuth-endpoints vereist een geldige bearer-token.
- Alleen redirect-URL's naar `claude.ai` en `claude.com` worden geaccepteerd (`ALLOWED_REDIRECT_HOSTS`).
- Tokens zijn HMAC-ondertekend en stateless (access 1 uur, refresh 90 dagen), zodat ze een herstart van de machine overleven. Losse tokens intrekken kan niet: roteer daarvoor de signing key.
- Fout wachtwoord: 2 seconden vertraging per poging.
- De Garmin-key staat alleen als Fly-secret, nooit in de repo.

## Kosten

Eén shared-cpu-1x-machine met 256 MB in `ams`, die stopt als er geen verkeer is en bij een request weer opstart (eerste call een paar seconden trager). Bij dit gebruik verwaarloosbaar.
