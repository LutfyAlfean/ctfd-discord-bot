# CTFd Discord Treasure Bot

Bot notifier Discord untuk CTFd 3.8.5.

## Fitur

- Health server di port `3892`.
- Test Discord webhook via `/test-discord`.
- Test CTFd API via `/test-ctfd`.
- New challenge announcement.
- First blood announcement.
- Optional normal solve announcement.
- State persistence di `state.json`.

## Install

```bash
unzip ctfd-discord-treasure-bot-v1.zip
cd ctfd-discord-treasure-bot-v1
cp .env.example .env
nano .env
docker compose up -d --build
```

## Test

```bash
curl http://127.0.0.1:3892/health
curl http://127.0.0.1:3892/test-discord
curl http://127.0.0.1:3892/test-ctfd
curl http://127.0.0.1:3892/status
curl http://127.0.0.1:3892/force-poll
```

## Catatan penting

Jika `SKIP_EXISTING_ON_FIRST_RUN=true`, bot tidak announce challenge/solve lama saat pertama jalan. Buat challenge baru atau solve baru setelah bot hidup.

Untuk reset state:

```bash
docker compose down
cat > state.json <<'EOF'
{
  "initialized": false,
  "seen_challenges": [],
  "seen_solves": [],
  "first_blood_challenges": [],
  "last_run": null
}
EOF
docker compose up -d --build
```
