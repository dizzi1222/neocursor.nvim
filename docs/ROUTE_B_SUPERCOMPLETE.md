# Ruta B — Sidecar de Supercomplete (Tab de Antigravity) · PLAN

**Objetivo**: que `host = "antigravity"` en neocursor produzca sugerencias inline
(Supercomplete / Tab-to-Jump) desde el backend de Google Antigravity, igual que
`sidecar.py` lo hace con Cursor. Hasta que esto exista, **el tab es SIEMPRE de
Cursor** (fallback) — no romper eso estándolo.

## Estado actual (verificado)

- El motor Lua de neocursor es **agnóstico al host**: habla stdio JSON neutro
  `{edits, prediction}` con un sidecar (`sidecar.py` por defecto).
- El fork ya tiene la opción `host` ("cursor" | "antigravity") con fallback
  defensivo → si no existe `sidecar_antigravity.py`, usa Cursor y avisa WARN.
- Descubrí el backend del CLI (MÉTODO correcto, estilo `:método` en HTTP/1.1):
  - `POST https://daily-cloudcode-pa.googleapis.com/v1internal:loadCodeAssist` (200)
  - `POST     .../v1internal:retrieveUserQuotaSummary` (200, ya usado por <leader>C)
  - `POST     .../v1internal:listExperiments`, `:setUserSettings`
  - Headers: `Authorization: Bearer <token del CLI>` + `User-Agent: antigravity/cli/1.1.4` + JSON.
  - Token real del CLI: **no** está en `~/.gemini/oauth_creds.json` (401); se
    captura por MITM (`capture_anty_token.sh` → `~/.config/nvim/antigravity_token`, 0600).
- Hosts que usa el CLI: `daily-cloudcode-pa.googleapis.com`, `play.googleapis.com`,
  `antigravity-unleash.goog`, `oauth2.googleapis.com`.

## Bloqueador 1 (lo que falla hoy)

`loadCodeAssist` rechaza mi body con: `Unknown name "currentFile"` (400 INVALID_ARGUMENT).
El request real lo construye el IDE con un descriptor protobuf:
`resources/app/out/main.js` → `makePostRequest("v1internal:loadCodeAssist", sne, Rr(sne, {...}))`.
El schema (campos exactos) está en `sne`/`fFe` (proto3json del descriptor) — **opaco
por inspección estática**.

## Pasos para superar el bloqueador (captura del request REAL del IDE)

1. **Arrancar el IDE con el MITM** (sesión GUI dedicada, ~30-45 min):
   - `~/.local/bin/antigravity` (wrapper → `~/.antigravity/runtime`, nix store app).
   - Con el proxy ya probado: `nix shell nixpkgs#openssl` → CA/leaf SAN
     `daily-cloudcode-pa.googleapis.com` → proxy python ssl (ALPN h1, túnel
     transparente para otros hosts, log de REQ LÍNEA/headers/body a archivo).
   - `HTTPS_PROXY=http://127.0.0.1:8443 SSL_CERT_FILE=<ca.pem> antigravity`.
2. **Escribir 1 línea en un archivo** (dispara Supercomplete) y detener la captura.
3. Del log extraer el JSON del request de `:loadCodeAssist` (o del método de
   sugerencia que use el IDE — anotar si difiere, p. ej. `:generateCode`/otro):
   campos, orden, shapes (currentFile? contents? cursorPosition? context?).
4. Probar el mismo JSON con curl/python contra `:loadCodeAssist` con el token de
   `antigravity_token` → si 200 devuelve `edits`/completion → schema mapeado.

## Implementación del sidecar

5. `sidecar_antigravity.py` (repo del fork, New):
   - Mismo stdio JSON que `sidecar.py` (lee `{path,content,line,col,...}`).
   - Construye el request real capturado (paso 3) y hace
     `POST /v1internal:loadCodeAssist` con `Authorization: Bearer <token>` del
     archivo `antigravity_token` (usar `os.path.expanduser("~")`).
   - Mapea la respuesta a `{edits:[{text,range:{start,endInclusive}}]}` neutras
     (igual regla que sidecar.py: a partir de rangos lineales).
   - Prediction/tab-to-jump: si la respuesta trae target, mapearlo; si no, `null`
     (el salto predictivo queda desactivado hasta confirmarlo).
   - Errores: si no hay token o 401 → `{"error": ...}` con mensaje claro
     ("corré capture_anty_token.sh").
6. Wire del fork: ya soporta `sidecar_antigravity.py` (busca ese nombre). Commit.
7. Spec: `host = "antigravity"` recién cuando el sidecar esté probado.

## Riesgos / notas

- Ruta puede variar si el IDE usa un método distinto (mismísimo host), o el
  streaming llega por `:generateCode`/chunked — capturarlo también.
- El token del CLI expira (~1h): el sidecar debe avisar 401 y apuntar al
  capturador. Futuro: refresh automático leyendo el token-source real del CLI
  (pendiente de ubicarlo; hoy oauth_creds da 401).
- Cada corrida del CLI/IDE quema cuota mínima; mantener capturas cortas.
- La shell de esta herramienta se cuelga si agy queda en background: matar con
  `pkill -9 -f "bin/agy"` y evitar lanzarlo sin redirigir a archivos.

## Ruta 2 (ACTUAL — el cloud NO expone el tab)

Descubierto: `POST /v1internal:tabChat?alt=sse` da **404 "entity not found"** en
el cloud público (daily/cloudcode-pa). Prior art confirma que el tab vive en el
**Language Server LOCAL** del IDE (ConnectRPC, h2, CSRF):
- LS binario: `~/.antigravity/runtime/.../resources/app/extensions/antigravity/bin/language_server_linux_x64`
- Referencias: `jkfujinami/antigravity-client` (conecta al LS; ofusca el método
  del tab → lo llama `"n"`), `jkfujinami/antigravity-grpc-schemas`
  (`JetskiService.TabChat[T]` = `nRequest/nResponse`), `ljw1004/antigravity-trace`.
- Auth: token permanente YA resuelto → `~/.config/nvim/anty_oauth.json`
  (refresh_token + client_id/client_secret del IDE; `invalid_grant` = doble
  encoding, hay que `urllib.parse.unquote` el refresh antes de urlencode).
  `loadCodeAssist` OK → `cloudaicompanionProject: aicode-consumers`.
- Pasos: lanzar el LS standalone (como `antigravity-client.launch`), descubrir
  port+CSRF desde el extension/server, conectar ConnectRPC →
  `JetskiService.TabChat` streaming JSON, mapear `delta_text`/edits al protocolo
  neutro. Faltan: conectar el LS a la cuenta (env/argv) y el shape del stream.

## Definición de "listo"

`host = "antigravity"` + escribir en un buffer → aparece ghost/diff del backend
de Antigravity; Tab acepta, Esc descarta, `:NeocursorLog` muestra
`sidecar ● ready · antigravity` SIN el WARN de fallback. Cursor queda solo como
opción `host = "cursor"`.