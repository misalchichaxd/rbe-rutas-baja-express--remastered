"""Diagnostico de voz Django <-> ELIPSE Engine.
Uso (desde la carpeta de tu proyecto Django, donde esta el .env):
    python probar_voz.py
No imprime tus llaves."""
import json, os, time, urllib.request, urllib.error

def leer_env(ruta=".env"):
    env = {}
    with open(ruta, encoding="utf-8") as f:
        for linea in f:
            linea = linea.strip()
            if linea and not linea.startswith("#") and "=" in linea:
                k, v = linea.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env

env = leer_env()
URL = env["ELIPSE_ENGINE_URL"].rstrip("/")
KEY = env["ELIPSE_API_KEY"]

def llamar(metodo, ruta, cuerpo=None, binario=False):
    data = json.dumps(cuerpo).encode() if cuerpo is not None else None
    req = urllib.request.Request(URL + ruta, data=data, method=metodo,
        headers={"Authorization": "Bearer " + KEY, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            raw = r.read()
            return r.status, (raw if binario else json.loads(raw.decode() or "null")), r.headers
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")[:300], None

print("1) Estado de voz del Engine (/v1/voice/status)")
st, body, _ = llamar("GET", "/v1/voice/status")
print("   HTTP", st)
if isinstance(body, dict):
    print("   ready:", body.get("ready"), "| enabled:", body.get("enabled"), "| mode:", body.get("mode"))
    for nombre, info in (body.get("providers") or {}).items():
        print("   proveedor", nombre, "->", info)
else:
    print("  ", body)

print("\n2) Pregunta con speak=true (igual que hace tu Django)")
st, resp, _ = llamar("POST", "/v1/chat", {"message": "hola, di una frase corta", "speak": True})
print("   HTTP", st)
task_id = resp.get("task_id") if isinstance(resp, dict) else None
if not task_id:
    raise SystemExit("   No hubo task_id: " + str(resp))
for _ in range(90):
    time.sleep(1)
    st, data, _ = llamar("GET", "/v1/task/" + task_id)
    if isinstance(data, dict) and data.get("status") != "en_progreso":
        break
res = (data or {}).get("result") or {}
print("   status:", data.get("status"))
print("   result.voice:", res.get("voice"))

if not res.get("voice"):
    print("\n>>> El Engine NO ofrecio voz para esta tarea. Por eso tu navegador nunca pide el audio.")
    print("    Causas tipicas: (a) el techo/caps de la llave tiene voice.allowed=false,")
    print("    (b) no hay proveedor de voz usable (Fish sin API key y TTS local apagado).")
else:
    print("\n3) Pidiendo el audio (/v1/voice/<id>/stream)")
    st, audio, hdr = llamar("GET", "/v1/voice/" + task_id + "/stream", binario=True)
    print("   HTTP", st, "| bytes:", len(audio) if isinstance(audio, bytes) else audio,
          "| proveedor:", hdr.get("X-Voice-Provider") if hdr else None)
