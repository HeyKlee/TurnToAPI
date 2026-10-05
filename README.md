# TurnToAPI

Turn browser-backed AI web sessions into an OpenAI-compatible API for tools such as Open WebUI and Hermes.

TurnToAPI is designed for **private/local use**. It launches a dedicated persistent Playwright Firefox profile only when a browser-backed model is actually called. It never intentionally binds to `0.0.0.0` unless you explicitly change the configuration.

## Linux — one command

```bash
curl -fsSL https://raw.githubusercontent.com/HeyKlee/TurnToAPI/main/install.sh | bash
```

The installer supports common `apt`, `dnf`, `pacman`, and `zypper` Linux distributions. It installs Python dependencies, Playwright Firefox, persistent profile directories, a start/kill script, and a user-level systemd service where available.

If Tailscale is installed and connected, TurnToAPI binds port `8000` to the machine's Tailscale IPv4. Otherwise it binds only to `127.0.0.1`.

## Windows

PowerShell:

```powershell
irm https://raw.githubusercontent.com/HeyKlee/TurnToAPI/main/install-windows.ps1 | iex
```

Default install location:

```text
%LOCALAPPDATA%\TurnToAPI
```

## macOS

```bash
curl -fsSL https://raw.githubusercontent.com/HeyKlee/TurnToAPI/main/install-macos.sh | bash
```

## API

Default endpoint:

```text
http://127.0.0.1:8000/v1
```

With Tailscale it will normally be:

```text
http://<TAILSCALE-IP>:8000/v1
```

Health check:

```bash
curl http://127.0.0.1:8000/health
```

Models:

```bash
curl http://127.0.0.1:8000/v1/models
```

Example completion:

```bash
curl http://127.0.0.1:8000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "arena:text:gemma-4-31b",
    "messages": [
      {"role": "user", "content": "Reply with exactly: TURNTOAPI WORKING"}
    ],
    "stream": false
  }'
```

Streaming uses OpenAI-style SSE. Arena thought text is emitted under:

```text
delta.reasoning_content
```

and the final answer under:

```text
delta.content
```

## Persistent browser

Arena uses a dedicated persistent Playwright Firefox profile:

```text
playwright_profiles/arena
```

The browser is **lazy launched**. Merely starting TurnToAPI or calling `/health` does not open Firefox. The first Arena model request opens it, and the same profile is reused for later requests.

This preserves normal login/session state more reliably than creating a fresh WebDriver session for every request.

## Security verification / CAPTCHA

TurnToAPI does **not** solve or bypass reCAPTCHA, Cloudflare, or other anti-bot/security challenges.

When Arena displays a verification page:

1. TurnToAPI suppresses the challenge/navigation text instead of returning it as an assistant response.
2. The same persistent browser stays open.
3. Complete the verification manually.
4. TurnToAPI waits for the challenge to disappear and resumes the request.

A Linux server therefore needs a usable graphical display for Arena's headed browser if human verification is required.

## Open WebUI

Add an OpenAI-compatible connection pointing to:

```text
http://<TURNTOAPI-HOST>:8000/v1
```

No API key is required by the default configuration.

## Models

Arena models are listed in:

```text
arena_models_live.json
```

Model IDs are exposed in both text and code forms, for example:

```text
arena:text:gemma-4-31b
arena:code:gemma-4-31b
```

TurnToAPI refuses a direct Arena model request if Arena silently redirects it to the generic **Max** router.

## Start / stop

Linux with systemd:

```bash
systemctl --user start turntoapi
systemctl --user stop turntoapi
systemctl --user status turntoapi
```

Linux kill switch:

```bash
~/.local/share/turntoapi/kill-turntoapi.sh
```

Windows:

```powershell
& "$env:LOCALAPPDATA\TurnToAPI\start-turntoapi.ps1"
& "$env:LOCALAPPDATA\TurnToAPI\kill-turntoapi.ps1"
```

The kill scripts target TurnToAPI server processes. They do not intentionally kill unrelated Firefox sessions.

## Configuration

The installer copies:

```text
config.example.yaml -> config.yaml
```

Important defaults:

- Arena browser is headed.
- Browser launch is on demand.
- Generation timeout is 15 minutes.
- Human verification timeout is 15 minutes.
- Public wildcard binding is disabled.
- ChatGPT browser mode is present but disabled by default because the Arena path is the primary tested workflow.

## Project layout

```text
turn_to_api_server.py       OpenAI-compatible FastAPI server + live SSE
browser_web_adapter.py      Persistent Playwright browser worker
arena_model_registry.py     Arena registry storage/helpers
arena_models_live.json      Initial Arena model catalogue
turn_to_api_live_proxy.py   Compatibility reverse proxy for older layouts
config.example.yaml         Safe default configuration
install.sh                  Linux one-shot installer
install-windows.ps1         Windows installer
install-macos.sh            macOS installer
scripts/                    start/kill helpers
```

## Current status

This repository is the portable TurnToAPI build derived from the working NOVA prototype. Browser-backed sites can change their DOM without notice, so selectors may require maintenance over time.

The Arena flow is the primary target. ChatGPT web mode is experimental.

## Security

Do not expose this service directly to the public Internet. Use loopback, LAN controls, or Tailscale. Browser profiles can contain authenticated session state and are excluded by `.gitignore`.
